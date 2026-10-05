"""Tests for v5 helpers: naming, proxy, source site, yt-dlp version compare."""
import pytest

import settings
from settings import (clean_title, generated_name, is_junk_stem, proxy_for,
                      site_host, source_site_for, _unique_path)
from ytdlp_update import version_key


@pytest.mark.parametrize("stem,junk", [
    ("watch", True), ("watch (2) (2)", True), ("index", True),
    ("2106192423772705044", True), ("video_1", True), ("a", True),
    ("Cool Song", False), ("001 - Tongue Rings", False),
])
def test_is_junk_stem(stem, junk):
    assert is_junk_stem(stem) is junk


def test_clean_title_strips_counts_and_site_suffix():
    assert clean_title("(3) Do people WANT to PROMPT their games - YouTube") \
        == "Do people WANT to PROMPT their games"
    assert clean_title("Plain title") == "Plain title"


def test_unique_path_does_not_stack_suffixes(tmp_path):
    (tmp_path / "watch (2).mp4").write_text("x")
    assert _unique_path(tmp_path / "watch (2).mp4").name == "watch (3).mp4"
    (tmp_path / "a.mp4").write_text("x")
    (tmp_path / "a (2).mp4").write_text("x")
    assert _unique_path(tmp_path / "a.mp4").name == "a (3).mp4"


def test_source_site_and_generated_name():
    assert site_host("https://www.youtube.com/watch?v=1") == "youtube.com"
    assert source_site_for(None, "http://127.0.0.1:5757/x",
                           "https://cdn.example.com/a.mp4") == "cdn.example.com"
    assert generated_name("https://www.youtube.com/watch?v=1").startswith("youtube_")


def test_proxy_scope(monkeypatch):
    monkeypatch.setitem(settings.STATE, "proxy_url", "http://127.0.0.1:8080")
    monkeypatch.setitem(settings.STATE, "proxy_scope", "youtube")
    assert proxy_for("https://www.youtube.com/watch?v=1") == "http://127.0.0.1:8080"
    assert proxy_for("https://example.com/a.zip") is None
    monkeypatch.setitem(settings.STATE, "proxy_scope", "all")
    assert proxy_for("https://example.com/a.zip") == "http://127.0.0.1:8080"
    monkeypatch.setitem(settings.STATE, "proxy_url", "ftp://nope")
    assert proxy_for("https://www.youtube.com/watch?v=1") is None
    monkeypatch.setitem(settings.STATE, "proxy_url", "")
    assert proxy_for("https://www.youtube.com/watch?v=1") is None


def test_version_key_orders_ytdlp_versions():
    assert version_key("2026.08.19") < version_key("2026.10.01")
    assert version_key("2026.8.19.1") > version_key("2026.8.19")
