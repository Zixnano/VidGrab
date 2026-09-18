"""Session Q behavior via a stubbed YoutubeDL (opts captured)."""
import sys
import types
from pathlib import Path

import pytest


class FakeYDL:
    captured = []

    def __init__(self, opts):
        FakeYDL.captured.append(dict(opts))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        pass


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    st = types.ModuleType("settings")
    st.STATE = {"subtitle_languages": ["en"], "engines_disabled": [],
                "preferred_engines": [], "per_site_engine": {}}
    monkeypatch.setitem(sys.modules, "settings", st)
    yt = types.ModuleType("yt_dlp")
    yt.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", yt)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "engines_under_test", Path(__file__).parents[1] / "engines.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.YtDlpEngine(), st


def _last():
    return FakeYDL.captured[-1]


def test_default_format_string_and_mkv_merge(eng, tmp_path):
    e, _ = eng
    e.download("u", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert _last()["format"].startswith("bestvideo[ext=mp4][vcodec^=avc1]")
    assert _last()["merge_output_format"] == "mkv"
    assert _last()["subtitleslangs"] == ["en"]


def test_subtitle_validation(eng, tmp_path):
    e, st = eng
    st.STATE["subtitle_languages"] = "en, es , fr"
    e.download("u", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert _last()["subtitleslangs"] == ["en", "es", "fr"]
    st.STATE["subtitle_languages"] = 123
    e.download("u", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert _last()["subtitleslangs"] == ["en"]


def test_audio_args_matrix(eng, tmp_path):
    e, _ = eng
    for tf, codec, args in [("mp3", "mp3", ["-q:a", "0"]),
                            ("opus", "opus", ["-b:a", "192k"]),
                            ("m4a", "m4a", ["-b:a", "256k"])]:
        e.download("u", None, tmp_path / "x.mp4", {"target_format": tf}, lambda p: None)
        assert _last()["postprocessors"][0]["preferredcodec"] == codec
        assert _last()["postprocessor_args"]["ExtractAudio"] == args
        assert "subtitleslangs" not in _last()


def test_explicit_mp4_target(eng, tmp_path):
    e, _ = eng
    e.download("u", None, tmp_path / "x.mp4", {"target_format": "mp4"}, lambda p: None)
    assert _last()["merge_output_format"] == "mp4"
