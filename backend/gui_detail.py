"""Detail pane shown under the download table when exactly one row is
selected: thumbnail, title, progress, speed / remaining / downloaded, action
buttons, and General / Progress / Connections / Log tabs.

Pure UI. MainWindow turns a job dict into the plain `info` dict that
DetailPane.set_job() takes (ready-made strings, no backend calls here).
Thumbnails come from ThumbLoader: images are scaled directly, videos get one
frame from ffmpeg (if it can be found) on a background thread."""
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QScrollArea, 
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

try:    # inline playback is optional; without it a click opens the file externally
    from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_MM = True
except Exception:
    HAVE_MM = False

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
THUMB_W, THUMB_H = 168, 104


def find_ffmpeg(extra_dir=None):
    """ffmpeg executable path: the app's own folder first, then PATH."""
    if extra_dir:
        for name in ("ffmpeg.exe", "ffmpeg"):
            cand = Path(str(extra_dir)) / name
            if cand.exists():
                return str(cand)
    return shutil.which("ffmpeg")


class ThumbLoader(QObject):
    """Makes (and caches) one thumbnail per finished download, off the GUI
    thread. `ready(job_id, image_path, duration_seconds)`: image_path is ''
    when nothing could be made, duration is 0 when unknown."""
    ready = Signal(str, str, float)

    def __init__(self, cache_dir, parent=None):
        super().__init__(parent)
        self._dir = Path(cache_dir)
        self._cache = {}
        self._busy = set()

    def request(self, job_id, path, ffmpeg_dir=None):
        job_id = str(job_id)
        if job_id in self._cache:
            self.ready.emit(job_id, *self._cache[job_id])
            return
        if job_id in self._busy:
            return
        self._busy.add(job_id)
        threading.Thread(target=self._work, args=(job_id, Path(path), ffmpeg_dir),
                         daemon=True).start()

    def _work(self, job_id, path, ffmpeg_dir):
        thumb, dur = "", 0.0
        try:
            ext = path.suffix.lower()
            if path.exists() and ext in IMAGE_EXT:
                img = QImage(str(path))
                if not img.isNull():
                    self._dir.mkdir(parents=True, exist_ok=True)
                    out = self._dir / f"{job_id}.jpg"
                    img.scaledToWidth(320, Qt.SmoothTransformation).save(str(out), "JPG", 85)
                    thumb = str(out)
            elif path.exists() and ext in VIDEO_EXT:
                exe = find_ffmpeg(ffmpeg_dir)
                if exe:
                    self._dir.mkdir(parents=True, exist_ok=True)
                    out = self._dir / f"{job_id}.jpg"
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    for seek in ("1", "0"):   # clips shorter than 1 s have no frame at 1 s
                        r = subprocess.run(
                            [exe, "-hide_banner", "-y", "-ss", seek, "-i", str(path),
                             "-frames:v", "1", "-vf", "scale=320:-2", str(out)],
                            capture_output=True, text=True, timeout=30, creationflags=flags)
                        m = _DURATION.search(r.stderr or "")
                        if m and not dur:
                            dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
                        if out.exists() and out.stat().st_size > 0:
                            thumb = str(out)
                            break
        except Exception:
            thumb = ""
        self._cache[job_id] = (thumb, dur)
        self._busy.discard(job_id)
        self.ready.emit(job_id, thumb, dur)


def _fmt_clock(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _Thumb(QWidget):
    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(THUMB_W, THUMB_H)
        self._pm = None
        self._label = ""
        self._video = False
        self._dur = 0.0
        self._clickable = False

    def set_content(self, label, video, clickable):
        self._label, self._video, self._clickable = label, video, clickable
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)
        self.update()

    def set_image(self, path, duration=0.0):
        pm = QPixmap(path) if path else QPixmap()
        self._pm = pm if not pm.isNull() else None
        self._dur = duration or 0.0
        self.update()

    def clear_image(self):
        self._pm, self._dur = None, 0.0
        self.update()

    def mousePressEvent(self, e):
        if self._clickable and e.button() == Qt.LeftButton:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, e):
        # The second click of a double click arrives here instead of as a
        # press, so a double click is exactly one `clicked` plus one of these.
        if self._clickable and e.button() == Qt.LeftButton:
            self.double_clicked.emit()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        if self._pm:
            scaled = self._pm.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap(int((self.width() - scaled.width()) / 2),
                         int((self.height() - scaled.height()) / 2), scaled)
        else:
            p.fillRect(self.rect(), QColor("#1c201e"))
            p.setPen(QColor("#6f776f"))
            f = p.font()
            f.setPixelSize(15)
            f.setBold(True)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, self._label or "FILE")
        if self._video and self._pm:
            c = QPointF(self.width() / 2, self.height() / 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 140))
            p.drawEllipse(c, 18, 18)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawPolygon(QPolygonF([QPointF(c.x() - 5, c.y() - 9), QPointF(c.x() - 5, c.y() + 9),
                                     QPointF(c.x() + 10, c.y())]))
        if self._dur:
            f = p.font()
            f.setPixelSize(11)
            f.setBold(True)
            p.setFont(f)
            txt = _fmt_clock(self._dur)
            w = QFontMetrics(f).horizontalAdvance(txt) + 12
            badge = QRectF(self.width() - w - 6, self.height() - 22, w, 16)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawRoundedRect(badge, 4, 4)
            p.setPen(QColor("#ffffff"))
            p.drawText(badge, Qt.AlignCenter, txt)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 8, 8)
        p.end()


class _Elide(QLabel):
    """Single-line label that shows '...' instead of widening the pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)

    def set_full(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._fit()

    def _fit(self):
        fm = QFontMetrics(self.font())
        self.setText(fm.elidedText(self._full, Qt.ElideRight, max(20, self.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class _KVPage(QWidget):
    """Two-column key / value list that reuses its labels between updates."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(12, 10, 12, 10)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(5)
        self._grid.setColumnStretch(1, 1)
        self._rows = []
        self._extra = None

    def add_extra(self, widget):
        """A widget (e.g. Copy link) pinned to the top-right corner."""
        self._extra = widget
        self._grid.addWidget(widget, 0, 2, Qt.AlignTop | Qt.AlignRight)

    def set_rows(self, rows):
        while len(self._rows) < len(rows):
            k, v = QLabel(), QLabel()
            k.setObjectName("DetailKey")
            k.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            i = len(self._rows)
            self._grid.addWidget(k, i, 0)
            self._grid.addWidget(v, i, 1)
            self._rows.append((k, v))
        for i, (k, v) in enumerate(self._rows):
            if i < len(rows):
                key, val = rows[i][0], rows[i][1]
                color = rows[i][2] if len(rows[i]) > 2 else ""
                k.setText(key)
                v.setText(str(val) if val not in (None, "") else "\u2014")
                v.setStyleSheet(f"color: {color};" if color else "")
                k.setVisible(True)
                v.setVisible(True)
            else:
                k.setVisible(False)
                v.setVisible(False)


class _Stat(QWidget):
    def __init__(self, caption, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.value = QLabel("\u2014")
        self.value.setObjectName("StatValue")
        cap = QLabel(caption)
        cap.setObjectName("StatCaption")
        lay.addWidget(self.value)
        lay.addWidget(cap)


class DetailPane(QFrame):
    pause_clicked = Signal()
    resume_clicked = Signal()
    stop_clicked = Signal()
    open_clicked = Signal()
    folder_clicked = Signal()
    copy_clicked = Signal()
    tabs_toggled = Signal(bool)

    def __init__(self, parent=None, tabs_open=False):
        super().__init__(parent)
        self.setObjectName("DetailPane")
        self.job_id = None
        # Inline thumbnail playback (v5.5): ONE QMediaPlayer for the pane's
        # whole life, built on first use and reused for every job.
        self._player = None
        self._audio = None
        self._video_w = None
        self._path = ""
        self._playable = False
        self._muted = False
        self._click_mode = "inline"
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(14)
        self.thumb = _Thumb()
        self.thumb.clicked.connect(self._on_thumb_click)
        self.thumb.double_clicked.connect(self._on_thumb_double)
        top.addWidget(self.thumb, 0, Qt.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(5)
        self.title = _Elide()
        self.title.setObjectName("DetailTitle")
        right.addWidget(self.title)
        self.bar = QProgressBar()
        self.bar.setObjectName("DetailBar")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(12)
        right.addWidget(self.bar)
        self.sub = QLabel("")
        self.sub.setObjectName("DetailSub")
        right.addWidget(self.sub)
        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.s_speed, self.s_left, self.s_done = _Stat("Speed"), _Stat("Remaining"), _Stat("Downloaded")
        for s in (self.s_speed, self.s_left, self.s_done):
            stats.addWidget(s)
        stats.addStretch(1)
        right.addLayout(stats)
        self.note = QLabel("")
        self.note.setObjectName("DetailSub")
        self.note.setWordWrap(True)
        self.note.setVisible(False)
        right.addWidget(self.note)
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.b_pause = QPushButton("Pause")
        self.b_resume = QPushButton("Resume")
        self.b_stop = QPushButton("Stop")
        self.b_open = QPushButton("Open")
        self.b_folder = QPushButton("Open Folder")
        self.b_pause.clicked.connect(self.pause_clicked)
        self.b_resume.clicked.connect(self.resume_clicked)
        self.b_stop.clicked.connect(self.stop_clicked)
        self.b_open.clicked.connect(self.open_clicked)
        self.b_folder.clicked.connect(self.folder_clicked)
        for b in (self.b_pause, self.b_resume, self.b_stop, self.b_open, self.b_folder):
            btns.addWidget(b)
        btns.addStretch(1)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.toggle.toggled.connect(self._on_toggle)
        btns.addWidget(self.toggle)
        right.addLayout(btns)
        top.addLayout(right, 1)
        root.addLayout(top)

        self.tabs = QTabWidget()
        self.p_general, self.p_progress, self.p_conn = _KVPage(), _KVPage(), _KVPage()
        self.copy_btn = QPushButton("Copy Link")
        self.copy_btn.clicked.connect(self.copy_clicked)
        self.p_general.add_extra(self.copy_btn)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        # Batch 4 #2: the log grows with the panel (it was fixed at 110 px),
        # and each info grid scrolls instead of being squeezed when the pane
        # is short, so every row always renders in full.
        self.log_view.setMinimumHeight(80)
        self.tabs.addTab(self._scrolled(self.p_general), "General")
        self.tabs.addTab(self._scrolled(self.p_progress), "Progress")
        self.tabs.addTab(self._scrolled(self.p_conn), "Connections")
        self.tabs.addTab(self.log_view, "Log")
        self.tabs.setMinimumHeight(150)
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.tabs, 1)
        self.toggle.setChecked(bool(tabs_open))
        self._on_toggle(bool(tabs_open), emit=False)

    @staticmethod
    def _scrolled(page):
        """Wrap an info grid in a frameless scroll area."""
        page.setObjectName("Plain")
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.viewport().setObjectName("Plain")
        area.setWidget(page)
        return area

    def _on_toggle(self, on, emit=True):
        # A sensible floor for the panel: the header block alone, or the header
        # plus the tabs, so nothing is ever squashed below readable size.
        self.setMinimumHeight(340 if on else 150)
        self.tabs.setVisible(on)
        self.toggle.setText(("\u25BE " if on else "\u25B8 ") + "Details")
        if emit:
            self.tabs_toggled.emit(bool(on))

    def set_job(self, info):
        """info: plain dict built by MainWindow (see gui_qt._detail_info)."""
        changed = info.get("id") != self.job_id
        self.job_id = info.get("id")
        if changed:
            self.thumb.clear_image()
            self.stop_inline()
        self._path = info.get("path") or ""
        self._playable = bool(info.get("done") and info.get("is_video") and info.get("path_exists"))
        self.title.set_full(info.get("title", ""))
        self.bar.setValue(int(max(0.0, min(100.0, info.get("pct", 0.0))) * 10))
        self.sub.setText(info.get("progress_text", ""))
        self.s_speed.value.setText(info.get("speed") or "\u2014")
        self.s_left.value.setText(info.get("remaining") or "\u2014")
        self.s_done.value.setText(info.get("downloaded") or "\u2014")
        note, color = info.get("note", ""), info.get("note_color", "")
        self.note.setVisible(bool(note))
        self.note.setText(note)
        self.note.setStyleSheet(f"color: {color};" if color else "")
        pausable, resumable = info.get("can_pause"), info.get("can_resume")
        self.b_pause.setVisible(bool(pausable))
        self.b_resume.setVisible(bool(resumable) and not pausable)
        self.b_stop.setVisible(bool(info.get("can_stop")))
        self.b_open.setVisible(bool(info.get("done")))
        self.thumb.set_content(info.get("thumb_label", ""), bool(info.get("is_video")),
                               bool(info.get("done")))
        self.p_general.set_rows(info.get("general", []))
        self.p_progress.set_rows(info.get("progress", []))
        self.p_conn.set_rows(info.get("connections", []))

    # ---- inline playback (v5.5) ------------------------------------------
    def set_play_prefs(self, muted, mode):
        """muted: play with no audio. mode: "inline" (click plays here,
        double click opens the external player) or "external" (click opens)."""
        self._muted = bool(muted)
        self._click_mode = mode if mode in ("inline", "external") else "inline"
        if self._audio is not None:
            self._audio.setMuted(self._muted)

    def _inline_ok(self):
        return HAVE_MM and self._click_mode == "inline" and self._playable and bool(self._path)

    def _ensure_player(self):
        if self._player is not None:
            return True
        try:
            self._video_w = QVideoWidget(self.thumb)
            self._video_w.setGeometry(0, 0, THUMB_W, THUMB_H)
            self._video_w.setAttribute(Qt.WA_TransparentForMouseEvents)   # clicks reach the thumb
            self._video_w.hide()
            self._player = QMediaPlayer(self)
            # Attach audio only when the PC has an output device (creating a
            # QAudioOutput without one can crash Qt Multimedia); same guard
            # as the Media Toolkit preview.
            try:
                has_audio = (not os.environ.get("VG_TOOLKIT_NO_AUDIO")
                             and bool(QMediaDevices.audioOutputs()))
            except Exception:
                has_audio = False
            if has_audio:
                self._audio = QAudioOutput(self)
                self._audio.setVolume(0.8)
                self._audio.setMuted(self._muted)
                self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self._video_w)
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._player.errorOccurred.connect(self._on_player_error)
            return True
        except Exception:
            self._player = None
            return False

    def _toggle_inline(self):
        if not self._ensure_player():
            self.open_clicked.emit()
            return
        url = QUrl.fromLocalFile(self._path)
        st = self._player.playbackState()
        if st == QMediaPlayer.PlayingState:
            self._player.pause()
            return
        if st == QMediaPlayer.PausedState and self._player.source() == url:
            self._player.play()
            return
        self._player.setSource(url)
        if self._audio is not None:
            self._audio.setMuted(self._muted)
        self._video_w.show()
        self._video_w.raise_()
        self._player.play()

    def stop_inline(self):
        if self._player is None:
            return
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except Exception:
            pass
        if self._video_w is not None:
            self._video_w.hide()

    def _on_media_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self.stop_inline()

    def _on_player_error(self, *_):
        # Can't play it here (codec etc.): fall back to the external player.
        self.stop_inline()
        self.open_clicked.emit()

    def _on_thumb_click(self):
        if self._inline_ok():
            self._toggle_inline()
        else:
            self.open_clicked.emit()

    def _on_thumb_double(self):
        if self._inline_ok():
            self.stop_inline()
            self.open_clicked.emit()

    def hideEvent(self, e):
        self.stop_inline()
        super().hideEvent(e)

    def set_thumb(self, job_id, path, duration=0.0):
        if str(job_id) == str(self.job_id):
            self.thumb.set_image(path, duration)

    def set_log_lines(self, lines):
        text = "\n".join(lines) if lines else "No log lines for this download yet."
        if text != self.log_view.toPlainText():
            self.log_view.setPlainText(text)
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())
