"""The pet window. Two timers with different jobs:

  * an animation timer (10 fps, only while the window is visible and the pet is on) that picks a
    frame from cached pixmaps, which costs almost nothing, and
  * a state timer (1 per second, same conditions) plus change signals from the core, which refresh
    labels, bars and buttons.

Neither timer touches the database or the filesystem on every tick.
"""
import json
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)

from .animation import Animator
from .store import load_registry

SCALE = 2
STYLE = """
QDialog { background:#171522; color:#eee9ff; }
QLabel { color:#eee9ff; }
QLabel#sub { color:#b9b2d9; }
QLabel#bubble { background:#2e2850; border:1px solid #6c62a8; border-radius:10px; padding:8px 12px; color:#f4f0ff; }
QLabel#stage { background:#242139; border:1px solid #514a78; border-radius:14px; }
QPushButton, QComboBox { background:#242139; color:#eee9ff; border:1px solid #514a78; border-radius:8px; padding:7px 10px; }
QPushButton:hover { background:#332e52; }
QPushButton:disabled { color:#7e78a0; background:#1d1a2e; }
QProgressBar { background:#242139; border:1px solid #514a78; border-radius:6px; height:12px; text-align:center; color:#eee9ff; }
QProgressBar::chunk { background:#a99cff; border-radius:5px; }
QCheckBox { color:#eee9ff; }
QPlainTextEdit { background:#242139; color:#eee9ff; border:1px solid #514a78; border-radius:8px; }
"""


class CompanionDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.ctl = controller
        self.core = controller.core
        self.bank = controller.bank
        self.setWindowTitle("Video Grabber \u2022 Companion")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(460)

        self._t0 = time.monotonic()
        self._anim = None
        self._anim_key = None
        self._species_sig = None
        self._notice = ("", 0.0)
        self._refresh_pending = False

        root = QVBoxLayout(self)
        self.title = QLabel("")
        self.title.setStyleSheet("font-size:19px;font-weight:700;")
        self.sub = QLabel("")
        self.sub.setObjectName("sub")
        root.addWidget(self.title)
        root.addWidget(self.sub)

        self.bubble = QLabel("")
        self.bubble.setObjectName("bubble")
        self.bubble.setWordWrap(True)
        self.bubble.setAlignment(Qt.AlignCenter)
        root.addWidget(self.bubble)

        self.sprite = QLabel()
        self.sprite.setObjectName("stage")
        self.sprite.setAlignment(Qt.AlignCenter)
        size = self.bank.frame_size() * SCALE
        self.sprite.setMinimumSize(size + 40, size + 28)
        root.addWidget(self.sprite, 1)

        self.xp = QProgressBar()
        self.xp.setTextVisible(True)
        root.addWidget(self.xp)
        self.needs = QLabel("")
        self.needs.setObjectName("sub")
        self.needs.setAlignment(Qt.AlignCenter)
        root.addWidget(self.needs)

        row = QHBoxLayout()
        self.buttons = {}
        for key, label in (("feed", "Feed (1 treat)"), ("play", "Play"), ("pet", "Pet"), ("nap", "Nap")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self._care(k))
            row.addWidget(b)
            self.buttons[key] = b
        root.addLayout(row)

        row2 = QHBoxLayout()
        self.games_btn = QPushButton("Play games")
        self.games_btn.clicked.connect(self.ctl.open_games)
        self.stickers_btn = QPushButton("Stickers")
        self.stickers_btn.clicked.connect(self.ctl.open_stickers)
        self.ach_btn = QPushButton("Achievements")
        self.ach_btn.clicked.connect(self._show_achievements)
        for b in (self.games_btn, self.stickers_btn, self.ach_btn):
            row2.addWidget(b)
        root.addLayout(row2)

        row3 = QHBoxLayout()
        self.species = QComboBox()
        self.species.activated.connect(self._species_chosen)
        row3.addWidget(QLabel("Companion:"))
        row3.addWidget(self.species, 1)
        self.more = QPushButton("More\u2026")
        menu = QMenu(self.more)
        menu.addAction("Export progress\u2026", self._export)
        menu.addAction("Reset companion\u2026", self._reset)
        self.more.setMenu(menu)
        row3.addWidget(self.more)
        root.addLayout(row3)

        row4 = QHBoxLayout()
        self.enabled = QCheckBox("Companion on")
        self.enabled.toggled.connect(self._toggle_enabled)
        self.bubbles = QCheckBox("Speech bubbles")
        self.bubbles.toggled.connect(lambda v: self.core.set_option("bubbles", v))
        self.animate = QCheckBox("Animate")
        self.animate.toggled.connect(lambda v: self.core.set_option("animations", v))
        for w in (self.enabled, self.bubbles, self.animate):
            row4.addWidget(w)
        row4.addStretch(1)
        root.addLayout(row4)

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(100)
        self.anim_timer.timeout.connect(self._paint_sprite)
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(1000)
        self.state_timer.timeout.connect(self._refresh_state)

        self.ctl.signals.changed.connect(self._schedule_refresh)
        self.ctl.signals.notice.connect(self._on_notice)
        self._refresh_state()

    # ---- visibility drives the timers -------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_state()
        self._sync_timers()

    def hideEvent(self, event):
        self.anim_timer.stop()
        self.state_timer.stop()
        super().hideEvent(event)

    def _sync_timers(self):
        snap = self.core.state
        if self.isVisible() and snap.enabled:
            self.state_timer.start()
            if snap.animations:
                self.anim_timer.start()
            else:
                self.anim_timer.stop()
        else:
            self.anim_timer.stop()
            self.state_timer.stop()

    # ---- refresh ------------------------------------------------------------------
    def _schedule_refresh(self):
        """Coalesce bursts of change signals into one refresh on the next event-loop turn."""
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(0, self._refresh_state)

    def _on_notice(self, kind, text):
        self._notice = (text, time.monotonic() + 6.0)
        self._schedule_refresh()

    def _refresh_state(self):
        self._refresh_pending = False
        snap = self.core.snapshot()
        on = snap["enabled"]
        stage = snap["stage"]
        self.title.setText("%s the %s" % (snap["name"], stage.title()))
        nxt = snap["next_stage"]
        hint = "   \u2022   %s at level %d" % ("Hatches" if stage == "egg" else "Grows up", nxt["level"]) if nxt else ""
        self.sub.setText("Level %d   \u2022   %d treats   \u2022   %d-day streak%s" % (snap["level"], snap["coins"], snap["streak"], hint))
        self.xp.setMaximum(max(1, snap["xp_needed"]))
        self.xp.setValue(min(snap["xp"], snap["xp_needed"]))
        self.xp.setFormat("%d / %d XP" % (snap["xp"], snap["xp_needed"]))
        self.needs.setText("Hunger %d    Energy %d    Affection %d    Mood: %s" % (
            snap["hunger"], snap["energy"], snap["affection"], snap["mood"].title()))

        text, until = self._notice
        speech = text if (text and time.monotonic() < until) else snap["speech"]
        self.bubble.setVisible(bool(snap["bubbles"]) or not on)
        self.bubble.setText(speech)

        for key, b in self.buttons.items():
            b.setEnabled(on)
        self.games_btn.setEnabled(on)
        for w, v in ((self.enabled, on), (self.bubbles, snap["bubbles"]), (self.animate, snap["animations"])):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.bubbles.setEnabled(on)
        self.animate.setEnabled(on)

        self._rebuild_species_if_needed(snap)
        self._sync_timers()
        self._paint_sprite()

    def _rebuild_species_if_needed(self, snap):
        """Only touch the combo box when the list of options really changed (never while open)."""
        reg = load_registry().get("species", [])
        sig = (snap["species"], tuple(sorted(snap["unlocked"])), len(reg))
        if sig == self._species_sig:
            return
        self._species_sig = sig
        self.species.blockSignals(True)
        self.species.clear()
        for entry in reg:
            unlocked = entry.get("unlocked_by_default") or ("species_" + entry["id"]) in snap["unlocked"]
            label = entry.get("display_name", entry["id"]) + ("" if unlocked else "  (locked)")
            self.species.addItem(label, entry["id"])
            if entry["id"] == snap["species"]:
                self.species.setCurrentIndex(self.species.count() - 1)
        self.species.blockSignals(False)

    def _paint_sprite(self):
        snap = self.core.snapshot()
        if not snap["enabled"]:
            self.sprite.setPixmap(self.bank.canvas(self.bank.entry_for(snap["species"], snap["stage"]), 0, 0, SCALE))
            self.sprite.setEnabled(False)
            return
        self.sprite.setEnabled(True)
        entry = self.bank.entry_for(snap["species"], snap["stage"])
        key = (snap["species"], snap["stage"])
        if self._anim_key != key:
            self._anim, self._anim_key = Animator(entry, load_registry().get("animation")), key
        t = (time.monotonic() - self._t0) if snap["animations"] else 0.0
        index, dy = self._anim.frame(snap["pose"], t)
        self.sprite.setPixmap(self.bank.canvas(entry, index, dy if snap["animations"] else 0, SCALE))

    # ---- actions ------------------------------------------------------------------------
    def _care(self, action):
        result = self.ctl.care(action)
        if not result.get("ok"):
            self._notice = (result.get("error", "Not now."), time.monotonic() + 3.0)
        self._refresh_state()

    def _toggle_enabled(self, value):
        self.ctl.set_enabled(bool(value))
        self._refresh_state()

    def _species_chosen(self, _index):
        result = self.ctl.core.set_species(self.species.currentData())
        if not result.get("ok"):
            QMessageBox.information(self, "Companion", result.get("error", "Could not switch companion."))
        self._species_sig = None
        self._refresh_state()

    def _show_achievements(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Achievements")
        dlg.setStyleSheet(STYLE)
        dlg.resize(440, 340)
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText("\n".join("%s  %s\n      %s" % ("\u2605" if a["unlocked"] else "\u2606", a["title"], a["description"])
                                    for a in self.core.achievements()))
        lay.addWidget(box)
        dlg.exec()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export companion progress", "companion-progress.json", "JSON (*.json)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(self.core.store.export_data(), fh, indent=2)
            except OSError as exc:
                QMessageBox.warning(self, "Export companion progress", "Could not write the file: %s" % exc)

    def _reset(self):
        ok = QMessageBox.question(self, "Reset companion",
                                  "Reset your companion, treats, unlocks and stickers? This cannot be undone.") == QMessageBox.Yes
        if ok:
            self.core.reset()
            self._species_sig = None
            self._refresh_state()
