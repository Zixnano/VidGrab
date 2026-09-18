"""Color tokens. Single source of truth for every surface in the app.

Accent: cyan (#26c6da) — green reads as a Material success state; cyan
reads modern and differentiates from every other download manager's blue.
Background: near-black (#0a0a0f) — true black smears on LCD scroll.
"""


def build_palette():
    base = {
        # Base surfaces — near-black, not true black.
        "bg_base":     "#0a0a0f",   # window background
        "bg_panel":    "#12121a",   # panels, table background
        "bg_elevated": "#1a1a24",   # dialogs, dropdowns

        # Borders
        "border":      "#242430",
        "border_hi":   "#33333f",   # hover/focus borders

        # Text
        "text":        "#e6e6eb",
        "text_muted":  "#8a8a95",
        "text_dim":    "#5a5a65",

        # Accent — CYAN.
        "accent":      "#26c6da",
        "accent_dim":  "#1a8a99",

        # Status colors — semantic, not branded.
        "success":     "#4caf50",
        "warning":     "#ffb300",
        "error":       "#ef5350",
        "info":        "#42a5f5",

        # Radii
        "radius":      "10px",
        "radius_pill": "999px",

        # Font — system stack, no bundled font.
        "font":        '-apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif',
        "font_mono":   '"JetBrains Mono", "Consolas", monospace',
    }
    return base


PALETTE = build_palette()

# Theme presets (Session 13 derives these from PALETTE).
THEME_PRESETS = {
    "amoled_black": build_palette(),
    "dark_gray": dict(build_palette(), bg_base="#16161c", bg_panel="#1d1d26",
                      bg_elevated="#262630", border="#30303c", border_hi="#3f3f4b"),
    "high_contrast": dict(build_palette(), bg_base="#000000", bg_panel="#0d0d0d",
                          bg_elevated="#1a1a1a", border="#555555", border_hi="#777777",
                          text="#ffffff", text_muted="#cccccc", text_dim="#999999",
                          accent="#00e5ff"),
}


def resolve_palette(settings_state=None):
    """Preset + per-token overrides -> concrete token dict."""
    state = settings_state or {}
    pal = dict(THEME_PRESETS.get(state.get("theme_preset", "amoled_black"),
                                 THEME_PRESETS["amoled_black"]))
    if state.get("accent"):
        pal["accent"] = state["accent"]
    for k, v in (state.get("theme_tokens") or {}).items():
        if v and k in pal:
            pal[k] = v
    return pal
