"""v4.0.1 Q2: restored v3.3-shaped jobs upgrade cleanly."""
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture()
def jm(tmp_path, monkeypatch):
    st = types.ModuleType("settings")
    st.STATE = {"rules": [], "file_types_overrides": {}, "output_dir": str(tmp_path)}
    st.JOBS_PATH = tmp_path / "jobs.json"
    st.category_for = lambda f: "Video"
    st.safe_filename = lambda n: n
    st.guess_filename = lambda u: "Video.mp4"
    st.detect_type = lambda u: "ytdlp"
    st.guess_ext_from_head = lambda u: ""
    st.stat_for = lambda job: tmp_path / "Video" / job["filename"]
    st.get_shutdown_pending = lambda: False
    st.set_shutdown_pending = lambda v: None
    monkeypatch.setitem(sys.modules, "settings", st)
    ls = types.ModuleType("logging_setup")
    ls.log = lambda *a, **k: None
    ls.LOG_QUEUE = []
    monkeypatch.setitem(sys.modules, "logging_setup", ls)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "jobs_mig_test", Path(__file__).parents[1] / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, st


def test_migrate_job_backfills_v4_fields(jm):
    mod, _ = jm
    v33 = {"id": "1", "url": "https://x/y.mp4", "filename": "y.mp4",
           "category": "Video", "type": "direct", "status": "done",
           "size_total": 10, "size_done": 10}
    j = mod._migrate_job(dict(v33))
    assert j["phase"] == "idle"
    assert j["target_format"] is None and j["format_id"] is None
    assert j["completed_ts"] is None and j["error"] is None
    assert "created_ts" in j
    # existing fields untouched
    assert j["size_done"] == 10 and j["status"] == "done"


def test_load_snapshot_upgrades_v33_job(jm):
    mod, st = jm
    v33 = {"1": {"id": "1", "url": "https://x/y.mp4", "filename": "y.mp4",
                 "category": "Video", "type": "direct", "status": "done",
                 "size_total": 10, "size_done": 10}}
    st.JOBS_PATH.write_text(json.dumps(v33))
    mod.load_jobs_snapshot()
    assert mod.JOBS["1"]["phase"] == "idle"
