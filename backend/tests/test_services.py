"""Animation logic, the Flask routes and the bridge worker (no Qt needed)."""
import sys
import time
import types

from companion import bridge, events
from companion.animation import Animator, pose_for, sheet_cell, speech_for
from companion.store import load_registry


# ---- animation ---------------------------------------------------------------------------
def test_pose_rules():
    assert pose_for("happy") == "idle"
    assert pose_for("hungry") == "sad" and pose_for("sleeping") == "sleeping"
    assert pose_for("happy", "celebrating") == "celebrating"       # reaction beats mood
    assert pose_for("happy", None, "egg", 0.1) == "idle"
    assert pose_for("happy", None, "egg", 0.9) == "cracked"
    assert pose_for("happy", "celebrating", "egg") == "wobble"


def test_speech_has_a_line_for_every_case():
    for mood in ("sleeping", "hungry", "lonely", "happy", "content", "curious"):
        assert speech_for(mood)
    assert "break" in speech_for("happy", enabled=False)
    assert speech_for("happy", "sad")


def test_every_species_and_pose_resolves_to_a_real_frame():
    reg = load_registry()
    poses = ("idle", "content", "happy", "curious", "sleeping", "sad", "excited", "celebrating")
    for entry in reg["species"]:
        cells = entry["columns"] * entry["rows"]
        anim = Animator(entry, reg["animation"])
        for pose in poses:
            for t in (0.0, 0.3, 1.7, 4.65, 9.9):
                index, dy = anim.frame(pose, t)
                assert 0 <= index < cells, (entry["id"], pose, index)
                assert 0 <= dy <= 3
    egg = Animator(reg["egg"], reg["animation"])
    for pose in ("idle", "wobble", "cracked"):
        assert 0 <= egg.frame(pose, 1.0)[0] < reg["egg"]["columns"]


def test_blink_only_on_idle_for_species_that_have_one():
    reg = load_registry()
    cat = next(s for s in reg["species"] if s["id"] == "cat")
    pup = next(s for s in reg["species"] if s["id"] == "puppy")
    a = Animator(cat, reg["animation"])
    t_blink = (reg["animation"]["blink_period_ms"] - 10) / 1000.0
    assert a.frame("idle", t_blink)[0] == 1
    assert a.frame("happy", t_blink)[0] == 2
    assert Animator(pup, reg["animation"]).frame("idle", t_blink)[0] == 0


def test_sheet_cell_row_major():
    assert sheet_cell(0, 4) == (0, 0) and sheet_cell(5, 4) == (64, 64) and sheet_cell(7, 8) == (448, 0)


def test_sheets_exist_and_match_the_manifest():
    from pathlib import Path
    from PIL import Image
    assets = Path(__file__).resolve().parents[1] / "backend" / "companion" / "assets"
    reg = load_registry()
    size = reg["frame_size"]
    for entry in reg["species"] + [reg["egg"]]:
        im = Image.open(assets / entry["sheet"])
        assert im.size == (entry["columns"] * size, entry["rows"] * size), entry["sheet"]


# ---- Flask routes ---------------------------------------------------------------------------
def _app(tmp_path):
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()
    from flask import Flask
    from companion.api_routes import companion_bp
    app = Flask(__name__)
    app.register_blueprint(companion_bp)
    return app.test_client()


def test_routes_state_actions_and_errors(tmp_path):
    c = _app(tmp_path)
    assert c.get("/companion").get_json()["enabled"] is False
    assert c.post("/companion/action", json={"action": "pet"}).status_code == 400       # off
    assert c.post("/companion/action", json={"action": "enable"}).get_json()["ok"]
    assert c.post("/companion/action", json={"action": "pet"}).get_json()["ok"]
    assert c.post("/companion/action", json={"action": "bogus"}).status_code == 400
    assert c.post("/companion/action", json={"action": "species", "species": "cat"}).status_code == 400
    assert c.post("/companion/action", json={"action": "option", "name": "bubbles", "value": False}).get_json()["ok"]
    assert c.get("/companion").headers["Cache-Control"] == "no-store"
    assert c.get("/companion/achievements").get_json()["achievements"]
    assert c.get("/companion/stickers").get_json() == {"stickers": []}
    bridge.reset_for_tests()


def test_routes_manifest_and_sprite_safety(tmp_path):
    c = _app(tmp_path)
    m = c.get("/companion/manifest").get_json()
    assert m["frame_size"] == 64 and m["egg"]["sheet"] == "egg.png"
    ok = c.get("/companion/sprite/puppy.png")
    assert ok.status_code == 200 and ok.mimetype == "image/png"
    assert c.get("/companion/sprite/egg.png").status_code == 200
    for bad in ("..%2Fcore.py", "core.py", "stickers_builtin", "nope.png"):
        assert c.get("/companion/sprite/" + bad).status_code == 404
    assert c.get("/companion/sticker/doesnotexist").status_code == 404
    bridge.reset_for_tests()


# ---- bridge worker -----------------------------------------------------------------------------
def _wait(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_bridge_queues_events_and_ignores_them_when_off(tmp_path):
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()
    core = bridge.initialize()
    job = {"id": "7", "url": "https://example.test/a.mp4", "status": "done", "size_total": 8_000_000}
    events.publish("job_finished", {"job": job})                 # companion is OFF
    time.sleep(0.2)
    assert core.state.downloads_counted == 0
    core.set_enabled(True)
    events.publish("job_finished", {"job": job})
    assert _wait(lambda: core.state.downloads_counted == 1)
    events.publish("job_failed", {"job": {"id": "8"}})
    assert _wait(lambda: core.reaction() == "sad")
    bridge.reset_for_tests()


def test_queue_active_never_writes_the_database(tmp_path):
    """The job state machine fires START on every progress tick. It must stay free."""
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()
    core = bridge.initialize()
    core.set_enabled(True)
    before = len(core.store.recent_events(500))
    for _ in range(200):
        events.publish("queue_active", {"job": {"id": "1"}})
    time.sleep(0.4)
    assert len(core.store.recent_events(500)) == before
    assert core.reaction() == "curious"
    bridge.reset_for_tests()


def test_initialize_is_idempotent(tmp_path):
    sys.modules["settings"] = types.SimpleNamespace(DB_PATH=str(tmp_path / "downloads.db"))
    bridge.reset_for_tests()
    a, b = bridge.initialize(), bridge.initialize()
    assert a is b
    bridge.reset_for_tests()
