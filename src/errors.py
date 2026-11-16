"""
errors.py — friendly error pages, and JSON errors for API calls.

Without these, Flask shows its bare "Internal Server Error" page. A 500 is
logged with the request ID that the page shows, so a studio owner can quote
the reference and the matching traceback can be found in logs/app.log.
"""

from __future__ import annotations

import logging

from flask import g, jsonify, make_response, render_template, request
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)

MESSAGES = {
    400: ("Something was wrong with that request", "Please go back and try again."),
    403: ("You can't do that here", "Your account doesn't have access to this page, or the form expired. "
                                    "Go back, refresh the page and try again."),
    404: ("Page not found", "This page doesn't exist, or the link has expired."),
    405: ("That action isn't allowed here", "Please go back and use the buttons on the page."),
    410: ("This link is no longer active", "Ask the studio for a new link."),
    413: ("That file is too large", "Upload a smaller file (up to 16 MB)."),
    429: ("Too many attempts", "Please wait a little and try again."),
    500: ("Something went wrong on our side", "The problem has been logged. Please try again in a moment."),
    503: ("Temporarily unavailable", "Please try again in a moment."),
}


def _wants_json() -> bool:
    return (request.is_json or request.path.startswith(("/api/", "/healthz"))
            or "/api/" in request.path
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.accept_mimetypes.best == "application/json")


def _respond(code: int, detail: str = ""):
    title, message = MESSAGES.get(code, MESSAGES[500] if code >= 500 else MESSAGES[400])
    message = detail or message
    if _wants_json():
        return jsonify({"error": title, "status": code}), code
    try:
        return render_template("error.html", code=code, title=title, message=message,
                               request_id=getattr(g, "request_id", "")), code
    except Exception:  # noqa: BLE001 — never fail while reporting a failure
        return f"{code} — {title}", code


def install(app) -> None:

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        # Routes that return their own text for an abort keep it (e.g. 410 messages).
        if exc.code is None or exc.code < 400:
            return exc
        return _respond(exc.code)

    @app.after_request
    def _dress_plain_errors(response):
        """Many routes `return "Estimate not found", 404`. Show that text on the error page."""
        if response.status_code < 400 or response.mimetype != "text/html" or response.direct_passthrough:
            return response
        text = response.get_data(as_text=True).strip()
        if not text or len(text) > 300 or "<" in text or _wants_json():
            return response
        code = response.status_code
        title, _ = MESSAGES.get(code, MESSAGES[500] if code >= 500 else MESSAGES[400])
        try:
            html = render_template("error.html", code=code, title=title, message=text,
                                   request_id=getattr(g, "request_id", ""))
        except Exception:  # noqa: BLE001
            return response
        dressed = make_response(html, code)
        for name, value in response.headers.items():       # keep security headers, cookies…
            if name.lower() == "set-cookie":
                dressed.headers.add(name, value)
            elif name.lower() not in ("content-length", "content-type"):
                dressed.headers[name] = value
        return dressed

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        if isinstance(exc, HTTPException):
            return _http_error(exc)
        logger.exception("Unhandled error on %s %s (request %s)", request.method, request.path,
                         getattr(g, "request_id", ""))
        return _respond(500)
