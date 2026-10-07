"""Color tokens. Single source of truth for every surface in the app.

resolve_palette(settings_state) = preset + accent + per-token overrides,
then the derived tokens (sidebar, input, hover, selection, soft text) are
computed from the result, so a preset only has to define the base colors.
"""

_MONO = '"JetBrains Mono", "Cascadia Mono", Consolas, "Courier New", monospace'
_SANS = '-apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif'


def build_palette():
    return {
        # Base surfaces
        "bg_base":     "#0a0a0f",   # window background
        "bg_panel":    "#12121a",   # panels, table background
        "bg_elevated": "#1a1a24",   # dialogs, dropdowns, header rows

        # Borders
        "border":      "#242430",
        "border_hi":   "#33333f",   # hover/focus borders

        # Text
        "text":        "#e6e6eb",
        "text_muted":  "#8a8a95",
        "text_dim":    "#5a5a65",

        # Accent
        "accent":      "#26c6da",
        "accent_dim":  "#1a8a99",

        # Status colors
        "success":     "#4caf50",
        "warning":     "#ffb300",
        "error":       "#ef5350",
        "info":        "#42a5f5",

        "radius":      "10px",
        "radius_pill": "999px",
        "font":        _SANS,
        "font_mono":   '"JetBrains Mono", "Consolas", monospace',
    }


# Presets only list what differs from the default.
_PRESET_DIFFS = {
    "amoled_black": {},
    "dark_gray": dict(bg_base="#16161c", bg_panel="#1d1d26", bg_elevated="#262630",
                      border="#30303c", border_hi="#3f3f4b"),
    "high_contrast": dict(bg_base="#000000", bg_panel="#0d0d0d", bg_elevated="#1a1a1a",
                          border="#555555", border_hi="#777777", text="#ffffff",
                          text_muted="#cccccc", text_dim="#999999", accent="#00e5ff"),
    # Dark LCD screen, muted green, monospace type (the handheld screen).
    "handheld": dict(bg_base="#101412", bg_panel="#171c19", bg_elevated="#222925",
                     border="#39413b", border_hi="#59625a", text="#f0ede2",
                     text_muted="#a3aaa0", text_dim="#6f786f", accent="#8ccf72",
                     accent_dim="#4e8b45", success="#8ccf72", warning="#d6b85a",
                     error="#e56a62", info="#7894c8", radius="8px", font=_MONO),
    # Light four-shade green LCD.
    "classic_lcd": dict(bg_base="#9bbc0f", bg_panel="#8bac0f", bg_elevated="#7d9d0e",
                        border="#306230", border_hi="#0f380f", text="#0f380f",
                        text_muted="#306230", text_dim="#4a7a2a", accent="#0f380f",
                        accent_dim="#306230", success="#0f380f", warning="#5c5a0a",
                        error="#7a1f12", info="#306230", radius="4px", font=_MONO),
}

PRESET_NAMES = {
    "amoled_black": "AMOLED Black",
    "dark_gray": "Dark Gray",
    "high_contrast": "High Contrast",
    "handheld": "Handheld",
    "classic_lcd": "Classic LCD",
}

THEME_PRESETS = {k: dict(build_palette(), **v) for k, v in _PRESET_DIFFS.items()}
PALETTE = THEME_PRESETS["amoled_black"]

# Computed from the base tokens; the user can still override them.
DERIVED = ("bg_side", "bg_input", "bg_hover", "bg_sel", "text_soft", "text_on_accent")


def _rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a, b, t):
    """Blend colour a toward b by t (0..1); returns '#rrggbb'."""
    try:
        (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    except Exception:
        return a
    return "#%02x%02x%02x" % (round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t),
                              round(b1 + (b2 - b1) * t))


def luminance(h):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    """WCAG contrast ratio, 1 (none) to 21 (black on white)."""
    try:
        la, lb = luminance(a), luminance(b)
    except Exception:
        return 21.0
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _derive(p):
    p["bg_side"] = p["bg_base"]
    p["bg_input"] = p["bg_base"]
    p["bg_hover"] = p["bg_elevated"]
    p["bg_sel"] = mix(p["bg_panel"], p["accent"], 0.22)
    p["text_soft"] = mix(p["text_muted"], p["text"], 0.35)
    if contrast(p["bg_base"], p["accent"]) >= 3:
        p["text_on_accent"] = p["bg_base"]
    else:
        p["text_on_accent"] = "#ffffff" if luminance(p["accent"]) < 0.4 else "#000000"
    return p


def resolve_palette(settings_state=None):
    """Preset + accent + per-token overrides -> concrete token dict."""
    state = settings_state or {}
    name = state.get("theme_preset", "amoled_black")
    base = THEME_PRESETS.get(name, THEME_PRESETS["amoled_black"])
    pal = dict(base)
    accent = state.get("accent")
    if accent and accent.lower() != base["accent"].lower():
        pal["accent"] = accent
        pal["accent_dim"] = mix(accent, pal["bg_base"], 0.35)
    overrides = {k: v for k, v in (state.get("theme_tokens") or {}).items() if v}
    for k, v in overrides.items():
        if k in pal:
            pal[k] = v
    _derive(pal)
    for k, v in overrides.items():          # explicit overrides of derived tokens win
        if k in DERIVED:
            pal[k] = v
    return pal
