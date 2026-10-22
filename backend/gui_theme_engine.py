"""Theme Engine: build a theme from an image or by picking colors, with a
live preview in the running app.

The dialog never writes settings. It emits themeChanged(state), where state
is the full settings dict (saved settings plus the theme being edited), and
the host applies it with gui_style.build_qss. The only file it writes is
HOME/themes/custom.json, in the Export format that
gui_theme_page._theme_from_dict reads back, so the Theme tab shows it.

Pillow is optional. With it, colors come from Pillow's quantizer; without it
the same step runs on Qt's own image reader and a built-in median cut, so the
drop zone always works.

The image step: drop an image on the zone at the top (or click it to browse).
The dialog shows a thumbnail and the extracted swatches, and waits. The theme
only changes when you press Fill chips.
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from gui_theme_page import ThemePage, _theme_from_dict
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
LOW_CONTRAST_CHIPS = ("text_muted",)   # each is checked against bg_base
MIN_TEXT_CONTRAST = 4.5                # WCAG AA for normal text
LIVE_DEBOUNCE_MS = 75
SAVE_NAME = "custom.json"
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
        im = im.convert("RGB")
        im.thumbnail(EXTRACT_SIZE)
        q = im.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
    flat = q.getpalette() or []
    out = []
    for _n, idx in sorted(q.getcolors() or [], reverse=True):
        r, g, b = flat[idx * 3: idx * 3 + 3]
        hx = "#%02x%02x%02x" % (r, g, b)
        if hx not in out:
            out.append(hx)
    return out[:count]


def suggest_chips(colors):
    """Map extracted colors onto the chips. The darkest color is the background
    and the lightest is the text. Of the colors in between, the most saturated
    becomes the accent, and the rest fill panel, elevated and border in order.
    If the text fails MIN_TEXT_CONTRAST against the background, it is replaced
    by #f2f2f2 or #111111, whichever contrasts more. Chips with no color to
    take are left out of the result, so they keep their current value."""
    if len(colors) < 2:
        return {}
    ordered = sorted(colors, key=luminance)
    bg, text = ordered[0], ordered[-1]
    middle = ordered[1:-1]
    out = {"bg_base": bg}
    if middle:
        accent = max(middle, key=lambda c: QColor(c).hsvSaturationF())
        out["accent"] = accent
        middle.remove(accent)
    for key in ("bg_panel", "bg_elevated", "border"):
        if middle:
            out[key] = middle.pop(0)
    if contrast(text, bg) < MIN_TEXT_CONTRAST:
        text = max(("#f2f2f2", "#111111"), key=lambda c: contrast(c, bg))
    out["text"] = text
    return out


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
        pm = self._pix.scaled(box.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p = QPainter(self)
        p.drawPixmap(box.x() + (box.width() - pm.width()) // 2,
                     box.y() + (box.height() - pm.height()) // 2, pm)


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

    def __init__(self, base_settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Theme Engine")
        self.setMinimumWidth(520)
        base = dict(base_settings or {})          # copy: load_settings() is read-only
        self._base = base
        preset = base.get("theme_preset")
        self._preset = preset if preset in PRESET_NAMES else "amoled_black"
        self._accent = base.get("accent") or THEME_PRESETS[self._preset]["accent"]
        self._tokens = {k: v for k, v in (base.get("theme_tokens") or {}).items()
                        if v and k != "accent"}
        self._extracted = []
        self._picker = None

        # A color drag fires many changes; the app restyles once per pause.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LIVE_DEBOUNCE_MS)
        self._timer.timeout.connect(self._emit_live)

        root = QVBoxLayout(self)

        hint = QLabel(f"Starting from {PRESET_NAMES[self._preset]}. Changes show in the app as "
                      "you make them. Save writes a theme file; closing this window puts "
                      "back your saved theme.")
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
        self.fill_btn.clicked.connect(self._fill_from_image)
        img_row.addWidget(self.fill_btn)
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

        # Load and save
        file_row = QHBoxLayout()
        self.load_combo = QComboBox()
        self.load_combo.setMinimumWidth(200)
        self.load_theme_btn = QPushButton("Load theme")
        self.load_theme_btn.clicked.connect(self._load_selected)
        self.save_btn = QPushButton(f"Save as {SAVE_NAME}")
        self.save_btn.clicked.connect(self._save)
        file_row.addWidget(self.load_combo, 1)
        file_row.addWidget(self.load_theme_btn)
        file_row.addWidget(self.save_btn)
        root.addLayout(file_row)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("CardHint")
        self.status.setWordWrap(True)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

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
        self._timer.start()

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
            if key in LOW_CONTRAST_CHIPS:
                chip.warn.setVisible(contrast(pal[key], pal["bg_base"]) < MIN_TEXT_CONTRAST)

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
        self._picker = dlg          # keep a reference while the picker is open
        dlg.open()

    # ---- image ----

    def _load_image(self):
        """Browse (the drop zone was clicked)."""
        path, _ = QFileDialog.getOpenFileName(self, "Pick an image", "",
                                              "Images (" + " ".join("*" + e for e in IMAGE_EXTS)
                                              + ");;All files (*)")
        if path:
            self._load_image_path(path)

    def _load_image_path(self, path):
        """A dropped or browsed image: show the thumbnail and the swatches,
        then wait. Nothing about the theme changes until Fill chips."""
        try:
            colors = extract_colors(path)
        except (OSError, ValueError) as e:
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

    def _fill_from_image(self):
        suggested = suggest_chips(self._extracted)
        if not suggested:
            return
        for key, value in suggested.items():
            if key == "accent":
                self._accent = value
            else:
                self._tokens[key] = value
        self._refresh_chips()
        self._schedule()
        self.status.setText("Filled from the image. Click any chip to adjust it.")

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

    def _scan_files(self):
        """List the theme files the Theme tab would also accept."""
        self.load_combo.clear()
        d = ThemePage._themes_dir()
        try:
            paths = sorted(d.glob("*.json"), key=lambda p: p.name.lower())
        except OSError:
            paths = []
        for p in paths:
            try:
                if not p.is_file() or p.stat().st_size > MAX_THEME_BYTES:
                    continue
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _theme_from_dict(raw) is None:
                continue
            self.load_combo.addItem(p.name, str(p))
        self.load_theme_btn.setEnabled(self.load_combo.count() > 0)

    def _load_selected(self):
        path = self.load_combo.currentData()
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Load failed", str(e))
            return
        entry = _theme_from_dict(raw)
        if entry is None:
            QMessageBox.warning(self, "Load failed", "That file is not a valid theme.")
            return
        # A loaded file replaces the theme being edited, not part of it.
        self._preset = entry["preset"]
        self._accent = entry["accent"]
        self._tokens = dict(entry["tokens"])
        self._refresh_chips()
        self._schedule()
        self.status.setText(f"Loaded {Path(path).name}.")

    def _save(self):
        d = ThemePage._themes_dir()
        target = d / SAVE_NAME
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        if target.exists() and QMessageBox.question(
                self, "Replace theme", f"{SAVE_NAME} already exists. Replace it?") != QMessageBox.Yes:
            return
        pal = self._pal()
        tokens = dict(self._tokens)
        tokens.update({k: pal[k] for k in CHIP_KEYS if k != "accent"})
        # Export format, the shape gui_theme_page._theme_from_dict reads back.
        data = {"name": "Custom", "theme_preset": self._preset, "accent": self._accent,
                "theme_tokens": tokens}
        try:
            target.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self._scan_files()
        self.status.setText(f"Saved {target}.")
