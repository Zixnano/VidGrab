"""Window sizing and stylesheet helpers shared by the GUI modules.

fit_to_screen / install_fit: a dialog asks for the size it would like, and gets
the largest size that fits the screen it opens on (so nothing spawns bigger than
the monitor), with a minimum small enough that it can always be shrunk until
every button is reachable. Content that does not fit scrolls instead of
forcing the window big.

Applies to EVERY dialog, not just the ones that call install_fit: the first
install_fit call (the main window makes one at startup) installs an
application-wide filter, and from then on each QDialog is wrapped in a scroll
area (make_scrollable) the first time it is shown, capped to the screen it
opens on, and given a minimum size small enough to shrink. So a new dialog
added later is covered without remembering to call anything. Set the
environment variable VG_NO_AUTOFIT=1 to switch the automatic part off.

apply_app_qss: QApplication.setStyleSheet re-polishes every widget in the app
even when the text is identical. This skips the call when nothing changed.
"""

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QScrollArea, QVBoxLayout, QWidget

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


def make_scrollable(win):
    """Wrap `win`'s existing layout in a QScrollArea so the window no longer
    has to be as tall/wide as its content. Idempotent. The window's own
    minimum size (set by fit_to_screen) then governs, and whatever does not
    fit scrolls."""
    if getattr(win, "_vg_scroll", None) is not None:
        return win._vg_scroll
    old = win.layout()
    if old is None:
        return None
    body = QWidget()
    body.setObjectName("VgScrollBody")
    body.setStyleSheet("#VgScrollBody { background: transparent; }")
    body.setLayout(old)              # Qt re-parents the layout to `body`
    scroll = QScrollArea(win)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    scroll.viewport().setAutoFillBackground(False)
    scroll.setWidget(body)
    body.setAutoFillBackground(False)
    outer = QVBoxLayout(win)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(scroll)
    win._vg_scroll = scroll
    return scroll


def auto_scroll_if_needed(win):
    """If a dialog's layout needs more room than the screen offers, make it
    scrollable and pull it back inside the screen. No-op for dialogs that fit
    and for anything that is not a QDialog."""
    if not isinstance(win, QDialog) or getattr(win, "_vg_scroll", None) is not None:
        return False
    lay = win.layout()
    scr = _screen_for(win)
    if lay is None or scr is None:
        return False
    av = scr.availableGeometry()
    need = lay.totalMinimumSize()
    # ~40 px for the title bar and frame.
    if (need.height() <= av.height() * FRACTION - 40
            and need.width() <= av.width() * FRACTION):
        return False
    if make_scrollable(win) is None:
        return False
    win.resize(min(win.width(), int(av.width() * FRACTION)),
               min(win.height(), int(av.height() * FRACTION) - 40))
    return True


def _after_show(win):
    try:
        auto_scroll_if_needed(win)
    except Exception:
        pass
    ensure_on_screen(win)


class _ShowFilter(QObject):
    """Per-window: runs the fit checks once the window is really shown."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and not event.spontaneous():
            QTimer.singleShot(0, lambda w=obj: _after_show(w))
        return False


# Dialogs whose own layout is native or purpose-built: never wrapped.
_SKIP_CLASSES = {"QMessageBox", "QFileDialog", "QColorDialog", "QFontDialog",
                 "QInputDialog", "QProgressDialog", "QWizard", "QErrorMessage"}


def _wrappable(win):
    if not isinstance(win, QDialog) or win.layout() is None:
        return False
    if win.windowType() not in (Qt.Window, Qt.Dialog):
        return False
    if getattr(win, "_vg_scroll", None) is not None:
        return False
    return not any(c.__name__ in _SKIP_CLASSES for c in type(win).__mro__)


def fit_dialog(win):
    """Make one dialog resizable on any screen: cap it to the screen, lower an
    oversized minimum, and wrap its content in a scroll area."""
    scr = _screen_for(win)
    if scr is None:
        return
    av = scr.availableGeometry()
    max_w = int(av.width() * FRACTION)
    max_h = int(av.height() * FRACTION) - 40          # title bar + frame
    if _wrappable(win):
        asked = hasattr(win, "_vg_fit_filter")     # called install_fit: its minimum is deliberate
        keep_min = win.minimumSize() if asked else None
        keep_size = win.size()
        make_scrollable(win)
        # The old minimum was driven by the (now scrolling) content, so reset
        # it: only an explicit, small minimum should limit shrinking.
        if keep_min is not None:
            win.setMinimumSize(min(keep_min.width(), max_w), min(keep_min.height(), max_h))
        else:
            win.setMinimumSize(min(360, max_w, keep_size.width()),
                               min(240, max_h, keep_size.height()))
        win.resize(keep_size)
    else:
        win.setMinimumSize(min(win.minimumWidth(), max_w),
                           min(win.minimumHeight(), max_h))
    if win.width() > max_w or win.height() > max_h:
        win.resize(min(win.width(), max_w), min(win.height(), max_h))


class _GlobalFit(QObject):
    """Application-wide: every top-level QDialog gets fit_dialog on first show."""

    def eventFilter(self, obj, event):
        try:
            if (event.type() == QEvent.Show and not event.spontaneous()
                    and isinstance(obj, QDialog)
                    and not getattr(obj, "_vg_fit_seen", False)):
                obj._vg_fit_seen = True
                fit_dialog(obj)
                QTimer.singleShot(0, lambda w=obj: ensure_on_screen(w))
        except Exception:
            pass
        return False


def _ensure_global_filter():
    import os
    if os.environ.get("VG_NO_AUTOFIT"):
        return
    app = QApplication.instance()
    if app is None or getattr(app, "_vg_global_fit", None) is not None:
        return
    flt = _GlobalFit(app)
    app._vg_global_fit = flt
    app.installEventFilter(flt)


def install_fit(win, width, height, min_w=420, min_h=320):
    """fit_to_screen now, and keep the window on screen each time it is shown."""
    _ensure_global_filter()
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
