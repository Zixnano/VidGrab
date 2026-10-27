"""Settings access, formatting helpers and constants shared by every GUI module (no widget imports)."""

import ctypes
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import settings as _settings
from settings import _dest_for


BACKEND_BASE = "http://127.0.0.1:5757"
# Always read as `gui_common.HOME`. Never do
# `from gui_common import HOME` -- the value can be rebound
# by launch_gui() during startup (portable mode) and a copied
# binding goes stale.
HOME = _settings.HOME


CONFIG_PATH = HOME / "settings.json"


def _bring_to_front(widget):
    """Raise a window above other apps WITHOUT pinning it there.

    The old code set WindowStaysOnTopHint on the Add/Playlist dialogs. A
    topmost window ignores its owner being minimized, so it stayed on screen
    after the main window went away and could not be dismissed except by
    answering it. Here the window is flipped to topmost and straight back
    (the standard Windows way to come to the front); afterwards it is an
    ordinary owned window that minimizes with the main window."""
    widget.raise_()
    widget.activateWindow()
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        flags = 0x0001 | 0x0002  # SWP_NOSIZE | SWP_NOMOVE
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
    except Exception:
        pass


def _parse_speed_kbps(text):
    m = re.match(r"\s*([\d.]+)\s*(B|KB|MB|GB)/s", text or "", re.I)
    if not m:
        return 0.0
    mult = {"b": 1 / 1024, "kb": 1, "mb": 1024, "gb": 1024 * 1024}[m.group(2).lower()]
    try:
        return float(m.group(1)) * mult
    except ValueError:
        return 0.0


def _eta_seconds(j):
    """Seconds left for an active download, or None when unknown."""
    if j.get("status") != "downloading":
        return None
    total, done = j.get("size_total") or 0, j.get("size_done") or 0
    kbps = _parse_speed_kbps(j.get("speed"))
    if not total or done >= total or kbps <= 0:
        return None
    return (total - done) / (kbps * 1024)


def _fmt_duration(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def _retry_text(j):
    """'retrying in 45s (#2)' while an auto-retry delay is pending."""
    ra = j.get("retry_after") or 0
    if j.get("status") == "queued" and ra > time.time():
        return f"retrying in {_fmt_duration(ra - time.time())} (#{j.get('retry_count') or 1})"
    return ""


_settings_cache = {"mtime": 0, "data": {}}


def load_settings():
    """settings.json, re-parsed only when the file's mtime changes. Returns
    the cached dict itself: callers must treat it as read-only."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime_ns  # ns: quick Windows saves still invalidate
    except OSError:
        return {}
    if mtime == _settings_cache["mtime"]:
        return _settings_cache["data"]
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        data = {}
    # data first, mtime second: a reader on another thread that sees the
    # new data with the old mtime just re-reads once; the reverse order
    # could hand it stale data under a fresh mtime.
    _settings_cache["data"] = data
    _settings_cache["mtime"] = mtime
    return data


def load_token():
    return load_settings().get("api_token", "")


def _path_for(job):
    # Reconciled path reported by the engine (yt-dlp post_hooks): wins when
    # the file is really there, so Open Folder works for renamed outputs.
    _d = job.get("dest")
    if _d:
        try:
            if Path(_d).exists():
                return Path(_d)
        except (OSError, ValueError):
            pass
    # settings._dest_for is the one canonical path builder.
    return _dest_for(job)


def fmt_ts(ts):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    if dt.year == now.year:
        return dt.strftime("%b %d, %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_bytes(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
