"""Flask HTTP API: routes, auth, pairing, upload nonces."""

from pathlib import Path
import glob
import os
import re
import requests
import secrets
import shutil
import subprocess
import sys
import threading
import time
from flask import Flask, jsonify, request
from flask_cors import CORS

from downloader import maybe_convert_to_mp4, _repair_recording, _probe_recording, _CREATE_NO_WINDOW
from settings import MAX_UPLOAD_BYTES
import queue
import html
from yt_dlp import YoutubeDL
from settings import APP_PORT, STATE, save_settings, safe_filename, guess_filename, _unique_path, _dest_for, stat_for
from jobs import transition, JobEvent, JobPhase, set_phase, JOBS, new_job, save_jobs_snapshot, _fire_new_job_hooks, _fire_show_dialog_hooks

from logging_setup import log, LOG_QUEUE

app = Flask(__name__)
# Content-script fetches carry the PAGE's origin (not the extension's), so
# the direct recording upload from content.js needs broader CORS on /upload.
# Safe: /upload is gated by a one-time nonce that can only be obtained with
# the API token — a random web page can't forge one, so wider origins here
# expose nothing. Every other route stays extension-origin-only.
CORS(app, resources={
    r"/*": {"origins": r"chrome-extension://.*"},
    r"/upload": {"origins": r".*"},
}, supports_credentials=False)

TOKEN_PROTECTED_PATHS = {
    "/download", "/batch", "/pause", "/resume", "/stop", "/delete",
    "/probe", "/rules", "/jobs", "/stats", "/convert", "/logs",
    "/category", "/probe-head", "/settings", "/probe-formats",
    "/upload-token", "/redownload", "/show-add-dialog",
}

# Random per-launch key, embedded in the LAN web UI's action links so that
# unauthenticated page can still drive the token-protected action endpoints
# without exposing them to anyone else on the network.
REMOTE_KEY = secrets.token_hex(8)

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
        if nonce in _UPLOAD_NONCES:
            del _UPLOAD_NONCES[nonce]
            return True
    return False

# Auto-pairing: one successful /pair per app session. The endpoint is
# deliberately NOT in TOKEN_PROTECTED_PATHS — it's guarded by the
# localhost-only check and this once-per-session flag instead.
_PAIR_GRANTED = False

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
    if request.args.get("key") == REMOTE_KEY:
        return
    supplied = request.headers.get("X-API-Token", "")
    if not supplied or not secrets.compare_digest(supplied, STATE.get("api_token", "")):
        return jsonify({"error": "missing or invalid API token"}), 401

def cleanup_partial_files(job):
    try:
        dest = _dest_for(job)
    except Exception:
        return
    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists():
        try:
            part.unlink()
        except OSError:
            pass
    if dest.parent.exists():
        for sibling in dest.parent.glob(glob.escape(dest.name) + ".part*"):
            try:
                sibling.unlink()
            except OSError:
                pass

# --------------------------------------------------------------- API ------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/pair", methods=["POST"])
def pair():
    """Handshake for the extension: hand out the API token, but only to a
    localhost peer, and only once per app session."""
    global _PAIR_GRANTED
    if request.remote_addr not in ("127.0.0.1", "::1"):
        log(f"pair attempt rejected: remote_addr={request.remote_addr}")
        return jsonify({"error": "pairing only allowed from localhost"}), 403
    if _PAIR_GRANTED:
        log("pair attempt rejected: already granted this session")
        return jsonify({"error": "pairing already granted this session"}), 403
    _PAIR_GRANTED = True
    log("extension paired automatically")
    return jsonify({"token": STATE.get("api_token", "")})

@app.route("/ping", methods=["GET"])
def ping():
    """Liveness + pairing-state probe for the Options 'Test connection'
    button. Unlike /pair this never flips _PAIR_GRANTED, so it can be
    called any number of times per session."""
    return jsonify({"ok": True, "paired": _PAIR_GRANTED})

@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    jid = new_job(
        url, filename=data.get("filename"), category=data.get("category"),
        referer=data.get("referer"), cookie=data.get("cookie"),
        user_agent=data.get("user_agent"), job_type=data.get("type"),
        format_id=data.get("format_id"), target_format=data.get("target_format"),
        resolution=data.get("resolution"), multi=bool(data.get("multi")),
    )
    return jsonify({"job_id": jid})

@app.route("/show-add-dialog", methods=["POST"])
def show_add_dialog():
    """IDM-style: the extension asks the app to show the Add dialog instead
    of queuing directly. If the user checked "don't show again", queue."""
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    if STATE.get("skip_add_dialog"):
        jid = new_job(
            url, filename=data.get("filename"),
            category=data.get("category"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
            format_id=data.get("format_id"),
            target_format=data.get("target_format"),
        )
        return jsonify({"ok": True, "auto_queued": True, "job_id": jid})
    _fire_show_dialog_hooks({
        "url": url,
        "filename": data.get("filename"),
        "category": data.get("category"),
        "referer": data.get("referer"),
        "cookie": data.get("cookie"),
        "user_agent": data.get("user_agent"),
        "format_id": data.get("format_id"),
        "target_format": data.get("target_format"),
    })
    return jsonify({"ok": True, "auto_queued": False})

@app.route("/batch", methods=["POST"])
def batch():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items", [])
    ids = []
    for it in items:
        if not it.get("url"):
            continue
        ids.append(new_job(
            it["url"], filename=it.get("filename"),
            category=it.get("category"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
            format_id=it.get("format_id"),
            target_format=it.get("target_format"),
            resolution=it.get("resolution"), multi=bool(it.get("multi")),
        ))
    return jsonify({"job_ids": ids})

def _job_action(job_id, action):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if action == "pause":
        job["pause_evt"].set()
    elif action == "resume":
        if job["status"] == "done":
            return jsonify({"error": "already done"}), 400
        if job["status"] == "downloading":
            return jsonify({"error": "already downloading"}), 400
        job["pause_evt"].clear()
        job["stop_evt"].clear()
        job["error"] = None
        transition(job, JobEvent.RESUME)
    elif action == "stop":
        job["stop_evt"].set()
        job["pause_evt"].clear()
    elif action == "delete":
        job["stop_evt"].set()
        cleanup_partial_files(job)
        JOBS.pop(job_id, None)
    save_jobs_snapshot()
    return jsonify({"ok": True})

@app.route("/pause/<job_id>", methods=["POST"])
def pause(job_id):
    return _job_action(job_id, "pause")

@app.route("/resume/<job_id>", methods=["POST"])
def resume(job_id):
    return _job_action(job_id, "resume")

@app.route("/stop/<job_id>", methods=["POST"])
def stop(job_id):
    return _job_action(job_id, "stop")

@app.route("/redownload/<job_id>", methods=["POST"])
def redownload(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] == "downloading":
        # The running download thread only checks stop_evt between chunks.
        # set() immediately followed by clear() here gives it no reliable
        # window to observe the signal, so the old thread can keep writing
        # to the same .part file the freshly-dispatched job also writes to.
        return jsonify({"error": "job is actively downloading — stop it first"}), 400
    job["stop_evt"].set()
    cleanup_partial_files(job)
    job["pause_evt"].clear()
    job["stop_evt"].clear()
    job["error"] = None
    job["size_done"] = 0
    job["size_total"] = 0
    transition(job, JobEvent.RESUME)
    save_jobs_snapshot()
    return jsonify({"ok": True})

@app.route("/delete/<job_id>", methods=["POST"])
def delete(job_id):
    return _job_action(job_id, "delete")

@app.route("/jobs", methods=["GET"])
def jobs():
    return jsonify({jid: {k: v for k, v in j.items() if k not in ("pause_evt", "stop_evt")}
                     for jid, j in JOBS.items()})

@app.route("/probe", methods=["POST"])
def probe():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries") or [info]
        out = []
        for e in entries[:200]:
            if not e:
                continue
            out.append({
                "url": e.get("url") or e.get("webpage_url") or url,
                "title": e.get("title") or "untitled",
                "duration": e.get("duration"),
            })
        return jsonify({"items": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/probe-formats", methods=["POST"])
def probe_formats():
    """Thin wrapper over the engine probe (Session 7)."""
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    from engines import route_for
    formats, last_err = None, None
    for engine in route_for(url):          # probe falls through; download doesn't
        try:
            formats = engine.probe(url, {})
            if formats:
                break
        except Exception as e:
            last_err = e
    if not formats:
        return jsonify({"error": str(last_err) or "probe failed"}), 500
    return jsonify({"formats": [f.to_dict() for f in formats][:30]})

@app.route("/upload-token", methods=["POST"])
def upload_token():
    return jsonify({"nonce": _issue_upload_nonce()})

@app.route("/upload", methods=["POST"])
def upload():
    """Direct handoff for extension screen recordings (multipart, one file)."""
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify({"error": "file too large (max 500 MB)"}), 413
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "missing 'file' field"}), 400
    filename = safe_filename(f.filename or "recording.webm")
    if not os.path.splitext(filename)[1]:
        filename += ".webm"
    dest_dir = Path(STATE["output_dir"]) / "Video"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    f.save(str(dest))
    log(f"recording upload: {dest.name} ({dest.stat().st_size} bytes)")

    dest = _repair_recording(dest)

    jid = new_job(url=f"upload:///{dest.name}", filename=dest.name,
                  category="Video", job_type="generic", defer=True)
    job = JOBS[jid]

    ok, reason = _probe_recording(dest)
    if not ok:
        transition(job, JobEvent.FAIL)
        job["error"] = f"corrupt recording: {reason}"
        log(f"uploaded recording is corrupt: {reason}")
        save_jobs_snapshot()
        _fire_new_job_hooks(job)
        return jsonify({"ok": False, "error": f"corrupt recording: {reason}"}), 400

    log(f"recording validated: {dest.name} ({dest.stat().st_size} bytes)")
    transition(job, JobEvent.COMPLETE)
    job["size_total"] = job["size_done"] = dest.stat().st_size
    dest = maybe_convert_to_mp4(job, dest)  # .webm → .mp4 when auto_mp4 is on
    job["completed_ts"] = time.time()
    save_jobs_snapshot()
    _fire_new_job_hooks(job)
    return jsonify({"ok": True, "job_id": jid})

@app.route("/stats", methods=["GET"])
def stats():
    return jsonify({"samples": STATE.get("download_stats", [])[-600:]})

@app.route("/convert/<job_id>", methods=["POST"])
def convert(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "job not done"}), 400
    if job.get("phase") == "converting":
        return jsonify({"error": "conversion already in progress"}), 400
    data = request.get_json(force=True, silent=True) or {}
    fmt = (data.get("format") or "").lower()
    if fmt not in ("mp4", "mkv", "mp3", "wav", "m4a", "flac", "opus"):
        return jsonify({"error": "unsupported format"}), 400
    src = stat_for(job)
    if not src.exists():
        return jsonify({"error": "source missing"}), 404
    ffmpeg = "ffmpeg"
    if getattr(sys, "frozen", False):
        ffmpeg = str(Path(sys._MEIPASS) / "ffmpeg.exe")
    dst = src.with_suffix("." + fmt)
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if fmt in ("mp3", "wav", "m4a", "flac", "opus"):
        cmd += ["-vn"]
        if fmt == "mp3":
            cmd += ["-q:a", "0"]
        elif fmt == "opus":
            cmd += ["-b:a", "192k"]
        elif fmt == "m4a":
            cmd += ["-b:a", "256k"]
    cmd += [str(dst)]

    set_phase(job, JobPhase.CONVERTING)
    job["converting_fmt"] = fmt
    job["conversion_progress"] = 0
    log(f"converting {src.name} → {dst.name}")

    def progress_thread():
        total_sec = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW, text=True, errors="replace",
            )
            dur_re = re.compile(r"Duration: (\d+):(\d+):(\d+)")
            time_re = re.compile(r"time=(\d+):(\d+):(\d+)")
            for line in proc.stdout:
                if total_sec is None:
                    m = dur_re.search(line)
                    if m:
                        total_sec = (int(m.group(1)) * 3600
                                     + int(m.group(2)) * 60 + int(m.group(3)))
                m = time_re.search(line)
                if m and total_sec:
                    done_sec = (int(m.group(1)) * 3600
                                + int(m.group(2)) * 60 + int(m.group(3)))
                    job["conversion_progress"] = min(99, int(done_sec / total_sec * 100))
            proc.wait()
            if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                job["conversion_progress"] = 100
                job["converted_to"] = fmt
                log(f"converted {src.name} → {dst.name}")
            else:
                log(f"conversion failed for {src.name} (rc={proc.returncode})")
        except Exception as e:
            log(f"conversion failed for {src.name}: {e}")
        finally:
            set_phase(job, JobPhase.DONE)
            save_jobs_snapshot()

    threading.Thread(target=progress_thread, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/logs", methods=["GET"])
def logs_endpoint():
    lines = []
    while not LOG_QUEUE.empty():
        try:
            lines.append(LOG_QUEUE.get_nowait())
        except queue.Empty:
            break
    return jsonify({"logs": lines[-200:]})

@app.route("/category/<job_id>", methods=["POST"])
def set_category(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    data = request.get_json(force=True, silent=True) or {}
    new_cat = data.get("category", "Other")
    old_cat = job.get("category", "Other")
    if new_cat == old_cat:
        return jsonify({"ok": True, "moved": False})
    try:
        old_path = _dest_for(job)
    except Exception:
        old_path = None
    job["category"] = new_cat
    try:
        new_path = _dest_for(job)
    except Exception as e:
        job["category"] = old_cat
        return jsonify({"error": str(e)}), 500
    if old_path and old_path.exists() and old_path != new_path:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if new_path.exists():
            new_path = _unique_path(new_path)
            job["filename"] = new_path.name
        try:
            try:
                os.replace(old_path, new_path)
            except OSError:
                import shutil as _sh
                _sh.move(str(old_path), str(new_path))
        except Exception as e:
            job["category"] = old_cat
            return jsonify({"error": f"could not move file: {e}"}), 500
    save_jobs_snapshot()
    return jsonify({"ok": True, "moved": True})

@app.route("/probe-head", methods=["POST"])
def probe_head():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        r = requests.head(url, allow_redirects=True,
                          timeout=STATE["connection_timeout"])
        return jsonify({
            "size": int(r.headers.get("Content-Length", 0)),
            "content_type": r.headers.get("Content-Type", ""),
            "filename_guess": guess_filename(url, r.headers.get("Content-Disposition")),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True, silent=True) or {}
    for k, v in data.items():
        if k == "api_token" or k not in STATE:
            continue
        # Dict-valued settings (e.g. per_category_dirs) are merged key-by-key
        # rather than replaced outright, so setting one category's override
        # (as the "Remember this path" checkbox does) doesn't wipe the rest.
        if isinstance(STATE[k], dict) and isinstance(v, dict):
            STATE[k].update(v)
        else:
            STATE[k] = v
    save_settings()
    return jsonify({"ok": True})

@app.route("/remote", methods=["GET"])
def remote_ui():
    rows = "".join(
        f"<tr><td>{html.escape(j['filename'])}</td><td>{html.escape(j['status'])}</td>"
        f"<td><a href='/pause/{jid}?key={REMOTE_KEY}'>pause</a> "
        f"<a href='/resume/{jid}?key={REMOTE_KEY}'>resume</a> "
        f"<a href='/stop/{jid}?key={REMOTE_KEY}'>stop</a></td></tr>"
        for jid, j in JOBS.items()
    )
    return f"""<!doctype html><meta name=viewport content="width=device-width">
    <title>Video Grabber</title>
    <style>body{{font:14px sans-serif;padding:12px}}table{{border-collapse:collapse;width:100%}}
    td,th{{border:1px solid #ccc;padding:6px;text-align:left}}</style>
    <h2>Video Grabber</h2><table>
    <tr><th>File</th><th>Status</th><th>Actions</th></tr>{rows}</table>
    <p><a href="/jobs?key={REMOTE_KEY}">raw JSON</a></p>"""

def run_server():
    host = "0.0.0.0" if STATE.get("lan_access") else "127.0.0.1"
    app.run(host=host, port=APP_PORT, debug=False, use_reloader=False)

