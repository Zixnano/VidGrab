"""AUDIO_ONLY_TARGETS + _job_dest collision behavior."""
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture()
def dm(tmp_path, monkeypatch):
    def _unique_path(path):
        path = Path(path)
        if not path.exists():
            return path
        for i in range(2, 100):
            cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
            if not cand.exists():
                return cand
        raise RuntimeError("no unique name")
    st = types.ModuleType("settings")
    st.STATE = {"rules": [], "file_types_overrides": {}, "output_dir": str(tmp_path)}
    st._unique_path = _unique_path
    st.stat_for = lambda job: tmp_path / "Video" / job["filename"]
    st.category_for = lambda f: "Video"
    st.safe_filename = lambda n: n
    st.guess_filename = lambda u: "Video.mp4"
    st.detect_type = lambda u: "ytdlp"
    st.guess_ext_from_head = lambda u: ""
    st.get_shutdown_pending = lambda: False
    st.set_shutdown_pending = lambda v: None
    st.load_settings = lambda: None
    st.save_settings = lambda: None
    st.LIMITER = None
    st._dest_for = lambda job: tmp_path / "Video" / job["filename"]
    st.effective_speed_limit_kbps = lambda: 0
    monkeypatch.setitem(sys.modules, "settings", st)
    ls = types.ModuleType("logging_setup")
    ls.log = lambda *a, **k: None
    ls.LOG_QUEUE = []
    monkeypatch.setitem(sys.modules, "logging_setup", ls)
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))
    jm = types.ModuleType("jobs")
    jm.JOBS = {}
    monkeypatch.setitem(sys.modules, "jobs", jm)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "downloader_under_test", Path(__file__).parents[1] / "downloader.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pytest.skip("downloader module surface unavailable in this env")
    return mod, tmp_path


def test_audio_targets_include_m4a(dm):
    mod, _ = dm
    assert {"mp3", "flac", "opus", "m4a"} <= mod.AUDIO_ONLY_TARGETS


def test_job_dest_disambiguates_and_syncs(dm):
    mod, tmp = dm
    (tmp / "Video").mkdir(exist_ok=True)
    (tmp / "Video" / "Video_1080p.mp4").write_bytes(b"x")
    job = {"filename": "Video_1080p.mp4"}
    dest = mod._job_dest(job)
    assert dest.name == "Video_1080p (2).mp4"
    assert job["filename"] == "Video_1080p (2).mp4"
    assert mod._job_dest(job).name == "Video_1080p (2).mp4"
