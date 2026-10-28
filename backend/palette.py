"""Color tokens. Single source of truth for every surface in the app.

resolve_palette(settings_state) = preset + accent + per-token overrides,
then the derived tokens (sidebar, input, hover, selection, soft text) are
computed from the result, so a preset only has to define the base colors.
"""
import re

_MONO = '"JetBrains Mono", "Cascadia Mono", Consolas, "Courier New", monospace'
_SANS = '-apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif'
_SEGOE = '"Segoe UI", -apple-system, Roboto, "Helvetica Neue", sans-serif'


def build_palette():
    return {
        # Base surfaces
        "bg_base":     "#0a0a0f",   # window background
        "bg_panel":    "#12121a",   # panels, table background
        "bg_elevated": "#1a1a24",   # dialogs, dropdowns, header rows

        # Borders
        "border":      "#242430",
        "border_hi":   "#33333f",   # hover/focus borders
        "border_soft": "",          # Batch 3: optional faint border, a CSS
                                    # rgba() string a preset can set for its
                                    # panels / table rows when border_w is 0
                                    # (Glass). Empty = unused.

        # Text
        "text":        "#e6e6eb",
        "text_muted":  "#8a8a95",
        "text_dim":    "#5a5a65",

        # Window frame (toolbar, menu bar, status bar, margins) when the
        # colored frame is on. gui_style derives its highlight/edge shades.
        "frame":       "#d9d3c0",

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
        # Layout tokens (v5.5): shape and spacing live apart from the colors,
        # so a preset can restyle shapes without touching a color.
        "radius_sm":   "6px",       # menu items, list items, menu-bar hover
        "radius_chip": "10px",      # small chips
        "pad_scale":   1.0,         # density: multiplies every padding in gui_style
        # Batch 2 layout tokens: border thickness (px), font size multiplier
        # (on top of the Text size zoom) and the global font weight.
        "border_w":    1,
        "font_scale":  1.0,
        "font_weight": "normal",
        "font":        _SANS,
        "font_mono":   '"JetBrains Mono", "Consolas", monospace',
    }


# Presets only list what differs from the default. Batch 2: each one has its
# own shape, density and type (radius / pad_scale / font ...), not just colors.
_PRESET_DIFFS = {
    # Default: tight corners, standard density, Segoe UI.
    "amoled_black": dict(radius="4px", radius_sm="3px", radius_chip="6px",
                         pad_scale=1.0, font=_SEGOE),
    # Softer and roomier.
    "dark_gray": dict(bg_base="#16161c", bg_panel="#1d1d26", bg_elevated="#262630",
                      border="#30303c", border_hi="#3f3f4b",
                      radius="12px", radius_sm="8px", radius_chip="12px", pad_scale=1.08),
    # Square corners, thick bright borders, bold type.
    "high_contrast": dict(bg_base="#000000", bg_panel="#0d0d0d", bg_elevated="#1a1a1a",
                          border="#cccccc", border_hi="#ffffff", text="#ffffff",
                          text_muted="#cccccc", text_dim="#999999", accent="#00e5ff",
                          radius="0px", radius_sm="0px", radius_chip="0px",
                          pad_scale=1.05, border_w=3, font_weight="bold"),
    # Dark LCD screen, muted green, monospace type (the handheld screen).
    # Selecting it also turns the handheld shell on (see gui_theme_page).
    "handheld": dict(bg_base="#101412", bg_panel="#171c19", bg_elevated="#222925",
                     border="#39413b", border_hi="#59625a", text="#f0ede2",
                     text_muted="#a3aaa0", text_dim="#6f786f", accent="#8ccf72",
                     accent_dim="#4e8b45", success="#8ccf72", warning="#d6b85a",
                     error="#e56a62", info="#7894c8", radius="14px", font=_MONO,
                     radius_sm="8px", radius_chip="14px", pad_scale=1.2),
    # Light four-shade green LCD: square, monospace, thick borders.
    "classic_lcd": dict(bg_base="#9bbc0f", bg_panel="#8bac0f", bg_elevated="#7d9d0e",
                        border="#306230", border_hi="#0f380f", text="#0f380f",
                        text_muted="#306230", text_dim="#4a7a2a", accent="#0f380f",
                        accent_dim="#306230", success="#0f380f", warning="#5c5a0a",
                        error="#7a1f12", info="#306230", radius="0px", font=_MONO,
                        radius_sm="0px", radius_chip="0px", pad_scale=0.9, border_w=3),
    # Lighter, frosted-looking blue-grey panels and big soft corners.
    # Batch 3: lighter still, radius up to 24px and border_w=0 — panels and
    # table rows keep their definition from border_soft, a faint 1px
    # semi-transparent white line, instead of a drawn border.
    "glass": dict(bg_base="#232e42", bg_panel="#303d59", bg_elevated="#3e4e6e",
                  border="#5a6d92", border_hi="#7e93ba", text="#f2f6ff",
                  text_muted="#c3cfe4", text_dim="#97a7c4", accent="#7fd4ff",
                  accent_dim="#4a9fc8", success="#6fe0a8", warning="#ffd479",
                  error="#ff8a8a", info="#8fb4ff", radius="24px", font=_SEGOE,
                  radius_sm="14px", radius_chip="24px", pad_scale=1.1,
                  border_w=0, border_soft="rgba(255,255,255,0.1)"),
    # Very dense: small type, tight corners, thin borders.
    "compact": dict(bg_base="#111113", bg_panel="#18181b", bg_elevated="#222226",
                    border="#2c2c31", border_hi="#3d3d44", text="#e4e4e8",
                    text_muted="#9a9aa4", text_dim="#6a6a74", accent="#4fc3f7",
                    accent_dim="#2f87b0", radius="2px", font=_SEGOE,
                    radius_sm="1px", radius_chip="2px", pad_scale=0.7,
                    border_w=1, font_scale=0.8),
}

PRESET_NAMES = {
    "amoled_black": "AMOLED Black",
    "dark_gray": "Dark Gray",
    "high_contrast": "High Contrast",
    "handheld": "Handheld",
    "classic_lcd": "Classic LCD",
    "glass": "Glass / Acrylic",
    "compact": "Compact / Minimal",
}

THEME_PRESETS = {k: dict(build_palette(), **v) for k, v in _PRESET_DIFFS.items()}
PALETTE = THEME_PRESETS["amoled_black"]

# The layout layer (shape and spacing). A color-only preset or theme file
# simply inherits the defaults above, so older themes keep working.
LAYOUT_KEYS = ("radius", "radius_sm", "radius_chip", "radius_pill", "pad_scale",
               "border_w", "font_scale", "font_weight", "border_soft")

# Computed from the base tokens; the user can still override them.
DERIVED = ("bg_side", "bg_input", "bg_hover", "bg_sel", "text_soft", "text_on_accent")


def zoom_factor(settings_state=None):
    """Global zoom from General > Text size (percent), clamped 0.7..2.0.
    gui_style multiplies fonts, paddings, margins, radii and control sizes by it."""
    try:
        return max(0.7, min(2.0, float((settings_state or {}).get("text_scale", 100) or 100) / 100.0))
    except (TypeError, ValueError):
        return 1.0


def scale_px(v, f=1.0):
    """Scale a "12px" style length by f; anything else is returned unchanged."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*px\s*$", str(v))
    if not m:
        return v
    return f"{round(float(m.group(1)) * f)}px"


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


def _lift(fg, backgrounds, toward, target):
    """Nudge fg toward `toward` (the main text color) in small steps until it
    reaches the contrast ratio `target` on every background."""
    cur = fg
    for _ in range(24):
        try:
            if all(contrast(cur, b) >= target for b in backgrounds):
                return cur
        except Exception:
            return fg
        cur = mix(cur, toward, 0.08)
    return cur


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


_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# Tokens that hold something other than a #hex color (kept as given when sane).
_NON_COLOR_TOKENS = {"radius", "radius_sm", "radius_chip", "radius_pill", "pad_scale",
                     "border_w", "font_scale", "font_weight", "border_soft",
                     "font", "font_mono"}


def _is_hex(v):
    return isinstance(v, str) and bool(_HEX_RE.match(v.strip()))


def _clean_overrides(raw, base):
    """Per-token overrides that are safe to use: a color token must be a
    #rgb / #rrggbb string. A theme file or settings.json edited by hand (or an
    import with 'red', 'rgb(1,2,3)', a number, null) used to reach the QSS
    builder and the contrast math as-is and could break the whole stylesheet or
    raise. Bad entries are dropped, so the preset value stays."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or v is None or v == "":
            continue
        if k in _NON_COLOR_TOKENS:
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out[k] = v
            continue
        if k in base or k in DERIVED:
            if _is_hex(v):
                out[k] = v.strip()
    return out


def resolve_palette(settings_state=None):
    """Preset + accent + per-token overrides -> concrete token dict."""
    state = settings_state if isinstance(settings_state, dict) else {}
    name = state.get("theme_preset", "amoled_black")
    base = THEME_PRESETS.get(name, THEME_PRESETS["amoled_black"]) if isinstance(name, str) \
        else THEME_PRESETS["amoled_black"]
    pal = dict(base)
    accent = state.get("accent")
    if _is_hex(accent) and accent.strip().lower() != base["accent"].lower():
        accent = accent.strip()
        pal["accent"] = accent
        pal["accent_dim"] = mix(accent, pal["bg_base"], 0.35)
    overrides = _clean_overrides(state.get("theme_tokens"), base)
    for k, v in overrides.items():
        if k in pal:
            pal[k] = v
    # v5.5 accessibility floor: secondary text reaches WCAG AA (4.5:1) and faint
    # text 3:1 on the surfaces it sits on. A color the user set explicitly is
    # left alone. Done before _derive so text_soft follows the lifted value.
    _surf = [pal["bg_base"], pal["bg_panel"], pal["bg_elevated"]]
    if "text_muted" not in overrides:
        pal["text_muted"] = _lift(pal["text_muted"], _surf, pal["text"], 4.5)
    if "text_dim" not in overrides:
        pal["text_dim"] = _lift(pal["text_dim"], _surf, pal["text"], 3.0)
    _derive(pal)
    for k, v in overrides.items():          # explicit overrides of derived tokens win
        if k in DERIVED:
            pal[k] = v
    return pal
