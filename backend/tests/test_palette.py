from palette import PALETTE, THEME_PRESETS, resolve_palette


def test_three_presets_declared_only_palette_keys():
    for name, preset in THEME_PRESETS.items():
        for key in preset:
            assert key in PALETTE, (name, key)


def test_resolve_order_preset_beats_tokens():
    s = {"theme_preset": "dark_gray", "theme_tokens": {"accent": "#ff0000"}}
    assert resolve_palette(s)["accent"] == "#ff0000"
    assert resolve_palette(s)["bg_base"] == THEME_PRESETS["dark_gray"]["bg_base"]


def test_accent_applies_to_accent_slot():
    p = resolve_palette({"accent": "#123456", "theme_tokens": {}})
    assert p["accent"] == "#123456"


def test_unknown_preset_falls_back():
    p = resolve_palette({"theme_preset": "nope"})
    assert p["bg_base"] == PALETTE["bg_base"]
