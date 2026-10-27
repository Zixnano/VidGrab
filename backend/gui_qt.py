"""
gui_qt.py — modern PySide6 frontend for Video Grabber.

Talks to the local Flask backend on 127.0.0.1:5757. Reads the pairing token
from settings.json. Imported and launched by server.py's main().
"""

import base64
from collections import deque
import html as _html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, Qt, QTimer, QThread, Signal, QMimeData, QUrl,
    QItemSelectionModel, QItemSelection, QPropertyAnimation, QEasingCurve, QAbstractAnimation,
    QElapsedTimer, QByteArray, QRect, QPoint, QSize,
)
from PySide6.QtGui import QAction, QColor, QDrag, QFont, QFontDatabase, QIcon, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QTextEdit, QTextBrowser
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame,
    QLabel, QPushButton, QToolButton, QLineEdit, QTableView, QHeaderView,
    QMenu, QMessageBox, QDialog, QDialogButtonBox, QFormLayout, QComboBox,
    QColorDialog, QTableWidget, QTableWidgetItem,
    QStatusBar, QSizePolicy, QAbstractItemView, QStyledItemDelegate,
    QInputDialog, QFileDialog, QCheckBox, QSystemTrayIcon, QStyle,
    QSpinBox, QSlider, QListWidget, QListWidgetItem, QTabWidget, QProgressBar,
    QStackedWidget, QScrollArea, QGroupBox, QBoxLayout, QGridLayout, QSplitter,
    QRubberBand,
)

BACKEND_BASE = "http://127.0.0.1:5757"
HOME = Path.home() / "Downloads" / "VideoGrabber"

# --- Supervisor Architecture (contract v3): sentinel markers + state ----
# The supervisor (launcher.py) spawns this app with VG_LAUNCH_ID set.
# On first show we write app_alive.tmp; once our own /health answers
# with the app identity we write startup_ok.tmp. Without VG_LAUNCH_ID
# (dev runs, old launcher) all of this is a no-op.
_VG_LAUNCH_ID = os.environ.get("VG_LAUNCH_ID", "")


try:
    import vg_state as _vg_state_mod
except Exception:
    # An install caught mid-transition (app\ swapped before
    # vg_state.py landed): the hooks degrade to no-ops rather than
    # taking the window down with an ImportError.
    _vg_state_mod = None


def _vg_state_dir():
    if _vg_state_mod is not None:
        return _vg_state_mod.state_dir()
    base = os.environ.get("VG_STATE_DIR") or os.environ.get("LOCALAPPDATA")
    if base and os.environ.get("VG_STATE_DIR"):
        return Path(base)
    if base:
        return Path(base) / "VideoGrabber"
    return Path.home() / "AppData" / "Local" / "VideoGrabber"


def _vg_boot_flag():
    if _vg_state_mod is not None:
        return _vg_state_mod.boot_flag()
    return _vg_state_dir() / "boot_recovery.flag"


def _vg_write_marker(name):
    if not _VG_LAUNCH_ID:
        return
    try:
        d = _vg_state_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(
            {"launch_id": _VG_LAUNCH_ID, "timestamp": int(time.time())}),
            encoding="utf-8")
    except Exception:
        pass


def _vg_update_state(fn):
    """Read-modify-write of recovery_state.json via the ONE shared
    implementation (vg_state.py, fix 10)."""
    if _vg_state_mod is None:
        return
    try:
        _vg_state_mod.mutate_state(fn)
    except Exception:
        pass


CONFIG_PATH = HOME / "settings.json"

# Palette-derived (single source of truth: palette.py).
from palette import resolve_palette
from settings import CATEGORIES, DISPLAY_VERSION, detect_type  # single source of truth: settings.py
from settings import _dest_for
from gui_detail import IMAGE_EXT, VIDEO_EXT, DetailPane, ThumbLoader
# Batch 3 (moved): the procedural completion-sound maker (sound_generator.py,
# same folder) opens from Settings > Sounds through open_sound_options. It no
# longer has a Tools menu entry.
from sound_generator import open_sound_options

from PySide6.QtSvg import QSvgRenderer  # pure-SVG icons, no icon-font dependency

# --- Pure SVG icon engine: Material-style paths, rendered by QSvgRenderer ---
_SVG_PATHS = {
    "add": '<path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>',
    "batch": '<path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z"/>',
    "playlist": '<path d="M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zM17 6v8.18c-.31-.11-.65-.18-1-.18-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3V8h3V6h-5z"/>',
    "play": '<path d="M8 5v14l11-7z"/>',
    "pause": '<path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>',
    "stop": '<path d="M6 6h12v12H6z"/>',
    "delete": '<path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>',
    "stream": '<path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/>',
    "queue_play": '<path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7zm10 7l4-4-4-4v8z"/>',
    "queue_pause": '<path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7zm10 5v4l-4-2 4-2z"/>',
    "settings": '<path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/>',
    "all": '<path d="M4 8h4V4H4v4zm6 12h4v-4h-4v4zm-6 0h4v-4H4v4zm0-6h4v-4H4v4zm6 0h4v-4h-4v4zm6-10v4h4V4h-4zm-6 4h4V4h-4v4zm6 6h4v-4h-4v4zm0 6h4v-4h-4v4z"/>',
    "active": '<path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>',
    "finished": '<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>',
    "unfinished": '<path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/>',
    "failed": '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/>',
    "video": '<path d="M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"/>',
    "music": '<path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/>',
    "images": '<path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/>',
    "compressed": '<path d="M20 6h-8l-2-2H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 10h-2v-2h2v2zm0-4h-2v-2h2v2zm0-4h-2V6h2v2z"/>',
    "documents": '<path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/>',
    "programs": '<path d="M20 18c1.1 0 1.99-.9 1.99-2L22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/>',
    "fonts": '<path d="M9 4v3h5v12h3V7h5V4H9zm-6 8h3v7h3v-7h3V9H3v3z"/>',
    "other": '<path d="M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/>',
}

_ICON_PX = 64  # rendered large, then scaled down by Qt: stays sharp on HiDPI


def _qicon(name, color):
    """Pure-SVG icon for `name` tinted `color` (hex string), or None when the
    name is unknown. Callers show the plain label (or its first letter)."""
    if not name or name not in _SVG_PATHS:
        return None
    key = (name, color)
    if key in _SVG_ICON_CACHE:
        return _SVG_ICON_CACHE[key]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="{color}">{_SVG_PATHS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(_ICON_PX, _ICON_PX)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    ic = QIcon(pm)
    _SVG_ICON_CACHE[key] = ic
    return ic

_PAL = resolve_palette()
ACCENT = _PAL["accent"]
BG = _PAL["bg_base"]
PANEL = _PAL["bg_panel"]
BORDER = _PAL["border"]
TEXT = _PAL["text"]
MUTED = _PAL["text_muted"]
DANGER = _PAL["error"]
WARN = _PAL["warning"]
SUCCESS = _PAL["success"]
TRACK = _PAL["bg_elevated"]   # progress-bar track

_ICON_CACHE = {}
_SVG_ICON_CACHE = {}


_TRAY_ICONS = {}

SEL = _PAL["bg_sel"]      # selected row / active nav background
SOFT = _PAL["text_soft"]  # secondary nav text


def _sync_palette(settings=None):
    """Re-read the theme tokens into this module's color globals. Painted
    pieces (progress delegate, row colors, tray icon) read these at paint
    time, so calling this and repainting is enough for a live theme change."""
    global _PAL, ACCENT, BG, PANEL, BORDER, TEXT, MUTED, DANGER, WARN, SUCCESS, TRACK, SEL, SOFT
    _PAL = resolve_palette(settings)
    ACCENT, BG, PANEL, BORDER = _PAL["accent"], _PAL["bg_base"], _PAL["bg_panel"], _PAL["border"]
    TEXT, MUTED = _PAL["text"], _PAL["text_muted"]
    DANGER, WARN, SUCCESS = _PAL["error"], _PAL["warning"], _PAL["success"]
    TRACK, SEL, SOFT = _PAL["bg_elevated"], _PAL["bg_sel"], _PAL["text_soft"]
    _ICON_CACHE.clear()
    _SVG_ICON_CACHE.clear()
    _TRAY_ICONS.clear()
    _STATUS_COLORS.update({"downloading": SUCCESS, "done": SUCCESS, "paused": WARN,
                           "stopped": DANGER, "error": DANGER, "queued": MUTED,
                           "skipped": _PAL["text_dim"]})


def _title_bar_colors():
    """(caption, text) colors for native title bars, from the live theme, so
    every window matches the app instead of only being light or dark."""
    try:
        return _PAL["bg_base"], _PAL["text"]
    except Exception:
        return None, None


def _title_bar_dark():
    """True when the current theme is dark, so the native title bar should be."""
    try:
        from palette import luminance
        return luminance(_PAL["bg_base"]) < 0.4
    except Exception:
        return True


# Toolbar keys are the toolbar labels (see MainWindow._build_ui); sidebar keys
# are nav names. Values are keys of _SVG_PATHS.
_TB_ICONS = {
    "Add URL": "add", "Batch": "batch", "Playlist": "playlist",
    "Resume": "play", "Pause": "pause", "Stop": "stop",
    "Delete": "delete", "Queue": "stream",
    "Pause Queue": "queue_pause", "Start Queue": "queue_play",
    "Settings": "settings",
}
_SB_ICONS = {
    "All": "all", "Active": "active", "Queue": "stream",
    "Finished": "finished", "Unfinished": "unfinished", "Failed": "failed",
    "Video": "video", "Music": "music", "Images": "images", "Compressed": "compressed",
    "Documents": "documents", "Programs": "programs", "Fonts": "fonts", "Other": "other",
}


def _toolbar_icon_color():
    """Icon tint for the toolbar: the secondary text color, or the frame's
    text color when the colored window frame is on."""
    try:
        if load_settings().get("frame_cream"):
            from palette import contrast, luminance
            fb = _PAL["frame"]
            return BG if contrast(fb, BG) >= 4.5 else ("#000000" if luminance(fb) > 0.4 else "#ffffff")
    except Exception:
        pass
    return SOFT


def reapply_theme():
    """Rebuild the app stylesheet from saved settings and repaint the main
    window. Called after Settings saves."""
    from gui_style import build_qss
    st = load_settings()
    _sync_palette(st)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(build_qss(st))
        f = getattr(app, "_vg_dark_filter", None)
        if f is not None:
            f.refresh()              # theme may have flipped light <-> dark
    w = globals().get("_window")
    if w is not None:
        w.table.viewport().update()
        w.table.horizontalHeader().viewport().update()
        w.select_category(w.current_filter)
        w.apply_shell()
        w._apply_blanc()


def _make_tray_icon(active: bool):
    """Green dot while downloading, grey when idle — like IDM's tray."""
    key = "active" if active else "idle"
    if key in _TRAY_ICONS:
        return _TRAY_ICONS[key]
    color = QColor(ACCENT if active else _PAL["text_dim"])
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 24, 24)
    p.end()
    icon = QIcon(pm)
    _TRAY_ICONS[key] = icon
    return icon


def _status_icon(status):
    key = status or "queued"
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    colors = {
        "downloading": SUCCESS, "done": SUCCESS, "paused": WARN,
        "stopped": DANGER, "error": DANGER, "queued": MUTED,
        "skipped": _PAL["text_dim"],
    }
    color = QColor(colors.get(key, MUTED))
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    _ICON_CACHE[key] = pm
    return pm


_STATUS_COLORS = {   # refreshed by _sync_palette()
    "downloading": SUCCESS, "done": SUCCESS, "paused": WARN,
    "stopped": DANGER, "error": DANGER, "queued": MUTED, "skipped": MUTED,
}


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


def _app_icon_path():
    """icon.ico next to this module (CI copies it into app\\), else the
    usual places next to the source. None when no icon is shipped."""
    for base in (Path(__file__).resolve().parent, HOME):
        for rel in ("icon.ico", "icons/icon.ico", "assets/icon.ico"):
            cand = base / rel
            try:
                if cand.is_file():
                    return cand
            except OSError:
                continue
    return None


def _apply_app_icon(app, window=None):
    """Give the Qt app and window the Video Grabber icon (title bar, taskbar,
    Alt+Tab). Also sets an explicit Windows AppUserModelID so the taskbar
    groups under this icon instead of python's. Returns the QIcon or None."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoGrabber.App")
        except Exception:
            pass
    path = _app_icon_path()
    if path is None:
        return None
    ic = QIcon(str(path))
    if ic.isNull():
        return None
    app.setWindowIcon(ic)
    if window is not None:
        window.setWindowIcon(ic)
    return ic


def _icons_dir():
    try:
        from settings import icons_dir
        return icons_dir()
    except Exception:
        return HOME / "icons"


_SITE_ICONS = {}   # site -> (QPixmap, checked_at, has_favicon)


def _site_icon(site):
    """Favicon for a site (letter tile until the extension has sent one)."""
    if not site:
        return None
    now = time.monotonic()
    hit = _SITE_ICONS.get(site)
    if hit and (hit[2] or now - hit[1] < 10):
        return hit[0]
    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    fav = QPixmap()
    has_fav = False
    try:
        path = _icons_dir() / (site + ".png")
        has_fav = path.is_file() and fav.load(str(path)) and not fav.isNull()
    except Exception:
        has_fav = False
    if has_fav:
        p.drawPixmap(1, 1, fav.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    else:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(TRACK))
        p.drawRoundedRect(1, 1, 16, 16, 4, 4)
        f = QFont("Segoe UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(ACCENT))
        p.drawText(1, 1, 16, 16, Qt.AlignCenter, site[:1].upper())
    p.end()
    _SITE_ICONS[site] = (pm, now, bool(has_fav))
    if len(_SITE_ICONS) > 300:
        _SITE_ICONS.clear()
    return pm


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


def _is_active(j):
    return j.get("status") == "downloading" or j.get("phase") == "converting"


def _in_queue_view(j):
    """v5.5 Queue view: what is running plus what is waiting to run."""
    return _is_active(j) or j.get("status") in ("queued", "held")


def _sidebar_counts(items, hide_hours=0):
    """Counts for the sidebar. All and Unfinished leave out failed downloads
    older than `hide_hours` (the hide_old_errors_hours setting), exactly like
    the table does in those two views (see MainWindow.apply_filter). The
    Failed view and the category views still show every failure, so their
    counts stay complete."""
    try:
        hrs = float(hide_hours or 0)
    except (TypeError, ValueError):
        hrs = 0
    shown = items
    if hrs > 0:
        cutoff = time.time() - hrs * 3600
        shown = [j for j in items
                 if not (j.get("status") == "error"
                         and (j.get("failed_ts") or j.get("created_ts") or 0) < cutoff)]
    c = {
        "All": len(shown),
        "Active": sum(1 for j in items if _is_active(j)),
        "Queue": sum(1 for j in items if _in_queue_view(j)),
        "Finished": sum(1 for j in items if j.get("status") == "done"),
        "Unfinished": sum(1 for j in shown if j.get("status") != "done"),
        "Failed": sum(1 for j in items if j.get("status") == "error"),
    }
    for cat in CATEGORIES:
        c[cat] = sum(1 for j in items if j.get("category") == cat)
    return c


def _gui_state_path():
    return HOME / "gui_state.json"


def _read_gui_state():
    try:
        return json.loads(_gui_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_gui_state(patch):
    try:
        st = _read_gui_state()
        st.update(patch)
        _gui_state_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = _gui_state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, _gui_state_path())
    except Exception:
        pass


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


import ctypes


def _win_select_in_explorer(path):
    """Highlight a file in Windows Explorer via the shell API — more
    reliable than `explorer /select,`. Falls back to it on any error."""
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    try:
        pidl = ctypes.c_void_p()
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.SHParseDisplayName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        hr = shell32.SHParseDisplayName(str(path), None,
                                        ctypes.byref(pidl), 0, None)
        if hr == 0 and pidl:
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            ole32.CoTaskMemFree(pidl)
    except Exception:
        subprocess.Popen(f'explorer /select,"{path}"')
    finally:
        ole32.CoUninitialize()


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


class ProbeError(Exception):
    """/probe failed. `code` is the server's machine-readable error code
    ("auth_required", "playlist_unavailable", ...) or "" when unknown."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


class ApiClient:
    def __init__(self):
        self.base = BACKEND_BASE
        self.session = requests.Session()
        self.token = load_token()

    def _headers(self):
        return {"X-API-Token": self.token} if self.token else {}

    def _req(self, method, path, **kw):
        headers = kw.pop("headers", {})
        headers.update(self._headers())
        timeout = kw.pop("timeout", 5)  # slow calls (batch, probe) pass their own
        r = self.session.request(method, self.base + path, headers=headers,
                                 timeout=timeout, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def reload_token(self):
        self.token = load_token()

    def jobs(self):
        data = self._req("GET", "/jobs") or {}
        return list(data.values())

    def add(self, url, filename=None, category=None, description=None,
            format_id=None, target_format=None, resolution=None, multi=False,
            download_playlist=False, referer=None, cookie=None, user_agent=None,
            page_url=None):
        body = {"url": url}
        if page_url:
            body["page_url"] = page_url
        if filename:
            body["filename"] = filename
        if referer:
            body["referer"] = referer
        if cookie:
            body["cookie"] = cookie
        if user_agent:
            body["user_agent"] = user_agent
        if category:
            body["category"] = category
        if description:
            body["description"] = description
        if format_id:
            body["format_id"] = format_id
        if target_format:
            body["target_format"] = target_format
        if resolution:
            body["resolution"] = resolution
        if multi:
            body["multi"] = True
        if download_playlist:
            body["download_playlist"] = True
        return self._req("POST", "/download", json=body)

    def pause(self, jid):
        return self._req("POST", f"/pause/{jid}")

    def resume(self, jid):
        return self._req("POST", f"/resume/{jid}")

    def stop(self, jid):
        return self._req("POST", f"/stop/{jid}")

    def delete(self, jid):
        return self._req("POST", f"/delete/{jid}")

    def set_category(self, jid, category):
        return self._req("POST", f"/category/{jid}", json={"category": category})

    def convert(self, jid, fmt):
        return self._req("POST", f"/convert/{jid}", json={"format": fmt})

    @staticmethod
    def _error_text(e):
        """Backend error message from a failed response, else the exception text."""
        try:
            return e.response.json().get("error") or str(e)
        except Exception:
            return str(e)

    def delete_file(self, jid):
        """Remove the job and its file (backend refuses paths outside the
        VideoGrabber / download folders)."""
        try:
            return self._req("POST", f"/delete-file/{jid}", timeout=30)
        except requests.HTTPError as e:
            raise RuntimeError(self._error_text(e)) from None

    def toolkit(self, kind, body):
        """Run /toolkit/<compress|trim|gif>. Encodes are slow: long timeout."""
        try:
            return self._req("POST", f"/toolkit/{kind}", json=body, timeout=7500)
        except requests.HTTPError as e:
            raise RuntimeError(self._error_text(e)) from None

    def probe_head(self, url):
        return self._req("POST", "/probe-head", json={"url": url})

    def probe_formats(self, url):
        return self._req("POST", "/probe-formats", json={"url": url})

    def probe_playlist(self, url, start=0, limit=200, referer=None,
                       cookie=None, user_agent=None):
        """POST /probe. Raises ProbeError carrying the server's error text
        and code (a plain raise_for_status would lose both)."""
        body = {"url": url, "start": start, "limit": limit}
        if referer:
            body["referer"] = referer
        if cookie:
            body["cookie"] = cookie
        if user_agent:
            body["user_agent"] = user_agent
        r = self.session.request("POST", self.base + "/probe",
                                 headers=self._headers(), json=body,
                                 timeout=120)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            raise ProbeError(data.get("error") or f"HTTP {r.status_code}",
                             data.get("code") or "")
        return data

    def batch(self, items, referer=None, cookie=None, user_agent=None):
        body = {"items": items}
        if referer:
            body["referer"] = referer
        if cookie:
            body["cookie"] = cookie
        if user_agent:
            body["user_agent"] = user_agent
        return self._req("POST", "/batch", json=body, timeout=60)

    def redownload(self, jid):
        return self._req("POST", f"/redownload/{jid}")

    def logs(self):
        data = self._req("GET", "/logs") or {}
        return data.get("logs", [])

    def version(self):
        data = self._req("GET", "/version") or {}
        return data.get("version", "")

    def changelog(self):
        return self._req("GET", "/changelog", timeout=12) or {}

    def disk_space(self):
        return self._req("GET", "/diskspace") or {}

    def reorder(self, jid, action):
        return self._req("POST", f"/reorder/{jid}", json={"action": action})

    def dup_check(self, url):
        return (self._req("POST", "/dup-check", json={"url": url}) or {}).get("duplicate")

    def queue_status(self):
        return self._req("GET", "/queue-status") or {}

    def update_status(self):
        return self._req("GET", "/update-status", timeout=20) or {}

    def ytdlp_update(self):
        return self._req("POST", "/ytdlp-update", timeout=10)

    def ytdlp_rollback(self):
        return self._req("POST", "/ytdlp-rollback")

    def clear_failed(self, older_than_hours=0):
        return self._req("POST", "/clear-failed",
                         json={"older_than_hours": older_than_hours})

    def save_setting(self, key, value):
        # Errors propagate: callers (toggle_queue, SettingsDialog._save)
        # show them; swallowing here made those handlers dead code.
        self._req("POST", "/settings", json={key: value})

    def set_queue_running(self, running):
        # queue_running is ignored by /settings on purpose; this route is
        # the only writer.
        self._req("POST", "/queue-running", json={"running": bool(running)})


# Which table columns each job field feeds (see DownloadModel.data). Used by
# update_items() so a progress tick repaints only the columns that changed.
_FIELD_COLS = {
    "filename": (0,), "status": (0, 2, 4, 6, 7), "size_total": (1, 2, 7),
    "size_done": (2, 7), "phase": (2, 4), "conversion_progress": (2,),
    "converting_fmt": (4,), "speed": (3, 7), "category": (5,),
    "completed_ts": (6,), "created_ts": (6,),
    "source_site": (8,), "retry_after": (4,), "retry_count": (4,),
    "error": (4,),
}


def _changed_cols(a, b):
    """(first, last) column span touched by differences between two job
    dicts, or None when no displayed field differs."""
    cols = set()
    for k, mapped in _FIELD_COLS.items():
        if a.get(k) != b.get(k):
            cols.update(mapped)
    return (min(cols), max(cols)) if cols else None


class DownloadModel(QAbstractTableModel):
    HEADERS = ["Name", "Size", "Progress", "Speed", "Status", "Category", "Completed", "ETA", "Source"]

    def __init__(self, parent_window=None):
        super().__init__()
        self.items = []
        self.parent_window = parent_window

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def update_items(self, items):
        """Incremental refresh. Same rows in the same order -> swap the data
        and emit dataChanged for only the rows whose values changed (keeps
        scroll position, selection and hover; no full repaint). Anything
        else (filter, sort, add/remove) falls back to a full reset."""
        old = self.items
        if (len(old) != len(items)
                or any(a.get("id") != b.get("id") for a, b in zip(old, items))):
            self.set_items(items)
            return
        self.items = items
        last_col = self.columnCount() - 1
        for r, (a, b) in enumerate(zip(old, items)):
            if a is b or a == b:
                continue
            span = _changed_cols(a, b)
            if span:
                self.dataChanged.emit(self.index(r, span[0]), self.index(r, span[1]))
        if items:
            self.headerDataChanged.emit(Qt.Horizontal, 0, last_col)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        j = self.items[index.row()]
        col = index.column()
        if role == Qt.DecorationRole and col == 0:
            return _status_icon(j.get("status"))
        if role == Qt.DecorationRole and col == 8:
            return _site_icon(j.get("source_site"))
        if role == Qt.ToolTipRole:
            if col == 0:
                return "\n".join(x for x in (j.get("filename"), j.get("source_site"),
                                             j.get("url")) if x)
            if col == 4 and j.get("error"):
                return str(j["error"])[:600]
            return None
        if role == Qt.BackgroundRole:
            pw = self.parent_window
            fade = getattr(pw, "_recently_done", {}).get(j.get("id"), 0) if pw else 0
            if fade > 0:
                flash = QColor(SEL)
                base = QColor(PANEL)
                t = max(0.0, min(1.0, float(fade)))
                return QColor(
                    int(base.red() + (flash.red() - base.red()) * t),
                    int(base.green() + (flash.green() - base.green()) * t),
                    int(base.blue() + (flash.blue() - base.blue()) * t),
                )
        if role == Qt.DisplayRole:
            pct = "—"
            if j.get("phase") == "converting":
                pct = f"conv {j.get('conversion_progress', 0)}%"
            elif j.get("size_total"):
                pct = f"{(j.get('size_done', 0) / j['size_total'] * 100):.0f}%"
            elif j["status"] == "done":
                pct = "100%"
            status = (f"converting → {j.get('converting_fmt', '')}" if j.get("phase") == "converting" else j["status"])
            retry = _retry_text(j)
            if retry:
                status = retry
            if col == 7:
                eta = _eta_seconds(j)
                return _fmt_duration(eta) if eta is not None else ""
            if col == 8:
                return j.get("source_site") or ""
            if col == 6:
                # Pre-column jobs: fall back to created_ts when done so the
                # column is still useful for sorting.
                ts = j.get("completed_ts")
                if not ts and j.get("status") == "done":
                    ts = j.get("created_ts")
                return fmt_ts(ts)
            return [
                j["filename"],
                fmt_bytes(j.get("size_total")),
                pct,
                j.get("speed") or "",
                status,
                j["category"],
            ][col]
        if role == Qt.ForegroundRole:
            if col == 4:
                if _retry_text(j):
                    return QColor(WARN)
                if j.get("phase") == "converting":
                    return QColor(WARN)
                s = j["status"]
                if s == "done":
                    return QColor(ACCENT)
                if s in ("error", "stopped"):
                    return QColor(DANGER)
                if s == "paused":
                    return QColor(WARN)
                if s == "skipped":
                    return QColor(_PAL["text_dim"])
            if col in (1, 3, 6, 7, 8):
                return QColor(SOFT)
            if col == 5:
                return QColor(SOFT)
            return QColor(TEXT)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            label = self.HEADERS[section]
            pw = self.parent_window
            if pw is not None and getattr(pw, "_sort_column", None) == section:
                label += " ▲" if pw._sort_ascending else " ▼"
            return label
        return None

    def flags(self, index):
        base = super().flags(index)
        # Only finished jobs can be dragged out (to Discord/Explorer). Qt
        # skips non-draggable rows in a mixed selection, so dragging a mix
        # of done and pending rows carries just the done ones.
        if (index.isValid() and index.row() < len(self.items)
                and self.items[index.row()].get("status") == "done"):
            return base | Qt.ItemIsDragEnabled
        return base

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes if i.isValid()})
        urls = []
        for r in rows:
            j = self.items[r]
            if j["status"] != "done":
                continue
            path = _path_for(j)
            if path.exists():
                urls.append(QUrl.fromLocalFile(str(path)))
        if not urls:
            return QMimeData()  # empty drag payload, never None
        mime = QMimeData()
        mime.setUrls(urls)
        return mime

    def mimeDataForJobs(self, ids):
        """File-drag payload for the given job ids (in the order given). The
        table calls this from _start_file_drag. Ids, not rows, because the
        model refreshes every second and rows can move before a drag starts.
        Only finished jobs whose file still exists are carried."""
        by_id = {j.get("id"): j for j in self.items}
        urls, seen = [], set()
        for jid in ids:
            j = by_id.get(jid)
            if j is None or j.get("status") != "done":
                continue
            try:
                path = _path_for(j)
                if not path.exists() or str(path) in seen:
                    continue
            except (OSError, ValueError, KeyError):
                continue
            seen.add(str(path))
            urls.append(QUrl.fromLocalFile(str(path)))
        mime = QMimeData()
        if urls:
            mime.setUrls(urls)
        return mime


class _BottomPane(QWidget):
    """Bottom half of the table / detail splitter. The detail pane inside it
    (thumbnail, tabs, log) can ask for a lot of height. Reporting that as this
    widget's minimum let it raise the whole window's minimum height every time
    a row got selected, which squeezed the status bar out of view. The splitter
    decides the real height (the divider is draggable and remembered), so the
    minimum reported here stays small."""
    MIN_H = 96

    def minimumSizeHint(self):
        return QSize(0, self.MIN_H)


class DownloadTable(QTableView):
    """The downloads table, with Windows-Explorer style mouse behaviour.

    A plain press never changes the selection on its own, so a box drag can
    start on any unselected row or empty space, even in a table that fills
    the whole viewport:
      * Press + drag draws a rubber-band box and highlights the rows it
        touches as you go. A plain click that never becomes a drag leaves the
        old selection alone until release. Ctrl/Shift + drag ADDS the box rows.
      * A file drag (to Explorer / Discord) starts from a press on an
        ALREADY-SELECTED, finished row (any column) and carries the whole
        selection, so you can box-select and then pick the rows up. Hold Alt
        on a selected row to draw a box from it instead.
      * A plain click collapses the selection to the clicked row (or clears it
        on empty space) on release. Ctrl+Click toggles and Shift+Click extends,
        handled by Qt as before.
    Qt's own drag is switched off (startDrag below does nothing): it started a
    file drag on any finished row, which is what made the box unreachable.
    The dragged row is found by job id at press time, because the model
    refreshes every second and rows can move before the drag starts."""
    DRAG_THRESHOLD = 5   # pixels
    EDGE_SCROLL_MS = 50  # box drag: auto-scroll tick while the cursor is outside the table
    boxFinished = Signal()   # a box drag ended; the host refreshes the details panel

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos = None      # left-press position (viewport coords); None = no press tracked
        self._press_kind = None     # "file" (may drag files out) or "band" (box select)
        self._press_id = None       # job id under a "file" press
        self._press_mods = Qt.NoModifier   # Ctrl/Shift held at the press
        self._press_event = None    # copy of a plain press, replayed on release if it was a click
        self._base_rows = set()     # rows selected when the press happened
        self.live_selecting = False # True while a box drag is changing the selection
        self._live_rows = None      # rows last applied by the box
        self._rb_origin = None      # rubber-band start, in content (scroll-independent) coords
        self._rb_last = None        # last mouse position in viewport coords
        self._rubber = None         # QRubberBand, created lazily
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(self.EDGE_SCROLL_MS)
        self._edge_timer.timeout.connect(self._edge_scroll)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDragEnabled(True)

    def startDrag(self, supportedActions):
        """Qt's built-in drag is disabled; _start_file_drag runs the file drag."""
        return

    # ---- rubber band -------------------------------------------------
    def _scroll_offset(self):
        # Pixel offsets of the content (the scroll bars count rows while the
        # table scrolls per item, so they cannot be used here).
        return self.horizontalHeader().offset(), self.verticalHeader().offset()

    def _band_begin(self, pos):
        ox, oy = self._scroll_offset()
        self._rb_origin = (pos.x() + ox, pos.y() + oy)
        self._rb_last = pos

    def _band_update(self):
        if self._rb_origin is None or self._rb_last is None:
            return
        ox, oy = self._scroll_offset()
        a = self._rb_origin
        start = QPoint(a[0] - ox, a[1] - oy)
        rect = QRect(start, self._rb_last).normalized()
        if rect.width() < self.DRAG_THRESHOLD and rect.height() < self.DRAG_THRESHOLD:
            return   # still just a click
        if self._rubber is None:
            self._rubber = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._rubber.setGeometry(rect.intersected(self.viewport().rect()))
        self._rubber.show()

    def _band_active(self):
        return self._rubber is not None and self._rubber.isVisible()

    def _band_end(self):
        self._rb_origin = self._rb_last = None
        if self._rubber is not None:
            self._rubber.hide()

    def _band_rows(self):
        """Rows the box touches, from its content-space rectangle."""
        if self._rb_origin is None or self._rb_last is None:
            return set()
        ox, oy = self._scroll_offset()
        a = self._rb_origin
        rect = QRect(QPoint(a[0], a[1]),
                     QPoint(self._rb_last.x() + ox, self._rb_last.y() + oy)).normalized()
        rows = set()
        for r in range(self.model().rowCount()):
            if self.isRowHidden(r):
                continue
            top = self.rowViewportPosition(r) + oy
            if top <= rect.bottom() and top + self.rowHeight(r) > rect.top():
                rows.add(r)
        return rows

    def _edge_scroll(self):
        """While a box drag is held outside the table, keep scrolling toward it."""
        if self._rb_last is None or self._press_kind != "band":
            self._edge_timer.stop()
            return
        sb = self.verticalScrollBar()
        y = self._rb_last.y()
        if y < 0:
            sb.setValue(sb.value() - 1)
        elif y > self.viewport().height():
            sb.setValue(sb.value() + 1)

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._band_update()   # keep the box anchored while autoscrolling

    # ---- selection helpers -------------------------------------------
    def _selected_row_set(self):
        sm = self.selectionModel()
        return {i.row() for i in sm.selectedIndexes()} if sm is not None else set()

    def _apply_rows(self, rows):
        """Make exactly `rows` the selection (empty clears it)."""
        sm, model = self.selectionModel(), self.model()
        if sm is None or model is None:
            return
        sel, last = QItemSelection(), model.columnCount() - 1
        for r in sorted(rows):
            sel.select(model.index(r, 0), model.index(r, last))
        sm.select(sel, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        if rows:   # the first row is the Shift+Click anchor afterwards
            sm.setCurrentIndex(model.index(min(rows), 0), QItemSelectionModel.NoUpdate)

    def _reset_press(self):
        self._press_pos = None
        self._press_kind = None
        self._press_id = None
        self._press_event = None
        self._press_mods = Qt.NoModifier
        self._base_rows = set()
        self._live_rows = None
        self.live_selecting = False
        self._edge_timer.stop()
        self._band_end()

    def _live_box_select(self):
        """Highlight the rows under the box while it is dragged."""
        rows = self._band_rows()
        target = (self._base_rows | rows) if self._press_mods else rows
        if target != self._live_rows:
            self._live_rows = target
            self._apply_rows(target)

    # ---- mouse -------------------------------------------------------
    def mousePressEvent(self, e):
        self._reset_press()
        if e.button() != Qt.LeftButton:
            super().mousePressEvent(e)       # right-click etc.: standard handling (context menu)
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        mods = e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
        items = getattr(self.model(), "items", [])
        self._press_pos = pos
        self._press_mods = mods
        self._press_kind = "band"
        self._base_rows = self._selected_row_set()
        self._band_begin(pos)
        self._edge_timer.start()
        if mods:
            # Ctrl/Shift+Click: Qt's own toggle / range selection, unchanged.
            # If this turns into a drag, the box rows are added on release.
            super().mousePressEvent(e)
            return
        # Plain press: leave the selection alone (a multi-row selection must
        # survive until we know if this is a file drag, a box or a click).
        self.setFocus(Qt.MouseFocusReason)
        if idx.isValid():
            self.selectionModel().setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
        if idx.isValid() and idx.row() < len(items):
            j = items[idx.row()]
            if (j.get("status") == "done"        # only finished files can be dragged out
                    and self.selectionModel().isRowSelected(idx.row(), idx.parent())
                    and not (e.modifiers() & Qt.AltModifier)):   # Alt: box from a selected row
                self._press_kind = "file"
                self._press_id = j.get("id")
        self._press_event = QMouseEvent(e)   # replayed on release if this was only a click
        e.accept()

    def mouseMoveEvent(self, e):
        if self._press_pos is None or not (e.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(e)        # hover etc.
            return
        pos = e.position().toPoint()
        if self._press_kind == "file":
            if (pos - self._press_pos).manhattanLength() >= self.DRAG_THRESHOLD:
                pid = self._press_id
                self._reset_press()
                self._start_file_drag(pid)
            return
        # Box select: draw the box and highlight the rows it touches.
        self._rb_last = pos
        self._band_update()
        if self._band_active():
            self.live_selecting = True
            self._live_box_select()
        e.accept()

    def mouseReleaseEvent(self, e):
        kind, mods, base = self._press_kind, self._press_mods, set(self._base_rows)
        press_event = self._press_event
        boxed = self._band_active()
        rows = self._band_rows() if boxed else None
        self._reset_press()
        if e.button() != Qt.LeftButton or kind is None:
            super().mouseReleaseEvent(e)
            return
        if mods:
            super().mouseReleaseEvent(e)     # Ctrl/Shift+Click result stands
            if rows is not None:
                self._apply_rows(base | rows)
                self.boxFinished.emit()
            return
        if rows is not None:                 # box finished: it is the selection
            self._apply_rows(rows)
            super().mouseReleaseEvent(e)
            self.boxFinished.emit()
            e.accept()
            return
        # A plain click: let Qt select (row, anchor and current row included).
        if press_event is not None:
            super().mousePressEvent(press_event)
        super().mouseReleaseEvent(e)

    def restore_selection(self, ids, current_id=None):
        """Re-select rows by job id after the model was rebuilt, and put the
        current row back. A model reset clears both. Without the current row
        Qt has no Shift+Click anchor, so the next Shift+Click extended the
        selection from the FIRST row instead of the row clicked last. Ctrl and
        Shift clicks themselves already reach Qt's selection model untouched
        (see mousePressEvent), and the drag-out of selected finished rows is
        unchanged."""
        sm, model = self.selectionModel(), self.model()
        if sm is None or model is None:
            return
        ids = set(ids or ())
        sel, cur = QItemSelection(), None
        last = model.columnCount() - 1
        for r, j in enumerate(getattr(model, "items", [])):
            jid = j.get("id")
            if jid in ids:
                sel.select(model.index(r, 0), model.index(r, last))
            if current_id is not None and jid == current_id:
                cur = model.index(r, 0)
        if not sel.isEmpty():
            sm.select(sel, QItemSelectionModel.Select | QItemSelectionModel.Rows)   # one signal
        if cur is not None:
            sm.setCurrentIndex(cur, QItemSelectionModel.NoUpdate)

    def _drag_pixmap(self, names):
        """Small rounded badge shown under the cursor during a file drag:
        the file name for one file, "N files" for several. Returns a null
        QPixmap on any failure so the drag still starts without it."""
        try:
            from PySide6.QtGui import QFontMetrics
            if not names:
                return QPixmap()
            text = names[0] if len(names) == 1 else f"{len(names)} files"
            if len(text) > 40:
                text = text[:37] + "..."
            font = QFont(self.font())
            fm = QFontMetrics(font)
            w, h = fm.horizontalAdvance(text) + 28, fm.height() + 14
            ratio = self.devicePixelRatioF()
            pm = QPixmap(int(w * ratio), int(h * ratio))
            pm.setDevicePixelRatio(ratio)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QColor(255, 255, 255, 90))
            p.setBrush(QColor(32, 36, 44, 235))
            p.drawRoundedRect(QRect(0, 0, w - 1, h - 1), 8, 8)
            p.setPen(QColor(255, 255, 255))
            p.setFont(font)
            p.drawText(QRect(0, 0, w, h), Qt.AlignCenter, text)
            p.end()
            return pm
        except Exception:
            return QPixmap()

    def _start_file_drag(self, pressed_id):
        model = self.model()
        rows = sorted({i.row() for i in self.selectionModel().selectedIndexes()})
        selected = [model.items[r].get("id") for r in rows if r < len(model.items)]
        # Press only starts on a selected row, so this carries the whole selection.
        ids = selected if pressed_id in selected else [pressed_id]
        mime = model.mimeDataForJobs(ids)
        if not mime.hasUrls():
            return
        # Windows drop fix: Explorer/Discord want plain text alongside the URLs.
        # Test switch: set VG_DRAG_NOTEXT=1 to drop the text/plain part and see
        # whether Discord's stop icon goes away (urls only, like Explorer).
        if os.environ.get("VG_DRAG_NOTEXT") != "1":
            mime.setText("\n".join(u.toLocalFile() for u in mime.urls()))
        # Preferred path on Windows: a drag that carries only the native CF_HDROP
        # file list, like Explorer's (Discord refuses drags that advertise
        # text/uri-list, which Qt always adds). Set VG_DRAG_QT=1 to force the Qt
        # drag below instead. Any failure here falls through to the Qt drag.
        if sys.platform == "win32" and os.environ.get("VG_DRAG_QT") != "1":
            native = None
            try:
                native = _native_file_drag(
                    int(self.window().winId()),
                    [u.toLocalFile() for u in mime.urls()])
            except Exception:
                native = None
            try:
                from settings import log as _nlog
                try:
                    _nadmin = bool(ctypes.windll.shell32.IsUserAnAdmin())
                except Exception:
                    _nadmin = None
                _nlog(f"file drag: native CF_HDROP drag, {len(mime.urls())} file(s), "
                      f"effect={native} (1=copied, 0=refused, -1=Esc, None=fell back to Qt), "
                      f"admin={_nadmin}")
            except Exception:
                pass
            if native is not None:
                try:
                    self.setState(QAbstractItemView.NoState)   # the drag swallowed the mouse release
                except Exception:
                    pass
                self._band_end()
                self.viewport().update()
                return
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Electron/Chromium targets (Discord, Slack, Telegram Desktop) behave
        # better when the drag carries a real image, so give it a small badge.
        pm = self._drag_pixmap([Path(u.toLocalFile()).name for u in mime.urls()])
        if not pm.isNull():
            drag.setPixmap(pm)
            ratio = pm.devicePixelRatio() or 1.0
            drag.setHotSpot(QPoint(14, int(pm.height() / ratio / 2)))
        # Copy only, on purpose: offering MoveAction lets Explorer MOVE the file
        # out of the download folder when you drop on the same drive.
        res = drag.exec(Qt.CopyAction)
        try:
            # What the drop target answered (CopyAction = accepted, IgnoreAction
            # = the stop icon), plus elevation, since an admin-run app cannot
            # drop onto a normal-user app (Windows UIPI).
            try:
                _admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                _admin = None
            from settings import log as _dlog
            _dlog(f"file drag: {len(mime.urls())} file(s), result={res}, "
                  f"admin={_admin}, text={'no' if os.environ.get('VG_DRAG_NOTEXT') == '1' else 'yes'}")
        except Exception:
            pass
        try:
            self.setState(QAbstractItemView.NoState)   # the drag swallowed the mouse release
        except Exception:
            pass
        self._band_end()
        self.viewport().update()


_LIVE_DRAG_OBJECTS = set()   # COM objects (and their callbacks) stay alive until the last Release


def _native_file_drag(hwnd, paths):
    """Windows only. Start a drag that carries exactly what Explorer carries
    for a file drag: the native CF_HDROP file list and nothing else. Qt cannot
    do this, because it only makes CF_HDROP from text/uri-list, and Discord
    refuses any drag that advertises text/uri-list (drag test bench, test 3).
    Built from a small hand-made IDataObject and IDropSource run through the
    standard OLE DoDragDrop, with no shell helper calls.
    Returns None when it could not start (the caller then falls back to Qt's
    QDrag), otherwise the final drop effect: 1 = copied, 0 = refused by the
    target, -1 = cancelled with Esc. hwnd is accepted for the caller's sake
    and is not needed here.
    Copy only, on purpose: no Move, so a drop can never take the file out of
    the download folder. Runs on the GUI thread, which Qt already put in OLE."""
    if sys.platform != "win32" or not paths:
        return None
    import struct
    import uuid
    c_void_p, HR, WINF = ctypes.c_void_p, ctypes.c_long, ctypes.WINFUNCTYPE

    def _hr(v):                      # unsigned HRESULT constant to the signed value ctypes returns
        return v - (1 << 32) if v & 0x80000000 else v
    E_NOINTERFACE, E_NOTIMPL, E_FAIL = _hr(0x80004002), _hr(0x80004001), _hr(0x80004005)
    DV_E_FORMATETC, DV_E_TYMED = _hr(0x80040064), _hr(0x80040069)
    ADVISE_NOTSUPPORTED = _hr(0x80040003)
    DATA_S_SAMEFORMATETC = 0x40130
    DRAGDROP_S_DROP, DRAGDROP_S_CANCEL, DRAGDROP_S_USEDEFAULTCURSORS = 0x40100, 0x40101, 0x40102
    CF_HDROP, TYMED_HGLOBAL, DVASPECT_CONTENT = 15, 1, 1
    IID_UNK = uuid.UUID("00000000-0000-0000-c000-000000000046").bytes_le
    IID_DATAOBJ = uuid.UUID("0000010e-0000-0000-c000-000000000046").bytes_le
    IID_DROPSRC = uuid.UUID("00000121-0000-0000-c000-000000000046").bytes_le

    class _FORMATETC(ctypes.Structure):
        _fields_ = [("cfFormat", ctypes.c_ushort), ("ptd", c_void_p),
                    ("dwAspect", ctypes.c_uint32), ("lindex", ctypes.c_int32),
                    ("tymed", ctypes.c_uint32)]

    class _STGMEDIUM(ctypes.Structure):
        _fields_ = [("tymed", ctypes.c_uint32), ("hGlobal", c_void_p),
                    ("pUnkForRelease", c_void_p)]

    class _Obj(ctypes.Structure):    # a COM object is just a pointer to its vtable
        _fields_ = [("vtbl", c_void_p)]

    def _make_vtbl(names):
        return type("_Vtbl", (ctypes.Structure,), {"_fields_": [(n, c_void_p) for n in names]})()

    FE, SM = ctypes.POINTER(_FORMATETC), ctypes.POINTER(_STGMEDIUM)
    PPV = ctypes.POINTER(c_void_p)

    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32 = ctypes.WinDLL("user32")
    ole32.OleInitialize.argtypes = [c_void_p]
    ole32.OleInitialize.restype = HR
    ole32.OleUninitialize.argtypes = []
    ole32.OleUninitialize.restype = None
    ole32.DoDragDrop.argtypes = [c_void_p, c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    ole32.DoDragDrop.restype = HR
    shell32.SHCreateStdEnumFmtEtc.argtypes = [ctypes.c_uint32, FE, PPV]
    shell32.SHCreateStdEnumFmtEtc.restype = HR
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint32, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = c_void_p
    kernel32.GlobalLock.argtypes = [c_void_p]
    kernel32.GlobalLock.restype = c_void_p
    kernel32.GlobalUnlock.argtypes = [c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [c_void_p]
    kernel32.GlobalFree.restype = c_void_p
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = ctypes.c_int

    # DROPFILES header (pFiles, pt.x, pt.y, fNC, fWide) + UTF-16 paths, double-null ended.
    blob = struct.pack("<IiiII", 20, 0, 0, 0, 1) + b"".join(
        os.path.normpath(q).encode("utf-16-le") + b"\x00\x00" for q in paths) + b"\x00\x00"

    class _Holder:
        pass
    h = _Holder()
    h.refs = 1

    def _drop_ref():
        h.refs -= 1
        if h.refs <= 0:
            _LIVE_DRAG_OBJECTS.discard(h)
        return max(h.refs, 0)

    # ---- IDataObject ---------------------------------------------------
    def d_qi(this, riid, ppv):
        try:
            if ctypes.string_at(riid, 16) in (IID_UNK, IID_DATAOBJ):
                ppv[0] = ctypes.addressof(h.dobj)
                h.refs += 1
                return 0
            ppv[0] = None
        except Exception:
            pass
        return E_NOINTERFACE

    def d_addref(this):
        h.refs += 1
        return h.refs

    def d_release(this):
        return _drop_ref()

    def _check(fe):
        if fe.cfFormat != CF_HDROP or not (fe.dwAspect & DVASPECT_CONTENT):
            return DV_E_FORMATETC
        if not (fe.tymed & TYMED_HGLOBAL):
            return DV_E_TYMED
        return 0

    def d_getdata(this, pfe, pmed):
        try:
            bad = _check(pfe.contents)
            if bad:
                return bad
            hg = kernel32.GlobalAlloc(0x2, len(blob))      # GMEM_MOVEABLE
            if not hg:
                return E_FAIL
            ptr = kernel32.GlobalLock(hg)
            if not ptr:
                kernel32.GlobalFree(hg)
                return E_FAIL
            ctypes.memmove(ptr, blob, len(blob))
            kernel32.GlobalUnlock(hg)
            m = pmed.contents
            m.tymed = TYMED_HGLOBAL
            m.hGlobal = hg
            m.pUnkForRelease = None
            return 0
        except Exception:
            return E_FAIL

    def d_getdatahere(this, pfe, pmed):
        return E_NOTIMPL

    def d_query(this, pfe):
        try:
            return _check(pfe.contents)
        except Exception:
            return E_FAIL

    def d_canonical(this, pin, pout):
        try:
            if pout:
                pout.contents.ptd = None
        except Exception:
            pass
        return DATA_S_SAMEFORMATETC

    def d_setdata(this, pfe, pmed, release):
        return E_NOTIMPL

    def d_enum(this, direction, ppenum):
        try:
            if direction != 1:                       # DATADIR_GET only
                return E_NOTIMPL
            fe = _FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
            pen = c_void_p()
            hr = shell32.SHCreateStdEnumFmtEtc(1, ctypes.byref(fe), ctypes.byref(pen))
            if hr >= 0:
                ppenum[0] = pen.value
            return hr
        except Exception:
            return E_FAIL

    def d_dadvise(this, pfe, flags, padv, pconn):
        return ADVISE_NOTSUPPORTED

    def d_dunadvise(this, conn):
        return ADVISE_NOTSUPPORTED

    def d_enumdadvise(this, ppenum):
        return ADVISE_NOTSUPPORTED

    # ---- IDropSource ---------------------------------------------------
    def s_qi(this, riid, ppv):
        try:
            if ctypes.string_at(riid, 16) in (IID_UNK, IID_DROPSRC):
                ppv[0] = ctypes.addressof(h.dsrc)
                return 0
            ppv[0] = None
        except Exception:
            pass
        return E_NOINTERFACE

    def s_addref(this):
        return 2

    def s_release(this):
        return 1

    def s_continue(this, esc, keys):
        if esc:
            return DRAGDROP_S_CANCEL
        if not (keys & 1):                           # MK_LBUTTON released: drop
            return DRAGDROP_S_DROP
        return 0

    def s_feedback(this, effect):
        return DRAGDROP_S_USEDEFAULTCURSORS

    d_protos = [
        (WINF(HR, c_void_p, c_void_p, PPV), d_qi),
        (WINF(ctypes.c_ulong, c_void_p), d_addref),
        (WINF(ctypes.c_ulong, c_void_p), d_release),
        (WINF(HR, c_void_p, FE, SM), d_getdata),
        (WINF(HR, c_void_p, FE, SM), d_getdatahere),
        (WINF(HR, c_void_p, FE), d_query),
        (WINF(HR, c_void_p, FE, FE), d_canonical),
        (WINF(HR, c_void_p, FE, SM, ctypes.c_int), d_setdata),
        (WINF(HR, c_void_p, ctypes.c_uint32, PPV), d_enum),
        (WINF(HR, c_void_p, FE, ctypes.c_uint32, c_void_p, ctypes.POINTER(ctypes.c_uint32)), d_dadvise),
        (WINF(HR, c_void_p, ctypes.c_uint32), d_dunadvise),
        (WINF(HR, c_void_p, PPV), d_enumdadvise),
    ]
    s_protos = [
        (WINF(HR, c_void_p, c_void_p, PPV), s_qi),
        (WINF(ctypes.c_ulong, c_void_p), s_addref),
        (WINF(ctypes.c_ulong, c_void_p), s_release),
        (WINF(HR, c_void_p, ctypes.c_int, ctypes.c_uint32), s_continue),
        (WINF(HR, c_void_p, ctypes.c_uint32), s_feedback),
    ]
    d_names = ["QueryInterface", "AddRef", "Release", "GetData", "GetDataHere",
               "QueryGetData", "GetCanonicalFormatEtc", "SetData", "EnumFormatEtc",
               "DAdvise", "DUnadvise", "EnumDAdvise"]
    s_names = ["QueryInterface", "AddRef", "Release", "QueryContinueDrag", "GiveFeedback"]
    h.callbacks = [proto(fn) for proto, fn in d_protos + s_protos]   # keep alive
    h.dvt, h.svt = _make_vtbl(d_names), _make_vtbl(s_names)
    for i, n in enumerate(d_names):
        setattr(h.dvt, n, ctypes.cast(h.callbacks[i], c_void_p).value)
    for i, n in enumerate(s_names):
        setattr(h.svt, n, ctypes.cast(h.callbacks[len(d_names) + i], c_void_p).value)
    h.dobj, h.dsrc = _Obj(ctypes.addressof(h.dvt)), _Obj(ctypes.addressof(h.svt))

    hr = ole32.OleInitialize(None)           # S_OK / S_FALSE both count; balanced below
    if hr < 0:
        return None
    _LIVE_DRAG_OBJECTS.add(h)
    try:
        user32.ReleaseCapture()              # let OLE take the mouse for the drag
        effect = ctypes.c_uint32(0)
        hr = ole32.DoDragDrop(ctypes.addressof(h.dobj), ctypes.addressof(h.dsrc),
                              1, ctypes.byref(effect))               # 1 = DROPEFFECT_COPY
        if hr < 0:
            return None
        if hr == DRAGDROP_S_CANCEL:
            return -1
        return int(effect.value)
    finally:
        _drop_ref()                          # our own reference; a target may still hold one
        try:
            ole32.OleUninitialize()
        except Exception:
            pass


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


def _playlist_dir_for(job):
    """Folder to open for a legacy whole-playlist job whose own file path
    does not exist, or None for any other job.

    Before Session D, download_playlist=True meant one job that walks a whole
    playlist. Session D queues playlists as one job per item
    (download_playlist=False, playlist_id set). download_playlist=True now
    appears only on pre-Session-D jobs and on jobs made with "Legacy
    whole-playlist mode" or by API callers - all share the batch-1 layout."""
    if not job.get("download_playlist") or job.get("playlist_id"):
        return None
    cat_dir = _path_for(job).parent
    name = job.get("playlist_dir")
    if name and (cat_dir / name).is_dir():
        return cat_dir / name
    return cat_dir if cat_dir.is_dir() else None


def _copy_files_to_clipboard(jobs):
    """Put finished files on the clipboard so Ctrl+V pastes the actual file
    into Explorer/Discord/Slack. Qt maps text/uri-list to CF_HDROP on
    Windows itself. Returns the number of files copied."""
    urls = []
    for j in jobs:
        if j.get("status") != "done":
            continue
        try:
            p = _path_for(j)
            if p.exists():
                urls.append(QUrl.fromLocalFile(str(p)))
        except Exception:
            continue
    if not urls:
        return 0
    mime = QMimeData()
    mime.setUrls(urls)
    mime.setText("\n".join(u.toLocalFile() for u in urls))
    QApplication.clipboard().setMimeData(mime)
    return len(urls)


def _progress_pct(job):
    """Numeric progress straight from the job dict (no string parsing):
    conversion progress while converting, otherwise size_done/size_total
    with a zero-total guard. Queued/paused/done jobs report 0."""
    if job.get("phase") == "converting":
        return float(job.get("conversion_progress") or 0)
    total = job.get("size_total") or 0
    return (job.get("size_done") or 0) / total * 100 if total else 0.0


def _ffmpeg_dir():
    """Folder with the app's own ffmpeg, if the backend module that knows it is
    already loaded. Never imports anything new (server.py runs as __main__)."""
    for mod in ("engines", "downloader", "jobs", "api"):
        fn = getattr(sys.modules.get(mod), "find_ffmpeg_dir", None)
        if fn:
            try:
                d = fn()
                if d:
                    return str(d)
            except Exception:
                pass
    return None


def _detail_info(j):
    """Everything the detail pane shows, as plain strings, from one job dict."""
    from urllib.parse import urlparse
    status = j.get("status") or ""
    converting = j.get("phase") == "converting"
    total, done = j.get("size_total") or 0, j.get("size_done") or 0
    pct = 100.0 if (status == "done" and not converting) else _progress_pct(j)
    eta = _eta_seconds(j)
    try:
        path = _path_for(j)
    except Exception:
        path = None
    ext = (path.suffix if path else Path(str(j.get("filename") or "")).suffix).lower()
    if converting:
        progress_text = f"Converting to {j.get('converting_fmt', '')}: {j.get('conversion_progress', 0)}%"
    elif total:
        progress_text = f"{fmt_bytes(done)} / {fmt_bytes(total)} ({pct:.0f}%)"
    else:
        progress_text = "Done" if status == "done" else status
    speed = j.get("speed") or ""
    remaining = _fmt_duration(eta) if eta is not None else ""
    downloaded = fmt_bytes(done) if done else ""
    note, note_color = [], ""
    retry = _retry_text(j)
    if retry:
        note.append(f"Auto-retry: {retry}")
        note_color = WARN
    if j.get("error"):
        note.append(str(j["error"])[:300])
        note_color = DANGER
    host = ""
    try:
        host = urlparse(str(j.get("url") or "")).hostname or ""
    except Exception:
        pass
    general = [
        ("URL", j.get("url")), ("Saved as", j.get("filename")), ("Category", j.get("category")),
        ("Size", fmt_bytes(total) if total else ""), ("Created", fmt_ts(j.get("created_ts"))),
        ("Status", f"{status} (converting)" if converting else status),
        ("Saved to", str(path) if path else ""), ("Source", j.get("source_site")),
    ]
    if j.get("completed_ts"):
        general.append(("Completed", fmt_ts(j.get("completed_ts"))))
    progress = [
        ("Progress", f"{pct:.0f}%"),
        ("Downloaded", f"{fmt_bytes(done)} of {fmt_bytes(total)}" if total else downloaded),
        ("Speed", speed), ("Time remaining", remaining),
    ]
    if converting:
        progress.append(("Conversion", f"to {j.get('converting_fmt', '')}, {j.get('conversion_progress', 0)}%"))
    if j.get("completed_ts"):
        progress.append(("Completed", fmt_ts(j.get("completed_ts"))))
    connections = [("Source site", j.get("source_site")), ("Host", host)]
    # Only fields the job actually carries are listed (no placeholders).
    for label, key in (("Engine", "engine"), ("Connections", "connections"),
                       ("Segments", "segments"), ("Retries", "retry_count")):
        if j.get(key) not in (None, "", 0):
            connections.append((label, j.get(key)))
    if retry:
        connections.append(("Next retry", retry))
    return {
        "id": j.get("id"), "title": str(j.get("filename") or ""), "pct": pct,
        "progress_text": progress_text, "speed": speed, "remaining": remaining,
        "downloaded": downloaded, "note": "\n".join(note), "note_color": note_color,
        "can_pause": status == "downloading",
        "can_resume": status in ("paused", "stopped", "error"),
        "can_stop": status in ("downloading", "paused", "queued"),
        "done": status == "done",
        "path": str(path) if path else "",
        "path_exists": bool(status == "done" and path and path.exists()),
        "is_video": ext in VIDEO_EXT, "thumb_label": ext.lstrip(".").upper() or "FILE",
        "general": general, "progress": progress, "connections": connections,
    }


_PROGRESS_EASE = QEasingCurve(QEasingCurve.OutQuart)


class ProgressDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 12.2: per-row interpolation toward the newest value over 150 ms.
        self._shown = {}  # job id -> [from_value, to_value, QElapsedTimer]
        self.animations_enabled = True  # 12.5: refreshed by MainWindow

    def _animated_pct(self, row, target):
        entry = self._shown.get(row)
        if entry is None:
            if len(self._shown) > 2000:
                self._shown.clear()  # long sessions: bound the cache
            timer = QElapsedTimer()
            timer.start()
            self._shown[row] = [target, target, timer]
            return target
        old, current, timer = entry
        if target != current:
            entry[0], entry[1] = current, target
            timer.restart()
        if not self.animations_enabled:
            entry[0] = target
            return target
        f = min(1.0, timer.elapsed() / 150.0)
        f = _PROGRESS_EASE.valueForProgress(f)  # OutQuart across the 150 ms window
        return entry[0] + (entry[1] - entry[0]) * f

    def paint(self, painter, option, index):
        if index.column() != 2:
            super().paint(painter, option, index)
            return
        job = index.model().items[index.row()]
        converting = job.get("phase") == "converting"
        # Keyed by job id, not row index: rows shift when jobs are added or
        # filtered, and a row-keyed cache animated the wrong job's bar.
        pct = self._animated_pct(job.get("id", index.row()), _progress_pct(job))
        painter.save()
        rect = option.rect.adjusted(6, 14, -6, -14)
        if rect.height() < 4:
            # Very short rows (Compact preset): keep the bar drawable and
            # centred in the row instead of negative-height.
            rect = QRect(option.rect.left() + 6,
                         option.rect.top() + (option.rect.height() - 4) // 2,
                         option.rect.width() - 12, 4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(TRACK))
        painter.drawRoundedRect(rect, 5, 5)
        fill = int(rect.width() * max(0, min(100, pct)) / 100)
        if fill > 0:
            painter.setBrush(QColor(WARN if converting else ACCENT))
            painter.drawRoundedRect(
                rect.adjusted(0, 0, -(rect.width() - fill), 0), 5, 5
            )
        painter.setPen(QColor(TEXT))
        painter.drawText(option.rect.adjusted(rect.width() + 14, 0, 0, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, f"{pct:.0f}%")
        painter.restore()


def _is_youtube_playlist_url(url):
    """True for youtube.com watch URLs carrying a &list= param."""
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse((url or "").strip())
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host != "youtube.com":
            return False
        return bool(parse_qs(parsed.query).get("list"))
    except Exception:
        return False


class AddDownloadDialog(QDialog):
    # Carries (probe_seq, response_dict) from the background probe thread —
    # Qt widgets are only touched in the slot, never in the thread.
    _formats_ready = Signal(object)
    _head_ready = Signal(object)

    def __init__(self, parent=None, api=None, prefill_url="",
                 prefill_format_id=None, prefill_target_format=None,
                 prefill_download_playlist=False):
        super().__init__(parent)
        self.api = api
        self._probe_seq = 0
        self._head_seq = 0
        self._prefill_format_id = prefill_format_id
        self._prefill_target_format = prefill_target_format
        self._prefill_download_playlist = bool(prefill_download_playlist)
        self.setWindowTitle("Download File Info")
        # Opened from the browser while another app has focus, so it must
        # come to the front: showEvent does that via _bring_to_front. It is
        # deliberately NOT always-on-top; a topmost dialog refuses to
        # minimize with the main window.
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.url = QLineEdit(prefill_url)
        self.url.setPlaceholderText("https://example.com/file.mp4")
        self.url.editingFinished.connect(self._probe)
        form.addRow("URL", self.url)
        self.dup_label = QLabel("")
        self.dup_label.setWordWrap(True)
        self.dup_label.setStyleSheet(f"color: {WARN};")
        self.dup_label.setVisible(False)
        form.addRow("", self.dup_label)
        quality_row = QHBoxLayout()
        self.quality = QListWidget()
        self.quality.setSelectionMode(QAbstractItemView.NoSelection)
        self.quality.setMaximumHeight(170)
        self.quality.itemChanged.connect(self._update_preview)
        quality_row.addWidget(self.quality)
        quality_btns = QVBoxLayout()
        self.select_best_btn = QPushButton("Select best")
        self.select_best_btn.clicked.connect(self._select_best)
        quality_btns.addWidget(self.select_best_btn)
        quality_btns.addStretch(1)
        quality_row.addLayout(quality_btns)
        form.addRow("Quality", quality_row)
        self.queue_preview = QLabel("Best available (1 file)")
        self.queue_preview.setStyleSheet(f"color: {MUTED};")
        form.addRow("Queue", self.queue_preview)
        self.playlist_checkbox = QCheckBox("Download entire playlist (choose items)")
        self.playlist_checkbox.setVisible(False)
        form.addRow("", self.playlist_checkbox)
        self.url.textChanged.connect(self._update_playlist_visibility)
        self._update_playlist_visibility()
        # Pill-initiated "Download entire playlist" pre-checks the box so
        # the user's pill choice survives into the dialog.
        # FIX: isVisibleTo(self) works during __init__; isVisible() does not
        # (isVisible() returns False until the dialog is actually shown).
        if self._prefill_download_playlist and self.playlist_checkbox.isVisibleTo(self):
            self.playlist_checkbox.setChecked(True)

        self._formats_ready.connect(self._on_formats)
        self._head_ready.connect(self._on_head_ready)
        self.category = QComboBox()
        self.category.addItems(CATEGORIES)
        form.addRow("Category", self.category)
        save_row = QHBoxLayout()
        self.save_path = QLineEdit(str(HOME))
        save_row.addWidget(self.save_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        save_row.addWidget(browse)
        form.addRow("Save As", save_row)
        self.remember = QCheckBox()
        form.addRow("", self.remember)
        self.description = QLineEdit()
        form.addRow("Description", self.description)
        self.size_label = QLabel("—")
        form.addRow("Size", self.size_label)
        buttons = QDialogButtonBox()
        self.later_btn = buttons.addButton("Download Later", QDialogButtonBox.ActionRole)
        self.start_btn = buttons.addButton("Start Download", QDialogButtonBox.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        self.later_btn.clicked.connect(lambda: self.done(2))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.skip_dialog = QCheckBox("Don't show this dialog again")
        form.addRow(self.skip_dialog)
        self.remember.setText(f"Remember this path for “{self.category.currentText()}”")
        self.category.currentTextChanged.connect(self._update_remember_label)
        if prefill_url:
            self._probe()

    def showEvent(self, event):
        """Task 3: exec()'s internal show() doesn't guarantee focus/front —
        force it explicitly every time the dialog actually becomes visible."""
        super().showEvent(event)
        _bring_to_front(self)

    def _update_remember_label(self, text):
        self.remember.setText(f"Remember this path for “{text}”")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save folder", self.save_path.text())
        if d:
            self.save_path.setText(d)

    def _probe(self):
        if not self.api:
            return
        url = self.url.text().strip()
        if not url:
            return
        # 11.2: HEAD probe moves off the UI thread (it blocked every
        # editingFinished). A seq counter drops stale responses.
        self._head_seq += 1
        seq = self._head_seq

        def worker():
            try:
                r = self.api.probe_head(url)
                size = r.get("size", 0)
            except Exception:
                size = 0
            try:
                # QoL: warn if this download would fill up the disk —
                # cheap enough to fetch alongside the size probe each time.
                free = self.api.disk_space().get("free", 0)
            except Exception:
                free = 0
            try:
                dup = self.api.dup_check(url)
            except Exception:
                dup = None
            self._head_ready.emit((seq, {"size": size, "free": free, "dup": dup}))

        threading.Thread(target=worker, daemon=True).start()
        self._probe_formats(url)

    def _on_head_ready(self, payload):
        seq, data = payload
        if seq != self._head_seq:
            return  # stale response for an older URL
        dup = data.get("dup")
        if dup:
            where = {"done": "already downloaded", "downloading": "downloading now",
                     "queued": "already queued", "paused": "already in your list (paused)"
                     }.get(dup.get("status"), "already in your list")
            self.dup_label.setText(f"\u26a0 This URL is {where}: {dup.get('filename') or ''}. "
                                   "Starting it again makes a second copy.")
            self.dup_label.setVisible(True)
        else:
            self.dup_label.setVisible(False)
        size = data.get("size", 0)
        free = data.get("free", 0)
        if not size:
            self.size_label.setText("unknown")
            self.size_label.setStyleSheet("")
            self.size_label.setToolTip("")
            return
        text = fmt_bytes(size)
        if free:
            text += f"   ({fmt_bytes(free)} free on disk)"
        self.size_label.setText(text)
        if free and size >= free:
            self.size_label.setStyleSheet(f"color: {DANGER};")
            self.size_label.setToolTip(
                "This file is larger than your available free space — "
                "the download will likely fail partway through.")
        elif free and size >= free * 0.9:
            self.size_label.setStyleSheet(f"color: {WARN};")
            self.size_label.setToolTip(
                "This will use most of your remaining free space.")
        else:
            self.size_label.setStyleSheet("")
            self.size_label.setToolTip("")

    def _probe_formats(self, url):
        """Ask the backend which formats exist, off the UI thread. A seq
        counter drops stale responses when the URL changes quickly."""
        self._probe_seq += 1
        seq = self._probe_seq
        self.quality.clear()
        placeholder = QListWidgetItem("Loading formats…")
        placeholder.setFlags(Qt.NoItemFlags)
        self.quality.addItem(placeholder)
        self.quality.setEnabled(False)

        def worker():
            try:
                data = self.api.probe_formats(url)
            except Exception as e:
                data = {"error": str(e)}
            self._formats_ready.emit((seq, data))

        threading.Thread(target=worker, daemon=True).start()

    def _on_formats(self, payload):
        seq, data = payload
        if seq != self._probe_seq:
            return  # stale — a newer probe superseded this one
        self.quality.clear()
        for f in data.get("formats") or []:
            item = QListWidgetItem(self._format_label(f))
            # format_id lives in UserRole, resolution in UserRole+1, ext in
            # UserRole+2 — if these are lost, values() returns names instead
            # of IDs and every multi-quality download fails.
            item.setData(Qt.UserRole, str(f.get("format_id") or ""))
            item.setData(Qt.UserRole + 1, f.get("resolution") or "")
            item.setData(Qt.UserRole + 2, f.get("ext") or "")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.quality.addItem(item)
        for codec in ("mp3", "flac", "opus", "m4a"):
            item = QListWidgetItem(f"Audio only · {codec.upper()}")
            item.setData(Qt.UserRole, "audio:" + codec)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.quality.addItem(item)
        self.quality.setEnabled(True)
        # Pill-sent format pre-selection ("720p" item opens pre-selected).
        if self._prefill_format_id:
            want = str(self._prefill_format_id)
            for i in range(self.quality.count()):
                if self.quality.item(i).data(Qt.UserRole) == want:
                    self.quality.item(i).setCheckState(Qt.Checked)
                    break
        if self._prefill_target_format:
            want = "audio:" + self._prefill_target_format.lower()
            for i in range(self.quality.count()):
                if self.quality.item(i).data(Qt.UserRole) == want:
                    self.quality.item(i).setCheckState(Qt.Checked)
                    break
        self._update_preview()

    @staticmethod
    def _format_label(f):
        res = f.get("resolution") or ""
        ext = f.get("ext") or ""
        vc = f.get("vcodec") or "none"
        ac = f.get("acodec") or "none"
        size = fmt_bytes(f.get("filesize")) if f.get("filesize") else "?"
        kind = "audio only" if vc == "none" else f"{vc}/{ac}"
        return " · ".join(p for p in (res, ext, kind, size) if p)

    def _update_playlist_visibility(self, *_):
        show = _is_youtube_playlist_url(self.url.text())
        self.playlist_checkbox.setVisible(show)
        if not show:
            self.playlist_checkbox.setChecked(False)

    def _select_best(self):
        """Check the highest-quality row (the probe lists best first)."""
        for i in range(self.quality.count()):
            self.quality.item(i).setCheckState(Qt.Unchecked)
        if self.quality.count():
            self.quality.item(0).setCheckState(Qt.Checked)

    def _checked_rows(self):
        """[(format_id_or_audio, resolution, ext)] for checked rows."""
        rows = []
        for i in range(self.quality.count()):
            item = self.quality.item(i)
            if item.checkState() != Qt.Checked:
                continue
            data = item.data(Qt.UserRole)
            if isinstance(data, str) and data.startswith("audio:"):
                rows.append((data, None, data.split(":", 1)[1]))
            else:
                rows.append((data or None,
                             item.data(Qt.UserRole + 1) or None,
                             item.data(Qt.UserRole + 2) or None))
        return rows

    @staticmethod
    def _res_suffix(resolution):
        """Mirror of jobs._resolution_suffix — keep in sync."""
        res = str(resolution or "").strip()
        m = re.match(r"^\d+x(\d+)$", res)
        return f"{m.group(1)}p" if m else re.sub(r"[^0-9A-Za-z]+", "", res)

    def _update_preview(self, *_):
        """Filename preview — refreshes as boxes are checked."""
        preview = getattr(self, "queue_preview", None)
        if preview is None:
            return  # signal can fire mid-__init__ before the label exists
        url = self.url.text().strip()
        raw = re.sub(r"[?#].*$", "", url).rstrip("/")
        stem = re.sub(r'[<>:"/\\|?*]', "",
                      os.path.splitext(os.path.basename(raw))[0]) or "download"
        rows = self._checked_rows()
        n_video = sum(1 for r in rows if not str(r[0] or "").startswith("audio:"))
        names = []
        for data, res, ext in rows:
            if str(data or "").startswith("audio:"):
                names.append(f"{stem}.{data.split(':', 1)[1]}")
            elif n_video > 1 and res:
                names.append(f"{stem}_{self._res_suffix(res)}.{ext or 'mp4'}")
            else:
                names.append(f"{stem}.{ext or 'mp4'}")
        if not names:
            preview.setText("Best available (1 file)")
        else:
            preview.setText(f"{len(names)} file(s): " + ", ".join(names))

    def values(self):
        """One dict per checked row, ready for ApiClient.add. When nothing
        is checked, a single best-available dict (format_id=None) — the
        legacy single-format behavior."""
        checked = self._checked_rows()
        if not checked:
            # Nothing checked: single best-available job, honoring any
            # pill-sent audio-extraction prefill.
            checked = [(None, None, None)]
            prefill_tfmt = self._prefill_target_format
        else:
            prefill_tfmt = None
        out = []
        for data, res, ext in checked:
            if str(data or "").startswith("audio:"):
                fid, tfmt = None, data.split(":", 1)[1]
            else:
                fid, tfmt = data, prefill_tfmt
            out.append({
                "url": self.url.text().strip(),
                "category": self.category.currentText(),
                "save_path": self.save_path.text().strip(),
                "remember": self.remember.isChecked(),
                "description": self.description.text().strip(),
                "format_id": fid,
                "target_format": tfmt,
                "resolution": res,
                # FIX: isChecked() alone is sufficient — the checkbox can only
                # be checked if the user could see it. The isVisible() guard
                # made the flag depend on Qt's rendering state, which is
                # unreliable during modal exec() loops.
                "download_playlist": self.playlist_checkbox.isChecked(),
                "skip_dialog": self.skip_dialog.isChecked(),
            })
        return out


# ---------------------------------------------------------------------------
# Playlist picker (Session D)
# ---------------------------------------------------------------------------
# Quality choices for the picker. key -> (label, format_id, target_format).
# Height caps use a selector containing "+", which YtDlpEngine.download passes
# to yt-dlp verbatim; it prefers mp4/m4a so no re-encode is needed when the
# site offers them, and falls back to any best-up-to-N.
def _height_cap_selector(h):
    return (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]")


_PL_QUALITIES = [
    ("best", "Best available", None, None),
    ("1080", "1080p max", _height_cap_selector(1080), None),
    ("720", "720p max", _height_cap_selector(720), None),
    ("480", "480p max", _height_cap_selector(480), None),
    ("360", "360p max", _height_cap_selector(360), None),
    ("mp3", "Audio only · MP3", None, "mp3"),
    ("m4a", "Audio only · M4A", None, "m4a"),
    ("opus", "Audio only · Opus", None, "opus"),
    ("flac", "Audio only · FLAC", None, "flac"),
]

_PROBE_HEADLINES = {
    "auth_required": "This playlist looks private. Private, Watch Later and "
                     "Liked playlists can't be loaded.",
    "playlist_unavailable": "Playlist not found or unavailable.",
    "timeout": "The request timed out.",
    "engine_disabled": "The yt-dlp engine is disabled in Settings.",
    "bad_url": "That doesn't look like a valid URL.",
}


def _fmt_hms(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_total_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h:
        return f"{h} h {m} m"
    return f"{m} m" if m else ("<1 m" if sec else "0 m")


def _playlist_batch_items(items, playlist, quality_key="best", category=None):
    """Probe items -> /batch item dicts. Shared by every playlist queueing
    path so they all produce identical jobs.

    Filename is "NNN - Title.ext": the playlist index zero-padded to at least
    3 digits (wider for playlists over 999), title cut to 90 characters. That
    reproduces the pre-Session-D "%(playlist_index)03d - %(title)s" layout."""
    quality = next((q for q in _PL_QUALITIES if q[0] == quality_key),
                   _PL_QUALITIES[0])
    _key, _label, format_id, target_format = quality
    playlist = playlist or {}
    top = max([i.get("index") or 0 for i in items] + [playlist.get("total") or 0])
    width = max(3, len(str(top)))
    ext = target_format or "mp4"
    out = []
    for it in items:
        title = (it.get("title") or "untitled").strip()[:90].strip() or "untitled"
        row = {
            "url": it["url"],
            "filename": f"{int(it['index']):0{width}d} - {title}.{ext}",
            "type": "ytdlp",
            "playlist_id": playlist.get("id"),
            "playlist_title": playlist.get("title"),
            "playlist_index": it["index"],
            "playlist_url": playlist.get("url"),
            "format_id": format_id,
            "target_format": target_format,
        }
        if target_format:
            row["category"] = category or "Music"
        elif category:
            row["category"] = category
        out.append(row)
    return out


class PlaylistPickerDialog(QDialog):
    """Checklist of a playlist's items. exec() == Accepted means "queue the
    checked ones": read them with values() and request_headers()."""

    # Carries (probe_seq, append, response_dict) from the probe thread; Qt
    # widgets are only touched in the slot, never in the thread.
    _page_ready = Signal(object)
    PAGE = 200
    CONFIRM_ABOVE = 50

    def __init__(self, parent=None, api=None, prefill_url="",
                 referer=None, cookie=None, user_agent=None):
        super().__init__(parent)
        self.api = api
        self._referer = referer
        self._cookie = cookie
        self._user_agent = user_agent
        self._seq = 0
        self._bulk = False            # re-entrancy guard for bulk check edits
        self._anchor_row = None       # last plain checkbox click (shift range)
        self._items = []              # probe item dicts, one per table row
        self._playlist = None
        self._probe_url = ""
        self._next_start = 0
        self._truncated = False
        self.setWindowTitle("Pick playlist items")
        # Same as AddDownloadDialog: brought to the front on show, but not
        # pinned always-on-top (see _bring_to_front).
        self.setMinimumSize(720, 560)
        root = QVBoxLayout(self)

        url_row = QHBoxLayout()
        self.url = QLineEdit(prefill_url)
        self.url.setPlaceholderText("https://www.youtube.com/playlist?list=...")
        self.url.returnPressed.connect(self._load)
        url_row.addWidget(self.url, 1)
        self.load_btn = QPushButton("Load")
        self.load_btn.setAutoDefault(False)
        self.load_btn.clicked.connect(self._load)
        url_row.addWidget(self.load_btn)
        root.addLayout(url_row)

        self.header = QLabel("Enter a playlist URL and press Load.")
        self.header.setWordWrap(True)
        self.header.setStyleSheet(f"color: {MUTED};")
        root.addWidget(self.header)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "#", "Title", "Duration"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)   # playlist order is the point
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        for label, fn in (("All", self._check_all), ("None", self._check_none),
                          ("Invert", self._check_invert)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(fn)
            btn_row.addWidget(b)
        self.more_btn = QPushButton(f"Load next {self.PAGE}")
        self.more_btn.setAutoDefault(False)
        self.more_btn.clicked.connect(lambda: self._load(append=True))
        self.more_btn.setVisible(False)
        btn_row.addWidget(self.more_btn)
        btn_row.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {MUTED};")
        btn_row.addWidget(self.status)
        root.addLayout(btn_row)

        q_row = QHBoxLayout()
        q_row.addWidget(QLabel("Quality"))
        self.quality = QComboBox()
        for key, label, _fid, _tf in _PL_QUALITIES:
            self.quality.addItem(label, key)
        q_row.addWidget(self.quality, 1)
        root.addLayout(q_row)

        buttons = QDialogButtonBox()
        self.queue_btn = buttons.addButton("Queue 0 items", QDialogButtonBox.AcceptRole)
        self.queue_btn.setAutoDefault(False)   # Enter in the URL field must not queue
        self.queue_btn.setDefault(False)
        self.queue_btn.setEnabled(False)
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._page_ready.connect(self._on_page)
        if prefill_url:
            self._load()

    # --- window behavior (matches AddDownloadDialog) ---
    def showEvent(self, event):
        super().showEvent(event)
        _bring_to_front(self)

    def keyPressEvent(self, event):
        # Enter in the URL field loads the playlist (returnPressed), but must
        # never fall through to QDialog's default-button handling, which
        # would queue the checked items.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def done(self, r):
        # Any close path (Queue, Cancel, X, Esc): drop in-flight probe
        # responses. The HTTP call itself can't be aborted from here; its
        # late result is ignored by the seq check.
        self._seq += 1
        super().done(r)

    # --- loading ---
    def _load(self, append=False):
        if not self.api:
            return
        append = bool(append) and self._playlist is not None
        url = self._probe_url if append else self.url.text().strip()
        if not url:
            return
        self._seq += 1
        seq = self._seq
        start = self._next_start if append else 0
        if not append:
            self._probe_url = url
            self._bulk = True
            try:
                self.table.setRowCount(0)
            finally:
                self._bulk = False
            self._items = []
            self._playlist = None
            self._anchor_row = None
            self._truncated = False
            self.more_btn.setVisible(False)
            self._set_header("Loading...", error=False)
        else:
            self._set_header(self._header_text() + "  -  loading more...", error=False)
        self.load_btn.setEnabled(False)
        self.more_btn.setEnabled(False)
        self._update_status()
        referer, cookie, ua = self._referer, self._cookie, self._user_agent

        def worker():
            try:
                data = self.api.probe_playlist(url, start=start, limit=self.PAGE,
                                               referer=referer, cookie=cookie,
                                               user_agent=ua)
            except ProbeError as e:
                data = {"error": str(e), "code": e.code}
            except Exception as e:
                data = {"error": str(e), "code": ""}
            try:
                self._page_ready.emit((seq, append, data))
            except RuntimeError:
                pass  # dialog already destroyed

        threading.Thread(target=worker, daemon=True).start()

    def _on_page(self, payload):
        seq, append, data = payload
        if seq != self._seq:
            return  # stale: a newer load (or closing the dialog) superseded it
        self.load_btn.setEnabled(True)
        self.more_btn.setEnabled(True)
        if data.get("error"):
            headline = _PROBE_HEADLINES.get(data.get("code"), "Couldn't load the playlist.")
            msg = f"{headline}\n{str(data['error'])[:300]}"
            if append:
                # Loaded rows stay; "Load next" doubles as Retry.
                self._set_header(self._header_text() + "\n" + msg, error=True)
            else:
                self._set_header(msg, error=True)
            self._update_status()
            return
        if not data.get("is_playlist"):
            self._set_header("This URL is a single video, not a playlist. "
                             "Use Add URL for single videos.", error=False)
            self.more_btn.setVisible(False)
            self._update_status()
            return
        if not append:
            self._playlist = data.get("playlist") or {}
        new_items = data.get("items") or []
        self._items.extend(new_items)
        self._truncated = bool(data.get("truncated"))
        self._next_start = int(data.get("start", 0)) + int(data.get("limit", self.PAGE))
        self._append_rows(new_items)
        self.more_btn.setVisible(self._truncated)
        if not self._items:
            self._set_header("This playlist is empty.", error=False)
        else:
            self._set_header(self._header_text(), error=False)
        self._update_status()

    def _header_text(self):
        pl = self._playlist or {}
        title = pl.get("title") or "Playlist"
        loaded = len(self._items)
        total = pl.get("total")
        text = f"{title}  -  {loaded:,} of {total:,}" if total else f"{title}  -  {loaded:,} items"
        if self._truncated:
            text += ", more available"
        return text

    def _set_header(self, text, error=False):
        self.header.setText(text)
        self.header.setStyleSheet(f"color: {DANGER if error else MUTED};")

    def _append_rows(self, new_items):
        base = self.table.rowCount()
        self._bulk = True
        try:
            self.table.setRowCount(base + len(new_items))
            for i, it in enumerate(new_items):
                r = base + i
                avail = bool(it.get("available"))
                chk = QTableWidgetItem("")
                idx = QTableWidgetItem(str(it.get("index", "")))
                idx.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                archived = bool(it.get("archived"))
                title = QTableWidgetItem((it.get("title") or "untitled")
                                         + ("   \u2713 already downloaded" if archived else ""))
                title.setToolTip(it.get("title") or "")
                dur = QTableWidgetItem(_fmt_hms(it.get("duration")))
                dur.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                chk.setData(Qt.UserRole, it)
                if avail:
                    chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    # all available pre-checked, except ones already downloaded
                    chk.setCheckState(Qt.Unchecked if archived else Qt.Checked)
                    for c in (idx, title, dur):
                        c.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                else:
                    for c in (chk, idx, title, dur):
                        c.setFlags(Qt.NoItemFlags)
                        c.setToolTip("Private or deleted video")
                for col, cell in enumerate((chk, idx, title, dur)):
                    self.table.setItem(r, col, cell)
        finally:
            self._bulk = False

    # --- selection semantics ---
    # "Checked" (the check state on column 0) is the only thing that decides
    # what gets queued. "Selected" (Qt's row highlight) never does.
    def _row_item(self, row):
        return self.table.item(row, 0)

    def _row_available(self, row):
        it = self._row_item(row)
        return bool(it and (it.data(Qt.UserRole) or {}).get("available"))

    def _on_item_changed(self, item):
        if self._bulk or item.column() != 0:
            return
        row = item.row()
        state = item.checkState()
        # Qt's own shift-click extends the highlight, not check states, so
        # range-check is built here: shift-click a checkbox and every
        # available row between it and the last plain click follows.
        if ((QApplication.keyboardModifiers() & Qt.ShiftModifier)
                and self._anchor_row is not None and self._anchor_row != row):
            lo, hi = sorted((self._anchor_row, row))
            self._bulk = True
            try:
                for r in range(lo, hi + 1):
                    it = self._row_item(r)
                    if it is not None and self._row_available(r):
                        it.setCheckState(state)
            finally:
                self._bulk = False
        self._anchor_row = row
        self._update_status()

    def _set_all(self, fn):
        self._bulk = True
        try:
            for r in range(self.table.rowCount()):
                it = self._row_item(r)
                if it is not None and self._row_available(r):
                    it.setCheckState(fn(it.checkState()))
        finally:
            self._bulk = False
        self._update_status()

    def _check_all(self):
        self._set_all(lambda _s: Qt.Checked)

    def _check_none(self):
        self._set_all(lambda _s: Qt.Unchecked)

    def _check_invert(self):
        self._set_all(lambda s: Qt.Unchecked if s == Qt.Checked else Qt.Checked)

    def _checked_items(self):
        out = []
        for r in range(self.table.rowCount()):
            it = self._row_item(r)
            if it is not None and self._row_available(r) and it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _update_status(self):
        checked = self._checked_items()
        n = len(checked)
        total = sum((c.get("duration") or 0) for c in checked)
        if self.table.rowCount():
            self.status.setText(f"{n} of {self.table.rowCount()} checked"
                                + (f"  -  {_fmt_total_duration(total)}" if n else ""))
        else:
            self.status.setText("")
        self.queue_btn.setText(f"Queue {n} item{'s' if n != 1 else ''}")
        self.queue_btn.setEnabled(n > 0)

    # --- result ---
    def _on_accept(self):
        n = len(self._checked_items())
        if n == 0:
            return
        if n > self.CONFIRM_ABOVE:
            title = (self._playlist or {}).get("title") or "this playlist"
            r = QMessageBox.question(
                self, "Queue many videos",
                f"Queue {n} videos from \"{title}\"?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self.accept()

    def values(self):
        """One /batch item dict per checked row, in playlist order."""
        return _playlist_batch_items(self._checked_items(), self._playlist,
                                     self.quality.currentData())

    def request_headers(self):
        """Kwargs for ApiClient.batch: sent once at the top level."""
        return {"referer": self._referer, "cookie": self._cookie,
                "user_agent": self._user_agent}

    def playlist_title(self):
        return (self._playlist or {}).get("title") or "playlist"


# v5.5: ffmpeg/deno check and update workers. Parentless and kept here so
# closing the Settings dialog mid-download can't destroy a running QThread.
_COMPONENT_WORKERS = set()


class _SettingsNav(QWidget):
    """Left-hand page list + stacked pages + search, in place of a QTabWidget
    (ten tabs behind scroll arrows). Exposes addTab() so the page-building
    code is unchanged."""

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search settings\u2026")
        self.search.setClearButtonEnabled(True)
        self.list = QListWidget()
        self.list.setObjectName("SettingsNav")
        self.list.setFixedWidth(176)
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        self.stack = QStackedWidget()
        outer.addLayout(left)
        outer.addWidget(self.stack, 1)
        self._index = []
        self.list.currentRowChanged.connect(self._on_row)
        self.search.textChanged.connect(self._filter)

    def addTab(self, widget, title):
        lay = widget.layout()
        if lay is not None:
            lay.setContentsMargins(6, 6, 16, 6)   # keep controls off the scrollbar edge
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(widget)
        self.stack.addWidget(scroll)
        self.list.addItem(title)
        if self.list.count() == 1:
            self.list.setCurrentRow(0)

    def _on_row(self, row):
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def build_index(self):
        """Collect each page's visible text so search matches setting names,
        not just page titles."""
        self._index = []
        for i in range(self.stack.count()):
            w = self.stack.widget(i).widget()
            texts = [self.list.item(i).text()]
            for cls in (QLabel, QCheckBox, QPushButton):
                texts += [c.text() for c in w.findChildren(cls)]
            texts += [c.title() for c in w.findChildren(QGroupBox)]
            self._index.append(" ".join(texts).lower())

    def _filter(self, text):
        q = (text or "").strip().lower()
        first = -1
        for i in range(self.list.count()):
            hide = bool(q) and q not in (self._index[i] if i < len(self._index) else "")
            self.list.item(i).setHidden(hide)
            if not hide and first < 0:
                first = i
        cur = self.list.currentRow()
        if first >= 0 and (cur < 0 or self.list.item(cur).isHidden()):
            self.list.setCurrentRow(first)


class SettingsDialog(QDialog):
    """Tabbed settings editor — POSTs each value to /settings on Save."""

    def __init__(self, parent=None, api=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Settings")
        self.setMinimumSize(780, 520)
        self.resize(860, 620)
        s = load_settings()
        layout = QVBoxLayout(self)
        tabs = _SettingsNav()
        self.nav = tabs
        layout.addWidget(tabs, 1)

        # ---- General ----
        g = QWidget()
        gl = QFormLayout(g)
        self.force_on_top = QCheckBox("Force window to front on new downloads")
        self.force_on_top.setChecked(bool(s.get("force_on_top", True)))
        self.close_to_tray = QCheckBox("Close to system tray")
        self.close_to_tray.setChecked(bool(s.get("close_to_tray", True)))
        self.watch_recording_folder = QCheckBox(
            "Auto-import screen recordings from the OS captures folder")
        self.watch_recording_folder.setChecked(
            bool(s.get("watch_recording_folder", True)))
        self.clipboard_monitor = QCheckBox(
            "Monitor clipboard for downloadable links")
        self.clipboard_monitor.setChecked(bool(s.get("clipboard_monitor", False)))
        for w in (self.force_on_top, self.close_to_tray,
                  self.watch_recording_folder, self.clipboard_monitor):
            gl.addRow(w)
        # v4 (Session 6): self.accent / self._theme_tokens still live here
        # since _save_inner reads them, but their controls now live in the
        # Theme tab below — one place for everything theme-related.
        self.notify_queue_done = QCheckBox("Show one notification when a batch of downloads finishes")
        self.notify_queue_done.setChecked(bool(s.get("notify_queue_done", True)))
        gl.addRow(self.notify_queue_done)
        self.animations_enabled = QCheckBox("Enable animations")
        self.animations_enabled.setChecked(bool(s.get("animations_enabled", True)))
        gl.addRow(self.animations_enabled)
        # v5.5: Blanc minimal mode
        self.blanc_mode = QCheckBox("Blanc mode: hide the Activity Log, toolbar labels and status bar extras")
        self.blanc_mode.setChecked(bool(s.get("blanc_mode", False)))
        self.blanc_mode.setToolTip("Keeps the sidebar, table, details pane and search. Toolbar buttons "
                                   "show icons only; the status bar keeps the connection indicator "
                                   "and free disk space.")
        gl.addRow(self.blanc_mode)
        # v5.5: inline thumbnail playback in the details pane
        self.thumb_click_mode = QComboBox()
        self.thumb_click_mode.addItem("Click plays inline, double-click opens the external player",
                                      userData="inline")
        self.thumb_click_mode.addItem("Click opens the external player", userData="external")
        _i = self.thumb_click_mode.findData(s.get("thumb_click_mode", "inline"))
        self.thumb_click_mode.setCurrentIndex(_i if _i >= 0 else 0)
        gl.addRow("Details thumbnail", self.thumb_click_mode)
        self.inline_play_muted = QCheckBox("Play thumbnail previews with no audio")
        self.inline_play_muted.setChecked(bool(s.get("inline_play_muted", False)))
        gl.addRow(self.inline_play_muted)
        # v5.5: global text size (scales the font sizes in the stylesheet)
        # Batch 3: a slider (100-150%) instead of the old preset combo, with
        # a live % readout beside it. A saved value outside the range (the
        # old combo offered 85%) is clamped into it.
        try:
            _cur = int(s.get("text_scale", 100) or 100)
        except (TypeError, ValueError):
            _cur = 100
        _cur = max(100, min(150, _cur))
        self.text_scale = QSlider(Qt.Horizontal)
        self.text_scale.setRange(100, 150)
        self.text_scale.setSingleStep(1)
        self.text_scale.setPageStep(5)
        self.text_scale.setTickPosition(QSlider.TicksBelow)
        self.text_scale.setTickInterval(10)
        self.text_scale.setValue(_cur)
        self.text_scale.setToolTip("Above 115% the main window compacts itself: "
                                   "toolbar labels, the Activity Log and the "
                                   "Source / ETA columns hide so nothing overflows.")
        self.text_scale_label = QLabel(f"{_cur}%")
        self.text_scale.valueChanged.connect(
            lambda v: self.text_scale_label.setText(f"{int(v)}%"))
        _ts_row = QWidget()
        _ts_lay = QHBoxLayout(_ts_row)
        _ts_lay.setContentsMargins(0, 0, 0, 0)
        _ts_lay.addWidget(self.text_scale, 1)
        _ts_lay.addWidget(self.text_scale_label)
        # Batch 4: live text preview next to the slider.
        self.text_preview = QLabel("Aa Preview")
        _ts_lay.addWidget(self.text_preview)
        self.text_scale.valueChanged.connect(self._update_text_preview)
        self._update_text_preview(_cur)
        gl.addRow("Text size", _ts_row)
        # Batch 2: App Font. Every installed family; "Theme default" keeps the
        # preset's own font. Saved as settings "app_font".
        self.app_font = QComboBox()
        self.app_font.addItem("Theme default", userData="")
        try:
            _fams = sorted({f for f in QFontDatabase.families() if f and not f.startswith("@")},
                           key=str.lower)
        except Exception:
            _fams = []
        for _fam in _fams:
            self.app_font.addItem(_fam, userData=_fam)
            # Live preview inside the dropdown: each family drawn in itself.
            self.app_font.setItemData(self.app_font.count() - 1, QFont(_fam), Qt.FontRole)
        _saved_font = str(s.get("app_font", "") or "")
        if _saved_font and self.app_font.findData(_saved_font) < 0:
            self.app_font.addItem(_saved_font, userData=_saved_font)   # not installed here: keep it
        _i = self.app_font.findData(_saved_font)
        self.app_font.setCurrentIndex(_i if _i >= 0 else 0)
        self.app_font.setMaxVisibleItems(20)
        self.app_font.setToolTip("Overrides the theme's font everywhere in the app.")
        gl.addRow("App Font", self.app_font)
        # Test notepad: type here to try the chosen font at the chosen size.
        # Nothing typed here is saved.
        self.font_notepad = QTextEdit()
        self.font_notepad.setAcceptRichText(False)
        self.font_notepad.setFixedHeight(76)
        self.font_notepad.setPlaceholderText(
            "Type here to try this font. The quick brown fox jumps over the lazy dog. 0123456789")
        self.font_notepad.setToolTip("Test area for the App Font and Text size. Not saved.")
        gl.addRow("Test text", self.font_notepad)
        # Batch 3: live font preview. The dropdown re-skins the running app
        # the moment it changes; nothing is written to settings until Save,
        # and Cancel puts the saved font back (see done() below).
        self._saved_app_font = _saved_font
        self._font_preview_on = False
        self.app_font.currentIndexChanged.connect(self._preview_app_font)
        self.app_font.currentIndexChanged.connect(lambda *_: self._update_font_notepad())
        self.text_scale.valueChanged.connect(lambda *_: self._update_font_notepad())
        self._update_font_notepad()
        tabs.addTab(g, "General")

        # ---- Theme tab (gui_theme_page.py) ----
        from gui_theme_page import ThemePage
        self.theme_page = ThemePage(s)
        # Double-clicking a theme tile applies it: save and close the dialog.
        self.theme_page.applied.connect(self._save)
        tabs.addTab(self.theme_page, "Theme")

        # ---- Theme Engine button (gui_theme_engine.py) ----
        self.theme_engine_btn = QPushButton("Open Theme Engine\u2026")
        self.theme_engine_btn.setToolTip("Build a theme from an image or by picking colors. "
                                         "Changes preview live; saving writes a theme file.")
        self.theme_engine_btn.clicked.connect(self._open_theme_engine)
        # Dock it directly under the Theme folder tiles and their Apply / Refresh row.
        (getattr(self.theme_page, "folder_layout", None)
         or self.theme_page.controls.layout()).addWidget(self.theme_engine_btn)

        # ---- Downloads ----
        d = QWidget()
        dl = QFormLayout(d)
        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 16)
        self.max_concurrent.setValue(int(s.get("max_concurrent") or 3))
        dl.addRow("Max simultaneous downloads", self.max_concurrent)
        self.speed_limit_kbps = QSpinBox()
        self.speed_limit_kbps.setRange(0, 10_000_000)
        self.speed_limit_kbps.setSingleStep(100)
        self.speed_limit_kbps.setSuffix(" KB/s")
        self.speed_limit_kbps.setSpecialValueText("unlimited")
        self.speed_limit_kbps.setValue(int(s.get("speed_limit_kbps") or 0))
        dl.addRow("Speed limit", self.speed_limit_kbps)
        self.auto_mp4 = QCheckBox("Auto-convert downloaded videos to MP4")
        self.auto_mp4.setChecked(bool(s.get("auto_mp4", True)))
        dl.addRow(self.auto_mp4)
        self.prefer_source_quality = QCheckBox(
            "Prefer source quality (keep original container/codecs)")
        self.prefer_source_quality.setChecked(
            bool(s.get("prefer_source_quality", True)))
        dl.addRow(self.prefer_source_quality)
        self.subtitle_languages = QLineEdit(
            ", ".join(s.get("subtitle_languages", ["en"])))
        self.subtitle_languages.setPlaceholderText("en, es, ...")
        dl.addRow("Subtitle languages", self.subtitle_languages)
        self.min_free_space_mb = QSpinBox()
        self.min_free_space_mb.setRange(0, 1_000_000)
        self.min_free_space_mb.setSingleStep(100)
        self.min_free_space_mb.setSuffix(" MB")
        self.min_free_space_mb.setSpecialValueText("off")
        self.min_free_space_mb.setValue(int(s.get("min_free_space_mb", 500) or 0))
        dl.addRow("Pause new downloads below", self.min_free_space_mb)
        self.hide_old_errors_hours = QSpinBox()
        self.hide_old_errors_hours.setRange(0, 720)
        self.hide_old_errors_hours.setSuffix(" h")
        self.hide_old_errors_hours.setSpecialValueText("never")
        self.hide_old_errors_hours.setValue(int(s.get("hide_old_errors_hours", 24) or 0))
        self.hide_old_errors_hours.setToolTip("Failed downloads older than this are hidden from "
                                              "All and Unfinished. The Failed view still lists them.")
        dl.addRow("Hide failed downloads older than", self.hide_old_errors_hours)
        self.ytdlp_use_archive = QCheckBox(
            "Skip videos already downloaded when queueing whole playlists")
        self.ytdlp_use_archive.setChecked(bool(s.get("ytdlp_use_archive", True)))
        dl.addRow(self.ytdlp_use_archive)
        # ---- Destinations (v5.5: the former "Save To" tab, same settings keys:
        # output_dir and per_category_dirs, so nothing needs migrating) ----
        dest_box = QGroupBox("Destinations")
        stl = QFormLayout(dest_box)
        self.output_dir = QLineEdit(s.get("output_dir", ""))
        stl.addRow("Default folder", self.output_dir)
        self.percat_edits = {}
        for cat in CATEGORIES:  # single source of truth (settings.py); includes Images and Fonts
            e = QLineEdit(s.get("per_category_dirs", {}).get(cat, ""))
            e.setPlaceholderText("(use default folder)")
            self.percat_edits[cat] = e
            stl.addRow(f"{cat} folder", e)
        dl.addRow(dest_box)
        tabs.addTab(d, "Downloads")

        # ---- Post-Download ----
        p = QWidget()
        pl = QFormLayout(p)
        self.defender_scan = QCheckBox("Scan downloads with Windows Defender")
        self.defender_scan.setChecked(bool(s.get("defender_scan", False)))
        self.auto_shutdown = QCheckBox("Shut down PC when the queue empties")
        self.auto_shutdown.setChecked(bool(s.get("auto_shutdown", False)))
        self.auto_extract = QCheckBox("Auto-extract archives after download")
        self.auto_extract.setChecked(bool(s.get("auto_extract", False)))
        self.open_folder_on_complete = QCheckBox(
            "Open containing folder on completion")
        self.open_folder_on_complete.setChecked(
            bool(s.get("open_folder_on_complete", False)))
        self.play_sound_on_complete = QCheckBox("Play sound on completion")
        self.play_sound_on_complete.setChecked(
            bool(s.get("play_sound_on_complete", False)))
        for w in (self.defender_scan, self.auto_shutdown, self.auto_extract,
                  self.open_folder_on_complete):
            pl.addRow(w)
        tabs.addTab(p, "Post-Download")

        # ---- Sounds ---- (moved out of Post-Download: the "Play sound on
        # completion" switch, the sound choice, the custom file and the
        # procedural generator)
        so = QWidget()
        sl = QFormLayout(so)
        sl.addRow(self.play_sound_on_complete)
        # v5.5: completion sound. Bundled presets or your own .wav/.mp3; the
        # Windows beep is only the fallback when the chosen file can't play.
        import sounds as _sounds
        self.sound_preset = QComboBox()
        for _pid, _label in _sounds.PRESETS.items():
            self.sound_preset.addItem(_label, userData=_pid)
        self.sound_preset.addItem("Custom file\u2026", userData=_sounds.CUSTOM)
        _i = self.sound_preset.findData(s.get("completion_sound", _sounds.DEFAULT_PRESET))
        self.sound_preset.setCurrentIndex(_i if _i >= 0 else 0)
        sl.addRow("Completion sound", self.sound_preset)
        self.sound_path = QLineEdit(s.get("completion_sound_path", ""))
        self.sound_path.setReadOnly(True)
        self.sound_path.setPlaceholderText("(no custom file chosen: .wav or .mp3)")
        # Library sounds (<home>/sounds/*.wav) follow the "Custom file..." entry.
        self._refresh_sound_library(
            select_path=self.sound_path.text().strip()
            if s.get("completion_sound") == _sounds.CUSTOM else None)
        self.sound_preset.currentIndexChanged.connect(self._on_sound_preset_changed)
        _brow = QHBoxLayout()
        _brow.addWidget(self.sound_path, 1)
        _pick = QPushButton("Choose custom file\u2026")
        _pick.clicked.connect(self._choose_sound)
        _brow.addWidget(_pick)
        _test = QPushButton("Test Sound")
        _test.clicked.connect(self._test_sound)
        _brow.addWidget(_test)
        sl.addRow("Custom file", _brow)
        _gen = QPushButton("Generate Sounds\u2026")
        _gen.setToolTip("Make three very different completion sounds from scratch and "
                        "keep the one you like")
        _gen.clicked.connect(self._open_sound_generator)
        sl.addRow("Procedural sounds", _gen)
        _gen_hint = QLabel("Builds three sounds from three different style families. "
                           "Pick a length, press Play on each, and Save as Default on "
                           "the one you want.")
        _gen_hint.setObjectName("CardHint")
        _gen_hint.setWordWrap(True)
        sl.addRow(_gen_hint)
        tabs.addTab(so, "Sounds")

        # ---- Connection ----
        c = QWidget()
        cl = QFormLayout(c)
        self.connection_timeout = QSpinBox()
        self.connection_timeout.setRange(5, 120)
        self.connection_timeout.setSuffix(" s")
        self.connection_timeout.setValue(int(s.get("connection_timeout") or 20))
        cl.addRow("Connection timeout", self.connection_timeout)
        self.max_retries = QSpinBox()
        self.max_retries.setRange(0, 20)
        self.max_retries.setValue(int(s.get("max_retries") or 3))
        cl.addRow("Max retries", self.max_retries)
        self.min_speed_kbps = QSpinBox()
        self.min_speed_kbps.setRange(0, 10_000_000)
        self.min_speed_kbps.setSingleStep(50)
        self.min_speed_kbps.setSuffix(" KB/s")
        self.min_speed_kbps.setSpecialValueText("off")
        self.min_speed_kbps.setValue(int(s.get("min_speed_kbps") or 0))
        cl.addRow("Abort if slower than", self.min_speed_kbps)
        self.yt_delay_min = QSpinBox()
        self.yt_delay_min.setRange(0, 60)
        self.yt_delay_min.setSuffix(" s")
        self.yt_delay_min.setValue(int(s.get("ytdlp_sleep_interval", 5) or 0))
        self.yt_delay_max = QSpinBox()
        self.yt_delay_max.setRange(0, 120)
        self.yt_delay_max.setSuffix(" s")
        self.yt_delay_max.setValue(int(s.get("ytdlp_max_sleep_interval", 15) or 0))
        delay_row = QHBoxLayout()
        delay_row.addWidget(self.yt_delay_min)
        delay_row.addWidget(QLabel("to"))
        delay_row.addWidget(self.yt_delay_max)
        delay_row.addStretch(1)
        cl.addRow("YouTube request delay (seconds)", delay_row)
        delay_hint = QLabel("A random wait between YouTube downloads keeps your IP from "
                            "being flagged. Set both to 0 to turn it off.")
        delay_hint.setWordWrap(True)
        delay_hint.setStyleSheet(f"color: {MUTED};")
        cl.addRow(delay_hint)
        self.yt_cooldown = QSpinBox()
        self.yt_cooldown.setRange(1, 240)
        self.yt_cooldown.setSuffix(" min")
        self.yt_cooldown.setValue(int(s.get("ytdlp_bot_cooldown_minutes", 30) or 30))
        cl.addRow("Hold YouTube after a bot check", self.yt_cooldown)
        self.proxy_url = QLineEdit(s.get("proxy_url", "") or "")
        self.proxy_url.setPlaceholderText("http://host:port  or  socks5://host:port  (blank = none)")
        cl.addRow("Proxy", self.proxy_url)
        self.proxy_scope = QComboBox()
        self.proxy_scope.addItem("YouTube only", userData="youtube")
        self.proxy_scope.addItem("All downloads", userData="all")
        _i = self.proxy_scope.findData(s.get("proxy_scope", "youtube"))
        self.proxy_scope.setCurrentIndex(_i if _i >= 0 else 0)
        cl.addRow("Use proxy for", self.proxy_scope)
        tabs.addTab(c, "Connection")

        # ---- Updates ----
        up = QWidget()
        upl = QFormLayout(up)
        self.auto_update_check = QCheckBox("Check for app updates automatically")
        self.auto_update_check.setChecked(bool(s.get("auto_update_check", True)))
        upl.addRow(self.auto_update_check)
        self.update_check_hours = QSpinBox()
        self.update_check_hours.setRange(1, 48)
        self.update_check_hours.setSuffix(" h")
        self.update_check_hours.setValue(int(s.get("update_check_hours", 6) or 6))
        upl.addRow("Check every", self.update_check_hours)
        self.ytdlp_auto_update = QCheckBox("Keep yt-dlp up to date automatically")
        self.ytdlp_auto_update.setChecked(bool(s.get("ytdlp_auto_update", True)))
        upl.addRow(self.ytdlp_auto_update)
        # v5.5: helper text, then the buttons, then a loading bar and the
        # result area below them (the result used to replace the helper line).
        self.update_help = QLabel("Press Refresh to see yt-dlp and app versions.")
        self.update_help.setWordWrap(True)
        self.update_help.setStyleSheet(f"color: {MUTED};")
        upl.addRow(self.update_help)
        ub = QHBoxLayout()
        self._update_btns = []
        for text, fn in (("Refresh", self._update_refresh),
                         ("Check yt-dlp now", self._update_ytdlp_now),
                         ("Roll back yt-dlp", self._update_ytdlp_rollback)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            ub.addWidget(b)
            self._update_btns.append(b)
        ub.addStretch(1)
        upl.addRow(ub)
        self.update_busy = QProgressBar()
        self.update_busy.setRange(0, 0)          # busy bar
        self.update_busy.setTextVisible(False)
        self.update_busy.setFixedHeight(6)
        self.update_busy.setVisible(False)
        upl.addRow(self.update_busy)
        self.update_info = QLabel("Not checked yet.")
        self.update_info.setWordWrap(True)
        self.update_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        upl.addRow(self.update_info)
        upnote = QLabel("A new yt-dlp is downloaded in the background and applies the "
                        "next time Video Grabber starts. The app itself is never "
                        "installed without asking.")
        upnote.setWordWrap(True)
        upnote.setStyleSheet(f"color: {MUTED};")
        upl.addRow(upnote)
        # v5.5: ffmpeg and deno as updatable components
        self.comp_info = QLabel('Press "Check ffmpeg and deno" to see their versions.')
        self.comp_info.setWordWrap(True)
        self.comp_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        upl.addRow(self.comp_info)
        cbrow = QHBoxLayout()
        _chk = QPushButton("Check ffmpeg and deno")
        _chk.clicked.connect(self._components_refresh)
        cbrow.addWidget(_chk)
        self.comp_btns = {}
        for _cname in ("ffmpeg", "deno"):
            _b = QPushButton(f"Update {_cname}")
            _b.setEnabled(False)
            _b.clicked.connect(lambda _=False, n=_cname: self._components_update(n))
            self.comp_btns[_cname] = _b
            cbrow.addWidget(_b)
        cbrow.addStretch(1)
        upl.addRow(cbrow)
        compnote = QLabel("ffmpeg and deno are downloaded, checked, and swapped in the next "
                          "time Video Grabber starts. Python is not updated here: the app "
                          "bundles its own interpreter, so it only changes with a full app update.")
        compnote.setWordWrap(True)
        compnote.setStyleSheet(f"color: {MUTED};")
        upl.addRow(compnote)
        tabs.addTab(up, "Updates")

        # ---- File Types ----
        ft = QWidget()
        ftl = QFormLayout(ft)
        ftl.addRow(QLabel("Auto-capture these extensions (uncheck to disable)"))
        self.ft_checks = {}
        disabled = s.get("file_types_overrides", {})
        # v5.5: a multi-column grid; a checked box means "capture this type".
        # The stored value is unchanged: an override of False disables a type.
        _ft_host = QWidget()
        _ft_grid = QGridLayout(_ft_host)
        _ft_grid.setContentsMargins(0, 0, 0, 0)
        _ft_grid.setHorizontalSpacing(18)
        _ft_grid.setVerticalSpacing(6)
        for _n, ext in enumerate(
                ["mp4", "mkv", "webm", "mov", "avi", "flv", "ts", "m4v",
                 "mp3", "wav", "flac", "m4a", "aac", "opus", "ogg",
                 "webp", "svg",
                 "zip", "rar", "7z", "tar", "gz", "tar.gz", "iso",
                 "pdf", "doc", "docx", "ppt", "pptx", "txt", "epub", "mobi",
                 "exe", "msi", "dmg"]):
            cb = QCheckBox(f".{ext}")
            cb.setChecked(disabled.get(ext) is not False)
            self.ft_checks[ext] = cb
            _ft_grid.addWidget(cb, _n // 4, _n % 4)
        ftl.addRow(_ft_host)
        tabs.addTab(ft, "File Types")

        # ---- Engines (Session 9 wires this up) ----
        en = QWidget()
        enl = QVBoxLayout(en)
        self.engine_checks = {}
        disabled = set(s.get("engines_disabled") or [])
        for name in ("yt-dlp", "streamlink"):
            cb = QCheckBox(f"Enable {name}")
            cb.setChecked(name not in disabled)
            self.engine_checks[name] = cb
            enl.addWidget(cb)
        enl.addWidget(QLabel("Engine priority (drag to reorder):"))
        self.engine_priority = QListWidget()
        self.engine_priority.setDragDropMode(QAbstractItemView.InternalMove)
        order = [n for n in (s.get("preferred_engines") or [])
                 if n in ("yt-dlp", "streamlink")]
        for n in ("yt-dlp", "streamlink"):
            if n not in order:
                order.append(n)
        for n in order:
            self.engine_priority.addItem(n)
        enl.addWidget(self.engine_priority)
        btn_row = QHBoxLayout()
        chk_btn = QPushButton("Check for update")
        chk_btn.clicked.connect(self._check_engine_updates)
        btn_row.addWidget(chk_btn)
        btn_row.addStretch(1)
        enl.addLayout(btn_row)
        note = QLabel("Versions are checked on demand; updates are never "
                      "installed automatically.")
        note.setStyleSheet(f"color: {MUTED};")
        enl.addWidget(note)
        enl.addStretch(1)
        tabs.addTab(en, "Engines")

        # ---- Rules: per-site category + engine ----
        ru = QWidget()
        rul = QVBoxLayout(ru)
        # v5.5: say what rules are for
        _rsub = QLabel("Override engine or category per domain. Add a rule, then pick a category "
                       "and an engine for that site; \"auto\" lets the app decide.")
        _rsub.setWordWrap(True)
        _rsub.setStyleSheet(f"color: {MUTED};")
        rul.addWidget(_rsub)
        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["Domain", "Category", "Engine"])
        self.rules_table.horizontalHeader().setStretchLastSection(True)
        rules = s.get("rules", []) or []
        engines = s.get("per_site_engine", {}) or {}
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            self._add_rule_row(rule.get("domain", ""),
                               rule.get("category", "Other"),
                               engines.get(rule.get("domain", ""), "auto"))
        if not self.rules_table.rowCount():
            self._add_rule_example()
        rul.addWidget(self.rules_table)
        btns = QHBoxLayout()
        add_btn = QPushButton("Add rule")
        add_btn.clicked.connect(lambda: self._add_rule_row("", "Other", "auto"))
        del_btn = QPushButton("Remove selected")
        del_btn.clicked.connect(self._remove_rule_row)
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        rul.addLayout(btns)
        tabs.addTab(ru, "Rules")

        # ---- Pairing ----
        pr = QWidget()
        prl = QFormLayout(pr)
        self.token_edit = QLineEdit(s.get("api_token", ""))
        self.token_edit.setReadOnly(True)
        prl.addRow("Pairing token", self.token_edit)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self._copy_token)
        prl.addRow(copy_btn)
        tabs.addTab(pr, "Pairing")

        tabs.build_index()
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copy_token(self):
        QApplication.clipboard().setText(self.token_edit.text())

    # --- Updates page -----------------------------------------------------
    @staticmethod
    def _fmt_update_status(d):
        yt = (d or {}).get("ytdlp") or {}
        app = (d or {}).get("app") or {}
        lines = [f"yt-dlp: running {yt.get('active') or 'unknown'}"
                 f" (bundled {yt.get('bundled') or '?'}"
                 + (f", override {yt['override']}" if yt.get("override") else "") + ")"]
        if yt.get("staged"):
            lines.append(f"yt-dlp {yt['staged']} is downloaded and applies on next start.")
        elif yt.get("latest"):
            lines.append(f"Latest yt-dlp on PyPI: {yt['latest']}")
        if yt.get("error"):
            lines.append(f"yt-dlp update error: {yt['error']}")
        lines.append(f"App update available: {app['available']}" if app.get("available")
                     else f"App: v{DISPLAY_VERSION} (no update found)")
        if app.get("error"):
            lines.append(f"App update check error: {app['error']}")
        lines.append(f"Checked at {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _set_update_busy(self, busy):
        self.update_busy.setVisible(busy)
        for b in self._update_btns:
            b.setEnabled(not busy)

    def _run_update_job(self, fn, on_done=None):
        prev = getattr(self, "_update_worker", None)
        if prev is not None and prev.isRunning():
            return      # one request at a time: a second click can't replace a running worker
        self._set_update_busy(True)
        w = _ApiFetchWorker(fn, self)

        def ok(d):
            self._set_update_busy(False)
            self.update_info.setStyleSheet("")
            try:
                self.update_info.setText(on_done(d) if on_done else self._fmt_update_status(d))
            except Exception as e:      # never leave the label stuck on "Checking..."
                self.update_info.setStyleSheet(f"color: {DANGER};")
                self.update_info.setText(f"Couldn't read the update status: {e}")

        def bad(msg):
            self._set_update_busy(False)
            self.update_info.setStyleSheet(f"color: {DANGER};")
            self.update_info.setText("Couldn't reach the app backend" + (f" ({msg})." if msg else "."))

        w.result.connect(ok, Qt.QueuedConnection)
        w.error.connect(bad, Qt.QueuedConnection)
        self._update_worker = w
        w.start()

    def _update_refresh(self):
        self.update_info.setStyleSheet("")
        self.update_info.setText("Checking\u2026")
        self._run_update_job(self.api.update_status)

    def _update_ytdlp_now(self):
        self.update_info.setText("Checking PyPI for a newer yt-dlp\u2026 press Refresh in a few seconds.")
        self._run_update_job(self.api.ytdlp_update, lambda d: "Started. Press Refresh in a few seconds to see the result.")

    def _update_ytdlp_rollback(self):
        if QMessageBox.question(self, "Roll back yt-dlp",
                                "Go back to the yt-dlp version that shipped with the app? "
                                "This applies the next time Video Grabber starts.",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._run_update_job(self.api.ytdlp_rollback, lambda d: (
            "Override removed. The bundled yt-dlp is used after restart."
            if (d or {}).get("removed") else "Nothing to roll back; already on the bundled version."))

    # --- completion sound (v5.5) ------------------------------------------
    def _choose_sound(self):
        start = self.sound_path.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose a completion sound", start,
                                              "Sound files (*.wav *.mp3)")
        if path:
            self.sound_path.setText(path)
            self.sound_preset.setCurrentIndex(self.sound_preset.findData("custom"))

    # --- sound library (v5.6) ---------------------------------------------
    def _sound_library_dir(self):
        """Where Save to Library / Save as Default put sounds: <home>/sounds."""
        try:
            import sounds as _sounds
            return Path(_sounds._home()) / "sounds"
        except Exception:
            return Path(HOME) / "sounds"

    def _refresh_sound_library(self, select_path=None):
        """Rebuild the "Library: ..." entries of the Completion sound dropdown
        from <home>/sounds/*.wav. Keeps the current choice, or selects
        `select_path` when it is a library file. Safe to call at any time, so a
        sound saved from the generator shows up without restarting."""
        import sounds as _sounds
        combo = self.sound_preset
        cur = select_path or combo.currentData(Qt.UserRole + 1) or ""
        on_lib = bool(combo.currentData(Qt.UserRole + 1))
        custom_idx = combo.findData(_sounds.CUSTOM)
        combo.blockSignals(True)
        try:
            for k in range(combo.count() - 1, custom_idx, -1):
                combo.removeItem(k)
            try:
                files = sorted(self._sound_library_dir().glob("*.wav"),
                               key=lambda p: p.name.lower())
            except OSError:
                files = []
            target = -1
            for p in files:
                combo.addItem(f"Library: {p.stem}", userData=_sounds.CUSTOM)
                combo.setItemData(combo.count() - 1, str(p), Qt.UserRole + 1)
                combo.setItemData(combo.count() - 1, p.name, Qt.ToolTipRole)
                if cur and Path(cur) == p:
                    target = combo.count() - 1
            if target >= 0:
                combo.setCurrentIndex(target)
            elif on_lib:                       # the selected library file is gone
                combo.setCurrentIndex(custom_idx)
        finally:
            combo.blockSignals(False)

    def _on_sound_preset_changed(self, _index=None):
        """Picking a library sound points the custom path at that file; the
        combo's data stays "custom", which is what Save already stores."""
        path = self.sound_preset.currentData(Qt.UserRole + 1)
        if path:
            self.sound_path.setText(path)

    def _open_sound_generator(self):
        open_sound_options(self, on_saved=self._on_generated_sound,
                           on_library_changed=self._refresh_sound_library)

    def _on_generated_sound(self, path, name):
        """Save as Default in the generator: point the completion sound at the
        new library file right away, so the choice holds even if Settings is
        closed without pressing Save, and keep the widgets in step with it."""
        self.sound_path.setText(path)
        self._refresh_sound_library(select_path=path)   # selects "Library: <name>"
        turned_on = not self.play_sound_on_complete.isChecked()
        if turned_on:
            self.play_sound_on_complete.setChecked(True)   # a default you never hear is no default
        if self.api is not None:
            try:
                self.api.save_setting("completion_sound", "custom")
                self.api.save_setting("completion_sound_path", path)
                if turned_on:
                    self.api.save_setting("play_sound_on_complete", True)
            except Exception:
                pass    # Save in Settings writes the same values

    def _test_sound(self):
        import sounds as _sounds
        ok = _sounds.play_completion_sound(self.sound_preset.currentData(),
                                           self.sound_path.text().strip(), why="test")
        if not ok:
            QMessageBox.information(
                self, "Test Sound",
                "That sound couldn't be played, so the Windows beep was used instead "
                "(it is silent if your Windows sound scheme is set to No Sounds). "
                "The log says why.")

    # --- ffmpeg / deno updates (v5.5) -------------------------------------
    @staticmethod
    def _fmt_components(d):
        d = d or {}
        if d.get("_error"):
            return f"Couldn't check ffmpeg and deno: {d['_error']}"
        if d.get("frozen_only"):
            return "ffmpeg and deno can only be updated from the installed app, not from a source run."
        lines = []
        for name in ("ffmpeg", "deno"):
            c = d.get(name) or {}
            line = f"{name}: running {c.get('installed') or 'not found'}"
            if c.get("error"):
                line += f" (latest check failed: {c['error']})"
            elif c.get("pending"):
                line += f". {c['pending']} is downloaded and applies on next start."
            elif c.get("update_available"):
                line += f". Latest: {c['latest']} (update available)"
            elif c.get("latest"):
                line += f". Latest: {c['latest']} (up to date)"
            lines.append(line)
        return "\n".join(lines)

    def _components_run(self, fn, on_result):
        def run():
            try:
                return fn()
            except Exception as e:
                return {"_error": str(e), "ok": False, "message": str(e)}
        w = _ApiFetchWorker(run)          # no parent: an update can outlive this dialog
        _COMPONENT_WORKERS.add(w)
        w.result.connect(on_result, Qt.QueuedConnection)
        w.finished.connect(lambda w=w: _COMPONENT_WORKERS.discard(w))
        w.start()

    def _components_refresh(self):
        import component_updater
        self.comp_info.setText("Checking\u2026")
        for b in self.comp_btns.values():
            b.setEnabled(False)
        self._components_run(component_updater.status, self._on_components_status)

    def _on_components_status(self, d):
        self.comp_info.setText(self._fmt_components(d))
        for name, b in self.comp_btns.items():
            b.setEnabled(bool(((d or {}).get(name) or {}).get("update_available")))

    def _components_update(self, name):
        import component_updater
        self.comp_info.setText(f"Downloading and checking {name}\u2026 this can take a minute. "
                               "You can keep using the app.")
        for b in self.comp_btns.values():
            b.setEnabled(False)
        self._components_run(lambda: component_updater.update(name), self._on_components_updated)

    def _on_components_updated(self, d):
        self.comp_info.setText((d or {}).get("message") or "Update finished.")

    ENGINES = ("auto", "yt-dlp", "streamlink")

    def _check_engine_updates(self):
        """Manual version check: reports via dialog + log; never auto-updates."""
        import json as _json
        import shutil as _shutil
        import subprocess as _sp
        from settings import log
        from ytdlp_update import version_key
        lines = []
        for name in ("yt-dlp", "streamlink"):
            try:
                if name == "yt-dlp":
                    import yt_dlp
                    local = yt_dlp.version.__version__
                else:
                    if _shutil.which("streamlink") is None:
                        lines.append("streamlink: not found on PATH (downloads will use another engine)")
                        continue
                    r = _sp.run(["streamlink", "--version"],
                                capture_output=True, text=True, timeout=15)
                    local = (r.stdout or r.stderr).strip().splitlines()[0]
                latest = None
                try:
                    p = requests.get(f"https://pypi.org/pypi/{name}/json",
                                     timeout=5).json()
                    latest = p.get("info", {}).get("version")
                except Exception:
                    pass
                if latest and version_key(latest) != version_key(local):
                    lines.append(f"{name}: local {local}, latest {latest} — update available")
                else:
                    lines.append(f"{name}: {local}" + ("" if latest else " (latest unknown)"))
            except Exception as e:
                lines.append(f"{name}: check failed — {e}")
        log("engine update check: " + "; ".join(lines))
        QMessageBox.information(self, "Engine versions", "\n".join(lines))

    # v5.5: a greyed example row so an empty table still shows what a rule
    # looks like. It carries a marker, is never saved, and goes away as soon
    # as a real rule is added.
    def _add_rule_example(self):
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        for col, text in enumerate(("example.com", "Video", "yt-dlp")):
            it = QTableWidgetItem(text)
            it.setFlags(Qt.NoItemFlags)
            it.setForeground(QColor(MUTED))
            f = it.font(); f.setItalic(True); it.setFont(f)
            if col == 0:
                it.setData(Qt.UserRole, "example")
                it.setToolTip("Example only. Press Add rule to make your own.")
            self.rules_table.setItem(row, col, it)

    def _drop_rule_example(self):
        for r in range(self.rules_table.rowCount() - 1, -1, -1):
            it = self.rules_table.item(r, 0)
            if it is not None and it.data(Qt.UserRole) == "example":
                self.rules_table.removeRow(r)

    def _add_rule_row(self, domain, category, engine):
        self._drop_rule_example()
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        self.rules_table.setItem(row, 0, QTableWidgetItem(domain))
        cat = QComboBox(); cat.addItems(CATEGORIES)
        cat.setCurrentText(category if category in CATEGORIES else "Other")
        self.rules_table.setCellWidget(row, 1, cat)
        eng = QComboBox(); eng.addItems(self.ENGINES)
        eng.setCurrentText(engine if engine in self.ENGINES else "auto")
        self.rules_table.setCellWidget(row, 2, eng)

    def _remove_rule_row(self):
        rows = sorted({i.row() for i in self.rules_table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.rules_table.removeRow(r)
        if not self.rules_table.rowCount():
            self._add_rule_example()

    def _preview_app_font(self, *_):
        """Batch 3: apply the App Font dropdown's family to the running app
        without saving it, so the user sees the real result before deciding.
        Mirrors reapply_theme() but with the dropdown's value in place of
        the saved one."""
        try:
            fam = self.app_font.currentData() or ""
            st = dict(load_settings())
            st["app_font"] = fam
            from gui_style import build_qss
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(build_qss(st))
            self._font_preview_on = (fam != self._saved_app_font)
        except Exception:
            pass

    def _update_font_notepad(self):
        """Show the test notepad (and the Aa label) in the font and size the
        dropdown and slider currently hold, before anything is saved."""
        pad = getattr(self, "font_notepad", None)
        if pad is None:
            return
        fam = (self.app_font.currentData() or "").replace('"', "").replace("\\", "")
        size = max(9, int(10 * (self.text_scale.value() / 100)))
        css = f"font-size: {size}pt;"
        if fam:
            css = f'font-family: "{fam}"; ' + css
        pad.setStyleSheet(css)
        try:
            self.text_preview.setStyleSheet(css)
        except Exception:
            pass

    def _update_text_preview(self, val):
        """Batch 4: scale the "Aa Preview" label with the Text size slider."""
        self.text_preview.setStyleSheet(
            f"font-size: {max(9, int(10 * (val / 100)))}pt;")

    def done(self, r):
        # Batch 3: closing this dialog without saving (Cancel, Esc, the
        # window X — all of them end here as Rejected) after a font preview
        # puts the saved font back. Saving ends as Accepted, and _save_inner
        # has already re-applied the theme with the new font, so only a
        # rejection reverts.
        if r == QDialog.Rejected and getattr(self, "_font_preview_on", False):
            self._font_preview_on = False
            try:
                reapply_theme()
            except Exception:
                pass
        super().done(r)

    def _open_theme_engine(self):
        from gui_theme_engine import ThemeEngineDialog
        dlg = ThemeEngineDialog(load_settings(), self)
        dlg.themeChanged.connect(self._theme_engine_live)
        dlg.themeSaved.connect(lambda _path: self.theme_page._scan_themes())   # new theme shows at once
        dlg.themeApplied.connect(self._theme_engine_apply)
        try:
            dlg.exec()
        finally:
            reapply_theme()                    # back to the saved theme, however the engine closed
            self.theme_page._scan_themes()     # so a new custom.json shows in the Theme tab

    def _theme_engine_apply(self, state):
        """Save = Apply: the engine's theme becomes the saved theme. Writes the
        three theme settings, then brings the Theme tab in step so a later
        Settings Save cannot write the old theme back over it."""
        pg = self.theme_page
        preset = state.get("theme_preset")
        accent = state.get("accent")
        toks = {k: v for k, v in (state.get("theme_tokens") or {}).items() if v and k != "accent"}
        if self.api:
            old = (load_settings().get("theme_tokens") or {})
            # the server merges dicts key by key: "" clears a color that was dropped
            send = {k: toks.get(k, "") for k in set(old) | set(toks) if k != "accent"}
            self.api.save_setting("theme_preset", preset)
            self.api.save_setting("accent", accent)
            self.api.save_setting("theme_tokens", send)
        try:
            pg._preset, pg._accent, pg._ov = preset, accent, toks
            for k, tile in pg._tiles.items():
                tile.setChecked(k == preset)
            pg.fine_toggle.setChecked(bool(toks))
            pg._refresh()
        except Exception:
            pass
        reapply_theme()

    def _theme_engine_live(self, state):
        from gui_style import build_qss
        app = QApplication.instance()
        if app is None:
            return
        _sync_palette(state)                   # gui_qt's painted colors follow the live theme
        app.setStyleSheet(build_qss(state))

    def _save(self):
        try:
            self._save_inner()
        except Exception as e:
            QMessageBox.critical(self, "Settings error",
                                 f"Couldn't save settings:\n{e}")


    def _save_inner(self):
        proxy = self.proxy_url.text().strip()
        if proxy and not re.match(r"^(https?|socks4|socks5h?)://\S+$", proxy, re.I):
            raise ValueError("Proxy must look like http://host:port, https://host:port, "
                             "socks4://host:port, socks5://host:port or socks5h://host:port")
        if self.api:
            for key, widget in [
                ("force_on_top", self.force_on_top),
                ("close_to_tray", self.close_to_tray),
                ("watch_recording_folder", self.watch_recording_folder),
                ("clipboard_monitor", self.clipboard_monitor),
                ("auto_mp4", self.auto_mp4),
                ("defender_scan", self.defender_scan),
                ("auto_shutdown", self.auto_shutdown),
                ("auto_extract", self.auto_extract),
                ("open_folder_on_complete", self.open_folder_on_complete),
                ("play_sound_on_complete", self.play_sound_on_complete),
            ]:
                self.api.save_setting(key, widget.isChecked())
            self.api.save_setting("max_concurrent", int(self.max_concurrent.value()))
            self.api.save_setting("speed_limit_kbps",
                                  int(self.speed_limit_kbps.value()))
            self.api.save_setting("connection_timeout",
                                  int(self.connection_timeout.value()))
            self.api.save_setting("max_retries", int(self.max_retries.value()))
            self.api.save_setting("min_speed_kbps",
                                  int(self.min_speed_kbps.value()))
            self.api.save_setting("ytdlp_sleep_interval", int(self.yt_delay_min.value()))
            self.api.save_setting("ytdlp_max_sleep_interval", int(self.yt_delay_max.value()))
            self.api.save_setting("ytdlp_bot_cooldown_minutes", int(self.yt_cooldown.value()))
            self.api.save_setting("proxy_url", proxy)
            self.api.save_setting("proxy_scope", self.proxy_scope.currentData() or "youtube")
            self.api.save_setting("min_free_space_mb", int(self.min_free_space_mb.value()))
            self.api.save_setting("ytdlp_use_archive", self.ytdlp_use_archive.isChecked())
            self.api.save_setting("hide_old_errors_hours", int(self.hide_old_errors_hours.value()))
            self.api.save_setting("notify_queue_done", self.notify_queue_done.isChecked())
            self.api.save_setting("auto_update_check", self.auto_update_check.isChecked())
            self.api.save_setting("update_check_hours", int(self.update_check_hours.value()))
            self.api.save_setting("ytdlp_auto_update", self.ytdlp_auto_update.isChecked())
            self.api.save_setting("output_dir", self.output_dir.text().strip())
            self.api.save_setting("per_category_dirs",
                                  {cat: e.text().strip()
                                   for cat, e in self.percat_edits.items()})
            # Send every known extension explicitly so unchecked boxes
            # reliably re-enable a type (server merges dicts key-by-key).
            self.api.save_setting("file_types_overrides",
                                  {ext: cb.isChecked()    # v5.5: checked = capture
                                   for ext, cb in self.ft_checks.items()})
            # --- v5.5 ---
            self.api.save_setting("completion_sound", self.sound_preset.currentData() or "chime")
            self.api.save_setting("completion_sound_path", self.sound_path.text().strip())
            self.api.save_setting("blanc_mode", self.blanc_mode.isChecked())
            self.api.save_setting("inline_play_muted", self.inline_play_muted.isChecked())
            self.api.save_setting("thumb_click_mode", self.thumb_click_mode.currentData() or "inline")
            self.api.save_setting("text_scale", int(self.text_scale.value() or 100))
            self.api.save_setting("app_font", self.app_font.currentData() or "")
            # --- v4 (Session 6) ---
            tv = self.theme_page.values()
            self.api.save_setting("accent", tv["accent"])
            self.api.save_setting("theme_preset", tv["theme_preset"])
            self.api.save_setting("theme_tokens", tv["theme_tokens"])
            self.api.save_setting("shell_enabled", tv["shell_enabled"])
            self.api.save_setting("shell_color", tv["shell_color"])
            self.api.save_setting("shell_backlight", tv["shell_backlight"])
            self.api.save_setting("shell_colors", tv["shell_colors"])
            self.api.save_setting("frame_cream", tv["frame_cream"])
            self.api.save_setting("theme_detail_mode", tv.get("theme_detail_mode", "simple"))
            # rebuild the stylesheet and repaint so the theme lands live
            reapply_theme()
            self.api.save_setting("animations_enabled",
                                  self.animations_enabled.isChecked())
            self.api.save_setting("prefer_source_quality",
                                  self.prefer_source_quality.isChecked())
            langs = [x.strip() for x in
                     self.subtitle_languages.text().split(",") if x.strip()]
            self.api.save_setting("subtitle_languages", langs or ["en"])
            rules, engines = [], {}
            for row in range(self.rules_table.rowCount()):
                dom = self.rules_table.item(row, 0)
                if dom is not None and dom.data(Qt.UserRole) == "example":
                    continue      # the greyed example row is not a rule
                dom = dom.text().strip() if dom else ""
                if not dom:
                    continue
                cat = self.rules_table.cellWidget(row, 1).currentText()
                eng = self.rules_table.cellWidget(row, 2).currentText()
                rules.append({"domain": dom, "category": cat})
                if eng != "auto":
                    engines[dom] = eng
            self.api.save_setting("rules", rules)
            self.api.save_setting("per_site_engine", engines)
            self.api.save_setting(
                "engines_disabled",
                [n for n, cb in self.engine_checks.items() if not cb.isChecked()])
            self.api.save_setting(
                "preferred_engines",
                [self.engine_priority.item(i).text()
                 for i in range(self.engine_priority.count())])
        self.accept()


class BandwidthProfilesDialog(QDialog):
    def __init__(self, parent=None, api=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Bandwidth profiles")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("Enable time-of-day bandwidth profiles")
        settings = load_settings()
        self.enabled.setChecked(bool(settings.get("bandwidth_profiles_enabled")))
        layout.addWidget(self.enabled)
        hint = QLabel(
            "Each profile is a time window (HH:MM–HH:MM) and a speed limit in "
            "KB/s (0 = unlimited). Overnight windows wrap midnight."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        layout.addWidget(hint)
        self.list = QListWidget()
        layout.addWidget(self.list)
        for p in settings.get("bandwidth_profiles") or []:
            self._add_item(p)
        form = QFormLayout()
        self.name = QLineEdit()
        self.start = QLineEdit("22:00")
        self.end = QLineEdit("08:00")
        self.limit = QSpinBox()
        self.limit.setRange(0, 1_000_000)
        self.limit.setSuffix(" KB/s")
        self.limit.setSpecialValueText("unlimited")
        form.addRow("Name", self.name)
        form.addRow("Start", self.start)
        form.addRow("End", self.end)
        form.addRow("Limit", self.limit)
        layout.addLayout(form)
        row = QHBoxLayout()
        add_btn = QPushButton("Add / Update")
        add_btn.clicked.connect(self._add_or_update)
        rem_btn = QPushButton("Remove selected")
        rem_btn.clicked.connect(self._remove)
        row.addWidget(add_btn)
        row.addWidget(rem_btn)
        row.addStretch()
        layout.addLayout(row)
        self.list.currentItemChanged.connect(self._load_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fmt(self, p):
        lim = int(p.get("speed_limit_kbps", 0) or 0)
        lim_s = "unlimited" if lim <= 0 else f"{lim} KB/s"
        name = p.get("name") or "Profile"
        return f"{name}  {p.get('start', '?')}–{p.get('end', '?')}  →  {lim_s}"

    def _add_item(self, p):
        item = QListWidgetItem(self._fmt(p))
        item.setData(Qt.UserRole, dict(p))
        self.list.addItem(item)

    def _load_selected(self, cur, _prev):
        if not cur:
            return
        p = cur.data(Qt.UserRole) or {}
        self.name.setText(p.get("name", ""))
        self.start.setText(p.get("start", "22:00"))
        self.end.setText(p.get("end", "08:00"))
        self.limit.setValue(int(p.get("speed_limit_kbps", 0) or 0))

    def _parse_hhmm(self, text):
        parts = text.strip().split(":")
        if len(parts) != 2:
            raise ValueError("Use HH:MM")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("Invalid time")
        return f"{hh:02d}:{mm:02d}"

    def _add_or_update(self):
        try:
            start = self._parse_hhmm(self.start.text())
            end = self._parse_hhmm(self.end.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid time", str(e))
            return
        p = {
            "name": self.name.text().strip() or "Profile",
            "start": start, "end": end,
            "speed_limit_kbps": int(self.limit.value()),
        }
        cur = self.list.currentItem()
        if cur:
            cur.setData(Qt.UserRole, p)
            cur.setText(self._fmt(p))
        else:
            self._add_item(p)

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)

    def _save(self):
        profiles = []
        for i in range(self.list.count()):
            profiles.append(self.list.item(i).data(Qt.UserRole))
        if self.api:
            self.api.save_setting("bandwidth_profiles", profiles)
            self.api.save_setting("bandwidth_profiles_enabled", self.enabled.isChecked())
        self.accept()


class Sidebar(QFrame):
    category_selected = Signal(str)
    tool_clicked = Signal(str)   # "settings" | "help" | "about"
    collapsed_changed = Signal(bool)   # v5.5: the user pressed the collapse button

    WIDTH_FULL, WIDTH_COLLAPSED = 200, 56
    TOOL_ICONS = {"settings": "\u2699", "help": "?", "about": "i"}

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(self.WIDTH_FULL)
        self._collapsed = False
        self._counts = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(4)
        self._layout = layout
        head = QHBoxLayout()
        head.setSpacing(4)
        brand = QLabel("↓  VIDEO GRABBER")
        brand.setObjectName("Brand")
        head.addWidget(brand, 1)
        self._brand = brand
        self.collapse_btn = QToolButton()
        self.collapse_btn.setObjectName("SidebarTool")
        self.collapse_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.collapse_btn.clicked.connect(
            lambda _=False: self.set_collapsed(not self._collapsed, user=True))
        head.addWidget(self.collapse_btn)
        layout.addLayout(head)
        layout.addSpacing(14)
        self.buttons = {}
        for name in ["All", "Active", "Queue", "Finished", "Unfinished", "Failed"]:
            self._add_nav(layout, name)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("SidebarSeparator")
        layout.addWidget(sep)
        label = QLabel("CATEGORIES")
        label.setObjectName("SectionLabel")
        layout.addWidget(label)
        self._cat_label = label
        for name in CATEGORIES:
            self._add_nav(layout, name)
        layout.addStretch()
        tools_label = QLabel("TOOLS")
        tools_label.setObjectName("SectionLabel")
        layout.addWidget(tools_label)
        self._tools_label = tools_label
        tools = QHBoxLayout()
        tools.setSpacing(2)
        self._tools = tools
        self._tool_btns = []     # (button, full text, collapsed glyph)
        # v5.5: Settings is no longer here; it lives on the toolbar cog (and in
        # File > Settings...). Help and About stay.
        for key, text, tip in (("help", "Help", "Tips and shortcuts"),
                               ("about", "About", "About Video Grabber")):
            b = QToolButton()
            b.setObjectName("SidebarTool")
            b.setText(text)
            b.setToolTip(tip)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, k=key: self.tool_clicked.emit(k))
            tools.addWidget(b)
            self._tool_btns.append((b, text, self.TOOL_ICONS.get(key, text[:1])))
        layout.addLayout(tools)
        self.set_collapsed(False)

    def _add_nav(self, layout, name):
        btn = QToolButton()
        btn.setText(name)
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        btn.setProperty("nav", True)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setMinimumHeight(32)
        btn.clicked.connect(lambda _=False, n=name: self.category_selected.emit(n))
        layout.addWidget(btn)
        self.buttons[name] = btn

    def _refresh_btn(self, name):
        """Text, tooltip and style of one nav button for the current mode.
        Called every refresh tick, so it only touches what changed."""
        btn = self.buttons[name]
        n = self._counts.get(name, 0)
        label = f"{name}  ({n})" if n else name
        ic = _qicon(_SB_ICONS.get(name), SOFT) if self._collapsed else None
        if self._collapsed:
            # If icon is missing, use first letter as fallback
            text = "" if ic is not None else name[:1]
            tip = label
            css = "QToolButton { text-align: center; padding: 0; }"
        else:
            text, tip, css = label, "", ""
        mode = f"icon|{SOFT}" if ic is not None else "text"
        if btn.property("_vg_mode") != mode:
            btn.setProperty("_vg_mode", mode)
            if ic is not None:
                btn.setIcon(ic)
                btn.setIconSize(QSize(18, 18))
                btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            else:
                btn.setIcon(QIcon())
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        if btn.text() != text:
            btn.setText(text)
        if btn.toolTip() != tip:
            btn.setToolTip(tip)
        if btn.styleSheet() != css:
            btn.setStyleSheet(css)

    def set_collapsed(self, flag, user=False):
        """Icons-only (True) or full width (False). `user` marks a press of
        the collapse button, which is what gets saved."""
        flag = bool(flag)
        changed = flag != self._collapsed
        self._collapsed = flag
        self.setFixedWidth(self.WIDTH_COLLAPSED if flag else self.WIDTH_FULL)
        side = 6 if flag else 14
        self._layout.setContentsMargins(side, 16, side, 14)
        self._brand.setVisible(not flag)
        self._cat_label.setVisible(not flag)
        self._tools_label.setVisible(not flag)
        self.collapse_btn.setText("\u00bb" if flag else "\u00ab")
        self.collapse_btn.setToolTip("Expand the sidebar" if flag else "Collapse the sidebar to icons")
        self._tools.setDirection(QBoxLayout.TopToBottom if flag else QBoxLayout.LeftToRight)
        for b, text, icon in self._tool_btns:
            b.setText(icon if flag else text)
        for name in self.buttons:
            self._refresh_btn(name)
        if changed and user:
            self.collapsed_changed.emit(flag)

    def set_counts(self, counts):
        """Show a count next to each entry that has any rows."""
        self._counts = counts
        for name in self.buttons:
            self._refresh_btn(name)


def _fade_widget(widget, enabled, duration=150):
    """Fade `widget` in over `duration` ms, OutCubic (Session 12.1). No-op
    when animations are disabled; the graphics effect is removed on finish
    so rendering and hit-testing return to normal."""
    if not enabled:
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QAbstractAnimation.DeleteWhenStopped)


_DIALOG_FADE_DONE = False


def _fade_dialog(dialog, enabled, duration=120):
    """Opacity fade for dialogs (Session 12.3). The dialog's own event loop
    (exec) keeps the animation running; no-op when animations are off.
    Dialogs are built fresh on every open, so a per-instance flag would never
    skip anything: only the first dialog of the session fades, the rest
    appear instantly."""
    global _DIALOG_FADE_DONE
    if not enabled or _DIALOG_FADE_DONE:
        return
    _DIALOG_FADE_DONE = True
    dialog.setWindowOpacity(0.0)
    anim = QPropertyAnimation(dialog, b"windowOpacity", dialog)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


class _ApiFetchWorker(QThread):
    """Runs a blocking ApiClient call off the GUI thread (Session 11.1).
    Queued-signal delivery applies the result back in the GUI thread; the
    window waits for in-flight workers in closeEvent so the app can exit
    cleanly."""
    result = Signal(object)
    failed = Signal()
    error = Signal(str)      # v5.5: why it failed (emitted just before `failed`)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.result.emit(self._fn())
        except Exception as e:
            self.error.emit(str(e)[:200])
            self.failed.emit()


# ---------------------------------------------------------------------------
# v5.5 Batch 3: Help, Pair Extension and What's New dialogs
# ---------------------------------------------------------------------------
def _md_to_html(md):
    """Small markdown subset (headers, bullets, bold, code, links) for the
    What's New dialog. Everything is HTML-escaped first."""
    import html as _html

    def inline(t):
        t = _html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        t = re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
        return t

    out, in_ul = [], False
    for raw in str(md or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*[-*+]\s+(.*)", line)
        if m:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if not line.strip():
            continue
        h = re.match(r"^(#{1,6})\s*(.*)", line)
        if h:
            out.append(f"<h{3 if len(h.group(1)) <= 2 else 4}>{inline(h.group(2))}</h{3 if len(h.group(1)) <= 2 else 4}>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "".join(out)


def _release_header(p):
    """'rev 59 — v5.5.0' style header for a /changelog payload."""
    tag = str(p.get("tag") or "")
    name = str(p.get("name") or "")
    rev = re.fullmatch(r"[vV]?(\d+)", tag.strip())
    ver = re.search(r"\bv?(\d+\.\d+\.\d+)\b", name + " " + tag)
    if rev and ver:
        return f"rev {rev.group(1)} \u2014 v{ver.group(1)}"
    if rev:
        return f"rev {rev.group(1)}" + (f" \u2014 {name}" if name and name != tag else "")
    return name or tag or "Latest release"


class WhatsNewDialog(QDialog):
    """Latest release notes from the app's /changelog route, formatted."""

    def __init__(self, parent, api):
        super().__init__(parent)
        self.setWindowTitle("What's New")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        self.status = QLabel("Loading release notes\u2026")
        lay.addWidget(self.status)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setFixedHeight(6)
        lay.addWidget(self.busy)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        # Parentless worker kept in the registry: closing the dialog while the
        # request is still running must not destroy a running QThread.
        w = _ApiFetchWorker(api.changelog)
        _COMPONENT_WORKERS.add(w)
        w.result.connect(self._show, Qt.QueuedConnection)
        w.error.connect(self._fail, Qt.QueuedConnection)
        w.finished.connect(lambda w=w: _COMPONENT_WORKERS.discard(w))
        w.start()

    def _show(self, payload):
        self.busy.setVisible(False)
        payload = payload or {}
        if not payload.get("ok"):
            self._fail(str(payload.get("error") or "no data"))
            return
        head = _release_header(payload)
        date = str(payload.get("published_at") or "")[:10]
        self.status.setText(head + (f"   ({date})" if date else ""))
        body = _md_to_html(payload.get("body"))
        self.view.setHtml(f"<h3>{head}</h3>" + (body or "<p>This release has no notes.</p>"))

    def _fail(self, msg):
        self.busy.setVisible(False)
        self.status.setStyleSheet(f"color: {DANGER};")
        self.status.setText(f"Couldn't load the release notes ({msg}). Check your connection and try again.")


class PairDialog(QDialog):
    """Step-by-step pairing for the browser extension. Same token as
    Settings > Pairing, which stays as the secondary entry."""

    def __init__(self, parent, token):
        super().__init__(parent)
        self.setWindowTitle("Pair Extension")
        self.resize(480, 360)
        lay = QVBoxLayout(self)
        intro = QLabel(
            "<h3 style='margin-bottom:2px;'>Pair the browser extension</h3>"
            "<p>Pairing is usually automatic: with this app running, open the extension "
            "popup and it should show as connected.</p>"
            "<p>If it can't reach the app, pair it by hand:</p>"
            "<ol>"
            "<li>Copy the token below.</li>"
            "<li>In your browser, right-click the Video Grabber icon and choose "
            "<b>Options</b>.</li>"
            "<li>Paste the token, press <b>Save</b>, then <b>Test connection</b>.</li>"
            "</ol>")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.token_edit = QLineEdit(token or "")
        self.token_edit.setReadOnly(True)
        row.addWidget(self.token_edit, 1)
        copy = QPushButton("Copy token")
        copy.setObjectName("primary")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        lay.addLayout(row)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color: {SUCCESS};")
        lay.addWidget(self.note)
        lay.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _copy(self):
        QApplication.clipboard().setText(self.token_edit.text())
        self.note.setText("Token copied to the clipboard.")


def _help_pages():
    """(section title, HTML) for the Help dialog."""
    dim = f"color:{MUTED};"
    return [
        ("Capabilities", f"""
<h3>What Video Grabber does</h3>
<p>It downloads videos, audio, images, documents, archives and other files, from the browser
extension or from links you add with the + button.</p>
<ul>
<li><b>Three engines.</b> yt-dlp handles most sites, including YouTube and HLS/DASH streams.
Streamlink handles live streams such as Twitch. A built-in segmented downloader fetches direct
files in parallel pieces. The app picks one per link, and Settings &gt; Rules can override it per site.</li>
<li><b>Queue.</b> Downloads run in order. The toolbar pauses or resumes the whole queue,
Alt+Up / Alt+Down reorder queued items, and the Queue entry in the sidebar shows only what is
running or waiting. Tools &gt; Downloads &gt; Bandwidth profiles manages bandwidth profiles.</li>
<li><b>Categories.</b> Files are sorted into Video, Music, Images, Compressed, Documents,
Programs, Fonts and Other. Settings &gt; Downloads &gt; Destinations sets the folders.</li>
<li><b>After a download.</b> Right-click a row to open it, move it to another category, convert
it (mp4, mkv, mp3, wav, m4a, flac, opus), reorder it or delete it. Settings &gt; Post-Download
covers what happens when a download finishes; Settings &gt; Sounds covers the completion sound.</li>
<li><b>Media Toolkit.</b> Compress, trim, or turn a clip into a GIF.</li>
<li><b>Look and feel.</b> Themes, text size, Blanc minimal mode, a collapsible sidebar and an
optional handheld shell.</li>
<li><b>Updates.</b> yt-dlp updates itself in the background. The app asks before it installs an
update. ffmpeg and deno update from Settings &gt; Updates.</li>
</ul>
<p>Basics: click a row to see its details below, double-click a finished file to open it, and
drag finished files out into other apps.</p>"""),
        ("FAQ", f"""
<h3>Common questions</h3>
<p><b>Why did my download fail?</b><br>Click the failed row: the details pane shows the reason.
Usual causes are an expired link (send it again from the page), a site that needs your login
(send it through the extension so your browser cookies go with it), or an engine that needs an
update. Try Tools &gt; Downloads &gt; Retry all failed, then Tools &gt; Updates &gt; Check for
updates. If a new yt-dlp broke a site that worked before, use Tools &gt; Updates &gt; Roll back
yt-dlp.</p>
<p><b>Why is a download "held"?</b><br>YouTube sometimes asks to confirm you're not a bot, and the
app waits before trying again. Switch network or VPN and press Resume to skip the wait.</p>
<p><b>Why is the pill not showing?</b><br>Open the extension popup, press the cog, and check that
<i>Show the pill on videos</i> is on and the site isn't in the hidden list. The pill sits on video
elements; if a page plays video another way, use the popup's list of detected media or
<b>Grab all</b>. Reload the page after changing a setting.</p>
<p><b>The extension says the app is unreachable.</b><br>Start Video Grabber. If it is running, use
Tools &gt; Pairing &gt; Pair Extension and follow the steps.</p>
<p><b>Where do my files go?</b><br>Settings &gt; Downloads &gt; Destinations: a default folder
plus an optional folder per category.</p>
<p><b>Why did a file land in Other?</b><br>The category follows the file type. Add a rule in
Settings &gt; Rules to send a site's downloads to a category.</p>
<p><b>Why no sound when a download finishes?</b><br>In Settings &gt; Sounds turn the
completion sound on, pick a sound and press Test Sound. If the test falls back to the Windows
beep and you hear nothing, your Windows sound scheme may be set to No Sounds.</p>
<p><b>I sent the same link twice.</b><br>A link that is already queued or downloading is not
added again.</p>"""),
        ("Troubleshooting", f"""
<h3>When something goes wrong</h3>
<p><b>Logs.</b> Tools &gt; Downloads &gt; Open logs folder. Settings, the job list and the logs live
in the VideoGrabber folder inside your Downloads folder. Attach the newest log when you report
a problem.</p>
<p><b>Repair and retry.</b></p>
<ul>
<li>Tools &gt; Downloads &gt; Retry all failed requeues every failed download.</li>
<li>Right-click a download to download it again from scratch.</li>
<li>Tools &gt; Updates &gt; Roll back yt-dlp returns to the yt-dlp that shipped with the app.</li>
<li>Recordings made by the extension are repaired with ffmpeg automatically.</li>
<li>Tools &gt; Pairing &gt; Pair Extension fixes an extension that lost its connection.</li>
<li>Tools &gt; Media &gt; Trim UI memory frees memory if the window feels sluggish.</li>
</ul>
<p><b>Updates.</b> Settings &gt; Updates shows the yt-dlp and app versions (press Refresh) and
checks ffmpeg and deno. Downloaded updates apply the next time the app starts.</p>
<p style="{dim}">Nothing here changes your downloaded files.</p>"""),
        ("Extension guide", f"""
<h3>The browser extension</h3>
<p><b>The pill.</b> A small button appears over videos. Click it to open the menu: pick a quality
(the app remembers your pick per site), extract audio, record, or choose <b>Start Queue</b>. On some
sites the pill sends the video in one click instead. Drag the pill to move it, double-click it to
reset its position, and right-click it to snap it to a corner, start the queue or hide it on the
current site. Hovering an image can show a small pill too.</p>
<p><b>The popup.</b> Click the toolbar icon. <b>Current Page</b> lists the media the extension
detected; <b>Other Pages</b> lists other tabs. Tick items and press <b>Download</b>. <b>Grab all</b>
scans the page for every media file and link. The <b>&hellip;</b> menu holds Merge, Copy, Toggle,
Sort, Filter, Clear and Search. The cog opens the extension settings (pill, sniffer,
notifications).</p>
<p><b>Shortcuts.</b> Ctrl+Shift+D downloads the largest media detected on the page. Ctrl+Shift+A
opens the add dialog.</p>
<p><b>Options page.</b> Holds the pairing token, an option to send the browser's own downloads to
Video Grabber (off by default) and a switch for in-page downloads on claude.ai.</p>
<p><b>Pairing.</b> Usually automatic. If not, see Tools &gt; Pairing &gt; Pair Extension.</p>"""),
        ("Shell guide", f"""
<h3>The handheld shell</h3>
<p>An optional console body around the window. Turn it on in Settings &gt; Theme (Handheld
shell), or flip it from anywhere with Ctrl+Shift+S. It works best with a window of about
900 by 640.</p>
<ul>
<li><b>D-pad up / down</b> moves the selection; <b>left / right</b> changes the filter.</li>
<li><b>A</b> resumes and <b>B</b> pauses the selected download.</li>
<li><b>START</b> pauses or resumes the whole queue. <b>SELECT</b> jumps to search.</li>
<li>The <b>power light</b> switches the backlight.</li>
</ul>
<p>Colors, backlight and the window frame are in Settings &gt; Theme (Detailed view).</p>"""),
        ("Toolkit guide", f"""
<h3>Media Toolkit</h3>
<p>Open it from Tools &gt; Media &gt; Media Toolkit. Preview a video, pick a range on the
timeline with the two draggable handles, then use one of the tabs:</p>
<ul>
<li><b>Compress</b> re-encodes the video smaller, optionally only the selected range.</li>
<li><b>Trim</b> cuts the Start to End range. <i>Exact Cut</i> re-encodes for a frame-accurate
cut.</li>
<li><b>Make GIF</b> turns the range into a GIF (Web Optimized 480p or High Quality 1080p).</li>
</ul>
<p>Results land next to the original and never overwrite it (<i>name</i>_compressed.mp4,
<i>name</i>_trimmed, <i>name</i>_clip.gif; a "(2)" is added if the name is taken).</p>
<p><b>Keys:</b> Space play/pause, I set start, O set end, comma and period step one frame.</p>"""),
    ]


class HelpDialog(QDialog):
    """Help with sections: capabilities, FAQ, troubleshooting and guides."""

    def __init__(self, parent, pair_fn=None, whats_new_fn=None):
        super().__init__(parent)
        self.setWindowTitle("Help")
        self.resize(780, 540)
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(170)
        self.stack = QStackedWidget()
        for title, html in _help_pages():
            self.nav.addItem(title)
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            view.setHtml(html)
            self.stack.addWidget(view)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        row = QHBoxLayout()
        if pair_fn:
            pair = QPushButton("Pair Extension\u2026")
            pair.setObjectName("primary")
            pair.clicked.connect(pair_fn)
            row.addWidget(pair)
        if whats_new_fn:
            wn = QPushButton("What's New\u2026")
            wn.clicked.connect(whats_new_fn)
            row.addWidget(wn)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)


class UpdateDialog(QDialog):
    """Prominent new-version prompt. self.choice ends up as "now",
    "later" (4 h snooze) or "skip" (this version only). The caller saves
    the snooze or skip; closing the window counts as "later"."""

    def __init__(self, parent, tag, current):
        super().__init__(parent)
        self.choice = "later"
        self.setWindowTitle("Video Grabber update")
        self.setModal(True)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        title = QLabel(f"Video Grabber {tag} is available")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        body = QLabel(
            f"You're on v{current}. Update now for the latest fixes and features. "
            "The app restarts briefly while the files are swapped.")
        body.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(body)
        row = QHBoxLayout()
        skip = QPushButton("Skip This Version")
        later = QPushButton("Remind Me Later")
        now = QPushButton("Update Now")
        now.setDefault(True)
        for btn, choice in ((skip, "skip"), (later, "later"), (now, "now")):
            btn.setMinimumHeight(30)
            btn.clicked.connect(lambda _=False, c=choice: self._pick(c))
        row.addWidget(skip)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(now)
        lay.addLayout(row)

    def _pick(self, choice):
        self.choice = choice
        self.accept()


class MainWindow(QMainWindow):
    # Carries the pill payload from /show-add-dialog to the GUI thread.
    _show_dialog_signal = Signal(object)
    _new_job_signal = Signal(object)
    # Update-check result back from the worker thread (updater runs off
    # the UI thread so the app doesn't freeze while it hits GitHub).
    # Background scheduler found a newer app release (tag string).
    _update_available_signal = Signal(str)

    def __init__(self, api=None):
        super().__init__()
        self.setAcceptDrops(True)     # drop a local video to open the Media Toolkit
        self.api = api or ApiClient()
        self.model = DownloadModel(self)
        self.current_filter = "All"
        self.all_items = []
        self._quitting = False
        self._tray = None
        self._tray_hint_shown = False
        self._done_seen = set()
        self._error_seen = set()
        self._recently_done = {}
        self._sort_column = None
        self._refresh_worker = None
        self._logs_worker = None
        self._diskspace_worker = None
        self._queue_worker = None
        self._pref_workers = set()   # v5.5: short save-setting calls (sidebar collapse)
        self._update_tag = ""
        self._batch = {"seeded": False, "busy": False, "done": 0, "failed": 0,
                       "known_done": set(), "known_err": set()}
        self._update_notified = ""
        # Update prompt + changelog state (see _maybe_show_update_dialog).
        self._update_dialog_open = False
        self._snooze_gen = 0
        self._changelog_worker = None
        # Activity Log feed: changelog lines drain here, one per tick.
        self._activity_feed = deque()
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(2200)
        self._activity_timer.timeout.connect(self._activity_tick)
        # Settings snapshot fetched on the worker thread each refresh —
        # the GUI thread never reads settings.json on the hot path (11.2).
        self._gui_settings = {}
        self.animations_enabled = True
        # 12.2: ~30 fps viewport repaint while a download is active so
        # progress interpolation actually renders. Self-guarding no-op.
        self._anim_timer = QTimer(self)
        # 8: ~15 fps is plenty for the progress interpolation; 30 fps
        # repainting the whole table viewport was a source of UI stutter.
        self._anim_timer.setInterval(66)
        self._anim_timer.timeout.connect(self._tick_animations)
        self._anim_timer.start()
        self._sort_ascending = True
        self.setWindowTitle(f"Video Grabber v{DISPLAY_VERSION}")
        self.resize(1200, 760)
        self.setMinimumSize(720, 480)
        self._build_ui()
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._show_dialog_signal.connect(self._on_show_dialog, Qt.QueuedConnection)
        self._apply_styles()
        self._build_tray()
        self._new_job_signal.connect(self._on_new_job, Qt.QueuedConnection)
        self._update_available_signal.connect(self._on_update_available, Qt.QueuedConnection)
        try:
            import updater
            updater.register_update_hook(self._update_available_signal.emit)
            _st = updater.get_update_state()
            if _st.get("available") and _st["available"] != _st.get("skipped"):
                self._update_available_signal.emit(_st["available"])
        except Exception:
            pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start(1500)
        QTimer.singleShot(2000, self._check_version_changelog)
        self.diskspace_timer = QTimer(self)
        self.diskspace_timer.timeout.connect(self.refresh_diskspace)
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.refresh_queue_status)
        self.queue_timer.start(3000)
        self.diskspace_timer.start(15000)  # disk space changes slowly — no need to poll every second
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self._tick_fade)
        self.fade_timer.start(50)
        # 6: periodic silent memory trim (Tools > Trim UI memory does it on demand).
        self._trim_timer = QTimer(self)
        self._trim_timer.timeout.connect(lambda: self.trim_ui_memory(silent=True))
        self._trim_timer.start(10 * 60 * 1000)
        # Generate Sounds moved to Settings > Sounds (see the import at the top).
        self.select_category("All")
        self.refresh()
        self.refresh_diskspace()
        self.refresh_queue_status()

    def new_job_hook(self):
        return self._new_job_signal.emit

    def show_dialog_hook(self):
        return self._show_dialog_signal.emit

    def _on_show_dialog(self, payload):
        """Open AddDownloadDialog pre-filled with the pill's payload. Runs
        on the GUI thread via the queued signal connection."""
        try:
            # Task 3: raise the main window first — an AddDownloadDialog
            # parented to a hidden/backgrounded window can still end up
            # behind other apps even with its own StaysOnTop flag. This is
            # unconditional (unlike _on_new_job's force_on_top check) since
            # the user explicitly needs to interact with this dialog.
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
            if payload.get("pick_playlist"):
                # Pill "Pick playlist items..." (or an older extension's
                # "Download entire playlist"): straight to the picker, with
                # the page's login so the probe sees what the browser sees.
                self._open_playlist_picker(
                    prefill_url=payload.get("url", ""),
                    referer=payload.get("referer"),
                    cookie=payload.get("cookie"),
                    user_agent=payload.get("user_agent"))
                return
            _rt = payload.get("_req_ts")
            if _rt:
                try:
                    from settings import log as _log
                    _log(f"add dialog: opening {int((time.time() - _rt) * 1000)} ms after "
                         f"the extension's request reached the app")
                except Exception:
                    pass
            d = AddDownloadDialog(self, api=self.api,
                                  prefill_url=payload.get("url", ""),
                                  prefill_format_id=payload.get("format_id"),
                                  prefill_target_format=payload.get("target_format"),
                                  prefill_download_playlist=payload.get("download_playlist", False))
            _fade_dialog(d, self.animations_enabled)
            result = d.exec()
            if result not in (QDialog.Accepted, 2):
                return
            v = d.values()
            if not v or not v[0]["url"]:
                return
            common = v[0]
            if common.get("remember") and common.get("save_path"):
                try:
                    self.api.save_setting("per_category_dirs",
                                          {common["category"]: common["save_path"]})
                except Exception:
                    pass
            src_url = (payload.get("url") or "").strip()
            if common.get("download_playlist"):
                # "Download entire playlist (choose items)": hand the URL to
                # the picker instead of queueing one job that walks it all.
                if common.get("skip_dialog"):
                    try:
                        self.api.save_setting("skip_add_dialog", True)
                    except Exception:
                        pass
                same = common["url"] == src_url
                self._open_playlist_picker(
                    prefill_url=common["url"],
                    referer=payload.get("referer") if same else None,
                    cookie=payload.get("cookie") if same else None,
                    user_agent=payload.get("user_agent") if same else None)
                return
            total = len(v)
            for item in v:
                # The payload's filename/referer/cookie/UA describe the URL
                # the browser handed over. Forward them whenever the URL is
                # unchanged (so a cookie is never sent to a different site
                # than it came from). This used to be gated on
                # detect_type()=="generic" — which dropped cookies for every
                # yt-dlp job and produced "Sign in to confirm you're not a
                # bot" on YouTube once the user's IP hit the rate limit.
                extra = {}
                if item["url"] == src_url:
                    extra = {"filename": payload.get("filename"),
                             "referer": payload.get("referer"),
                             "cookie": payload.get("cookie"),
                             "user_agent": payload.get("user_agent"),
                             "page_url": payload.get("page_url")}
                self.api.add(item["url"], category=item["category"],
                             description=item["description"],
                             format_id=item.get("format_id"),
                             target_format=item.get("target_format"),
                             resolution=item.get("resolution"),
                             download_playlist=item.get("download_playlist", False),
                             multi=total > 1, **extra)
            if common.get("skip_dialog"):
                try:
                    self.api.save_setting("skip_add_dialog", True)
                except Exception:
                    pass
        except Exception as e:
            QMessageBox.critical(self, "Add failed", str(e))
        self.refresh()

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_tray_icon(False))
        self._tray.setToolTip("Video Grabber")
        menu = QMenu()
        a_restore = menu.addAction("Restore")
        a_restore.triggered.connect(self._restore_from_tray)
        a_folder = menu.addAction("Open Downloads folder")
        a_folder.triggered.connect(self._open_downloads_folder)
        menu.addSeparator()
        a_pause = menu.addAction("Pause all")
        a_pause.triggered.connect(self._pause_all)
        a_resume = menu.addAction("Resume all")
        a_resume.triggered.connect(self._resume_all)
        menu.addSeparator()
        a_exit = menu.addAction("Exit")
        a_exit.triggered.connect(self._real_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_downloads_folder(self):
        path = Path(load_settings().get("output_dir", str(HOME)))
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))

    def _open_logs_folder(self):
        path = HOME / "logs"
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))

    def _pause_all(self):
        try:
            for j in self.api.jobs():
                if j.get("status") in ("downloading", "queued"):
                    try:
                        self.api.pause(j["id"])
                    except Exception:
                        pass
        except Exception as e:
            QMessageBox.warning(self, "Pause all", str(e))
        self.refresh()

    def _resume_all(self):
        try:
            for j in self.api.jobs():
                if j.get("status") in ("paused", "stopped"):
                    try:
                        self.api.resume(j["id"])
                    except Exception:
                        pass
        except Exception as e:
            QMessageBox.warning(self, "Resume all", str(e))
        self.refresh()

    # --- Supervisor Architecture hooks --------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_vg_started", False):
            self._vg_started = True
            _vg_write_marker("app_alive.tmp")          # window painted
            _vg_update_state(lambda st: st.setdefault("last_paths", {}).update(
                {"data_home": str(HOME)}))
            threading.Thread(target=self._vg_health_watch, daemon=True).start()
            QTimer.singleShot(1500, self._vg_show_update_result)

    def _vg_show_update_result(self):
        """Fix 13: an update that ran while the app was closed (or
        failed inside the updater window nobody saw) is reported
        here, exactly once: vg_state.pop_update_result clears it in
        the same locked pass that reads it."""
        if _vg_state_mod is None:
            return
        try:
            result = _vg_state_mod.pop_update_result()
        except Exception:
            return
        if not result:
            return
        tag = result.get("tag") or "the update"
        if result.get("ok"):
            QMessageBox.information(
                self, "Update finished",
                f"{result.get('message') or f'Updated to {tag}.'}")
        else:
            QMessageBox.warning(
                self, "Update did not finish",
                f"{result.get('message') or 'The update failed.'}\n\n"
                "The previous version is still installed. You can try "
                "again from Tools > Updates, or open Recovery "
                "(Tools > App) if the app misbehaves.")

    def _vg_health_watch(self):
        """Write startup_ok.tmp once our own backend answers /health
        with the Video Grabber identity (contract §3)."""
        if not _VG_LAUNCH_ID:
            return
        try:
            from settings import APP_PORT
        except Exception:
            APP_PORT = 5757
        import urllib.request
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{APP_PORT}/health", timeout=2) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                if isinstance(data, dict) and data.get("app") == "videograbber":
                    _vg_write_marker("startup_ok.tmp")
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def _restart_in_recovery(self):
        """Tools > App: set the boot flag and quit; the supervisor sees
        the flag when we exit and boots recovery.exe instead."""
        if QMessageBox.question(
                self, "Restart in Recovery Mode",
                "Video Grabber will close and the Recovery tool will open. "
                "Continue?") != QMessageBox.Yes:
            return
        try:
            flag = _vg_boot_flag()
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("gui", encoding="utf-8")
        except Exception:
            pass
        self._real_quit()

    def _request_supervised_update(self):
        """Tools > Updates: hand the install to the supervisor (§5).
        We only set update_pending and exit; the supervisor runs the
        animated updater, with updater.bat as its fallback."""
        if QMessageBox.question(
                self, "Install update (supervised)",
                "Video Grabber will close. The supervisor will back up the "
                "current version, download and install the newest allowed "
                "release, and relaunch. Continue?") != QMessageBox.Yes:
            return
        _vg_update_state(lambda st: st.update({"update_pending": True}))
        self._real_quit()

    def _real_quit(self):
        self._quitting = True
        if self._tray:
            self._tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        if (self._tray and self._tray.isVisible()
                and load_settings().get("close_to_tray", True)):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.showMessage(
                    "Video Grabber",
                    "Still running in the system tray. Right-click the icon to Exit.",
                    QSystemTrayIcon.Information,
                    3000,
                )
            return
        self._quitting = True
        # 11.1: stop in-flight fetch workers — a running QThread at exit
        # hangs the app.
        self._save_header_state()
        for w in (self._refresh_worker, self._logs_worker, self._diskspace_worker,
                  self._queue_worker, *list(self._pref_workers), *list(_COMPONENT_WORKERS)):
            if w is not None:
                w.wait(2000)
        if self._tray:
            self._tray.hide()
        event.accept()
        QApplication.instance().quit()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("MainRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(18, 10, 18, 10)
        tl.setSpacing(6)
        title = QLabel("Video Grabber")
        title.setObjectName("ToolbarTitle")
        tl.addWidget(title)
        tl.addSpacing(16)
        self._tb = []  # [button, icon, label]: compact mode swaps text for icon
        for label, action in [
            ("Add URL", self.add_download),
            ("Batch", self.add_batch),
            ("Playlist", self.add_playlist),
            ("Resume", lambda: self._selected("resume")),
            ("Pause", lambda: self._selected("pause")),
            ("Stop", lambda: self._selected("stop")),
            ("Delete", self.remove_selected),
            ("Queue", self.toggle_queue),
            ("Settings", self.open_settings),
        ]:
            b = QPushButton(label)
            b.setObjectName("ToolbarButton")
            b.setToolTip(label)
            b.clicked.connect(action)
            b.setText(label)  # guaranteed fallback if the icon never loads
            ic = _qicon(_TB_ICONS.get(label), _toolbar_icon_color())
            if ic is not None:
                b.setIcon(ic)
                b.setIconSize(QSize(16, 16))
            tl.addWidget(b)
            self._tb.append([b, ic, label])
            if label == "Queue":
                self.queue_btn = b
        self._update_queue_btn()
        tl.addStretch()
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        # One filter pass per pause in typing, not one per keystroke: each pass
        # rebuilds the table and used to show/hide the detail pane in between.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(lambda: self.apply_filter(self.search.text()))
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        tl.addWidget(self.search)
        root_layout.addWidget(toolbar)
        body = QWidget()
        body.setObjectName("FrameArea")
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.category_selected.connect(self.select_category)
        self.sidebar.tool_clicked.connect(self._on_tool)
        self.sidebar.collapsed_changed.connect(self._on_sidebar_collapsed)
        self.sidebar.set_collapsed(bool(load_settings().get("sidebar_collapsed", False)))
        bl.addWidget(self.sidebar)
        content = QWidget()
        content.setObjectName("Content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 12, 12, 10)
        cl.setSpacing(8)
        self.table = DownloadTable()
        _table_shadow = QGraphicsDropShadowEffect(self.table)
        _table_shadow.setBlurRadius(16)
        _table_shadow.setOffset(0, 4)
        _table_shadow.setColor(QColor(0, 0, 0, 80))
        self.table.setGraphicsEffect(_table_shadow)
        self.table.setModel(self.model)
        self.table.setItemDelegate(ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        # Batch 5: every column, Name included, is user-resizable. The last
        # visual column absorbs spare width, and each column has a floor
        # (_clamp_section) so none can be dragged shut.
        for c in range(len(self.model.HEADERS)):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
        for c, w in {0: 360, 1: 80, 2: 120, 3: 90, 4: 140, 5: 90, 6: 120, 7: 70, 8: 120}.items():
            self.table.setColumnWidth(c, w)
        hh.setMinimumSectionSize(40)
        hh.setStretchLastSection(True)
        self._hdr_clamping = False
        hh.setSectionsMovable(True)
        hh.moveSection(7, 4)   # ETA shows after Speed; logical order is unchanged
        hh.moveSection(8, 1)   # Source sits right after Name
        self._restore_header_state()
        hh.sectionResized.connect(self._clamp_section)
        self._hdr_timer = QTimer(self)
        self._hdr_timer.setSingleShot(True)
        self._hdr_timer.timeout.connect(self._save_header_state)
        hh.sectionResized.connect(lambda *_: self._hdr_timer.start(800))
        hh.sectionMoved.connect(lambda *_: self._hdr_timer.start(800))
        self.table.setShowGrid(False)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.doubleClicked.connect(self._on_double_click)
        # Batch 4 #2: table and details panel share a vertical splitter, so the
        # divider between them can be dragged (the height is remembered).
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(4)
        self.table.setMinimumHeight(120)
        self.split.addWidget(self.table)
        cl.addWidget(self.split, 1)
        self.empty_label = QLabel("", self.table.viewport())
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.empty_label.setVisible(False)
        self.table.viewport().installEventFilter(self)
        self.details = QLabel()
        self.details.setObjectName("Details")
        self.details.setTextFormat(Qt.RichText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details.setVisible(False)
        self._log_lines = []
        self._thumb_req = None
        self.detail_pane = DetailPane(tabs_open=bool(_read_gui_state().get("detail_tabs_open", False)))
        self.detail_pane.setVisible(False)
        self.detail_pane.pause_clicked.connect(lambda: self._selected("pause"))
        self.detail_pane.resume_clicked.connect(lambda: self._selected("resume"))
        self.detail_pane.stop_clicked.connect(lambda: self._selected("stop"))
        self.detail_pane.open_clicked.connect(lambda: self._open_selected(folder=False))
        self.detail_pane.folder_clicked.connect(lambda: self._open_selected(folder="select"))
        self.detail_pane.copy_clicked.connect(self._copy_selected_url)
        self.detail_pane.tabs_toggled.connect(lambda on: _write_gui_state({"detail_tabs_open": bool(on)}))
        self._thumbs = ThumbLoader(HOME / "thumbs", self)
        self._thumbs.ready.connect(self._on_thumb_ready, Qt.QueuedConnection)
        # The details label (several rows selected) and the detail pane share
        # the splitter's bottom half; it is hidden while neither is showing.
        self._bottom = _BottomPane()
        self._bottom.setObjectName("Plain")
        _bl = QVBoxLayout(self._bottom)
        _bl.setContentsMargins(0, 0, 0, 0)
        _bl.setSpacing(8)
        _bl.addWidget(self.details)
        _bl.addWidget(self.detail_pane, 1)
        self.split.addWidget(self._bottom)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self._bottom.setVisible(False)
        self._bottom_shown = False
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.timeout.connect(self._save_split)
        self.split.splitterMoved.connect(lambda *_: self._split_timer.start(600))
        # While a box drag is live the selection changes on every mouse move;
        # the details panel waits for boxFinished instead of flickering.
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: None if self.table.live_selecting else self._update_details())
        self.table.boxFinished.connect(self._update_details)
        from PySide6.QtGui import QShortcut, QKeySequence
        for seq, act in (("Alt+Up", "up"), ("Alt+Down", "down")):
            sc = QShortcut(QKeySequence(seq), self.table)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda a=act: self._reorder_selected(a))
        log_label = QLabel("ACTIVITY LOG")
        log_label.setObjectName("SectionLabel")
        self._log_label = log_label
        cl.addWidget(log_label)
        self.log = QLineEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("LogLine")
        cl.addWidget(self.log)
        bl.addWidget(content, 1)
        root_layout.addWidget(body, 1)
        self._root = root
        self.shell = None
        self.apply_shell()
        status = QStatusBar()
        status.setObjectName("BottomStatus")
        # The bar keeps its own height; the central widget gives way, not the bar.
        status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        status.setMinimumHeight(max(24, status.sizeHint().height()))
        self.status_label = QLabel("● Connecting…")
        self.status_label.setObjectName("StatusLabel")
        status.addWidget(self.status_label)
        self.diskspace_label = QLabel("")
        self.diskspace_label.setObjectName("DiskSpaceLabel")
        self.diskspace_label.setToolTip("Free space on the downloads drive")
        self.hold_label = QLabel("")
        self.hold_label.setObjectName("HoldLabel")
        status.addPermanentWidget(self.hold_label)
        self.update_btn = QPushButton("")
        self.update_btn.setObjectName("UpdateBadge")
        self.update_btn.setFlat(True)
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(self._on_update_badge)
        status.addPermanentWidget(self.update_btn)
        status.addPermanentWidget(self.diskspace_label)
        self.version_label = QLabel(f"v{DISPLAY_VERSION} · PySide6 · Flask API")
        status.addPermanentWidget(self.version_label)
        self.setStatusBar(status)
        self._apply_blanc()
        menu = self.menuBar()
        menu.setNativeMenuBar(False)
        f = menu.addMenu("File")
        a = QAction("Add URL…", self); a.triggered.connect(self.add_download); f.addAction(a)
        a = QAction("Batch add…", self); a.triggered.connect(self.add_batch); f.addAction(a)
        a = QAction("Add playlist…", self); a.triggered.connect(self.add_playlist); f.addAction(a)
        f.addSeparator()
        a = QAction("Settings…", self); a.triggered.connect(self.open_settings); f.addAction(a)
        f.addSeparator()
        a = QAction("Exit", self); a.triggered.connect(self._real_quit); f.addAction(a)
        t = menu.addMenu("Tools")
        # v5.5: grouped into submenus. Every action keeps its handler, and the
        # Toggle handheld shell action keeps its Ctrl+Shift+S shortcut.
        tm = t.addMenu("Updates")
        a = QAction("Check for updates…", self); a.triggered.connect(self.check_for_updates); tm.addAction(a)
        a = QAction("Roll back yt-dlp…", self); a.triggered.connect(self.rollback_ytdlp); tm.addAction(a)
        a = QAction("Install update (supervised)…", self); a.triggered.connect(self._request_supervised_update); tm.addAction(a)
        tm = t.addMenu("Pairing")
        a = QAction("Pair Extension\u2026", self); a.triggered.connect(self.show_pair_dialog); tm.addAction(a)
        a = QAction("Copy pairing token", self); a.triggered.connect(self.copy_token); tm.addAction(a)
        a = QAction("Reload token", self); a.triggered.connect(self.reload_token); tm.addAction(a)
        tm = t.addMenu("Downloads")
        a = QAction("Bandwidth profiles…", self); a.triggered.connect(self.open_bandwidth_profiles); tm.addAction(a)
        a = QAction("Retry all failed", self); a.triggered.connect(self.retry_all_failed); tm.addAction(a)
        a = QAction("Clear failed downloads", self); a.triggered.connect(self.clear_failed); tm.addAction(a)
        a = QAction("Open logs folder", self); a.triggered.connect(self._open_logs_folder); tm.addAction(a)
        tm = t.addMenu("Media")
        a = QAction("Media Toolkit\u2026", self); a.triggered.connect(lambda: self.open_toolkit()); tm.addAction(a)
        a = QAction("Trim UI memory", self); a.triggered.connect(lambda: self.trim_ui_memory()); tm.addAction(a)
        tm = t.addMenu("App")
        a = QAction("Toggle handheld shell", self); a.setShortcut("Ctrl+Shift+S"); a.triggered.connect(self.toggle_shell); tm.addAction(a)
        a = QAction("What's New\u2026", self); a.triggered.connect(self.show_whats_new); tm.addAction(a)
        a = QAction("About Video Grabber…", self); a.triggered.connect(self.show_about); tm.addAction(a)
        a = QAction("Restart in Recovery Mode…", self); a.triggered.connect(self._restart_in_recovery); tm.addAction(a)

    def _apply_styles(self):
        # All styling now comes from gui_style.build_qss (app-wide), so the
        # window follows the Theme settings. Nothing is hardcoded here.
        self.setStyleSheet("")

    # ---------------------------------------------------------------- v5 ---
    _EMPTY_TEXT = {
        "Active": "Nothing is downloading right now.",
        "Queue": "Nothing is active or queued.",
        "Failed": "No failed downloads.",
        "Finished": "Nothing has finished yet.",
        "Unfinished": "No unfinished downloads.",
    }

    def _track_batch(self, items):
        """One tray notification when a run of 2+ downloads finishes, instead
        of one per file. 'Busy' = something is downloading or ready to start
        (jobs waiting out a retry delay don't count)."""
        now = time.time()
        done = {j.get("id") for j in items if j.get("status") == "done"}
        errs = {j.get("id") for j in items if j.get("status") == "error"}
        busy = any(j.get("status") == "downloading"
                   or (j.get("status") == "queued" and not (j.get("retry_after") or 0) > now)
                   for j in items)
        b = self._batch
        if not b["seeded"]:
            b.update(seeded=True, busy=busy, known_done=done, known_err=errs)
            return
        b["done"] += len(done - b["known_done"])
        b["failed"] += len(errs - b["known_err"])
        b["known_done"], b["known_err"] = done, errs
        if b["busy"] and not busy:
            total = b["done"] + b["failed"]
            if (total >= 2 and self._tray
                    and self._gui_settings.get("notify_queue_done", True)):
                msg = f"{b['done']} download(s) finished"
                if b["failed"]:
                    msg += f", {b['failed']} failed"
                self._tray.showMessage("Video Grabber", msg + ".",
                                       QSystemTrayIcon.Information, 4000)
            b["done"] = b["failed"] = 0
        elif not busy:
            b["done"] = b["failed"] = 0   # drift while idle (removals)
        b["busy"] = busy

    def _update_empty_state(self):
        if self.model.rowCount() > 0:
            self.empty_label.setVisible(False)
            return
        if self.search.text().strip():
            text = "No downloads match your search."
        elif self.current_filter in self._EMPTY_TEXT:
            text = self._EMPTY_TEXT[self.current_filter]
        elif self.current_filter == "All":
            text = ("No downloads yet.\nClick the pill on a video in your browser, "
                    "or press Add URL.")
        else:
            text = f"No {self.current_filter.lower()} downloads."
        self.empty_label.setText(text)
        self._place_empty_label()
        self.empty_label.setVisible(True)
        self.empty_label.raise_()

    def _place_empty_label(self):
        vp = self.table.viewport()
        self.empty_label.setGeometry(24, 40, max(60, vp.width() - 48), 90)

    def eventFilter(self, obj, event):
        try:
            if obj is self.table.viewport() and event.type() == event.Type.Resize:
                self._place_empty_label()
            # The table accepts drops for its own row reordering, which would
            # swallow file/URL drops before the window sees them; route those here.
            elif obj is self.table.viewport() and event.type() in (
                    event.Type.DragEnter, event.Type.DragMove, event.Type.Drop):
                md = event.mimeData()
                if md.hasUrls():
                    if event.type() == event.Type.Drop:
                        self._set_drop_cue(False)
                        self.dropEvent(event)
                    else:
                        self._set_drop_cue(True)
                        event.acceptProposedAction()
                    return True
            elif obj is self.table.viewport() and event.type() == event.Type.DragLeave:
                self._set_drop_cue(False)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ---- drag and drop: local media -> Media Toolkit, links -> download queue
    def _set_drop_cue(self, on):
        """Batch 4 #5b: outline the table while a link or file is dragged over
        it (QSS: QTableView[dropActive="true"])."""
        on = bool(on)
        if bool(self.table.property("dropActive")) == on:
            return
        self.table.setProperty("dropActive", on)
        self.table.style().unpolish(self.table)
        self.table.style().polish(self.table)
        self.table.update()

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            self._set_drop_cue(True)
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._set_drop_cue(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drop_cue(False)
        from gui_toolkit import is_media_file
        md = event.mimeData()
        files, links = [], []
        for u in md.urls():
            if u.isLocalFile():
                files.append(u.toLocalFile())
            elif u.scheme() in ("http", "https"):
                links.append(u.toString())
        if not md.hasUrls() and md.hasText():
            links += [t for t in md.text().split() if t.lower().startswith(("http://", "https://"))]
        event.acceptProposedAction()
        if links:
            queued = 0
            for link in links:
                try:
                    self.api.add(link)
                    queued += 1
                except Exception as e:
                    QMessageBox.critical(self, "Add failed", str(e))
            if queued:
                self.statusBar().showMessage(
                    f"Queued {queued} link{'s' if queued != 1 else ''} from drop", 5000)
                self.refresh()
        media = [f for f in files if is_media_file(f)]
        if media:
            if len(media) > 1:
                self.statusBar().showMessage(
                    f"Opened the first of {len(media)} dropped files in the Media Toolkit", 5000)
            # Deferred: opening a modal dialog inside dropEvent would block the
            # drag source (Explorer) until the dialog closes.
            QTimer.singleShot(0, lambda p=media[0]: self.open_toolkit(path=p))
        elif files and not links:
            self.statusBar().showMessage(
                f"Not a media file: {Path(files[0]).name}", 5000)

    def _apply_blanc(self):
        """Blanc minimal mode (Settings > General). Hides the Activity Log,
        the toolbar text labels (icons stay) and the status-bar extras (hold
        notice and version text); the connection indicator and disk space
        stay. The sidebar, detail pane, table and search are never touched,
        so this can't fight sidebar collapse or the Queue view."""
        on = bool(load_settings().get("blanc_mode", False))
        self._blanc = on
        for w in (self.hold_label, self.version_label):
            w.setVisible(not on)
        # The Activity Log's visibility is decided together with the text
        # scale (Batch 3): either Blanc or a scale above 115% hides it.
        self._apply_text_scale_layout()

    def _apply_text_scale_layout(self):
        """Batch 3: smart responsive scaling. While Text size is above 115%
        the main window hands the space back so the layout still fits at
        150% without a horizontal scrollbar: the toolbar goes icons-only,
        the Activity Log hides, and the two least important table columns
        (ETA, logical 7; Source, logical 8) hide. At or below 115% all of
        it is restored. Blanc mode composes on top of this (it is checked
        again here), never against it. Called from _apply_blanc(), which
        runs at startup and on every theme/settings re-apply."""
        try:
            scale = int(load_settings().get("text_scale", 100) or 100)
        except (TypeError, ValueError):
            scale = 100
        big = scale > 115
        self._scale_compact = big
        log_on = not big and not getattr(self, "_blanc", False)
        for w in (getattr(self, "_log_label", None), getattr(self, "log", None)):
            if w is not None:
                w.setVisible(log_on)
        table = getattr(self, "table", None)
        if table is not None:
            table.setColumnHidden(7, big)   # ETA
            table.setColumnHidden(8, big)   # Source
        self._apply_toolbar_text()

    def _apply_toolbar_text(self):
        extra = 262 if getattr(self, "shell", None) else 0   # the shell eats width
        compact = (getattr(self, "_blanc", False)
                   or getattr(self, "_scale_compact", False)
                   or (self.width() - extra) < 1240)
        col = _toolbar_icon_color()
        for entry in getattr(self, "_tb", []):
            btn, label = entry[0], entry[2]
            ic = _qicon(_TB_ICONS.get(label), col)
            if ic is not None:
                entry[1] = ic
                btn.setIcon(ic)
                btn.setIconSize(QSize(16, 16))
                btn.setText("" if compact else label)
            else:
                # no icon for this label: show text so the button doesn't vanish
                btn.setIcon(QIcon())
                btn.setText(label)
            btn.setToolTip(label)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_toolbar_text()

    # Minimum width per logical column (Name, Size, Progress, Speed, Status,
    # Category, Completed, ETA, Source).
    _COL_MIN = {0: 160, 1: 60, 2: 100, 3: 70, 4: 110, 5: 70, 6: 100, 7: 60, 8: 80}

    def _clamp_section(self, logical, old, new):
        if getattr(self, "_hdr_clamping", False):
            return
        hh = self.table.horizontalHeader()
        if hh.stretchLastSection() and hh.visualIndex(logical) == hh.count() - 1:
            return      # the stretched column is sized by Qt
        lo = self._COL_MIN.get(logical, 60)
        if new < lo:
            self._hdr_clamping = True
            try:
                hh.resizeSection(logical, lo)
            finally:
                self._hdr_clamping = False

    def _restore_header_state(self):
        # Saved under "table_header_v2": the old "table_header" (Name was a
        # fixed Stretch column) is ignored, which resets the layout once.
        try:
            raw = _read_gui_state().get("table_header_v2")
            if not raw:
                return
            hh = self.table.horizontalHeader()
            if hh.restoreState(QByteArray(base64.b64decode(raw))):
                for c in range(len(self.model.HEADERS)):
                    hh.setSectionResizeMode(c, QHeaderView.Interactive)
                    if hh.sectionSize(c) < self._COL_MIN.get(c, 60):
                        hh.resizeSection(c, self._COL_MIN.get(c, 60))
                hh.setStretchLastSection(True)
        except Exception:
            pass

    def _save_header_state(self):
        try:
            hh = self.table.horizontalHeader()
            _write_gui_state({"table_header_v2": base64.b64encode(
                bytes(hh.saveState())).decode("ascii")})
        except Exception:
            pass

    def _sync_bottom(self):
        """Show the splitter's bottom half only while the details label or the
        detail pane is showing; restore the saved divider position when it
        first appears."""
        show = (self.details.isVisibleTo(self._bottom)
                or self.detail_pane.isVisibleTo(self._bottom))
        self._bottom.setVisible(show)
        if show and not self._bottom_shown:
            self._bottom_shown = True
            QTimer.singleShot(0, self._restore_split)
        elif not show:
            self._bottom_shown = False

    def _restore_split(self):
        h = int(_read_gui_state().get("detail_height", 0) or 0)
        total = self.split.height()
        if h > 0 and total > 0:
            h = max(self._bottom.minimumSizeHint().height(), min(h, total - 120))
            self.split.setSizes([total - h, h])

    def _save_split(self):
        sizes = self.split.sizes()
        if self._bottom.isVisible() and len(sizes) == 2 and sizes[1] > 0:
            _write_gui_state({"detail_height": int(sizes[1])})

    def _update_details(self):
        self._update_details_inner()
        self._sync_bottom()
        sb = self.statusBar()          # safety net: the bottom bar is never left hidden
        if sb is not None and self.isVisible() and not sb.isVisible():
            sb.show()

    def _selection_snapshot(self):
        """(selected job ids, current job id) before a table rebuild."""
        try:
            ids = {j["id"] for j in self._selected_rows()}
        except Exception:
            ids = set()
        ci = self.table.currentIndex()
        cur = None
        if ci.isValid() and ci.row() < len(self.model.items):
            cur = self.model.items[ci.row()].get("id")
        return ids, cur

    def _update_details_inner(self):
        """Panel under the table: the detail pane for one selected row, a
        one-line count for several."""
        try:
            rows = self._selected_rows()
        except Exception:
            rows = []
        if not rows:
            self.details.setVisible(False)
            self.detail_pane.setVisible(False)
            return
        if len(rows) > 1:
            self.detail_pane.setVisible(False)
            self.details.setText(f"{len(rows)} downloads selected")
            self.details.setVisible(True)
            return
        j = rows[0]
        self.details.setVisible(False)
        self.detail_pane.set_job(_detail_info(j))
        self.detail_pane.setVisible(True)
        req = (j.get("id"), j.get("status") == "done")
        if req != self._thumb_req:
            self._thumb_req = req
            if req[1]:
                try:
                    self._thumbs.request(j["id"], _path_for(j), _ffmpeg_dir())
                except Exception:
                    pass
        self._push_job_log(j.get("id"))

    def _push_job_log(self, job_id):
        pat = re.compile(rf"\bjob\s+{re.escape(str(job_id))}\b", re.I)
        self.detail_pane.set_log_lines([ln for ln in self._log_lines if pat.search(ln)][-100:])

    def _on_thumb_ready(self, job_id, path, duration):
        self.detail_pane.set_thumb(job_id, path, duration)

    def _copy_selected_url(self):
        rows = self._selected_rows()
        if len(rows) == 1:
            QApplication.clipboard().setText(str(rows[0].get("url") or ""))

    def rollback_ytdlp(self):
        """Tools > Updates > Roll back yt-dlp (same action as Settings > Updates)."""
        if QMessageBox.question(self, "Roll back yt-dlp",
                                "Go back to the yt-dlp version that shipped with the app? "
                                "This applies the next time Video Grabber starts.",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        w = _ApiFetchWorker(self.api.ytdlp_rollback, self)
        w.result.connect(lambda d: QMessageBox.information(
            self, "Roll back yt-dlp",
            "Override removed. The bundled yt-dlp is used after restart."
            if (d or {}).get("removed") else
            "Nothing to roll back; already on the bundled version."), Qt.QueuedConnection)
        w.failed.connect(lambda: QMessageBox.warning(
            self, "Roll back yt-dlp", "Couldn't reach the app backend."), Qt.QueuedConnection)
        self._rb_worker = w
        w.start()

    def _save_pref(self, key, value):
        """Persist one setting through the backend without blocking the UI."""
        w = _ApiFetchWorker(lambda: self.api.save_setting(key, value), self)
        self._pref_workers.add(w)
        w.finished.connect(lambda w=w: self._pref_workers.discard(w))
        w.start()

    def _on_sidebar_collapsed(self, flag):
        self._save_pref("sidebar_collapsed", bool(flag))

    def _on_tool(self, key):
        {"settings": self.open_settings, "help": self.show_help, "about": self.show_about}[key]()

    def show_help(self):
        # v5.5: sectioned Help dialog (capabilities, FAQ, troubleshooting, guides).
        HelpDialog(self, pair_fn=self.show_pair_dialog, whats_new_fn=self.show_whats_new).exec()

    def show_pair_dialog(self):
        """Pair Extension: Help dialog button and Tools > Pairing."""
        PairDialog(self, getattr(self.api, "token", "")).exec()

    def show_whats_new(self):
        """What's New: Help dialog button and Tools > App."""
        WhatsNewDialog(self, self.api).exec()

    def refresh_queue_status(self):
        if self._queue_worker is not None:
            return
        self._queue_worker = _ApiFetchWorker(self.api.queue_status, self)
        self._queue_worker.result.connect(self._on_queue_status, Qt.QueuedConnection)
        self._queue_worker.finished.connect(lambda: setattr(self, "_queue_worker", None))
        self._queue_worker.start()

    def _on_queue_status(self, data):
        try:
            parts = []
            hold = int((data or {}).get("youtube_hold_s") or 0)
            if hold > 0:
                parts.append(f"\u23f3 YouTube held {max(1, math.ceil(hold / 60))} min")
            if (data or {}).get("low_disk"):
                parts.append("\u26a0 Low disk space: new downloads paused")
            self.hold_label.setText("  \u00b7  ".join(parts))
            self.hold_label.setToolTip(
                "YouTube jobs are held after a bot check or rate limit. Press Resume "
                "on a YouTube job (for example after switching VPN) to retry now."
                if hold > 0 else "")
        except Exception:
            pass

    def _on_update_available(self, tag):
        # The status-bar badge is no longer the prompt; the dialog is.
        self._update_tag = tag
        self.update_btn.setText(f"\u2b06 Update {tag} available")
        self.update_btn.setVisible(False)
        if self._tray and self._update_notified != tag:
            self._update_notified = tag
            self._tray.showMessage(
                "Video Grabber update",
                f"{tag} is available. Open Video Grabber to install.",
                QSystemTrayIcon.Information, 5000)
        self._maybe_show_update_dialog(tag)

    # ---- update prompt (dialog) -------------------------------------------
    def _maybe_show_update_dialog(self, tag, force=False):
        """Show the update prompt for tag, unless that version was skipped or
        a Remind-Me-Later snooze is still running. force=True ignores both."""
        if not tag:
            return
        st = _read_gui_state()
        if not force:
            if st.get("skipped_update_tag") == tag:
                return
            wait = float(st.get("update_snooze_until") or 0) - time.time()
            if wait > 0:
                self._schedule_update_prompt(tag, wait)
                return
        if self._update_dialog_open:
            return
        self._update_dialog_open = True
        try:
            dlg = UpdateDialog(self, tag, DISPLAY_VERSION)
            dlg.exec()
            choice = dlg.choice
        finally:
            self._update_dialog_open = False
        if choice == "now":
            self.check_for_updates()
        elif choice == "skip":
            # Saved in gui_state.json (the /settings route only takes keys
            # it already knows, and the GUI state file is always writable).
            _write_gui_state({"skipped_update_tag": tag})
            try:
                import updater
                updater.skip_version(tag)
            except Exception:
                pass
        else:  # "later": 4-hour snooze, saved so it survives a restart
            _write_gui_state({"update_snooze_until": time.time() + 4 * 3600})
            self._schedule_update_prompt(tag, 4 * 3600)

    def _schedule_update_prompt(self, tag, seconds):
        self._snooze_gen += 1
        gen = self._snooze_gen

        def _fire():
            if gen == self._snooze_gen:  # a newer schedule replaces this one
                self._maybe_show_update_dialog(tag)
        QTimer.singleShot(int(seconds * 1000) + 1000, _fire)

    # ---- changelog after an update -----------------------------------------
    def _check_version_changelog(self):
        """First start after an app update (the version differs from the one
        this GUI saw last): fetch the release notes for the Activity Log.
        A first run only records the version."""
        last = _read_gui_state().get("last_seen_version", "")
        if last == DISPLAY_VERSION:
            return
        if not last:
            _write_gui_state({"last_seen_version": DISPLAY_VERSION})
            return
        self._changelog_worker = _ApiFetchWorker(self.api.changelog, self)
        self._changelog_worker.result.connect(self._on_changelog, Qt.QueuedConnection)
        self._changelog_worker.failed.connect(
            lambda: self._on_changelog({"ok": False, "error": "backend unreachable"}),
            Qt.QueuedConnection)
        self._changelog_worker.start()

    def _on_changelog(self, payload):
        payload = payload or {}
        if payload.get("ok"):
            # Marked seen only once the notes arrive, so an offline start
            # simply tries again on the next launch.
            _write_gui_state({"last_seen_version": DISPLAY_VERSION})
            lines = [f"What's new in v{DISPLAY_VERSION}:"]
            for raw in str(payload.get("body") or "").splitlines():
                t = raw.strip()
                if not t:
                    continue
                t = re.sub(r"^#+\s*", "", t)
                t = re.sub(r"^[-*+]\s+", "\u2022 ", t)
                lines.append(t)
            if len(lines) == 1:
                lines.append("(this release has no notes)")
        else:
            lines = [f"Updated to v{DISPLAY_VERSION}. Couldn't load the release notes "
                     f"({payload.get('error') or 'unknown error'})."]
        self._activity_feed.extend(lines[:25])
        self._activity_tick()
        self._activity_timer.start()

    def _activity_tick(self):
        if not self._activity_feed:
            self._activity_timer.stop()
            return
        self.log.setText(self._activity_feed.popleft())

    def _on_update_badge(self):
        m = QMenu(self)
        a_now = m.addAction("Install now (restarts the app)")
        a_skip = m.addAction("Skip this version")
        chosen = m.exec(self.update_btn.mapToGlobal(self.update_btn.rect().topLeft()))
        if chosen == a_now:
            self.check_for_updates()
        elif chosen == a_skip:
            try:
                import updater
                updater.skip_version(self._update_tag)
            except Exception:
                pass
            self.update_btn.setVisible(False)

    def clear_failed(self):
        n = sum(1 for j in self.all_items if j.get("status") == "error")
        if not n:
            QMessageBox.information(self, "Clear failed", "No failed downloads to clear.")
            return
        if QMessageBox.question(self, "Clear failed",
                                f"Remove {n} failed download(s) from the list?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        def _do():
            try:
                self.api.clear_failed()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        self.log.setText(f"Clearing {n} failed download(s)\u2026")

    def _tick_animations(self):
        if not self.animations_enabled:
            return
        try:
            now = time.time()
            if any(j.get("status") == "downloading"
                   or (j.get("retry_after") or 0) > now for j in self.all_items):
                self.table.viewport().update()
        except RuntimeError:
            self._anim_timer.stop()  # widgets torn down during exit

    def trim_ui_memory(self, silent=False):
        """Drop caches that only grow: favicon pixmaps, done/fade bookkeeping
        for jobs that no longer exist, the yt-dlp format-audio map, and any
        stale QTableWidget rows left in closed dialogs. Safe to run any time;
        everything is rebuilt on demand."""
        import gc
        freed = {}
        try:
            live = {j.get("id") for j in getattr(self, "all_items", [])}
            freed["icons"] = len(_SITE_ICONS)
            _SITE_ICONS.clear()
            before = len(self._done_seen)
            self._done_seen &= live
            freed["done"] = before - len(self._done_seen)
            for jid in [k for k in self._recently_done if k not in live]:
                self._recently_done.pop(jid, None)
            try:
                import engines
                freed["formats"] = len(engines._FORMAT_AUDIO)
                engines._FORMAT_AUDIO.clear()
            except Exception:
                pass
            # Orphaned QTableWidgets (batch/playlist pickers that were closed
            # but not yet garbage collected) keep every cell item alive.
            for tw in self.findChildren(QTableWidget):
                try:
                    if not tw.isVisible():
                        tw.clearContents()
                        tw.setRowCount(0)
                except RuntimeError:
                    pass
            self.table.viewport().update()
        except Exception as e:
            print(f"trim_ui_memory failed: {e}")
        gc.collect()
        if not silent:
            self.statusBar().showMessage(
                f"UI memory trimmed ({freed.get('icons', 0)} icons, "
                f"{freed.get('formats', 0)} format maps cleared)", 4000)

    def refresh(self):
        # 11.1: HTTP off the GUI thread. If a fetch is still in flight
        # (slow backend), skip this tick instead of piling up requests.
        if self._refresh_worker is not None:
            return

        def fetch():
            return self.api.jobs(), load_settings()

        self._refresh_worker = _ApiFetchWorker(fetch, self)
        self._refresh_worker.result.connect(self._on_refreshed, Qt.QueuedConnection)
        self._refresh_worker.failed.connect(self._on_refresh_failed, Qt.QueuedConnection)
        self._refresh_worker.finished.connect(self._refresh_worker_done)
        self._refresh_worker.start()

    def _refresh_worker_done(self):
        self._refresh_worker = None

    def _on_refresh_failed(self):
        self.status_label.setText("● Backend offline")
        self.status_label.setStyleSheet(f"color: {DANGER}; padding-left: 10px;")

    def _on_refreshed(self, payload):
        items, settings = payload
        self.all_items = items
        self.sidebar.set_counts(_sidebar_counts(items, settings.get("hide_old_errors_hours", 24)))
        self._gui_settings = settings
        self._track_batch(items)
        # 11.3: re-read every tick — a Settings-dialog toggle propagates
        # within ~1s; nothing is cached from startup.
        self._gui_settings = settings
        self.animations_enabled = bool(settings.get("animations_enabled", True))
        self.detail_pane.set_play_prefs(bool(settings.get("inline_play_muted", False)),
                                        settings.get("thumb_click_mode", "inline"))
        delegate = self.table.itemDelegate()
        if delegate is not None:
            delegate.animations_enabled = self.animations_enabled
        self.status_label.setText("● Connected")
        self.status_label.setStyleSheet(f"color: {SUCCESS}; padding-left: 10px;")
        for j in self.all_items:
            jid = j.get("id")
            if j.get("status") == "done" and jid not in self._done_seen:
                self._done_seen.add(jid)
                if getattr(self, "_done_seeded", False):
                    self._recently_done[jid] = 1.0
        self._done_seeded = True
        for j in self.all_items:
            jid = j.get("id")
            if j.get("status") == "error" and jid not in self._error_seen:
                self._error_seen.add(jid)
                if getattr(self, "_error_seeded", False):
                    self._apply_reactive_color("error")
        self._error_seeded = True
        # Tray icon reflects download activity (cached icon swap, ~1/sec).
        if getattr(self, "_tray", None):
            active = any(j.get("status") == "downloading" for j in self.all_items)
            self._tray.setIcon(_make_tray_icon(active))
        # Snapshot the selection before the model reset wipes it, then restore.
        selected_ids, current_id = self._selection_snapshot()
        self.apply_filter(self.search.text())
        self.table.restore_selection(selected_ids, current_id)
        # 12.1: rows inserted/removed -> fade the viewport in (150 ms).
        visible_ids = frozenset(j["id"] for j in self.model.items)
        if visible_ids != getattr(self, "_last_visible_ids", None):
            self._last_visible_ids = visible_ids
            _fade_widget(self.table.viewport(), self.animations_enabled)
        self._update_details()

    def _apply_reactive_color(self, status):
        """Batch 4 reactivity: while a job is in the error state, flash
        the theme accent red, then restore the saved theme after 3 s."""
        if status != "error":
            return
        from gui_style import build_qss
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss(load_settings(), override_tokens={
                "accent": "#ef5350", "accent_dim": "#b71c1c"}))
        QTimer.singleShot(3000, self._reset_reactive_color)

    def _reset_reactive_color(self):
        try:
            reapply_theme()
        except RuntimeError:
            pass  # window closed before the reset fired

    def _tick_fade(self):
        if not self._recently_done:
            return
        done = []
        for jid, val in list(self._recently_done.items()):
            val -= 0.04
            if val <= 0:
                done.append(jid)
            else:
                self._recently_done[jid] = val
        for jid in done:
            self._recently_done.pop(jid, None)
        self.table.viewport().update()

    def refresh_logs(self):
        if self._logs_worker is not None:
            return
        self._logs_worker = _ApiFetchWorker(self.api.logs, self)
        self._logs_worker.result.connect(self._on_logs_refreshed, Qt.QueuedConnection)
        self._logs_worker.finished.connect(lambda: setattr(self, "_logs_worker", None))
        self._logs_worker.start()

    def _on_logs_refreshed(self, lines):
        try:
            if lines and not self._activity_timer.isActive():
                self.log.setText(lines[-1])
            self._log_lines = [str(x) for x in (lines or [])]
            if self.detail_pane.isVisible() and self.detail_pane.job_id is not None:
                self._push_job_log(self.detail_pane.job_id)
        except Exception:
            pass

    def refresh_diskspace(self):
        if self._diskspace_worker is not None:
            return
        self._diskspace_worker = _ApiFetchWorker(self.api.disk_space, self)
        self._diskspace_worker.result.connect(self._on_diskspace_refreshed, Qt.QueuedConnection)
        self._diskspace_worker.finished.connect(lambda: setattr(self, "_diskspace_worker", None))
        self._diskspace_worker.start()

    def _on_diskspace_refreshed(self, data):
        try:
            free = (data or {}).get("free")
            total = (data or {}).get("total")
            if not free or not total:
                self.diskspace_label.setText("")
                return
            pct_used = 100 * (1 - free / total) if total else 0
            text = f"💾 {fmt_bytes(free)} free"
            self.diskspace_label.setText(text)
            # Low-space warning: red once free space drops under 2 GB or 95%
            # of the drive is used — either one usually means "about to fail".
            low = free < 2 * 1024**3 or pct_used > 95
            self.diskspace_label.setStyleSheet(
                f"color: {DANGER};" if low else f"color: {MUTED};")
            self.diskspace_label.setToolTip(
                f"{fmt_bytes(free)} free of {fmt_bytes(total)} on the downloads drive"
                + ("  —  running low!" if low else ""))
        except Exception:
            pass

    def _on_new_job(self, job):
        # Silent auto-queue (skip_add_dialog) gives no other feedback when the
        # window is minimized or in the background: one tray toast, at most
        # one per 3 seconds.
        try:
            if (self._tray and (self.isMinimized() or not self.isActiveWindow())
                    and time.time() - getattr(self, "_last_queued_toast", 0.0) >= 3.0):
                self._last_queued_toast = time.time()
                name = (job or {}).get("filename") or (job or {}).get("url") or "download"
                self._tray.showMessage("Video Grabber", f"Queued {name}",
                                       QSystemTrayIcon.Information, 3000)
        except Exception:
            pass
        if not self._gui_settings.get("force_on_top", True):
            return
        # Already focused — raising would only cause a flicker.
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            return
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_bandwidth_profiles(self):
        try:
            dlg = BandwidthProfilesDialog(self, self.api)
            _fade_dialog(dlg, self.animations_enabled)
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Bandwidth profiles error", str(e))

    def show_about(self):
        QMessageBox.about(
            self, "About Video Grabber",
            f"<h3 style='margin-bottom:2px;'>Video Grabber</h3>"
            f"<p style='color:{MUTED};margin-top:0;'>Version {DISPLAY_VERSION}</p>"
            f"<p>A personal video downloader — a Chrome/Vivaldi extension "
            f"paired with this desktop app, built on yt-dlp and Streamlink.</p>"
            f"<p style='color:{MUTED};font-size:11px;'>PySide6 · Flask · {BACKEND_BASE}</p>")

    def retry_all_failed(self):
        failed = [j["id"] for j in self.all_items if j.get("status") == "error"]
        if not failed:
            QMessageBox.information(self, "Retry all failed",
                                    "No failed downloads to retry.")
            return

        def _do():
            for jid in failed:
                try:
                    self.api.redownload(jid)
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True).start()
        self.log.setText(f"Retrying {len(failed)} failed download(s)…")

    def check_for_updates(self):
        """All update entry points (Tools menu, the startup prompt's
        "now", the update badge's "Install now") land here — and all of
        them go through the supervisor (§5). The old in-process
        updater download-and-swap path staged a swap with no blacklist
        check, no backup and no manifest verify, so it is gone: the
        supervised updater is blacklist-aware, backs up first and
        verifies the package before swapping. Only works in the frozen
        (installed) build; the source run just tells the user to use
        git."""
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self, "Check for updates",
                "Updates only apply to the installed (exe) build.\n"
                "Source runs should pull from git instead.")
            return
        self._request_supervised_update()

    def open_settings(self):
        try:
            dlg = SettingsDialog(self, self.api)
            _fade_dialog(dlg, self.animations_enabled)
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Settings error", str(e))

    # ---- optional handheld shell (gui_shell.py) ----
    def apply_shell(self):
        """Wrap the main view in the handheld shell, or unwrap it, per the
        shell_enabled / shell_color settings. Safe to call repeatedly."""
        st = load_settings()
        sig = None
        if st.get("shell_enabled"):
            # The shell bakes the theme accent in at construction, so a
            # changed accent also counts as "no longer matches".
            sig = (st.get("shell_color", "cream"), resolve_palette(st)["accent"])
            if self.shell is not None and getattr(self, "_shell_sig", None) == sig:
                # Same shell: just refresh the parts that have setters.
                self.shell.set_overrides(st.get("shell_colors"))
                self.shell.set_backlight(bool(st.get("shell_backlight", True)))
                self._root.show()
                self._apply_toolbar_text()
                return
        old = self.takeCentralWidget()
        if old is not None and old is not self._root:
            old.layout().removeWidget(self._root)
            self._root.setParent(None)
            old.deleteLater()
        if st.get("shell_enabled"):
            from gui_shell import ShellFrame
            self.shell = ShellFrame(self._root, st.get("shell_color", "cream"),
                                    backlight=bool(st.get("shell_backlight", True)),
                                    overrides=st.get("shell_colors"), theme=st)
            self.shell.dpad.connect(self._shell_dpad)
            self.shell.button.connect(self._shell_button)
            self.setCentralWidget(self.shell)
            self._shell_sig = sig
        else:
            self.shell = None
            self._shell_sig = None
            self.setCentralWidget(self._root)
        self._root.show()
        self._apply_toolbar_text()

    def _shell_dpad(self, d):
        if d in ("up", "down"):
            n = self.model.rowCount()
            if not n:
                return
            cur = self.table.currentIndex().row()
            self.table.selectRow(max(0, min(n - 1, (0 if cur < 0 else cur) + (-1 if d == "up" else 1))))
        else:
            names = list(self.sidebar.buttons.keys())
            i = names.index(self.current_filter) if self.current_filter in names else 0
            self.select_category(names[(i + (-1 if d == "left" else 1)) % len(names)])

    def _shell_button(self, name):
        if name == "start":
            self.toggle_queue()
        elif name == "select":
            self.search.setFocus()
            self.search.selectAll()
        elif name == "power":
            self._toggle_backlight()
        elif self._selected_rows():
            self._selected("resume" if name == "a" else "pause")

    def _toggle_backlight(self):
        """Power light click: flip the shell's backlight and remember it."""
        if not self.shell:
            return
        on = not self.shell.backlight
        if self.api:
            try:
                self.api.save_setting("shell_backlight", on)
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
                return
        self.shell.set_backlight(on)

    def toggle_queue(self):
        """Flip the dispatcher's queue_running flag via /queue-running."""
        running = not bool(load_settings().get("queue_running", True))
        if self.api:
            try:
                self.api.set_queue_running(running)
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
        # Pass the value we just set — re-reading from disk here can race
        # the server's save and show the stale label until next refresh.
        self._update_queue_btn(running)

    def _update_queue_btn(self, running=None):
        if not hasattr(self, "queue_btn"):
            return
        if running is None:
            running = bool(load_settings().get("queue_running", True))
        for entry in getattr(self, "_tb", []):
            if entry[0] is self.queue_btn:
                entry[2] = "Pause Queue" if running else "Start Queue"
                entry[1] = _qicon(_TB_ICONS.get(entry[2]), _toolbar_icon_color())
        self._apply_toolbar_text()
        self.queue_btn.setToolTip(
            "Pause the whole download queue" if running
            else "Resume the whole download queue")

    def _on_header_clicked(self, col):
        if col == self._sort_column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = col
            self._sort_ascending = True
        self.model.headerDataChanged.emit(Qt.Horizontal, 0, len(self.model.HEADERS) - 1)
        self.apply_filter(self.search.text())

    def apply_filter(self, text):
        text = (text or "").lower().strip()
        src = self.all_items
        f = self.current_filter
        if f in ("All", "Unfinished") and not text:
            try:
                hrs = float(self._gui_settings.get("hide_old_errors_hours", 24) or 0)
            except (TypeError, ValueError):
                hrs = 0
            if hrs > 0:
                cutoff = time.time() - hrs * 3600
                src = [x for x in src
                       if not (x.get("status") == "error"
                               and (x.get("failed_ts") or x.get("created_ts") or 0) < cutoff)]
        if f == "Active":
            src = [x for x in src if _is_active(x)]
        elif f == "Queue":
            src = [x for x in src if _in_queue_view(x)]
        elif f == "Failed":
            src = [x for x in src if x["status"] == "error"]
        elif f == "Finished":
            src = [x for x in src if x["status"] == "done"]
        elif f == "Unfinished":
            src = [x for x in src if x["status"] != "done"]
        elif f != "All":
            src = [x for x in src if x["category"] == f]
        if text:
            def matches(x):
                hay = " ".join([
                    x.get("filename", ""),
                    x.get("category", ""),
                    x.get("status", ""),
                    x.get("url", ""),
                    x.get("playlist_title") or "",
                ]).lower()
                return text in hay
            src = [x for x in src if matches(x)]
        # Sort runs after filter+search so it only reorders visible rows.
        if self._sort_column is not None:
            def sort_key(x):
                c = self._sort_column
                if c == 0:
                    return x.get("filename", "").lower()
                if c == 1:
                    return x.get("size_total", 0) or 0
                if c == 2:
                    total = x.get("size_total", 0) or 0
                    return (x.get("size_done", 0) / total) if total else 0
                if c == 3:
                    m = re.match(r"([\d.]+)", x.get("speed", "") or "")
                    return float(m.group(1)) if m else 0
                if c == 4:
                    return x.get("status", "")
                if c == 5:
                    return x.get("category", "").lower()
                if c == 6:
                    return x.get("completed_ts", 0) or 0
                if c == 7:
                    eta = _eta_seconds(x)
                    return eta if eta is not None else 1e12
                if c == 8:
                    return (x.get("source_site") or "").lower()
                return 0
            src = sorted(src, key=sort_key, reverse=not self._sort_ascending)
        else:
            # A playlist batch shares one created_ts: playlist_index keeps it
            # in playlist order (001 first) instead of arbitrary.
            src = sorted(src, key=lambda x: (-(x.get("created_ts") or 0),
                                             x.get("playlist_index") or 0))
        _ids, _cur = self._selection_snapshot()
        self.model.update_items(src)
        self.table.restore_selection(_ids, _cur)   # a rebuild clears the selection and the Shift anchor
        self._update_empty_state()
        self._update_details()                     # detail pane matches what is still selected

    def select_category(self, name):
        self.current_filter = name
        for n, b in self.sidebar.buttons.items():
            b.setProperty("active", n == name)
            b.style().unpolish(b)
            b.style().polish(b)
        self.apply_filter(self.search.text())

    def _selected_rows(self):
        sm = self.table.selectionModel()
        rows = sorted({i.row() for i in sm.selectedRows()})
        if not rows:
            # Ctrl+click can select individual cells without whole rows —
            # fall back to any selected index so batch delete still works.
            rows = sorted({i.row() for i in sm.selectedIndexes()})
        return [self.model.items[r] for r in rows if r < len(self.model.items)]

    def _selected(self, action):
        if not self._selected_rows():
            QMessageBox.information(self, "No job selected", "Select a row first.")
            return
        for j in self._selected_rows():
            try:
                getattr(self.api, action)(j["id"])
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def remove_selected(self):
        items = self._selected_rows()
        if not items:
            QMessageBox.information(self, "No job selected", "Select a row first.")
            return
        if QMessageBox.question(self, "Remove", f"Remove {len(items)} job(s)?") != QMessageBox.Yes:
            return
        for j in items:
            try:
                self.api.delete(j["id"])
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def add_download(self):
        d = AddDownloadDialog(self, api=self.api)
        _fade_dialog(d, self.animations_enabled)
        result = d.exec()
        if result not in (QDialog.Accepted, 2):
            return
        v = d.values()
        if not v or not v[0]["url"]:
            return
        common = v[0]
        if common.get("remember") and common.get("save_path"):
            try:
                self.api.save_setting("per_category_dirs",
                                      {common["category"]: common["save_path"]})
            except Exception:
                pass
        if common.get("download_playlist"):
            self._open_playlist_picker(prefill_url=common["url"])
            return
        total = len(v)
        for item in v:
            try:
                self.api.add(item["url"], category=item["category"],
                             description=item["description"],
                             format_id=item.get("format_id"),
                             target_format=item.get("target_format"),
                             resolution=item.get("resolution"),
                             download_playlist=item.get("download_playlist", False),
                             multi=total > 1)
            except Exception as e:
                QMessageBox.critical(self, "Add failed", str(e))
        self.refresh()

    def add_playlist(self):
        self._open_playlist_picker()

    def _open_playlist_picker(self, prefill_url="", referer=None, cookie=None,
                              user_agent=None):
        """Modal playlist picker; queues the checked items as one job each
        through /batch. Called from the pill hook, the Add URL dialog's
        playlist checkbox, the toolbar and the File menu."""
        try:
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
            d = PlaylistPickerDialog(self, api=self.api, prefill_url=prefill_url,
                                     referer=referer, cookie=cookie,
                                     user_agent=user_agent)
            _fade_dialog(d, self.animations_enabled)
            if d.exec() != QDialog.Accepted:
                return
            items = d.values()
            if not items:
                return
            res = self.api.batch(items, **d.request_headers())
            n = len((res or {}).get("job_ids") or [])
            self.statusBar().showMessage(
                f"Queued {n} item{'s' if n != 1 else ''} from \"{d.playlist_title()}\"", 6000)
        except Exception as e:
            QMessageBox.critical(self, "Add failed", str(e))
        self.refresh()

    def add_batch(self):
        text, ok = QInputDialog.getMultiLineText(self, "Batch add", "One URL per line:")
        if not ok or not text.strip():
            return
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    self.api.add(line)
                except Exception:
                    pass
        self.refresh()

    def copy_token(self):
        QApplication.clipboard().setText(self.api.token)
        QMessageBox.information(self, "Token copied", "Pairing token copied to clipboard.")

    def reload_token(self):
        self.api.reload_token()
        QMessageBox.information(self, "Token reloaded", f"Token is now: {self.api.token[:8]}…")

    def context_menu(self, pos):
        # Resolve the row from the click position itself — the selection
        # model may have been wiped by the 1s refresh by the time the user
        # picks a menu item, so handlers key off self._current_ctx instead.
        idx = self.table.indexAt(pos)
        if not idx.isValid() or idx.row() >= len(self.model.items):
            if self.model.items:           # empty space: just offer Select all
                em = QMenu(self)
                if em.addAction("Select all   (Ctrl+A)") == em.exec(
                        self.table.viewport().mapToGlobal(pos)):
                    self.table.selectAll()
            return
        self._current_ctx = self.model.items[idx.row()]["id"]
        j = self.model.items[idx.row()]
        m = QMenu(self)
        a_open = m.addAction("Open")
        a_open_folder = m.addAction("Open folder")
        a_open_location = m.addAction("Open file location")
        m.addSeparator()
        a_resume = m.addAction("Resume")
        a_stop = m.addAction("Stop")
        a_redl = m.addAction("Redownload")
        m.addSeparator()
        q_menu = m.addMenu("Queue position")
        q_actions = {q_menu.addAction("Move to top"): "top",
                     q_menu.addAction("Move up   (Alt+Up)"): "up",
                     q_menu.addAction("Move down   (Alt+Down)"): "down",
                     q_menu.addAction("Move to bottom"): "bottom"}
        q_menu.setEnabled(j["status"] == "queued")
        m.addSeparator()
        cat_menu = m.addMenu("Move to Category")
        cat_actions = {cat_menu.addAction(c): c for c in CATEGORIES}
        m.addSeparator()
        conv_menu = m.addMenu("Convert to")
        conv_actions = {conv_menu.addAction(f): f for f in ["mp4", "mkv", "mp3", "wav", "m4a", "flac", "opus"]}
        m.addSeparator()
        a_remove = m.addAction("Remove from Queue")
        a_delete_file = m.addAction("Delete Job && File")
        a_toolkit = m.addAction("Media Toolkit\u2026")
        a_copy_file = m.addAction("Copy file")
        a_copy_url = m.addAction("Copy URL")
        a_props = m.addAction("Properties")
        m.addSeparator()
        a_select_all = m.addAction("Select all   (Ctrl+A)")
        a_toolkit.setEnabled(j["status"] == "done")
        a_open.setEnabled(j["status"] == "done")
        a_open_folder.setEnabled(j["status"] == "done")
        a_open_location.setEnabled(j["status"] == "done")
        a_copy_file.setEnabled(j["status"] == "done")
        retrying = bool(_retry_text(j))
        if retrying:
            a_resume.setText("Retry now")
        a_resume.setEnabled(j["status"] in ("paused", "stopped", "error") or retrying)
        a_stop.setEnabled(j["status"] in ("downloading", "queued"))
        a_redl.setEnabled(j["status"] in ("done", "error", "stopped"))
        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        # Re-look-up the row now: a refresh may have replaced it while the
        # menu was open, so every handler below reads the fresh dict.
        j = next((x for x in self.model.items
                  if x["id"] == self._current_ctx), j)
        if chosen == a_select_all:
            self.table.selectAll()
        elif chosen == a_open:
            self._ctx_open()
        elif chosen == a_open_folder:
            self._ctx_open_folder()
        elif chosen == a_open_location:
            self._ctx_open_location()
        elif chosen == a_resume:
            self._ctx_resume()
        elif chosen == a_stop:
            self._ctx_stop()
        elif chosen == a_redl:
            self._ctx_redownload()
        elif chosen in q_actions:
            self._reorder(self._current_ctx, q_actions[chosen])
        elif chosen in cat_actions:
            try:
                self.api.set_category(self._current_ctx, cat_actions[chosen])
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
            self.refresh()
        elif chosen in conv_actions:
            try:
                self.api.convert(self._current_ctx, conv_actions[chosen])
            except Exception as e:
                QMessageBox.warning(self, "Convert", str(e))
        elif chosen == a_remove:
            self._ctx_remove()
        elif chosen == a_delete_file:
            self._ctx_delete_file()
        elif chosen == a_toolkit:
            self.open_toolkit(job_id=self._current_ctx)
        elif chosen == a_copy_file:
            # Right-clicked row is part of a multi-selection -> copy all the
            # selected finished files; otherwise just this one.
            picked = self._selected_rows()
            if not any(x["id"] == self._current_ctx for x in picked):
                picked = [j]
            if not _copy_files_to_clipboard(picked):
                QMessageBox.information(self, "Copy file",
                                        "The file no longer exists on disk.")
        elif chosen == a_copy_url:
            QApplication.clipboard().setText(j.get("url", ""))
        elif chosen == a_props:
            # Re-fetch at click time — the row object captured when the menu
            # opened can be stale if a refresh fired while the menu was up.
            fresh = next((x for x in self.model.items
                          if x["id"] == self._current_ctx), j)
            self._show_props(fresh)

    def _reorder(self, jid, action):
        def _do():
            try:
                self.api.reorder(jid, action)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        QTimer.singleShot(400, self.refresh)

    def _reorder_selected(self, action):
        rows = self._selected_rows()
        if len(rows) != 1 or rows[0].get("status") != "queued":
            return
        self._reorder(rows[0]["id"], action)

    def _ctx_open(self):
        jid = getattr(self, "_current_ctx", None)
        if jid:
            self._open_selected(job_id=jid)

    def _ctx_open_folder(self):
        jid = getattr(self, "_current_ctx", None)
        if jid:
            self._open_selected(folder=True, job_id=jid)

    def _ctx_open_location(self):
        jid = getattr(self, "_current_ctx", None)
        if jid:
            self._open_selected(folder="select", job_id=jid)

    def _ctx_resume(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            self.api.resume(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_stop(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            self.api.stop(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_redownload(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            self.api.redownload(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_remove(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        if QMessageBox.question(self, "Remove", "Remove this job?") != QMessageBox.Yes:
            return
        try:
            self.api.delete(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_delete_file(self):
        """Delete the job AND its file. The backend only deletes inside the
        VideoGrabber / download folders and refuses anything else."""
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        j = next((x for x in self.model.items if x["id"] == jid), {})
        if QMessageBox.warning(
                self, "Delete job & file",
                f"Delete this job AND its file from disk?\n\n{j.get('filename', '')}\n\n"
                "This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.api.delete_file(jid)
        except Exception as e:
            QMessageBox.warning(self, "Delete job & file", str(e))
        self.refresh()

    def open_toolkit(self, job_id=None, path=None):
        """Media Toolkit (visual Trim / Compress / GIF editor). Opens on an
        explicit file path (drag and drop), a finished job, or the selected
        finished job; with none of those it opens empty (Open file / drop)."""
        from gui_toolkit import ToolkitDialog
        if path is None:
            job = None
            if job_id is not None:
                job = next((x for x in self.model.items if x["id"] == job_id), None)
            else:
                done = [x for x in self._selected_rows() if x.get("status") == "done"]
                job = done[0] if done else None
            if job is not None:
                try:
                    p = _path_for(job)
                    path = str(p) if p and Path(p).is_file() else None
                except Exception:
                    path = None
        ToolkitDialog(str(path) if path else None, self).exec()

    def toggle_shell(self):
        """Flip the handheld shell on/off from anywhere (Ctrl+Shift+S): off
        gives a plain, modern window with more room for rows."""
        try:
            on = not bool(load_settings().get("shell_enabled"))
            self.api.save_setting("shell_enabled", on)
            _settings_cache["mtime"] = 0      # force load_settings to re-read
            self.apply_shell()
            self.statusBar().showMessage(
                "Handheld shell on" if on else "Handheld shell off", 3000)
        except Exception as e:
            QMessageBox.warning(self, "Shell", str(e))

    def _open_selected(self, folder=False, job_id=None):
        """Open a job's file (or its containing folder) with proper error
        reporting so failures are visible instead of silent. When job_id is
        given, only that job is opened; otherwise every selected row is."""
        if job_id is not None:
            items = [j for j in self.model.items if j["id"] == job_id]
        else:
            items = self._selected_rows()
        for job in items:
            path = _path_for(job)
            if not path.exists():
                # Legacy whole-playlist job: its own file path never existed
                # (the files live in <category>/<PlaylistTitle>/). Open the
                # playlist folder instead of reporting "File Not Found".
                pl_dir = _playlist_dir_for(job)
                if pl_dir is not None:
                    try:
                        if sys.platform == "win32":
                            os.startfile(str(pl_dir))
                        elif sys.platform == "darwin":
                            subprocess.Popen(["open", str(pl_dir)])
                        else:
                            subprocess.Popen(["xdg-open", str(pl_dir)])
                    except Exception as e:
                        QMessageBox.critical(
                            self, "Error Opening Target",
                            f"Couldn't open, error: {e}\n\nPath:\n{pl_dir}")
                    continue
            # folder: False = open the file; True = open its folder;
            # "select" = open the folder with the file highlighted.
            target = path if folder in (False, "select") else path.parent
            if not target.exists():
                QMessageBox.warning(
                    self, "File Not Found",
                    f"File not found, expected at:\n{target}"
                )
                continue
            try:
                target_str = str(target.resolve())
                if sys.platform == "win32":
                    if folder == "select":
                        _win_select_in_explorer(path.resolve())
                    elif folder:
                        os.startfile(str(path.parent))
                    else:
                        os.startfile(target_str)
                elif sys.platform == "darwin":
                    if folder == "select":
                        subprocess.Popen(["open", "-R", target_str])
                    elif folder:
                        subprocess.Popen(["open", str(path.parent)])
                    else:
                        subprocess.Popen(["open", target_str])
                else:
                    subprocess.Popen(["xdg-open", str(path.parent)])
            except Exception as e:
                QMessageBox.critical(
                    self, "Error Opening Target",
                    f"Couldn't open, error: {e}\n\nPath:\n{target}"
                )

    def _on_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self.model.items):
            job = self.model.items[row]
            if job.get("status") == "done":
                self._open_selected(folder=False, job_id=job["id"])

    def _show_props(self, j):
        path = _path_for(j)
        text = "\n".join([
            f"Name:     {j['filename']}",
            f"URL:      {j['url']}",
            f"Type:     {j['type']}",
            f"Status:   {j['status']}",
            f"Category: {j['category']}",
            f"Size:     {fmt_bytes(j.get('size_total'))}",
            f"Done:     {fmt_bytes(j.get('size_done'))}",
            f"Path:     {path}",
            f"Exists:   {path.exists()}",
            f"Completed:{fmt_ts(j.get('completed_ts'))}",
        ])
        QMessageBox.information(self, "Properties", text)


_app = None
_window = None


def launch_gui(new_job_hook=None, home_dir=None, show_dialog_hook=None):
    """Run the Qt GUI in the current (main) thread. `new_job_hook` is an
    optional callable the server calls whenever a job is queued — it must
    be safe to invoke from any thread. `home_dir` should be the same Path
    server.py resolved for HOME (portable_home()) so this module reads and
    writes the exact settings.json the backend is using."""
    global _app, _window, HOME, CONFIG_PATH
    if home_dir is not None:
        HOME = Path(home_dir)
        CONFIG_PATH = HOME / "settings.json"
    _app = QApplication.instance() or QApplication(sys.argv)
    from gui_style import build_qss
    _sync_palette(load_settings())
    _app.setStyleSheet(build_qss(load_settings()))
    _app.setApplicationName("Video Grabber")
    _apply_app_icon(_app)
    _app.setFont(QFont("Segoe UI", 10))
    _app.setQuitOnLastWindowClosed(False)
    # Batch 4 #4: dark native title bars on the main window and every dialog.
    from gui_shell import DarkTitleBarFilter
    _app._vg_dark_filter = DarkTitleBarFilter(_title_bar_dark, _app, _title_bar_colors)
    _app.installEventFilter(_app._vg_dark_filter)
    _window = MainWindow()
    _apply_app_icon(_app, _window)
    _window.show()
    if new_job_hook is not None:
        new_job_hook(_window.new_job_hook())
    if show_dialog_hook is not None:
        show_dialog_hook(_window.show_dialog_hook())
    return _app.exec()
