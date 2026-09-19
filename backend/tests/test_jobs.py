"""Session 10 suffix + snapshot under stubbed settings."""
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
        "jobs_under_test", Path(__file__).parents[1] / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolution_suffix(jm):
    assert jm._resolution_suffix("1920x1080") == "1080p"
    assert jm._resolution_suffix("1280x720") == "720p"
    assert jm._resolution_suffix("garbage!!") == "garbage"
    assert jm._resolution_suffix(None) == ""


def test_multi_suffix_and_single_unchanged(jm):
    j1 = jm.new_job("https://youtube.com/watch?v=x", resolution="1920x1080", multi=True)
    assert jm.JOBS[j1]["filename"] == "Video_1080p.mp4"
    j2 = jm.new_job("https://youtube.com/watch?v=x", resolution="1920x1080")
    assert jm.JOBS[j2]["filename"] == "Video.mp4"


def test_snapshot_writes_via_settings_path(jm, tmp_path):
    j1 = jm.new_job("https://youtube.com/watch?v=x")
    jm.save_jobs_snapshot()
    # jobs.py writes via settings.JOBS_PATH (module-level, set to
    # tmp_path/"jobs.json" by the fixture) — it has no JOBS_PATH attribute
    # of its own, so read back from the same path the fixture configured.
    snap = json.loads((tmp_path / "jobs.json").read_text())
    assert j1 in snap
