"""v5 additions: quiet failure counters, safe game input, legacy import edge cases, diag route."""
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qt_stub import install, is_stub  # noqa: E402

install()

from companion import bridge, diag  # noqa: E402
from companion.store import CompanionStore  # noqa: E402


def test_diag_counts_repeats_and_logs_rarely(monkeypatch=None):
    diag.reset_for_tests()
    lines = []
    orig = diag._emit
    diag._emit = lines.append
    try:
        for _ in range(50):
            diag.note("k", "something failed", ValueError("boom"))
    finally:
        diag._emit = orig
    snap = diag.snapshot()
    assert snap["counts"]["k"] == 50 and "ValueError" in snap["last"]["k"]
    assert len(lines) == 1                                  # first one logged, repeats only counted


def test_diag_never_raises():
    diag.reset_for_tests()
    diag.note(object(), None, None)                         # odd input must not raise


def test_bad_registry_is_reported_once(tmp_path):
    from companion import store
    diag.reset_for_tests()
    path = tmp_path / "species.json"
    path.write_text("{not json", encoding="utf-8")
    orig = store.registry_path
    store.registry_path = lambda: path
    store._REGISTRY_CACHE.update(mtime="x", data={})
    try:
        for _ in range(3):
            reg = store.load_registry()
    finally:
        store.registry_path = orig
        store._REGISTRY_CACHE.update(mtime=None, data={})
    assert reg["species"][0]["id"] == "puppy"
    assert diag.snapshot()["counts"]["registry"] == 1


def _make_legacy(db_path):
    c = sqlite3.connect(str(db_path))
    c.execute("CREATE TABLE companion_state (id INTEGER PRIMARY KEY, data TEXT NOT NULL, updated_ts REAL NOT NULL)")
    c.execute("INSERT INTO companion_state VALUES (1, '{\"enabled\": true, \"level\": 4, \"coins\": 7}', 1.0)")
    c.commit()
    c.close()


def test_legacy_import_is_idempotent_and_handles_odd_paths(tmp_path):
    odd = tmp_path / "Jo Smith #1 100%"
    odd.mkdir()
    main = odd / "videograbber.db"
    _make_legacy(main)
    store = CompanionStore(tmp_path / "companion.db")
    assert store.import_legacy(main) is True
    assert store.load_state()["coins"] == 7
    store.save_state({"enabled": True, "level": 9, "coins": 1})
    assert store.import_legacy(main) is False               # second run copies nothing
    assert store.load_state()["level"] == 9                 # and does not overwrite newer progress
    again = CompanionStore(tmp_path / "companion.db")       # a restart is also a no-op
    assert again.import_legacy(main) is False and again.load_state()["level"] == 9


def test_diag_route_needs_nothing_secret(tmp_path):
    from flask import Flask
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()
    from companion.api_routes import companion_bp
    app = Flask(__name__)
    app.register_blueprint(companion_bp)
    diag.reset_for_tests()
    diag.note("demo", "hello")
    body = app.test_client().get("/companion/diag").get_json()
    assert body["counts"] == {"demo": 1}
    bridge.reset_for_tests()


def test_pad_hook_survives_a_broken_game(tmp_path):
    if is_stub():
        return
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
    QApplication.instance() or QApplication([])
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()
    from companion.integration import install as install_companion
    w = QMainWindow(); root = QWidget(); w._root = root; w.setCentralWidget(root); w.show()
    ctl = install_companion(w)
    ctl.set_enabled(True)
    ctl.open_games()
    ov = ctl.overlay

    def boom(*_a, **_k):
        raise RuntimeError("game bug")
    ov.handle = boom
    diag.reset_for_tests()
    assert ctl.pad_hook("dpad", "down") is True             # press swallowed, console not trapped
    assert diag.snapshot()["counts"]["pad_hook"] == 1
    assert ctl.pad_hook("dpad", "down") is False            # overlay closed, shell is back to normal
    bridge.reset_for_tests()
