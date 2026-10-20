"""Application security boundaries for customer sessions and private media."""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from flask import abort, g, request, session, send_file
from .config import Config
from .db_manager import get_db


def safe_next(value,default='/dashboard'):
    if not isinstance(value,str) or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c)<32 for c in value):
        return default
    parsed=urlsplit(value)
    return default if parsed.scheme or parsed.netloc else value


def authorize_job(job,customer_visible=False):
    if not job or not session.get('logged_in') or job['studio_id']!=session.get('studio_id'):abort(404)
    role=session.get('role')
    if role in ('admin','general_manager','service_advisor'):return
    if role=='customer':
        if not customer_visible or job['customer_id']!=session.get('user_id'):abort(404)
        vehicle=get_db().execute('SELECT id FROM vehicles WHERE studio_id=? AND id=? AND customer_id=?',(job['studio_id'],job['vehicle_id'],session['user_id'])).fetchone()
        if not vehicle:abort(404)
        return
    if role in ('technician','photographer') and job['assigned_staff_id']==session.get('staff_id'):return
    abort(403)


def private_media(studio_id,filename):
    relative=Path(filename)
    if relative.suffix.lower() not in ('.png','.jpg','.jpeg','.gif','.webp','.mp4','.mov','.avi'):abort(404)
    if relative.is_absolute() or '..' in relative.parts or '\\' in filename:abort(404)
    if not session.get('logged_in') or session.get('studio_id')!=studio_id:abort(404)
    conn=get_db();job=None;visible=False
    if len(relative.parts)==1:
        media=conn.execute('SELECT * FROM media WHERE studio_id=? AND filename=?',(studio_id,filename)).fetchone()
        if media:
            job=conn.execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(studio_id,media['job_id'])).fetchone()
            visible=bool(media['customer_visible'])
    elif len(relative.parts)==2 and relative.parts[0]=='dvi':
        job=conn.execute('''SELECT j.* FROM dvi_photos p JOIN dvi_inspections i ON i.id=p.inspection_id AND i.studio_id=p.studio_id
            JOIN jobs j ON j.id=i.job_id AND j.studio_id=i.studio_id WHERE p.studio_id=? AND p.filename=?''',(studio_id,relative.name)).fetchone()
    elif len(relative.parts)==2 and relative.parts[0]=='inspection':
        # Legacy panel arrays contain URLs. Require an exact reference, never a filename prefix guess.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='panel_inspections'").fetchone():
            for panel in conn.execute('SELECT * FROM panel_inspections WHERE studio_id=?',(studio_id,)):
                values=[]
                for key in ('images','after_images'):
                    if key in panel.keys():
                        try:values+=json.loads(panel[key] or '[]')
                        except (ValueError,TypeError):pass
                if any(v in (f'/static/uploads/studio_{studio_id}/{filename}',f'/uploads/studio_{studio_id}/{filename}') for v in values):
                    job=conn.execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(studio_id,panel['job_id'])).fetchone();break
    authorize_job(job,visible)
    roots=[Path(Config.PRIVATE_UPLOAD_ROOT)]
    if not Config.PRODUCTION:roots.append(Path(Config.STATIC_FOLDER)/'uploads')
    for root in roots:
        studio_root=(root/f'studio_{studio_id}').resolve()
        path=studio_root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(studio_root):abort(404)
        if path.is_file():
            response=send_file(path,conditional=True)
            response.headers['Cache-Control']='private, no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            return response
    abort(404)


def install_security(app):
    @app.before_request
    def boundary():
        g.request_id=uuid.uuid4().hex
        if request.path.startswith('/customer/tracking/'):
            return '<h1>This tracking link has been replaced</h1><p>Ask your studio for a new private job tracking link. No account is needed.</p>',410
        if request.path.endswith('/send-tracking-sms'):
            return 'Legacy tracking messages are disabled. Use the reviewed follow-up workflow.',410
        if request.endpoint=='static':
            target=(Path(Config.STATIC_FOLDER)/(request.view_args or {}).get('filename','')).resolve()
            uploads=(Path(Config.STATIC_FOLDER)/'uploads').resolve()
            if target.is_relative_to(uploads):
                relative=target.relative_to(uploads).as_posix()
                private=re.fullmatch(r'studio_(\d+)/(.+)',relative)
                if not private:abort(404)
                return private_media(int(private[1]),private[2])
        match=re.fullmatch(r'/(?:static/)?uploads/studio_(\d+)/(.+)',request.path)
        if match:return private_media(int(match[1]),match[2])
        if request.endpoint=='dvi.serve_photo':
            return private_media(request.view_args['studio_id'],'dvi/'+request.view_args['filename'])
        if session.get('role')=='customer' and session.get('logged_in'):
            allowed=request.endpoint in ('main.login','main.logout','static','approvals.public','portal_admin.public_tracking','portal_admin.public_status') or str(request.endpoint).startswith('portal.')
            if not allowed:abort(403)
        # Existing media mutation paths now require both CSRF and job assignment.
        if request.method=='POST' and (request.path.startswith('/media/') or '/inspection-upload/' in request.path or re.fullmatch(r'/dvi/inspection/\d+/photos',request.path)):
            from .routes.job_record_routes import verify_csrf
            verify_csrf()
            jid=(request.view_args or {}).get('job_id') or request.form.get('job_id')
            if request.endpoint=='main.delete_media':
                media=get_db().execute('SELECT job_id FROM media WHERE studio_id=? AND id=?',(session.get('studio_id'),request.view_args['media_id'])).fetchone()
                jid=media['job_id'] if media else None
            if request.endpoint=='dvi.upload_photos':
                inspection=get_db().execute('SELECT job_id FROM dvi_inspections WHERE studio_id=? AND id=?',(session.get('studio_id'),request.view_args['inspection_id'])).fetchone()
                jid=inspection['job_id'] if inspection else None
            authorize_job(get_db().execute('SELECT * FROM jobs WHERE studio_id=? AND id=?',(session.get('studio_id'),jid)).fetchone())

    @app.after_request
    def headers(response):
        response.headers['X-Request-ID']=getattr(g,'request_id','')
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers.setdefault('Referrer-Policy','same-origin')
        response.headers.setdefault('X-Frame-Options','DENY')
        if session.get('logged_in'):response.headers['Cache-Control']='no-store'
        if Config.PRODUCTION:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @app.get('/healthz')
    def health():
        try:get_db().execute('SELECT 1').fetchone()
        except Exception:return {'status':'unavailable'},503
        return {'status':'ok'},200


# ── Added with invoices / Studio Assistant ────────────────────────────────────
# Login throttling, safe redirects for form-supplied targets, and two request
# checks the boundary above doesn't make: a cross-site Origin/Referer check on
# every state-changing request, and re-validating the account behind a session.

import logging as _logging
import threading as _threading
import time as _time
from typing import Dict as _Dict, List as _List, Optional as _Optional, Tuple as _Tuple

from flask import redirect as _redirect, url_for as _url_for

_log = _logging.getLogger(__name__)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def safe_redirect_target(target: _Optional[str], fallback: str) -> str:
    """`target` only if it is a same-site relative path, else `fallback`."""
    result = safe_next(target or "", default="")
    return result or fallback


class LoginThrottle:
    """
    In-memory failed-login tracker keyed by (client IP, username).

    Per-process by design: this is a single-process app. Under several
    workers, move it into the database or Redis.
    """

    def __init__(self, max_attempts: int, window_seconds: int, lockout_seconds: int):
        self.max_attempts = max(1, int(max_attempts))
        self.window = int(window_seconds)
        self.lockout = int(lockout_seconds)
        self._failures: _Dict[_Tuple[str, str], _List[float]] = {}
        self._locked_until: _Dict[_Tuple[str, str], float] = {}
        self._lock = _threading.Lock()

    @staticmethod
    def key(ip: str, username: str) -> _Tuple[str, str]:
        return (ip or "?", (username or "").strip().lower())

    def locked_for(self, key) -> int:
        with self._lock:
            remaining = int(self._locked_until.get(key, 0) - _time.time())
            if remaining <= 0:
                self._locked_until.pop(key, None)
                return 0
            return remaining

    def record_failure(self, key) -> None:
        now = _time.time()
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if now - t < self.window] + [now]
            self._failures[key] = recent
            if len(recent) >= self.max_attempts:
                self._locked_until[key] = now + self.lockout
                self._failures.pop(key, None)
                _log.warning("Login locked for %s after %d failures", key[1], len(recent))

    def record_success(self, key) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


_throttle: _Optional[LoginThrottle] = None


def login_throttle() -> LoginThrottle:
    global _throttle
    if _throttle is None:
        _throttle = LoginThrottle(Config.AUTH_MAX_LOGIN_ATTEMPTS, Config.AUTH_LOGIN_WINDOW_SECONDS,
                                  Config.AUTH_LOCKOUT_SECONDS)
    return _throttle


def remember_tracked_job(job_id: int) -> None:
    """Kept for the legacy tracking routes (now retired by the boundary above)."""
    jobs = list(session.get("tracked_jobs", []))
    if job_id not in jobs:
        session["tracked_jobs"] = (jobs + [int(job_id)])[-20:]


def _allowed_hosts() -> set:
    hosts = {request.host}
    if getattr(Config, "APP_BASE_URL", ""):
        hosts.add(urlsplit(Config.APP_BASE_URL).netloc)
    return hosts


def register_security(app) -> None:

    @app.before_request
    def _verify_same_origin():
        if request.method not in UNSAFE_METHODS:
            return None
        origin = request.headers.get("Origin")
        if origin is not None:
            if origin == "null" or urlsplit(origin).netloc not in _allowed_hosts():
                _log.warning("Blocked cross-site %s %s from origin %s", request.method, request.path, origin)
                abort(403)
            return None
        referer = request.headers.get("Referer")
        if referer and urlsplit(referer).netloc not in _allowed_hosts():
            _log.warning("Blocked cross-site %s %s from referer %s", request.method, request.path, referer)
            abort(403)
        return None

    @app.before_request
    def _revalidate_session():
        """Removing a staff member (or changing their role) takes effect on their next request."""
        if not session.get("logged_in") or request.endpoint == "static":
            return None
        table = {"admin": "studios", "staff": "staff", "customer": "customers"}.get(session.get("user_type"))
        row = get_db().execute(f"SELECT * FROM {table} WHERE id=?", (session.get("user_id"),)).fetchone() if table else None
        valid = bool(row)
        if valid and table != "studios":
            valid = row["studio_id"] == session.get("studio_id")
        if valid and table == "studios":
            valid = row["id"] == session.get("studio_id")
        if valid and table == "staff":
            valid = row["role"] == session.get("role")
        if not valid:
            _log.warning("Ending session for %s: account removed or changed", session.get("name"))
            session.clear()
            if request.method in UNSAFE_METHODS or request.is_json or request.path.startswith("/api"):
                abort(401)
            return _redirect(_url_for("main.login", next=request.path))
        return None

    if getattr(Config, "SECRET_KEY_IS_EPHEMERAL", False):
        _log.warning("SECRET_KEY is not set — using a random per-process key. Sessions reset on restart.")
