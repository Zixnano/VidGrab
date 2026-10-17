"""Application settings, paths, filename helpers, bandwidth profiles."""
from pathlib import Path
from urllib.parse import unquote
import copy
from urllib.parse import urlparse, parse_qs
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


def _read_install_version():
    """Real installed version from install-version.txt (written by the
    updater/installer). Returns "" if the file can't be found or looks wrong.
    Display only: APP_VERSION above stays untouched for anything that
    compares against it."""
    bases = []
    try:
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            bases += [exe_dir, exe_dir.parent]
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                bases.append(Path(meipass))
        here = Path(__file__).resolve().parent
        bases += [here, here.parent]
    except Exception:
        return ""
    for base in bases:
        try:
            p = base / "install-version.txt"
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
                line = text.splitlines()[0].strip() if text else ""
                line = line.lstrip("vV")
                if re.fullmatch(r"[0-9A-Za-z._+-]{1,32}", line):
                    return line
        except Exception:
            continue
    return ""


DISPLAY_VERSION = _read_install_version() or APP_VERSION
APP_PORT = int(os.environ.get("VIDEOGRABBER_PORT", "5757"))
# Recording upload size cap (nonce-gated /upload endpoint).
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"
JOBS_PATH = HOME / "jobs.json"

# Canonical category list (single source of truth — gui_qt.py used to keep
# its own duplicate copy of this; category_for() below always falls back
# to "Other" for unmapped extensions, so it's included here too).
CATEGORIES = ("Video", "Music", "Images", "Compressed", "Documents", "Programs", "Fonts", "Other")

# Category -> extensions, kept as sets and derived into the lookup dict so
# the two can never drift apart. Anything not listed lands in "Other".
# "Images" and "Fonts" are real sidebar categories: the app is a general
# download manager, not just video. (If the GUI sidebar keeps its own copy
# of the category list, add the two new names there too.)
_CATEGORY_EXTS = {
    "Video": {"mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "wmv", "mpg", "mpeg",
              "ts", "m2ts", "mts", "vob", "ogv", "divx", "rm", "rmvb", "asf", "mxf",
              "3gp", "3g2"},
    "Music": {"mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma", "aiff", "aif",
              "alac", "ape", "m4b", "mid", "midi", "amr"},
    "Images": {"jpg", "jpeg", "jpe", "png", "gif", "webp", "avif", "heic", "heif", "bmp",
               "svg", "ico", "tif", "tiff", "jxl", "dng", "cr2", "nef", "raw", "psd",
               "xcf"},
    "Compressed": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "lz", "lzma", "zst",
                   "cab", "iso", "img", "tgz",
                   # compound forms, matched longest-first by split_ext():
                   "tar.gz", "tar.bz2", "tar.xz"},
    "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
                  "rtf", "odt", "ods", "odp", "csv", "tsv", "json", "xml", "yaml",
                  "yml", "epub", "mobi", "azw3"},
    "Programs": {"exe", "msi", "dmg", "deb", "rpm", "apk", "appimage", "bat", "cmd",
                 "sh", "ps1", "jar", "py", "js", "appx"},
    "Fonts": {"ttf", "otf", "woff", "woff2", "eot"},
}
CATEGORY_MAP = {("." + ext): cat for cat, exts in _CATEGORY_EXTS.items()
                for ext in exts}

# Compound suffixes matched longest-first: os.path.splitext("file.tar.gz")
# returns ".gz", hiding the real archive type. ".tgz" is tar+gzip too.
_COMPOUND_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")


def split_ext(name):
    """file.tar.gz -> (stem, '.tar.gz'); everything else defers to
    os.path.splitext. Longest compound suffix wins."""
    low = (name or "").lower()
    for ext in _COMPOUND_EXTS:
        if low.endswith(ext):
            return name[:-len(ext)], ext
    return os.path.splitext(name)
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

# Explicit on purpose: this used to be a slice of the last 12 STATE items,
# which silently changed whenever a key was added to STATE. Same 12 keys and
# values as the old slice; fresh containers so nothing aliases STATE.
_V4_DEFAULTS = {
    "bandwidth_profiles": [],
    "bandwidth_profiles_enabled": False,
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

# --- v5 additions. Kept in their own dict (NOT appended to STATE above):
# _V4_DEFAULTS slices the last 12 STATE items, so appending keys there
# would silently shift that window and drop v4 defaults from migration.
_V5_DEFAULTS = {
    # YouTube request pacing (yt-dlp jobs on YouTube hosts only).
    # sleep_interval/max: random gap in seconds between YouTube job starts.
    # 0 for max disables pacing entirely.
    "ytdlp_sleep_interval": 5,
    "ytdlp_max_sleep_interval": 15,
    "ytdlp_sleep_interval_requests": 1,
    "ytdlp_sleep_interval_subtitles": 1,
    # How long YouTube jobs are held after a "not a bot" check. Manual
    # Resume/Redownload clears the hold (e.g. after switching VPN).
    "ytdlp_bot_cooldown_minutes": 30,
    # Optional proxy (http/https/socks4/socks5/socks5h URL). Scope "youtube"
    # sends only YouTube traffic through it; "all" sends every download.
    "proxy_url": "",
    "proxy_scope": "youtube",
    # Pause starting new jobs when free space on the output drive drops
    # below this many MB (0 disables).
    "min_free_space_mb": 500,
    # Background update checks.
    "auto_update_check": True,
    "update_check_hours": 6,
    "skipped_update_tag": "",
    "ytdlp_auto_update": True,
    # Legacy whole-playlist jobs skip videos already in archive.txt.
    "ytdlp_use_archive": True,
    # GUI: hide failed rows older than this many hours from All/Unfinished
    # (the Failed view still lists them). 0 = never hide.
    "hide_old_errors_hours": 24,
    # GUI: one tray notification when a batch of 2+ downloads finishes.
    "notify_queue_done": True,
    # YouTube player-client override (v5.1). Empty list = yt-dlp default.
    # Set to ["mweb", "tv", "web_safari"] to force the bgutil PO token
    # provider to fire (see bgutil-ytdlp-pot-provider issue #253).
    # Only applied to YouTube URLs. Non-YouTube jobs are unaffected.
    "ytdlp_player_clients": [],
    # GUI (v5.2): optional handheld shell around the main window.
    # shell_color is one of: cream, black, indigo, silver.
    "shell_enabled": False,
    "shell_color": "cream",
    # Backlight on = normal screen. Off = dim green tint (click the power light).
    "shell_backlight": True,
    # Per-part shell color overrides, e.g. {"dpad": "#3a0808"}. Empty string = no override.
    "shell_colors": {},
    # Cream window frame (toolbar, menu bar, status bar, margins) around the dark panels.
    "frame_cream": False,
}
STATE.update(_V5_DEFAULTS)


def _migrate_settings(old):
    """Fill any keys missing from an older settings.json with v4/v5 defaults."""
    for k, v in _V4_DEFAULTS.items():
        old.setdefault(k, copy.deepcopy(v))
    for k, v in _V5_DEFAULTS.items():
        old.setdefault(k, copy.deepcopy(v))


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


# Windows device names: a file called CON, NUL.txt or com1.tar.gz cannot be
# created (or opened) on NTFS, with or without an extension.
_WIN_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)])


def safe_filename(name):
    """Strip Windows-illegal characters, collapse whitespace, trim trailing
    dots/spaces, and cap the length so the resulting file can be created.
    Reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9) get a "_"
    prefix; the extension is preserved."""
    name = ILLEGAL_FILENAME_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if not name:
        name = "download"
    # Windows matches the device name before the FIRST dot, so "con.tar.gz"
    # is reserved too, not just a bare "con".
    if name.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED_NAMES:
        name = "_" + name
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[: 180 - len(ext)] + ext
    return name


# Whole destination paths stay under this many characters (Windows
# MAX_PATH is 260). The reserve leaves room for the suffixes added around
# a file name: ".part7", ".merge", " (12)".
_PATH_BUDGET = 240
_PATH_SUFFIX_RESERVE = 16


def truncate_for_dir(base_dir, category, subdir, name):
    """Shorten `name` so base_dir/category/subdir/name (plus the suffix
    reserve) stays inside _PATH_BUDGET. Keeps the extension. Pass "" for a
    category or subdir that is not part of the path. A name that already
    fits is returned unchanged."""
    try:
        d = Path(base_dir)
        if category:
            d = d / category
        if subdir:
            d = d / subdir
        room = _PATH_BUDGET - _PATH_SUFFIX_RESERVE - len(str(d)) - 1
    except Exception:
        return name
    if len(name) <= room:
        return name
    # A directory this deep is the user's problem; never shrink to nothing.
    room = max(room, 24)
    stem, ext = os.path.splitext(name)
    if len(ext) > 16:
        stem, ext = name, ""
    keep = max(room - len(ext), 8)
    return (stem[:keep].rstrip(". ") or "download") + ext


_CT_EXT_MAP = [
    ("video/mp4", ".mp4"), ("video/webm", ".webm"), ("video/x-matroska", ".mkv"),
    ("video/quicktime", ".mov"), ("video/x-msvideo", ".avi"), ("video/mp2t", ".ts"),
    ("audio/mpeg", ".mp3"), ("audio/mp4", ".m4a"), ("audio/x-m4a", ".m4a"),
    ("audio/wav", ".wav"), ("audio/x-wav", ".wav"), ("audio/flac", ".flac"),
    ("audio/aac", ".aac"), ("audio/ogg", ".ogg"), ("audio/opus", ".opus"),
    ("application/zip", ".zip"),
    ("application/x-zip-compressed", ".zip"), ("application/x-7z-compressed", ".7z"),
    ("application/x-rar-compressed", ".rar"), ("application/pdf", ".pdf"),
    ("application/epub+zip", ".epub"), ("application/x-mobipocket-ebook", ".mobi"),
    ("application/json", ".json"), ("text/markdown", ".md"),
    ("application/x-tar", ".tar"), ("application/gzip", ".gz"),
    ("application/x-bzip2", ".bz2"), ("application/x-xz", ".xz"),
    ("application/zstd", ".zst"), ("application/x-iso9660-image", ".iso"),
    ("image/jpeg", ".jpg"), ("image/png", ".png"), ("image/gif", ".gif"),
    ("image/webp", ".webp"), ("image/avif", ".avif"), ("image/heic", ".heic"),
    ("image/tiff", ".tiff"), ("image/bmp", ".bmp"), ("image/svg+xml", ".svg"),
    ("font/woff2", ".woff2"), ("font/woff", ".woff"),
    ("application/font-woff", ".woff"), ("application/vnd.ms-fontobject", ".eot"),
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


_QUERY_NAME_KEYS = ("fn", "filename", "file", "name", "file_name", "path")

# "?format=jpg" / "?f=mp4" style extension hints (pbs.twimg.com and friends).
# Anchored on purpose: "?format=json" must NOT match.
_QUERY_FORMAT_KEYS = ("format", "f")
_QUERY_FORMAT_RE = re.compile(
    r"^(?:jpg|jpeg|png|webp|gif|avif|bmp|svg|tif|tiff|heic|heif|"
    r"mp4|webm|mov|m4v|mkv|mp3|m4a|wav|flac|opus|ogg)$", re.I)


def _filename_from_query(url):
    """Pick a filename out of a query string (fn=, filename=, ...).
    Returns "" when none looks usable."""
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return ""
    for key in _QUERY_NAME_KEYS:
        for v in qs.get(key, []):
            v = (v or "").strip()
            if not v:
                continue
            if key == "path":
                # "?path=/mnt/user-data/outputs/report.zip" (claude.ai) or a
                # Windows-style path: normalise separators, decode again
                # (the value is often double-encoded), keep the basename.
                base = os.path.basename(unquote(v).replace("\\", "/"))
                if base and os.path.splitext(base)[1] and len(base) < 200:
                    return base
                continue
            # Only accept values that look like a filename with an
            # extension: "report.pdf" yes, "abc123" no, "a/b" no.
            base = os.path.basename(v.replace("\\", "/"))
            if base and os.path.splitext(base)[1] and len(base) < 200:
                return unquote(base)
    # No usable name key: accept "format=jpg" when the URL path itself has
    # no extension (https://pbs.twimg.com/media/ABC?format=jpg&name=large).
    try:
        path_base = unquote(os.path.basename(urlparse(url).path))
    except ValueError:
        path_base = ""
    if path_base and not os.path.splitext(path_base)[1]:
        for v in qs.get("format", []):
            v = (v or "").strip().lower()
            if _QUERY_FORMAT_RE.match(v):
                return safe_filename(f"{path_base}.{v}")
    return ""


def guess_filename(url, content_disposition=None):
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if m:
            return safe_filename(unquote(m.group(1)))
    qname = _filename_from_query(url)
    if qname:
        return safe_filename(qname)
    try:
        path = urlparse(url).path
    except ValueError:
        path = ""
    name = os.path.basename(path) or "download"
    return safe_filename(unquote(name))


_REDDIT_CDN_HOST_RE = re.compile(r"(^|\.)(v|packaged-media)\.redd\.it$", re.I)


def preferred_stem(job):
    """Title-derived filename stem for Reddit CDN downloads, else None.

    Reddit's CDN names files m2-res_1280p.mp4 / DASH_720.mp4. When the job
    carries the post's page_url (".../comments/<id>/<slug>/"), the slug is a
    better stem. NOTE: jobs do not store page_url yet (new_job only derives
    source_site from it), so this returns None until that field exists.
    Nothing calls it yet."""
    try:
        host = urlparse(job.get("url") or "").hostname or ""
        page = job.get("page_url")
        if not page or not _REDDIT_CDN_HOST_RE.search(host):
            return None
        parts = [p for p in urlparse(page).path.split("/") if p]
        if "comments" in parts:
            i = parts.index("comments")
            if len(parts) > i + 2:
                slug = unquote(parts[i + 2]).replace("_", " ").strip()
                if slug:
                    return safe_filename(slug)
    except Exception:
        pass
    return None


# Only suffixes _unique_path itself would have added: " (2)" to " (999)".
# A year or other real number ("Report (2019).pdf") is part of the name.
_UNIQUE_SUFFIX_RE = re.compile(r"(?: \((?:[2-9]|[1-9]\d{1,2})\))+$")


def _unique_path(path, taken=None):
    """If path exists, append ' (2)', ' (3)', ... before the extension.

    `taken` is an optional collection of lower-cased path strings that count
    as occupied even though no file exists yet (in-flight reservations, see
    downloader._job_dest). Raises RuntimeError after 1000 collisions: the
    old behaviour of returning the existing path made callers overwrite it."""
    def _busy(p):
        return p.exists() or (taken is not None and str(p).lower() in taken)

    if not _busy(path):
        return path
    stem, ext = os.path.splitext(path.name)
    # Collapse existing " (N)" suffixes first so repeats never stack into
    # "name (2) (2).mp4".
    stem = _UNIQUE_SUFFIX_RE.sub("", stem) or stem
    for i in range(2, 1001):
        candidate = path.with_name(f"{stem} ({i}){ext}")
        if not _busy(candidate):
            return candidate
    raise RuntimeError(f"no free file name for {path.name} after 1000 tries")


def category_for(filename):
    _, ext = split_ext(filename)
    return CATEGORY_MAP.get(ext.lower(), "Other")


# Any URL whose path ends in one of these extensions is a plain file
# fetch, not something yt-dlp should extract. Covers media files the
# user might want, AND arbitrary files (scripts, archives, docs) that
# shouldn't be pushed through yt-dlp's extractor pipeline at all.
# html/htm/php are deliberately NOT listed: a page URL such as
# https://site.com/v/123.html should try yt-dlp first (it is usually a video
# page), not be saved as an HTML file. It is matched against the URL PATH only.
# One tuple drives BOTH the path check and the query-value check below, so
# they can never drift. Covers every family _CATEGORY_EXTS knows about (the
# app is a general download manager) plus code files.
# html/htm/php are deliberately NOT listed: a page URL such as
# https://site.com/v/123.html should try yt-dlp first (it is usually a video
# page), not be saved as an HTML file. It is matched against the URL PATH only.
_PLAIN_FILE_EXTS = (
    "mp4|m4v|mov|webm|mkv|avi|flv|wmv|mpg|mpeg|ts|m2ts|mts|vob|ogv|divx|rm|rmvb|asf|mxf|3gp|3g2|"
    "mp3|wav|flac|m4a|aac|ogg|opus|wma|aiff|aif|alac|ape|m4b|mid|midi|amr|"
    "zip|rar|7z|tar|gz|bz2|xz|lz|lzma|zst|cab|iso|img|tgz|"
    "pdf|docx?|xlsx?|pptx?|txt|md|rtf|csv|tsv|json|xml|yaml|yml|odt|ods|odp|epub|mobi|azw3|"
    "css|"
    "py|js|ts|java|c|cpp|h|hpp|rs|go|rb|sh|bat|cmd|ps1|"
    "exe|msi|dmg|deb|rpm|apk|appimage|appx|jar|"
    "jpg|jpeg|jpe|png|gif|webp|avif|svg|bmp|ico|tif|tiff|heic|heif|jxl|psd|raw|dng|cr2|nef|xcf|"
    "ttf|otf|woff2?|eot"
)
_PLAIN_FILE_EXT_RE = re.compile(r"\.(" + _PLAIN_FILE_EXTS + r")(\?|#|$)", re.I)

# Page extensions: a URL ending in one of these is a document page, not a
# file — yt-dlp gets first crack at it (its generic extractor can still pull
# a player out of the page).
_PAGE_EXT_RE = re.compile(r"\.(html?|php|aspx?|jsp|do)(\?|#|$)", re.I)
# ANY other dotted suffix on the path means "some file", even an extension
# we don't know: the generic engine just copies bytes and names it from the
# URL, while yt-dlp would download it and name the result the infamous
# "UNKNOWN_VIDEO" (an .epub URL used to land that way before .epub was in
# _PLAIN_FILE_EXTS). At least one letter required, so "/v/1.5" is not a file.
_EXT_LIKE_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}(\?|#|$)")

# Query params that commonly carry the filename (Dropbox, ChatGPT,
# S3 presigned URLs, Google Drive, generic file hosts).
_FILENAME_QUERY_KEYS = ("fn", "filename", "file", "name", "file_name")


def detect_type(url):
    if ".m3u8" in url.lower() or ".mpd" in url.lower():
        return "ytdlp"
    # Path only: an extension-looking string in a query value or host must
    # not route a page URL to the generic downloader (the query-value check
    # below handles fn=/filename=/format= on its own terms).
    try:
        _url_path = urlparse(url).path
    except ValueError:
        _url_path = ""
    if _PLAIN_FILE_EXT_RE.search(_url_path):
        return "generic"
    # Filename in the query string? (fn=..., filename=..., file=...)
    # Match the extension regex against each value; anchor it so
    # "?fn=foo.pdf" matches but "?fn=foo.pdfbar" doesn't.
    try:
        qs = parse_qs(urlparse(url).query)
        for key in _FILENAME_QUERY_KEYS:
            for v in qs.get(key, []):
                if re.search(r"\.(" + _PLAIN_FILE_EXTS + r"|html?|php)$",
                             (v or "").strip(), re.I):
                    return "generic"
        # "?format=jpg" / "?f=mp4": value is a bare extension (anchored).
        for key in _QUERY_FORMAT_KEYS:
            for v in qs.get(key, []):
                if _QUERY_FORMAT_RE.match((v or "").strip()):
                    return "generic"
    except Exception:
        pass
    # No filename signal at all from here on. Page-like addresses keep the
    # yt-dlp default (embedded-player extraction is still worth a try), but
    # any other dotted suffix is a plain file even when we don't know the
    # extension — see _EXT_LIKE_RE above for why.
    if _PAGE_EXT_RE.search(_url_path):
        return "ytdlp"
    # An "@handle" last segment (youtube.com/@some.handle, tiktok.com/@john.doe)
    # is a channel or profile page, not a file, even though handles may hold dots.
    _last_seg = _url_path.rstrip("/").rsplit("/", 1)[-1]
    if _EXT_LIKE_RE.search(_url_path) and not _last_seg.startswith("@"):
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
    # NOTE: gui_qt._path_for mirrors this logic - keep the two in sync.
    override = STATE.get("per_category_dirs", {}).get(job["category"])
    base = Path(override) if override else Path(STATE["output_dir"])
    # The category comes from rules, /category and the API, so it is
    # sanitised before it is joined: "..\\x" or "D:\\x" must not escape the
    # output folder. (The per_category_dirs lookup above uses the raw name.)
    cat = "" if override else safe_filename(job["category"] or "Other")
    cat_dir = base / cat if cat else base
    # Session D: per-item playlist jobs land in <category>/<subdir>/. subdir
    # is a real path component (not the legacy playlist_dir display hint).
    sub = safe_filename(job["subdir"]) if job.get("subdir") else ""
    if sub:
        cat_dir = cat_dir / sub
    # The name is shortened when the whole path would pass the Windows limit.
    return cat_dir / truncate_for_dir(base, cat, sub, safe_filename(job["filename"]))


def stat_for(job):
    dest = _dest_for(job)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # _dest_for shortened an over-long name: keep job["filename"] in sync
    # (the GUI builds its own path from it).
    if dest.name != safe_filename(job["filename"]):
        job["filename"] = dest.name
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





# ------------------------------------------------------------------ v5 ----
# Proxy, naming, source-site and archive helpers.

_PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


def _is_yt_host(host):
    host = (host or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def proxy_for(url):
    """Proxy URL to use for `url`, or None. Honors proxy_scope."""
    p = str(STATE.get("proxy_url") or "").strip()
    if not p:
        return None
    try:
        u = urlparse(p)
    except ValueError:
        return None
    if u.scheme.lower() not in _PROXY_SCHEMES or not u.netloc:
        return None
    if STATE.get("proxy_scope", "youtube") == "all":
        return p
    try:
        host = urlparse(url or "").hostname
    except ValueError:
        return None
    return p if _is_yt_host(host) else None


def proxies_for(url):
    """requests-style proxies dict, or None. SOCKS needs PySocks; without
    it the proxy is skipped (and logged once) rather than failing every
    request."""
    p = proxy_for(url)
    if not p:
        return None
    if p.lower().startswith("socks"):
        try:
            import socks  # noqa: F401  (PySocks)
        except ImportError:
            if not getattr(proxies_for, "_warned", False):
                proxies_for._warned = True
                log("proxy: SOCKS proxy set but PySocks isn't installed; "
                    "generic downloads will go direct (yt-dlp is unaffected)")
            return None
    return {"http": p, "https": p}


_JUNK_STEMS = {
    "watch", "index", "download", "downloads", "video", "videos", "playlist",
    "file", "stream", "master", "manifest", "embed", "player", "view", "get",
    "status", "document", "media", "content", "default", "untitled", "null",
    "undefined", "unknown", "live", "clip",
}
_JUNK_PATTERN_RE = re.compile(
    r"(?:video|download|file|watch|playlist|media)[ _-]?\(?\d*\)?(?: \(\d+\))*")
# Raw URL basenames that name a stream rather than a title: HLS/DASH variant
# playlists (index-f2-v1-a1, chunklist_720p, media_0, seg-12-v1-a1), bare
# quality labels (720p, 1080), hash-like ids and master/manifest leftovers.
_JUNK_URL_RE = re.compile(
    r"(?:index|playlist|master|manifest|main|stream|video|audio|chunklist|media|"
    r"variant|rendition|segment|seg|frag|fragment|init|hls|dash)"
    r"(?:[ _.-]?(?:f\d+|v\d+|a\d+|s\d+|\d{1,5}p?|\d{3,4}p|[0-9a-f]{6,}))*"
    r"|(?:f\d+|v\d+|a\d+)(?:-(?:f\d+|v\d+|a\d+))+"
    r"|\d{3,4}p?"
    r"|[0-9a-f]{16,}",             # long hex ids
    re.I)
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-|\u2013\u2014:]\s*(?:YouTube|Twitch|Twitter|Vimeo|Facebook|Instagram|"
    r"TikTok|Reddit|Dailymotion|Bilibili)\s*$", re.I)
_TITLE_X_RE = re.compile(r"\s*/\s*X\s*$")


def is_junk_stem(stem):
    """True for names that say nothing: 'watch', 'index', pure numbers,
    'watch (2) (2)', 'video_1', etc."""
    s = (stem or "").strip().lower()
    if len(s) < 2:
        return True
    if s in _JUNK_STEMS or s.isdigit():
        return True
    if _JUNK_PATTERN_RE.fullmatch(s) or _JUNK_URL_RE.fullmatch(s):
        return True
    # Hyphen/underscore-joined tokens that are ALL stream tokens, e.g.
    # "index-f2-v1-a1" or "master_720p".
    toks = [t for t in re.split(r"[-_. ]+", s) if t]
    return bool(toks) and all(_JUNK_URL_RE.fullmatch(t) for t in toks)


def clean_title(title):
    """Drop notification counts like '(3) ' and ' - YouTube'-style suffixes."""
    t = re.sub(r"^\s*\(\d+\+?\)\s*", "", title or "")
    t = _TITLE_SUFFIX_RE.sub("", t)
    t = _TITLE_X_RE.sub("", t)
    return t.strip()


def site_host(url):
    try:
        h = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        h = ""
    return h[4:] if h.startswith("www.") else h


def source_site_for(*urls):
    """First usable site domain among the given URLs (page, referer, file)."""
    for u in urls:
        h = site_host(u)
        if h and h not in ("localhost",) and not re.fullmatch(r"[\d.:]+", h):
            return h
    return None


_TYPE_LABELS = {"video": "video", "audio": "audio", "music": "audio",
                "image": "image", "images": "image", "document": "doc",
                "documents": "doc", "archive": "archive", "archives": "archive",
                "program": "app", "programs": "app", "other": "file",
                "generic": "file"}


def generated_name(url, ftype=None, ext=None):
    """Fallback stem when nothing better exists: {domain}_{date}_{type}.
    Pass ext (with or without the dot) to get the full "{domain}_{date}_{type}.{ext}"
    filename; without it the bare stem is returned, as before. ftype is a
    category/type name ("Video", "video", ...); when omitted the type is
    guessed from the URL's extension."""
    host = site_host(url)
    parts = host.split(".")
    label = parts[-2] if len(parts) >= 2 else (parts[0] if parts and parts[0] else "download")
    label = re.sub(r"[^A-Za-z0-9]+", "", label) or "download"
    if not ext:
        m = re.search(r"\.([A-Za-z0-9]{2,5})(?:[?#]|$)", urlparse(url or "").path or "")
        guess = m.group(1).lower() if m else ""
    else:
        guess = ext.lstrip(".").lower()
    kind = _TYPE_LABELS.get((ftype or "").strip().lower(), "")
    if not kind:
        if guess in ("m3u8", "mpd", "mp4", "m4v", "mov", "webm", "mkv", "avi", "ts", "flv"):
            kind = "video"
        elif guess in ("mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"):
            kind = "audio"
        elif guess in ("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"):
            kind = "image"
        else:
            kind = "file"
    stem = safe_filename(f"{label}_{time.strftime('%Y-%m-%d')}_{kind}")
    if ext:
        # HLS/DASH manifests are downloaded as mp4, never as .m3u8/.mpd.
        e = "mp4" if guess in ("m3u8", "mpd") else guess
        return f"{stem}.{e}" if e else stem
    return stem


_ARCHIVE_LOCK = threading.Lock()


def archive_path():
    return HOME / "archive.txt"


def archive_ids():
    """Set of 'extractor id' lines recorded as downloaded (yt-dlp format)."""
    try:
        with _ARCHIVE_LOCK:
            return {ln.strip() for ln in
                    archive_path().read_text(encoding="utf-8").splitlines()
                    if ln.strip()}
    except OSError:
        return set()


def archive_add(extractor, vid):
    """Record a finished download so playlist pickers can flag it later."""
    if not extractor or not vid:
        return
    line = f"{str(extractor).lower()} {vid}"
    try:
        with _ARCHIVE_LOCK:
            p = archive_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                if line in {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()}:
                    return
            except OSError:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        log(f"archive write failed: {e}")


def icons_dir():
    return HOME / "icons"