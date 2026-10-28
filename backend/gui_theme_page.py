"""Theme settings page: live preview, preset tiles, accent swatches and an
optional fine-tune section. Pure UI. SettingsDialog creates one ThemePage
and reads ThemePage.values() on Save. Nothing here touches the network or
saves settings by itself."""
import json
import re
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QRectF, QSize, QPoint, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QColorDialog, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLayout, QMessageBox, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget, QWidgetItem,
)

from gui_shell import SHELL_NAMES, SHELLS
from palette import (DERIVED, PRESET_NAMES, THEME_PRESETS, contrast, luminance, mix,
                     resolve_palette)
import settings as _settings

ACCENT_SWATCHES = ["#26c6da", "#7c4dff", "#66bb6a", "#ffa726", "#ef5350",
                   "#42a5f5", "#ec407a", "#8ccf72", "#9aa09c"]

SHELL_PART_GROUPS = [
    ("Case colors", [("top", "Top"), ("body", "Body"), ("bottom", "Bottom"), ("edge", "Edge")]),
    ("Control colors", [("dpad", "D-pad"), ("dpad_hi", "D-pad highlight"),
                        ("select_start", "Select + Start"), ("button_labels", "Labels"),
                        ("power_led", "Power LED"), ("bezel", "Screen bezel")]),
]
SHELL_PART_KEYS = [k for _, items in SHELL_PART_GROUPS for k, _ in items]

GROUPS = [
    ("Surfaces", [("bg_base", "Window background"), ("bg_panel", "Panels and table"),
                  ("bg_elevated", "Menus and table header")]),
    ("Text", [("text", "Main text"), ("text_muted", "Secondary text"),
              ("text_dim", "Faint text")]),
    ("Lines", [("border", "Borders"), ("border_hi", "Borders on hover")]),
    ("Accent", [("accent", "Accent color"), ("accent_dim", "Accent when pressed")]),
    ("Status", [("success", "Done"), ("warning", "Warning"),
                ("error", "Error"), ("info", "Info")]),
]


def _family(font_stack):
    for part in font_stack.split(","):
        part = part.strip().strip('"')
        if part and not part.startswith("-"):
            return part
    return "Segoe UI"


def _px(v, d=0.0):
    """'14px' -> 14.0"""
    try:
        return float(str(v).lower().replace("px", "").strip())
    except (TypeError, ValueError):
        return d


def _num(v, d=1.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _soft_color(v):
    """'rgba(255,255,255,0.1)' -> a QColor with alpha, or None. Palette
    tokens are hex, but a preset may carry a CSS rgba() string for a faint
    border (Batch 3: Glass's border_soft); QColor does not parse that form,
    so the preview converts it by hand. The alpha may be 0..1 or 0..255."""
    try:
        m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*"
                     r"(?:,\s*([0-9.]+)\s*)?\)", str(v or "").strip())
        if not m:
            return None
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        alpha = int(round(a * 255)) if a <= 1 else int(a)
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      max(0, min(255, alpha)))
    except Exception:
        return None


def _pick_color(initial, parent, title):
    """Color dialog with Qt's own dialog forced, so its "Pick Screen Color"
    eye dropper (sample any pixel on screen) is always there; a native
    dialog would not have it. Returns an invalid QColor when cancelled."""
    dlg = QColorDialog(QColor(initial), parent)
    dlg.setWindowTitle(title)
    dlg.setOption(QColorDialog.DontUseNativeDialog, True)
    if dlg.exec() == QDialog.Accepted:
        return dlg.currentColor()
    return QColor()


class FlowLayout(QLayout):
    """Wraps its items onto new rows when the width runs out."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do(self, rect, test):
        x, y, row_h = rect.x(), rect.y(), 0
        for it in self._items:
            sz = it.sizeHint()
            if x + sz.width() > rect.right() + 1 and row_h > 0:
                x = rect.x()
                y += row_h + self._sp
                row_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), sz))
            x += sz.width() + self._sp
            row_h = max(row_h, sz.height())
        return y + row_h - rect.y()


class MiniPreview(QWidget):
    """A small painted copy of the main window, drawn from the tokens. It
    scales with its width, so it stays readable at any dialog size."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.t = resolve_palette({})
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumWidth(320)

    def set_tokens(self, t):
        self.t = t
        self.update()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return int(w * 0.43)

    def sizeHint(self):
        return QSize(560, 314)

    def paintEvent(self, _e):
        t = self.t
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = int(w * 0.43)
        s = w / 560.0
        fam = _family(t["font"])
        # Batch 2: the preview follows the preset's shape tokens. This card is
        # about half the size of the app window, so lengths are halved.
        K = 0.5
        rad = _px(t.get("radius"), 10) * K
        rad_sm = _px(t.get("radius_sm"), 6) * K
        ps = _num(t.get("pad_scale"), 1.0)
        bw = _num(t.get("border_w"), 1.0)
        fmul = _num(t.get("font_scale"), 1.0)
        bold_all = str(t.get("font_weight") or "normal").lower() in ("bold", "600", "700")
        pf = 0.6 + 0.4 * ps                       # row pitch follows the density
        # Batch 3: border_w is drawn as-is — 0 really means no border (the
        # old max(1.0, ...) floor hid the Glass preset's shape). A preset
        # with border_soft (Glass) instead gets a faint 1px line on its
        # panel / table / input boxes, drawn by box(..., soft_border=True).
        bpen = bw * max(1.0, s * 0.7)             # border thickness
        soft = _soft_color(t.get("border_soft"))

        def font(px, bold=False, mono=False):
            f = QFont(_family(t["font_mono"]) if mono else fam)
            f.setPixelSize(max(7, int(px * s * fmul)))
            f.setBold(bold or (bold_all and not mono))
            return f

        def text(x, y, wd, ht, s_, color, fnt, flags=Qt.AlignVCenter | Qt.AlignLeft):
            p.setFont(fnt)
            p.setPen(QColor(color))
            p.drawText(QRectF(x * s, y * s, wd * s, ht * s), int(flags), s_)

        def box(x, y, wd, ht, fill, border=None, r=6, soft_border=False):
            r = max(0.0, min(r, wd / 2.0, ht / 2.0))
            if soft_border and soft is not None:
                p.setPen(QPen(soft, max(1.0, s)))   # the faint 1px line
            elif border and bpen > 0:
                p.setPen(QPen(QColor(border), bpen))
            else:
                p.setPen(Qt.NoPen)
            p.setBrush(QColor(fill) if fill else Qt.NoBrush)
            p.drawRoundedRect(QRectF(x * s, y * s, wd * s, ht * s), r * s, r * s)

        box(0.5, 0.5, 559, h / s - 1, t["bg_base"], t["border"], rad * 1.5)
        # sidebar
        box(1, 1, 120, h / s - 2, t["bg_side"], None, rad * 1.5)
        text(12, 10, 100, 16, "\u2193 VIDEO GRABBER", t["text"], font(8, True))
        for i, name in enumerate(["All", "Active", "Finished", "Failed", "Video"]):
            y = 36 + i * 24 * pf
            if i == 0:
                box(8, y, 104, 20, t["bg_sel"], None, rad_sm)
            text(18, y, 90, 20, name, t["text"] if i == 0 else t["text_soft"], font(9, i == 0))
        # toolbar
        box(122, 1, 437, 36, t["bg_panel"], None, 0)
        box(132, 8, 70, 22, t["accent"], None, rad)
        text(132, 8, 70, 22, "+ Add URL", t["text_on_accent"], font(9, True), Qt.AlignCenter)
        for i, name in enumerate(["Start", "Pause", "Stop"]):
            text(212 + i * 48, 8, 46, 22, name, t["text_soft"], font(9), Qt.AlignCenter)
        box(440, 8, 108, 22, t["bg_input"], t["border"], rad, soft_border=True)
        text(448, 8, 90, 22, "Search\u2026", t["text_dim"], font(8))
        # table
        top = 46
        box(130, top, 420, 150, t["bg_panel"], t["border"], rad, soft_border=True)
        box(131, top + 1, 418, 22, t["bg_elevated"], None, rad_sm)
        for x, label in ((140, "Name"), (300, "Size"), (350, "Progress"), (480, "Status")):
            text(x, top + 1, 70, 22, label, t["text_muted"], font(8, True))
        rows = [("Studio session part 4.mp4", "212 MB", 62, "Loading", t["text_soft"]),
                ("Home _ X (2).mp4", "122 KB", 100, "Done", t["accent"]),
                ("watch (2) (1).mp4", "48 MB", 30, "Paused", t["warning"]),
                ("clip-0412.mp4", "0 B", 0, "Error", t["error"])]
        for i, (n, sz, pct, st, col) in enumerate(rows):
            y = top + 26 + i * 30 * pf
            if i == 1:
                box(132, y - 2, 416, 28, t["bg_sel"], None, rad_sm)
            text(140, y, 150, 24, n if len(n) < 24 else n[:22] + "\u2026", t["text"], font(9))
            text(300, y, 50, 24, sz, t["text_muted"], font(9))
            box(350, y + 8, 80, 8, t["bg_elevated"], None, min(rad_sm, 4))
            if pct:
                box(350, y + 8, 80 * pct / 100.0, 8, t["accent"], None, min(rad_sm, 4))
            text(440, y, 36, 24, "%d%%" % pct, t["text_soft"], font(8))
            text(480, y, 60, 24, st, col, font(9, True))
        # log
        box(130, 204, 420, 22, t["bg_input"], t["border"], rad, soft_border=True)
        text(138, 204, 400, 22, "18:35:10  Job 564 started: Studio session part 4.mp4",
             t["text_muted"], font(8, mono=True))
        p.end()


class PresetTile(QAbstractButton):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, e):
        # Swallow it: the first click already selected the tile, and letting
        # the base class turn the second press into another click would
        # toggle the preview straight back off.
        if e.button() == Qt.LeftButton:
            self.doubleClicked.emit()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def __init__(self, key, ring, label_color, parent=None, tokens=None, title=None):
        super().__init__(parent)
        self.key = key
        self.ring = ring
        self.label_color = label_color
        # tokens/title are for tiles built from a theme file in the themes
        # folder; the built-in presets leave both unset.
        self._tokens = tokens
        self._title = title or PRESET_NAMES.get(key, key)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(132, 86)
        self.setToolTip(self._title)
        self.setAccessibleName(self._title)

    def paintEvent(self, _e):
        t = self._tokens or THEME_PRESETS[self.key]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1.5, 1.5, self.width() - 3, 58)
        p.setPen(QPen(QColor(self.ring if self.isChecked() else t["border_hi"]),
                      2.5 if self.isChecked() else 1))
        p.setBrush(QColor(t["bg_base"]))
        _rr = max(0.0, _px(t.get("radius"), 10) * 0.7)
        p.drawRoundedRect(r, _rr, _rr)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t["bg_panel"]))
        p.drawRoundedRect(QRectF(10, 10, 34, 40), 4, 4)
        for i, c in enumerate((t["bg_elevated"], t["bg_elevated"], t["bg_elevated"])):
            p.setBrush(QColor(c))
            p.drawRoundedRect(QRectF(52, 12 + i * 13, 70, 9), 3, 3)
        p.setBrush(QColor(t["accent"]))
        p.drawRoundedRect(QRectF(52, 12, 42, 9), 3, 3)
        p.drawRoundedRect(QRectF(14, 16, 26, 7), 3, 3)
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setBold(self.isChecked())
        p.setFont(f)
        p.setPen(QColor(self.label_color))
        p.drawText(QRectF(4, 62, self.width() - 8, 22), Qt.AlignCenter,
                   p.fontMetrics().elidedText(self._title, Qt.ElideRight, self.width() - 8))
        p.end()


_NUM_TOKENS = {"pad_scale": (0.6, 1.5), "border_w": (0, 6), "font_scale": (0.7, 1.3)}


_FILE_CACHE = {}      # path -> ((mtime_ns, size), (raw, entry) or None)


def _load_theme_file(p):
    """(raw json, normalized entry) for a theme file, or None when it is
    missing, too big, unreadable or not a theme. Parsed once per file version:
    opening Settings or the Theme Engine used to re-read and re-resolve every
    theme file (up to 200) on the UI thread each time."""
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _FILE_CACHE.pop(str(p), None)
        return None
    hit = _FILE_CACHE.get(str(p))
    if hit is not None and hit[0] == key:
        return hit[1]
    res = None
    if st.st_size <= 256 * 1024:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            entry = _theme_from_dict(raw)
            if entry is not None:
                res = (raw, entry)
        except (OSError, ValueError):
            res = None
    _FILE_CACHE[str(p)] = (key, res)
    return res


def _theme_from_dict(d):
    """Normalize a theme file's JSON into a dict the page can preview and
    apply, or None when it holds nothing usable. Accepts the Export format
    (theme_preset / accent / theme_tokens / shell_*) and a flat file that is
    just {"bg_base": "#...", ...}. Unknown keys and invalid colors are
    dropped, so a bad file can never put junk into theme_tokens."""
    if not isinstance(d, dict):
        return None
    base = THEME_PRESETS["amoled_black"]
    allowed = set(base) | set(DERIVED)
    non_color = {"radius", "radius_pill", "font", "font_mono", "radius_sm", "radius_chip",
                 "border_soft"}

    def clean(toks):
        out = {}
        for k, v in (toks or {}).items():
            if k in _NUM_TOKENS:      # layout tokens that are plain numbers (v5.5, Batch 2)
                lo, hi = _NUM_TOKENS[k]
                if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                    out[k] = float(v)
                continue
            if k == "font_weight":
                if isinstance(v, str) and v.strip().lower() in ("normal", "500", "600", "bold", "700"):
                    out[k] = v.strip().lower()
                continue
            if k == "accent" or k not in allowed or not isinstance(v, str) or not v.strip():
                continue
            if k in non_color or QColor(v.strip()).isValid():
                out[k] = v.strip()
        return out

    wrapped = "theme_preset" in d or "theme_tokens" in d
    toks = clean(d.get("theme_tokens") if isinstance(d.get("theme_tokens"), dict) else {}) \
        if wrapped else clean(d)
    preset = d.get("theme_preset") if d.get("theme_preset") in PRESET_NAMES else "amoled_black"
    acc = d.get("accent")
    accent = acc.strip() if isinstance(acc, str) and QColor(acc.strip()).isValid() else None
    if not toks and not accent and not (wrapped and d.get("theme_preset") in PRESET_NAMES):
        return None
    accent = accent or THEME_PRESETS[preset]["accent"]
    out = {"preset": preset, "accent": accent, "tokens": toks,
           "pal": resolve_palette({"theme_preset": preset, "accent": accent,
                                   "theme_tokens": dict(toks)})}
    for k in ("shell_enabled", "shell_backlight", "frame_cream"):
        if k in d:
            out[k] = bool(d[k])
    if d.get("shell_color") in SHELLS:
        out["shell_color"] = d["shell_color"]
    if isinstance(d.get("shell_colors"), dict):
        out["shell_colors"] = ThemePage._clean_shell_colors(d["shell_colors"])
    return out


class Swatch(QAbstractButton):
    def __init__(self, color, ring, parent=None):
        super().__init__(parent)
        self.color = color
        self.ring = ring
        self.setCheckable(True)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(color)
        self.setAccessibleName("Accent " + color)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.isChecked():
            p.setPen(QPen(QColor(self.ring), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(1.5, 1.5, 27, 27))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(QRectF(6, 6, 18, 18))
        p.end()


def _card(title, hint=None):
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 16)
    lay.setSpacing(10)
    t = QLabel(title)
    t.setObjectName("CardTitle")
    lay.addWidget(t)
    if hint:
        h = QLabel(hint)
        h.setObjectName("CardHint")
        h.setWordWrap(True)
        lay.addWidget(h)
    return f, lay


GALLERY_PAGE_SIZE = 12     # 3 columns x 4 rows
GALLERY_COLS = 3


class ThemePage(QWidget):
    changed = Signal()
    applied = Signal()      # a tile was double-clicked: apply it and close the dialog

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        s = settings or {}
        self._preset = s.get("theme_preset") if s.get("theme_preset") in PRESET_NAMES else "amoled_black"
        self._accent = s.get("accent") or THEME_PRESETS[self._preset]["accent"]
        self._ov = {k: v for k, v in (s.get("theme_tokens") or {}).items()
                    if v and k != "accent"}
        self._shell_on = bool(s.get("shell_enabled"))
        self._shell_color = s.get("shell_color") if s.get("shell_color") in SHELLS else "cream"
        self._backlight = bool(s.get("shell_backlight", True))
        self._frame_cream = bool(s.get("frame_cream"))
        self._shell_ov = self._clean_shell_colors(s.get("shell_colors"))
        self._shell_rows = {}
        chrome = resolve_palette(s)
        self._ring, self._label = chrome["accent"], chrome["text"]
        self._rows = {}
        # v5.5: Simple (presets, accent, shell toggle) or Detailed (adds the
        # shell colors, window frame and the full fine-tune grid).
        self._detail = (s.get("theme_detail_mode") == "detailed")

        root = QGridLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        self._grid = root

        # preview
        self.preview_card, pl = _card("Preview", "This is how the main window will look. "
                                      "Changes apply when you press Save.")
        self.preview = MiniPreview()
        pl.addWidget(self.preview)
        self.warn = QLabel("")
        self.warn.setObjectName("Warn")
        self.warn.setWordWrap(True)
        self.warn.setVisible(False)
        pl.addWidget(self.warn)

        # controls
        self.controls = QWidget()
        self.controls.setObjectName("Plain")
        cl = QVBoxLayout(self.controls)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(14)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self.mode_simple = QPushButton("Simple")
        self.mode_detailed = QPushButton("Detailed")
        for _b in (self.mode_simple, self.mode_detailed):
            _b.setCheckable(True)
            _b.setCursor(Qt.PointingHandCursor)
            self._mode_group.addButton(_b)
            mode_row.addWidget(_b)
        self.mode_simple.setChecked(not self._detail)
        self.mode_detailed.setChecked(self._detail)
        self.mode_detailed.toggled.connect(self._set_detail)
        mode_row.addStretch(1)
        cl.addLayout(mode_row)

        c1, l1 = _card("Preset", "Start from a look, then adjust the accent or individual "
                                 "colors below. Your tweaks stay when you switch presets.")
        flow_host = QWidget()
        flow_host.setObjectName("Plain")
        self._flow = FlowLayout(flow_host, 10)
        self._tiles = {}
        for key in PRESET_NAMES:
            tile = PresetTile(key, self._ring, self._label)
            tile.setChecked(key == self._preset)
            tile.clicked.connect(lambda _=False, k=key: self._pick_preset(k))
            tile.doubleClicked.connect(lambda k=key: self._preset_double_clicked(k))
            self._flow.addWidget(tile)
            self._tiles[key] = tile
        l1.addWidget(flow_host)
        cl.addWidget(c1)

        # Themes folder: every .json in <home>/themes shows up as a tile.
        # Clicking a tile previews it in the Preview card only; Apply loads
        # it into this page, and Save (as always) is what writes settings.
        cf, lf = _card("Theme folder", "...")
        self._folder_hint = cf.findChildren(QLabel, "CardHint")[0]
        ff_host = QWidget()
        ff_host.setObjectName("Plain")
        self._ff = QGridLayout(ff_host)
        self._ff.setContentsMargins(0, 0, 0, 0)
        self._ff.setSpacing(10)
        lf.addWidget(ff_host)
        # Pagination: 12 themes per page, flipped with < and >.
        nav = QHBoxLayout()
        self.pg_prev = QPushButton("<")
        self.pg_prev.setFixedWidth(40)
        self.pg_prev.setAccessibleName("Previous page")
        self.pg_prev.clicked.connect(lambda: self._page_step(-1))
        self.pg_next = QPushButton(">")
        self.pg_next.setFixedWidth(40)
        self.pg_next.setAccessibleName("Next page")
        self.pg_next.clicked.connect(lambda: self._page_step(1))
        self.pg_label = QLabel("")
        self.pg_label.setObjectName("CardHint")
        self.pg_label.setAlignment(Qt.AlignCenter)
        nav.addStretch(1)
        nav.addWidget(self.pg_prev)
        nav.addWidget(self.pg_label)
        nav.addWidget(self.pg_next)
        nav.addStretch(1)
        lf.addLayout(nav)
        self._page = 0
        self._file_entries = []
        self._file_tiles = []
        self._file_sel = None
        self._file_empty = QLabel("No themes yet. Export one, or drop .json files into the folder.")
        self._file_empty.setObjectName("CardHint")
        self._file_empty.setWordWrap(True)
        lf.addWidget(self._file_empty)
        frow = QHBoxLayout()
        self.file_apply = QPushButton("Apply")
        self.file_apply.setEnabled(False)
        self.file_apply.clicked.connect(self._apply_file_theme)
        frow.addWidget(self.file_apply)
        for text, fn in (("Refresh", self._scan_themes), ("Open folder", self._open_themes_dir),
                         ("Import folder\u2026", self._import_folder)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            frow.addWidget(b)
        frow.addStretch(1)
        lf.addLayout(frow)
        self.folder_card, self.folder_layout = cf, lf   # SettingsDialog docks the Theme Engine button here
        cl.addWidget(cf)

        c2, l2 = _card("Accent color", "Used for buttons, progress bars and highlights.")
        sw_host = QWidget()
        sw_host.setObjectName("Plain")
        sw_flow = FlowLayout(sw_host, 6)
        self._swatches = []
        for hexc in ACCENT_SWATCHES:
            b = Swatch(hexc, self._label)
            b.clicked.connect(lambda _=False, c=hexc: self._set_accent(c))
            sw_flow.addWidget(b)
            self._swatches.append(b)
        self.custom_btn = QPushButton("Custom\u2026")
        self.custom_btn.clicked.connect(self._pick_accent)
        sw_flow.addWidget(self.custom_btn)
        l2.addWidget(sw_host)
        self.accent_label = QLabel("")
        self.accent_label.setObjectName("CardHint")
        l2.addWidget(self.accent_label)
        cl.addWidget(c2)

        c4, l4 = _card("Handheld shell", "Wraps the window in a console body. The D-pad, A, B, "
                       "Start and Select buttons work. Best with a window of about 900 by 640.")
        self.shell_check = QCheckBox("Show the handheld shell (uncheck for a plain, resizable window)")
        self.shell_check.setChecked(self._shell_on)
        self.shell_check.toggled.connect(self._shell_toggled)
        l4.addWidget(self.shell_check)
        self.backlight_check = QCheckBox("Backlight on (turn off for the dim green screen)")
        self.backlight_check.setChecked(self._backlight)
        self.backlight_check.toggled.connect(self._backlight_toggled)
        l4.addWidget(self.backlight_check)
        sh_host = QWidget()
        sh_host.setObjectName("Plain")
        sh_flow = FlowLayout(sh_host, 6)
        self._shell_sw = []
        for key, nm in SHELL_NAMES.items():
            b = Swatch(SHELLS[key]["body"], self._label)
            b.setToolTip(nm)
            b.setAccessibleName(nm + " shell")
            b.clicked.connect(lambda _=False, k=key: self._pick_shell(k))
            sh_flow.addWidget(b)
            self._shell_sw.append((key, b))
        # Shell body colors and per-part colors: Detailed view only.
        self.shell_detail = QWidget()
        self.shell_detail.setObjectName("Plain")
        sdl = QVBoxLayout(self.shell_detail)
        sdl.setContentsMargins(0, 0, 0, 0)
        sdl.setSpacing(l4.spacing())
        sdl.addWidget(sh_host)
        for gname, items in SHELL_PART_GROUPS:
            gl = QLabel(gname.upper())
            gl.setObjectName("SectionLabel")
            sdl.addSpacing(6)
            sdl.addWidget(gl)
            for key, label in items:
                sdl.addLayout(self._make_shell_row(key, label))
        l4.addWidget(self.shell_detail)
        cl.addWidget(c4)

        c5, l5 = _card("Window frame", "Paints the window frame (toolbar, menus, status bar and "
                       "margins) around your theme's dark panels, like the handheld mockup. "
                       "Cream by default; pick any color below. Works with or without the shell.")
        self.frame_check = QCheckBox("Cream frame around the panels")
        self.frame_check.setChecked(self._frame_cream)
        self.frame_check.toggled.connect(self._frame_toggled)
        l5.addWidget(self.frame_check)
        l5.addLayout(self._make_row("frame", "Frame color"))
        self.frame_card = c5
        cl.addWidget(c5)

        c3, l3 = _card("Fine-tune colors", None)
        head = QHBoxLayout()
        self.fine_toggle = QToolButton()
        self.fine_toggle.setCheckable(True)
        self.fine_toggle.setCursor(Qt.PointingHandCursor)
        self.fine_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.fine_toggle.toggled.connect(self._toggle_fine)
        head.addWidget(self.fine_toggle)
        head.addStretch(1)
        self.count_chip = QLabel("")
        self.count_chip.setObjectName("Chip")
        head.addWidget(self.count_chip)
        self.reset_preset_btn = QPushButton("Reset to Preset")
        self.reset_preset_btn.setToolTip("Drop every color change and restore the preset's own colors "
                                         "and accent")
        self.reset_preset_btn.clicked.connect(self._reset_to_preset)
        head.addWidget(self.reset_preset_btn)
        self.clear_btn = QPushButton("Clear tweaks")
        self.clear_btn.clicked.connect(self._clear_tweaks)
        head.addWidget(self.clear_btn)
        l3.addLayout(head)
        self.fine_body = QWidget()
        self.fine_body.setObjectName("Plain")
        fb = QVBoxLayout(self.fine_body)
        fb.setContentsMargins(0, 4, 0, 0)
        fb.setSpacing(4)
        for gname, items in GROUPS:
            gl = QLabel(gname.upper())
            gl.setObjectName("SectionLabel")
            fb.addSpacing(6)
            fb.addWidget(gl)
            for key, label in items:
                fb.addLayout(self._make_row(key, label))
        l3.addWidget(self.fine_body)
        self.fine_card = c3
        cl.addWidget(c3)

        row = QHBoxLayout()
        for text, fn in (("Export theme\u2026", self._export), ("Import theme\u2026", self._import),
                         ("Reset to defaults", self._reset_all)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        cl.addLayout(row)
        cl.addStretch(1)

        self.changed.connect(self._clear_file_selection)
        self._scan_themes()
        self._wide = None
        self._relayout(self.width() or 600)
        self.fine_toggle.setChecked(bool(self._ov) or self._detail)
        self._toggle_fine(self.fine_toggle.isChecked())
        self._apply_detail()
        self._refresh()

    # ---- layout: preview beside the controls when wide, above when narrow ----
    def _relayout(self, width):
        wide = width >= 900
        if wide == self._wide:
            return
        self._wide = wide
        for w in (self.preview_card, self.controls):
            self._grid.removeWidget(w)
        if wide:
            self._grid.addWidget(self.controls, 0, 0)
            self._grid.addWidget(self.preview_card, 0, 1, Qt.AlignTop)
            self._grid.setColumnStretch(0, 5)
            self._grid.setColumnStretch(1, 6)
        else:
            self._grid.addWidget(self.preview_card, 0, 0)
            self._grid.addWidget(self.controls, 1, 0)
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout(e.size().width())

    # ---- fine-tune rows ----
    def _make_row(self, key, label):
        lay = QHBoxLayout()
        lay.setSpacing(8)
        name = QLabel(label)
        name.setMinimumWidth(150)
        chip = QPushButton()
        chip.setMinimumWidth(110)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setAccessibleName(label)
        chip.clicked.connect(lambda _=False, k=key: self._pick_token(k))
        reset = QToolButton()
        reset.setText("Reset")
        reset.setToolTip("Back to the preset's color")
        reset.clicked.connect(lambda _=False, k=key: self._reset_token(k))
        lay.addWidget(name)
        lay.addWidget(chip)
        lay.addWidget(reset)
        lay.addStretch(1)
        self._rows[key] = (chip, reset, name)
        return lay

    @staticmethod
    def _clean_shell_colors(d):
        """Keep only known parts with a valid color; empty means no override."""
        if not isinstance(d, dict):
            return {}
        return {k: v for k, v in d.items()
                if k in SHELL_PART_KEYS and isinstance(v, str) and v and QColor(v).isValid()}

    def _shell_part(self, key):
        return self._shell_ov.get(key) or SHELLS[self._shell_color].get(key, "#000000")

    def _make_shell_row(self, key, label):
        lay = QHBoxLayout()
        lay.setSpacing(8)
        name = QLabel(label)
        name.setMinimumWidth(150)
        chip = QPushButton()
        chip.setMinimumWidth(110)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setAccessibleName("Shell " + label)
        chip.clicked.connect(lambda _=False, k=key: self._pick_shell_part(k))
        reset = QToolButton()
        reset.setText("Reset")
        reset.setToolTip("Back to the shell's own color")
        reset.clicked.connect(lambda _=False, k=key: self._reset_shell_part(k))
        lay.addWidget(name)
        lay.addWidget(chip)
        lay.addWidget(reset)
        lay.addStretch(1)
        self._shell_rows[key] = (chip, reset, name, label)
        return lay

    def _pick_shell_part(self, key):
        c = _pick_color(self._shell_part(key), self, "Choose color")
        if c.isValid():
            self._shell_ov[key] = c.name()
            self._refresh()

    def _reset_shell_part(self, key):
        self._shell_ov.pop(key, None)
        self._refresh()

    def _tokens(self):
        return resolve_palette({"theme_preset": self._preset, "accent": self._accent,
                                "theme_tokens": dict(self._ov)})

    def _refresh(self):
        if not hasattr(self, "clear_btn"):
            return
        t = self._tokens()
        self.preview.set_tokens(t)
        for key, (chip, reset, name) in self._rows.items():
            col = t[key]
            fg = "#000000" if luminance(col) > 0.4 else "#ffffff"
            chip.setText(col.upper())
            chip.setStyleSheet(f"QPushButton {{ background: {col}; color: {fg}; "
                               f"border: 1px solid {t['border_hi']}; padding: 5px 10px; }}")
            edited = key in self._ov or (key == "accent" and
                                        self._accent.lower() != THEME_PRESETS[self._preset]["accent"].lower())
            reset.setVisible(edited)
            name.setText(dict(i for _, g in GROUPS for i in g).get(key, "Frame color")
                         + ("  \u2022" if edited else ""))
        _fr = self._rows.get("frame")
        if _fr:
            _fr[0].setEnabled(self._frame_cream)
        n = len(self._ov)
        self.count_chip.setText(f"{n} customized" if n else "No tweaks")
        self.clear_btn.setVisible(n > 0)
        self.reset_preset_btn.setEnabled(n > 0 or self._accent.lower()
                                         != THEME_PRESETS[self._preset]["accent"].lower())
        self.fine_toggle.setText(("\u25BE " if self.fine_toggle.isChecked() else "\u25B8 ")
                                 + "Edit individual colors")
        self.accent_label.setText(f"Current accent: {self._accent.upper()}")
        self.backlight_check.setEnabled(self._shell_on)
        for key, (chip, reset, name, label) in self._shell_rows.items():
            col = self._shell_part(key)
            fg = "#000000" if luminance(col) > 0.4 else "#ffffff"
            chip.setText(col.upper())
            chip.setStyleSheet(f"QPushButton {{ background: {col}; color: {fg}; "
                               f"border: 1px solid {t['border_hi']}; padding: 5px 10px; }}")
            edited = key in self._shell_ov
            chip.setEnabled(self._shell_on)
            reset.setEnabled(self._shell_on)
            reset.setVisible(edited)
            name.setText(label + ("  \u2022" if edited else ""))
        for key, b in self._shell_sw:
            b.setChecked(self._shell_on and key == self._shell_color)
            b.setEnabled(self._shell_on)
        for b in self._swatches:
            b.setChecked(b.color.lower() == self._accent.lower())
        warns = []
        if contrast(t["text"], t["bg_panel"]) < 4.5:
            warns.append("Main text is hard to read on panels.")
        if contrast(t["text_muted"], t["bg_panel"]) < 2.5:
            warns.append("Secondary text is very faint.")
        if contrast(t["accent"], t["bg_base"]) < 2:
            warns.append("The accent barely stands out from the background.")
        self.warn.setText("\u26A0 " + " ".join(warns))
        self.warn.setVisible(bool(warns))
        self.changed.emit()

    # ---- Simple / Detailed (v5.5) ----
    def _apply_detail(self):
        for w in (self.shell_detail, self.frame_card, self.fine_card):
            w.setVisible(self._detail)

    def _set_detail(self, on):
        self._detail = bool(on)
        self._apply_detail()
        if self._detail:
            self.fine_toggle.setChecked(True)     # Detailed reveals the full grid

    # ---- actions ----
    def _toggle_fine(self, on):
        self.fine_body.setVisible(on)
        self._refresh() if hasattr(self, "count_chip") else None

    def _pick_preset(self, key):
        self._preset = key
        self._accent = THEME_PRESETS[key]["accent"]
        for k, tile in self._tiles.items():
            tile.setChecked(k == key)
            tile.update()
        if key == "handheld" and not self.shell_check.isChecked():
            self.shell_check.setChecked(True)     # Handheld brings its shell (still switchable)
        self._refresh()

    def _shell_toggled(self, on):
        self._shell_on = on
        self._refresh()

    def _preset_double_clicked(self, key):
        self._pick_preset(key)
        self.applied.emit()

    def _frame_toggled(self, on):
        self._frame_cream = on
        self._refresh()

    def _backlight_toggled(self, on):
        self._backlight = on
        self._refresh()

    def _pick_shell(self, key):
        self._shell_color = key
        self._shell_on = True
        self.shell_check.setChecked(True)
        self._refresh()

    def _set_accent(self, hexc):
        self._accent = hexc
        self._refresh()

    def _pick_accent(self):
        c = _pick_color(self._accent, self, "Custom accent color")
        if c.isValid():
            self._set_accent(c.name())

    def _pick_token(self, key):
        cur = QColor(self._tokens()[key])
        c = _pick_color(cur, self, "Choose color")
        if not c.isValid():
            return
        if key == "accent":
            self._accent = c.name()
        else:
            self._ov[key] = c.name()
        self._refresh()

    def _reset_token(self, key):
        if key == "accent":
            self._accent = THEME_PRESETS[self._preset]["accent"]
        else:
            self._ov.pop(key, None)
        self._refresh()

    def _reset_to_preset(self):
        """Clear every override (and a changed accent): back to the base preset."""
        self._ov = {}
        self._accent = THEME_PRESETS[self._preset]["accent"]
        self._refresh()

    def _clear_tweaks(self):
        self._ov = {}
        self._refresh()

    def _reset_all(self):
        self._ov = {}
        self._shell_on = False
        self.shell_check.setChecked(False)
        self._backlight = True
        self.backlight_check.setChecked(True)
        self._shell_ov = {}
        self._frame_cream = False
        self.frame_check.setChecked(False)
        self._pick_preset("amoled_black")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export theme", str(self._themes_dir() / "theme.json"), "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self.values(for_save=False), indent=2, sort_keys=True),
                                  encoding="utf-8")
            self._scan_themes()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import theme", "", "JSON (*.json)")
        if not path:
            return
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not a theme file")
            preset = d.get("theme_preset")
            self._preset = preset if preset in PRESET_NAMES else self._preset
            self._accent = str(d.get("accent") or THEME_PRESETS[self._preset]["accent"])
            toks = d.get("theme_tokens")
            self._ov = {k: v for k, v in toks.items() if v and k != "accent"} \
                if isinstance(toks, dict) else {}
            for k, tile in self._tiles.items():
                tile.setChecked(k == self._preset)
            self.fine_toggle.setChecked(bool(self._ov))
            self._shell_on = bool(d.get("shell_enabled", self._shell_on))
            sc = d.get("shell_color")
            self._shell_color = sc if sc in SHELLS else self._shell_color
            self.shell_check.setChecked(self._shell_on)
            self._backlight = bool(d.get("shell_backlight", self._backlight))
            self.backlight_check.setChecked(self._backlight)
            self._frame_cream = bool(d.get("frame_cream", self._frame_cream))
            self.frame_check.setChecked(self._frame_cream)
            if isinstance(d.get("shell_colors"), dict):
                self._shell_ov = self._clean_shell_colors(d["shell_colors"])
            self._refresh()
        except Exception as e:
            QMessageBox.warning(self, "Import failed", str(e))

    # ---- themes folder (browse, preview, apply, bulk import) ----
    @staticmethod
    def _themes_dir():
        return Path(_settings.HOME) / "themes"

    def _scan_themes(self):
        """(Re)read the .json files in the themes folder, then show the page
        of tiles that holds the current selection (or the current page)."""
        d = self._themes_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        sel_path = self._file_sel["path"] if self._file_sel else None
        self._file_entries, self._file_sel = [], None
        try:
            files = sorted((p for p in d.glob("*.json") if p.is_file()),
                           key=lambda p: p.name.lower())[:200]
        except OSError:
            files = []
        for p in files:
            loaded = _load_theme_file(p)
            if loaded is None:
                continue
            raw, e = loaded
            e = dict(e)          # the cache keeps the original; this copy gets name/path
            nm = raw.get("name") if isinstance(raw.get("name"), str) and raw["name"].strip() else p.stem
            e["name"], e["path"] = nm.strip(), str(p)
            self._file_entries.append(e)
            if e["path"] == sel_path:
                self._file_sel = e
                self._page = (len(self._file_entries) - 1) // GALLERY_PAGE_SIZE
        self._file_empty.setVisible(not self._file_entries)
        self.file_apply.setEnabled(self._file_sel is not None)
        self._folder_hint.setText(f"{d}  ({len(self._file_entries)} theme"
                                  f"{'' if len(self._file_entries) == 1 else 's'}). "
                                  "Click a tile to preview it, then Apply. Double-click to apply and close.")
        self._render_page()

    def _page_count(self):
        return max(1, -(-len(self._file_entries) // GALLERY_PAGE_SIZE))

    def _page_step(self, delta):
        self._page = max(0, min(self._page_count() - 1, self._page + delta))
        self._render_page()

    def _render_page(self):
        """Build only the tiles of the current page (12 max, 3 x 4)."""
        self._page = max(0, min(self._page_count() - 1, self._page))
        while self._ff.count():
            it = self._ff.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._file_tiles = []
        start = self._page * GALLERY_PAGE_SIZE
        for pos, idx in enumerate(range(start, min(start + GALLERY_PAGE_SIZE,
                                                    len(self._file_entries)))):
            e = self._file_entries[idx]
            tile = PresetTile("amoled_black", self._ring, self._label,
                              tokens=e["pal"], title=e["name"])
            tile._gidx = idx
            tile.clicked.connect(lambda _=False, i=idx: self._select_file_tile(i))
            tile.doubleClicked.connect(lambda i=idx: self._file_double_clicked(i))
            self._ff.addWidget(tile, pos // GALLERY_COLS, pos % GALLERY_COLS)
            tile.setChecked(e is self._file_sel)
            self._file_tiles.append(tile)
        pages = self._page_count()
        self.pg_label.setText(f"Page {self._page + 1} of {pages}")
        self.pg_prev.setEnabled(self._page > 0)
        self.pg_next.setEnabled(self._page < pages - 1)
        self.pg_prev.setVisible(pages > 1)
        self.pg_next.setVisible(pages > 1)
        self.pg_label.setVisible(pages > 1)
        self._ff.invalidate()

    def _file_double_clicked(self, idx):
        self._file_sel = self._file_entries[idx]
        self._apply_file_theme()
        self.applied.emit()

    def _select_file_tile(self, idx):
        e = self._file_entries[idx]
        if self._file_sel is e:                      # click again = cancel the preview
            self._clear_file_selection()
            self._refresh()
            return
        self._file_sel = e
        for tile in self._file_tiles:
            tile.setChecked(getattr(tile, "_gidx", -1) == idx)
            tile.update()
        self.preview.set_tokens(e["pal"])            # preview only: nothing is stored
        self.file_apply.setEnabled(True)
        self._folder_hint.setText(f"Previewing {e['name']}. Press Apply to use it "
                                  "(Save in Settings writes it), or click the tile again to cancel.")

    def _clear_file_selection(self):
        if self._file_sel is None and not any(t.isChecked() for t in self._file_tiles):
            return
        self._file_sel = None
        for tile in self._file_tiles:
            tile.setChecked(False)
            tile.update()
        self.file_apply.setEnabled(False)
        self._folder_hint.setText(f"{self._themes_dir()}  ({len(self._file_entries)} theme"
                                  f"{'' if len(self._file_entries) == 1 else 's'}). "
                                  "Click a tile to preview it, then Apply.")

    def _apply_file_theme(self):
        e = self._file_sel
        if not e:
            return
        self._preset = e["preset"]
        self._accent = e["accent"]
        self._ov = dict(e["tokens"])
        for k, tile in self._tiles.items():
            tile.setChecked(k == self._preset)
            tile.update()
        self.fine_toggle.setChecked(bool(self._ov))
        if "shell_enabled" in e:
            self._shell_on = e["shell_enabled"]
            self.shell_check.setChecked(self._shell_on)
        if "shell_color" in e:
            self._shell_color = e["shell_color"]
        if "shell_backlight" in e:
            self._backlight = e["shell_backlight"]
            self.backlight_check.setChecked(self._backlight)
        if "frame_cream" in e:
            self._frame_cream = e["frame_cream"]
            self.frame_check.setChecked(self._frame_cream)
        if "shell_colors" in e:
            self._shell_ov = dict(e["shell_colors"])
        self._refresh()

    def _open_themes_dir(self):
        d = self._themes_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _import_folder(self):
        src = QFileDialog.getExistingDirectory(self, "Import themes from folder", "")
        if not src:
            return
        dest = self._themes_dir()
        added = skipped = 0
        try:
            dest.mkdir(parents=True, exist_ok=True)
            same = Path(src).resolve() == dest.resolve()
            files = sorted(p for p in Path(src).glob("*.json") if p.is_file())[:500]
        except OSError as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return
        if not same:
            for p in files:
                try:
                    if p.stat().st_size > 256 * 1024:
                        skipped += 1
                        continue
                    if _theme_from_dict(json.loads(p.read_text(encoding="utf-8"))) is None:
                        skipped += 1
                        continue
                    target, n = dest / p.name, 2
                    while target.exists():            # never overwrite an existing theme
                        target = dest / f"{p.stem} ({n}){p.suffix}"
                        n += 1
                    shutil.copy2(p, target)
                    added += 1
                except (OSError, ValueError):
                    skipped += 1
        self._scan_themes()
        if same:
            QMessageBox.information(self, "Import folder",
                                    "That is already the themes folder. Tiles refreshed.")
        else:
            QMessageBox.information(self, "Import folder",
                                    f"Added {added} theme(s) to the themes folder"
                                    + (f"; skipped {skipped} file(s) that were not valid themes."
                                       if skipped else "."))

    def values(self, for_save=True):
        # v5.5: the server merges dict settings key by key, so a color that was
        # reset or cleared has to be sent as "" or the old override stayed in
        # settings after Save. Exports keep only the real overrides.
        if for_save:
            _keys = {k for k in self._rows if k != "accent"} | set(self._ov)
            _tokens = {k: self._ov.get(k, "") for k in sorted(_keys)}
        else:
            _tokens = dict(self._ov)
        out = {"theme_preset": self._preset, "accent": self._accent,
                "theme_tokens": _tokens, "shell_enabled": self._shell_on,
                "shell_color": self._shell_color, "shell_backlight": self._backlight, "frame_cream": self._frame_cream,
                # every key is sent ("" = no override) so the server's key-by-key
                # dict merge can never leave a reset color behind
                "shell_colors": {k: self._shell_ov.get(k, "") for k in SHELL_PART_KEYS}}
        if for_save:
            out["theme_detail_mode"] = "detailed" if self._detail else "simple"
        return out
