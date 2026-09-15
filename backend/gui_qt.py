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
    QAbstractTableModel, QModelIndex, Qt, QTimer, Signal, QMimeData, QUrl,
    QItemSelectionModel,
)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame,
    QLabel, QPushButton, QToolButton, QLineEdit, QTableView, QHeaderView,
    QMenu, QMessageBox, QDialog, QDialogButtonBox, QFormLayout, QComboBox,
    QStatusBar, QSizePolicy, QAbstractItemView, QStyledItemDelegate,
    QInputDialog, QFileDialog, QCheckBox, QSystemTrayIcon, QStyle,
    QSpinBox, QListWidget, QListWidgetItem, QTabWidget,
)

BACKEND_BASE = "http://127.0.0.1:5757"
HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"

ACCENT = "#4caf50"
BG = "#0b1117"
PANEL = "#101922"
BORDER = "#22313f"
TEXT = "#e6edf3"
MUTED = "#8b9aaa"
DANGER = "#ef5350"
WARN = "#ffca28"

CATEGORIES = ["Video", "Music", "Compressed", "Documents", "Programs", "Other"]

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


def fmt_ts(ts):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M:%S")
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
            format_id=None):
        body = {"url": url}
        if filename:
            body["filename"] = filename
        if category:
            body["category"] = category
        if description:
            body["description"] = description
        if format_id:
            body["format_id"] = format_id
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

    def logs(self):
        data = self._req("GET", "/logs") or {}
        return data.get("logs", [])

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
            if j.get("converting"):
                pct = f"conv {j.get('conversion_progress', 0)}%"
            elif j.get("size_total"):
                pct = f"{(j.get('size_done', 0) / j['size_total'] * 100):.0f}%"
            elif j["status"] == "done":
                pct = "100%"
            status = (f"converting → {j['converting']}" if j.get("converting")
                      else j["status"])
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
                if j.get("converting"):
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
            return None
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


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() != 2:
            super().paint(painter, option, index)
            return
        value = index.data(Qt.DisplayRole) or "0%"
        # "conv 37%" (conversion progress) and plain "37%" both parse here.
        m = re.search(r"(\d+)", str(value))
        pct = float(m.group(1)) if m else 0
        converting = str(value).startswith("conv")
        painter.save()
        rect = option.rect.adjusted(6, 14, -6, -14)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#1d2a35"))
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


class AddDownloadDialog(QDialog):
    # Carries (probe_seq, response_dict) from the background probe thread —
    # Qt widgets are only touched in the slot, never in the thread.
    _formats_ready = Signal(object)

    def __init__(self, parent=None, api=None, prefill_url=""):
        super().__init__(parent)
        self.api = api
        self._probe_seq = 0
        self.setWindowTitle("Download File Info")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.url = QLineEdit(prefill_url)
        self.url.setPlaceholderText("https://example.com/file.mp4")
        self.url.editingFinished.connect(self._probe)
        form.addRow("URL", self.url)
        self.quality = QComboBox()
        self.quality.addItem("Best available")
        form.addRow("Quality", self.quality)
        self._formats_ready.connect(self._on_formats)
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
        self.remember.setText(f"Remember this path for “{self.category.currentText()}”")
        self.category.currentTextChanged.connect(self._update_remember_label)
        if prefill_url:
            self._probe()

    def _update_remember_label(self, text):
        self.remember.setText(f"Remember this path for “{text}”")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save folder", self.save_path.text())
        if d:
            self.save_path.setText(d)

    def _probe(self):
        url = self.url.text().strip()
        if not url or not self.api:
            return
        try:
            r = self.api.probe_head(url)
            size = r.get("size", 0)
            self.size_label.setText(fmt_bytes(size) if size else "unknown")
        except Exception:
            self.size_label.setText("unknown")
        self._probe_formats(url)

    def _probe_formats(self, url):
        """Ask the backend which formats exist, off the UI thread. A seq
        counter drops stale responses when the URL changes quickly."""
        self._probe_seq += 1
        seq = self._probe_seq
        self.quality.clear()
        self.quality.addItem("Loading formats…")
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
        self.quality.addItem("Best available")
        for f in data.get("formats") or []:
            self.quality.addItem(self._format_label(f), f.get("format_id"))
        self.quality.setEnabled(True)

    @staticmethod
    def _format_label(f):
        res = f.get("resolution") or ""
        ext = f.get("ext") or ""
        vc = f.get("vcodec") or "none"
        ac = f.get("acodec") or "none"
        size = fmt_bytes(f.get("filesize")) if f.get("filesize") else "?"
        kind = "audio only" if vc == "none" else f"{vc}/{ac}"
        return " · ".join(p for p in (res, ext, kind, size) if p)

    def values(self):
        return {
            "url": self.url.text().strip(),
            "category": self.category.currentText(),
            "save_path": self.save_path.text().strip(),
            "remember": self.remember.isChecked(),
            "description": self.description.text().strip(),
            "format_id": self.quality.currentData(),
        }


class SettingsDialog(QDialog):
    """Tabbed settings editor — POSTs each value to /settings on Save."""

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
        tabs.addTab(g, "General")

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
        ftl.addRow("Check to DISABLE auto-capture for that type:")
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

class MainWindow(QMainWindow):
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
        self._sort_ascending = True
        self.setWindowTitle("Video Grabber")
        self.resize(1200, 760)
        self.setMinimumSize(880, 560)
        self._build_ui()
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._apply_styles()
        self._build_tray()
        self._new_job_signal.connect(self._on_new_job, Qt.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start(1500)
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self._tick_fade)
        self.fade_timer.start(50)
        self.refresh()

    def new_job_hook(self):
        return self._new_job_signal.emit

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
        status.addPermanentWidget(QLabel("PySide6 · Flask API"))
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
        #StatusLabel {{ color: {ACCENT}; padding-left: 10px; }}
        QDialog {{ background: {PANEL}; }}
        QLineEdit, QComboBox {{
            background: #0d151d; border: 1px solid #293b49;
            border-radius: 6px; padding: 7px;
        }}
        QPushButton {{ background: #18242e; border: 1px solid #2b3d4b;
            border-radius: 6px; padding: 7px 13px; }}
        QPushButton:hover {{ background: #20313e; }}
        """)

    def refresh(self):
        try:
            self.all_items = self.api.jobs()
            self.status_label.setText("● Connected")
            self.status_label.setStyleSheet(f"color: {ACCENT}; padding-left: 10px;")
        except Exception:
            self.status_label.setText("● Backend offline")
            self.status_label.setStyleSheet(f"color: {DANGER}; padding-left: 10px;")
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
        try:
            lines = self.api.logs()
            if lines:
                self.log.setText(lines[-1])
        except Exception:
            pass

    def _on_new_job(self, job):
        if not load_settings().get("force_on_top", True):
            return
        # Already focused — raising would only cause a flicker.
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            return
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        # Only flip the topmost flag if it isn't set — toggling a window
        # flag forces a hide/show cycle on Windows (the batch flicker).
        if not (self.windowFlags() & Qt.WindowStaysOnTopHint):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
            QTimer.singleShot(400, self._clear_topmost)

    def _clear_topmost(self):
        if self.windowFlags() & Qt.WindowStaysOnTopHint:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            self.show()

    def open_bandwidth_profiles(self):
        try:
            BandwidthProfilesDialog(self, self.api).exec()
        except Exception as e:
            QMessageBox.critical(self, "Bandwidth profiles error", str(e))

    def open_settings(self):
        try:
            SettingsDialog(self, self.api).exec()
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
        result = d.exec()
        if result not in (QDialog.Accepted, 2):
            return
        v = d.values()
        if not v["url"]:
            return
        if v["remember"] and v["save_path"]:
            try:
                self.api.save_setting("per_category_dirs",
                                      {v["category"]: v["save_path"]})
            except Exception:
                pass
        try:
            self.api.add(v["url"], category=v["category"],
                         description=v["description"], format_id=v.get("format_id"))
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
        m.addSeparator()
        a_resume = m.addAction("Resume")
        a_stop = m.addAction("Stop")
        a_redl = m.addAction("Redownload")
        m.addSeparator()
        cat_menu = m.addMenu("Move to Category")
        cat_actions = {cat_menu.addAction(c): c for c in CATEGORIES}
        m.addSeparator()
        conv_menu = m.addMenu("Convert to")
        conv_actions = {conv_menu.addAction(f): f for f in ["mp4", "mkv", "mp3", "wav"]}
        m.addSeparator()
        a_remove = m.addAction("Remove")
        a_props = m.addAction("Properties")
        a_open.setEnabled(j["status"] == "done")
        a_open_folder.setEnabled(j["status"] == "done")
        a_resume.setEnabled(j["status"] in ("paused", "stopped"))
        a_stop.setEnabled(j["status"] in ("downloading", "queued"))
        a_redl.setEnabled(j["status"] in ("done", "error", "stopped"))
        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == a_open:
            self._ctx_open()
        elif chosen == a_open_folder:
            self._ctx_open_folder()
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
            self.api.resume(jid)
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
            target = path.parent if folder else path
            if not target.exists():
                QMessageBox.warning(
                    self, "File Not Found",
                    f"File not found, expected at:\n{target}"
                )
                continue
            try:
                target_str = str(target.resolve())
                if sys.platform == "win32":
                    if folder and path.exists():
                        # explorer wants /select,<path> as ONE argument —
                        # as a list it silently opens the wrong folder.
                        # String form, no shell=True.
                        subprocess.Popen(f'explorer /select,"{target_str}"')
                    else:
                        os.startfile(target_str)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", target_str])
                else:
                    subprocess.Popen(["xdg-open", target_str])
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
        text = "\n".join([
            f"Name:     {j['filename']}",
            f"URL:      {j['url']}",
            f"Type:     {j['type']}",
            f"Status:   {j['status']}",
            f"Category: {j['category']}",
            f"Size:     {fmt_bytes(j.get('size_total'))}",
            f"Done:     {fmt_bytes(j.get('size_done'))}",
            f"Completed:{fmt_ts(j.get('completed_ts'))}",
        ])
        QMessageBox.information(self, "Properties", text)


_app = None
_window = None


def launch_gui(new_job_hook=None, home_dir=None):
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
    _app.setApplicationName("Video Grabber")
    _app.setFont(QFont("Segoe UI", 10))
    _app.setQuitOnLastWindowClosed(False)
    _window = MainWindow()
    _window.show()
    if new_job_hook is not None:
        new_job_hook(_window.new_job_hook())
    return _app.exec()