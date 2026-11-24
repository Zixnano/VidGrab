"""A permissive stand-in for PySide6, used only when PySide6 is not installed.

It lets the tests import and *run* the Qt modules' logic (constructors, signal wiring, game
overlay flow) to catch typos and wiring mistakes. It does not validate real Qt API usage: on a
machine with PySide6 the same tests run against the real library, offscreen.
"""
import sys
import types


class _Meta(type):
    def __getattr__(cls, name):
        return Any()


class Any(metaclass=_Meta):
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return Any()

    def __call__(self, *a, **k):
        return Any()

    def __or__(self, other):
        return self

    def __int__(self):
        return 0

    def __sub__(self, other):
        return Any()

    __add__ = __mul__ = __truediv__ = __rsub__ = __radd__ = __sub__


def is_stub():
    try:
        import PySide6  # noqa: F401
        return getattr(sys.modules["PySide6"], "__stub__", False)
    except ImportError:
        return True


def install():
    if "PySide6" in sys.modules and not getattr(sys.modules["PySide6"], "__stub__", False):
        return False
    try:
        import PySide6  # noqa: F401
        return False
    except ImportError:
        pass

    def module(name, names):
        m = types.ModuleType(name)
        for n in names:
            setattr(m, n, Any)
        return m

    core = module("PySide6.QtCore", ["QRectF", "QPointF", "QEvent", "QTimer", "QSize", "QObject"])
    core.Qt = Any()
    core.Signal = lambda *a, **k: Any()
    gui = module("PySide6.QtGui", ["QColor", "QFont", "QPolygonF", "QPainter", "QPixmap", "QImage",
                                   "QImageReader", "QAction", "QIcon", "QKeySequence", "QShortcut"])
    widgets = module("PySide6.QtWidgets", [
        "QWidget", "QDialog", "QMainWindow", "QLabel", "QPushButton", "QComboBox", "QCheckBox", "QProgressBar",
        "QPlainTextEdit", "QMenu", "QMessageBox", "QFileDialog", "QHBoxLayout", "QVBoxLayout", "QGraphicsScene",
        "QGraphicsView", "QGraphicsItem", "QGraphicsPixmapItem", "QInputDialog", "QListWidget", "QListWidgetItem"])
    pkg = types.ModuleType("PySide6")
    pkg.__stub__ = True
    sys.modules.update({"PySide6": pkg, "PySide6.QtCore": core, "PySide6.QtGui": gui, "PySide6.QtWidgets": widgets})
    return True
