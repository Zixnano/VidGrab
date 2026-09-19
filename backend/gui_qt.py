"""
gui_qt.py — modern PySide6 frontend for Video Grabber.

Talks to the local Flask backend on 127.0.0.1:5757. Reads the pairing token
from settings.json. Imported and launched by server.py's main().
"""

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import requests
from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, Qt, QTimer, QThread, Signal, QMimeData, QUrl,
    QItemSelectionModel, QPropertyAnimation, QEasingCurve, QAbstractAnimation,
    QElapsedTimer,
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
)

BACKEND_BASE = "http://127.0.0.1:5757"
HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"

# Palette-derived (single source of truth: palette.py).
from palette import resolve_palette
from settings import CATEGORIES, APP_VERSION  # single source of truth: settings.py

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


def _make_tray_icon(active: bool):
    """Green dot while downloading, grey when idle — like IDM's tray."""
    key = "active" if active else "idle"
    if key in _TRAY_ICONS:
        return _TRAY_ICONS[key]
    color = QColor(ACCENT if active else "#666666")
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
        "downloading": "#4caf50", "done": "#4caf50", "paused": "#ffca28",
        "stopped": "#ef5350", "error": "#ef5350", "queued": "#9e9e9e",
        "skipped": "#607d8b",
    }
    color = QColor(colors.get(key, "#9e9e9e"))
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


def load_settings():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


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
        r = self.session.request(method, self.base + path, headers=headers,
                                 timeout=5, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def reload_token(self):
        self.token = load_token()

    def jobs(self):
        data = self._req("GET", "/jobs") or {}
        return list(data.values())

    def add(self, url, filename=None, category=None, description=None,
            format_id=None, target_format=None, resolution=None, multi=False,
            download_playlist=False):
        body = {"url": url}
        if filename:
            body["filename"] = filename
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

    def save_setting(self, key, value):
        # Errors propagate: callers (toggle_queue, SettingsDialog._save)
        # show them; swallowing here made those handlers dead code.
        self._req("POST", "/settings", json={key: value})


class DownloadModel(QAbstractTableModel):
    HEADERS = ["Name", "Size", "Progress", "Speed", "Status", "Category", "Completed"]

    def __init__(self, parent_window=None):
        super().__init__()
        self.items = []
        self.parent_window = parent_window

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

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
        if role == Qt.BackgroundRole:
            pw = self.parent_window
            fade = getattr(pw, "_recently_done", {}).get(j.get("id"), 0) if pw else 0
            if fade > 0:
                flash = QColor("#173c2a")
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
                    return QColor("#607d8b")
            if col in (1, 3, 6):
                return QColor("#b8c4cf")
            if col == 5:
                return QColor("#a9b7c5")
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
        if index.isValid():
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
    name = job["filename"]
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return cat_dir / name


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
        self._shown = {}  # row -> [from_value, to_value, QElapsedTimer]
        self.animations_enabled = True  # 12.5: refreshed by MainWindow

    def _animated_pct(self, row, target):
        entry = self._shown.get(row)
        if entry is None:
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
        pct = self._animated_pct(index.row(), _progress_pct(job))
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
        # Task 3: this dialog is triggered from the browser extension, often
        # while some other window has focus — without this it can open
        # behind everything and look like nothing happened.
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.url = QLineEdit(prefill_url)
        self.url.setPlaceholderText("https://example.com/file.mp4")
        self.url.editingFinished.connect(self._probe)
        form.addRow("URL", self.url)
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
        self.playlist_checkbox = QCheckBox("Download entire playlist")
        self.playlist_checkbox.setVisible(False)
        form.addRow("", self.playlist_checkbox)
        self.url.textChanged.connect(self._update_playlist_visibility)
        self._update_playlist_visibility()
        # Pill-initiated "Download entire playlist" pre-checks the box so
        # the user's pill choice survives into the dialog.
        if self._prefill_download_playlist and self.playlist_checkbox.isVisible():
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
        self.raise_()
        self.activateWindow()

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
            self._head_ready.emit((seq, {"size": size, "free": free}))

        threading.Thread(target=worker, daemon=True).start()
        self._probe_formats(url)

    def _on_head_ready(self, payload):
        seq, data = payload
        if seq != self._head_seq:
            return  # stale response for an older URL
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
                "download_playlist": (self.playlist_checkbox.isVisible()
                                      and self.playlist_checkbox.isChecked()),
                "skip_dialog": self.skip_dialog.isChecked(),
            })
        return out


class SettingsDialog(QDialog):
    """Tabbed settings editor — POSTs each value to /settings on Save."""

    PRESET_LABELS = [("amoled_black", "AMOLED Black"),
                      ("dark_gray", "Dark Gray"),
                      ("high_contrast", "High Contrast")]
    ACCENT_SWATCHES = ["#26c6da", "#7c4dff", "#66bb6a", "#ffa726",
                       "#ef5350", "#42a5f5", "#ec407a"]
    TOKEN_ORDER = ("bg_base", "bg_panel", "bg_elevated", "border", "border_hi",
                   "text", "text_muted", "text_dim", "accent", "accent_dim",
                   "success", "warning", "error", "info")
    TOKEN_LABELS = {
        "bg_base": "Main background", "bg_panel": "Panels",
        "bg_elevated": "Dialogs / dropdowns", "border": "Borders",
        "border_hi": "Borders (hover)", "text": "Main text",
        "text_muted": "Secondary text", "text_dim": "Faint text",
        "accent": "Accent (fine-tune)", "accent_dim": "Accent (pressed)",
        "success": "Success", "warning": "Warning", "error": "Error", "info": "Info",
    }

    def __init__(self, parent=None, api=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        s = load_settings()
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

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
        self.accent = s.get("accent", "#26c6da")
        self._theme_tokens = dict(s.get("theme_tokens") or {})
        self.animations_enabled = QCheckBox("Enable animations")
        self.animations_enabled.setChecked(bool(s.get("animations_enabled", True)))
        gl.addRow(self.animations_enabled)
        tabs.addTab(g, "General")

        # ---- Theme tab — redesigned to be approachable, not a raw token
        # editor. Preset + accent + a real live preview up front; the old
        # 14-row raw-hex table is still here for power users, just hidden
        # behind a checkbox instead of being the first thing anyone sees. ----
        tw = QWidget()
        tl = QVBoxLayout(tw)
        tl.setSpacing(14)

        preset_label = QLabel("Preset")
        preset_label.setObjectName("SectionLabel")
        tl.addWidget(preset_label)
        self.theme_preset = QComboBox()
        for key, label in self.PRESET_LABELS:
            self.theme_preset.addItem(label, userData=key)
        preset = s.get("theme_preset", "amoled_black")
        if preset not in dict(self.PRESET_LABELS):
            preset = "amoled_black"
        idx = self.theme_preset.findData(preset)
        if idx >= 0:
            self.theme_preset.setCurrentIndex(idx)
        self.theme_preset.currentIndexChanged.connect(lambda _i: self._refresh_theme_preview())
        tl.addWidget(self.theme_preset)

        accent_label = QLabel("Accent color")
        accent_label.setObjectName("SectionLabel")
        tl.addWidget(accent_label)
        accent_row = QHBoxLayout()
        self._accent_swatches = []
        for hexcolor in self.ACCENT_SWATCHES:
            b = QPushButton()
            b.setFixedSize(28, 28)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(hexcolor)
            b.clicked.connect(lambda _=False, c=hexcolor: self._set_accent(c))
            accent_row.addWidget(b)
            self._accent_swatches.append((hexcolor, b))
        self.accent_btn = QPushButton(f"Custom…  {self.accent}")
        self.accent_btn.clicked.connect(self._pick_accent)
        accent_row.addWidget(self.accent_btn)
        accent_row.addStretch(1)
        tl.addLayout(accent_row)
        self._restyle_accent_swatches()

        preview_label = QLabel("Preview")
        preview_label.setObjectName("SectionLabel")
        tl.addWidget(preview_label)
        self.theme_preview_frame = QFrame()
        self.theme_preview_frame.setObjectName("ThemePreviewFrame")
        pf = QVBoxLayout(self.theme_preview_frame)
        pf.setContentsMargins(16, 16, 16, 16)
        pf.setSpacing(10)
        ptitle = QLabel("Video Grabber")
        ptitle.setObjectName("PreviewTitle")
        pf.addWidget(ptitle)
        psub = QLabel("This is roughly what your download list will look like")
        psub.setObjectName("PreviewMuted")
        pf.addWidget(psub)
        pbtn_row = QHBoxLayout()
        pbtn1 = QPushButton("Download")
        pbtn1.setObjectName("PreviewPrimary")
        pbtn2 = QPushButton("Cancel")
        pbtn2.setObjectName("PreviewSecondary")
        pbtn_row.addWidget(pbtn1)
        pbtn_row.addWidget(pbtn2)
        pbtn_row.addStretch(1)
        pf.addLayout(pbtn_row)
        self.theme_preview_bar = QProgressBar()
        self.theme_preview_bar.setRange(0, 100)
        self.theme_preview_bar.setValue(64)
        pf.addWidget(self.theme_preview_bar)
        tl.addWidget(self.theme_preview_frame)

        self.show_advanced_theme = QCheckBox("Show advanced color overrides")
        self.show_advanced_theme.setChecked(bool(s.get("theme_tokens")))
        tl.addWidget(self.show_advanced_theme)
        self.tok_table = QTableWidget(0, 3)
        self.tok_table.setHorizontalHeaderLabels(["Setting", "Color", ""])
        self.tok_table.horizontalHeader().setStretchLastSection(True)
        self.tok_table.verticalHeader().setVisible(False)
        for name in self.TOKEN_ORDER:
            self._add_token_row(name)
        self.tok_table.setVisible(self.show_advanced_theme.isChecked())
        self.show_advanced_theme.toggled.connect(self.tok_table.setVisible)
        tl.addWidget(self.tok_table)

        row = QHBoxLayout()
        exp = QPushButton("Export theme…")
        imp = QPushButton("Import theme…")
        reset_theme_btn = QPushButton("Reset to defaults")
        exp.clicked.connect(self._export_theme)
        imp.clicked.connect(self._import_theme)
        reset_theme_btn.clicked.connect(self._reset_theme_all)
        row.addWidget(exp)
        row.addWidget(imp)
        row.addWidget(reset_theme_btn)
        row.addStretch(1)
        tl.addLayout(row)
        tl.addStretch(1)
        self._refresh_theme_preview()
        tabs.addTab(tw, "Theme")

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
        tabs.addTab(c, "Connection")

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

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copy_token(self):
        QApplication.clipboard().setText(self.token_edit.text())

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

    def _add_token_row(self, name):
        from palette import resolve_palette
        base = resolve_palette({})[name]
        r = self.tok_table.rowCount()
        self.tok_table.insertRow(r)
        item = QTableWidgetItem(self.TOKEN_LABELS.get(name, name))
        item.setToolTip(name)  # raw token key, for anyone editing a theme.json by hand
        self.tok_table.setItem(r, 0, item)
        btn = QPushButton(self._theme_tokens.get(name, base))
        btn.setStyleSheet(f"background: {btn.text()}; color: #fff; border: 0;")
        btn.clicked.connect(lambda _=False, n=name, b=btn: self._pick_token_color(n, b))
        self.tok_table.setCellWidget(r, 1, btn)
        reset = QPushButton("Reset")
        reset.clicked.connect(lambda _=False, n=name, b=btn: self._reset_token(n, b))
        self.tok_table.setCellWidget(r, 2, reset)

    def _pick_token_color(self, name, btn):
        from palette import resolve_palette
        cur = QColor(self._theme_tokens.get(name, resolve_palette({})[name]))
        c = QColorDialog.getColor(cur, self, f"Color for {self.TOKEN_LABELS.get(name, name)}")
        if c.isValid():
            self._theme_tokens[name] = c.name()
            btn.setText(c.name())
            btn.setStyleSheet(f"background: {c.name()}; color: #fff; border: 0;")
            self._refresh_theme_preview()

    def _reset_token(self, name, btn):
        from palette import resolve_palette
        self._theme_tokens.pop(name, None)
        base = resolve_palette({})[name]
        btn.setText(base)
        btn.setStyleSheet(f"background: {base}; color: #fff; border: 0;")
        self._refresh_theme_preview()

    def _restyle_accent_swatches(self):
        """Highlight whichever swatch (if any) matches the current accent."""
        for hexcolor, btn in getattr(self, "_accent_swatches", []):
            selected = hexcolor.lower() == self.accent.lower()
            btn.setStyleSheet(
                f"background:{hexcolor}; border-radius:14px; "
                f"border:2px solid {'#ffffff' if selected else hexcolor};")

    def _set_accent(self, hexcolor):
        """Single entry point for changing the accent — used by swatch
        clicks, the custom color picker, and theme import, so the swatch
        highlight/preview never drift out of sync with self.accent."""
        self.accent = hexcolor
        self.accent_btn.setText(f"Custom…  {self.accent}")
        self._restyle_accent_swatches()
        self._refresh_theme_preview()

    def _reset_theme_all(self):
        """One button to get back to the app's defaults — preset, accent,
        and every advanced token override at once."""
        self._theme_tokens = {}
        self.tok_table.setRowCount(0)
        for name in self.TOKEN_ORDER:
            self._add_token_row(name)
        idx = self.theme_preset.findData("amoled_black")
        if idx >= 0:
            self.theme_preset.setCurrentIndex(idx)
        self._set_accent("#26c6da")

    def _refresh_theme_preview(self):
        """Live preview: restyle a small mock UI (title, buttons, progress
        bar) with the palette the current preset+accent+overrides would
        resolve to — not a raw QSS text dump, so it's actually readable at
        a glance."""
        from palette import resolve_palette
        tokens = resolve_palette({
            "theme_preset": self.theme_preset.currentData() or "amoled_black",
            "accent": self.accent,
            "theme_tokens": dict(self._theme_tokens),
        })
        self.theme_preview_frame.setStyleSheet(f"""
        #ThemePreviewFrame {{
            background: {tokens['bg_panel']}; border: 1px solid {tokens['border']};
            border-radius: {tokens['radius']};
        }}
        #ThemePreviewFrame QLabel {{ background: transparent; }}
        #ThemePreviewFrame QLabel#PreviewTitle {{
            color: {tokens['text']}; font-weight: 600; font-size: 14px;
        }}
        #ThemePreviewFrame QLabel#PreviewMuted {{
            color: {tokens['text_muted']}; font-size: 11px;
        }}
        #ThemePreviewFrame QPushButton#PreviewPrimary {{
            background: {tokens['accent']}; color: {tokens['bg_base']};
            border: none; border-radius: {tokens['radius']}; padding: 8px 16px;
            font-weight: 600;
        }}
        #ThemePreviewFrame QPushButton#PreviewSecondary {{
            background: {tokens['bg_elevated']}; color: {tokens['text']};
            border: 1px solid {tokens['border']}; border-radius: {tokens['radius']};
            padding: 8px 16px;
        }}
        #ThemePreviewFrame QProgressBar {{
            background: {tokens['bg_elevated']}; border: 1px solid {tokens['border']};
            border-radius: {tokens['radius_pill']}; text-align: center;
            color: {tokens['text_muted']};
        }}
        #ThemePreviewFrame QProgressBar::chunk {{
            background: {tokens['accent']}; border-radius: {tokens['radius_pill']};
        }}
        """)

    def _export_theme(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export theme", "theme.json",
                                              "JSON (*.json)")
        if not path:
            return
        theme = {"theme_preset": self.theme_preset.currentData() or "amoled_black",
                 "accent": self.accent,
                 "theme_tokens": dict(self._theme_tokens)}
        Path(path).write_text(json.dumps(theme, indent=2, sort_keys=True),
                              encoding="utf-8")

    def _import_theme(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import theme", "",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if data.get("theme_preset"):
                i = self.theme_preset.findData(str(data["theme_preset"]))
                if i >= 0:
                    self.theme_preset.setCurrentIndex(i)
            toks = data.get("theme_tokens")
            self._theme_tokens = dict(toks) if isinstance(toks, dict) else {}
            self.tok_table.setRowCount(0)
            for name in self.TOKEN_ORDER:
                self._add_token_row(name)
            # accent last: _set_accent() also calls _refresh_theme_preview(),
            # which needs the just-rebuilt token table / new preset in place.
            if data.get("accent"):
                self._set_accent(str(data["accent"]))
            else:
                self._refresh_theme_preview()
        except Exception as e:
            QMessageBox.warning(self, "Import failed", str(e))

    def _pick_accent(self):
        c = QColorDialog.getColor(QColor(self.accent), self, "Custom accent color")
        if c.isValid():
            self._set_accent(c.name())

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
            self.api.save_setting("accent", self.accent)
            self.api.save_setting("theme_preset", self.theme_preset.currentData() or "amoled_black")
            self.api.save_setting("theme_tokens", self._theme_tokens)
            # 13.2: rebuild QSS from the freshly saved settings and reapply
            # app-wide so theme changes land without a restart.
            from gui_style import build_qss
            QApplication.instance().setStyleSheet(build_qss(load_settings()))
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
        for name in ["All", "Finished", "Unfinished"]:
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


def _fade_dialog(dialog, enabled, duration=120):
    """Opacity fade for dialogs (Session 12.3). The dialog's own event loop
    (exec) keeps the animation running; no-op when animations are off."""
    if not enabled:
        return
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
        self.setWindowTitle(f"Video Grabber v{APP_VERSION}")
        self.resize(1200, 760)
        self.setMinimumSize(880, 560)
        self._build_ui()
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._show_dialog_signal.connect(self._on_show_dialog, Qt.QueuedConnection)
        self._apply_styles()
        self._build_tray()
        self._new_job_signal.connect(self._on_new_job, Qt.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start(1500)
        self.diskspace_timer = QTimer(self)
        self.diskspace_timer.timeout.connect(self.refresh_diskspace)
        self.diskspace_timer.start(15000)  # disk space changes slowly — no need to poll every second
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self._tick_fade)
        self.fade_timer.start(50)
        self.refresh()
        self.refresh_diskspace()

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
            total = len(v)
            for item in v:
                self.api.add(item["url"], category=item["category"],
                             description=item["description"],
                             format_id=item.get("format_id"),
                             target_format=item.get("target_format"),
                             resolution=item.get("resolution"),
                             download_playlist=item.get("download_playlist", False),
                             multi=total > 1)
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
        for w in (self._refresh_worker, self._logs_worker, self._diskspace_worker):
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
        for label, icon, action in [
            ("Add URL", "+", self.add_download),
            ("Batch", "☰", self.add_batch),
            ("Resume", "▶", lambda: self._selected("resume")),
            ("Pause", "Ⅱ", lambda: self._selected("pause")),
            ("Stop", "■", lambda: self._selected("stop")),
            ("Delete", "⌫", self.remove_selected),
            ("Queue", "⏯", self.toggle_queue),
            ("Settings", "⚙", self.open_settings),
        ]:
            b = QPushButton(f"{icon}  {label}")
            b.setObjectName("ToolbarButton")
            b.clicked.connect(action)
            tl.addWidget(b)
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
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 7):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.setShowGrid(False)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.doubleClicked.connect(self._on_double_click)
        cl.addWidget(self.table, 1)
        log_label = QLabel("ACTIVITY LOG")
        log_label.setObjectName("SectionLabel")
        cl.addWidget(log_label)
        self.log = QLineEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("LogLine")
        cl.addWidget(self.log)
        bl.addWidget(content, 1)
        root_layout.addWidget(body, 1)
        self.setCentralWidget(root)
        status = QStatusBar()
        status.setObjectName("BottomStatus")
        self.status_label = QLabel("● Connecting…")
        self.status_label.setObjectName("StatusLabel")
        status.addWidget(self.status_label)
        self.diskspace_label = QLabel("")
        self.diskspace_label.setObjectName("DiskSpaceLabel")
        self.diskspace_label.setToolTip("Free space on the downloads drive")
        status.addPermanentWidget(self.diskspace_label)
        status.addPermanentWidget(QLabel(f"v{APP_VERSION} · PySide6 · Flask API"))
        self.setStatusBar(status)
        menu = self.menuBar()
        menu.setNativeMenuBar(False)
        f = menu.addMenu("File")
        a = QAction("Add URL…", self); a.triggered.connect(self.add_download); f.addAction(a)
        a = QAction("Batch add…", self); a.triggered.connect(self.add_batch); f.addAction(a)
        f.addSeparator()
        a = QAction("Settings…", self); a.triggered.connect(self.open_settings); f.addAction(a)
        f.addSeparator()
        a = QAction("Exit", self); a.triggered.connect(self._real_quit); f.addAction(a)
        t = menu.addMenu("Tools")
        a = QAction("Copy pairing token", self); a.triggered.connect(self.copy_token); t.addAction(a)
        a = QAction("Reload token", self); a.triggered.connect(self.reload_token); t.addAction(a)
        a = QAction("Bandwidth profiles…", self); a.triggered.connect(self.open_bandwidth_profiles); t.addAction(a)
        t.addSeparator()
        a = QAction("Retry all failed", self); a.triggered.connect(self.retry_all_failed); t.addAction(a)
        a = QAction("Open logs folder", self); a.triggered.connect(self._open_logs_folder); t.addAction(a)
        t.addSeparator()
        a = QAction("About Video Grabber…", self); a.triggered.connect(self.show_about); t.addAction(a)

    def _apply_styles(self):
        self.setStyleSheet(f"""
        * {{ font-family: "Segoe UI"; color: {TEXT}; }}
        QMainWindow, QWidget {{ background: {BG}; }}
        QMenuBar {{ background: {PANEL}; color: {MUTED}; padding: 3px 8px; }}
        QMenuBar::item:selected {{ background: #18242e; color: {TEXT}; }}
        QMenu {{ background: #111a23; border: 1px solid #2a3b49; padding: 6px; }}
        QMenu::item {{ padding: 8px 26px 8px 12px; border-radius: 5px; }}
        QMenu::item:selected {{ background: #1b2a35; }}
        #Toolbar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
        #ToolbarTitle {{ font-size: 16px; font-weight: 600; }}
        #ToolbarButton {{
            background: transparent; border: 1px solid transparent;
            border-radius: 7px; padding: 8px 11px; color: #c5d0da;
        }}
        #ToolbarButton:hover {{ background: #18242e; border-color: #263846; color: white; }}
        #ToolbarButton:pressed {{ background: #20313e; }}
        #Search {{
            background: #0d151d; border: 1px solid #293b49;
            border-radius: 8px; padding: 8px 12px; min-width: 200px;
        }}
        #Search:focus {{ border-color: {ACCENT}; }}
        #Sidebar {{ background: #0d151d; border-right: 1px solid {BORDER}; }}
        #Brand {{ font-size: 12px; font-weight: 700; letter-spacing: 1px; color: #dce6ed; }}
        #SectionLabel {{ color: #6f8191; font-size: 10px; font-weight: 700; letter-spacing: 1.4px; }}
        #SidebarSeparator {{ color: {BORDER}; background: {BORDER}; max-height: 1px; }}
        QToolButton[nav="true"] {{
            text-align: left; background: transparent; border: 0;
            border-radius: 6px; color: #9caebb; padding: 0 10px;
        }}
        QToolButton[nav="true"]:hover {{ background: #15212b; color: #e9f0f5; }}
        QTableView {{
            background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px;
            selection-background-color: #173c2a; selection-color: white; outline: 0;
        }}
        QHeaderView::section {{
            background: #121d27; color: #8fa1b0; border: 0;
            border-bottom: 1px solid {BORDER}; padding: 11px 10px;
            font-size: 11px; font-weight: 600;
        }}
        QTableView::item {{ border: 0; padding: 7px 8px; }}
        QTableView::item:selected {{ background: #173c2a; }}
        #LogLine {{
            background: #0c141c; border: 1px solid {BORDER}; border-radius: 7px;
            padding: 8px 12px; color: #8fa1b0; font-family: "Consolas";
            font-size: 11px;
        }}
        #BottomStatus {{ background: #0a1016; border-top: 1px solid {BORDER}; color: #718493; }}
        #StatusLabel {{ color: {SUCCESS}; padding-left: 10px; }}
        QDialog {{ background: {PANEL}; }}
        QLineEdit, QComboBox {{
            background: #0d151d; border: 1px solid #293b49;
            border-radius: 6px; padding: 7px;
        }}
        QPushButton {{ background: #18242e; border: 1px solid #2b3d4b;
            border-radius: 6px; padding: 7px 13px; }}
        QPushButton:hover {{ background: #20313e; }}
        """)

    def _tick_animations(self):
        if not self.animations_enabled:
            return
        try:
            if any(j.get("status") == "downloading" for j in self.all_items):
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
            f"<p style='color:{MUTED};margin-top:0;'>Version {APP_VERSION}</p>"
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

    def open_settings(self):
        try:
            dlg = SettingsDialog(self, self.api)
            _fade_dialog(dlg, self.animations_enabled)
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Settings error", str(e))

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
        self.queue_btn.setText("⏸  Pause Queue" if running else "⏵  Start Queue")
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
        if f == "Finished":
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
                return 0
            src = sorted(src, key=sort_key, reverse=not self._sort_ascending)
        else:
            src = sorted(src, key=lambda x: -x.get("created_ts", 0))
        self.model.set_items(src)

    def select_category(self, name):
        self.current_filter = name
        for n, b in self.sidebar.buttons.items():
            b.setStyleSheet(
                f"QToolButton {{ background: {'#173c2a' if n == name else 'transparent'}; "
                f"color: {'#ffffff' if n == name else '#9caebb'}; }}"
            )
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
        cat_menu = m.addMenu("Move to Category")
        cat_actions = {cat_menu.addAction(c): c for c in CATEGORIES}
        m.addSeparator()
        conv_menu = m.addMenu("Convert to")
        conv_actions = {conv_menu.addAction(f): f for f in ["mp4", "mkv", "mp3", "wav", "m4a", "flac", "opus"]}
        m.addSeparator()
        a_remove = m.addAction("Remove")
        a_copy_url = m.addAction("Copy URL")
        a_props = m.addAction("Properties")
        a_open.setEnabled(j["status"] == "done")
        a_open_folder.setEnabled(j["status"] == "done")
        a_open_location.setEnabled(j["status"] == "done")
        a_resume.setEnabled(j["status"] in ("paused", "stopped"))
        a_stop.setEnabled(j["status"] in ("downloading", "queued"))
        a_redl.setEnabled(j["status"] in ("done", "error", "stopped"))
        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
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
        elif chosen == a_copy_url:
            QApplication.clipboard().setText(j.get("url", ""))
        elif chosen == a_props:
            # Re-fetch at click time — the row object captured when the menu
            # opened can be stale if a refresh fired while the menu was up.
            fresh = next((x for x in self.model.items
                          if x["id"] == self._current_ctx), j)
            self._show_props(fresh)

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
