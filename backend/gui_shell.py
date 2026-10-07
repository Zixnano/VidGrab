"""Optional handheld shell drawn around the main window: plastic body, hinge,
dark screen bezel, D-pad, A/B, speaker, Select/Start and a power light.
The controls are real: they emit signals MainWindow connects to actions.
Pure painting, no backend calls."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QWidget

# name: (top, body, bottom, edge)
SHELLS = {
    "cream": ("#efebdc", "#d9d3c0", "#b9b39f", "#8f8a78"),
    "black": ("#4a4d4b", "#2f3231", "#1b1d1c", "#0c0d0d"),
    "indigo": ("#6a6fc4", "#4a4fa8", "#2e3278", "#1d2050"),
    "silver": ("#d5d9dc", "#b8bdc0", "#8e9396", "#6a6f72"),
}
SHELL_NAMES = {"cream": "Cream", "black": "Matte black", "indigo": "Indigo", "silver": "Silver"}
COLUMN = 210   # width of the controls column


class ShellFrame(QWidget):
    dpad = Signal(str)      # "up" "down" "left" "right"
    button = Signal(str)    # "a" "b" "select" "start"

    def __init__(self, inner, color="cream", parent=None):
        super().__init__(parent)
        self.inner = inner
        self.color = color if color in SHELLS else "cream"
        self._hits = {}
        self.setMouseTracking(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(38, 62, COLUMN + 12, 40)
        lay.addWidget(inner)
        self.setMinimumSize(900, 600)

    def set_color(self, color):
        self.color = color if color in SHELLS else "cream"
        self.update()

    # ---- painting ----
    def paintEvent(self, _e):
        top, body, bot, edge = (QColor(c) for c in SHELLS[self.color])
        dark = self.color == "black"
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
        # hinge strip with two raised tabs
        p.setPen(QPen(edge, 1.5))
        p.setBrush(top.darker(106) if not dark else top.lighter(115))
        p.drawRoundedRect(QRectF(4, 14, w - 8, 26), 14, 14)
        for x in (70, w - 220):
            p.drawRoundedRect(QRectF(x, 2, 150, 24), 9, 9)
        # screen bezel behind the inner widget
        r = self.inner.geometry().adjusted(-12, -12, 12, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1b1f1d"))
        p.drawRoundedRect(QRectF(r), 16, 16)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(QRectF(r.x(), r.bottom() - 2, r.width(), 2), 1, 1)
        self._controls(p, w, h, dark)
        p.end()

    def _controls(self, p, w, h, dark):
        cx = w - COLUMN / 2 - 4
        ctl = QColor("#2b2e2c") if not dark else QColor("#121413")
        ctl_hi = QColor("#4a4e4b")
        label = QColor("#6f6a58") if not dark else QColor("#8c908d")
        self._hits = {}
        # power light
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#5ee07a"))
        p.drawEllipse(QPointF(w - 44, 74), 8, 8)
        f = p.font(); f.setPixelSize(10); f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4); p.setFont(f)
        p.setPen(label)
        p.drawText(QRectF(w - 130, 64, 70, 20), Qt.AlignRight | Qt.AlignVCenter, "POWER")
        # d-pad
        dy = max(190, h * 0.30)
        arm, ln = 40, 120
        p.setPen(QPen(QColor("#000"), 1))
        p.setBrush(ctl)
        p.drawRoundedRect(QRectF(cx - ln / 2, dy - arm / 2, ln, arm), 8, 8)
        p.drawRoundedRect(QRectF(cx - arm / 2, dy - ln / 2, arm, ln), 8, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(ctl_hi)
        for name, (ox, oy) in {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.items():
            c = QPointF(cx + ox * 40, dy + oy * 40)
            sz = 6
            tip = QPointF(c.x() + ox * sz, c.y() + oy * sz)
            b1 = QPointF(c.x() - ox * sz * 0.6 - oy * sz, c.y() - oy * sz * 0.6 + ox * sz)
            b2 = QPointF(c.x() - ox * sz * 0.6 + oy * sz, c.y() - oy * sz * 0.6 - ox * sz)
            p.drawPolygon(QPolygonF([tip, b1, b2]))
            self._hits[name] = QRectF(c.x() - 20, c.y() - 20, 40, 40)
        p.setBrush(QColor("#1d201e"))
        p.drawEllipse(QPointF(cx, dy), 9, 9)
        # A / B
        ay = max(340, h * 0.55)
        for name, (x, y) in {"a": (cx + 46, ay - 22), "b": (cx - 28, ay + 14)}.items():
            gr = QLinearGradient(x, y - 30, x, y + 30)
            gr.setColorAt(0, ctl_hi)
            gr.setColorAt(1, ctl)
            p.setPen(QPen(QColor("#000"), 1))
            p.setBrush(gr)
            p.drawEllipse(QPointF(x, y), 30, 30)
            p.setPen(QColor("#9aa09c"))
            f2 = p.font(); f2.setPixelSize(22); f2.setBold(True); f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0); p.setFont(f2)
            p.drawText(QRectF(x - 30, y - 30, 60, 60), Qt.AlignCenter, name.upper())
            self._hits[name] = QRectF(x - 30, y - 30, 60, 60)
        # speaker dots
        sy = max(450, h * 0.72)
        p.setPen(Qt.NoPen)
        p.setBrush(label if not dark else QColor("#3a3d3b"))
        for i in range(5):
            for j in range(4):
                p.drawEllipse(QPointF(cx + 10 + i * 14 + j * 6, sy + j * 12 - i * 5), 3, 3)
        # select / start
        by = h - 70
        f3 = p.font(); f3.setPixelSize(10); f3.setBold(False); f3.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4); p.setFont(f3)
        for name, x in (("select", cx - 38), ("start", cx + 38)):
            p.setPen(QPen(QColor("#a8a28c") if not dark else QColor("#000"), 1))
            p.setBrush(QColor("#e8e4d4") if not dark else QColor("#3f4240"))
            p.save()
            p.translate(x, by)
            p.rotate(-22)
            p.drawRoundedRect(QRectF(-24, -8, 48, 16), 8, 8)
            p.restore()
            p.setPen(label)
            p.drawText(QRectF(x - 40, by + 12, 80, 16), Qt.AlignCenter, name.upper())
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
