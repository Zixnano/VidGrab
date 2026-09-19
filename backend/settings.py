"""Application settings, paths, filename helpers, bandwidth profiles."""
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlparse
import glob
import json
import os
import re
import requests
import secrets
import sys
import threading
import time

from logging_setup import log, LOG_QUEUE


APP_VERSION = "4.0.9"  # single source of truth; keep in sync with extension/manifest.json
APP_PORT = int(os.environ.get("VIDEOGRABBER_PORT", "5757"))
# Recording upload size cap (nonce-gated /upload endpoint).
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"
JOBS_PATH = HOME / "jobs.json"

# Canonical category list (single source of truth — gui_qt.py used to keep
# its own duplicate copy of this; category_for() below always falls back
# to "Other" for unmapped extensions, so it's included here too).
CATEGORIES = ("Video", "Music", "Compressed", "Documents", "Programs", "Other")

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
    "force_on_top": True,
    "watch_recording_folder": True,
    "skip_add_dialog": False,
    "recording_watch_dirs": [],
    "auto_mp4": True,
    "close_to_tray": True,
    "bandwidth_profiles": [],
    "bandwidth_profiles_enabled": False,
    # --- v4 schema additions (Task 1.2) ---
    "accent": "#26c6da",
    "theme_preset": "amoled_black",
    "theme_tokens": {},
    "animations_enabled": True,
    "preferred_engines": [],
    "engines_disabled": [],
    "per_site_engine": {},
    "prefer_source_quality": True,
    "subtitle_languages": ["en"],
    "settings_version": 4,
}

_V4_DEFAULTS = {k: v for k, v in list(STATE.items())[-12:]}


def _migrate_settings(old):
    """Fill any keys missing from a v3.x settings.json with v4 defaults."""
    for k, v in _V4_DEFAULTS.items():
        old.setdefault(k, v)


_SHUTDOWN_PENDING = False


def get_shutdown_pending():
    return _SHUTDOWN_PENDING


def set_shutdown_pending(v):
    global _SHUTDOWN_PENDING
    _SHUTDOWN_PENDING = v


def set_paths(h):
    """main() calls this after resolving the portable home directory."""
    global HOME, CONFIG_PATH, JOBS_PATH
    HOME = h
    CONFIG_PATH = HOME / "settings.json"
    JOBS_PATH = HOME / "jobs.json"


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
        except Exception as e:
            log(f"couldn't read settings.json ({e}); using defaults")
    _migrate_settings(STATE)
    if not STATE.get("api_token"):
        STATE["api_token"] = secrets.token_hex(16)
        save_settings()


def save_settings():
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(STATE, indent=2))
    os.replace(tmp, CONFIG_PATH)



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


_CT_EXT_MAP = [
    ("video/mp4", ".mp4"), ("video/webm", ".webm"), ("video/x-matroska", ".mkv"),
    ("video/quicktime", ".mov"), ("video/x-msvideo", ".avi"), ("video/mp2t", ".ts"),
    ("audio/mpeg", ".mp3"), ("audio/mp4", ".m4a"), ("audio/x-m4a", ".m4a"),
    ("audio/wav", ".wav"), ("audio/x-wav", ".wav"), ("audio/flac", ".flac"),
    ("audio/aac", ".aac"), ("application/zip", ".zip"),
    ("application/x-zip-compressed", ".zip"), ("application/x-7z-compressed", ".7z"),
    ("application/x-rar-compressed", ".rar"), ("application/pdf", ".pdf"),
    ("image/jpeg", ".jpg"), ("image/png", ".png"), ("image/gif", ".gif"),
]
_HEAD_EXT_CACHE = {}


def guess_ext_from_head(url):
    """HEAD the URL and map Content-Type to an extension. Cached per-URL so
    repeated guesses (restored jobs, retries) don't re-hit the server."""
    if url in _HEAD_EXT_CACHE:
        return _HEAD_EXT_CACHE[url]
    ext = ""
    try:
        r = requests.head(url, allow_redirects=True,
                          timeout=STATE["connection_timeout"])
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        for mime, e in _CT_EXT_MAP:
            if ct == mime:
                ext = e
                break
        if not ext:
            cd = r.headers.get("Content-Disposition")
            if cd:
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
                if m:
                    ext = os.path.splitext(unquote(m.group(1)))[1]
    except Exception:
        pass
    if ext:
        _HEAD_EXT_CACHE[url] = ext
        if len(_HEAD_EXT_CACHE) > 5000:
            _HEAD_EXT_CACHE.clear()
    return ext


def guess_filename(url, content_disposition=None):
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if m:
            return safe_filename(unquote(m.group(1)))
    path = urlparse(url).path
    name = os.path.basename(path) or "download"
    return safe_filename(unquote(name))


def _unique_path(path):
    """If path exists, append ' (2)', ' (3)', ... before the extension."""
    if not path.exists():
        return path
    stem, ext = os.path.splitext(path.name)
    i = 2
    while True:
        candidate = path.with_name(f"{stem} ({i}){ext}")
        if not candidate.exists():
            return candidate
        i += 1
        if i > 1000:
            return path  # give up, unlikely


def category_for(filename):
    ext = os.path.splitext(filename)[1].lower()
    return CATEGORY_MAP.get(ext, "Other")


def detect_type(url):
    if ".m3u8" in url.lower() or ".mpd" in url.lower():
        return "ytdlp"
    if DIRECT_EXT_RE.search(url):
        return "generic"
    return "ytdlp"


def _active_bandwidth_profile():
    """Return the first enabled profile whose time window covers now, or None.

    Profiles are dicts: {"name", "start": "HH:MM", "end": "HH:MM",
    "speed_limit_kbps": int}. Overnight windows (start > end) wrap midnight.
    """
    if not STATE.get("bandwidth_profiles_enabled"):
        return None
    now = time.localtime()
    mins = now.tm_hour * 60 + now.tm_min
    for p in STATE.get("bandwidth_profiles") or []:
        try:
            sh, sm = map(int, str(p.get("start", "")).split(":"))
            eh, em = map(int, str(p.get("end", "")).split(":"))
            start = sh * 60 + sm
            end = eh * 60 + em
        except Exception:
            continue
        if start == end:
            continue
        if start < end:
            in_range = start <= mins < end
        else:
            in_range = mins >= start or mins < end
        if in_range:
            return p
    return None


def effective_speed_limit_kbps():
    """Active bandwidth-profile limit if enabled and matching; else global."""
    p = _active_bandwidth_profile()
    if p is not None:
        try:
            return int(p.get("speed_limit_kbps", 0))
        except (TypeError, ValueError):
            return 0
    return int(STATE.get("speed_limit_kbps", 0) or 0)


class SpeedLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = time.time()
        self.tokens = 0.0

    def consume(self, n):
        limit = effective_speed_limit_kbps() * 1024
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


def start_bandwidth_profile_watcher():
    """Log when the active bandwidth profile changes (limit applied live)."""
    last = {"key": None}

    def loop():
        while True:
            try:
                p = _active_bandwidth_profile()
                if p is None:
                    key = ("off", effective_speed_limit_kbps())
                else:
                    key = (p.get("name") or p.get("start"),
                           int(p.get("speed_limit_kbps", 0) or 0))
                if key != last["key"]:
                    last["key"] = key
                    if not STATE.get("bandwidth_profiles_enabled"):
                        pass
                    elif p is None:
                        log(f"Bandwidth profile: none active "
                            f"(global {effective_speed_limit_kbps()} KB/s)")
                    else:
                        lim = int(p.get("speed_limit_kbps", 0) or 0)
                        label = p.get("name") or f"{p.get('start')}-{p.get('end')}"
                        lim_s = "unlimited" if lim <= 0 else f"{lim} KB/s"
                        log(f"Bandwidth profile active: {label} → {lim_s}")
            except Exception as e:
                log(f"bandwidth watcher: {e}")
            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()



def disk_usage_for(path=None):
    """Free/total/used bytes for the drive holding `path` (default:
    STATE['output_dir']). Falls back to HOME if the configured output_dir
    doesn't exist yet (e.g. a per-category override the user hasn't
    created). Returns a dict; never raises."""
    import shutil as _shutil
    target = Path(path) if path else Path(STATE.get("output_dir") or HOME)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        total, used, free = _shutil.disk_usage(target)
    except OSError:
        total = used = free = 0
    return {"path": str(target), "total": total, "used": used, "free": free}


def _dest_for(job):
    override = STATE.get("per_category_dirs", {}).get(job["category"])
    base = Path(override) if override else Path(STATE["output_dir"])
    cat_dir = base if override else base / job["category"]
    return cat_dir / safe_filename(job["filename"])


def stat_for(job):
    dest = _dest_for(job)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Collision handling: "name (2).ext" — but ONLY when the job hasn't
    # started yet (no .part file, no bytes downloaded), otherwise we'd
    # rename a file mid-resume. Side effect on job["filename"] is
    # intentional: _dest_for stays pure and every caller agrees on the name.
    part = dest.with_suffix(dest.suffix + ".part")
    segments = []
    if dest.parent.exists():
        segments = list(dest.parent.glob(glob.escape(dest.name) + ".part*"))
    if (not part.exists() and not segments
            and not job.get("size_done") and dest.exists()):
        unique = _unique_path(dest)
        if unique.name != dest.name:
            job["filename"] = unique.name
            dest = unique
    return dest


