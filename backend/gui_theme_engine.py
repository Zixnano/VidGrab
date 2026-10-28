"""Theme Engine: build a theme from an image or by picking colors, with a
live preview in the running app.

The dialog never writes settings. It emits themeChanged(state), where state
is the full settings dict (saved settings plus the theme being edited), and
the host applies it with gui_style.build_qss. The only files it writes are
HOME/themes/<name>.json, in the Export format that
gui_theme_page._theme_from_dict reads back, so the Theme tab shows them.
Saving emits themeSaved(path) so the host can refresh its theme list at once.

Pillow is optional. With it, colors come from Pillow's quantizer; without it
the same step runs on Qt's own image reader and a built-in median cut, so the
drop zone always works.

The image step: drop an image on the zone at the top (or click it to browse).
The dialog shows a thumbnail and the extracted swatches, and waits. The theme
only changes when you press Fill chips (smart: if the image has no strong
accent color, a complementary one is generated) or Scramble (shuffles the
colors across the chips). Undo steps back one Fill or Scramble.
"""
import json
import random
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from gui_fit import install_fit
from gui_theme_page import ThemePage, _load_theme_file
from palette import PRESET_NAMES, THEME_PRESETS, contrast, luminance, resolve_palette

try:
    from PIL import Image
except ImportError:          # Pillow missing: the rest of the dialog still works
    Image = None

# The seven chips. "accent" is saved as the top-level "accent", never inside
# theme_tokens, because the Theme tab drops it there.
CHIPS = (
    ("bg_base", "Background"),
    ("bg_panel", "Panel"),
    ("bg_elevated", "Elevated"),
    ("text", "Text"),
    ("text_muted", "Text muted"),
    ("accent", "Accent"),
    ("border", "Border"),
)
CHIP_KEYS = tuple(k for k, _ in CHIPS)
LOW_CONTRAST_CHIPS = ("text_muted",)   # kept for older callers; see AA_RULES
# WCAG AA, per chip: (ratio needed, surfaces it is drawn on). Text sits on the
# background and on panels; the accent is a UI color, so AA's 3:1 non-text rule.
AA_RULES = {
    "text": (4.5, ("bg_base", "bg_panel")),
    "text_muted": (4.5, ("bg_base", "bg_panel")),
    "accent": (3.0, ("bg_base",)),
}
MIN_TEXT_CONTRAST = 4.5                # WCAG AA for normal text
LIVE_DEBOUNCE_MS = 120      # each live update restyles the whole app; fewer is smoother
DEFAULT_NAME = "My Theme"
MAX_NAME_LEN = 60
SMART_MIN_SAT = 0.35                   # best accent candidate below this is "not a real accent"
SMART_TINT_SAT = 0.12                  # below this a color has no usable hue (grayscale)
ACCENT_MIN_CONTRAST = 3.0              # a generated accent must stand out from the background
MUTED_MIN_CONTRAST = MIN_TEXT_CONTRAST  # muted text is text too: WCAG AA 4.5:1
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}
MAX_THEME_BYTES = 256 * 1024           # the Theme tab skips bigger files
EXTRACT_SIZE = (160, 160)              # thumbnail size; plenty for color stats
EXTRACT_COLORS = 5


def _image_exts():
    """Every picture extension we can read: Qt's image plugins, Pillow's
    registered formats when Pillow is there, and the common ones."""
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".ico"}
    exts |= {"." + bytes(f).decode("ascii", "ignore").lower()
             for f in QImageReader.supportedImageFormats()}
    if Image is not None:
        try:
            exts |= {e.lower() for e in Image.registered_extensions()}
        except Exception:
            pass
    return tuple(sorted(exts))


IMAGE_EXTS = _image_exts()
# The Browse dialog only lists real photo formats. The full IMAGE_EXTS list
# (PDF, PSD, EPS, DDS, HDF, WMF ...) made the Windows file dialog run the
# shell thumbnail handlers for those types, in this process, while you scroll
# the folder. "All files" is still there for anything else, and drag and drop
# accepts every type in IMAGE_EXTS.
PICKER_EXTS = tuple(e for e in (".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp",
                                ".gif", ".tif", ".tiff", ".ico") if e in IMAGE_EXTS)
THUMB_MAX = 480                        # longest side the preview ever decodes


def _extract_colors_qt(path, count):
    """Pillow-free extraction: Qt reads and shrinks the image, then a median cut."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        size.scale(*EXTRACT_SIZE, Qt.KeepAspectRatio)
        reader.setScaledSize(size)
    img = reader.read()
    if img.isNull():
        raise OSError(reader.errorString() or "unreadable image")
    img = img.convertToFormat(QImage.Format_RGB32)
    px = [(c.red(), c.green(), c.blue())
          for c in (QColor(img.pixel(x, y)) for y in range(img.height())
                    for x in range(img.width()))]
    def spread(box):
        return [max(p[i] for p in box) - min(p[i] for p in box) for i in range(3)]

    boxes = [px]
    while len(boxes) < count:
        # split the box with the widest color range (a flat box cannot be split)
        cand = [b for b in boxes if len(b) > 1 and max(spread(b)) > 0]
        if not cand:
            break
        box = max(cand, key=lambda b: max(spread(b)) * len(b) ** 0.5)
        rng = spread(box)
        ch = rng.index(max(rng))
        box.sort(key=lambda p: p[ch])
        mid = len(box) // 2
        i = next(k for k, b in enumerate(boxes) if b is box)
        boxes[i:i + 1] = [box[:mid], box[mid:]]
    out = []
    for box in sorted(boxes, key=len, reverse=True):
        n = len(box)
        hx = "#%02x%02x%02x" % tuple(sum(p[i] for p in box) // n for i in range(3))
        if hx not in out:
            out.append(hx)
    return out[:count]


def extract_colors(path, count=EXTRACT_COLORS):
    """Up to `count` dominant colors of an image as #rrggbb, most frequent
    first. Raises OSError or ValueError when the file is not a readable image."""
    if Image is None:
        return _extract_colors_qt(path, count)
    with Image.open(path) as im:
        # Shrink BEFORE converting: a 100 MP photo used to be decoded to a full
        # RGB copy (hundreds of MB, seconds on the GUI thread) just to read 5 colors.
        im.draft("RGB", EXTRACT_SIZE)       # JPEG: decode at reduced scale
        im.thumbnail(EXTRACT_SIZE)
        im = im.convert("RGB")
        q = im.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
    flat = q.getpalette() or []
    out = []
    for _n, idx in sorted(q.getcolors() or [], reverse=True):
        r, g, b = flat[idx * 3: idx * 3 + 3]
        hx = "#%02x%02x%02x" % (r, g, b)
        if hx not in out:
            out.append(hx)
    return out[:count]


def _safe_stem(name):
    """A file-name stem from a theme name: letters, digits, space, - and _ only,
    spaces become underscores, no Windows reserved names. Never empty."""
    stem = re.sub(r"[^\w\- ]+", " ", name or "")
    stem = re.sub(r"\s+", "_", stem.strip()).strip("._-")[:MAX_NAME_LEN]
    if not stem:
        stem = DEFAULT_NAME.replace(" ", "_")
    if stem.lower() in _RESERVED:
        stem += "_theme"
    return stem


def _sat(c):
    return QColor(c).hsvSaturationF()


def _mix(a, b, t):
    """Blend color a toward b by t (0 = a, 1 = b), as #rrggbb."""
    ca, cb = QColor(a), QColor(b)
    return "#%02x%02x%02x" % tuple(
        round(x * (1 - t) + y * t)
        for x, y in ((ca.red(), cb.red()), (ca.green(), cb.green()), (ca.blue(), cb.blue())))


def _readable_text(text, bg):
    """`text` if it reads on `bg`; else near-white or near-black, whichever
    contrasts more; else pure white or black (a mid-gray background cannot
    reach MIN_TEXT_CONTRAST with the softer pair)."""
    if contrast(text, bg) >= MIN_TEXT_CONTRAST:
        return text
    for pair in (("#f2f2f2", "#111111"), ("#ffffff", "#000000")):
        best = max(pair, key=lambda c: contrast(c, bg))
        if contrast(best, bg) >= MIN_TEXT_CONTRAST:
            return best
    return best


def _readable_muted(muted, text, bg):
    """`muted` if it holds MUTED_MIN_CONTRAST on `bg`; else the text color
    blended toward the background as far as that contrast allows."""
    if contrast(muted, bg) >= MUTED_MIN_CONTRAST:
        return muted
    for t in (0.35, 0.25, 0.15, 0.0):
        c = _mix(text, bg, t)
        if contrast(c, bg) >= MUTED_MIN_CONTRAST:
            return c
    return text


def _min_contrast(color, surfaces):
    return min(contrast(color, b) for b in surfaces)


def _readable_on(text, surfaces):
    """Like _readable_text, but the color must read on every surface."""
    if _min_contrast(text, surfaces) >= MIN_TEXT_CONTRAST:
        return text
    best = text
    for pair in (("#f2f2f2", "#111111"), ("#ffffff", "#000000")):
        best = max(pair, key=lambda c: _min_contrast(c, surfaces))
        if _min_contrast(best, surfaces) >= MIN_TEXT_CONTRAST:
            return best
    return best


def _muted_on(muted, text, surfaces):
    """`muted` if it holds AA on every surface; else the text color blended
    toward the background as far as AA allows."""
    if _min_contrast(muted, surfaces) >= MUTED_MIN_CONTRAST:
        return muted
    for t in (0.35, 0.25, 0.15, 0.0):
        c = _mix(text, surfaces[0], t)
        if _min_contrast(c, surfaces) >= MUTED_MIN_CONTRAST:
            return c
    return text


def _accent_on(accent, bg):
    """`accent` if it stands out from `bg` by ACCENT_MIN_CONTRAST; else the
    same hue, stepped lighter (dark bg) or darker (light bg) until it does."""
    if contrast(accent, bg) >= ACCENT_MIN_CONTRAST:
        return accent
    q = QColor(accent)
    hue, sat, light = max(q.hslHueF(), 0.0), q.hslSaturationF(), q.lightnessF()
    dark_bg = QColor(bg).lightnessF() < 0.5
    out = accent
    for _ in range(30):
        light = min(0.95, light + 0.03) if dark_bg else max(0.05, light - 0.03)
        out = QColor.fromHslF(hue, sat, light).name()
        if contrast(out, bg) >= ACCENT_MIN_CONTRAST:
            break
    return out


def _aa_repair(pal):
    """The chip changes that bring `pal` up to WCAG AA: {key: new color}.
    Empty when it already passes. Backgrounds and borders are never touched:
    the user's surfaces stay, the colors drawn on them move."""
    surfaces = (pal["bg_base"], pal["bg_panel"])
    out = {}
    text = _readable_on(pal["text"], surfaces)
    if text != pal["text"]:
        out["text"] = text
    muted = _muted_on(pal["text_muted"], text, surfaces)
    if muted != pal["text_muted"]:
        out["text_muted"] = muted
    accent = _accent_on(pal["accent"], pal["bg_base"])
    if accent != pal["accent"]:
        out["accent"] = accent
    return out


def generate_accent(colors, bg):
    """Smart accent: a vivid color that suits `bg`, built in HSL.
    If the image has any tint, take the hue of its most saturated color and
    turn it 180 degrees (the complement). A fully grayscale image has no hue,
    so use blue on a dark background and red on a light one. Saturation is
    high; lightness starts mid-high on dark backgrounds and mid-low on light
    ones, then steps until the accent contrasts with `bg` by at least
    ACCENT_MIN_CONTRAST."""
    dark_bg = QColor(bg).lightnessF() < 0.5
    tinted = [c for c in colors if _sat(c) >= SMART_TINT_SAT and QColor(c).hslHueF() >= 0]
    if tinted:
        src = max(tinted, key=_sat)
        hue = (QColor(src).hslHueF() + 0.5) % 1.0
    else:
        hue = (215 if dark_bg else 5) / 360.0
    light = 0.60 if dark_bg else 0.42
    out = QColor.fromHslF(hue, 0.85, light).name()
    for _ in range(24):
        out = QColor.fromHslF(hue, 0.85, light).name()
        if contrast(out, bg) >= ACCENT_MIN_CONTRAST:
            break
        light = min(0.92, light + 0.03) if dark_bg else max(0.08, light - 0.03)
    return out


def suggest_chips(colors, smart=True):
    """Map extracted colors onto the chips. The darkest color is the background
    and the lightest is the text. Of the colors in between, the most saturated
    becomes the accent, and the rest fill panel, elevated and border in order.
    Smart mode: if no in-between color is saturated enough to be an accent
    (SMART_MIN_SAT), or there is none at all, a complementary accent is
    generated instead (see generate_accent) and every in-between color goes to
    panel, elevated and border. If the text fails MIN_TEXT_CONTRAST against the
    background, it is replaced by #f2f2f2 or #111111 (or pure white or black on
    a mid-gray background), whichever contrasts more.
    Chips with no color to take are left out of the result, so they keep their
    current value."""
    if len(colors) < 2:
        return {}
    ordered = sorted(colors, key=luminance)
    bg, text = ordered[0], ordered[-1]
    middle = ordered[1:-1]
    out = {"bg_base": bg}
    accent = None
    if middle:
        # Smart: a candidate must also stand out from the background, so a dark
        # saturated brown on a near-black background is not taken as an accent.
        pool = [c for c in middle if contrast(c, bg) >= ACCENT_MIN_CONTRAST] if smart else middle
        if pool:
            best = max(pool, key=_sat)
            if not smart or _sat(best) >= SMART_MIN_SAT:
                accent = best
                middle.remove(best)
    if accent is None and smart:
        accent = generate_accent(colors, bg)
    if accent is not None:
        out["accent"] = accent
    for key in ("bg_panel", "bg_elevated", "border"):
        if middle:
            out[key] = middle.pop(0)
    out["text"] = _readable_text(text, bg)
    return out


def _rule():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


class ImageDropZone(QFrame):
    """A bordered zone that takes a dropped image file or a click (browse).
    After an image is set it shows a thumbnail, scaled to fit. It only reports
    the choice (imageChosen / clicked); the dialog decides what to do with it."""
    imageChosen = Signal(str)       # a valid image file was dropped
    clicked = Signal()              # click or Enter/Space: ask for a file

    PROMPT = "Drop an image here, or click to browse."
    _QSS = ("QFrame#ImageDropZone { border: 2px dashed palette(mid); border-radius: 10px; }"
            "QFrame#ImageDropZone:hover, QFrame#ImageDropZone[dragOver=\"true\"] "
            "{ border: 2px dashed palette(highlight); }"
            "QFrame#ImageDropZone:disabled { border: 2px dashed palette(midlight); }")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageDropZone")
        self.setStyleSheet(self._QSS)
        self.setAcceptDrops(True)
        self.setMinimumHeight(130)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Image drop zone")
        self._pix = None
        lay = QVBoxLayout(self)
        self.label = QLabel(self.PROMPT)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.label)

    # ---- state ----
    def disable_with(self, text, tip):
        """Pillow is missing: the zone stays visible but inert, and says why."""
        self.setEnabled(False)
        self.setCursor(Qt.ArrowCursor)
        self.label.setText(text)
        self.setToolTip(tip)

    def set_image(self, path):
        """Show a thumbnail of `path`. Returns False when Qt cannot decode it."""
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > THUMB_MAX:
            size.scale(THUMB_MAX, THUMB_MAX, Qt.KeepAspectRatio)
            reader.setScaledSize(size)
        img = reader.read()
        if img.isNull():
            return False
        self._pix = QPixmap.fromImage(img)
        self.label.setVisible(False)
        self.setToolTip(f"{path}\nDrop another image to replace it.")
        self.update()
        return True

    @staticmethod
    def _dropped_image(md):
        for u in md.urls():
            if not u.isLocalFile():
                continue
            f = u.toLocalFile()
            if f.lower().endswith(IMAGE_EXTS) or QImageReader(f).canRead():
                return f
        return None

    def _drag_over(self, on):
        self.setProperty("dragOver", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    # ---- events ----
    def dragEnterEvent(self, e):
        if self._dropped_image(e.mimeData()):
            e.acceptProposedAction()
            self._drag_over(True)
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._drag_over(False)

    def dropEvent(self, e):
        self._drag_over(False)
        path = self._dropped_image(e.mimeData())
        if path:
            e.acceptProposedAction()
            self.imageChosen.emit(path)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)       # the dashed border
        if self._pix is None:
            return
        box = self.rect().adjusted(10, 10, -10, -10)
        # Fill the box and crop the overflow (no letterbox bars).
        pm = self._pix.scaled(box.size(), Qt.KeepAspectRatioByExpanding,
                              Qt.SmoothTransformation)
        p = QPainter(self)
        p.setClipRect(box)
        p.drawPixmap(box.x() + (box.width() - pm.width()) // 2,
                     box.y() + (box.height() - pm.height()) // 2, pm)
        p.end()


class ColorChip(QWidget):
    """One editable token: a color button, its name, its hex and an optional
    low-contrast note under it."""

    def __init__(self, label, on_click, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.swatch = QPushButton()
        self.swatch.setFixedSize(56, 28)
        self.swatch.setCursor(Qt.PointingHandCursor)
        self.swatch.setToolTip(f"Click to change {label.lower()}")
        self.swatch.setAccessibleName(f"{label} color")
        self.swatch.clicked.connect(on_click)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        name = QLabel(label)
        self.hex = QLabel("")
        self.hex.setObjectName("CardHint")
        self.warn = QLabel("low contrast")
        self.warn.setObjectName("Warn")
        self.warn.setVisible(False)
        text_col.addWidget(name)
        text_col.addWidget(self.hex)
        text_col.addWidget(self.warn)
        row.addWidget(self.swatch)
        row.addLayout(text_col)
        row.addStretch(1)

    def set_color(self, hex_value, edge):
        self.swatch.setStyleSheet(f"background: {hex_value}; border: 1px solid {edge}; "
                                  "border-radius: 6px;")
        self.hex.setText(hex_value)


class ThemeEngineDialog(QDialog):
    # Emitted on every change (debounced). Payload: the full settings state.
    themeChanged = Signal(dict)
    # Emitted after a theme file is written. Payload: its path.
    themeSaved = Signal(str)
    # Emitted when the theme being edited should become the saved theme
    # (Save, or Apply on the close prompt). Payload: the full settings state.
    themeApplied = Signal(dict)

    def __init__(self, base_settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Theme Engine")
        install_fit(self, 540, 640, 380, 320)
        base = dict(base_settings or {})          # copy: load_settings() is read-only
        self._base = base
        preset = base.get("theme_preset")
        self._preset = preset if preset in PRESET_NAMES else "amoled_black"
        self._accent = base.get("accent") or THEME_PRESETS[self._preset]["accent"]
        self._tokens = {k: v for k, v in (base.get("theme_tokens") or {}).items()
                        if v and k != "accent"}
        self._extracted = []
        self._picker = None
        self._undo = None           # (accent, tokens) before the last Fill or Scramble
        self._dirty = False         # edits made since the theme was last applied

        # A color drag fires many changes; the app restyles once per pause.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LIVE_DEBOUNCE_MS)
        self._timer.timeout.connect(self._emit_live)

        root = QVBoxLayout(self)

        hint = QLabel(f"Starting from {PRESET_NAMES[self._preset]}. Changes show in the app as "
                      "you make them. Save writes a theme file and applies it. If you close "
                      "with changes that are not applied, you are asked first.")
        hint.setObjectName("CardHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # Image step: a drop zone, then Fill chips. Dropping only previews;
        # the theme changes when Fill chips is pressed.
        self.drop = ImageDropZone()
        self.drop.imageChosen.connect(self._load_image_path)
        self.drop.clicked.connect(self._load_image)
        root.addWidget(self.drop)
        img_row = QHBoxLayout()
        self.fill_btn = QPushButton("Fill chips")
        self.fill_btn.setEnabled(False)
        self.fill_btn.setToolTip("Use the image's colors. If the image has no strong accent "
                                 "color, a complementary one is generated.")
        self.fill_btn.clicked.connect(self._fill_from_image)
        self.scramble_btn = QPushButton("Scramble")
        self.scramble_btn.setToolTip("Shuffle the image colors (or the current colors, when no "
                                     "image is loaded) across the seven chips.")
        self.scramble_btn.clicked.connect(self._scramble)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setEnabled(False)
        self.undo_btn.setToolTip("Undo the last Fill chips, Scramble or Fix contrast.")
        self.undo_btn.clicked.connect(self._undo_last)
        self.fix_btn = QPushButton("Fix contrast")
        self.fix_btn.setToolTip("Adjust text, muted text and accent so they meet WCAG AA "
                                "(4.5:1 for text, 3:1 for the accent). Backgrounds are kept.")
        self.fix_btn.clicked.connect(self._fix_contrast)
        for b in (self.fill_btn, self.scramble_btn, self.fix_btn, self.undo_btn):
            img_row.addWidget(b)
        img_row.addStretch(1)
        root.addLayout(img_row)
        self._swatch_row = QHBoxLayout()
        root.addLayout(self._swatch_row)

        # The seven chips, two per row.
        grid = QGridLayout()
        self._chips = {}
        for i, (key, label) in enumerate(CHIPS):
            chip = ColorChip(label, lambda _=False, k=key: self._pick(k))
            self._chips[key] = chip
            grid.addWidget(chip, i // 2, i % 2)
        root.addLayout(grid)

        root.addWidget(_rule())

        # Load: pick a saved theme and edit it.
        load_row = QHBoxLayout()
        load_row.addWidget(QLabel("Saved themes"))
        self.load_combo = QComboBox()
        self.load_combo.setMinimumWidth(200)
        self.load_theme_btn = QPushButton("Load theme")
        self.load_theme_btn.clicked.connect(self._load_selected)
        load_row.addWidget(self.load_combo, 1)
        load_row.addWidget(self.load_theme_btn)
        root.addLayout(load_row)

        # Save: name it, then write HOME/themes/<name>.json.
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("Theme name"))
        self.name_edit = QLineEdit(DEFAULT_NAME)
        self.name_edit.setMaxLength(MAX_NAME_LEN)
        self.name_edit.setPlaceholderText(DEFAULT_NAME)
        self.name_edit.setClearButtonEnabled(True)
        self.name_edit.returnPressed.connect(self._save)
        self.save_btn = QPushButton("Save Theme")
        self.save_btn.clicked.connect(self._save)
        self.save_btn.setToolTip("Write the theme file and apply it to the app.")
        save_row.addWidget(self.name_edit, 1)
        save_row.addWidget(self.save_btn)
        root.addLayout(save_row)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("CardHint")
        self.status.setWordWrap(True)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        # Enter in the name box saves; it must not also press some other button.
        for btn in self.findChildren(QPushButton):
            btn.setAutoDefault(False)

        self._scan_files()
        self._refresh_chips()

    # ---- state and live apply ----

    def _state(self):
        """Full settings state for the app. Derived tokens are not stored here:
        resolve_palette computes them from the seven chips, so they stay in step."""
        st = dict(self._base)
        st["theme_preset"] = self._preset
        st["accent"] = self._accent
        st["theme_tokens"] = dict(self._tokens)
        return st

    def _pal(self):
        return resolve_palette(self._state())

    def _schedule(self):
        self._dirty = True
        self._timer.start()

    def _apply_now(self):
        """Make the theme being edited the saved theme (the host writes it)."""
        self._timer.stop()
        self.themeApplied.emit(self._state())
        self._dirty = False

    def reject(self):
        # Close button, Esc and the window X all end here.
        if self._dirty:
            box = QMessageBox(QMessageBox.Question, "Apply before closing?",
                              "This theme has changes that are not applied. "
                              "Apply them before closing?", QMessageBox.NoButton, self)
            apply_b = box.addButton("Apply", QMessageBox.AcceptRole)
            box.addButton("Discard", QMessageBox.DestructiveRole)
            cancel_b = box.addButton("Cancel", QMessageBox.RejectRole)
            box.setDefaultButton(apply_b)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked is cancel_b:
                return
            if clicked is apply_b:
                self._apply_now()
            else:
                self._dirty = False       # Discard: the host puts the saved theme back
        super().reject()

    def _emit_live(self):
        self.themeChanged.emit(self._state())

    def done(self, r):
        # Drop a pending live update, so it cannot restyle the app after the
        # host has put back the saved theme.
        self._timer.stop()
        super().done(r)

    # ---- chips ----

    def _refresh_chips(self):
        pal = self._pal()
        for key, chip in self._chips.items():
            chip.set_color(pal[key], pal["border_hi"])
            rule = AA_RULES.get(key)
            if rule:
                need, surfaces = rule
                ratio = _min_contrast(pal[key], [pal[b] for b in surfaces])
                chip.warn.setText(f"contrast {ratio:.1f}:1, AA needs {need:g}:1")
                chip.warn.setVisible(ratio < need)
        self.fix_btn.setEnabled(bool(_aa_repair(pal)))

    def _aa_fix(self):
        """Apply _aa_repair to the chips being edited. Returns the labels of
        the chips that changed (empty when nothing needed fixing)."""
        fixes = _aa_repair(self._pal())
        for key, value in fixes.items():
            if key == "accent":
                self._accent = value
            else:
                self._tokens[key] = value
        names = dict(CHIPS)
        return [names[k] for k in fixes]

    def _fix_contrast(self):
        before = (self._accent, dict(self._tokens))
        fixed = self._aa_fix()
        if not fixed:
            self.status.setText("Contrast already meets WCAG AA.")
            return
        self._undo = before
        self.undo_btn.setEnabled(True)
        self._refresh_chips()
        self._schedule()
        self.status.setText("Adjusted " + ", ".join(fixed).lower() + " to meet WCAG AA.")

    def _set_chip(self, key, hex_value):
        if not QColor(hex_value).isValid():
            return
        if key == "accent":
            self._accent = hex_value
        else:
            self._tokens[key] = hex_value
        self._refresh_chips()
        self._schedule()

    def _pick(self, key):
        """Open a color picker for one chip. Its color changes live; Cancel
        puts back the color the chip had when the picker opened."""
        original = self._pal()[key]
        dlg = QColorDialog(QColor(original), self)
        dlg.setWindowTitle(dict(CHIPS)[key])
        dlg.setOption(QColorDialog.DontUseNativeDialog, True)   # keeps the eye dropper
        dlg.currentColorChanged.connect(lambda c, k=key: self._set_chip(k, c.name()))
        dlg.rejected.connect(lambda k=key, o=original: self._set_chip(k, o))
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        if self._picker is not None:
            try:
                self._picker.close()    # one picker at a time
            except RuntimeError:
                pass
        self._picker = dlg          # keep a reference while the picker is open
        dlg.destroyed.connect(lambda *_: setattr(self, "_picker", None))
        dlg.open()

    # ---- image ----

    def _load_image(self):
        """Browse (the drop zone was clicked)."""
        path, _ = QFileDialog.getOpenFileName(self, "Pick an image", "",
                                              "Images (" + " ".join("*" + e for e in PICKER_EXTS)
                                              + ");;All files (*)")
        if path:
            self._load_image_path(path)

    def _load_image_path(self, path):
        """A dropped or browsed image: show the thumbnail and the swatches,
        then wait. Nothing about the theme changes until Fill chips."""
        try:
            colors = extract_colors(path)
        except Exception as e:      # also Pillow's DecompressionBombError and MemoryError
            QMessageBox.warning(self, "Image failed", f"Couldn't read that image:\n{e}")
            return
        if len(colors) < 2:
            QMessageBox.warning(self, "Image failed", "That image has too few colors to use.")
            return
        self._extracted = colors
        self.drop.set_image(path)
        self._show_swatches(colors)
        self.fill_btn.setEnabled(True)
        self.status.setText(f"Loaded {Path(path).name}. Press Fill chips to use its colors.")

    def _apply_chips(self, mapping):
        """Set several chips at once (Fill chips, Scramble), remembering the
        previous state for Undo."""
        self._undo = (self._accent, dict(self._tokens))
        self.undo_btn.setEnabled(True)
        for key, value in mapping.items():
            if key == "accent":
                self._accent = value
            else:
                self._tokens[key] = value
        fixed = self._aa_fix()              # Fill and Scramble always land on AA
        self._refresh_chips()
        self._schedule()
        return fixed

    def _undo_last(self):
        if self._undo is None:
            return
        self._accent, self._tokens = self._undo[0], dict(self._undo[1])
        self._undo = None
        self.undo_btn.setEnabled(False)
        self._refresh_chips()
        self._schedule()
        self.status.setText("Undone.")

    def _fill_from_image(self):
        suggested = suggest_chips(self._extracted, smart=True)
        if not suggested:
            return
        fixed = self._apply_chips(suggested)
        if suggested.get("accent") not in self._extracted:
            msg = ("The image had no strong accent color, so a complementary one "
                   "was generated. Click any chip to adjust it.")
        else:
            msg = "Filled from the image. Click any chip to adjust it."
        if fixed:
            msg += " Adjusted " + ", ".join(fixed).lower() + " to meet WCAG AA."
        self.status.setText(msg)

    def _scramble(self):
        """Shuffle colors across the seven chips. The pool is the extracted
        image colors (topped up from the current chips to seven), or the
        current seven colors when no image is loaded. The result is kept
        readable: text is swapped for light or dark if it fails contrast, and
        muted text is blended toward the text color if it nearly vanishes."""
        pal = self._pal()
        current = [pal[k] for k in CHIP_KEYS]
        pool = list(dict.fromkeys(self._extracted)) if self._extracted else list(current)
        extras = [c for c in current if c not in pool] + current
        i = 0
        while len(pool) < len(CHIP_KEYS):
            pool.append(extras[i % len(extras)])
            i += 1
        pool = pool[:len(CHIP_KEYS)]
        if len(set(pool)) < 2:
            self.status.setText("Nothing to scramble: all the colors are the same.")
            return
        picked = list(pool)
        for _ in range(8):                   # avoid landing on the arrangement we already have
            random.shuffle(picked)
            if picked != current:
                break
        mapping = dict(zip(CHIP_KEYS, picked))
        bg = mapping["bg_base"]
        mapping["text"] = _readable_text(mapping["text"], bg)
        mapping["text_muted"] = _readable_muted(mapping["text_muted"], mapping["text"], bg)
        fixed = self._apply_chips(mapping)
        msg = "Scrambled. Press Scramble again for another arrangement, or Undo."
        if fixed:
            msg += " Adjusted " + ", ".join(fixed).lower() + " to meet WCAG AA."
        self.status.setText(msg)

    def _show_swatches(self, colors):
        while self._swatch_row.count():
            item = self._swatch_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for hx in colors:
            sw = QFrame()
            sw.setFixedSize(36, 28)
            sw.setToolTip(hx)
            sw.setStyleSheet(f"background: {hx}; border: 1px solid transparent; "
                             "border-radius: 4px;")
            self._swatch_row.addWidget(sw)
        self._swatch_row.addStretch(1)

    # ---- files ----

    def _scan_files(self, select=None):
        """List the theme files the Theme tab would also accept, by theme name.
        Keeps the current choice (or selects `select`, a path) after a rescan."""
        keep = select or self.load_combo.currentData()
        self.load_combo.clear()
        d = ThemePage._themes_dir()
        try:
            paths = sorted(d.glob("*.json"), key=lambda p: p.name.lower())
        except OSError:
            paths = []
        for p in paths:
            loaded = _load_theme_file(p)
            if loaded is None:
                continue
            raw, _entry = loaded
            self.load_combo.addItem(self._display_name(raw, p), str(p))
            self.load_combo.setItemData(self.load_combo.count() - 1, p.name, Qt.ToolTipRole)
        idx = self.load_combo.findData(keep) if keep else -1
        if idx >= 0:
            self.load_combo.setCurrentIndex(idx)
        self.load_theme_btn.setEnabled(self.load_combo.count() > 0)

    @staticmethod
    def _display_name(raw, path):
        nm = raw.get("name") if isinstance(raw, dict) else None
        return nm.strip() if isinstance(nm, str) and nm.strip() else Path(path).stem

    def _load_selected(self):
        path = self.load_combo.currentData()
        if not path:
            return
        loaded = _load_theme_file(Path(path))
        raw, entry = loaded if loaded else (None, None)
        if entry is None:
            QMessageBox.warning(self, "Load failed", "That file is not a valid theme.")
            return
        # A loaded file replaces the theme being edited, not part of it.
        self._preset = entry["preset"]
        self._accent = entry["accent"]
        self._tokens = dict(entry["tokens"])
        self._undo = None
        self.undo_btn.setEnabled(False)
        self.name_edit.setText(self._display_name(raw, path)[:MAX_NAME_LEN])
        self._refresh_chips()
        self._schedule()
        self.status.setText(f"Loaded {Path(path).name}.")

    def _save(self):
        display = self.name_edit.text().strip() or DEFAULT_NAME
        d = ThemePage._themes_dir()
        target = d / (_safe_stem(display) + ".json")
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        if target.exists() and QMessageBox.question(
                self, "Replace theme", f"{target.name} already exists. Replace it?") != QMessageBox.Yes:
            return
        pal = self._pal()
        tokens = dict(self._tokens)
        tokens.update({k: pal[k] for k in CHIP_KEYS if k != "accent"})
        # Export format, the shape gui_theme_page._theme_from_dict reads back.
        data = {"name": display, "theme_preset": self._preset, "accent": self._accent,
                "theme_tokens": tokens}
        try:
            target.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.name_edit.setText(display)
        self._scan_files(select=str(target))      # new file shows in the dropdown, selected
        self.themeSaved.emit(str(target))
        self._apply_now()                         # Save = Apply: one click does both
        self.status.setText(f"Saved \u201c{display}\u201d as {target.name} and applied it.")
