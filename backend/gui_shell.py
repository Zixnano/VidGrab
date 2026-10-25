"""Optional handheld shell drawn around the main window: plastic body, hinge,
dark screen bezel, D-pad, A/B, speaker, Select/Start and a power light.
The controls are real: they emit signals MainWindow connects to actions.
Pure painting, no backend calls."""
import sys

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPolygonF, QRadialGradient, QTransform)
from PySide6.QtWidgets import QHBoxLayout, QWidget

from palette import resolve_palette

# Every shell is a dict of parts:
#   top / body / bottom / edge   the case (gradient + outline)
#   dpad, dpad_hi                D-pad cross and A/B key fill (hi = lit top)
#   select_start                 SELECT/START pill (lit/shaded tones derived)
#   button_labels                A/B letters, POWER text, SELECT/START captions
#   power_led                    the LED dot
#   bezel                        the screen surround
# The user can override any part (settings "shell_colors"); see ShellFrame._col.
_CONTROLS = {
    "dpad": "#2b2e2c", "dpad_hi": "#4a4e4b", "select_start": "#e4dfcc",
    "button_labels": "#6f6a58", "power_led": "#5ee07a", "bezel": "#1b1f1d",
}


def _shell(top, body, bottom, edge, **controls):
    d = {"top": top, "body": body, "bottom": bottom, "edge": edge}
    d.update(_CONTROLS)
    d.update(controls)
    return d


SHELLS = {
    "cream": _shell("#efebdc", "#d9d3c0", "#b9b39f", "#8f8a78"),
    "black": _shell("#4a4d4b", "#2f3231", "#1b1d1c", "#0c0d0d",
                    dpad="#121413", select_start="#3f4240", button_labels="#8c908d"),
    "indigo": _shell("#6a6fc4", "#4a4fa8", "#2e3278", "#1d2050"),
    "silver": _shell("#d5d9dc", "#b8bdc0", "#8e9396", "#6a6f72"),
    "crystal": _shell("#f4f6f8", "#dfe4e8", "#bfc7cd", "#8d979e",
                      dpad="#3a4046", dpad_hi="#5a626a", select_start="#eef1f3",
                      button_labels="#6b747b"),
    "atomic_purple": _shell("#b58ae0", "#8a5cc4", "#5e3a96", "#3d2368",
                            dpad="#3b2260", dpad_hi="#5d3a94", select_start="#d9c5f0",
                            button_labels="#eadcf8", bezel="#150c24"),
    "graphite": _shell("#5b5f63", "#3c3f43", "#25282b", "#111315",
                       dpad="#1a1c1e", dpad_hi="#43474b", select_start="#585c60",
                       button_labels="#9aa0a5", bezel="#101213"),
    "ruby": _shell("#e0505a", "#b8222e", "#7e1019", "#4f0910",
                   dpad="#4a0f14", dpad_hi="#7a2028", select_start="#f0c9cc",
                   button_labels="#f2d2d4", bezel="#1a0a0c"),
    "sapphire": _shell("#3b5fc4", "#1b3280", "#0f1d4d", "#080f2b",
                       dpad="#0c1534", dpad_hi="#2b4290", select_start="#2e4182",
                       button_labels="#9fb4ee", bezel="#070b1a"),
    "mint": _shell("#d9f5e6", "#aee3c6", "#7cbf9b", "#4f8a6a",
                   dpad="#27403a", dpad_hi="#456b60", select_start="#e3f6ec",
                   button_labels="#3f6b57", bezel="#16241f"),
    "sunset": _shell("#ffb36b", "#f2825a", "#c8504f", "#8c2f3a",
                     dpad="#4a2030", dpad_hi="#7a3a4a", select_start="#ffe0c8",
                     button_labels="#5a2230", bezel="#1e0f16"),
    "bloody": _shell("#6a1a1a", "#4a1010", "#2a0808", "#150404",
                     dpad="#3a0808", dpad_hi="#5a1010", select_start="#6a2222",
                     button_labels="#e8c8c8", power_led="#c03030", bezel="#0a0202"),
}
SHELL_NAMES = {
    "cream": "Cream", "black": "Matte black", "indigo": "Indigo", "silver": "Silver",
    "crystal": "Crystal", "atomic_purple": "Atomic purple", "graphite": "Graphite",
    "ruby": "Ruby", "sapphire": "Sapphire", "mint": "Mint", "sunset": "Sunset",
    "bloody": "Bloody",
}
SHELL_PARTS = ("top", "body", "bottom", "edge") + tuple(_CONTROLS)
DARK_LUM = 0.06   # a case body darker than this gets the dark-theme bevels


def _lin(v):
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(c):
    return 0.2126 * _lin(c.red()) + 0.7152 * _lin(c.green()) + 0.0722 * _lin(c.blue())


COLUMN = 210   # width of the controls column


# ---------------------------------------------------------------------------
# Dark title bars (Windows). The native title bar is drawn by Windows, so it
# stays white unless each top-level window opts in to the immersive dark mode
# through DWM. No custom frameless title bar: the standard one just goes dark.
# ---------------------------------------------------------------------------
def _colorref(hex_value):
    """'#rrggbb' -> Windows COLORREF (0x00BBGGRR), or None if unparseable."""
    c = QColor(hex_value)
    if not c.isValid():
        return None
    return (c.blue() << 16) | (c.green() << 8) | c.red()


def apply_dark_titlebar(widget, dark=True, caption=None, text=None):
    """Switch one top-level window's native title bar to dark (or back to
    light). `caption` / `text` ('#rrggbb') also color the bar and its title
    on Windows 11, so every window matches the theme exactly instead of just
    being black or white. Returns True when Windows accepted the dark mode.
    A no-op off Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        hwnd = ctypes.c_void_p(int(widget.winId()))
        val = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        ok = False
        # DWMWA_USE_IMMERSIVE_DARK_MODE is 20 on Windows 10 2004+ and 11,
        # and 19 on the older Windows 10 builds that had it.
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                ok = True
                break
        # Windows 11 (build 22000+) only; older builds return an error that is
        # ignored, and keep the plain dark / light bar from above.
        # DWMWA_BORDER_COLOR = 34, DWMWA_CAPTION_COLOR = 35, DWMWA_TEXT_COLOR = 36
        for attr, col in ((35, caption), (34, caption), (36, text)):
            ref = _colorref(col) if col else None
            if ref is not None:
                c_ref = ctypes.c_uint(ref)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c_ref), ctypes.sizeof(c_ref))
        if ok:
            # Repaint the frame so the change shows without a focus change.
            SWP = 0x0002 | 0x0001 | 0x0004 | 0x0020 | 0x0010   # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED|NOACTIVATE
            ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP)
        return ok
    except Exception:
        return False


class DarkTitleBarFilter(QObject):
    """Application-wide event filter: every normal top-level window (main
    window, dialogs, message boxes) gets the title bar matching the theme the
    moment it is shown. `dark_fn()` says whether the current theme is dark;
    `colors_fn()` (optional) returns (caption, text) hex colors for the bar.
    The bar is re-applied whenever a window is shown or activated, so dialogs
    that were created before a theme change cannot keep a stale bar."""
    _TYPES = (Qt.Window, Qt.Dialog, Qt.Tool)

    def __init__(self, dark_fn, parent=None, colors_fn=None):
        super().__init__(parent)
        self._dark_fn = dark_fn
        self._colors_fn = colors_fn

    def _apply(self, w):
        """Apply the current theme's bar to `w`, skipping work when this
        window already has exactly this look."""
        flags = w.windowFlags()
        if w.windowType() not in self._TYPES or (flags & Qt.FramelessWindowHint):
            return
        dark = bool(self._dark_fn())
        cap = txt = None
        if self._colors_fn is not None:
            try:
                cap, txt = self._colors_fn()
            except Exception:
                cap = txt = None
        key = f"{int(dark)}|{cap}|{txt}"   # a str: Qt properties turn tuples into lists
        if w.property("_vg_titlebar") == key and w.isVisible():
            return
        apply_dark_titlebar(w, dark, cap, txt)
        w.setProperty("_vg_titlebar", key)

    def eventFilter(self, obj, event):
        if (event.type() in (QEvent.Show, QEvent.WindowActivate)
                and isinstance(obj, QWidget) and obj.isWindow()):
            try:
                self._apply(obj)
            except Exception:
                pass
        return False

    def refresh(self):
        """Re-apply to every open window (the theme changed)."""
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            if w.isVisible():
                try:
                    self._apply(w)
                except Exception:
                    pass


class _Backlight(QWidget):
    """Sits over the screen. With the backlight off it tints the content
    green-grey, dims it and darkens the corners. Never takes mouse input."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.dim = False

    def paintEvent(self, _e):
        if not self.dim:
            return
        p = QPainter(self)
        r = self.rect()
        p.setCompositionMode(QPainter.CompositionMode_Multiply)
        p.fillRect(r, QColor(150, 188, 150))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        rg = QRadialGradient(QPointF(r.center()), max(r.width(), r.height()) * 0.75)
        rg.setColorAt(0.55, QColor(0, 0, 0, 0))
        rg.setColorAt(1, QColor(0, 0, 0, 120))
        p.fillRect(r, rg)
        p.end()


class ShellFrame(QWidget):
    dpad = Signal(str)      # "up" "down" "left" "right"
    button = Signal(str)    # "a" "b" "select" "start"

    def __init__(self, inner, color="cream", parent=None, backlight=True,
                 overrides=None, theme=None):
        super().__init__(parent)
        self.inner = inner
        self.color = color if color in SHELLS else "cream"
        self.overrides = dict(overrides) if isinstance(overrides, dict) else {}
        # Speaker dots follow the APP theme accent, not the shell.
        self._accent = QColor(resolve_palette(theme)["accent"])
        if not self._accent.isValid():
            self._accent = QColor("#8ccf72")
        self._hits = {}
        self.setMouseTracking(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(38, 62, COLUMN + 12, 40)
        lay.addWidget(inner)
        # Freely resizable: the case stretches with the window, the inner screen
        # takes all the extra room, and the controls column scales down on
        # short windows (see paintEvent) instead of forcing a big minimum.
        self.setMinimumSize(760, 520)
        self.backlight = bool(backlight)
        self.overlay = _Backlight(self)
        self.overlay.dim = not self.backlight
        self.overlay.setVisible(not self.backlight)
        inner.installEventFilter(self)

    def set_color(self, color):
        self.color = color if color in SHELLS else "cream"
        self.update()

    def set_overrides(self, overrides):
        """Per-part colors, e.g. {"dpad": "#3a0808"}. Empty values mean 'no override'."""
        self.overrides = dict(overrides) if isinstance(overrides, dict) else {}
        self.update()

    def _col(self, key):
        """Override first, then the shell's own value, then the built-in default."""
        for src in (self.overrides, SHELLS[self.color], _CONTROLS):
            v = src.get(key)
            if v:
                c = QColor(v)
                if c.isValid():
                    return c
        return QColor("#000000")

    def set_backlight(self, on):
        """Backlight off = dim green screen. The power light stays lit."""
        self.backlight = bool(on)
        self.overlay.dim = not self.backlight
        self.overlay.setVisible(not self.backlight)
        self._sync_overlay()
        self.overlay.update()
        self.update()

    def _sync_overlay(self):
        self.overlay.setGeometry(self.inner.geometry())
        self.overlay.raise_()

    def eventFilter(self, obj, e):
        if obj is self.inner and e.type() in (QEvent.Resize, QEvent.Move, QEvent.Show):
            self._sync_overlay()
        return super().eventFilter(obj, e)

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_overlay()

    # ---- painting ----
    def paintEvent(self, _e):
        top, body, bot, edge = (self._col(k) for k in ("top", "body", "bottom", "edge"))
        dark = _luminance(body) < DARK_LUM
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        body_r = QRectF(4, 14, w - 8, h - 22)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, top)
        g.setColorAt(0.35, body)
        g.setColorAt(1, bot)
        p.setPen(QPen(edge, 2))
        p.setBrush(g)
        p.drawRoundedRect(body_r, 26, 26)
        # rounded edge: light along the top lip, shade along the bottom
        rim = QLinearGradient(0, 0, 0, h)
        rim.setColorAt(0, QColor(255, 255, 255, 120 if not dark else 40))
        rim.setColorAt(0.45, QColor(255, 255, 255, 0))
        rim.setColorAt(0.8, QColor(0, 0, 0, 0))
        rim.setColorAt(1, QColor(0, 0, 0, 70))
        p.setPen(QPen(QBrush(rim), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(body_r.adjusted(3, 3, -3, -3), 23, 23)
        # hinge strip with two raised, rounded barrels
        hinge_c = top.darker(106) if not dark else top.lighter(115)

        def barrel(rect, rad):
            gb = QLinearGradient(0, rect.top(), 0, rect.bottom())
            gb.setColorAt(0, hinge_c.lighter(112))
            gb.setColorAt(0.45, hinge_c)
            gb.setColorAt(1, hinge_c.darker(118))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 60))
            p.drawRoundedRect(rect.translated(0, 2), rad, rad)
            p.setPen(QPen(edge, 1.5))
            p.setBrush(gb)
            p.drawRoundedRect(rect, rad, rad)
            p.setPen(QPen(QColor(255, 255, 255, 80 if not dark else 30), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(2, 2, -2, -2), rad - 2, rad - 2)

        barrel(QRectF(4, 14, w - 8, 26), 14)
        for x in (70, w - 220):
            barrel(QRectF(x, 2, 150, 24), 9)
        # screen bezel behind the inner widget: sunk into the body
        r = self.inner.geometry().adjusted(-12, -12, 12, 12)
        bez = QPainterPath()
        bez.addRoundedRect(QRectF(r), 16, 16)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 110 if not dark else 40), 1.5))
        p.drawPath(bez.translated(0, 1.5))
        p.setPen(Qt.NoPen)
        p.setBrush(self._col("bezel"))
        p.drawPath(bez)
        p.save()
        p.setClipPath(bez)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 140), 4))
        p.drawPath(bez.translated(0, 3))
        p.restore()
        # Controls are laid out for a ~640 px tall case; on shorter windows
        # draw them scaled down (0.7 floor) and scale the hit boxes to match.
        k = max(0.7, min(1.0, h / 640.0))
        if k < 1.0:
            p.save()
            p.scale(k, k)
            self._controls(p, w / k, h / k, dark)
            p.restore()
            self._hits = {n: QRectF(r.x() * k, r.y() * k, r.width() * k, r.height() * k)
                          for n, r in self._hits.items()}
        else:
            self._controls(p, w, h, dark)
        p.end()

    def _well(self, p, c, r, body, edge, dark):
        """Recessed circular socket: shadowed at the top lip, catch-light at the bottom."""
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        g = QLinearGradient(0, c.y() - r, 0, c.y() + r)
        g.setColorAt(0, body.darker(140 if not dark else 180))
        g.setColorAt(1, body.lighter(108 if not dark else 130))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(rect)
        p.setBrush(Qt.NoBrush)
        # inner shadow along the top edge
        p.setPen(QPen(QColor(0, 0, 0, 70), 3))
        p.drawArc(rect.adjusted(1.5, 1.5, -1.5, -1.5), 20 * 16, 140 * 16)
        # catch-light just outside the bottom edge
        p.setPen(QPen(QColor(255, 255, 255, 150 if not dark else 45), 1.5))
        p.drawArc(rect.adjusted(-1, -1, 1, 1), 200 * 16, 140 * 16)
        ec = QColor(edge)
        ec.setAlpha(120)
        p.setPen(QPen(ec, 1))
        p.drawEllipse(rect)

    @staticmethod
    def _xpath(cx, cy, w, h, ang):
        """Rounded capsule of w x h centred on (cx, cy), rotated by ang degrees."""
        path = QPainterPath()
        path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), h / 2, h / 2)
        t = QTransform()
        t.translate(cx, cy)
        t.rotate(ang)
        return t.map(path)

    def _well_path(self, p, path, body, edge, dark):
        """Recessed socket of any shape: catch-light under the bottom lip, shadow inside the top."""
        r = path.boundingRect()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 150 if not dark else 45), 1.5))
        p.drawPath(path.translated(0, 1.2))
        g = QLinearGradient(0, r.top(), 0, r.bottom())
        g.setColorAt(0, body.darker(140 if not dark else 180))
        g.setColorAt(1, body.lighter(108 if not dark else 130))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(path)
        p.save()
        p.setClipPath(path)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 70), 3))
        p.drawPath(path.translated(0, 2.5))
        p.restore()
        ec = QColor(edge)
        ec.setAlpha(120)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(ec, 1))
        p.drawPath(path)

    def _raised(self, p, path, top_c, bot_c, line, shadow=90, lit=60):
        """Raised key: drop shadow, vertical gradient, light top edge, dark bottom edge."""
        r = path.boundingRect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, shadow))
        p.drawPath(path.translated(0, 3))
        g = QLinearGradient(0, r.top(), 0, r.bottom())
        g.setColorAt(0, top_c)
        g.setColorAt(1, bot_c)
        p.setPen(QPen(line, 1))
        p.setBrush(g)
        p.drawPath(path)
        p.save()
        p.setClipPath(path)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, lit), 2))
        p.drawPath(path.translated(0, 2))
        p.setPen(QPen(QColor(0, 0, 0, 90), 2))
        p.drawPath(path.translated(0, -2))
        p.restore()

    def _controls(self, p, w, h, dark):
        cx = w - COLUMN / 2 - 4
        body, edge = self._col("body"), self._col("edge")
        ctl, ctl_hi = self._col("dpad"), self._col("dpad_hi")
        label = self._col("button_labels")
        led_c = self._col("power_led")
        self._hits = {}
        # power light
        led = QPointF(w - 44, 74)
        halo = QRadialGradient(led, 18)
        halo_c = QColor(led_c)
        halo_c.setAlpha(120)
        halo.setColorAt(0, halo_c)
        halo_c.setAlpha(0)
        halo.setColorAt(1, halo_c)
        p.setPen(Qt.NoPen)
        if self.backlight:
            p.setBrush(halo)
            p.drawEllipse(led, 18, 18)
        lg = QRadialGradient(QPointF(led.x() - 2, led.y() - 3), 10)
        lg.setColorAt(0, led_c.lighter(175))
        lg.setColorAt(0.5, led_c)
        lg.setColorAt(1, led_c.darker(170))
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        p.setBrush(lg)
        p.drawEllipse(led, 8, 8)
        f = p.font(); f.setPixelSize(10); f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4); p.setFont(f)
        p.setPen(label)
        p.drawText(QRectF(w - 130, 64, 70, 20), Qt.AlignRight | Qt.AlignVCenter, "POWER")
        self._hits["power"] = QRectF(w - 134, 58, 106, 32)   # click: backlight on/off
        # d-pad: recessed well, raised cross
        dy = max(190, h * 0.30)
        arm, ln, well_r = 40, 120, 74
        self._well(p, QPointF(cx, dy), well_r, body, edge, dark)
        cross = QPainterPath()
        cross.addRoundedRect(QRectF(cx - ln / 2, dy - arm / 2, ln, arm), 10, 10)
        cross.addRoundedRect(QRectF(cx - arm / 2, dy - ln / 2, arm, ln), 10, 10)
        cross.setFillRule(Qt.WindingFill)   # odd-even would punch a hole where the arms cross
        cross = cross.simplified()
        # shadow the cross casts into the well
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawPath(cross.translated(0, 3))
        # cross body
        gr = QLinearGradient(0, dy - ln / 2, 0, dy + ln / 2)
        gr.setColorAt(0, ctl_hi)
        gr.setColorAt(0.5, ctl)
        gr.setColorAt(1, ctl.darker(130))
        p.setPen(QPen(QColor("#000"), 1))
        p.setBrush(gr)
        p.drawPath(cross)
        # bevel: light along the top edges, dark along the bottom edges
        p.save()
        p.setClipPath(cross)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 60), 2))
        p.drawPath(cross.translated(0, 2))
        p.setPen(QPen(QColor(0, 0, 0, 110), 2))
        p.drawPath(cross.translated(0, -2))
        p.restore()
        # engraved arrows + hit areas
        p.setPen(Qt.NoPen)
        for name, (ox, oy) in {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.items():
            c = QPointF(cx + ox * 40, dy + oy * 40)
            sz = 6
            tip = QPointF(c.x() + ox * sz, c.y() + oy * sz)
            b1 = QPointF(c.x() - ox * sz * 0.6 - oy * sz, c.y() - oy * sz * 0.6 + ox * sz)
            b2 = QPointF(c.x() - ox * sz * 0.6 + oy * sz, c.y() - oy * sz * 0.6 - ox * sz)
            tri = QPolygonF([tip, b1, b2])
            p.setBrush(QColor(255, 255, 255, 45))
            p.drawPolygon(tri.translated(QPointF(0, 1)))
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawPolygon(tri)
            self._hits[name] = QRectF(c.x() - 20, c.y() - 20, 40, 40)
        # centre dimple
        rg = QRadialGradient(QPointF(cx, dy + 2), 11)
        rg.setColorAt(0, ctl.darker(160))
        rg.setColorAt(1, ctl.lighter(115))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(rg)
        p.drawEllipse(QPointF(cx, dy), 9, 9)
        # A / B: one diagonal recessed track, two raised round keys
        ay = max(340, h * 0.55)
        track = self._xpath(cx + 9, ay - 4, 160, 76, -26)
        self._well_path(p, track, body, edge, dark)
        for name, (x, y) in {"a": (cx + 46, ay - 22), "b": (cx - 28, ay + 14)}.items():
            key = QPainterPath()
            key.addEllipse(QPointF(x, y), 30, 30)
            self._raised(p, key, ctl_hi, ctl, QColor("#000"))
            f2 = p.font(); f2.setPixelSize(22); f2.setBold(True); f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0); p.setFont(f2)
            box = QRectF(x - 30, y - 30, 60, 60)
            letter = QColor(label)
            if abs(_luminance(letter) - _luminance(ctl_hi)) < 0.10:
                # labels tuned for the case would vanish on the key: lift or sink them
                letter = letter.lighter(260) if _luminance(ctl) < 0.3 else letter.darker(300)
            p.setPen(QColor(0, 0, 0, 170))
            p.drawText(box.translated(0, 1), Qt.AlignCenter, name.upper())
            p.setPen(letter)
            p.drawText(box, Qt.AlignCenter, name.upper())
            self._hits[name] = box
        # speaker dots
        sy = max(450, h * 0.72)
        p.setPen(Qt.NoPen)
        hole = body.darker(160) if not dark else QColor("#0b0c0c")
        dot = QColor(self._accent)
        dot.setAlpha(235)
        for i in range(5):
            for j in range(4):
                c = QPointF(cx + 10 + i * 14 + j * 6, sy + j * 12 - i * 5)
                p.setBrush(QColor(255, 255, 255, 110 if not dark else 35))
                p.drawEllipse(c + QPointF(0.6, 1.2), 3.6, 3.6)
                p.setBrush(hole)
                p.drawEllipse(c, 3.6, 3.6)
                p.setBrush(dot)
                p.drawEllipse(c, 2.6, 2.6)
        # select / start: raised pills in recessed slots
        by = h - 70
        f3 = p.font(); f3.setPixelSize(10); f3.setBold(False); f3.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4); p.setFont(f3)
        pill = self._col("select_start")
        if dark:
            pill_top, pill_bot, pill_line = pill.lighter(115), pill.darker(120), QColor("#000")
        else:
            pill_top, pill_bot, pill_line = pill.lighter(108), pill.darker(108), pill.darker(140)
        for name, x in (("select", cx - 38), ("start", cx + 38)):
            self._well_path(p, self._xpath(x, by, 62, 24, -22), body, edge, dark)
            self._raised(p, self._xpath(x, by, 48, 16, -22), pill_top, pill_bot, pill_line, shadow=80, lit=110)
            p.setPen(label)
            p.drawText(QRectF(x - 40, by + 22, 80, 16), Qt.AlignCenter, name.upper())
            self._hits[name] = QRectF(x - 28, by - 14, 56, 28)

    # ---- input ----
    def _hit(self, pos):
        for name, r in self._hits.items():
            if r.contains(pos):
                return name
        return None

    def mousePressEvent(self, e):
        name = self._hit(e.position())
        if name is None:
            return super().mousePressEvent(e)
        if name in ("up", "down", "left", "right"):
            self.dpad.emit(name)
        else:
            self.button.emit(name)

    def mouseMoveEvent(self, e):
        self.setCursor(QCursor(Qt.PointingHandCursor if self._hit(e.position()) else Qt.ArrowCursor))
        super().mouseMoveEvent(e)
