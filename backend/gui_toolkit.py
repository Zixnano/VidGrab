"""gui_toolkit.py - Media Toolkit dialog: Compress, Trim and Make GIF.

The dialog never runs ffmpeg itself. Each tab posts to the local backend
(/toolkit/compress, /toolkit/trim, /toolkit/gif in api.py), which runs ffmpeg
and writes the result into the download folder. Calls run on a worker thread
so the window stays responsive during long encodes.
"""
import os
import sys
from pathlib import Path

import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QProgressBar, QRadioButton, QSlider, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)


class _Worker(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, api, kind, body, parent=None):
        super().__init__(parent)
        self._api, self._kind, self._body = api, kind, body

    def run(self):
        try:
            self.done.emit(self._api.toolkit(self._kind, self._body) or {})
        except Exception as e:  # message already cleaned by ApiClient.toolkit
            self.failed.emit(str(e))


class ToolkitDialog(QDialog):
    """api: gui_qt.ApiClient. job: optional finished job dict to work on."""

    def __init__(self, api, job=None, parent=None):
        super().__init__(parent)
        self.api = api
        self._job_id = (job or {}).get("id")
        self._path = ""
        self._worker = None
        self._last_output = ""
        self.setWindowTitle("Media Toolkit")
        self.setMinimumSize(520, 440)

        root = QVBoxLayout(self)

        src_row = QHBoxLayout()
        self.src_label = QLabel(self._src_text(job))
        self.src_label.setWordWrap(True)
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse)
        src_row.addWidget(self.src_label, 1)
        src_row.addWidget(browse)
        root.addLayout(src_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_compress(), "Compress")
        self.tabs.addTab(self._build_trim(), "Trim")
        self.tabs.addTab(self._build_gif(), "Make GIF")
        root.addWidget(self.tabs, 1)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setVisible(False)
        root.addWidget(self.busy)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        btns = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run)
        self.open_btn = QPushButton("Show result")
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(self._show_result)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(self.open_btn)
        btns.addWidget(self.run_btn)
        btns.addWidget(close)
        root.addLayout(btns)

    # ---------------------------------------------------------------- tabs
    def _build_compress(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.c_quality = QRadioButton("Quality (CRF): lower is better, bigger file")
        self.c_quality.setChecked(True)
        self.c_slider = QSlider(Qt.Horizontal)
        self.c_slider.setRange(18, 36)
        self.c_slider.setValue(28)
        self.c_val = QLabel("28")
        self.c_slider.valueChanged.connect(lambda v: self.c_val.setText(str(v)))
        srow = QHBoxLayout()
        srow.addWidget(self.c_slider, 1)
        srow.addWidget(self.c_val)
        self.c_size = QRadioButton("Target size (MB)")
        self.c_mb = QDoubleSpinBox()
        self.c_mb.setRange(1, 100000)
        self.c_mb.setDecimals(1)
        self.c_mb.setValue(50)
        self.c_mb.setSuffix(" MB")
        lay.addWidget(self.c_quality)
        lay.addLayout(srow)
        lay.addSpacing(8)
        lay.addWidget(self.c_size)
        lay.addWidget(self.c_mb)
        hint = QLabel("Re-encodes to H.264 + AAC (MP4). Target size picks a video bitrate "
                      "from the clip's length, so it is approximate.")
        hint.setObjectName("CardHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    def _build_trim(self):
        w = QWidget()
        form = QFormLayout(w)
        self.t_start = QLineEdit("00:00:00")
        self.t_end = QLineEdit()
        self.t_end.setPlaceholderText("end of file")
        form.addRow("Start", self.t_start)
        form.addRow("End", self.t_end)
        hint = QLabel("Times look like 90, 1:30 or 00:01:30. Uses stream copy: instant and "
                      "lossless, cutting at the nearest keyframe.")
        hint.setObjectName("CardHint")
        hint.setWordWrap(True)
        form.addRow(hint)
        return w

    def _build_gif(self):
        w = QWidget()
        form = QFormLayout(w)
        self.g_start = QLineEdit("00:00:00")
        self.g_dur = QDoubleSpinBox()
        self.g_dur.setRange(0.1, 60)
        self.g_dur.setValue(5)
        self.g_dur.setSuffix(" s")
        self.g_fps = QSpinBox()
        self.g_fps.setRange(1, 30)
        self.g_fps.setValue(12)
        self.g_width = QSpinBox()
        self.g_width.setRange(64, 1280)
        self.g_width.setValue(480)
        self.g_width.setSuffix(" px")
        form.addRow("Start", self.g_start)
        form.addRow("Duration", self.g_dur)
        form.addRow("Frame rate", self.g_fps)
        form.addRow("Width", self.g_width)
        hint = QLabel("Builds a custom 256-color palette for the clip (palettegen + paletteuse) "
                      "for a clean, high-quality GIF.")
        hint.setObjectName("CardHint")
        hint.setWordWrap(True)
        form.addRow(hint)
        return w

    # ------------------------------------------------------------- actions
    @staticmethod
    def _src_text(job):
        return f"Source: {job.get('filename')}" if job and job.get("filename") \
            else "Source: none selected. Use Browse\u2026"

    def _browse(self):
        start = str(Path.home() / "Downloads" / "VideoGrabber")
        p, _ = QFileDialog.getOpenFileName(
            self, "Choose a video", start,
            "Video / audio (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mp3 *.m4a);;All files (*)")
        if p:
            self._path, self._job_id = p, None
            self.src_label.setText(f"Source: {Path(p).name}")

    def _body(self):
        body = {"job_id": self._job_id} if self._job_id else {"path": self._path}
        i = self.tabs.currentIndex()
        if i == 0:
            if self.c_size.isChecked():
                body.update(mode="size", target_mb=self.c_mb.value())
            else:
                body.update(mode="quality", crf=self.c_slider.value())
            return "compress", body
        if i == 1:
            body.update(start=self.t_start.text().strip(), end=self.t_end.text().strip())
            return "trim", body
        body.update(start=self.g_start.text().strip(), duration=self.g_dur.value(),
                    fps=self.g_fps.value(), width=self.g_width.value())
        return "gif", body

    def _run(self):
        if not self._job_id and not self._path:
            self.status.setText("Pick a source file first.")
            return
        kind, body = self._body()
        self.run_btn.setEnabled(False)
        self.open_btn.setVisible(False)
        self.busy.setVisible(True)
        self.status.setText("Working\u2026 this can take a while for long videos.")
        self._worker = _Worker(self.api, kind, body, self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _finish(self):
        self.busy.setVisible(False)
        self.run_btn.setEnabled(True)

    def _on_done(self, res):
        self._finish()
        self._last_output = res.get("output", "")
        size = res.get("size") or 0
        self.status.setText(f"Saved: {self._last_output}  ({size / 1048576:.1f} MB)")
        self.open_btn.setVisible(bool(self._last_output))

    def _on_failed(self, msg):
        self._finish()
        self.status.setText(f"Failed: {msg}")

    def _show_result(self):
        p = self._last_output
        if not p or not Path(p).exists():
            return
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(p).parent)))

    def done(self, r):
        # Don't close mid-encode and orphan the worker thread.
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("Wait for the current job to finish.")
            return
        super().done(r)
