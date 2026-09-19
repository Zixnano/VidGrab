"""Session Q behavior via a stubbed YoutubeDL (opts captured)."""
import os
import sys
import types
from pathlib import Path

import pytest


class FakeYDL:
    captured = []
    cookiefile_contents = []
    extract_info_return = {"formats": []}

    def __init__(self, opts):
        FakeYDL.captured.append(dict(opts))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        # Task 1: capture the cookie file's content while it still exists —
        # engines.py deletes it in a finally block right after this returns.
        cf = FakeYDL.captured[-1].get("cookiefile")
        if cf:
            FakeYDL.cookiefile_contents.append(Path(cf).read_text())
        else:
            FakeYDL.cookiefile_contents.append(None)

    def extract_info(self, url, download=False):
        return FakeYDL.extract_info_return


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


# --- Task 1: cookie handoff (replaces cookiesfrombrowser) ---

def test_no_cookiesfrombrowser_present(eng, tmp_path):
    e, _ = eng
    e.download("https://www.youtube.com/watch?v=x", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert "cookiesfrombrowser" not in _last()


def test_no_cookie_means_no_cookiefile(eng, tmp_path):
    e, _ = eng
    e.download("u", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert "cookiefile" not in _last()


def test_cookie_written_as_netscape_file_and_cleaned_up(eng, tmp_path):
    e, _ = eng
    FakeYDL.cookiefile_contents.clear()
    opts = {"cookie": "foo=bar; baz=qux"}
    e.download("https://www.youtube.com/watch?v=x", None, tmp_path / "x.mp4", opts, lambda p: None)
    cookiefile = _last()["cookiefile"]
    content = FakeYDL.cookiefile_contents[-1]
    assert content.splitlines()[0] == "# Netscape HTTP Cookie File"
    assert ".youtube.com\tTRUE\t/\tFALSE\t0\tfoo\tbar" in content
    assert ".youtube.com\tTRUE\t/\tFALSE\t0\tbaz\tqux" in content
    # cleaned up in the finally block after download() returns
    assert not os.path.exists(cookiefile)


def test_cookie_falls_back_to_header(eng, tmp_path):
    e, _ = eng
    FakeYDL.cookiefile_contents.clear()
    opts = {"headers": {"Cookie": "sid=abc123"}}
    e.download("https://www.youtube.com/watch?v=x", None, tmp_path / "x.mp4", opts, lambda p: None)
    assert "sid\tabc123" in FakeYDL.cookiefile_contents[-1]


def test_opts_cookie_takes_priority_over_header(eng, tmp_path):
    e, _ = eng
    FakeYDL.cookiefile_contents.clear()
    opts = {"cookie": "a=1", "headers": {"Cookie": "b=2"}}
    e.download("https://www.youtube.com/watch?v=x", None, tmp_path / "x.mp4", opts, lambda p: None)
    content = FakeYDL.cookiefile_contents[-1]
    assert "a\t1" in content
    assert "b\t2" not in content


# --- Task 2: full format list (probe) ---

def test_probe_keeps_audio_only_and_drops_storyboards(eng):
    e, _ = eng
    FakeYDL.extract_info_return = {"formats": [
        {"format_id": "1", "ext": "mp4", "resolution": "640x360",
         "vcodec": "avc1.42", "acodec": "mp4a.40", "format_note": "360p"},
        {"format_id": "2", "ext": "mp4", "resolution": "1920x1080",
         "vcodec": "avc1.64", "acodec": "none", "format_note": "1080p"},
        {"format_id": "3", "ext": "m4a", "resolution": "",
         "vcodec": "none", "acodec": "mp4a.40", "format_note": "audio"},
        {"format_id": "4", "ext": "mhtml", "resolution": "",
         "vcodec": "none", "acodec": "none", "format_note": "storyboard"},
    ]}
    formats = e.probe("https://www.youtube.com/watch?v=x")
    ids = [f.format_id for f in formats]
    assert "4" not in ids                 # storyboard dropped
    assert set(ids) == {"1", "2", "3"}    # video-only + audio-only both kept
    assert ids[0] == "2"                  # highest resolution first
    assert ids[-1] == "3"                 # audio-only sorts last


def test_probe_prefers_mp4_h264_at_same_resolution(eng):
    e, _ = eng
    FakeYDL.extract_info_return = {"formats": [
        {"format_id": "webm720", "ext": "webm", "resolution": "1280x720",
         "vcodec": "vp9", "acodec": "opus", "format_note": ""},
        {"format_id": "mp4720", "ext": "mp4", "resolution": "1280x720",
         "vcodec": "avc1.4d", "acodec": "mp4a.40", "format_note": ""},
    ]}
    formats = e.probe("u")
    assert [f.format_id for f in formats] == ["mp4720", "webm720"]


# --- BUGS.md #7: warn (don't block) on an empty cookie for YouTube ---

@pytest.fixture()
def eng_with_module(tmp_path, monkeypatch):
    """Like eng(), but also hands back the loaded module itself so a test
    can monkeypatch its `log` name and capture what got logged."""
    st = types.ModuleType("settings")
    st.STATE = {"subtitle_languages": ["en"], "engines_disabled": [],
                "preferred_engines": [], "per_site_engine": {}}
    monkeypatch.setitem(sys.modules, "settings", st)
    yt = types.ModuleType("yt_dlp")
    yt.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", yt)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "engines_under_test_logcap", Path(__file__).parents[1] / "engines.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _captured_log(mod, monkeypatch):
    logged = []
    monkeypatch.setattr(mod, "log", lambda *a: logged.append(" ".join(str(x) for x in a)))
    return logged


def test_warns_on_empty_cookie_for_youtube(eng_with_module, tmp_path, monkeypatch):
    mod = eng_with_module
    logged = _captured_log(mod, monkeypatch)
    e = mod.YtDlpEngine()
    e.download("https://www.youtube.com/watch?v=x", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert any("no cookies available" in msg for msg in logged)


def test_no_warning_when_cookie_present(eng_with_module, tmp_path, monkeypatch):
    mod = eng_with_module
    logged = _captured_log(mod, monkeypatch)
    e = mod.YtDlpEngine()
    e.download("https://www.youtube.com/watch?v=x", None, tmp_path / "x.mp4",
               {"cookie": "a=1"}, lambda p: None)
    assert not any("no cookies available" in msg for msg in logged)


def test_no_warning_for_non_youtube_without_cookie(eng_with_module, tmp_path, monkeypatch):
    mod = eng_with_module
    logged = _captured_log(mod, monkeypatch)
    e = mod.YtDlpEngine()
    e.download("https://example.com/video.mp4", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert not any("no cookies available" in msg for msg in logged)


def test_warning_fires_for_youtu_be_short_links_too(eng_with_module, tmp_path, monkeypatch):
    mod = eng_with_module
    logged = _captured_log(mod, monkeypatch)
    e = mod.YtDlpEngine()
    e.download("https://youtu.be/x", None, tmp_path / "x.mp4", {}, lambda p: None)
    assert any("no cookies available" in msg for msg in logged)

