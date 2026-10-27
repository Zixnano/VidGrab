"""Flask HTTP API: routes, auth, pairing, upload nonces."""

from pathlib import Path
import glob
import ipaddress
import os
import re
import requests
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from flask import Flask, jsonify, request, make_response, redirect
from flask_cors import CORS

from downloader import maybe_convert_to_mp4, _repair_recording, _probe_recording, _CREATE_NO_WINDOW, _WORKERS, queue_status
from engines import find_ffmpeg_dir
from engines import clear_youtube_cooldown
from settings import MAX_UPLOAD_BYTES
import queue
import html
from settings import APP_PORT, APP_VERSION, STATE, save_settings, safe_filename, guess_filename, _unique_path, _dest_for, stat_for, disk_usage_for, category_for
from jobs import reorder_job, transition, repair_status, JobEvent, JobPhase, set_phase, JOBS, new_job, save_jobs_snapshot, _fire_new_job_hooks, _fire_show_dialog_hooks

from logging_setup import log, LOG_QUEUE

app = Flask(__name__)
# Content-script fetches carry the PAGE's origin (not the extension's), so
# the direct recording upload from content.js needs broader CORS on /upload.
# Safe: /upload is gated by a one-time nonce that can only be obtained with
# the API token — a random web page can't forge one, so wider origins here
# expose nothing. Every other route stays extension-origin-only.
#
# The extension origin is pinned to ONE extension ID. The ID is fixed by the
# "key" field in extension/manifest.json (see the generate-key command in the
# release notes). Set it here, or override with the VG_EXTENSION_ID env var.
EXTENSION_ID = os.environ.get("VG_EXTENSION_ID", "").strip() or "REPLACE_WITH_EXTENSION_ID"
_EXT_ORIGIN = "chrome-extension://" + EXTENSION_ID
if not re.fullmatch(r"[a-p]{32}", EXTENSION_ID):
    log("api: EXTENSION_ID is not set to a valid 32-letter extension ID; "
        "the extension will be refused by CORS and /pair until it is set")
CORS(app, resources={
    r"/*": {"origins": [_EXT_ORIGIN]},
    r"/upload": {"origins": r".*"},
}, supports_credentials=False)

TOKEN_PROTECTED_PATHS = {
    "/download", "/batch", "/pause", "/resume", "/stop", "/delete",
    "/probe", "/jobs", "/stats", "/convert", "/logs",
    "/category", "/probe-head", "/settings", "/probe-formats",
    "/upload-token", "/redownload", "/show-add-dialog",
    "/clear-failed", "/queue-status", "/dup-check", "/favicon",
    "/update-status", "/update-skip", "/ytdlp-update", "/ytdlp-rollback",
    "/reorder", "/theme", "/queue-running", "/delete-file", "/toolkit",
    "/open-folder", "/diskspace",
}

# Routes that may be driven with a /remote login cookie.
# Everything else needs the real X-API-Token header.
_KEY_ALLOWED_PREFIXES = ("/pause/", "/resume/", "/stop/")


def _safe_digest_eq(a, b):
    """Constant-time string compare that never raises on non-ASCII input
    (secrets.compare_digest raises TypeError for non-ASCII str)."""
    try:
        return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))
    except Exception:
        return False

# One-time nonces for /upload: content scripts fetch the recording straight
# to the backend (bypassing the ~32MB chrome.runtime.sendMessage cap), but
# they can't read the long-lived API token. The nonce bridges that: issued
# via the token-authenticated /upload-token, consumed exactly once.
_UPLOAD_NONCES = {}  # nonce -> created_ts
_UPLOAD_NONCE_LOCK = threading.Lock()

def _issue_upload_nonce():
    nonce = secrets.token_hex(16)
    with _UPLOAD_NONCE_LOCK:
        now = time.time()
        for k, ts in list(_UPLOAD_NONCES.items()):
            if now - ts > 300:
                del _UPLOAD_NONCES[k]
        _UPLOAD_NONCES[nonce] = now
    return nonce

def _consume_upload_nonce(nonce):
    if not nonce:
        return False
    with _UPLOAD_NONCE_LOCK:
        ts = _UPLOAD_NONCES.pop(nonce, None)   # one-time: always removed
        if ts is None:
            return False
        if time.time() - ts > 300:             # expired: removed above, refuse
            return False
        return True

# Auto-pairing: one successful /pair per app session. The endpoint is
# deliberately NOT in TOKEN_PROTECTED_PATHS — it's guarded by the
# localhost-only check and this once-per-session flag instead.
# Mutated only by routes_pair.pair(). Always read via
# `api._PAIR_GRANTED` (attribute access), never via
# `from api import _PAIR_GRANTED` -- a copied bool would go
# stale on the first pairing.
_PAIR_GRANTED = False

_LOCAL_HOSTS = ("127.0.0.1", "localhost")


def _host_only(raw):
    """Host header -> lowercase hostname without the port ('' if malformed)."""
    h = (raw or "").strip().lower()
    if h.startswith("["):                       # [::1]:5757
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    return h.partition(":")[0]


@app.before_request
def _require_local_host():
    """DNS-rebinding guard: only answer requests addressed to 127.0.0.1 or
    localhost. A rebinding page reaches us under ITS hostname, so the Host
    header gives it away. With LAN access switched on the phone UI is reached
    by IP address, so raw IP literals are also accepted then (rebinding needs
    a hostname, never an IP literal). Single-label and .local names are
    accepted in that mode too, for phones that use the PC name."""
    host = _host_only(request.headers.get("Host", ""))
    if host in _LOCAL_HOSTS:
        return
    if STATE.get("lan_access") and host:
        try:
            ipaddress.ip_address(host)
            return
        except ValueError:
            pass
        # PC names on the home network (DESKTOP-X, mypc.local) have no dots
        # or end in .local; an attacker's rebinding domain is a public
        # multi-label name, so it still fails here.
        if "." not in host or host.endswith(".local"):
            return
    log(f"rejected request with Host header {request.headers.get('Host', '')!r} "
        f"from {request.remote_addr} for {request.path}")
    return jsonify({"error": "bad host"}), 403


@app.before_request
def _require_api_token():
    if request.method == "OPTIONS":
        return
    # /upload accepts a one-time nonce instead of the long-lived token,
    # because content scripts can't read chrome.storage.local.
    if request.path == "/upload":
        if _consume_upload_nonce(request.headers.get("X-Upload-Nonce", "")):
            return
        return jsonify({"error": "missing or invalid upload nonce"}), 401
    if request.path not in TOKEN_PROTECTED_PATHS and not any(
        request.path.startswith(p + "/") for p in TOKEN_PROTECTED_PATHS
    ):
        return
    # The /remote login cookie unlocks ONLY pause / resume / stop.
    # Every other protected route (jobs, settings, delete, ...) needs the
    # real token header.
    if request.path.startswith(_KEY_ALLOWED_PREFIXES):
        if request.method == "POST" and _valid_remote_session(
                request.cookies.get(_REMOTE_COOKIE)):
            return
    supplied = request.headers.get("X-API-Token", "")
    real = STATE.get("api_token", "")
    if not supplied or not real or not _safe_digest_eq(supplied, real):
        return jsonify({"error": "missing or invalid API token"}), 401

def cleanup_partial_files(job):
    try:
        dest = _dest_for(job)
    except Exception as e:
        log(f"cleanup_partial_files: cannot resolve destination: {e}")
        return
    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists():
        try:
            part.unlink()
        except OSError as e:
            log(f"cleanup_partial_files: could not delete {part.name}: {e}")
    if dest.parent.exists():
        for sibling in dest.parent.glob(glob.escape(dest.name) + ".part*"):
            try:
                sibling.unlink()
            except OSError as e:
                log(f"cleanup_partial_files: could not delete {sibling.name}: {e}")

# ---- Delete job + file ----------------------------------------------------
def _allowed_roots():
    """The only folder the app may delete from or read toolkit sources from:
    the root VideoGrabber directory (settings.HOME). Nothing else."""
    from settings import HOME
    try:
        return [Path(HOME).resolve()]
    except (OSError, ValueError):
        return []


def _safe_in_roots(path):
    """Resolved Path when `path` lies strictly inside an allowed root (symlinks
    and ".." resolved first), else None. A root folder itself never passes."""
    try:
        rp = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for root in _allowed_roots():
        try:
            if rp != root and rp.is_relative_to(root):
                return rp
        except ValueError:
            continue
    return None


# ---- Media toolkit (ffmpeg): /toolkit/compress, /toolkit/trim, /toolkit/gif ----
_TOOLKIT_TIMEOUT = 7200


def _ff_bins():
    d = find_ffmpeg_dir()
    if d:
        return str(Path(d) / "ffmpeg.exe"), str(Path(d) / "ffprobe.exe")
    return "ffmpeg", "ffprobe"


def _parse_clock(value, name):
    """'90', '1:30', '00:01:30.5' -> seconds (float). Rejects anything else so
    nothing user-typed can ever reach ffmpeg as an option."""
    s = str(value if value is not None else "").strip()
    parts = s.split(":")
    if not s or len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ValueError(f"{name} must look like 90, 1:30 or 00:01:30")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def _toolkit_source(data):
    """Source file from a finished job id, or a path inside the allowed folders."""
    jid = data.get("job_id")
    if jid:
        job = JOBS.get(jid)
        if not job:
            raise ValueError("unknown job")
        cands = [Path(job["dest"])] if job.get("dest") else []
        try:
            cands.append(_dest_for(job))
        except Exception as e:
            log(f"toolkit: cannot resolve destination for job {jid}: {e}")
        for c in cands:
            if c.is_file():
                return c
        raise ValueError("the job's file is missing on disk")
    raw = data.get("path")
    if not raw:
        raise ValueError("no source file given")
    p = _safe_in_roots(raw)
    if p is None or not p.is_file():
        raise ValueError("source must be an existing file inside the VideoGrabber / download folders")
    return p


def _toolkit_out(src, tag, ext):
    # Toolkit exports (trim / compress / gif) go to <VideoGrabber root>/Edited,
    # i.e. Downloads/VideoGrabber/Edited, not next to the source file. The root
    # is settings.HOME, the same folder _allowed_roots() trusts, so the outputs
    # stay valid toolkit sources and deletable from the app.
    try:
        from settings import HOME
        root = Path(HOME)
    except Exception:
        root = Path.home() / "Downloads" / "VideoGrabber"
    out_dir = root / "Edited"
    out_dir.mkdir(parents=True, exist_ok=True)
    return _unique_path(out_dir / safe_filename(f"{src.stem} ({tag}).{ext.lstrip('.')}"))


def _probe_duration(src):
    _, ffprobe = _ff_bins()
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(src)],
                       capture_output=True, text=True, errors="replace", timeout=60,
                       creationflags=_CREATE_NO_WINDOW)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        raise ValueError("couldn't read the video's duration")


def _run_toolkit(cmd, out, label):
    log(f"toolkit {label}: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=_TOOLKIT_TIMEOUT, creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        return jsonify({"error": "ffmpeg timed out"}), 500
    except OSError as e:
        return jsonify({"error": f"couldn't start ffmpeg: {e}"}), 500
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        tail = (r.stderr or "").strip().splitlines()[-3:]
        log(f"toolkit {label} failed (rc={r.returncode}): {' | '.join(tail)}")
        return jsonify({"error": "ffmpeg failed: " + " ".join(tail)[:300]}), 500
    log(f"toolkit {label}: wrote {out.name}")
    return jsonify({"ok": True, "output": str(out), "size": out.stat().st_size})

def _probe_headers(data):
    """Cookie/Referer/User-Agent from the extension for the format probe.
    Strings only, CR/LF stripped (no header injection), length-capped."""
    out = {}
    for key, name, limit in (("cookie", "Cookie", 32768),
                             ("referer", "Referer", 2048),
                             ("user_agent", "User-Agent", 512)):
        v = data.get(key)
        if not isinstance(v, str):
            continue
        v = v.replace("\r", "").replace("\n", "").strip()[:limit]
        if v:
            out[name] = v
    return out


def _log_probe_headers(tag, headers):
    """Names + shape only: never log full cookie values."""
    if not headers:
        return
    parts = []
    for name, value in sorted(headers.items()):
        if name == "Cookie":
            parts.append(f"Cookie(len={len(value)},prefix={value[:8]})")
        else:
            parts.append(f"{name}(len={len(value)})")
    log(f"{tag}: forwarding headers {', '.join(parts)}")


def _probe_int(data, key, default):
    """Optional integer request field. Returns (value, ok); bool, float and
    string values are rejected rather than coerced."""
    v = data.get(key)
    if v is None:
        return default, True
    if isinstance(v, bool) or not isinstance(v, int):
        return default, False
    return v, True


def _probe_error(message, code, status):
    return jsonify({"error": message, "code": code}), status

# Extensions /upload treats as media: repaired (ffmpeg) and validated
# (ffprobe) like a screen recording. Anything else (zip, js, pdf, ...) from
# the blob-download hook is saved to its category folder and registered as-is.
_MEDIA_UPLOAD_EXTS = {".webm", ".mp4", ".mkv", ".mov", ".m4v", ".avi", ".flv"}


def _norm_url_key(url):
    """Compare URLs ignoring fragments; YouTube links by video id."""
    try:
        u = urlparse(url or "")
        host = (u.hostname or "").lower()
        if host == "youtu.be":
            return "yt:" + u.path.strip("/")
        if host.endswith("youtube.com"):
            m = re.search(r"(?:^|&)v=([\w-]{11})", u.query or "")
            if m:
                return "yt:" + m.group(1)
        return u._replace(fragment="").geturl()
    except Exception:
        return url or ""


def _find_duplicate(url):
    key = _norm_url_key(url)
    for j in list(JOBS.values()):
        if (j.get("status") in ("done", "downloading", "queued", "paused")
                and _norm_url_key(j.get("url")) == key):
            return j
    return None


_FAVICON_DOMAIN_RE = re.compile(r"^[a-z0-9.-]{3,253}$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ---- /changelog: latest GitHub release notes (cached 10 minutes) ----------
# The GUI calls this once on the first start after an app update and prints
# the notes to the Activity Log. Public read-only data, so no API token.
CHANGELOG_REPO = "Zixnano/VidGrab"
_CHANGELOG_CACHE = {"ts": 0.0, "data": None}
_CHANGELOG_TTL = 600

# --------------------------------------------------------------------------
# Route blueprints. Imported here, after every helper they take from this
# module is defined (they do `from api import ...`). The names are
# re-exported so `from api import <route or helper>` keeps working.
from routes_health import health_bp, health, ping, version, diskspace, stats
from routes_pair import pair_bp, pair, upload_token, favicon_store, favicon_get
from routes_jobs import jobs_bp, jobs, download, batch, show_add_dialog, queue_status_route, set_queue_running, dup_check, clear_failed
from routes_job_actions import job_actions_bp, _job_action, pause, resume, stop, delete, delete_job_and_file, redownload, reorder, set_category, open_folder, convert
from routes_files import files_bp, upload, toolkit_compress, toolkit_trim, toolkit_gif
from routes_probe import probe_bp, probe, probe_formats, probe_head
from routes_settings import settings_bp, update_settings, theme, logs_endpoint
from routes_updates import updates_bp, update_status, update_skip, ytdlp_update_now, ytdlp_rollback, changelog_route
from routes_remote import remote_bp, _REMOTE_COOKIE, _REMOTE_SESSION_TTL, _REMOTE_SESSIONS, _REMOTE_SESSION_LOCK, _new_remote_session, _valid_remote_session, _REMOTE_CSS, _remote_page, _remote_login_page, remote_login, _remote_action_form, remote_ui

app.register_blueprint(health_bp)
app.register_blueprint(pair_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(job_actions_bp)
app.register_blueprint(files_bp)
app.register_blueprint(probe_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(updates_bp)
app.register_blueprint(remote_bp)


def run_server():
    host = "0.0.0.0" if STATE.get("lan_access") else "127.0.0.1"
    app.run(host=host, port=APP_PORT, debug=False, use_reloader=False)
