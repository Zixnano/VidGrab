"""The game screen: an overlay that covers the main window's content area (the console's screen).

While it is open it owns the shell's D-pad, A, B, START and SELECT through MainWindow._vg_pad_hook,
and it also reads the keyboard (arrows or WASD, Z or Space = A, X = B, Enter or P = Start, Esc = Select).
"""
import random
import time

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from .. import diag
from . import GAME_CLASSES
from .engine import ACCENT, BG, DIM, GOLD, GREEN, H, INK, PANEL, PINK, W, draw_pet, text

KEYMAP = {int(k): v for k, v in {
    Qt.Key_Up: "up", Qt.Key_W: "up", Qt.Key_Down: "down", Qt.Key_S: "down",
    Qt.Key_Left: "left", Qt.Key_A: "left", Qt.Key_Right: "right", Qt.Key_D: "right",
    Qt.Key_Z: "a", Qt.Key_Space: "a", Qt.Key_X: "b", Qt.Key_Backspace: "b",
    Qt.Key_Return: "start", Qt.Key_Enter: "start", Qt.Key_P: "start", Qt.Key_Escape: "select",
}.items()}
DIRECTIONS = ("up", "down", "left", "right")


class _Ctx:
    """What games need from the app: the pet's current art and a random source."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.rng = random.Random()

    def pet(self, pose):
        state = self.ctl.core.state
        entry = self.ctl.bank.entry_for(state.species, state.stage)
        frames = entry.get("frames", {})
        seq = frames.get(pose) or frames.get("idle") or [0]
        return self.ctl.bank.frame(entry, seq[0], 1)


class GameOverlay(QWidget):
    closed = Signal()

    def __init__(self, ctl, parent):
        super().__init__(parent)
        self.ctl = ctl
        self.core = ctl.core
        self.ctx = _Ctx(ctl)
        self.mode = "menu"                  # menu | play | result
        self.sel = 0
        self.game = None
        self.result = None
        self._last = time.monotonic()
        self.setFocusPolicy(Qt.StrongFocus)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._frame)
        parent.installEventFilter(self)
        self._fit()
        self.show()
        self.raise_()
        self.setFocus()
        self.timer.start()

    # ---- geometry ---------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._fit()
        return False

    def _fit(self):
        self.setGeometry(self.parentWidget().rect())

    # ---- loop ----------------------------------------------------------------
    def _frame(self):
        try:
            now = time.monotonic()
            dt = min(0.1, now - self._last)
            self._last = now
            if self.mode == "play" and self.game is not None:
                if not self.window().isActiveWindow():
                    self.game.paused = True            # never lose a run to a stray alt-tab
                self.game.update(dt)
                if self.game.done:
                    self._finish()
            self.update()
        except Exception as exc:
            self._fail("frame", exc)

    def _fail(self, where, exc):
        """A game bug closes the game screen once instead of repeating an error 30 times a second."""
        diag.note("overlay_" + where, "game screen error in %s; closing it" % where, exc)
        if not getattr(self, "_failed", False):
            self._failed = True
            QTimer.singleShot(0, self.close_overlay)

    def _finish(self):
        game = self.game
        res = self.core.award_minigame(game.score, game.id)
        self.result = res if res.get("ok") else {"score": game.score, "coins": 0, "new_best": False, "best": game.score}
        self.mode = "result"

    def _start(self):
        cls = GAME_CLASSES[self.sel]
        self.game = cls(self.ctx)
        self.mode = "play"
        self._last = time.monotonic()

    def close_overlay(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.timer.stop()
        self.parentWidget().removeEventFilter(self)
        self.hide()
        self.closed.emit()
        self.deleteLater()

    # ---- input -----------------------------------------------------------------
    def handle(self, key):
        if self.mode == "menu":
            if key == "up":
                self.sel = (self.sel - 1) % len(GAME_CLASSES)
            elif key == "down":
                self.sel = (self.sel + 1) % len(GAME_CLASSES)
            elif key in ("a", "start"):
                self._start()
            elif key in ("b", "select"):
                self.close_overlay()
        elif self.mode == "play":
            if key in DIRECTIONS:
                self.game.press(key)
            elif key == "start":
                self.game.paused = not self.game.paused
            elif key == "select":
                self.mode = "menu"                     # leaving early earns nothing
        elif self.mode == "result":
            if key in ("a", "start"):
                self._start()
            elif key in ("b", "select"):
                self.mode = "menu"
        self.update()

    def pad(self, kind, name):
        """Called by MainWindow for every shell press. True means the overlay took it."""
        if not self.isVisible():
            return False
        if kind == "dpad" and name in DIRECTIONS:
            self.handle(name)
            return True
        if kind == "button" and name in ("a", "b", "start", "select"):
            self.handle(name)
            return True
        return False                                   # power and anything else keep their normal job

    def keyPressEvent(self, event):
        key = KEYMAP.get(int(event.key()))
        if key is None:
            super().keyPressEvent(event)
            return
        if event.isAutoRepeat() and key not in ("left", "right"):
            return
        try:
            self.handle(key)
        except Exception as exc:
            self._fail("key", exc)

    # ---- drawing -------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), BG)
            scale = min(self.width() / float(W), self.height() / float(H))
            p.translate((self.width() - W * scale) / 2.0, (self.height() - H * scale) / 2.0)
            p.scale(scale, scale)
            p.setClipRect(QRectF(0, 0, W, H))
            p.fillRect(QRectF(0, 0, W, H), BG)
            if self.mode == "menu":
                self._draw_menu(p)
            elif self.mode == "play" and self.game is not None:
                self.game.draw(p)
                if self.game.paused:
                    p.fillRect(QRectF(0, 0, W, H), QColor(10, 8, 24, 170))
                    text(p, "PAUSED", W / 2, 66, 16, GOLD, "center", 160)
                    text(p, "Start: resume   Select: quit", W / 2, 90, 9, DIM, "center", 220)
            else:
                self._draw_result(p)
        except Exception as exc:
            self._fail("paint", exc)
        finally:
            p.end()

    def _draw_menu(self, p):
        text(p, "GAME ROOM", W / 2, 8, 14, ACCENT, "center", 200)
        left = self.core.minigame_remaining()
        text(p, "Treats left today: %d" % left, W / 2, 26, 9, GOLD if left else DIM, "center", 220)
        for i, cls in enumerate(GAME_CLASSES):
            y = 46 + i * 30
            on = i == self.sel
            p.fillRect(QRectF(16, y, 150, 26), PANEL if on else BG)
            if on:
                p.fillRect(QRectF(16, y, 3, 26), PINK)
            best = self.core.state.best_scores.get(cls.id, 0)
            text(p, cls.title, 26, y + 2, 11, INK if on else DIM)
            text(p, "best %d" % best, 26, y + 14, 8, DIM)
        draw_pet(p, self.ctx, "excited" if self.sel % 2 == 0 else "idle", 176, 56, 52)
        sel = GAME_CLASSES[self.sel]
        text(p, sel.blurb, W / 2, 138, 9, INK, "center", 232)
        text(p, "A: play   B: back   " + sel.controls, W / 2, 150, 7, DIM, "center", 236)

    def _draw_result(self, p):
        res = self.result or {}
        score = res.get("score", 0)
        text(p, "GAME OVER", W / 2, 22, 16, ACCENT, "center", 220)
        draw_pet(p, self.ctx, "celebrating" if res.get("coins", 0) else "idle", 94, 46, 52)
        text(p, "Score  %d" % score, W / 2, 104, 12, INK, "center", 200)
        coins = res.get("coins", 0)
        text(p, ("+%d treats" % coins) if coins else "No treats left today" if self.core.minigame_remaining() == 0 else "No treats this time",
             W / 2, 120, 10, GREEN if coins else DIM, "center", 220)
        if res.get("new_best"):
            text(p, "NEW BEST!", W / 2, 134, 10, GOLD, "center", 200)
        text(p, "A: again   B: menu", W / 2, 150, 8, DIM, "center", 220)
