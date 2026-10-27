"""The Add URL dialog."""

import os
import re
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from settings import CATEGORIES

import gui_palette
import gui_common
from gui_common import _bring_to_front, fmt_bytes


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
        self.dup_label.setStyleSheet(f"color: {gui_palette.WARN};")
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
        self.queue_preview.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        self.save_path = QLineEdit(str(gui_common.HOME))
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
            self.size_label.setStyleSheet(f"color: {gui_palette.DANGER};")
            self.size_label.setToolTip(
                "This file is larger than your available free space — "
                "the download will likely fail partway through.")
        elif free and size >= free * 0.9:
            self.size_label.setStyleSheet(f"color: {gui_palette.WARN};")
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
