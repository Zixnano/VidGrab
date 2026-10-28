"""Window sizing and stylesheet helpers shared by the GUI modules.

fit_to_screen / install_fit: a dialog asks for the size it would like, and gets
the largest size that fits the screen it opens on (so nothing spawns bigger than
the monitor), with a minimum small enough that it can always be shrunk until
every button is reachable. Content that does not fit scrolls instead of
forcing the window big.

apply_app_qss: QApplication.setStyleSheet re-polishes every widget in the app
even when the text is identical. This skips the call when nothing changed.
"""

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer
from PySide6.QtGui import QCursor, QGuiApplication

FRACTION = 0.92          # a window never asks for more than this share of the screen


def apply_app_qss(app, qss):
    """app.setStyleSheet(qss) unless the app already has exactly this text.
    Returns True when the stylesheet was applied."""
    if app is None:
        return False
    try:
        if app.styleSheet() == qss:
            return False
    except Exception:
        pass
    app.setStyleSheet(qss)
    return True


def _screen_for(win):
    scr = None
    try:
        parent = win.parentWidget()
        if parent is not None:
            scr = parent.screen()
        if scr is None:
            scr = QGuiApplication.screenAt(QCursor.pos())
    except Exception:
        scr = None
    return scr or QGuiApplication.primaryScreen()


def fit_to_screen(win, width, height, min_w=420, min_h=320):
    """Size `win` to (width, height) but never beyond FRACTION of the usable
    screen, and give it a minimum no bigger than that either."""
    scr = _screen_for(win)
    if scr is None:
        win.setMinimumSize(min_w, min_h)
        win.resize(width, height)
        return
    av = scr.availableGeometry()
    max_w, max_h = int(av.width() * FRACTION), int(av.height() * FRACTION)
    win.setMinimumSize(min(min_w, max_w), min(min_h, max_h))
    win.resize(min(width, max_w), min(height, max_h))


def ensure_on_screen(win):
    """Nudge `win` so its whole frame is inside the usable screen area (as far
    as its size allows). A big dialog centered over a main window near a screen
    edge used to open with its bottom row of buttons off screen."""
    try:
        scr = win.screen() or _screen_for(win)
        if scr is None:
            return
        av = scr.availableGeometry()
        fg = win.frameGeometry()
        dx = dy = 0
        if fg.width() >= av.width() or fg.left() < av.left():
            dx = av.left() - fg.left()
        elif fg.right() > av.right():
            dx = av.right() - fg.right()
        if fg.height() >= av.height() or fg.top() < av.top():
            dy = av.top() - fg.top()
        elif fg.bottom() > av.bottom():
            dy = av.bottom() - fg.bottom()
        if dx or dy:
            win.move(win.pos() + QPoint(dx, dy))
    except Exception:
        pass


class _ShowFilter(QObject):
    """Runs ensure_on_screen once the window is really shown."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and not event.spontaneous():
            QTimer.singleShot(0, lambda w=obj: ensure_on_screen(w))
        return False


def install_fit(win, width, height, min_w=420, min_h=320):
    """fit_to_screen now, and keep the window on screen each time it is shown."""
    fit_to_screen(win, width, height, min_w, min_h)
    flt = _ShowFilter(win)
    win._vg_fit_filter = flt
    win.installEventFilter(flt)


# ---------------------------------------------------------------------------
# Animations
# ---------------------------------------------------------------------------
from PySide6.QtCore import QEasingCurve, QVariantAnimation, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPainter  # noqa: E402
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget  # noqa: E402


class _Veil(QWidget):
    """A flat color laid over a widget that fades out. Painted, not a graphics
    effect, so it is safe on scroll-area viewports and cheap."""

    def __init__(self, target):
        super().__init__(target)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self._alpha = 0
        self._color = QColor("#000000")
        self._anim = None

    def run(self, color, duration, start_alpha):
        if self._anim is not None:
            self._anim.stop()
        self._color = QColor(color)
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        a = QVariantAnimation(self)
        a.setDuration(duration)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.setStartValue(float(start_alpha))
        a.setEndValue(0.0)
        a.valueChanged.connect(self._step)
        a.finished.connect(self.hide)
        self._anim = a
        a.start()

    def _step(self, v):
        self._alpha = int(v)
        self.update()

    def paintEvent(self, _e):
        if self._alpha <= 0:
            return
        p = QPainter(self)
        c = QColor(self._color)
        c.setAlpha(self._alpha)
        p.fillRect(self.rect(), c)
        p.end()


def fade_veil(widget, enabled, color, duration=170, start_alpha=170):
    """Soft fade-in for `widget` (used on the table viewport when rows change).
    One veil per widget, restarted if a fade is already running."""
    if not enabled or widget is None:
        return
    veil = getattr(widget, "_vg_veil", None)
    if veil is None:
        veil = _Veil(widget)
        widget._vg_veil = veil
    veil.run(color, duration, start_alpha)


def fade_in_widget(widget, enabled, duration=140):
    """Opacity fade-in for an ordinary widget (not a viewport). One effect per
    widget, reused; a fade that is already running is restarted, so rapid calls
    never replace an effect an animation is still driving."""
    if not enabled or widget is None:
        return
    eff = getattr(widget, "_vg_fade_effect", None)
    if eff is None:
        eff = QGraphicsOpacityEffect(widget)
        widget._vg_fade_effect = eff
    anim = getattr(widget, "_vg_fade_anim", None)
    if anim is not None:
        anim.stop()
    widget.setGraphicsEffect(eff)
    eff.setOpacity(0.0)
    a = QVariantAnimation(widget)
    a.setDuration(duration)
    a.setEasingCurve(QEasingCurve.OutCubic)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.valueChanged.connect(lambda v, e=eff: e.setOpacity(float(v)))
    a.finished.connect(lambda w=widget: w.setGraphicsEffect(None))
    widget._vg_fade_anim = a
    a.start()
