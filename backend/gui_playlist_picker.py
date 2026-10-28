"""The playlist checklist dialog."""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

import gui_palette
from gui_common import _bring_to_front
from gui_api import ProbeError
from gui_fit import install_fit


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
        install_fit(self, 720, 560, 480, 340)
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
        self.header.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        self.status.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        self.header.setStyleSheet(f"color: {gui_palette.DANGER if error else gui_palette.MUTED};")

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
