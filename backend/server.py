"""
Video Grabber — desktop companion app (v3.1: full feature set)
==============================================================================
A local HTTP server + Tkinter GUI that the Chrome extension talks to.
Handles both:
  - "generic" direct files (mp4, zip, exe, pdf, etc.) via a resumable,
    pausable chunked HTTP downloader with a global speed limiter.
  - "ytdlp" jobs (HLS/.m3u8, DASH/.mpd, or a page URL like a YouTube link)
    via yt-dlp used as a library.

v3.1 adds: segmented (multi-connection) downloads, tabbed Options dialog,
right-click context menu, drag-and-drop out to other apps, download-finished
toast, minimize-to-bar, Defender scan, auto-shutdown, auto-extract, portable
mode, per-site rules, post-download conversion, Link Grabber, LAN web UI.

Freeze with PyInstaller — see build_exe.md.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
from tkinterdnd2 import TkinterDnD, DND_FILES, DND_COPY

APP_PORT = 5757
HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"
JOBS_PATH = HOME / "jobs.json"

CATEGORY_MAP = {
    ".mp4": "Video", ".mkv": "Video", ".webm": "Video", ".avi": "Video", ".mov": "Video", ".m4v": "Video",
    ".mp3": "Music", ".wav": "Music", ".flac": "Music", ".m4a": "Music", ".aac": "Music",
    ".zip": "Compressed", ".rar": "Compressed", ".7z": "Compressed", ".tar": "Compressed", ".gz": "Compressed",
    ".pdf": "Documents", ".doc": "Documents", ".docx": "Documents", ".txt": "Documents", ".ppt": "Documents", ".pptx": "Documents",
    ".exe": "Programs", ".msi": "Programs", ".dmg": "Programs",
}
DIRECT_EXT_RE = re.compile(r"\.(mp4|m4v|mov|webm|mkv|avi|mp3|wav|flac|m4a|aac|zip|rar|7z|tar|gz|pdf|docx?|pptx?|txt|exe|msi|dmg)(\?|#|$)", re.I)

STATE = {
    "output_dir": str(HOME),
    "max_concurrent": 3,
    "speed_limit_kbps": 0,
    "queue_running": True,
    "clipboard_monitor": False,
    "api_token": "",
    "connection_timeout": 20,
    "max_retries": 3,
    "min_speed_kbps": 0,
    "min_speed_grace_sec": 30,
    "auto_shutdown": False,
    "defender_scan": False,
    "auto_extract": False,
    "open_folder_on_complete": False,
    "play_sound_on_complete": False,
    "lan_access": False,
    "per_category_dirs": {},
    "file_types_overrides": {},
    "rules": [],
    "download_stats": [],
    "portable": False,
    "double_click": "open",
}

JOBS = {}
JOB_COUNTER = 0
JOB_LOCK = threading.Lock()
LOG_QUEUE = queue.Queue()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": r"chrome-extension://.*"}}, supports_credentials=False)

TOKEN_PROTECTED_PATHS = {"/download", "/batch", "/pause", "/resume", "/stop", "/delete", "/probe", "/rules", "/jobs", "/stats", "/convert"}

# Random per-launch key, embedded in the LAN web UI's action links so that
# unauthenticated page can still drive the token-protected action endpoints
# without exposing them to anyone else on the network.
REMOTE_KEY = secrets.token_hex(8)


@app.before_request
def _require_api_token():
    if request.method == "OPTIONS":
        return
    if request.path not in TOKEN_PROTECTED_PATHS and not any(
        request.path.startswith(p + "/") for p in TOKEN_PROTECTED_PATHS
    ):
        return
    if request.args.get("key") == REMOTE_KEY:
        return
    supplied = request.headers.get("X-API-Token", "")
    if not supplied or not secrets.compare_digest(supplied, STATE.get("api_token", "")):
        return jsonify({"error": "missing or invalid API token"}), 401


def log(msg):
    LOG_QUEUE.put(f"[{time.strftime('%H:%M:%S')}] {msg}")


def portable_home():
    """If portable.txt sits next to the executable, keep settings/jobs with it."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    if (base / "portable.txt").exists():
        STATE["portable"] = True
        return base / "VideoGrabberData"
    return HOME


def load_settings():
    if CONFIG_PATH.exists():
        try:
            STATE.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    if not STATE.get("api_token"):
        STATE["api_token"] = secrets.token_hex(16)
        save_settings()


def save_settings():
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(STATE, indent=2))
    os.replace(tmp, CONFIG_PATH)


def save_jobs_snapshot():
    """Persist a plain (non-Event) view of jobs so history survives restarts."""
    try:
        snap = {}
        for jid, j in JOBS.items():
            snap[jid] = {k: v for k, v in j.items()
                         if k not in ("pause_evt", "stop_evt", "cookie", "referer", "user_agent")}
        tmp = JOBS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2))
        os.replace(tmp, JOBS_PATH)
    except Exception as e:
        log(f"couldn't save job history: {e}")


def load_jobs_snapshot():
    if JOBS_PATH.exists():
        try:
            snap = json.loads(JOBS_PATH.read_text())
            for jid, j in snap.items():
                j["pause_evt"] = threading.Event()
                j["stop_evt"] = threading.Event()
                if j["status"] in ("downloading", "queued"):
                    j["status"] = "stopped"
                JOBS[jid] = j
            global JOB_COUNTER
            JOB_COUNTER = max([int(k) for k in JOBS.keys()] + [0])
        except Exception as e:
            log(f"couldn't load job history: {e}")


def next_job_id():
    global JOB_COUNTER
    with JOB_LOCK:
        JOB_COUNTER += 1
        return str(JOB_COUNTER)


ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name):
    """Strip Windows-illegal characters, collapse whitespace, trim trailing
    dots/spaces, and cap the length so the resulting file can be created."""
    name = ILLEGAL_FILENAME_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if not name:
        name = "download"
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[: 180 - len(ext)] + ext
    return name


def guess_filename(url, content_disposition=None):
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if m:
            return safe_filename(unquote(m.group(1)))
    path = urlparse(url).path
    name = os.path.basename(path) or "download"
    return safe_filename(unquote(name))


def category_for(filename):
    ext = os.path.splitext(filename)[1].lower()
    return CATEGORY_MAP.get(ext, "Other")


def detect_type(url):
    if ".m3u8" in url.lower() or ".mpd" in url.lower():
        return "ytdlp"
    if DIRECT_EXT_RE.search(url):
        return "generic"
    return "ytdlp"


def new_job(url, filename=None, category=None, referer=None, cookie=None, user_agent=None, job_type=None):
    jid = next_job_id()
    jtype = job_type or detect_type(url)
    fname = safe_filename(filename) if filename else guess_filename(url)
    job_cat = category or category_for(fname)

    # Apply per-site rules
    netloc = urlparse(url).netloc
    for rule in STATE.get("rules", []):
        pattern = rule.get("domain", "").strip().lstrip("*.")
        if pattern and (netloc == pattern or netloc.endswith("." + pattern)):
            if "category" in rule:
                job_cat = rule["category"]
            break

    JOBS[jid] = {
        "id": jid, "url": url, "filename": fname,
        "category": job_cat,
        "type": jtype, "status": "queued",
        "size_total": 0, "size_done": 0, "speed": "", "error": None,
        "referer": referer, "cookie": cookie, "user_agent": user_agent,
        "created_ts": time.time(),
        "pause_evt": threading.Event(), "stop_evt": threading.Event(),
    }
    ext = os.path.splitext(fname)[1].lstrip(".").lower()
    if ext and STATE.get("file_types_overrides", {}).get(ext) is False:
        JOBS[jid]["status"] = "skipped"
        log(f"job {jid}: skipped — '.{ext}' is disabled in Options → File Types")
        save_jobs_snapshot()
        return jid
    log(f"job {jid} queued: {fname}")
    save_jobs_snapshot()
    return jid


class SpeedLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = time.time()
        self.tokens = 0.0

    def consume(self, n):
        limit = STATE["speed_limit_kbps"] * 1024
        if limit <= 0:
            return
        with self.lock:
            now = time.time()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(limit, self.tokens + elapsed * limit)
            self.tokens -= n
            deficit = -self.tokens
        if deficit > 0:
            time.sleep(deficit / limit)


LIMITER = SpeedLimiter()


def _dest_for(job):
    override = STATE.get("per_category_dirs", {}).get(job["category"])
    base = Path(override) if override else Path(STATE["output_dir"])
    cat_dir = base if override else base / job["category"]
    return cat_dir / safe_filename(job["filename"])


def stat_for(job):
    dest = _dest_for(job)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


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
        for sibling in dest.parent.glob(dest.name + ".part*"):
            try:
                sibling.unlink()
            except OSError:
                pass


def scan_file(path):
    if not STATE.get("defender_scan"):
        return
    defender = r"C:\Program Files\Windows Defender\MpCmdRun.exe"
    if not os.path.exists(defender):
        return
    try:
        r = subprocess.run([defender, "-Scan", "-ScanType", "3", "-File", str(path)],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            log(f"Defender flagged {path.name} (rc={r.returncode})")
    except Exception as e:
        log(f"Defender scan failed: {e}")


def maybe_shutdown_if_idle():
    if not STATE.get("auto_shutdown"):
        return
    if any(j["status"] in ("downloading", "queued") for j in JOBS.values()):
        return
    log("Queue empty — shutting down in 60s (cancel with `shutdown /a`)")
    os.system("shutdown /s /t 60")


def maybe_extract_archive(job, dest):
    if not STATE.get("auto_extract"):
        return
    if dest.suffix.lower() != ".zip":
        return
    try:
        import zipfile
        out = dest.with_suffix("")
        out.mkdir(exist_ok=True)
        with zipfile.ZipFile(dest) as z:
            z.extractall(out)
        log(f"extracted {dest.name} → {out.name}/")
    except Exception as e:
        log(f"auto-extract failed for {dest.name}: {e}")


def maybe_open_folder(dest):
    if STATE.get("open_folder_on_complete") and dest.exists():
        try:
            os.startfile(str(dest.parent))
        except Exception:
            pass


def maybe_play_sound():
    if not STATE.get("play_sound_on_complete"):
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def record_stat(nbytes):
    stats = STATE.setdefault("download_stats", [])
    stats.append({"ts": time.time(), "bytes": nbytes})
    if len(stats) > 3600:
        del stats[: len(stats) - 3600]


# ---------------------------------------------------------------- generic downloader ------

SEGMENT_THRESHOLD = 1 * 1024 * 1024
SEGMENT_COUNT = 8
_segment_lock = threading.Lock()


def _probe_ranges(url, headers):
    try:
        h = requests.head(url, headers=headers, allow_redirects=True,
                          timeout=STATE["connection_timeout"])
        size = int(h.headers.get("Content-Length", 0))
        ranges = "bytes" in h.headers.get("Accept-Ranges", "").lower()
        return size, ranges
    except Exception:
        return 0, False


def _download_segment(job, url, idx, start, end, headers, part_path):
    h = dict(headers)
    h["Range"] = f"bytes={start}-{end}"
    try:
        with requests.get(url, headers=h, stream=True,
                           timeout=STATE["connection_timeout"]) as r:
            if r.status_code != 206:
                raise RuntimeError(f"segment {idx} got HTTP {r.status_code}, expected 206")
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if job["stop_evt"].is_set():
                        return
                    if job["pause_evt"].is_set():
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    LIMITER.consume(len(chunk))
                    with _segment_lock:
                        job["size_done"] += len(chunk)
    except Exception as e:
        job["error"] = str(e)
        raise


def _generic_headers(job):
    headers = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if job.get("user_agent"):
        headers["User-Agent"] = job["user_agent"]
    return headers


def run_generic_segmented(job_id):
    job = JOBS[job_id]
    dest = stat_for(job)
    headers = _generic_headers(job)

    total, ranges_ok = _probe_ranges(job["url"], headers)
    if not ranges_ok or total < SEGMENT_THRESHOLD:
        return _run_generic_single(job_id)

    seg_size = total // SEGMENT_COUNT
    parts = []
    threads = []
    job["size_total"] = total
    job["size_done"] = 0
    job["status"] = "downloading"
    for i in range(SEGMENT_COUNT):
        start = i * seg_size
        end = total - 1 if i == SEGMENT_COUNT - 1 else start + seg_size - 1
        part = dest.with_suffix(dest.suffix + f".part{i}")
        parts.append(part)
        t = threading.Thread(target=_download_segment,
                             args=(job, job["url"], i, start, end, headers, part),
                             daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    if job["stop_evt"].is_set():
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        job["status"] = "stopped"
        log(f"job {job_id}: stopped")
        save_jobs_snapshot()
        return
    if job.get("error"):
        job["status"] = "error"
        log(f"job {job_id}: FAILED (segmented) — {job['error']}")
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        return
    if job["pause_evt"].is_set():
        # Segments can't pause mid-flight without corrupting the merge —
        # drop the partial parts and let Resume restart the download cleanly.
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        job["status"] = "paused"
        job["size_done"] = 0
        log(f"job {job_id}: paused (segmented downloads restart on resume)")
        save_jobs_snapshot()
        return
    with open(dest, "wb") as out:
        for p in parts:
            with open(p, "rb") as inp:
                shutil.copyfileobj(inp, out)
            p.unlink()
    job["status"] = "done"
    job["speed"] = ""
    record_stat(job["size_done"])
    log(f"job {job_id}: complete ✓ ({dest.name}, {SEGMENT_COUNT}x)")
    scan_file(dest)
    maybe_extract_archive(job, dest)
    maybe_open_folder(dest)
    maybe_play_sound()
    maybe_shutdown_if_idle()
    save_jobs_snapshot()


def run_generic(job_id):
    job = JOBS[job_id]
    dest = stat_for(job)
    part = dest.with_suffix(dest.suffix + ".part")
    existing = part.stat().st_size if part.exists() else 0
    # Multi-connection segmented download only makes sense from byte 0 —
    # a resumed .part file always continues single-stream with a Range header.
    if existing == 0:
        try:
            total, ok = _probe_ranges(job["url"], _generic_headers(job))
            if ok and total >= SEGMENT_THRESHOLD:
                return run_generic_segmented(job_id)
        except Exception:
            pass
    return _run_generic_single(job_id)


def _run_generic_single(job_id):
    job = JOBS[job_id]
    dest = stat_for(job)
    part = dest.with_suffix(dest.suffix + ".part")
    headers = _generic_headers(job)

    existing = part.stat().st_size if part.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"

    try:
        with requests.get(job["url"], headers=headers, stream=True,
                          timeout=STATE["connection_timeout"]) as r:
            if r.status_code not in (200, 206):
                job["status"] = "error"
                job["error"] = f"HTTP {r.status_code}"
                log(f"job {job_id}: server returned {r.status_code}")
                return
            if r.status_code == 200 and existing:
                log(f"job {job_id}: server ignored Range, restarting from 0")
                existing = 0
            total = int(r.headers.get("Content-Length", 0)) + existing
            job["size_total"] = total
            job["size_done"] = existing
            mode = "ab" if existing else "wb"
            last_report = time.time()
            last_bytes = existing
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if job["stop_evt"].is_set():
                        job["status"] = "stopped"
                        log(f"job {job_id}: stopped")
                        return
                    if job["pause_evt"].is_set():
                        job["status"] = "paused"
                        log(f"job {job_id}: paused")
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    LIMITER.consume(len(chunk))
                    job["size_done"] += len(chunk)
                    now = time.time()
                    if now - last_report >= 1:
                        rate = (job["size_done"] - last_bytes) / (now - last_report)
                        job["speed"] = f"{rate/1024:.0f} KB/s"
                        last_report, last_bytes = now, job["size_done"]
            os.replace(part, dest)
            job["status"] = "done"
            job["speed"] = ""
            record_stat(job["size_done"])
            log(f"job {job_id}: complete ✓ ({dest.name})")
            scan_file(dest)
            maybe_extract_archive(job, dest)
            maybe_open_folder(dest)
            maybe_play_sound()
            maybe_shutdown_if_idle()
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")
    finally:
        save_jobs_snapshot()


class DownloadCancelled(Exception):
    pass


def run_ytdlp(job_id):
    job = JOBS[job_id]
    headers = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if job.get("user_agent"):
        headers["User-Agent"] = job["user_agent"]

    dest = stat_for(job)
    outtmpl = str(dest.with_suffix(".%(ext)s"))

    def hook(d):
        while job["pause_evt"].is_set():
            job["status"] = "paused"
            if job["stop_evt"].is_set():
                raise DownloadCancelled()
            time.sleep(0.5)
        if job["stop_evt"].is_set():
            raise DownloadCancelled()
        if d["status"] == "downloading":
            job["status"] = "downloading"
            job["size_total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            job["size_done"] = d.get("downloaded_bytes", 0)
            job["speed"] = f"{(d.get('speed') or 0)/1024:.0f} KB/s"
        elif d["status"] == "finished":
            job["speed"] = ""

    ydl_opts = {
        "outtmpl": outtmpl,
        "http_headers": headers,
        "progress_hooks": [hook],
        "quiet": True, "no_warnings": True,
        "merge_output_format": "mp4",
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10485760,
        "retries": 10,
        "fragment_retries": 10,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "embedsubs": True,
    }
    if STATE["speed_limit_kbps"] > 0:
        ydl_opts["ratelimit"] = STATE["speed_limit_kbps"] * 1024
    if getattr(sys, "frozen", False):
        ydl_opts["ffmpeg_location"] = sys._MEIPASS

    job["status"] = "downloading"
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([job["url"]])
        job["status"] = "done"
        log(f"job {job_id}: complete ✓")
        try:
            d = stat_for(job)
            if d.exists():
                record_stat(job["size_done"])
                scan_file(d)
                maybe_open_folder(d)
                maybe_play_sound()
        except Exception:
            pass
        maybe_shutdown_if_idle()
    except DownloadCancelled:
        job["status"] = "stopped"
        log(f"job {job_id}: stopped")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")
    finally:
        save_jobs_snapshot()


def start_job_thread(job_id):
    job = JOBS[job_id]
    job["pause_evt"].clear()
    job["stop_evt"].clear()
    job["error"] = None
    job["status"] = "downloading"
    target = run_generic if job["type"] == "generic" else run_ytdlp
    threading.Thread(target=target, args=(job_id,), daemon=True).start()


def dispatcher_loop():
    while True:
        time.sleep(1)
        if not STATE["queue_running"]:
            continue
        active = sum(1 for j in JOBS.values() if j["status"] == "downloading")
        slots = STATE["max_concurrent"] - active
        if slots <= 0:
            continue
        started = 0
        for jid, j in list(JOBS.items()):
            if started >= slots:
                break
            if j["status"] == "queued":
                start_job_thread(jid)
                started += 1


# --------------------------------------------------------------- API ------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


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
    )
    return jsonify({"job_id": jid})


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
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
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
        job["pause_evt"].clear()
        job["stop_evt"].clear()
        job["status"] = "queued"
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


@app.route("/stats", methods=["GET"])
def stats():
    return jsonify({"samples": STATE.get("download_stats", [])[-600:]})


@app.route("/convert/<job_id>", methods=["POST"])
def convert(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "job not done"}), 400
    data = request.get_json(force=True, silent=True) or {}
    fmt = (data.get("format") or "").lower()
    if fmt not in ("mp4", "mkv", "mp3", "wav"):
        return jsonify({"error": "unsupported format"}), 400
    src = stat_for(job)
    if not src.exists():
        return jsonify({"error": "source missing"}), 404
    ffmpeg = "ffmpeg"
    if getattr(sys, "frozen", False):
        ffmpeg = str(Path(sys._MEIPASS) / "ffmpeg.exe")
    dst = src.with_suffix("." + fmt)
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if fmt in ("mp3", "wav"):
        cmd += ["-vn"]
    cmd += [str(dst)]
    try:
        subprocess.Popen(cmd)
        log(f"converting {src.name} → {dst.name}")
        return jsonify({"ok": True, "output": str(dst)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/remote", methods=["GET"])
def remote_ui():
    rows = "".join(
        f"<tr><td>{j['filename']}</td><td>{j['status']}</td>"
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


# ---------------------------------------------------------------- GUI ------
STATUS_COLORS = {
    "queued": "#999", "downloading": "#4caf50", "paused": "#f9a825",
    "stopped": "#e53935", "done": "#4caf50", "error": "#e53935",
    "skipped": "#607d8b",
}


def human_size(n):
    if not n:
        return "-"
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class GUI:
    def __init__(self, root):
        self.root = root
        root.title("Video Grabber")
        root.geometry("880x520")
        DARK_BG, DARK_FG, DARK_PANEL = "#1e1e1e", "#e0e0e0", "#262626"
        root.configure(bg=DARK_BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=DARK_PANEL, fieldbackground=DARK_PANEL,
                        foreground=DARK_FG, rowheight=24)
        style.configure("Treeview.Heading", background="#333", foreground=DARK_FG)
        style.map("Treeview", background=[("selected", "#3a6ea5")])

        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Export queue…", command=self.export_queue)
        filemenu.add_command(label="Import queue…", command=self.import_queue)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=root.quit)
        menubar.add_cascade(label="File", menu=filemenu)

        toolsmenu = tk.Menu(menubar, tearoff=0)
        toolsmenu.add_command(label="Copy pairing token for extension", command=self.copy_api_token)
        menubar.add_cascade(label="Tools", menu=toolsmenu)
        root.config(menu=menubar)

        bar = tk.Frame(root, bg=DARK_BG)
        bar.pack(fill="x", padx=6, pady=6)
        for text, cmd in [
            ("+ Add URL", self.add_url), ("+ Batch", self.add_batch),
            ("Resume", self.resume_sel), ("Pause", self.pause_sel), ("Stop", self.stop_sel),
            ("Delete", self.delete_sel), ("Delete Completed", self.delete_completed),
            ("Start Queue", self.start_queue), ("Stop Queue", self.stop_queue),
            ("Scheduler", self.open_scheduler), ("Options", self.open_options),
        ]:
            tk.Button(bar, text=text, command=cmd, bg="#333", fg=DARK_FG,
                      activebackground="#444", activeforeground=DARK_FG,
                      relief="flat", padx=8).pack(side="left", padx=2)

        body = tk.Frame(root, bg=DARK_BG)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        side = tk.Frame(body, bg=DARK_PANEL, width=140)
        side.pack(side="left", fill="y")
        tk.Label(side, text="Categories", bg=DARK_PANEL, fg=DARK_FG,
                 font=("", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 4))
        self.cat_var = tk.StringVar(value="All")
        for c in ["All", "Video", "Music", "Compressed", "Documents", "Programs", "Other", "Finished", "Unfinished"]:
            tk.Radiobutton(side, text=c, variable=self.cat_var, value=c, command=self.refresh,
                           bg=DARK_PANEL, fg=DARK_FG, selectcolor="#444", activebackground=DARK_PANEL,
                           activeforeground=DARK_FG, anchor="w").pack(fill="x", padx=6)

        cols = ("name", "size", "progress", "speed", "status", "category")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        for c, w in [("name", 260), ("size", 90), ("progress", 90),
                     ("speed", 90), ("status", 90), ("category", 90)]:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w)
        self.tree.pack(side="left", fill="both", expand=True)

        self.log_box = tk.Text(root, height=6, bg="#111", fg="#8bc34a", state="disabled")
        self.log_box.pack(fill="x", padx=6, pady=(0, 6))

        self.root.after(300, self.drain_log)
        self.root.after(500, self.refresh)

        self.bar = None
        self._toast_offset = 0
        self.seen_done = {jid for jid, j in JOBS.items() if j["status"] == "done"}
        root.protocol("WM_DELETE_WINDOW", self.minimize_to_bar)

        self._build_context_menu()
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.drag_source_register(1, DND_FILES)
        self.tree.bind("<<DragInitCmd>>", self._on_drag_init)
        self.tree.drop_target_register(DND_FILES)
        self.tree.dnd_bind("<<Drop>>", self._on_drop_files)

    def _build_context_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open", command=self._ctx_open)
        self.menu.add_command(label="Open with…", command=self._ctx_open_with)
        self.menu.add_command(label="Open folder", command=self._ctx_open_folder)
        self.menu.add_separator()
        self.menu.add_command(label="Move/Rename (Ctrl-M)", command=self._ctx_rename)
        self.menu.add_separator()
        self.menu.add_command(label="Redownload", command=self._ctx_redownload)
        self.menu.add_separator()
        self.menu.add_command(label="Resume Download", command=self._ctx_resume)
        self.menu.add_command(label="Stop Download", command=self._ctx_stop)
        self.menu.add_separator()
        self.menu.add_command(label="Refresh download address", command=self._ctx_refresh)
        self.menu.add_separator()
        self.menu.add_command(label="Remove", command=self._ctx_remove)
        self.menu.add_separator()
        addq = tk.Menu(self.menu, tearoff=0)
        for cat in ["Video", "Music", "Compressed", "Documents", "Programs", "Other"]:
            addq.add_command(label=cat, command=lambda c=cat: self._ctx_move_cat(c))
        self.menu.add_cascade(label="Add to queue", menu=addq)
        self.menu.add_command(label="Delete from queue", command=self._ctx_remove)
        self.menu.add_separator()
        dbl = tk.Menu(self.menu, tearoff=0)
        dbl.add_command(label="Open file", command=lambda: STATE.update({"double_click": "open"}))
        dbl.add_command(label="Open folder", command=lambda: STATE.update({"double_click": "folder"}))
        dbl.add_command(label="Show in Grabber", command=lambda: STATE.update({"double_click": "show"}))
        dbl.add_command(label="Nothing", command=lambda: STATE.update({"double_click": "none"}))
        self.menu.add_cascade(label="On Double click", menu=dbl)
        self.menu.add_separator()
        self.menu.add_command(label="Play", command=self._ctx_play)
        self.menu.add_command(label="Convert to ▸ MP4", command=lambda: self._ctx_convert("mp4"))
        self.menu.add_command(label="Convert to ▸ MKV", command=lambda: self._ctx_convert("mkv"))
        self.menu.add_command(label="Convert to ▸ MP3", command=lambda: self._ctx_convert("mp3"))
        self.menu.add_command(label="Convert to ▸ WAV", command=lambda: self._ctx_convert("wav"))
        self.menu.add_separator()
        self.menu.add_command(label="Properties", command=self._ctx_props)

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self._current_ctx = iid
        job = JOBS[iid]
        st = job["status"]
        ok = lambda cond: "normal" if cond else "disabled"
        self.menu.entryconfig("Open", state=ok(st == "done"))
        self.menu.entryconfig("Open with…", state=ok(st == "done"))
        self.menu.entryconfig("Open folder", state=ok(st == "done"))
        self.menu.entryconfig("Move/Rename (Ctrl-M)", state=ok(st == "done"))
        self.menu.entryconfig("Redownload", state=ok(st in ("done", "error", "stopped")))
        self.menu.entryconfig("Resume Download", state=ok(st in ("paused", "stopped")))
        self.menu.entryconfig("Stop Download", state=ok(st in ("downloading", "queued")))
        self.menu.entryconfig("Refresh download address", state=ok(st in ("error", "stopped")))
        self.menu.entryconfig("Play", state=ok(st == "done"))
        self.menu.post(event.x_root, event.y_root)

    def _ctx_open(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        p = stat_for(JOBS[jid])
        if p.exists():
            os.startfile(str(p))

    def _ctx_open_with(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        p = stat_for(JOBS[jid])
        if p.exists():
            try:
                os.startfile(str(p), "open")
            except Exception:
                pass

    def _ctx_open_folder(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        p = stat_for(JOBS[jid])
        if p.parent.exists():
            os.startfile(str(p.parent))

    def _ctx_rename(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        job = JOBS[jid]
        new = simpledialog.askstring("Rename", "New name:", initialvalue=job["filename"])
        if new:
            old = stat_for(job)
            job["filename"] = safe_filename(new)
            newp = stat_for(job)
            if old.exists() and old != newp:
                try:
                    os.replace(old, newp)
                except OSError as e:
                    messagebox.showerror("Rename", str(e))
            save_jobs_snapshot()

    def _ctx_redownload(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        job = JOBS[jid]
        job["stop_evt"].clear()
        job["pause_evt"].clear()
        cleanup_partial_files(job)
        job["status"] = "queued"
        job["size_done"] = 0
        job["size_total"] = 0

    def _ctx_resume(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        JOBS[jid]["pause_evt"].clear()
        JOBS[jid]["stop_evt"].clear()
        JOBS[jid]["status"] = "queued"

    def _ctx_stop(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        JOBS[jid]["stop_evt"].set()
        JOBS[jid]["pause_evt"].clear()

    def _ctx_refresh(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        job = JOBS[jid]
        try:
            r = requests.head(job["url"], allow_redirects=True, timeout=10)
            if r.status_code < 400:
                job["status"] = "queued"
                log(f"refreshed {jid}")
            else:
                messagebox.showwarning("Refresh", f"URL returned {r.status_code}")
        except Exception as e:
            messagebox.showerror("Refresh", str(e))

    def _ctx_remove(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        j = JOBS[jid]
        j["stop_evt"].set()
        cleanup_partial_files(j)
        JOBS.pop(jid, None)
        save_jobs_snapshot()

    def _ctx_move_cat(self, cat):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        JOBS[jid]["category"] = cat

    def _ctx_play(self):
        self._ctx_open()

    def _ctx_convert(self, fmt):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            requests.post(f"http://127.0.0.1:{APP_PORT}/convert/{jid}",
                          headers={"X-API-Token": STATE["api_token"]},
                          json={"format": fmt}, timeout=5)
        except Exception as e:
            messagebox.showerror("Convert", str(e))

    def _ctx_props(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid or jid not in JOBS:
            return
        j = JOBS[jid]
        p = stat_for(j)
        info = "\n".join(f"{k}: {v}" for k, v in [
            ("Name", j["filename"]), ("URL", j["url"]), ("Type", j["type"]),
            ("Status", j["status"]), ("Category", j["category"]),
            ("Size", human_size(j["size_total"])), ("Done", human_size(j["size_done"])),
            ("Path", str(p)), ("Exists", str(p.exists())),
        ])
        messagebox.showinfo("Properties", info)

    def _on_double_click(self, event):
        jid = self.tree.identify_row(event.y)
        if not jid:
            return
        action = STATE.get("double_click", "open")
        if action == "none":
            return
        self._current_ctx = jid
        if action == "open":
            self._ctx_open()
        elif action == "folder":
            self._ctx_open_folder()
        elif action == "show":
            self.tree.selection_set(jid)
            self.tree.see(jid)

    def _on_drag_init(self, event):
        paths = []
        for jid in self.tree.selection():
            if jid in JOBS and JOBS[jid]["status"] == "done":
                p = stat_for(JOBS[jid])
                if p.exists():
                    paths.append(str(p))
        if not paths:
            return ("refuse_drag", DND_FILES)
        return (DND_COPY, (DND_FILES, tuple(paths)))

    def _on_drop_files(self, event):
        raw = event.data
        items = re.findall(r"\{[^}]+\}|\S+", raw)
        for it in items:
            it = it.strip("{}")
            if it.startswith("http"):
                new_job(it)
            elif os.path.isfile(it):
                # Import local files by copying them into the output folder —
                # a file:// path can never work as a download job.
                try:
                    target_dir = Path(STATE["output_dir"])
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(it, target_dir / Path(it).name)
                    log(f"imported local file: {Path(it).name}")
                except Exception as e:
                    log(f"couldn't import '{it}': {e}")

    def selected_ids(self):
        return list(self.tree.selection())

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        filt = self.cat_var.get()
        for jid, j in sorted(JOBS.items(), key=lambda kv: -kv[1]["created_ts"]):
            if j["status"] == "done" and jid not in self.seen_done:
                self.seen_done.add(jid)
                self.show_toast(j)
            if filt == "Finished" and j["status"] != "done":
                continue
            if filt == "Unfinished" and j["status"] == "done":
                continue
            if filt not in ("All", "Finished", "Unfinished") and j["category"] != filt:
                continue
            pct = f"{(j['size_done']/j['size_total']*100):.0f}%" if j["size_total"] else ("100%" if j["status"] == "done" else "-")
            self.tree.insert("", "end", iid=jid, values=(
                j["filename"], human_size(j["size_total"]), pct, j["speed"], j["status"], j["category"],
            ))
        self._update_bar_dot()
        self.root.after(700, self.refresh)

    def minimize_to_bar(self):
        self.root.withdraw()
        if self.bar is not None:
            try:
                self.bar.deiconify()
                return
            except tk.TclError:
                self.bar = None
        self.bar = tk.Toplevel(self.root)
        self.bar.overrideredirect(True)
        self.bar.attributes("-topmost", True)
        sw, sh = self.bar.winfo_screenwidth(), self.bar.winfo_screenheight()
        w, h = 170, 34
        self.bar.geometry(f"{w}x{h}+{sw - w - 12}+{sh - h - 48}")
        frame = tk.Frame(self.bar, bg="#222", cursor="hand2")
        frame.pack(fill="both", expand=True)
        self.bar_dot = tk.Canvas(frame, width=14, height=14, bg="#222", highlightthickness=0)
        self.bar_dot_id = self.bar_dot.create_oval(2, 2, 12, 12, fill="#999", outline="")
        self.bar_dot.pack(side="left", padx=8)
        label = tk.Label(frame, text="Video Grabber", bg="#222", fg="#ddd", font=("", 9))
        label.pack(side="left")
        for w_ in (frame, self.bar_dot, label):
            w_.bind("<Button-1>", lambda e: self.restore_from_bar())
        self._update_bar_dot()

    def restore_from_bar(self):
        if self.bar is not None:
            try:
                self.bar.destroy()
            except tk.TclError:
                pass
            self.bar = None
        self.root.deiconify()
        self.root.lift()

    def _update_bar_dot(self):
        if self.bar is None:
            return
        try:
            active = any(j["status"] == "downloading" for j in JOBS.values())
            self.bar_dot.itemconfig(self.bar_dot_id, fill="#4caf50" if active else "#999")
        except tk.TclError:
            self.bar = None

    def show_toast(self, job):
        try:
            dest = stat_for(job)
        except Exception:
            dest = None

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        sw, sh = toast.winfo_screenwidth(), toast.winfo_screenheight()
        w, h = 300, 100
        offset = self._toast_offset
        toast.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 60 - offset}")
        self._toast_offset += h + 8

        frame = tk.Frame(toast, bg="#262626", highlightbackground="#444", highlightthickness=1)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Download complete", bg="#262626", fg="#4caf50",
                 font=("", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(frame, text=job["filename"], bg="#262626", fg="#ddd", font=("", 9),
                 wraplength=270, justify="left").pack(anchor="w", padx=10)

        def open_file():
            if dest and dest.exists():
                os.startfile(str(dest))

        def open_folder():
            if dest and dest.parent.exists():
                os.startfile(str(dest.parent))

        def dismiss():
            self._toast_offset = max(0, self._toast_offset - (h + 8))
            try:
                toast.destroy()
            except tk.TclError:
                pass

        btns = tk.Frame(frame, bg="#262626")
        btns.pack(anchor="w", padx=6, pady=6)
        tk.Button(btns, text="Open file", command=open_file, bg="#333", fg="#ddd",
                  relief="flat", padx=6).pack(side="left", padx=4)
        tk.Button(btns, text="Open folder", command=open_folder, bg="#333", fg="#ddd",
                  relief="flat", padx=6).pack(side="left", padx=4)
        tk.Button(frame, text="✕", command=dismiss, bg="#262626", fg="#999",
                  relief="flat", bd=0).place(x=w - 26, y=4)
        toast.after(6000, dismiss)

    def drain_log(self):
        try:
            while True:
                line = LOG_QUEUE.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(300, self.drain_log)

    def add_url(self):
        url = simpledialog.askstring("Add URL", "Paste a video / file / page URL:")
        if url:
            new_job(url.strip())

    def add_batch(self):
        win = tk.Toplevel(self.root)
        win.title("Add batch download")
        tk.Label(win, text="One URL per line:").pack(anchor="w", padx=8, pady=(8, 0))
        txt = tk.Text(win, width=70, height=12)
        txt.pack(padx=8, pady=8)

        def submit():
            for line in txt.get("1.0", "end").splitlines():
                line = line.strip()
                if line:
                    new_job(line)
            win.destroy()

        tk.Button(win, text="Add all", command=submit).pack(pady=(0, 8))

    def pause_sel(self):
        for jid in self.selected_ids():
            JOBS[jid]["pause_evt"].set()

    def resume_sel(self):
        for jid in self.selected_ids():
            j = JOBS[jid]
            if j["status"] == "done":
                continue
            j["pause_evt"].clear()
            j["stop_evt"].clear()
            j["status"] = "queued"

    def stop_sel(self):
        for jid in self.selected_ids():
            JOBS[jid]["stop_evt"].set()

    def delete_sel(self):
        for jid in self.selected_ids():
            j = JOBS[jid]
            j["stop_evt"].set()
            cleanup_partial_files(j)
            JOBS.pop(jid, None)
        save_jobs_snapshot()

    def delete_completed(self):
        for jid in [j for j, v in JOBS.items() if v["status"] == "done"]:
            JOBS.pop(jid, None)
        save_jobs_snapshot()

    def start_queue(self):
        STATE["queue_running"] = True
        save_settings()

    def stop_queue(self):
        STATE["queue_running"] = False
        save_settings()

    def copy_api_token(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(STATE["api_token"])
        messagebox.showinfo(
            "Pairing token copied",
            "Copied to clipboard. Open the Video Grabber extension's options page "
            "(right-click the toolbar icon → Options) and paste it in to pair.",
        )

    def open_options(self):
        win = tk.Toplevel(self.root)
        win.title("Options")
        win.geometry("560x520")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        g = ttk.Frame(nb)
        nb.add(g, text="General")
        clip_var = tk.BooleanVar(value=STATE["clipboard_monitor"])
        ttk.Checkbutton(g, text="Monitor clipboard for downloadable links",
                        variable=clip_var).pack(anchor="w", padx=8, pady=6)
        lan_var = tk.BooleanVar(value=STATE.get("lan_access", False))
        ttk.Checkbutton(g, text="Allow LAN access (unauthenticated web UI at /remote)",
                        variable=lan_var).pack(anchor="w", padx=8, pady=6)
        ttk.Label(g, text="API token (share with extension):").pack(anchor="w", padx=8, pady=(12, 2))
        tok_var = tk.StringVar(value=STATE["api_token"])
        ttk.Entry(g, textvariable=tok_var, width=50).pack(anchor="w", padx=8)

        def copy_tok():
            self.root.clipboard_clear()
            self.root.clipboard_append(tok_var.get())
        ttk.Button(g, text="Copy token", command=copy_tok).pack(anchor="w", padx=8, pady=4)

        ft = ttk.Frame(nb)
        nb.add(ft, text="File Types")
        ttk.Label(ft, text="Disable auto-capture for these extensions:").pack(anchor="w", padx=8, pady=6)
        ft_box = tk.Listbox(ft, selectmode="multiple", height=10)
        exts = sorted({k.lstrip(".") for k in CATEGORY_MAP})
        disabled = set(STATE.get("file_types_overrides", {}).keys())
        for e in exts:
            ft_box.insert("end", e)
            if e in disabled:
                ft_box.selection_set("end")
        ft_box.pack(fill="both", expand=True, padx=8, pady=4)

        st = ttk.Frame(nb)
        nb.add(st, text="Save to")
        ttk.Label(st, text="Default folder:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        dir_var = tk.StringVar(value=STATE["output_dir"])
        ttk.Entry(st, textvariable=dir_var, width=40).grid(row=0, column=1, padx=4)
        ttk.Button(st, text="Browse…", command=lambda: dir_var.set(
            filedialog.askdirectory(initialdir=dir_var.get()) or dir_var.get())
        ).grid(row=0, column=2, padx=4)
        percat_vars = {}
        for i, cat in enumerate(["Video", "Music", "Compressed", "Documents", "Programs", "Other"], start=1):
            ttk.Label(st, text=f"{cat} folder override:").grid(row=i, column=0, sticky="w", padx=8, pady=2)
            v = tk.StringVar(value=STATE.get("per_category_dirs", {}).get(cat, ""))
            percat_vars[cat] = v
            ttk.Entry(st, textvariable=v, width=40).grid(row=i, column=1, padx=4)
            ttk.Button(st, text="Browse…", command=lambda vv=v: vv.set(
                filedialog.askdirectory() or vv.get())).grid(row=i, column=2, padx=4)

        dl = ttk.Frame(nb)
        nb.add(dl, text="Downloads")
        ttk.Label(dl, text="Max simultaneous downloads:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        conc_var = tk.IntVar(value=STATE["max_concurrent"])
        ttk.Spinbox(dl, from_=1, to=16, textvariable=conc_var, width=5).grid(row=0, column=1, sticky="w")
        ttk.Label(dl, text="Speed limit (KB/s, 0=unlimited):").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        speed_var = tk.IntVar(value=STATE["speed_limit_kbps"])
        ttk.Entry(dl, textvariable=speed_var, width=8).grid(row=1, column=1, sticky="w")

        cn = ttk.Frame(nb)
        nb.add(cn, text="Connection")
        ttk.Label(cn, text="Timeout (seconds):").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        to_var = tk.IntVar(value=STATE.get("connection_timeout", 20))
        ttk.Spinbox(cn, from_=5, to=120, textvariable=to_var, width=5).grid(row=0, column=1, sticky="w")
        ttk.Label(cn, text="Max retries:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        rt_var = tk.IntVar(value=STATE.get("max_retries", 3))
        ttk.Spinbox(cn, from_=0, to=20, textvariable=rt_var, width=5).grid(row=1, column=1, sticky="w")
        ttk.Label(cn, text="Abort if below (KB/s, 0=off):").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ms_var = tk.IntVar(value=STATE.get("min_speed_kbps", 0))
        ttk.Entry(cn, textvariable=ms_var, width=8).grid(row=2, column=1, sticky="w")

        pd = ttk.Frame(nb)
        nb.add(pd, text="Post-Download")
        scan_var = tk.BooleanVar(value=STATE.get("defender_scan", False))
        shut_var = tk.BooleanVar(value=STATE.get("auto_shutdown", False))
        extr_var = tk.BooleanVar(value=STATE.get("auto_extract", False))
        opnf_var = tk.BooleanVar(value=STATE.get("open_folder_on_complete", False))
        snd_var = tk.BooleanVar(value=STATE.get("play_sound_on_complete", False))
        for text, var in [("Scan downloads with Windows Defender", scan_var),
                          ("Shut down PC when queue empties", shut_var),
                          ("Auto-extract archives after download", extr_var),
                          ("Open containing folder on completion", opnf_var),
                          ("Play sound on completion", snd_var)]:
            ttk.Checkbutton(pd, text=text, variable=var).pack(anchor="w", padx=8, pady=4)

        def save():
            STATE["clipboard_monitor"] = clip_var.get()
            STATE["lan_access"] = lan_var.get()
            STATE["output_dir"] = dir_var.get()
            STATE["max_concurrent"] = conc_var.get()
            STATE["speed_limit_kbps"] = speed_var.get()
            STATE["connection_timeout"] = to_var.get()
            STATE["max_retries"] = rt_var.get()
            STATE["min_speed_kbps"] = ms_var.get()
            STATE["defender_scan"] = scan_var.get()
            STATE["auto_shutdown"] = shut_var.get()
            STATE["auto_extract"] = extr_var.get()
            STATE["open_folder_on_complete"] = opnf_var.get()
            STATE["play_sound_on_complete"] = snd_var.get()
            STATE["per_category_dirs"] = {c: v.get() for c, v in percat_vars.items() if v.get()}
            new_overrides = {}
            for i, e in enumerate(exts):
                if i in ft_box.curselection():
                    new_overrides[e] = False
            STATE["file_types_overrides"] = new_overrides
            save_settings()
            win.destroy()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="Reset to defaults",
                   command=self._reset_options).pack(side="left")
        ttk.Button(btn_frame, text="Save", command=save).pack(side="right")

    def _reset_options(self):
        if messagebox.askyesno("Reset", "Reset all options to defaults?"):
            defaults = {"max_concurrent": 3, "speed_limit_kbps": 0,
                        "double_click": "open", "rules": [],
                        "clipboard_monitor": False, "lan_access": False,
                        "connection_timeout": 20, "max_retries": 3,
                        "min_speed_kbps": 0, "defender_scan": False,
                        "auto_shutdown": False, "auto_extract": False,
                        "open_folder_on_complete": False, "play_sound_on_complete": False,
                        "per_category_dirs": {}, "file_types_overrides": {}}
            STATE.update(defaults)
            save_settings()

    def open_scheduler(self):
        win = tk.Toplevel(self.root)
        win.title("Scheduler")
        tk.Label(win, text="Start queue at (HH:MM, 24h, today):").grid(row=0, column=0, padx=8, pady=8)
        time_var = tk.StringVar()
        tk.Entry(win, textvariable=time_var, width=8).grid(row=0, column=1)

        def arm():
            target = time_var.get().strip()
            try:
                hh, mm = map(int, target.split(":"))
            except Exception:
                messagebox.showerror("Scheduler", "Use HH:MM, e.g. 23:30")
                return

            def waiter():
                while True:
                    now = time.localtime()
                    if now.tm_hour == hh and now.tm_min == mm:
                        STATE["queue_running"] = True
                        log(f"Scheduler: queue started at {target}")
                        return
                    time.sleep(20)

            threading.Thread(target=waiter, daemon=True).start()
            log(f"Scheduler armed for {target}")
            win.destroy()

        tk.Button(win, text="Arm", command=arm).grid(row=1, column=0, columnspan=2, pady=8)

    def export_queue(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="queue.json")
        if not path:
            return
        data = [{"url": j["url"], "filename": j["filename"], "category": j["category"]} for j in JOBS.values()]
        Path(path).write_text(json.dumps(data, indent=2))

    def import_queue(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = json.loads(Path(path).read_text())
        for item in data:
            new_job(item["url"], filename=item.get("filename"), category=item.get("category"))


def _prompt_and_maybe_add(content):
    if messagebox.askyesno("Video Grabber", f"Download this from clipboard?\n\n{content[:120]}"):
        new_job(content.strip())


def start_clipboard_watcher(root):
    """Poll the clipboard on the Tk main thread — Tk calls aren't thread-safe."""
    last_seen = {"value": None}

    def tick():
        if STATE["clipboard_monitor"]:
            content = None
            try:
                content = root.clipboard_get()
            except Exception:
                content = None
            if (content and content != last_seen["value"]
                    and content.startswith("http") and DIRECT_EXT_RE.search(content)):
                last_seen["value"] = content
                _prompt_and_maybe_add(content)
        root.after(1500, tick)

    root.after(1500, tick)


def main():
    global HOME, CONFIG_PATH, JOBS_PATH
    h = portable_home()
    HOME = h
    CONFIG_PATH = HOME / "settings.json"
    JOBS_PATH = HOME / "jobs.json"
    HOME.mkdir(parents=True, exist_ok=True)
    load_settings()
    load_jobs_snapshot()

    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=dispatcher_loop, daemon=True).start()
    log(f"Server started on port {APP_PORT}. Saving to {STATE['output_dir']}")

    root = TkinterDnD.Tk()
    gui = GUI(root)
    start_clipboard_watcher(root)
    root.mainloop()
    save_jobs_snapshot()


if __name__ == "__main__":
    main()