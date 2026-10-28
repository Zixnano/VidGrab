"""gui_toolkit.py - Media Toolkit: a standalone visual mini-editor.

Preview a video (QMediaPlayer), pick a range on a painted timeline with two
draggable handles, then Compress, Trim or make a GIF. Exports run ffmpeg
through QProcess (never on the UI thread, with a progress bar and a safe
cancel), and the finished file is loaded straight back into the preview.

Output always lands next to the original and never overwrites it:
  <name>_compressed.mp4 / <name>_trimmed.<ext> / <name>_clip.gif
(a " (2)" is added if that name is taken).

Keys: Space play/pause, I set start, O set end, , and . step one frame.
"""
import json
from gui_fit import install_fit
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QProcess, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QMovie, QPainter, QPen, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QSizePolicy, QSlider, QStackedWidget, QTabWidget,
    QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_MM = True
except Exception:          # preview is optional; export still works
    HAVE_MM = False

MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".flv", ".wmv",
              ".ts", ".mpg", ".mpeg", ".3gp", ".ogv", ".mp3", ".m4a", ".aac",
              ".wav", ".flac", ".ogg", ".opus"}
MIN_GAP_MS = 100


# ---------------------------------------------------------------- helpers
def is_media_file(path):
    return Path(str(path)).suffix.lower() in MEDIA_EXTS


def _bin(name):
    """ffmpeg/ffprobe: bundled copy if the app found one, else PATH."""
    try:
        from engines import find_ffmpeg_dir
        d = find_ffmpeg_dir()
        if d:
            for cand in (f"{name}.exe", name):
                if (Path(d) / cand).is_file():
                    return str(Path(d) / cand)
    except Exception:
        pass
    return name


def fmt_time(ms, frac=False):
    ms = max(0, int(round(ms)))
    s, milli = divmod(ms, 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    out = f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{out}.{milli:03d}" if frac else out


def parse_time(text):
    """'90', '1:30', '00:01:30.5' -> milliseconds, or None if invalid."""
    parts = str(text or "").strip().split(":")
    if not parts or len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return int(round(secs * 1000))


def unique_out(src, suffix, ext):
    """Same folder as the source, <stem><suffix><ext>; never the source itself
    and never an existing file (adds ' (2)', ' (3)' ...)."""
    src = Path(src)
    ext = ext if ext.startswith(".") else "." + ext
    cand = src.with_name(f"{src.stem}{suffix}{ext}")
    n = 2
    while cand.exists() or cand == src:
        cand = src.with_name(f"{src.stem}{suffix} ({n}){ext}")
        n += 1
    return cand


def _theme_colors():
    d = {"accent": "#26c6da", "track": "#1a1a24", "edge": "#33333f", "text": "#e6e6eb",
         "dim": "#8a8a95"}
    try:
        from palette import resolve_palette
        from settings import STATE
        p = resolve_palette(STATE)
        d.update(accent=p["accent"], track=p["bg_elevated"], edge=p["border_hi"],
                 text=p["text"], dim=p["text_muted"])
    except Exception:
        pass
    return d


# ------------------------------------------------------------ ffprobe thread
class _ProbeThread(QThread):
    done = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(self, token, path, parent=None):
        super().__init__(parent)
        self._token, self._path = token, str(path)

    def run(self):
        try:
            cmd = [_bin("ffprobe"), "-v", "error", "-show_entries",
                   "format=duration,size,format_name:stream=codec_type,codec_name,width,"
                   "height,r_frame_rate,avg_frame_rate,duration:stream_disposition=attached_pic",
                   "-of", "json", self._path]
            kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               timeout=45, **kw)
            data = json.loads(r.stdout or "{}")
            fmt = data.get("format") or {}
            streams = data.get("streams") or []
            vid = next((s for s in streams if s.get("codec_type") == "video"
                        and not (s.get("disposition") or {}).get("attached_pic")), None)
            aud = next((s for s in streams if s.get("codec_type") == "audio"), None)
            dur = fmt.get("duration") or (vid or aud or {}).get("duration")
            dur_ms = int(float(dur) * 1000)
            if dur_ms <= 0:
                raise ValueError("no duration")
            fps = 30.0
            if vid:
                for key in ("avg_frame_rate", "r_frame_rate"):
                    num, _, den = str(vid.get(key) or "0/0").partition("/")
                    try:
                        v = float(num) / float(den or 1)
                    except (ValueError, ZeroDivisionError):
                        v = 0
                    if 1 <= v <= 240:
                        fps = v
                        break
            self.done.emit(self._token, {
                "duration_ms": dur_ms,
                "has_video": vid is not None, "has_audio": aud is not None,
                "width": int((vid or {}).get("width") or 0),
                "height": int((vid or {}).get("height") or 0),
                "fps": fps, "vcodec": (vid or {}).get("codec_name") or "",
                "size": int(fmt.get("size") or 0),
            })
        except Exception as e:
            self.failed.emit(self._token, f"{type(e).__name__}: {e}")


# ------------------------------------------------------------ painted timeline
class TimelineSlider(QSlider):
    """A QSlider whose value is the playhead (ms), painted with a highlighted
    selection range and two draggable handles (start / end)."""
    scrubbed = Signal(int)          # user moved the playhead
    startChanged = Signal(int)
    endChanged = Signal(int)

    PAD = 12
    # Batch 4 #6: a handle is grabbed within GRAB px of the centre of its
    # painted body (the old test was 9 px from the line, which is narrower than
    # the handle looks). While dragging, seeks and field updates are emitted at
    # most every EMIT_MS so the handle follows the mouse smoothly.
    GRAB = 14
    EMIT_MS = 16

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setFixedHeight(48)
        self.setMouseTracking(True)
        self._start, self._end = 0, 0
        self._show_sel = True
        self._drag = None
        self._grab_dx = 0.0          # where inside the handle the press landed
        self._pending = None         # (kind, value) waiting to be emitted
        self._last_emit = 0.0
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.timeout.connect(self._flush)
        self._c = _theme_colors()

    # -- state
    def setRangeMs(self, dur):
        self.setRange(0, max(1, int(dur)))
        self._start, self._end = 0, int(dur)
        self.update()

    def setSelection(self, s, e):
        self._start, self._end = int(s), int(e)
        self.update()

    def selection(self):
        return self._start, self._end

    def setSelectionVisible(self, on):
        self._show_sel = on
        self.update()

    # -- geometry
    def _x(self, ms):
        span = max(1, self.width() - 2 * self.PAD)
        return self.PAD + span * ms / max(1, self.maximum())

    def _ms(self, x):
        span = max(1, self.width() - 2 * self.PAD)
        return int(max(0, min(1, (x - self.PAD) / span)) * self.maximum())

    def _hit(self, x):
        if not self._show_sel:
            return "scrub"
        # The painted start handle sits just left of its line and the end
        # handle just right of its line (see paintEvent), so measure from the
        # centre of each body.
        ds = abs(x - (self._x(self._start) - 3))
        de = abs(x - (self._x(self._end) + 3))
        if min(ds, de) <= self.GRAB:
            # Handles stacked at the same spot: pick by which side the click is on.
            if ds == de:
                return "start" if x < self._x(self._start) else "end"
            return "start" if ds < de else "end"
        return "scrub"

    # -- mouse
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.isEnabled():
            return
        x = e.position().x()
        self._drag = self._hit(x)
        # Remember how far from the handle's line the press landed, so the
        # handle keeps that offset instead of snapping its line to the cursor.
        if self._drag == "start":
            self._grab_dx = x - self._x(self._start)
        elif self._drag == "end":
            self._grab_dx = x - self._x(self._end)
        else:
            self._grab_dx = 0.0
        self.setSliderDown(True)
        self._apply(x)

    def mouseMoveEvent(self, e):
        if self._drag:
            self._apply(e.position().x())
        elif self.isEnabled():
            self.setCursor(Qt.SizeHorCursor if self._hit(e.position().x()) != "scrub"
                           else Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = None
            self._emit_timer.stop()
            self._flush()                 # the final position is always delivered
            self.setSliderDown(False)

    def _flush(self):
        if self._pending is None:
            return
        kind, value = self._pending
        self._pending = None
        self._last_emit = time.monotonic()
        if kind == "start":
            self.startChanged.emit(value)
        elif kind == "end":
            self.endChanged.emit(value)
        self.scrubbed.emit(value)

    def _queue_emit(self, kind, value):
        self._pending = (kind, value)
        if time.monotonic() - self._last_emit >= self.EMIT_MS / 1000.0:
            self._flush()
        elif not self._emit_timer.isActive():
            self._emit_timer.start(self.EMIT_MS)

    def _apply(self, x):
        ms = self._ms(x - self._grab_dx)
        if self._drag == "start":
            self._start = max(0, min(ms, self._end - MIN_GAP_MS))
            self.setValue(self._start)
            self._queue_emit("start", self._start)
        elif self._drag == "end":
            self._end = min(self.maximum(), max(ms, self._start + MIN_GAP_MS))
            self.setValue(self._end)
            self._queue_emit("end", self._end)
        else:
            self.setValue(ms)
            self._queue_emit("scrub", ms)
        self.update()

    # -- paint
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._c
        cy, h = self.height() / 2, 10
        groove = QRectF(self.PAD, cy - h / 2, self.width() - 2 * self.PAD, h)
        p.setPen(QPen(QColor(c["edge"]), 1))
        p.setBrush(QColor(c["track"]))
        p.drawRoundedRect(groove, 5, 5)
        enabled = self.isEnabled() and self.maximum() > 1
        if enabled and self._show_sel:
            xs, xe = self._x(self._start), self._x(self._end)
            sel = QColor(c["accent"])
            sel.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(sel)
            p.drawRoundedRect(QRectF(xs, cy - h / 2, max(2, xe - xs), h), 5, 5)
            for x, left in ((xs, True), (xe, False)):
                r = QRectF(x - (8 if left else 2), cy - 15, 10, 30)
                p.setPen(QPen(QColor(c["text"]), 1))
                p.setBrush(QColor(c["accent"]))
                p.drawRoundedRect(r, 3, 3)
        if enabled:
            x = self._x(self.value())
            p.setPen(QPen(QColor(c["text"]), 2))
            p.drawLine(QPointF(x, 4), QPointF(x, self.height() - 4))
            p.setBrush(QColor(c["text"]))
            p.drawEllipse(QPointF(x, 5), 3.5, 3.5)
        p.end()


# ------------------------------------------------------------------- dialog
class ToolkitDialog(QDialog):
    def __init__(self, source_path=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Media Toolkit")
        self.setAcceptDrops(True)
        install_fit(self, 940, 800, 600, 440)

        self._src = None
        self._info = {}
        self._dur_ms = 0
        self._fps = 30.0
        self._token = 0
        self._probes = []
        self._proc = None
        self._out = None
        self._total_ms = 1
        self._stdout_buf = ""
        self._stderr_tail = ""
        self._cancelled = False
        self._result = None
        self._result_is_gif = False
        self._movie = None
        self._mode = "source"          # "source" | "result"
        self._play_sel = False

        self._player = None
        self._build_ui()
        self._build_player()
        self._wire_shortcuts()
        self._set_loaded(False)
        if source_path:
            QTimer.singleShot(0, lambda: self.load_source(source_path))

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.src_label = QLabel("Drop a video here or click Open file\u2026")
        self.src_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.open_btn = QPushButton("Open file\u2026")
        self.open_btn.clicked.connect(self._browse)
        top.addWidget(self.src_label, 1)
        top.addWidget(self.open_btn)
        root.addLayout(top)

        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(250)
        self.placeholder = QLabel("No video loaded")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setObjectName("EmptyState")
        self.stack.addWidget(self.placeholder)                       # 0
        if HAVE_MM:
            self.video = QVideoWidget()
            self.video.setStyleSheet("background: #000;")
            self.stack.addWidget(self.video)                         # 1
        else:
            self.video = None
            no = QLabel("Preview unavailable (Qt Multimedia missing). Export still works.")
            no.setAlignment(Qt.AlignCenter)
            no.setWordWrap(True)
            self.stack.addWidget(no)                                 # 1
        self.gif_label = QLabel()
        self.gif_label.setAlignment(Qt.AlignCenter)
        self.gif_label.setStyleSheet("background: #000;")
        self.stack.addWidget(self.gif_label)                         # 2
        root.addWidget(self.stack, 1)

        self.info_label = QLabel("")
        self.info_label.setObjectName("CardHint")
        root.addWidget(self.info_label)

        tr = QHBoxLayout()
        self.b_prev = QPushButton("\u23ee")
        self.b_prev.setToolTip("Back one frame ( , )")
        self.b_prev.clicked.connect(lambda: self._step(-1))
        self.b_play = QPushButton("\u25b6  Play")
        self.b_play.setToolTip("Play / pause (Space)")
        self.b_play.clicked.connect(self._toggle_play)
        self.b_next = QPushButton("\u23ed")
        self.b_next.setToolTip("Forward one frame ( . )")
        self.b_next.clicked.connect(lambda: self._step(1))
        self.b_playsel = QPushButton("Play Selection")
        self.b_playsel.clicked.connect(self._play_selection)
        self.mute = QCheckBox("Mute")
        self.mute.toggled.connect(lambda on: self._audio and self._audio.setMuted(on))
        self.pos_label = QLabel("00:00:00 / 00:00:00")
        for w in (self.b_prev, self.b_play, self.b_next, self.b_playsel):
            tr.addWidget(w)
        tr.addWidget(self.mute)
        tr.addStretch(1)
        tr.addWidget(self.pos_label)
        root.addLayout(tr)

        self.timeline = TimelineSlider()
        self.timeline.scrubbed.connect(self._on_scrub)
        self.timeline.startChanged.connect(lambda ms: self._sync_marker_edits())
        self.timeline.endChanged.connect(lambda ms: self._sync_marker_edits())
        root.addWidget(self.timeline)

        mk = QHBoxLayout()
        self.start_edit = QLineEdit("00:00:00.000")
        self.start_edit.setMaximumWidth(120)
        self.start_edit.editingFinished.connect(lambda: self._edit_marker("start"))
        self.b_set_start = QPushButton("Set Start")
        self.b_set_start.setToolTip("Use the current position (I)")
        self.b_set_start.clicked.connect(lambda: self._set_marker("start"))
        self.end_edit = QLineEdit("00:00:00.000")
        self.end_edit.setMaximumWidth(120)
        self.end_edit.editingFinished.connect(lambda: self._edit_marker("end"))
        self.b_set_end = QPushButton("Set End")
        self.b_set_end.setToolTip("Use the current position (O)")
        self.b_set_end.clicked.connect(lambda: self._set_marker("end"))
        self.b_reset = QPushButton("Reset Selection")
        self.b_reset.clicked.connect(self._reset_selection)
        for w in (QLabel("Start"), self.start_edit, self.b_set_start, QLabel("End"),
                  self.end_edit, self.b_set_end, self.b_reset):
            mk.addWidget(w)
        mk.addStretch(1)
        root.addLayout(mk)
        self.sel_label = QLabel("Start: 00:00:00 | End: 00:00:00 | Duration: 00:00:00")
        self.sel_label.setStyleSheet("font-weight: 600;")
        root.addWidget(self.sel_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_compress(), "Compress")
        self.tabs.addTab(self._build_trim(), "Trim")
        self.tabs.addTab(self._build_gif(), "Make GIF")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        root.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        bt = QHBoxLayout()
        self.view_orig = QPushButton("Original")
        self.view_res = QPushButton("Result")
        for b in (self.view_orig, self.view_res):
            b.setCheckable(True)
            b.setVisible(False)
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        self._view_group.addButton(self.view_orig)
        self._view_group.addButton(self.view_res)
        self.view_orig.clicked.connect(lambda: self._show_view("source"))
        self.view_res.clicked.connect(lambda: self._show_view("result"))
        self.b_folder = QPushButton("Show in folder")
        self.b_folder.setVisible(False)
        self.b_folder.clicked.connect(self._show_in_folder)
        self.b_edit_result = QPushButton("Edit result")
        self.b_edit_result.setToolTip("Load the result as the new source to keep editing")
        self.b_edit_result.setVisible(False)
        self.b_edit_result.clicked.connect(self._edit_result)
        self.b_cancel = QPushButton("Cancel export")
        self.b_cancel.setVisible(False)
        self.b_cancel.clicked.connect(self._cancel_export)
        self.b_export = QPushButton("Compress")
        self.b_export.setObjectName("primary")
        self.b_export.clicked.connect(self._start_export)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.reject)
        for w in (self.view_orig, self.view_res, self.b_folder, self.b_edit_result):
            bt.addWidget(w)
        bt.addStretch(1)
        for w in (self.b_cancel, self.b_export, b_close):
            bt.addWidget(w)
        root.addLayout(bt)

    def _build_compress(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.c_quality = QRadioButton("Quality (CRF): lower = better quality, bigger file")
        self.c_quality.setChecked(True)
        self.c_slider = QSlider(Qt.Horizontal)
        self.c_slider.setRange(18, 36)
        self.c_slider.setValue(28)
        self.c_val = QLabel("28")
        self.c_slider.valueChanged.connect(lambda v: (self.c_val.setText(str(v)),
                                                      self.c_quality.setChecked(True)))
        srow = QHBoxLayout()
        srow.addWidget(self.c_slider, 1)
        srow.addWidget(self.c_val)
        self.c_size = QRadioButton("Target size")
        self.c_mb = QDoubleSpinBox()
        self.c_mb.setRange(1, 100000)
        self.c_mb.setDecimals(1)
        self.c_mb.setValue(50)
        self.c_mb.setSuffix(" MB")
        self.c_mb.valueChanged.connect(lambda _v: (self.c_size.setChecked(True),
                                                   self._update_estimate()))
        self.c_size.toggled.connect(lambda _on: self._update_estimate())
        self.c_sel = QCheckBox("Only compress the selected range")
        self.c_sel.toggled.connect(lambda _on: self._update_estimate())
        self.c_est = QLabel("")
        self.c_est.setObjectName("CardHint")
        self.c_est.setWordWrap(True)
        for x in (self.c_quality,):
            lay.addWidget(x)
        lay.addLayout(srow)
        row2 = QHBoxLayout()
        row2.addWidget(self.c_size)
        row2.addWidget(self.c_mb)
        row2.addStretch(1)
        lay.addLayout(row2)
        lay.addWidget(self.c_sel)
        lay.addWidget(self.c_est)
        return w

    def _build_trim(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.t_exact = QCheckBox("Exact Cut (frame-accurate re-encode)")
        self.t_exact.toggled.connect(self._update_trim_hint)
        self.t_hint = QLabel("")
        self.t_hint.setObjectName("CardHint")
        self.t_hint.setWordWrap(True)
        lay.addWidget(QLabel("Cuts the Start to End range chosen on the timeline."))
        lay.addWidget(self.t_exact)
        lay.addWidget(self.t_hint)
        lay.addStretch(1)
        self._update_trim_hint()
        return w

    def _build_gif(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.g_preset = QComboBox()
        self.g_preset.addItem("Web Optimized (480p)", "web")
        self.g_preset.addItem("High Quality (1080p)", "hq")
        self.g_preset.currentIndexChanged.connect(lambda _i: self._update_gif_hint())
        self.g_hint = QLabel("")
        self.g_hint.setObjectName("CardHint")
        self.g_hint.setWordWrap(True)
        lay.addWidget(QLabel("Makes a GIF from the Start to End range on the timeline."))
        lay.addWidget(self.g_preset)
        lay.addWidget(self.g_hint)
        lay.addStretch(1)
        self._update_gif_hint()
        return w

    # ---------------------------------------------------------- player
    def _build_player(self):
        self._audio = None
        if not HAVE_MM:
            return
        self._player = QMediaPlayer(self)
        # Only attach audio when the PC has an output device; creating a
        # QAudioOutput without one can crash Qt Multimedia.
        # VG_TOOLKIT_NO_AUDIO=1 forces silent preview (headless / diagnostics).
        try:
            has_audio_dev = (not os.environ.get("VG_TOOLKIT_NO_AUDIO")
                             and bool(QMediaDevices.audioOutputs()))
        except Exception:
            has_audio_dev = False
        if has_audio_dev:
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.8)
            self._player.setAudioOutput(self._audio)
        else:
            self.mute.setEnabled(False)
            self.mute.setToolTip("No audio output device found")
        self._player.setVideoOutput(self.video)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_player_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_player_error)

    def _wire_shortcuts(self):
        for key, fn in (("Space", self._toggle_play), ("I", lambda: self._set_marker("start")),
                        ("O", lambda: self._set_marker("end")),
                        (",", lambda: self._step(-1)), (".", lambda: self._step(1))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(lambda f=fn: self._shortcut(f))

    def _shortcut(self, fn):
        # Plain-key shortcuts must not steal typing from the time fields.
        if isinstance(QApplication.focusWidget(), (QLineEdit, QDoubleSpinBox)):
            return
        if self.isEnabled() and self._src is not None and self._proc is None:
            fn()

    # ----------------------------------------------------------- loading
    def _browse(self):
        start = str(self._src.parent) if self._src else str(Path.home())
        exts = " ".join("*" + e for e in sorted(MEDIA_EXTS))
        p, _ = QFileDialog.getOpenFileName(self, "Choose a video", start,
                                           f"Media ({exts});;All files (*)")
        if p:
            self.load_source(p)

    def dragEnterEvent(self, e):
        if self._proc is None and e.mimeData().hasUrls() and any(
                u.isLocalFile() and is_media_file(u.toLocalFile()) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.isLocalFile() and is_media_file(u.toLocalFile()):
                e.acceptProposedAction()
                QTimer.singleShot(0, lambda p=u.toLocalFile(): self.load_source(p))
                return

    def load_source(self, path):
        path = Path(path)
        if self._proc is not None:
            return
        if not path.is_file():
            self.status.setText(f"File not found: {path}")
            return
        self._stop_playback()
        self._clear_result()
        self._src, self._info, self._mode = path, {}, "source"
        self._token += 1
        self._set_loaded(False)
        self.src_label.setText(f"Source: {path.name}")
        self.info_label.setText("Reading metadata\u2026")
        self.status.setText("")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        if self._player is not None:
            self._player.setSource(QUrl.fromLocalFile(str(path)))
        self.stack.setCurrentIndex(1)
        t = _ProbeThread(self._token, path, self)
        t.done.connect(self._on_probe)
        t.failed.connect(self._on_probe_failed)
        t.finished.connect(lambda th=t: self._probes.remove(th) if th in self._probes else None)
        self._probes.append(t)
        t.start()

    def _on_probe(self, token, info):
        if token != self._token:
            return
        self._info = info
        self._dur_ms = info["duration_ms"]
        self._fps = info["fps"]
        self.timeline.setRangeMs(self._dur_ms)
        self.timeline.setValue(0)
        self._sync_marker_edits()
        bits = []
        if info["has_video"]:
            bits.append(f"{info['width']}x{info['height']}")
            bits.append(f"{info['fps']:.2f}".rstrip("0").rstrip(".") + " fps")
            if info["vcodec"]:
                bits.append(info["vcodec"])
        else:
            bits.append("audio only")
        bits.append(fmt_time(self._dur_ms))
        if info["size"]:
            bits.append(f"{info['size'] / 1048576:.1f} MB")
        self.info_label.setText("  \u00b7  ".join(bits))
        if info["size"]:
            self.c_mb.blockSignals(True)
            self.c_mb.setValue(max(1.0, round(info["size"] / 1048576 / 2, 1)))
            self.c_mb.blockSignals(False)
        self._set_loaded(True)
        self._update_estimate()
        self._update_gif_hint()
        self._on_position(0)

    def _on_probe_failed(self, token, msg):
        if token != self._token:
            return
        self.info_label.setText("")
        self.status.setText(f"Couldn't read this file: {msg}")

    def _set_loaded(self, ok):
        vid = bool(self._info.get("has_video"))
        for w in (self.b_prev, self.b_play, self.b_next, self.b_playsel, self.timeline,
                  self.start_edit, self.end_edit, self.b_set_start, self.b_set_end,
                  self.b_reset, self.b_export):
            w.setEnabled(ok)
        # Compress / GIF need a video stream.
        self.tabs.setTabEnabled(0, ok and vid)
        self.tabs.setTabEnabled(2, ok and vid)
        if ok and not vid and self.tabs.currentIndex() != 1:
            self.tabs.setCurrentIndex(1)
        if not ok:
            self.sel_label.setText("Start: 00:00:00 | End: 00:00:00 | Duration: 00:00:00")

    # -------------------------------------------------- transport / sync
    def _toggle_play(self):
        if self._mode == "result" and self._result_is_gif and self._movie:
            self._movie.setPaused(self._movie.state() == QMovie.Running)
            self.b_play.setText("\u25b6  Play" if self._movie.state() != QMovie.Running
                                else "\u23f8  Pause")
            return
        if self._player is None:
            return
        self._play_sel = False
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state(self, st):
        playing = st == QMediaPlayer.PlayingState
        self.b_play.setText("\u23f8  Pause" if playing else "\u25b6  Play")

    def _step(self, direction):
        if self._player is None or self._mode == "result" and self._result_is_gif:
            return
        self._player.pause()
        self._play_sel = False
        ms = int(round(1000.0 / (self._fps or 30.0)))
        pos = max(0, min(self.timeline.maximum(), self._player.position() + direction * ms))
        self._player.setPosition(pos)
        self.timeline.setValue(pos)

    def _on_scrub(self, ms):
        self._play_sel = False
        if self._player is not None:
            self._player.setPosition(ms)
        self._update_pos_label(ms)

    def _on_position(self, ms):
        if not self.timeline.isSliderDown():
            self.timeline.setValue(ms)
        self._update_pos_label(ms)
        if self._play_sel and self._mode == "source":
            _s, e = self.timeline.selection()
            if ms >= e:
                self._play_sel = False
                self._player.pause()
                self._player.setPosition(e)

    def _on_player_duration(self, ms):
        if self._mode == "result" and ms > 0:
            self.timeline.setRangeMs(ms)
            self.timeline.setSelectionVisible(False)

    def _on_player_error(self, _err, msg):
        if msg:
            self.status.setText(f"Preview problem: {msg} (export still works)")

    def _update_pos_label(self, ms):
        self.pos_label.setText(f"{fmt_time(ms)} / {fmt_time(self.timeline.maximum())}")

    def _play_selection(self):
        if self._player is None:
            return
        s, e = self.timeline.selection()
        self._player.setPosition(s)
        self.timeline.setValue(s)
        self._play_sel = True
        self._player.play()

    # ----------------------------------------------------------- markers
    def _set_marker(self, which):
        if self._src is None or self._mode != "source":
            return
        pos = self._player.position() if self._player else self.timeline.value()
        s, e = self.timeline.selection()
        if which == "start":
            s = max(0, min(pos, e - MIN_GAP_MS))
        else:
            e = min(self._dur_ms, max(pos, s + MIN_GAP_MS))
        self.timeline.setSelection(s, e)
        self._sync_marker_edits()

    def _edit_marker(self, which):
        edit = self.start_edit if which == "start" else self.end_edit
        ms = parse_time(edit.text())
        s, e = self.timeline.selection()
        if ms is not None:
            if which == "start":
                s = max(0, min(ms, e - MIN_GAP_MS))
            else:
                e = min(self._dur_ms, max(ms, s + MIN_GAP_MS))
            self.timeline.setSelection(s, e)
        self._sync_marker_edits()

    def _reset_selection(self):
        if self._src is None:
            return
        self.timeline.setSelection(0, self._dur_ms)
        self._sync_marker_edits()

    def _sync_marker_edits(self):
        s, e = self.timeline.selection()
        self.start_edit.setText(fmt_time(s, True))
        self.end_edit.setText(fmt_time(e, True))
        self.sel_label.setText(
            f"Start: {fmt_time(s)} | End: {fmt_time(e)} | Duration: {fmt_time(e - s)}")
        self._update_estimate()
        self._update_gif_hint()

    # ------------------------------------------------------------ hints
    def _selection_s(self):
        s, e = self.timeline.selection()
        return s / 1000.0, (e - s) / 1000.0

    def _update_estimate(self):
        if not self._info or not self._info.get("has_video"):
            self.c_est.setText("")
            return
        dur = self._selection_s()[1] if self.c_sel.isChecked() else self._dur_ms / 1000.0
        if self.c_size.isChecked() and dur > 0:
            kbps = self.c_mb.value() * 8192 / dur - (128 if self._info.get("has_audio") else 0)
            src_mb = self._info.get("size", 0) / 1048576
            msg = f"About {kbps / 1000:.2f} Mbps of video over {fmt_time(dur * 1000)}."
            if kbps < 50:
                msg = "Target is too small for this length. Raise it or shorten the range."
            elif src_mb and self.c_mb.value() >= src_mb:
                msg += " That is not smaller than the original."
            self.c_est.setText(msg)
        else:
            self.c_est.setText("Re-encodes to H.264 + AAC (MP4). Typical CRF: 23 high quality, "
                               "28 balanced, 32+ small.")

    def _update_trim_hint(self, *_):
        if self.t_exact.isChecked():
            self.t_hint.setText("Re-encodes to MP4 (H.264, CRF 18) so the cut lands on the exact "
                                "frames you picked. Slower than a copy.")
        else:
            self.t_hint.setText("Instant and lossless (stream copy). The cut snaps to the nearest "
                                "keyframe, so the clip may begin slightly early.")

    def _update_gif_hint(self, *_):
        if not hasattr(self, "g_hint"):
            return
        dur = self._selection_s()[1]
        hq = self.g_preset.currentData() == "hq"
        base = ("1080p max, 20 fps, best dithering: big files." if hq
                else "480p, 12 fps: small and shareable.")
        warn = ""
        if dur > (10 if hq else 20):
            warn = "  Long selections make large GIFs; consider a shorter range."
        self.g_hint.setText(base + warn)

    def _tab_changed(self, i):
        self.b_export.setText(("Compress", "Trim", "Make GIF")[i])

    # ------------------------------------------------------------ export
    def _base_args(self):
        return ["-n", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]

    def _build_export(self):
        """-> (args, out_path, total_ms) or raises ValueError."""
        src, info = self._src, self._info
        s_ms, e_ms = self.timeline.selection()
        has_a = info.get("has_audio")
        tab = self.tabs.currentIndex()
        a = self._base_args()
        if tab == 0:                                                  # Compress
            whole = not self.c_sel.isChecked()
            st, du = (0.0, self._dur_ms / 1000.0) if whole else self._selection_s()
            if du <= 0:
                raise ValueError("The range is empty.")
            if not whole:
                a += ["-ss", f"{st:.3f}"]
            a += ["-i", str(src)]
            if not whole:
                a += ["-t", f"{du:.3f}"]
            a += ["-map", "0:v:0", "-map", "0:a:0?",
                  "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                  "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
            if self.c_size.isChecked():
                v_k = int(self.c_mb.value() * 8192 / du - (128 if has_a else 0))
                if v_k < 50:
                    raise ValueError("Target size is too small for this length.")
                a += ["-b:v", f"{v_k}k", "-maxrate", f"{int(v_k * 1.5)}k",
                      "-bufsize", f"{v_k * 3}k"]
            else:
                a += ["-crf", str(self.c_slider.value())]
            if has_a:
                a += ["-c:a", "aac", "-b:a", "128k"]
            a += ["-movflags", "+faststart"]
            out = unique_out(src, "_compressed", ".mp4")
            total = int(du * 1000)
        elif tab == 1:                                                # Trim
            st, du = self._selection_s()
            if du <= 0:
                raise ValueError("The range is empty.")
            a += ["-ss", f"{st:.3f}", "-i", str(src), "-t", f"{du:.3f}",
                  "-map", "0:v:0?", "-map", "0:a:0?"]
            if self.t_exact.isChecked():
                a += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                      "-pix_fmt", "yuv420p"]
                if has_a:
                    a += ["-c:a", "aac", "-b:a", "192k"]
                a += ["-movflags", "+faststart"]
                out = unique_out(src, "_trimmed", ".mp4")
            else:
                a += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
                out = unique_out(src, "_trimmed", src.suffix or ".mp4")
            total = int(du * 1000)
        else:                                                         # GIF
            st, du = self._selection_s()
            if du <= 0:
                raise ValueError("The range is empty.")
            if du > 120:
                raise ValueError("GIFs are limited to 2 minutes. Shorten the range.")
            hq = self.g_preset.currentData() == "hq"
            h, fps = (1080, 20) if hq else (480, 12)
            pal = ("palettegen=max_colors=256:stats_mode=full",
                   "paletteuse=dither=sierra2_4a") if hq else (
                   "palettegen=stats_mode=diff",
                   "paletteuse=dither=bayer:bayer_scale=5")
            # Never upscale: min(target height, source height).
            vf = (f"fps={fps},scale=-2:min({h}\\,ih):flags=lanczos,split[s0][s1];"
                  f"[s0]{pal[0]}[p];[s1][p]{pal[1]}")
            a += ["-ss", f"{st:.3f}", "-t", f"{du:.3f}", "-i", str(src),
                  "-vf", vf, "-loop", "0"]
            out = unique_out(src, "_clip", ".gif")
            total = int(du * 1000)
        a.append(str(out))
        return a, out, max(1, total)

    def _start_export(self):
        if self._src is None or self._proc is not None:
            return
        if self._mode != "source":
            self._show_view("source")
        try:
            args, out, total = self._build_export()
        except ValueError as e:
            self.status.setText(str(e))
            return
        self._stop_playback()
        self._out, self._total_ms = out, total
        self._cancelled = False
        self._stdout_buf = self._stderr_tail = ""
        self._proc = QProcess(self)
        self._proc.setProgram(_bin("ffmpeg"))
        self._proc.setArguments(args)
        self._proc.readyReadStandardOutput.connect(self._on_proc_out)
        self._proc.readyReadStandardError.connect(self._on_proc_err)
        self._proc.finished.connect(self._on_proc_finished)
        self._proc.errorOccurred.connect(self._on_proc_error)
        self._set_busy(True)
        self.bar.setRange(0, 0)                       # busy until first progress line
        self.status.setText(f"Working on {out.name}\u2026")
        self._proc.start()

    def _set_busy(self, busy):
        self.b_cancel.setVisible(busy)
        self.b_export.setVisible(not busy)
        for w in (self.tabs, self.open_btn, self.timeline, self.start_edit, self.end_edit,
                  self.b_set_start, self.b_set_end, self.b_reset, self.b_playsel,
                  self.view_orig, self.view_res, self.b_edit_result):
            w.setEnabled(not busy)

    def _on_proc_out(self):
        self._stdout_buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._stdout_buf = self._stdout_buf.split("\n")
        speed = ""
        us = None
        for ln in lines:
            k, _, v = ln.strip().partition("=")
            if k in ("out_time_us", "out_time_ms") and v.lstrip("-").isdigit():
                us = int(v)                      # both keys are microseconds
            elif k == "speed":
                speed = v
        if us is not None and us >= 0:
            if self.bar.maximum() == 0:
                self.bar.setRange(0, 100)
            pct = max(0, min(99, int(us / 1000 * 100 / self._total_ms)))
            self.bar.setValue(pct)
            self.status.setText(f"{pct}%" + (f"  \u00b7  {speed}" if speed and speed != "N/A" else ""))

    def _on_proc_err(self):
        self._stderr_tail = (self._stderr_tail + bytes(
            self._proc.readAllStandardError()).decode("utf-8", "replace"))[-2000:]

    def _on_proc_error(self, err):
        if err == QProcess.FailedToStart and self._proc is not None:
            self._stderr_tail = "ffmpeg could not be started. Is it installed or bundled?"
            self._finish_export(failed=True)

    def _on_proc_finished(self, code, _status):
        if self._proc is None:
            return
        ok = (code == 0 and self._out is not None and self._out.exists()
              and self._out.stat().st_size > 0 and not self._cancelled)
        self._finish_export(failed=not ok)

    def _finish_export(self, failed):
        proc, out = self._proc, self._out
        self._proc = None
        if proc is not None:
            proc.deleteLater()
        self._set_busy(False)
        self.bar.setRange(0, 100)
        if self._cancelled or failed:
            try:
                if out is not None and out.exists():
                    out.unlink()
            except OSError:
                pass
            self.bar.setValue(0)
            if self._cancelled:
                self.status.setText("Export cancelled.")
            else:
                tail = " ".join((self._stderr_tail or "").strip().splitlines()[-3:])
                self.status.setText("Failed: " + (tail[:300] or "ffmpeg reported an error."))
            self._restore_source_preview()
            return
        self.bar.setValue(100)
        mb = out.stat().st_size / 1048576
        self.status.setText(f"Saved: {out.name}  ({mb:.1f} MB)  in {out.parent}")
        self._set_result(out)

    def _cancel_export(self):
        if self._proc is None:
            return
        self._cancelled = True
        self.status.setText("Cancelling\u2026")
        try:
            self._proc.write(b"q")                   # ask ffmpeg to stop cleanly
        except Exception:
            pass
        QTimer.singleShot(1500, self._kill_if_running)

    def _kill_if_running(self):
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self._proc.kill()

    # ----------------------------------------------- in-app result preview
    def _set_result(self, path):
        self._result = path
        self._result_is_gif = path.suffix.lower() == ".gif"
        for w in (self.view_orig, self.view_res, self.b_folder, self.b_edit_result):
            w.setVisible(True)
        self.view_res.setChecked(True)
        self._show_view("result")

    def _clear_result(self):
        self._stop_gif()
        self._result = None
        for w in (self.view_orig, self.view_res, self.b_folder, self.b_edit_result):
            w.setVisible(False)

    def _restore_source_preview(self):
        if self._src is not None and self._player is not None and self._mode == "source":
            self._player.setSource(QUrl.fromLocalFile(str(self._src)))
            self.stack.setCurrentIndex(1)

    def _show_view(self, which):
        if self._proc is not None:
            return
        self._stop_playback()
        self._stop_gif()
        if which == "result" and self._result is not None:
            self._mode = "result"
            self.view_res.setChecked(True)
            self._edit_controls(False)
            if self._result_is_gif:
                self._play_gif(self._result)
            elif self._player is not None:
                self.timeline.setSelectionVisible(False)
                self._player.setSource(QUrl.fromLocalFile(str(self._result)))
                self.stack.setCurrentIndex(1)
                self._player.play()
        else:
            self._mode = "source"
            self.view_orig.setChecked(True)
            self._edit_controls(True)
            self.timeline.setSelectionVisible(True)
            if self._src is not None:
                s, e = self.timeline.selection()
                self.timeline.setRangeMs(self._dur_ms)
                self.timeline.setSelection(s, e)
                self._restore_source_preview()
                self._on_position(0)
            self._sync_marker_edits()

    def _edit_controls(self, on):
        for w in (self.start_edit, self.end_edit, self.b_set_start, self.b_set_end,
                  self.b_reset, self.b_playsel):
            w.setEnabled(on)
        gif = self._mode == "result" and self._result_is_gif
        self.timeline.setEnabled(not gif)
        self.b_prev.setEnabled(not gif)
        self.b_next.setEnabled(not gif)

    def _play_gif(self, path):
        self._movie = QMovie(str(path))
        self._movie.jumpToFrame(0)
        size = self._movie.currentImage().size()
        if size.isValid():
            box = self.stack.size()
            self._movie.setScaledSize(size.scaled(box.width() - 8, box.height() - 8,
                                                  Qt.KeepAspectRatio))
        self.gif_label.setMovie(self._movie)
        self.stack.setCurrentIndex(2)
        self._movie.start()
        self.b_play.setText("\u23f8  Pause")

    def _stop_gif(self):
        if self._movie is not None:
            self._movie.stop()
            self.gif_label.setMovie(None)
            self._movie.deleteLater()
            self._movie = None

    def _stop_playback(self):
        self._play_sel = False
        if self._player is not None:
            self._player.pause()

    def _show_in_folder(self):
        p = self._result
        if not p or not Path(p).exists():
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(Path(p))])
        else:
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(p).parent)))

    def _edit_result(self):
        if self._result is not None and self._proc is None:
            self.load_source(self._result)

    # --------------------------------------------------------- safe exit
    def done(self, r):
        if self._proc is not None:
            if QMessageBox.question(
                    self, "Export in progress",
                    "An export is still running. Cancel it and close?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self._cancelled = True
            proc, out = self._proc, self._out
            self._proc = None
            try:
                proc.finished.disconnect()
                proc.kill()
                proc.waitForFinished(3000)
            except Exception:
                pass
            try:
                if out is not None and out.exists():
                    out.unlink()
            except OSError:
                pass
        self._shutdown()
        super().done(r)

    def _shutdown(self):
        self._token += 1                      # ignore any late probe results
        self._stop_gif()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())    # release the file handle (Windows)
        for t in list(self._probes):
            t.wait(2000)
