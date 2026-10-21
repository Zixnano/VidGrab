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
from urllib.parse import urlparse
from flask import Flask, jsonify, request
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
CORS(app, resources={
    r"/*": {"origins": r"chrome-extension://.*"},
    r"/upload": {"origins": r".*"},
}, supports_credentials=False)

TOKEN_PROTECTED_PATHS = {
    "/download", "/batch", "/pause", "/resume", "/stop", "/delete",
    "/probe", "/rules", "/jobs", "/stats", "/convert", "/logs",
    "/category", "/probe-head", "/settings", "/probe-formats",
    "/upload-token", "/redownload", "/show-add-dialog",
    "/clear-failed", "/queue-status", "/dup-check", "/favicon",
    "/update-status", "/update-skip", "/ytdlp-update", "/ytdlp-rollback",
    "/reorder", "/theme", "/queue-running", "/delete-file", "/toolkit",
    "/open-folder",
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
    if secrets.compare_digest(request.args.get("key", ""), REMOTE_KEY):
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
    return jsonify({"ok": True, "paired": _PAIR_GRANTED, "version": APP_VERSION})

@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    dup = _find_duplicate(url)
    # v5.5 duplicate guard: the exact same URL already queued or downloading is
    # not queued a second time (HLS pills sent the same stream twice). The
    # existing job's id is returned with 200, so callers treat it as success.
    for _j in list(JOBS.values()):
        if _j.get("url") == url and _j.get("status") in ("queued", "downloading"):
            log(f"download: {url} is already {_j['status']} as job {_j['id']}; not queuing a duplicate")
            return jsonify({"job_id": _j["id"], "duplicate_of": _j["id"],
                            "already_queued": True, "status": _j["status"]})
    jid = new_job(
        url, filename=data.get("filename"), category=data.get("category"),
        referer=data.get("referer"), cookie=data.get("cookie"),
        user_agent=data.get("user_agent"), job_type=data.get("type"),
        format_id=data.get("format_id"), target_format=data.get("target_format"),
        resolution=data.get("resolution"), multi=bool(data.get("multi")),
        download_playlist=data.get("download_playlist", False),
        page_url=data.get("page_url"),
    )
    # The job is still created (re-downloading on purpose is legitimate);
    # the hint lets the caller tell the user it was a repeat.
    return jsonify({"job_id": jid, "duplicate_of": dup["id"] if dup else None})

@app.route("/show-add-dialog", methods=["POST"])
def show_add_dialog():
    """IDM-style: the extension asks the app to show the Add dialog instead
    of queuing directly. If the user checked "don't show again", queue."""
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    # Session D: a playlist request goes to the picker. The auto-queue branch
    # is skipped for it - there is nothing to queue until items are picked,
    # and queueing the URL would recreate the one-row-for-many-files job. An
    # older extension still sending download_playlist lands here too.
    pick = bool(data.get("pick_playlist") or data.get("download_playlist"))
    if pick:
        log("show-add-dialog: playlist picker requested")
    dup = None if pick else _find_duplicate(url)
    dup_id = dup["id"] if dup else None
    if STATE.get("skip_add_dialog") and not pick:
        jid = new_job(
            url, filename=data.get("filename"),
            category=data.get("category"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
            format_id=data.get("format_id"),
            target_format=data.get("target_format"),
            download_playlist=data.get("download_playlist", False),
            page_url=data.get("page_url"),
        )
        log(f"auto-queued {data.get('filename') or url} (skip_add_dialog)")
        return jsonify({"ok": True, "auto_queued": True, "job_id": jid,
                        "duplicate_of": dup_id})
    _fire_show_dialog_hooks({
        "url": url,
        "filename": data.get("filename"),
        "category": data.get("category"),
        "referer": data.get("referer"),
        "cookie": data.get("cookie"),
        "user_agent": data.get("user_agent"),
        "format_id": data.get("format_id"),
        "target_format": data.get("target_format"),
        "download_playlist": data.get("download_playlist", False),
        "pick_playlist": pick,
        "page_url": data.get("page_url"),
        "duplicate_of": dup_id,
        "_req_ts": time.time(),   # lets the GUI log how long the dialog took to open
    })
    return jsonify({"ok": True, "auto_queued": False, "duplicate_of": dup_id})

@app.route("/batch", methods=["POST"])
def batch():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items", [])
    ids = []
    deferred_save = False
    batch_ts = time.time()  # one timestamp for a whole playlist batch
    for it in items:
        if not it.get("url"):
            continue
        # Session D: playlist-picker items carry playlist_* fields. Those
        # batches skip the per-job snapshot (one save after the loop) and
        # share created_ts so the table can show them in playlist order.
        pid = it.get("playlist_id")
        pl = {}
        if pid:
            idx = it.get("playlist_index")
            pl = {
                "playlist_id": str(pid)[:200],
                "playlist_title": str(it.get("playlist_title") or "")[:300] or None,
                "playlist_index": idx if isinstance(idx, int) and not isinstance(idx, bool) else None,
                "playlist_url": str(it.get("playlist_url") or "")[:2000] or None,
                "snapshot": False,
                "created_ts": batch_ts,
            }
            deferred_save = True
        ids.append(new_job(
            it["url"], filename=it.get("filename"),
            category=it.get("category"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
            job_type=it.get("type"),
            format_id=it.get("format_id"),
            target_format=it.get("target_format"),
            resolution=it.get("resolution"), multi=bool(it.get("multi")),
            download_playlist=it.get("download_playlist", data.get("download_playlist", False)),
            page_url=data.get("page_url") or data.get("referer"),
            **pl,
        ))
    if deferred_save:
        save_jobs_snapshot()
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
        # v5: a manual Resume overrides auto-retry delays and the YouTube
        # hold (e.g. the user just switched VPN after a bot check).
        job["retry_count"] = 0
        job["retry_after"] = 0
        clear_youtube_cooldown()
        w = _WORKERS.get(job_id)
        if (job["status"] == "paused" and job.get("type") != "generic"
                and w is not None and w.is_alive()):
            # yt-dlp keeps its worker thread parked in progress_cb while a
            # job is paused and simply carries on once pause_evt clears.
            # Routing that through "queued" let the dispatcher start a
            # second yt-dlp on the same .part files about one resume in
            # four. Hand the job straight back to the parked worker.
            transition(job, JobEvent.START)
        else:
            transition(job, JobEvent.RESUME)
    elif action == "stop":
        w = _WORKERS.get(job_id)
        if job["status"] == "queued":
            # No worker thread exists yet to observe stop_evt, and the
            # dispatcher clears it when it starts the job — so the old
            # set() was a silent no-op and the job started anyway. Flip the
            # status instead (transition() also sets stop_evt).
            transition(job, JobEvent.STOP)
        elif (job["status"] == "paused"
                and not (w is not None and w.is_alive())):
            # A paused generic job's worker already exited when it paused (and
            # after a restart there is no worker at all), so nothing would
            # ever observe stop_evt and the row stayed "paused" forever. Move
            # it to stopped ourselves (transition() also sets stop_evt).
            transition(job, JobEvent.STOP)
        else:
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
    w = _WORKERS.get(job_id)
    if (job["status"] == "paused" and job.get("type") != "generic"
            and w is not None and w.is_alive()):
        # A paused yt-dlp job's worker is still parked in progress_cb. The
        # set()/clear() below gives it no window to see a stop, and the
        # dispatcher then started a SECOND yt-dlp on the same files. Stop the
        # parked worker and wait for it to exit before cleaning anything up.
        job["stop_evt"].set()
        job["pause_evt"].clear()
        w.join(timeout=5)
        if w.is_alive():
            return jsonify({"error": "job is still stopping, try again in a moment"}), 409
    if job["status"] == "done":
        # RESUME isn't a legal transition from DONE: this route used to zero
        # size_done/size_total below and THEN raise (HTTP 500), leaving a
        # finished job showing 0 bytes. Reopen it so RESUME -> QUEUED works.
        repair_status(job, "stopped")
    job["stop_evt"].set()
    cleanup_partial_files(job)
    job["pause_evt"].clear()
    job["stop_evt"].clear()
    job["error"] = None
    job["retry_count"] = 0
    job["retry_after"] = 0
    job["no_archive"] = True   # an explicit redownload must not be skipped
    clear_youtube_cooldown()
    job["size_done"] = 0
    job["size_total"] = 0
    transition(job, JobEvent.RESUME)
    save_jobs_snapshot()
    return jsonify({"ok": True})

@app.route("/delete/<job_id>", methods=["POST"])
def delete(job_id):
    return _job_action(job_id, "delete")

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


@app.route("/delete-file/<job_id>", methods=["POST"])
def delete_job_and_file(job_id):
    """Remove the job AND its file on disk. The path must resolve inside the
    VideoGrabber / download folders; anything else is refused and the job is
    left alone. Thumbnails (thumbs/) are never touched."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    cands = []
    if job.get("dest"):
        cands.append(Path(job["dest"]))
    try:
        cands.append(_dest_for(job))
    except Exception:
        pass
    uniq = []
    for c in cands:
        if c not in uniq:
            uniq.append(c)
    targets = []
    for c in uniq:
        try:
            exists = c.is_file()
        except OSError:
            exists = False
        if not exists:
            continue
        safe = _safe_in_roots(c)
        # Files only: the resolved target must be a regular file (never a
        # folder) and sit strictly inside the root VideoGrabber directory.
        if safe is None or not safe.is_file() or safe.is_dir():
            log(f"delete-file refused for job {job_id}: {c} is not a file inside the VideoGrabber folder")
            return jsonify({"error": "File is outside the VideoGrabber folder; "
                                     "refusing to delete it. The job was kept."}), 403
        if safe not in targets:
            targets.append(safe)
    # v5.5 safe deletion: another job can point at the same file (duplicate or
    # HLS rows). A shared file is never removed from disk; only this job goes.
    _shared = set()
    for _oid, _other in list(JOBS.items()):
        if _oid == job_id:
            continue
        _oc = []
        if _other.get("dest"):
            _oc.append(Path(_other["dest"]))
        try:
            _oc.append(_dest_for(_other))
        except Exception:
            pass
        for _c in _oc:
            try:
                _shared.add(Path(_c).resolve())
            except (OSError, ValueError):
                pass
    _kept = [t for t in targets if t in _shared]
    if _kept:
        log(f"delete-file: job {job_id} shares {[str(t) for t in _kept]} with another job; "
            "keeping the file on disk and removing only the job")
        targets = [t for t in targets if t not in _shared]
    job["stop_evt"].set()
    try:
        _inside = _safe_in_roots(_dest_for(job)) is not None
    except Exception:
        _inside = False
    if _inside and not _kept:
        cleanup_partial_files(job)   # .part leftovers, only when dest is inside the root
    removed = []
    for t in targets:
        try:
            os.remove(t)
            removed.append(str(t))
        except PermissionError:
            return jsonify({"error": f"{t.name} is in use by another program. Close it and try again."}), 409
        except OSError as e:
            return jsonify({"error": f"Couldn't delete {t.name}: {e}"}), 500
    JOBS.pop(job_id, None)
    save_jobs_snapshot()
    log(f"deleted job {job_id} and file(s): {removed or 'none on disk'}")
    return jsonify({"ok": True, "removed": removed, "kept_shared": [str(t) for t in _kept]})


# ---- Open folder: popup "done" link on Recent downloads -------------------
@app.route("/open-folder", methods=["POST"])
def open_folder():
    """Open Explorer on a finished job's file (selected) or, if the file is
    gone, its folder. The path comes only from the job record, never from the
    request, and no shell is involved."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = JOBS.get(job_id) if isinstance(job_id, str) else None
    if not job:
        return jsonify({"error": "unknown job"}), 404
    cands = []
    if job.get("dest"):
        cands.append(Path(job["dest"]))
    try:
        cands.append(_dest_for(job))
    except Exception:
        pass
    target = None
    for c in cands:
        try:
            if c.is_file():
                target = c
                break
        except OSError:
            continue
    folder = None
    if target is None:
        for c in cands:
            try:
                if c.parent.is_dir():
                    folder = c.parent
                    break
            except OSError:
                continue
        if folder is None:
            return jsonify({"error": "Folder not found"}), 404
    try:
        if sys.platform == "win32":
            if target is not None:
                subprocess.Popen(["explorer", f"/select,{target}"])
            else:
                os.startfile(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder or target.parent)])
    except Exception as e:
        log(f"open-folder failed for job {job_id}: {e}")
        return jsonify({"error": "Couldn't open the folder"}), 500
    return jsonify({"ok": True})


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
        except Exception:
            pass
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


@app.route("/toolkit/compress", methods=["POST"])
def toolkit_compress():
    data = request.get_json(force=True, silent=True) or {}
    try:
        src = _toolkit_source(data)
        ffmpeg, _ = _ff_bins()
        out = _toolkit_out(src, "compressed", "mp4")
        cmd = [ffmpeg, "-y", "-i", str(src), "-c:v", "libx264", "-preset", "medium",
               "-pix_fmt", "yuv420p"]
        if (data.get("mode") or "quality") == "size":
            mb = float(data.get("target_mb") or 0)
            if mb <= 0:
                raise ValueError("target size must be above 0 MB")
            dur = _probe_duration(src)
            audio_k = 128
            v_k = int(mb * 8192 / dur - audio_k)      # MB -> kilobit, minus audio
            if v_k < 50:
                raise ValueError("target size is too small for this video's length")
            cmd += ["-b:v", f"{v_k}k", "-maxrate", f"{int(v_k * 1.5)}k",
                    "-bufsize", f"{v_k * 3}k"]
        else:
            crf = int(data.get("crf") if data.get("crf") is not None else 28)
            cmd += ["-crf", str(max(16, min(40, crf)))]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return _run_toolkit(cmd, out, "compress")


@app.route("/toolkit/trim", methods=["POST"])
def toolkit_trim():
    data = request.get_json(force=True, silent=True) or {}
    try:
        src = _toolkit_source(data)
        ffmpeg, _ = _ff_bins()
        start = _parse_clock(data.get("start") or "0", "start")
        end_raw = str(data.get("end") or "").strip()
        end = _parse_clock(end_raw, "end") if end_raw else None
        if end is not None and end <= start:
            raise ValueError("end must be after start")
        out = _toolkit_out(src, "trim", src.suffix or ".mp4")
        # -ss / -to before -i + -c copy: no re-encode, so it is instant and
        # lossless (cuts land on the nearest keyframe).
        cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}"]
        if end is not None:
            cmd += ["-to", f"{end:.3f}"]
        cmd += ["-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return _run_toolkit(cmd, out, "trim")


@app.route("/toolkit/gif", methods=["POST"])
def toolkit_gif():
    data = request.get_json(force=True, silent=True) or {}
    try:
        src = _toolkit_source(data)
        ffmpeg, _ = _ff_bins()
        start = _parse_clock(data.get("start") or "0", "start")
        dur = float(data.get("duration") or 5)
        if not 0.1 <= dur <= 60:
            raise ValueError("duration must be between 0.1 and 60 seconds")
        fps = max(1, min(30, int(data.get("fps") or 12)))
        width = max(64, min(1280, int(data.get("width") or 480)))
        out = _toolkit_out(src, "gif", "gif")
        # Two-stage palette in one pass: palettegen builds the best 256 colors
        # for this clip, paletteuse maps frames onto it. Far cleaner than the
        # default GIF palette.
        vf = (f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];"
              f"[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5")
        cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
               "-vf", vf, "-loop", "0", str(out)]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return _run_toolkit(cmd, out, "gif")


@app.route("/jobs", methods=["GET"])
def jobs():
    # cookie is dropped: the GUI never reads it, and shipping every job's
    # cookie string on each 1 Hz poll bloats the payload and leaks it.
    return jsonify({jid: {k: v for k, v in j.items()
                          if k not in ("pause_evt", "stop_evt", "cookie")}
                     for jid, j in list(JOBS.items())})

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


@app.route("/probe", methods=["POST"])
def probe():
    """Playlist/single-video item list (Session D). Request: url, optional
    start/limit and cookie/referer/user_agent. See engines.probe_playlist."""
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url or not isinstance(url, str):
        return _probe_error("missing url", "missing_url", 400)
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return _probe_error("not an http(s) URL", "bad_url", 400)
    start, ok_start = _probe_int(data, "start", 0)
    limit, ok_limit = _probe_int(data, "limit", 200)
    if not (ok_start and ok_limit):
        return _probe_error("start and limit must be integers", "bad_params", 400)
    start = max(0, start)
    limit = max(1, min(1000, limit))
    if "yt-dlp" in (STATE.get("engines_disabled") or []):
        return _probe_error("yt-dlp engine is disabled in settings",
                            "engine_disabled", 503)
    from engines import probe_playlist, PlaylistProbeError
    headers = _probe_headers(data)
    _log_probe_headers("probe", headers)
    try:
        result = probe_playlist(url, headers, start, limit)
    except PlaylistProbeError as e:
        log(f"probe: failed ({e.code}): {e}")
        return _probe_error(str(e), e.code, e.status)
    except Exception as e:
        log(f"probe: failed (extractor_error): {e}")
        return _probe_error(str(e), "extractor_error", 500)
    if result["is_playlist"]:
        pl = result["playlist"]
        log(f"probe: {url} -> playlist '{pl['title']}', "
            f"{len(result['items'])} item(s)"
            + (", truncated" if result["truncated"] else ""))
    else:
        log(f"probe: {url} -> single video")
    return jsonify(result)


@app.route("/probe-formats", methods=["POST"])
def probe_formats():
    """Thin wrapper over the engine probe (Session 7)."""
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    from engines import route_for
    headers = _probe_headers(data)
    _log_probe_headers("probe-formats", headers)
    formats, last_err = None, None
    for engine in route_for(url):          # probe falls through; download doesn't
        try:
            formats = engine.probe(url, {"headers": headers})
            if formats:
                break
        except Exception as e:
            last_err = e
    if not formats:
        return jsonify({"error": str(last_err) if last_err else "probe failed"}), 500
    # Task 2: formats are already sorted (best-first) by the engine; the
    # cap here is just a sanity ceiling against pathological format lists,
    # not the thing limiting what the user normally sees.
    return jsonify({"formats": [f.to_dict() for f in formats][:40]})

@app.route("/upload-token", methods=["POST"])
def upload_token():
    return jsonify({"nonce": _issue_upload_nonce()})

# Extensions /upload treats as media: repaired (ffmpeg) and validated
# (ffprobe) like a screen recording. Anything else (zip, js, pdf, ...) from
# the blob-download hook is saved to its category folder and registered as-is.
_MEDIA_UPLOAD_EXTS = {".webm", ".mp4", ".mkv", ".mov", ".m4v", ".avi", ".flv"}


@app.route("/upload", methods=["POST"])
def upload():
    """Direct handoff for extension uploads (multipart, one file): screen
    recordings (media: repaired and validated) and arbitrary files from the
    blob-download hook (registered as-is, no ffmpeg/ffprobe)."""
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"}), 413
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "missing 'file' field"}), 400
    # An upload with no name at all is a recording. A NAMED upload keeps its
    # name as-is: a blob download called "README" must not become .webm, it
    # would then be probed as media and fail.
    filename = safe_filename(f.filename or "recording.webm")
    ext = os.path.splitext(filename)[1].lower()
    _is_media = ext in _MEDIA_UPLOAD_EXTS
    category = "Video" if _is_media else category_for(filename)
    # _dest_for resolves the folder exactly like downloads do (per-category
    # folders, sanitised names, path-length cap).
    dest = _dest_for({"category": category, "filename": filename})
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest = _unique_path(dest)   # never overwrite an existing file
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    try:
        f.save(str(dest))
    except Exception as e:
        try:
            dest.unlink()
        except OSError:
            pass
        log(f"upload save failed: {dest.name}: {e}")
        return jsonify({"error": f"couldn't save upload: {e}"}), 500
    log(f"recording upload: {dest.name} ({dest.stat().st_size} bytes)")

    if _is_media:
        dest = _repair_recording(dest)

    jid = new_job(url=f"upload:///{dest.name}", filename=dest.name,
                  category=category, job_type="generic", defer=True)
    job = JOBS[jid]
    # The file is already on disk under this name in this folder: new_job's
    # naming cleanup and per-site rules must not point the job anywhere else.
    job["filename"] = dest.name
    job["category"] = category
    if job.get("status") == "skipped":
        # File type disabled in Options: the file is already saved, so keep
        # it. A skipped job has no COMPLETE transition; reopen it first.
        repair_status(job, "held")

    if _is_media:
        ok, reason = _probe_recording(dest)
        if not ok:
            transition(job, JobEvent.FAIL)
            job["error"] = f"corrupt recording: {reason}"
            log(f"uploaded recording is corrupt: {reason}")
            save_jobs_snapshot()
            _fire_new_job_hooks(job)
            return jsonify({"ok": False, "error": f"corrupt recording: {reason}"}), 400
        log(f"recording validated: {dest.name} ({dest.stat().st_size} bytes)")
    else:
        log(f"upload registered as-is: {dest.name} ({category}, {dest.stat().st_size} bytes)")
    transition(job, JobEvent.COMPLETE)
    job["size_total"] = job["size_done"] = dest.stat().st_size
    if _is_media:
        dest = maybe_convert_to_mp4(job, dest)  # .webm → .mp4 when auto_mp4 is on
    job["completed_ts"] = time.time()
    save_jobs_snapshot()
    _fire_new_job_hooks(job)
    # "category" lets the extension say where the file went ("Sent to Compressed").
    return jsonify({"ok": True, "job_id": jid, "category": category})

@app.route("/stats", methods=["GET"])
def stats():
    return jsonify({"samples": STATE.get("download_stats", [])[-600:]})


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


@app.route("/reorder/<jid>", methods=["POST"])
def reorder(jid):
    """Move a queued job in the start order: {"action": top|up|down|bottom}."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        pos = reorder_job(jid, str(data.get("action") or ""))
    except KeyError:
        return jsonify({"error": "no such job"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if pos is None:
        return jsonify({"error": "only queued jobs can be reordered"}), 409
    save_jobs_snapshot()
    return jsonify({"ok": True, "position": pos})


@app.route("/dup-check", methods=["POST"])
def dup_check():
    data = request.get_json(force=True, silent=True) or {}
    j = _find_duplicate(data.get("url") or "")
    return jsonify({"duplicate": ({"job_id": j["id"], "status": j["status"],
                                   "filename": j.get("filename")} if j else None)})


@app.route("/queue-status", methods=["GET"])
def queue_status_route():
    return jsonify(queue_status())


@app.route("/clear-failed", methods=["POST"])
def clear_failed():
    """Remove error rows (and their partial files). Optional JSON body:
    {"older_than_hours": N} limits it to rows at least that old."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        hours = float(data.get("older_than_hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    cutoff = time.time() - hours * 3600
    removed = 0
    for jid, j in list(JOBS.items()):
        if j.get("status") != "error":
            continue
        if hours and (j.get("created_ts") or 0) > cutoff:
            continue
        j["stop_evt"].set()
        cleanup_partial_files(j)
        JOBS.pop(jid, None)
        removed += 1
    if removed:
        save_jobs_snapshot()
    return jsonify({"removed": removed})


_FAVICON_DOMAIN_RE = re.compile(r"^[a-z0-9.-]{3,253}$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@app.route("/favicon", methods=["POST"])
def favicon_store():
    """The extension sends each site's favicon once (PNG data URL)."""
    import base64
    from settings import icons_dir
    data = request.get_json(force=True, silent=True) or {}
    domain = str(data.get("domain") or "").lower().strip()
    uri = str(data.get("data_url") or "")
    prefix = "data:image/png;base64,"
    if not _FAVICON_DOMAIN_RE.match(domain) or not uri.startswith(prefix):
        return jsonify({"error": "bad favicon request"}), 400
    try:
        raw = base64.b64decode(uri[len(prefix):], validate=True)
    except Exception:
        return jsonify({"error": "bad base64"}), 400
    if len(raw) > 65536 or not raw.startswith(_PNG_MAGIC):
        return jsonify({"error": "not a small PNG"}), 400
    d = icons_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / (domain + ".png")).write_bytes(raw)
    return jsonify({"ok": True})


@app.route("/favicon/<domain>", methods=["GET"])
def favicon_get(domain):
    from flask import send_file
    from settings import icons_dir
    domain = domain.lower()
    p = icons_dir() / (domain + ".png")
    if not _FAVICON_DOMAIN_RE.match(domain) or not p.is_file():
        return jsonify({"error": "no icon"}), 404
    return send_file(str(p), mimetype="image/png")


@app.route("/update-status", methods=["GET"])
def update_status():
    import updater
    import ytdlp_update
    return jsonify({"app": updater.get_update_state(),
                    "ytdlp": ytdlp_update.status(),
                    "queue": queue_status()})


@app.route("/update-skip", methods=["POST"])
def update_skip():
    import updater
    data = request.get_json(force=True, silent=True) or {}
    updater.skip_version(str(data.get("tag") or ""))
    return jsonify({"ok": True})


@app.route("/ytdlp-update", methods=["POST"])
def ytdlp_update_now():
    """Stage the newest yt-dlp now (applies on next launch)."""
    import ytdlp_update
    threading.Thread(target=ytdlp_update.update_ytdlp, daemon=True).start()
    return jsonify({"started": True})


@app.route("/ytdlp-rollback", methods=["POST"])
def ytdlp_rollback():
    import ytdlp_update
    return jsonify({"removed": ytdlp_update.rollback()})

# ---- /changelog: latest GitHub release notes (cached 10 minutes) ----------
# The GUI calls this once on the first start after an app update and prints
# the notes to the Activity Log. Public read-only data, so no API token.
CHANGELOG_REPO = "Zixnano/VidGrab"
_CHANGELOG_CACHE = {"ts": 0.0, "data": None}
_CHANGELOG_TTL = 600


@app.route("/changelog", methods=["GET"])
def changelog_route():
    import json as _json
    import time as _time
    from urllib.request import Request, urlopen
    now = _time.time()
    cached = _CHANGELOG_CACHE["data"]
    if cached and now - _CHANGELOG_CACHE["ts"] < _CHANGELOG_TTL:
        return jsonify(cached)
    req = Request(
        f"https://api.github.com/repos/{CHANGELOG_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"VideoGrabber/{APP_VERSION}"})
    try:
        with urlopen(req, timeout=8) as r:
            rel = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        if cached:  # stale notes beat none when GitHub is unreachable
            return jsonify(cached)
        return jsonify({"ok": False, "error": str(e)}), 502
    # Batch 3: clean the notes for the app. The release plumbing is noise
    # in the Activity Log, so drop the "Automatic build from commit ..."
    # line and any asset lines that point at the .zip download. An asset
    # line is a line whose ONLY content is a .zip filename, a URL ending
    # in .zip, or a markdown link to one — prose that merely mentions a
    # zip (even with a URL in it) is a real changelog note and is kept.
    _ZIP_ASSET_RE = re.compile(
        r"^[\s\-\*\+]*"                       # optional list marker / whitespace
        r"(?:"
            r"[\w.\-]+\.zip\b"                # bare "name.zip"
            r"|https?://\S+\.zip\b"           # bare URL ending in .zip
            r"|\[[^\]]*\]\(\s*https?://\S+\.zip\s*\)"   # [label](url.zip)
        r")\s*$",                             # nothing else on the line
        re.IGNORECASE,
    )

    def _clean_changelog(body):
        kept = []
        for ln in str(body or "").splitlines():
            low = ln.strip().lower()
            if low.startswith("automatic build from commit"):
                continue
            if _ZIP_ASSET_RE.match(ln):
                continue
            kept.append(ln)
        return "\n".join(kept).strip()

    data = {
        "ok": True,
        "tag": rel.get("tag_name") or "",
        "name": rel.get("name") or "",
        "body": _clean_changelog(rel.get("body") or ""),
        "url": rel.get("html_url") or "",
        "published_at": rel.get("published_at") or "",
    }
    _CHANGELOG_CACHE.update(ts=now, data=data)
    return jsonify(data)


@app.route("/version", methods=["GET"])
def version():
    return jsonify({"version": APP_VERSION})

@app.route("/diskspace", methods=["GET"])
def diskspace():
    return jsonify(disk_usage_for())

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
    _ffdir = find_ffmpeg_dir()
    ffmpeg = str(Path(_ffdir) / "ffmpeg.exe") if _ffdir else "ffmpeg"
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
        if k == "queue_running":
            # Deliberately ignored here (see /queue-running); logged so the
            # log shows if anything tries to flip it through this route.
            log("settings: ignoring queue_running write (use /queue-running)")
        if k in ("api_token", "queue_running") or k not in STATE:
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

@app.route("/queue-running", methods=["POST"])
def set_queue_running():
    """Pause/resume the whole queue. /settings ignores queue_running so a
    stray settings POST can't flip it; this is the one deliberate writer."""
    data = request.get_json(force=True, silent=True) or {}
    if "running" not in data:
        return jsonify({"error": "missing running"}), 400
    STATE["queue_running"] = bool(data["running"])
    save_settings()
    return jsonify({"ok": True, "queue_running": STATE["queue_running"]})

@app.route("/theme", methods=["GET"])
def theme():
    """Resolved color tokens, so the extension popup, options page and
    in-page pill can follow the app's Theme settings."""
    from palette import resolve_palette
    pal = resolve_palette(STATE)
    keys = ("bg_base", "bg_panel", "bg_elevated", "bg_sel", "bg_hover", "border",
            "border_hi", "text", "text_soft", "text_muted", "text_dim", "accent",
            "accent_dim", "text_on_accent", "success", "warning", "error", "info",
            "radius", "font", "font_mono")
    return jsonify({k: pal[k] for k in keys if k in pal})

@app.route("/remote", methods=["GET"])
def remote_ui():
    rows = "".join(
        f"<tr><td>{html.escape(j['filename'])}</td><td>{html.escape(j['status'])}</td>"
        f"<td><a href='/pause/{jid}?key={REMOTE_KEY}'>pause</a> "
        f"<a href='/resume/{jid}?key={REMOTE_KEY}'>resume</a> "
        f"<a href='/stop/{jid}?key={REMOTE_KEY}'>stop</a></td></tr>"
        for jid, j in list(JOBS.items())
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

