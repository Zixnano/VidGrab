"""The extension's JavaScript animator must pick exactly the same frames as the desktop one."""
import json
import shutil
import subprocess
from pathlib import Path

from companion.animation import Animator
from companion.store import load_registry, registry_path

HERE = Path(__file__).resolve().parent
POSES = ["idle", "content", "happy", "curious", "sleeping", "sad", "excited", "celebrating", "wobble", "cracked"]


def test_js_and_python_animators_agree():
    node = shutil.which("node")
    if not node:
        return                                              # nothing to compare against on this machine
    raw = subprocess.run([node, str(HERE / "js_anim_dump.js"), str(registry_path())],
                         capture_output=True, text=True, check=True).stdout
    dump = json.loads(raw)
    reg = load_registry()
    times = dump["times"]
    for entry in [dict(reg["egg"], id="egg")] + reg["species"]:
        anim = Animator(entry, reg["animation"])
        for pose in POSES:
            expected = [list(anim.frame(pose, t)) for t in times]
            assert dump["out"][entry["id"]][pose] == expected, (entry["id"], pose)


def test_extension_panel_runs_against_a_fake_dom():
    node = shutil.which("node")
    if not node:
        return
    r = subprocess.run([node, str(HERE / "js_panel_smoke.js"), str(registry_path())], capture_output=True, text=True)
    assert r.returncode == 0 and "panel ok" in r.stdout, r.stdout + r.stderr
