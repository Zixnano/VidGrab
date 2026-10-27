"""Settings dialog, its nav, and the bandwidth profiles dialog."""

import re
import time
from pathlib import Path

import requests

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from settings import CATEGORIES, DISPLAY_VERSION
# Batch 3 (moved): the procedural completion-sound maker (sound_generator.py,
# same folder) opens from Settings > Sounds through open_sound_options. It no
# longer has a Tools menu entry.
from sound_generator import open_sound_options

import gui_palette
import gui_common
from gui_palette import _sync_palette
from gui_common import load_settings
from gui_api import _ApiFetchWorker


def reapply_theme():
    """gui_main owns reapply_theme (it reads gui_main's _window global). The
    import is deferred to call time because gui_main imports this module."""
    from gui_main import reapply_theme as _reapply_theme
    return _reapply_theme()


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
        delay_hint.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        self.update_help.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        upnote.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        compnote.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        note.setStyleSheet(f"color: {gui_palette.MUTED};")
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
        _rsub.setStyleSheet(f"color: {gui_palette.MUTED};")
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
                self.update_info.setStyleSheet(f"color: {gui_palette.DANGER};")
                self.update_info.setText(f"Couldn't read the update status: {e}")

        def bad(msg):
            self._set_update_busy(False)
            self.update_info.setStyleSheet(f"color: {gui_palette.DANGER};")
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
            return Path(gui_common.HOME) / "sounds"

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
            it.setForeground(QColor(gui_palette.MUTED))
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
        hint.setStyleSheet(f"color: {gui_palette.MUTED};")
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
