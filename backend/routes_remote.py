"""Phone remote UI routes and login sessions."""

import html
import secrets
import threading
import time

from flask import Blueprint, make_response, redirect, request

from jobs import JOBS
from logging_setup import log
from settings import STATE
from api import _safe_digest_eq

remote_bp = Blueprint("routes_remote", __name__)


# /remote login sessions: random session id -> expiry timestamp. Held in
# memory only, so every app restart logs remote devices out.
_REMOTE_COOKIE = "vg_remote"
_REMOTE_SESSION_TTL = 12 * 3600
_REMOTE_SESSIONS = {}
_REMOTE_SESSION_LOCK = threading.Lock()


def _new_remote_session():
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with _REMOTE_SESSION_LOCK:
        for k, exp in list(_REMOTE_SESSIONS.items()):
            if exp <= now:
                del _REMOTE_SESSIONS[k]
        _REMOTE_SESSIONS[sid] = now + _REMOTE_SESSION_TTL
    return sid


def _valid_remote_session(sid):
    if not sid:
        return False
    with _REMOTE_SESSION_LOCK:
        exp = _REMOTE_SESSIONS.get(sid)
        if exp is None:
            return False
        if exp <= time.time():
            del _REMOTE_SESSIONS[sid]
            return False
    return True

_REMOTE_CSS = ("body{font:14px sans-serif;padding:12px}"
               "table{border-collapse:collapse;width:100%}"
               "td,th{border:1px solid #ccc;padding:6px;text-align:left}"
               "form{display:inline;margin:0}button{margin-right:4px}")


def _remote_page(body, status=200):
    resp = make_response(
        "<!doctype html><meta name=viewport content=\"width=device-width\">"
        f"<title>Video Grabber</title><style>{_REMOTE_CSS}</style>{body}", status)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


def _remote_login_page(error="", status=200):
    msg = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
    return _remote_page(
        "<h2>Video Grabber</h2>"
        f"{msg}"
        "<form method='POST' action='/remote-login'>"
        "<p>Enter the API token from the app's settings to continue.</p>"
        "<input type='password' name='token' autocomplete='off' autofocus required "
        "style='width:100%;max-width:360px;padding:6px'> "
        "<button type='submit'>Log in</button></form>", status)


@remote_bp.route("/remote-login", methods=["POST"])
def remote_login():
    supplied = request.form.get("token", "")
    real = STATE.get("api_token", "")
    if not supplied or not real or not _safe_digest_eq(supplied, real):
        log(f"remote login rejected from {request.remote_addr}")
        time.sleep(0.5)   # blunt brute-force speed bump
        return _remote_login_page("Wrong token.", 401)
    log(f"remote login accepted from {request.remote_addr}")
    resp = redirect("/remote", code=303)
    # HttpOnly + SameSite=Strict always. Secure only over HTTPS: the LAN UI
    # is plain HTTP, and browsers silently drop Secure cookies on http://.
    resp.set_cookie(_REMOTE_COOKIE, _new_remote_session(),
                    max_age=_REMOTE_SESSION_TTL, httponly=True,
                    samesite="Strict", secure=request.is_secure)
    return resp


def _remote_action_form(action, jid):
    # Plain POST form (works without JS). The inline handler just posts in
    # the background and reloads, so the page doesn't navigate to raw JSON.
    return (f"<form method='POST' action='/{action}/{html.escape(str(jid), quote=True)}' "
            "onsubmit=\"fetch(this.action,{method:'POST',credentials:'same-origin'})"
            ".then(function(){location.reload()});return false\">"
            f"<button type='submit'>{action}</button></form>")


@remote_bp.route("/remote", methods=["GET"])
def remote_ui():
    if not _valid_remote_session(request.cookies.get(_REMOTE_COOKIE)):
        return _remote_login_page()
    rows = "".join(
        f"<tr><td>{html.escape(str(j.get('filename') or ''))}</td>"
        f"<td>{html.escape(str(j.get('status') or ''))}</td>"
        f"<td>{_remote_action_form('pause', jid)}"
        f"{_remote_action_form('resume', jid)}"
        f"{_remote_action_form('stop', jid)}</td></tr>"
        for jid, j in list(JOBS.items())
    )
    return _remote_page(
        "<h2>Video Grabber</h2><table>"
        f"<tr><th>File</th><th>Status</th><th>Actions</th></tr>{rows}</table>")
