"""Wires the companion into MainWindow. install(main_window) is the only entry point.

One CompanionController owns everything the desktop side needs: the sprite cache, the needs timer,
the Companion menu, the pet window, the game overlay and the shell pad hook. It lives as long as the
main window, so turning the pet on from any place (menu, pet window, browser extension) starts and
stops the same timer.
"""
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from . import bridge, diag
from .games.overlay import GameOverlay
from .sprites import SpriteBank
from .sticker_board import StickerBoardDialog
from .stickers import StickerManager
from .ui import CompanionDialog

NEEDS_TICK_MS = 60_000


def _notice_text(kind, data):
    if kind == "level_up":
        return "Level %d! I'm growing!" % data.get("level", 0)
    if kind == "evolved":
        return "I hatched!" if data.get("from") == "egg" else "I grew into a %s!" % data.get("stage", "big pal")
    if kind == "achievement":
        return "Achievement: %s" % data.get("title", "")
    if kind == "download":
        return "Got it! +%d XP" % data.get("xp", 0)
    return ""


class CompanionSignals(QObject):
    """Core listeners can fire on any thread. Emitting a signal queues the work onto the GUI thread."""
    changed = Signal()
    notice = Signal(str, str)


class CompanionController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.core = bridge.initialize()
        self.bank = SpriteBank()
        self.signals = CompanionSignals(self)
        self.dialog = None
        self.overlay = None
        self._manager = None

        self.needs_timer = QTimer(self)
        self.needs_timer.setInterval(NEEDS_TICK_MS)
        self.needs_timer.timeout.connect(self._tick)

        self._build_menu()
        self.signals.changed.connect(self._sync)
        self.core.add_listener(self._on_core)
        window._vg_pad_hook = self.pad_hook
        self._sync()

    # ---- core -> GUI --------------------------------------------------------------
    def _on_core(self, kind, data):
        text = _notice_text(kind, data)
        if text:
            self.signals.notice.emit(kind, text)
        self.signals.changed.emit()

    def _tick(self):
        self.core.tick()
        self.core.mark_daily_open()              # a no-op unless the local day has rolled over

    def _sync(self):
        on = self.core.state.enabled
        if on and not self.needs_timer.isActive():
            self.needs_timer.start()
        elif not on and self.needs_timer.isActive():
            self.needs_timer.stop()
        self.toggle_action.blockSignals(True)
        self.toggle_action.setChecked(on)
        self.toggle_action.blockSignals(False)
        self.games_action.setEnabled(on)

    # ---- menu ---------------------------------------------------------------------------
    def _build_menu(self):
        menu = self.window.menuBar().addMenu("Companion")
        self.open_action = QAction("Open companion\u2026", self.window)
        self.open_action.triggered.connect(self.open_dialog)
        self.games_action = QAction("Play games\u2026", self.window)
        self.games_action.triggered.connect(self.open_games)
        self.stickers_action = QAction("Sticker board\u2026", self.window)
        self.stickers_action.triggered.connect(self.open_stickers)
        self.toggle_action = QAction("Companion on", self.window)
        self.toggle_action.setCheckable(True)
        self.toggle_action.toggled.connect(self.set_enabled)
        for action in (self.open_action, self.games_action, self.stickers_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.toggle_action)

    # ---- actions ---------------------------------------------------------------------------
    def set_enabled(self, value):
        self.core.set_enabled(bool(value))
        self._sync()

    def care(self, action):
        return self.core.care(action)

    def open_dialog(self):
        if self.dialog is None:
            self.dialog = CompanionDialog(self, self.window)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def open_games(self):
        if not self.core.state.enabled:
            QMessageBox.information(self.window, "Companion", "Turn the companion on first (Companion menu).")
            return
        if self.overlay is not None and self.overlay.isVisible():
            self.overlay.raise_()
            self.overlay.setFocus()
            return
        parent = getattr(self.window, "_root", None) or self.window.centralWidget() or self.window
        self.overlay = GameOverlay(self, parent)
        self.overlay.closed.connect(self._overlay_closed)

    def _overlay_closed(self):
        self.overlay = None

    def open_stickers(self):
        if self._manager is None:
            self._manager = StickerManager(self.core.store, bridge.data_dir())
        StickerBoardDialog(self.core, self._manager, self.window).exec()

    # ---- shell buttons ------------------------------------------------------------------------
    def pad_hook(self, kind, name):
        """MainWindow calls this first for every shell press. True means a game took it."""
        overlay = self.overlay
        try:
            if overlay is not None and overlay.isVisible():
                return bool(overlay.pad(kind, name))
        except Exception as exc:
            # A game bug must never trap the console: log it, close the game screen, swallow this press.
            diag.note("pad_hook", "game input failed; closing the game screen", exc)
            try:
                overlay.close_overlay()
            except Exception:
                pass
            return True
        return False


def install(main_window):
    """Create the controller and attach it to the window. Safe to call once per window."""
    controller = CompanionController(main_window)
    main_window._vg_companion = controller
    return controller
