"""
gui_qt.py — modern PySide6 frontend for Video Grabber.

Talks to the local Flask backend on 127.0.0.1:5757. Reads the pairing token
from settings.json. Imported and launched by server.py's main().
"""

import base64
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
    QItemSelectionModel, QPropertyAnimation, QEasingCurve, QAbstractAnimation,
    QElapsedTimer, QByteArray,
)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QTextEdit
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame,
    QLabel, QPushButton, QToolButton, QLineEdit, QTableView, QHeaderView,
    QMenu, QMessageBox, QDialog, QDialogButtonBox, QFormLayout, QComboBox,
    QColorDialog, QTableWidget, QTableWidgetItem,
    QStatusBar, QSizePolicy, QAbstractItemView, QStyledItemDelegate,
    QInputDialog, QFileDialog, QCheckBox, QSystemTrayIcon, QStyle,
    QSpinBox, QListWidget, QListWidgetItem, QTabWidget, QProgressBar,
    QStackedWidget, QScrollArea, QGroupBox,
)

BACKEND_BASE = "http://127.0.0.1:5757"
HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"

# Palette-derived (single source of truth: palette.py).
from palette import resolve_palette
from settings import CATEGORIES, DISPLAY_VERSION, detect_type  # single source of truth: settings.py

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
    _TRAY_ICONS.clear()
    _STATUS_COLORS.update({"downloading": SUCCESS, "done": SUCCESS, "paused": WARN,
                           "stopped": DANGER, "error": DANGER, "queued": MUTED,
                           "skipped": _PAL["text_dim"]})


def reapply_theme():
    """Rebuild the app stylesheet from saved settings and repaint the main
    window. Called after Settings saves."""
    from gui_style import build_qss
    st = load_settings()
    _sync_palette(st)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(build_qss(st))
    w = globals().get("_window")
    if w is not None:
        w.table.viewport().update()
        w.table.horizontalHeader().viewport().update()
        w.select_category(w.current_filter)
        w.apply_shell()


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


def _sidebar_counts(items):
    c = {
        "All": len(items),
        "Active": sum(1 for j in items if _is_active(j)),
        "Finished": sum(1 for j in items if j.get("status") == "done"),
        "Unfinished": sum(1 for j in items if j.get("status") != "done"),
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

    def disk_space(self):
        return self._req("GET", "/diskspace") or {}

    def reorder(self, jid, action):
        return self._req("POST", f"/reorder/{jid}", json={"action": action})

    def dup_check(self, url):
        return (self._req("POST", "/dup-check", json={"url": url}) or {}).get("duplicate")

    def queue_status(self):
        return self._req("GET", "/queue-status") or {}

    def update_status(self):
        return self._req("GET", "/update-status") or {}

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


def _path_for(job):
    settings = load_settings()
    category = job.get("category", "Other")
    override = settings.get("per_category_dirs", {}).get(category)
    base = Path(override) if override else Path(settings.get("output_dir", str(HOME)))
    cat_dir = base if override else base / category
    # Session D: mirrors settings._dest_for - keep the two in sync.
    sub = job.get("subdir")
    if sub:
        for ch in '<>:"/\\|?*':
            sub = sub.replace(ch, "_")
        cat_dir = cat_dir / sub
    name = job["filename"]
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return cat_dir / name


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


class ProgressDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 12.2: per-row interpolation toward the newest value over 100 ms.
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
        f = min(1.0, timer.elapsed() / 100.0)
        f = 1.0 - (1.0 - f) ** 3  # ease-out across the 100 ms window
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
            # Very short rows: keep the bar drawable instead of negative-height.
            rect.setHeight(4)
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
        tabs.addTab(g, "General")

        # ---- Theme tab (gui_theme_page.py) ----
        from gui_theme_page import ThemePage
        self.theme_page = ThemePage(s)
        tabs.addTab(self.theme_page, "Theme")

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
                  self.open_folder_on_complete, self.play_sound_on_complete):
            pl.addRow(w)
        tabs.addTab(p, "Post-Download")

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
        self.update_info = QLabel("Press Refresh to see yt-dlp and app versions.")
        self.update_info.setWordWrap(True)
        self.update_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        upl.addRow(self.update_info)
        ub = QHBoxLayout()
        for text, fn in (("Refresh", self._update_refresh),
                         ("Check yt-dlp now", self._update_ytdlp_now),
                         ("Roll back yt-dlp", self._update_ytdlp_rollback)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            ub.addWidget(b)
        ub.addStretch(1)
        upl.addRow(ub)
        upnote = QLabel("A new yt-dlp is downloaded in the background and applies the "
                        "next time Video Grabber starts. The app itself is never "
                        "installed without asking.")
        upnote.setWordWrap(True)
        upnote.setStyleSheet(f"color: {MUTED};")
        upl.addRow(upnote)
        tabs.addTab(up, "Updates")

        # ---- Save To ----
        st = QWidget()
        stl = QFormLayout(st)
        self.output_dir = QLineEdit(s.get("output_dir", ""))
        stl.addRow("Default folder", self.output_dir)
        self.percat_edits = {}
        for cat in ["Video", "Music", "Compressed", "Documents", "Programs", "Other"]:
            e = QLineEdit(s.get("per_category_dirs", {}).get(cat, ""))
            e.setPlaceholderText("(use default folder)")
            self.percat_edits[cat] = e
            stl.addRow(f"{cat} folder", e)
        tabs.addTab(st, "Save To")

        # ---- File Types ----
        ft = QWidget()
        ftl = QFormLayout(ft)
        ftl.addRow(QLabel("Check to DISABLE auto-capture for that type:"))
        self.ft_checks = {}
        disabled = s.get("file_types_overrides", {})
        for ext in ["mp4", "mkv", "webm", "mov", "avi", "mp3", "wav", "flac",
                    "m4a", "aac", "zip", "rar", "7z", "tar", "gz", "pdf", "doc",
                    "docx", "ppt", "pptx", "txt", "exe", "msi", "dmg"]:
            cb = QCheckBox(f".{ext}")
            cb.setChecked(disabled.get(ext) is False)
            self.ft_checks[ext] = cb
            ftl.addRow(cb)
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
        return "\n".join(lines)

    def _run_update_job(self, fn, on_done=None):
        w = _ApiFetchWorker(fn, self)
        w.result.connect(lambda d: (self.update_info.setText(
            on_done(d) if on_done else self._fmt_update_status(d))), Qt.QueuedConnection)
        w.failed.connect(lambda: self.update_info.setText(
            "Couldn't reach the app backend."), Qt.QueuedConnection)
        self._update_worker = w
        w.start()

    def _update_refresh(self):
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

    ENGINES = ("auto", "yt-dlp", "streamlink")

    def _check_engine_updates(self):
        """Manual version check: reports via dialog + log; never auto-updates."""
        import json as _json
        import subprocess as _sp
        from settings import log
        lines = []
        for name in ("yt-dlp", "streamlink"):
            try:
                if name == "yt-dlp":
                    import yt_dlp
                    local = yt_dlp.version.__version__
                else:
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
                if latest and latest != local:
                    lines.append(f"{name}: local {local}, latest {latest} — update available")
                else:
                    lines.append(f"{name}: {local}" + ("" if latest else " (latest unknown)"))
            except Exception as e:
                lines.append(f"{name}: check failed — {e}")
        log("engine update check: " + "; ".join(lines))
        QMessageBox.information(self, "Engine versions", "\n".join(lines))

    def _add_rule_row(self, domain, category, engine):
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
                                  {ext: not cb.isChecked()
                                   for ext, cb in self.ft_checks.items()})
            # --- v4 (Session 6) ---
            tv = self.theme_page.values()
            self.api.save_setting("accent", tv["accent"])
            self.api.save_setting("theme_preset", tv["theme_preset"])
            self.api.save_setting("theme_tokens", tv["theme_tokens"])
            self.api.save_setting("shell_enabled", tv["shell_enabled"])
            self.api.save_setting("shell_color", tv["shell_color"])
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

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(200)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(4)
        brand = QLabel("↓  VIDEO GRABBER")
        brand.setObjectName("Brand")
        layout.addWidget(brand)
        layout.addSpacing(14)
        self.buttons = {}
        for name in ["All", "Active", "Finished", "Unfinished", "Failed"]:
            self._add_nav(layout, name)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("SidebarSeparator")
        layout.addWidget(sep)
        label = QLabel("CATEGORIES")
        label.setObjectName("SectionLabel")
        layout.addWidget(label)
        for name in CATEGORIES:
            self._add_nav(layout, name)
        layout.addStretch()

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

    def set_counts(self, counts):
        """Show a count next to each entry that has any rows."""
        for name, btn in self.buttons.items():
            n = counts.get(name, 0)
            text = f"{name}  ({n})" if n else name
            if btn.text() != text:
                btn.setText(text)

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

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.result.emit(self._fn())
        except Exception:
            self.failed.emit()


class MainWindow(QMainWindow):
    # Carries the pill payload from /show-add-dialog to the GUI thread.
    _show_dialog_signal = Signal(object)
    _new_job_signal = Signal(object)
    # Update-check result back from the worker thread (updater runs off
    # the UI thread so the app doesn't freeze while it hits GitHub).
    _update_result_signal = Signal(object)
    # Background scheduler found a newer app release (tag string).
    _update_available_signal = Signal(str)

    def __init__(self, api=None):
        super().__init__()
        self.api = api or ApiClient()
        self.model = DownloadModel(self)
        self.current_filter = "All"
        self.all_items = []
        self._quitting = False
        self._tray = None
        self._tray_hint_shown = False
        self._done_seen = set()
        self._recently_done = {}
        self._sort_column = None
        self._refresh_worker = None
        self._logs_worker = None
        self._diskspace_worker = None
        self._queue_worker = None
        self._update_tag = ""
        self._batch = {"seeded": False, "busy": False, "done": 0, "failed": 0,
                       "known_done": set(), "known_err": set()}
        self._update_notified = ""
        # Settings snapshot fetched on the worker thread each refresh —
        # the GUI thread never reads settings.json on the hot path (11.2).
        self._gui_settings = {}
        self.animations_enabled = True
        # 12.2: ~30 fps viewport repaint while a download is active so
        # progress interpolation actually renders. Self-guarding no-op.
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._tick_animations)
        self._anim_timer.start()
        self._sort_ascending = True
        self.setWindowTitle(f"Video Grabber v{DISPLAY_VERSION}")
        self.resize(1200, 760)
        self.setMinimumSize(880, 560)
        self._build_ui()
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._show_dialog_signal.connect(self._on_show_dialog, Qt.QueuedConnection)
        self._apply_styles()
        self._build_tray()
        self._new_job_signal.connect(self._on_new_job, Qt.QueuedConnection)
        self._update_result_signal.connect(self._on_update_result, Qt.QueuedConnection)
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
        self.timer.start(1000)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start(1500)
        self.diskspace_timer = QTimer(self)
        self.diskspace_timer.timeout.connect(self.refresh_diskspace)
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.refresh_queue_status)
        self.queue_timer.start(3000)
        self.diskspace_timer.start(15000)  # disk space changes slowly — no need to poll every second
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self._tick_fade)
        self.fade_timer.start(50)
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
                  self._queue_worker):
            if w is not None:
                w.wait(2000)
        if self._tray:
            self._tray.hide()
        event.accept()
        QApplication.instance().quit()

    def _build_ui(self):
        root = QWidget()
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
        for label, icon, action in [
            ("Add URL", "+", self.add_download),
            ("Batch", "☰", self.add_batch),
            ("Playlist", "☷", self.add_playlist),
            ("Resume", "▶", lambda: self._selected("resume")),
            ("Pause", "Ⅱ", lambda: self._selected("pause")),
            ("Stop", "■", lambda: self._selected("stop")),
            ("Delete", "⌫", self.remove_selected),
            ("Queue", "⏯", self.toggle_queue),
            ("Settings", "⚙", self.open_settings),
        ]:
            b = QPushButton(f"{icon}  {label}")
            b.setObjectName("ToolbarButton")
            b.setToolTip(label)
            b.clicked.connect(action)
            tl.addWidget(b)
            self._tb.append([b, icon, label])
            if label == "Queue":
                self.queue_btn = b
        self._update_queue_btn()
        tl.addStretch()
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        tl.addWidget(self.search)
        root_layout.addWidget(toolbar)
        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.category_selected.connect(self.select_category)
        bl.addWidget(self.sidebar)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 12, 12, 10)
        cl.setSpacing(8)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        # Every other column is user-resizable (drag the header edge).
        for c, w in {1: 80, 2: 120, 3: 90, 4: 140, 5: 90, 6: 120, 7: 70, 8: 120}.items():
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.table.setColumnWidth(c, w)
        hh.setMinimumSectionSize(60)
        hh.setStretchLastSection(False)
        hh.setSectionsMovable(True)
        hh.moveSection(7, 4)   # ETA shows after Speed; logical order is unchanged
        hh.moveSection(8, 1)   # Source sits right after Name
        self._restore_header_state()
        self._hdr_timer = QTimer(self)
        self._hdr_timer.setSingleShot(True)
        self._hdr_timer.timeout.connect(self._save_header_state)
        hh.sectionResized.connect(lambda *_: self._hdr_timer.start(800))
        hh.sectionMoved.connect(lambda *_: self._hdr_timer.start(800))
        self.table.setShowGrid(False)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.doubleClicked.connect(self._on_double_click)
        cl.addWidget(self.table, 1)
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
        cl.addWidget(self.details)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_details())
        from PySide6.QtGui import QShortcut, QKeySequence
        for seq, act in (("Alt+Up", "up"), ("Alt+Down", "down")):
            sc = QShortcut(QKeySequence(seq), self.table)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda a=act: self._reorder_selected(a))
        log_label = QLabel("ACTIVITY LOG")
        log_label.setObjectName("SectionLabel")
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
        status.addPermanentWidget(QLabel(f"v{DISPLAY_VERSION} · PySide6 · Flask API"))
        self.setStatusBar(status)
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
        a = QAction("Check for updates…", self); a.triggered.connect(self.check_for_updates); t.addAction(a)
        t.addSeparator()
        a = QAction("Copy pairing token", self); a.triggered.connect(self.copy_token); t.addAction(a)
        a = QAction("Reload token", self); a.triggered.connect(self.reload_token); t.addAction(a)
        a = QAction("Bandwidth profiles…", self); a.triggered.connect(self.open_bandwidth_profiles); t.addAction(a)
        t.addSeparator()
        a = QAction("Retry all failed", self); a.triggered.connect(self.retry_all_failed); t.addAction(a)
        a = QAction("Clear failed downloads", self); a.triggered.connect(self.clear_failed); t.addAction(a)
        a = QAction("Open logs folder", self); a.triggered.connect(self._open_logs_folder); t.addAction(a)
        t.addSeparator()
        a = QAction("About Video Grabber…", self); a.triggered.connect(self.show_about); t.addAction(a)

    def _apply_styles(self):
        # All styling now comes from gui_style.build_qss (app-wide), so the
        # window follows the Theme settings. Nothing is hardcoded here.
        self.setStyleSheet("")

    # ---------------------------------------------------------------- v5 ---
    _EMPTY_TEXT = {
        "Active": "Nothing is downloading right now.",
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
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _apply_toolbar_text(self):
        extra = 262 if getattr(self, "shell", None) else 0   # the shell eats width
        compact = (self.width() - extra) < 1240
        for btn, icon, label in getattr(self, "_tb", []):
            btn.setText(icon if compact else f"{icon}  {label}")
            btn.setToolTip(label)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_toolbar_text()

    def _restore_header_state(self):
        try:
            raw = _read_gui_state().get("table_header")
            if not raw:
                return
            hh = self.table.horizontalHeader()
            if hh.restoreState(QByteArray(base64.b64decode(raw))):
                hh.setSectionResizeMode(0, QHeaderView.Stretch)
                for c in range(1, len(self.model.HEADERS)):
                    hh.setSectionResizeMode(c, QHeaderView.Interactive)
        except Exception:
            pass

    def _save_header_state(self):
        try:
            hh = self.table.horizontalHeader()
            _write_gui_state({"table_header": base64.b64encode(
                bytes(hh.saveState())).decode("ascii")})
        except Exception:
            pass

    def _update_details(self):
        """One-line-per-fact panel under the table for the selected row."""
        try:
            rows = self._selected_rows()
        except Exception:
            rows = []
        if not rows:
            self.details.setVisible(False)
            return
        if len(rows) > 1:
            self.details.setText(f"{len(rows)} downloads selected")
            self.details.setVisible(True)
            return
        j = rows[0]
        e = _html.escape
        site = j.get("source_site") or ""
        lines = [f"<b>{e(str(j.get('filename') or ''))}</b>"
                 + (f" &nbsp;\u00b7&nbsp; {e(site)}" if site else "")
                 + f" &nbsp;\u00b7&nbsp; {e(str(j.get('status') or ''))}"]
        retry = _retry_text(j)
        if retry:
            lines.append(f"<span style='color:{WARN}'>Auto-retry: {e(retry)}</span>")
        if j.get("error"):
            lines.append(f"<span style='color:{DANGER}'>{e(str(j['error'])[:600])}</span>")
        lines.append(f"<span style='color:{MUTED}'>{e(str(j.get('url') or ''))}</span>")
        try:
            lines.append(f"<span style='color:{MUTED}'>{e(str(_path_for(j)))}</span>")
        except Exception:
            pass
        self.details.setText("<br>".join(lines))
        self.details.setVisible(True)

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
        self._update_tag = tag
        self.update_btn.setText(f"\u2b06 Update {tag} available")
        self.update_btn.setVisible(True)
        if self._tray and self._update_notified != tag:
            self._update_notified = tag
            self._tray.showMessage(
                "Video Grabber update",
                f"{tag} is available. Click the badge in the status bar to install.",
                QSystemTrayIcon.Information, 5000)

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
        self.sidebar.set_counts(_sidebar_counts(items))
        self._gui_settings = settings
        self._track_batch(items)
        # 11.3: re-read every tick — a Settings-dialog toggle propagates
        # within ~1s; nothing is cached from startup.
        self._gui_settings = settings
        self.animations_enabled = bool(settings.get("animations_enabled", True))
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
        # Tray icon reflects download activity (cached icon swap, ~1/sec).
        if getattr(self, "_tray", None):
            active = any(j.get("status") == "downloading" for j in self.all_items)
            self._tray.setIcon(_make_tray_icon(active))
        # Snapshot the selection before the model reset wipes it, then restore.
        selected_ids = {j["id"] for j in self._selected_rows()}
        self.apply_filter(self.search.text())
        if selected_ids:
            sm = self.table.selectionModel()
            if sm is not None:
                for r, j in enumerate(self.model.items):
                    if j["id"] in selected_ids:
                        sm.select(self.model.index(r, 0),
                                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
        # 12.1: rows inserted/removed -> fade the viewport in (150 ms).
        visible_ids = frozenset(j["id"] for j in self.model.items)
        if visible_ids != getattr(self, "_last_visible_ids", None):
            self._last_visible_ids = visible_ids
            _fade_widget(self.table.viewport(), self.animations_enabled)
        self._update_details()

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
            if lines:
                self.log.setText(lines[-1])
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
        """Run the updater on a background thread. On success it stages the
        new app zip and hands off to updater.bat, which finishes the swap
        after this process exits. Only works in the frozen (installed)
        build; the source run just tells the user to use git."""
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self, "Check for updates",
                "Updates only apply to the installed (exe) build.\n"
                "Source runs should pull from git instead.")
            return
        try:
            from updater import apply_update
        except Exception as e:
            QMessageBox.warning(self, "Check for updates",
                                f"Updater module not available:\n{e}")
            return

        self.status_label.setText("● Checking for updates…")
        self.status_label.setStyleSheet(f"color: {MUTED}; padding-left: 10px;")

        def _do():
            try:
                staged = apply_update()
                self._update_result_signal.emit({"ok": True, "staged": bool(staged)})
            except Exception as e:
                self._update_result_signal.emit({"ok": False, "error": str(e)})

        threading.Thread(target=_do, daemon=True).start()

    def _on_update_result(self, payload):
        """GUI-thread slot for the update check result."""
        # Restore the status label to whatever refresh() would have set.
        try:
            self.refresh()
        except Exception:
            pass
        if not payload.get("ok"):
            QMessageBox.warning(self, "Update check failed",
                                payload.get("error") or "Unknown error")
            return
        if not payload.get("staged"):
            QMessageBox.information(
                self, "Check for updates",
                f"You're already on the latest version.\n\nCurrent: v{DISPLAY_VERSION}")
            return
        # Update staged — the bat is already waiting for us to exit.
        answer = QMessageBox.question(
            self, "Update ready",
            "An update has been downloaded and is ready to install.\n\n"
            "The app needs to close briefly while the files are swapped, "
            "then it will reopen automatically.\n\n"
            "Restart now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            # Bypass closeEvent's tray-intercept so the process actually
            # exits — updater.bat is polling for exactly that.
            self._quitting = True
            if self._tray:
                self._tray.hide()
            QApplication.instance().quit()
        else:
            QMessageBox.information(
                self, "Update ready",
                "The update will apply the next time you close the app.")

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
        old = self.takeCentralWidget()
        if old is not None and old is not self._root:
            old.layout().removeWidget(self._root)
            self._root.setParent(None)
            old.deleteLater()
        if st.get("shell_enabled"):
            from gui_shell import ShellFrame
            self.shell = ShellFrame(self._root, st.get("shell_color", "cream"))
            self.shell.dpad.connect(self._shell_dpad)
            self.shell.button.connect(self._shell_button)
            self.setCentralWidget(self.shell)
        else:
            self.shell = None
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
        elif self._selected_rows():
            self._selected("resume" if name == "a" else "pause")

    def toggle_queue(self):
        """Flip the dispatcher's queue_running flag via /settings."""
        running = not bool(load_settings().get("queue_running", True))
        if self.api:
            try:
                self.api.save_setting("queue_running", running)
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
                entry[1], entry[2] = ("⏸", "Pause Queue") if running else ("⏵", "Start Queue")
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
        self.model.update_items(src)
        self._update_empty_state()

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
        a_remove = m.addAction("Remove")
        a_copy_file = m.addAction("Copy file")
        a_copy_url = m.addAction("Copy URL")
        a_props = m.addAction("Properties")
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
        if chosen == a_open:
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
    _app.setFont(QFont("Segoe UI", 10))
    _app.setQuitOnLastWindowClosed(False)
    _window = MainWindow()
    _window.show()
    if new_job_hook is not None:
        new_job_hook(_window.new_job_hook())
    if show_dialog_hook is not None:
        show_dialog_hook(_window.show_dialog_hook())
    return _app.exec()