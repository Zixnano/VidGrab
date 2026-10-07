"""Theme settings page: live preview, preset tiles, accent swatches and an
optional fine-tune section. Pure UI. SettingsDialog creates one ThemePage
and reads ThemePage.values() on Save. Nothing here touches the network or
saves settings by itself."""
import json
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QRectF, QSize, QPoint, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QColorDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLayout, QMessageBox, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget, QWidgetItem,
)

from gui_shell import SHELL_NAMES, SHELLS
from palette import (PRESET_NAMES, THEME_PRESETS, contrast, luminance, mix,
                     resolve_palette)

ACCENT_SWATCHES = ["#26c6da", "#7c4dff", "#66bb6a", "#ffa726", "#ef5350",
                   "#42a5f5", "#ec407a", "#8ccf72"]

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

        def font(px, bold=False, mono=False):
            f = QFont(_family(t["font_mono"]) if mono else fam)
            f.setPixelSize(max(7, int(px * s)))
            f.setBold(bold)
            return f

        def text(x, y, wd, ht, s_, color, fnt, flags=Qt.AlignVCenter | Qt.AlignLeft):
            p.setFont(fnt)
            p.setPen(QColor(color))
            p.drawText(QRectF(x * s, y * s, wd * s, ht * s), int(flags), s_)

        def box(x, y, wd, ht, fill, border=None, r=6):
            p.setPen(QPen(QColor(border), 1) if border else Qt.NoPen)
            p.setBrush(QColor(fill) if fill else Qt.NoBrush)
            p.drawRoundedRect(QRectF(x * s, y * s, wd * s, ht * s), r * s, r * s)

        box(0.5, 0.5, 559, h / s - 1, t["bg_base"], t["border"], 10)
        # sidebar
        box(1, 1, 120, h / s - 2, t["bg_side"], None, 9)
        text(12, 10, 100, 16, "\u2193 VIDEO GRABBER", t["text"], font(8, True))
        for i, name in enumerate(["All", "Active", "Finished", "Failed", "Video"]):
            y = 36 + i * 24
            if i == 0:
                box(8, y, 104, 20, t["bg_sel"], None, 6)
            text(18, y, 90, 20, name, t["text"] if i == 0 else t["text_soft"], font(9, i == 0))
        # toolbar
        box(122, 1, 437, 36, t["bg_panel"], None, 0)
        box(132, 8, 70, 22, t["accent"], None, 6)
        text(132, 8, 70, 22, "+ Add URL", t["text_on_accent"], font(9, True), Qt.AlignCenter)
        for i, name in enumerate(["Start", "Pause", "Stop"]):
            text(212 + i * 48, 8, 46, 22, name, t["text_soft"], font(9), Qt.AlignCenter)
        box(440, 8, 108, 22, t["bg_input"], t["border"], 6)
        text(448, 8, 90, 22, "Search\u2026", t["text_dim"], font(8))
        # table
        top = 46
        box(130, top, 420, 150, t["bg_panel"], t["border"], 6)
        box(131, top + 1, 418, 22, t["bg_elevated"], None, 5)
        for x, label in ((140, "Name"), (300, "Size"), (350, "Progress"), (480, "Status")):
            text(x, top + 1, 70, 22, label, t["text_muted"], font(8, True))
        rows = [("Studio session part 4.mp4", "212 MB", 62, "Loading", t["text_soft"]),
                ("Home _ X (2).mp4", "122 KB", 100, "Done", t["accent"]),
                ("watch (2) (1).mp4", "48 MB", 30, "Paused", t["warning"]),
                ("clip-0412.mp4", "0 B", 0, "Error", t["error"])]
        for i, (n, sz, pct, st, col) in enumerate(rows):
            y = top + 26 + i * 30
            if i == 1:
                box(132, y - 2, 416, 28, t["bg_sel"], None, 4)
            text(140, y, 150, 24, n if len(n) < 24 else n[:22] + "\u2026", t["text"], font(9))
            text(300, y, 50, 24, sz, t["text_muted"], font(9))
            box(350, y + 8, 80, 8, t["bg_elevated"], None, 4)
            if pct:
                box(350, y + 8, 80 * pct / 100.0, 8, t["accent"], None, 4)
            text(440, y, 36, 24, "%d%%" % pct, t["text_soft"], font(8))
            text(480, y, 60, 24, st, col, font(9, True))
        # log
        box(130, 204, 420, 22, t["bg_input"], t["border"], 6)
        text(138, 204, 400, 22, "18:35:10  Job 564 started: Studio session part 4.mp4",
             t["text_muted"], font(8, mono=True))
        p.end()


class PresetTile(QAbstractButton):
    def __init__(self, key, ring, label_color, parent=None):
        super().__init__(parent)
        self.key = key
        self.ring = ring
        self.label_color = label_color
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(132, 86)
        self.setToolTip(PRESET_NAMES.get(key, key))
        self.setAccessibleName(PRESET_NAMES.get(key, key))

    def paintEvent(self, _e):
        t = THEME_PRESETS[self.key]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1.5, 1.5, self.width() - 3, 58)
        p.setPen(QPen(QColor(self.ring if self.isChecked() else t["border_hi"]),
                      2.5 if self.isChecked() else 1))
        p.setBrush(QColor(t["bg_base"]))
        p.drawRoundedRect(r, 8, 8)
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
        p.drawText(QRectF(0, 62, self.width(), 22), Qt.AlignCenter,
                   PRESET_NAMES.get(self.key, self.key))
        p.end()


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


class ThemePage(QWidget):
    changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        s = settings or {}
        self._preset = s.get("theme_preset") if s.get("theme_preset") in PRESET_NAMES else "amoled_black"
        self._accent = s.get("accent") or THEME_PRESETS[self._preset]["accent"]
        self._ov = {k: v for k, v in (s.get("theme_tokens") or {}).items()
                    if v and k != "accent"}
        self._shell_on = bool(s.get("shell_enabled"))
        self._shell_color = s.get("shell_color") if s.get("shell_color") in SHELLS else "cream"
        chrome = resolve_palette(s)
        self._ring, self._label = chrome["accent"], chrome["text"]
        self._rows = {}

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
            self._flow.addWidget(tile)
            self._tiles[key] = tile
        l1.addWidget(flow_host)
        cl.addWidget(c1)

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
        self.shell_check = QCheckBox("Show the handheld shell")
        self.shell_check.setChecked(self._shell_on)
        self.shell_check.toggled.connect(self._shell_toggled)
        l4.addWidget(self.shell_check)
        sh_host = QWidget()
        sh_host.setObjectName("Plain")
        sh_flow = FlowLayout(sh_host, 6)
        self._shell_sw = []
        for key, nm in SHELL_NAMES.items():
            b = Swatch(SHELLS[key][1], self._label)
            b.setToolTip(nm)
            b.setAccessibleName(nm + " shell")
            b.clicked.connect(lambda _=False, k=key: self._pick_shell(k))
            sh_flow.addWidget(b)
            self._shell_sw.append((key, b))
        l4.addWidget(sh_host)
        cl.addWidget(c4)

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

        self._wide = None
        self._relayout(self.width() or 600)
        self.fine_toggle.setChecked(bool(self._ov))
        self._toggle_fine(self.fine_toggle.isChecked())
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
            name.setText(dict(i for _, g in GROUPS for i in g)[key] + ("  \u2022" if edited else ""))
        n = len(self._ov)
        self.count_chip.setText(f"{n} customized" if n else "No tweaks")
        self.clear_btn.setVisible(n > 0)
        self.fine_toggle.setText(("\u25BE " if self.fine_toggle.isChecked() else "\u25B8 ")
                                 + "Edit individual colors")
        self.accent_label.setText(f"Current accent: {self._accent.upper()}")
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
        self._refresh()

    def _shell_toggled(self, on):
        self._shell_on = on
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
        c = QColorDialog.getColor(QColor(self._accent), self, "Custom accent color")
        if c.isValid():
            self._set_accent(c.name())

    def _pick_token(self, key):
        cur = QColor(self._tokens()[key])
        c = QColorDialog.getColor(cur, self, "Choose color")
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

    def _clear_tweaks(self):
        self._ov = {}
        self._refresh()

    def _reset_all(self):
        self._ov = {}
        self._shell_on = False
        self.shell_check.setChecked(False)
        self._pick_preset("amoled_black")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export theme", "theme.json", "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self.values(), indent=2, sort_keys=True),
                                  encoding="utf-8")

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
            self._refresh()
        except Exception as e:
            QMessageBox.warning(self, "Import failed", str(e))

    def values(self):
        return {"theme_preset": self._preset, "accent": self._accent,
                "theme_tokens": dict(self._ov), "shell_enabled": self._shell_on,
                "shell_color": self._shell_color}
