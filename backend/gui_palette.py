"""Palette state for the GUI. Read colors as gui_palette.ACCENT at paint time; never copy them with from-import."""

# Palette-derived (single source of truth: palette.py).
from palette import resolve_palette


_PAL = resolve_palette()
ACCENT = _PAL["accent"]
BG = _PAL["bg_base"]
PANEL = _PAL["bg_panel"]
BORDER = _PAL["border"]
TEXT = _PAL["text"]
MUTED = _PAL["text_muted"]
DANGER = _PAL["error"]
WARN = _PAL["warning"]
SUCCESS = _PAL["success"]
TRACK = _PAL["bg_elevated"]   # progress-bar track

_ICON_CACHE = {}
_SVG_ICON_CACHE = {}


_TRAY_ICONS = {}

SEL = _PAL["bg_sel"]      # selected row / active nav background
SOFT = _PAL["text_soft"]  # secondary nav text


def _sync_palette(settings=None):
    """Re-read the theme tokens into this module's color globals. Painted
    pieces (progress delegate, row colors, tray icon) read these at paint
    time, so calling this and repainting is enough for a live theme change."""
    global _PAL, ACCENT, BG, PANEL, BORDER, TEXT, MUTED, DANGER, WARN, SUCCESS, TRACK, SEL, SOFT
    _PAL = resolve_palette(settings)
    ACCENT, BG, PANEL, BORDER = _PAL["accent"], _PAL["bg_base"], _PAL["bg_panel"], _PAL["border"]
    TEXT, MUTED = _PAL["text"], _PAL["text_muted"]
    DANGER, WARN, SUCCESS = _PAL["error"], _PAL["warning"], _PAL["success"]
    TRACK, SEL, SOFT = _PAL["bg_elevated"], _PAL["bg_sel"], _PAL["text_soft"]
    _ICON_CACHE.clear()
    _SVG_ICON_CACHE.clear()
    _TRAY_ICONS.clear()
    _STATUS_COLORS.update({"downloading": SUCCESS, "done": SUCCESS, "paused": WARN,
                           "stopped": DANGER, "error": DANGER, "queued": MUTED,
                           "skipped": _PAL["text_dim"]})


_STATUS_COLORS = {   # refreshed by _sync_palette()
    "downloading": SUCCESS, "done": SUCCESS, "paused": WARN,
    "stopped": DANGER, "error": DANGER, "queued": MUTED, "skipped": MUTED,
}


_SITE_ICONS = {}   # site -> (QPixmap, checked_at, has_favicon)
