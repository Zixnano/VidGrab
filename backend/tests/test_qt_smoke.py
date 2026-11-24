"""End to end wiring through the Qt layer: controller, pet window, game overlay, shell pad hook.

With PySide6 installed this runs against the real library, offscreen. Without it, qt_stub stands in:
that still executes every line of the constructors, signal wiring and game flow, so typos, wrong
attribute names and broken control flow are caught, but real Qt API misuse is not. Run it on your
own machine (or in CI) with PySide6 for the full check.
"""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qt_stub import install, is_stub  # noqa: E402

install()

from companion import bridge  # noqa: E402


def _window():
    if is_stub():
        from PySide6.QtWidgets import QMainWindow
        return QMainWindow()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
    QApplication.instance() or QApplication([])
    w = QMainWindow()
    root = QWidget()
    w._root = root
    w.setCentralWidget(root)
    w.resize(900, 600)
    w.show()
    return w


def _settings(tmp_path):
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()


def test_controller_dialog_and_game_overlay_flow(tmp_path):
    _settings(tmp_path)
    from companion.integration import install as install_companion
    window = _window()
    ctl = install_companion(window)
    core = ctl.core
    assert window._vg_companion is ctl and window._vg_pad_hook == ctl.pad_hook

    assert ctl.pad_hook("dpad", "up") is False             # no game open: shell keeps its normal job
    ctl.set_enabled(True)
    assert core.state.enabled

    ctl.open_dialog()
    dlg = ctl.dialog
    dlg._refresh_state()
    dlg._paint_sprite()
    dlg._care("pet")
    dlg._care("pet")                                       # cooldown message path
    dlg._toggle_enabled(True)

    ctl.open_games()
    ov = ctl.overlay
    assert ov is not None
    assert ctl.pad_hook("button", "power") is False        # power light is never taken
    assert ctl.pad_hook("dpad", "down") is True and ov.sel == 1
    assert ctl.pad_hook("dpad", "up") is True and ov.sel == 0
    assert ctl.pad_hook("button", "a") is True and ov.mode == "play"
    if not is_stub():
        window.isActiveWindow = lambda: True       # an offscreen window is never "active", which would auto-pause
    for _ in range(5):
        ov._frame()
    ctl.pad_hook("dpad", "left")
    ctl.pad_hook("button", "start")                        # pause
    assert ov.game.paused
    ctl.pad_hook("button", "start")                        # resume
    ov.game.paused = False
    ov.game.score = 7
    ov.game.done = True
    ov._frame()
    assert ov.mode == "result" and core.state.games_played == 1
    assert core.state.best_scores["catch_bytes"] == 7
    ctl.pad_hook("button", "b")                            # result -> menu
    assert ov.mode == "menu"
    ctl.pad_hook("button", "b")                            # menu -> close
    if not is_stub():
        assert ctl.overlay is None

    ctl.set_enabled(False)
    assert not core.state.enabled
    dlg._refresh_state()
    bridge.reset_for_tests()


def test_sticker_manager_limits_and_packs(tmp_path):
    if is_stub():
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from companion.store import CompanionStore
    from companion.stickers import StickerError, StickerManager

    store = CompanionStore(tmp_path / "companion.db")
    mgr = StickerManager(store, tmp_path)
    src = tmp_path / "pic.png"
    Image.new("RGBA", (40, 30), (200, 80, 160, 255)).save(src)
    made = mgr.import_image(src, "Test!", pixelate=True)
    assert Image.open(tmp_path / "stickers" / made["filename"]).size == (128, 128)
    assert [s["name"] for s in mgr.custom_items()] == ["Test"]
    pack = tmp_path / "pack.zip"
    mgr.export_pack(pack)
    assert len(mgr.import_pack(pack)) == 1 and len(store.list_stickers()) == 2
    wide = tmp_path / "wide.png"
    Image.new("RGBA", (5000, 10), (0, 0, 0, 255)).save(wide)
    for bad in (wide, tmp_path / "x.jpg"):
        try:
            mgr.import_image(bad)
            raise AssertionError("should have been rejected")
        except StickerError:
            pass
    assert mgr.delete(made["id"]) and not mgr.delete("nope")
    assert len(mgr.builtin_items(lambda key: False)) == 6      # two stickers stay locked
    assert len(mgr.builtin_items(lambda key: True)) == 8
