"""
security.py — cross-cutting request security, registered once in create_app().

Four things live here because every route needs them and none should
reimplement them:

1. **Cross-site request check.** Every state-changing request must come from
   our own pages. Browsers always attach `Origin` (or at least `Referer`) to a
   cross-site POST, so comparing it to our host blocks CSRF without touching a
   single template. Together with `SameSite=Lax` cookies this is the OWASP
   "verify origin" defence.

2. **Upload access.** Customer photos live under /static/uploads, which Flask
   would otherwise serve to anyone who can guess a path. One policy,
   `can_view_upload`, now decides for both that path and /uploads/…:
   staff see their own studio only; a customer sees only files that belong to
   a job they hold the tracking link for.

3. **Login throttling.** AUTH_MAX_LOGIN_ATTEMPTS / AUTH_LOGIN_WINDOW_SECONDS /
   AUTH_LOCKOUT_SECONDS were configured but never enforced.

4. **Safe redirects.** `?next=` and `redirect_to` may only point back into
   this app.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from flask import abort, request, session

logger = logging.getLogger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ── Safe redirects ────────────────────────────────────────────────────────────

def safe_redirect_target(target: Optional[str], fallback: str) -> str:
    """Return `target` only if it is a same-site relative path, else `fallback`."""
    if not target:
        return fallback
    target = target.strip()
    # Reject absolute URLs, scheme-relative "//evil.com", and backslash tricks
    # ("/\\evil.com") that some browsers normalise into a host.
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return fallback
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return fallback
    return target


# ── Login throttling ──────────────────────────────────────────────────────────

class LoginThrottle:
    """
    In-memory failed-login tracker keyed by (client IP, username).

    Per-process by design: this is a single-process app. If it ever runs under
    several workers, move this into the database or Redis — otherwise each
    worker grants its own allowance.
    """

    def __init__(self, max_attempts: int, window_seconds: int, lockout_seconds: int):
        self.max_attempts = max(1, int(max_attempts))
        self.window = int(window_seconds)
        self.lockout = int(lockout_seconds)
        self._failures: Dict[Tuple[str, str], List[float]] = {}
        self._locked_until: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(ip: str, username: str) -> Tuple[str, str]:
        return (ip or "?", (username or "").strip().lower())

    def locked_for(self, key: Tuple[str, str]) -> int:
        """Seconds remaining on a lockout, or 0."""
        with self._lock:
            until = self._locked_until.get(key, 0)
            remaining = int(until - time.time())
            if remaining <= 0:
                self._locked_until.pop(key, None)
                return 0
            return remaining

    def record_failure(self, key: Tuple[str, str]) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if now - t < self.window]
            recent.append(now)
            self._failures[key] = recent
            if len(recent) >= self.max_attempts:
                self._locked_until[key] = now + self.lockout
                self._failures.pop(key, None)
                logger.warning("Login locked for %s after %d failures", key[1], len(recent))

    def record_success(self, key: Tuple[str, str]) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


_throttle: Optional[LoginThrottle] = None


def login_throttle() -> LoginThrottle:
    global _throttle
    if _throttle is None:
        from .config import Config
        _throttle = LoginThrottle(
            getattr(Config, "AUTH_MAX_LOGIN_ATTEMPTS", 5),
            getattr(Config, "AUTH_LOGIN_WINDOW_SECONDS", 900),
            getattr(Config, "AUTH_LOCKOUT_SECONDS", 900),
        )
    return _throttle


# ── Customer tracking-link access ─────────────────────────────────────────────

def remember_tracked_job(job_id: int) -> None:
    """
    Record that this browser opened a valid tracking link for `job_id`.

    Customer pages load photos with plain <img> tags; this lets those requests
    be authorised by the session instead of putting the token in every URL.
    """
    jobs = list(session.get("tracked_jobs", []))
    if job_id not in jobs:
        jobs = (jobs + [int(job_id)])[-20:]
        session["tracked_jobs"] = jobs


_INSPECTION_PREFIX = re.compile(r"^(\d+)_")


def _job_for_upload(conn, studio_id: int, relpath: str) -> Optional[int]:
    """Work out which job an uploaded file belongs to, or None if unknown."""
    parts = relpath.split("/")
    filename = parts[-1]

    if len(parts) == 2 and parts[0] == "inspection":
        m = _INSPECTION_PREFIX.match(filename)
        return int(m.group(1)) if m else None

    if len(parts) == 2 and parts[0] == "dvi":
        row = conn.execute(
            "SELECT i.job_id FROM dvi_photos p JOIN dvi_inspections i ON p.inspection_id = i.id "
            "WHERE p.studio_id=? AND p.filename=?",
            (studio_id, filename),
        ).fetchone()
        return row["job_id"] if row else None

    if len(parts) == 1:
        row = conn.execute(
            "SELECT job_id FROM media WHERE studio_id=? AND filename=?", (studio_id, filename)
        ).fetchone()
        if row and row["job_id"]:
            return row["job_id"]
        row = conn.execute(
            "SELECT job_id FROM panel_inspections WHERE studio_id=? "
            "AND (before_photo=? OR after_photo=?)",
            (studio_id, filename, filename),
        ).fetchone()
        return row["job_id"] if row else None

    return None


def can_view_upload(studio_id: int, relpath: str) -> bool:
    """Single access policy for every customer-uploaded file."""
    from .db_manager import get_db

    if ".." in relpath.split("/"):
        return False

    role = session.get("role")
    if session.get("logged_in") and role and role != "customer":
        return session.get("studio_id") == studio_id

    conn = get_db()
    job_id = _job_for_upload(conn, studio_id, relpath)
    if job_id is None:
        return False

    if session.get("logged_in") and role == "customer":
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE id=? AND studio_id=? AND customer_id=?",
            (job_id, studio_id, session.get("user_id")),
        ).fetchone()
        return bool(row)

    if job_id in session.get("tracked_jobs", []):
        return True

    token = (request.args.get("token") or "").strip()
    if token:
        row = conn.execute(
            "SELECT id FROM jobs WHERE tracking_token=? AND studio_id=?", (token, studio_id)
        ).fetchone()
        return bool(row) and row["id"] == job_id
    return False


_STATIC_UPLOAD = re.compile(r"^/static/uploads/studio_(\d+)/(.+)$")


# ── Registration ──────────────────────────────────────────────────────────────

def _allowed_hosts() -> set:
    from .config import Config
    hosts = {request.host}
    base = getattr(Config, "APP_BASE_URL", "")
    if base:
        hosts.add(urlparse(base).netloc)
    return hosts


def register_security(app) -> None:

    @app.before_request
    def _verify_same_origin():
        if request.method not in UNSAFE_METHODS:
            return None
        origin = request.headers.get("Origin")
        if origin is not None:
            if origin == "null" or urlparse(origin).netloc not in _allowed_hosts():
                logger.warning("Blocked cross-site %s %s from origin %s",
                               request.method, request.path, origin)
                abort(403)
            return None
        referer = request.headers.get("Referer")
        if referer and urlparse(referer).netloc not in _allowed_hosts():
            logger.warning("Blocked cross-site %s %s from referer %s",
                           request.method, request.path, referer)
            abort(403)
        return None

    @app.before_request
    def _revalidate_session():
        """
        A session is only as good as the account behind it. Removing a staff
        member (or changing their role) must take effect on their very next
        request, not when their cookie happens to expire.
        """
        if not session.get("logged_in") or request.endpoint == "static":
            return None
        from .db_manager import get_db

        table = {"admin": "studios", "staff": "staff", "customer": "customers"}.get(session.get("user_type"))
        row = None
        if table:
            row = get_db().execute(
                f"SELECT * FROM {table} WHERE id=?", (session.get("user_id"),)
            ).fetchone()
        valid = bool(row)
        if valid and table != "studios":
            valid = row["studio_id"] == session.get("studio_id")
        if valid and table == "studios":
            valid = row["id"] == session.get("studio_id")
        if valid and table == "staff":
            valid = row["role"] == session.get("role")
        if not valid:
            logger.warning("Ending session for %s: account removed or changed", session.get("name"))
            session.clear()
            if request.method in UNSAFE_METHODS or request.is_json or request.path.startswith("/api"):
                abort(401)
            from flask import redirect, url_for
            return redirect(url_for("main.login", next=request.path))
        return None

    @app.before_request
    def _guard_static_uploads():
        m = _STATIC_UPLOAD.match(request.path)
        if m and not can_view_upload(int(m.group(1)), m.group(2)):
            abort(403)
        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    from .config import Config
    if getattr(Config, "SECRET_KEY_IS_EPHEMERAL", False):
        logger.warning("SECRET_KEY is not set — using a random per-process key. "
                       "Sessions will reset on restart. Set SECRET_KEY in .env.")
