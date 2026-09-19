from pathlib import Path

import settings


def test_safe_filename_strips_illegal():
    # safe_filename replaces each illegal char with "_" (not a bare strip) —
    # see its docstring/ILLEGAL_FILENAME_CHARS_RE in settings.py.
    assert settings.safe_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_category_for_known_extensions():
    assert settings.category_for("movie.mp4") == "Video"
    assert settings.category_for("song.mp3") == "Music"
    assert settings.category_for("unknown.xyz99") in settings.CATEGORIES


def test_disk_usage_for_valid_path(tmp_path):
    d = settings.disk_usage_for(str(tmp_path))
    assert d["path"]
    assert d["total"] >= d["free"] >= 0
    assert d["total"] >= d["used"] >= 0


def test_disk_usage_for_missing_path_walks_up_to_existing_parent(tmp_path):
    missing = tmp_path / "does" / "not" / "exist"
    d = settings.disk_usage_for(str(missing))
    # walked up to an ancestor that does exist — never raises, never 0/0/0
    # just because the exact requested folder hasn't been created yet
    assert d["total"] > 0


def test_app_version_matches_extension_manifest():
    # QoL follow-through: settings.APP_VERSION is the single source of
    # truth for "what version is this" in the GUI (title bar, status bar,
    # About dialog, /version endpoint) — this guards against it silently
    # drifting from extension/manifest.json's version the way CATEGORIES
    # drifted between settings.py and gui_qt.py before that got fixed.
    import json
    manifest_path = Path(__file__).resolve().parents[2] / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == settings.APP_VERSION
