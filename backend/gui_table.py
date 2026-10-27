"""Download table, progress delegate, bottom pane and the file / drag helpers."""

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QElapsedTimer, QItemSelection, QItemSelectionModel, QMimeData, QPoint, QRect,
    QSize, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QColor, QDrag, QFont, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QRubberBand, QStyledItemDelegate, QTableView, QWidget,
)

from gui_detail import VIDEO_EXT

import gui_palette
from gui_common import _eta_seconds, _fmt_duration, _path_for, _retry_text, fmt_bytes, fmt_ts


class _BottomPane(QWidget):
    """Bottom half of the table / detail splitter. The detail pane inside it
    (thumbnail, tabs, log) can ask for a lot of height. Reporting that as this
    widget's minimum let it raise the whole window's minimum height every time
    a row got selected, which squeezed the status bar out of view. The splitter
    decides the real height (the divider is draggable and remembered), so the
    minimum reported here stays small."""
    MIN_H = 96

    def minimumSizeHint(self):
        return QSize(0, self.MIN_H)


class DownloadTable(QTableView):
    """The downloads table, with Windows-Explorer style mouse behaviour.

    A plain press never changes the selection on its own, so a box drag can
    start on any unselected row or empty space, even in a table that fills
    the whole viewport:
      * Press + drag draws a rubber-band box and highlights the rows it
        touches as you go. A plain click that never becomes a drag leaves the
        old selection alone until release. Ctrl/Shift + drag ADDS the box rows.
      * A file drag (to Explorer / Discord) starts from a press on an
        ALREADY-SELECTED, finished row (any column) and carries the whole
        selection, so you can box-select and then pick the rows up. Hold Alt
        on a selected row to draw a box from it instead.
      * A plain click collapses the selection to the clicked row (or clears it
        on empty space) on release. Ctrl+Click toggles and Shift+Click extends,
        handled by Qt as before.
    Qt's own drag is switched off (startDrag below does nothing): it started a
    file drag on any finished row, which is what made the box unreachable.
    The dragged row is found by job id at press time, because the model
    refreshes every second and rows can move before the drag starts."""
    DRAG_THRESHOLD = 5   # pixels
    EDGE_SCROLL_MS = 50  # box drag: auto-scroll tick while the cursor is outside the table
    boxFinished = Signal()   # a box drag ended; the host refreshes the details panel

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos = None      # left-press position (viewport coords); None = no press tracked
        self._press_kind = None     # "file" (may drag files out) or "band" (box select)
        self._press_id = None       # job id under a "file" press
        self._press_mods = Qt.NoModifier   # Ctrl/Shift held at the press
        self._press_event = None    # copy of a plain press, replayed on release if it was a click
        self._base_rows = set()     # rows selected when the press happened
        self.live_selecting = False # True while a box drag is changing the selection
        self._live_rows = None      # rows last applied by the box
        self._rb_origin = None      # rubber-band start, in content (scroll-independent) coords
        self._rb_last = None        # last mouse position in viewport coords
        self._rubber = None         # QRubberBand, created lazily
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(self.EDGE_SCROLL_MS)
        self._edge_timer.timeout.connect(self._edge_scroll)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDragEnabled(True)

    def startDrag(self, supportedActions):
        """Qt's built-in drag is disabled; _start_file_drag runs the file drag."""
        return

    # ---- rubber band -------------------------------------------------
    def _scroll_offset(self):
        # Pixel offsets of the content (the scroll bars count rows while the
        # table scrolls per item, so they cannot be used here).
        return self.horizontalHeader().offset(), self.verticalHeader().offset()

    def _band_begin(self, pos):
        ox, oy = self._scroll_offset()
        self._rb_origin = (pos.x() + ox, pos.y() + oy)
        self._rb_last = pos

    def _band_update(self):
        if self._rb_origin is None or self._rb_last is None:
            return
        ox, oy = self._scroll_offset()
        a = self._rb_origin
        start = QPoint(a[0] - ox, a[1] - oy)
        rect = QRect(start, self._rb_last).normalized()
        if rect.width() < self.DRAG_THRESHOLD and rect.height() < self.DRAG_THRESHOLD:
            return   # still just a click
        if self._rubber is None:
            self._rubber = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._rubber.setGeometry(rect.intersected(self.viewport().rect()))
        self._rubber.show()

    def _band_active(self):
        return self._rubber is not None and self._rubber.isVisible()

    def _band_end(self):
        self._rb_origin = self._rb_last = None
        if self._rubber is not None:
            self._rubber.hide()

    def _band_rows(self):
        """Rows the box touches, from its content-space rectangle."""
        if self._rb_origin is None or self._rb_last is None:
            return set()
        ox, oy = self._scroll_offset()
        a = self._rb_origin
        rect = QRect(QPoint(a[0], a[1]),
                     QPoint(self._rb_last.x() + ox, self._rb_last.y() + oy)).normalized()
        rows = set()
        for r in range(self.model().rowCount()):
            if self.isRowHidden(r):
                continue
            top = self.rowViewportPosition(r) + oy
            if top <= rect.bottom() and top + self.rowHeight(r) > rect.top():
                rows.add(r)
        return rows

    def _edge_scroll(self):
        """While a box drag is held outside the table, keep scrolling toward it."""
        if self._rb_last is None or self._press_kind != "band":
            self._edge_timer.stop()
            return
        sb = self.verticalScrollBar()
        y = self._rb_last.y()
        if y < 0:
            sb.setValue(sb.value() - 1)
        elif y > self.viewport().height():
            sb.setValue(sb.value() + 1)

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._band_update()   # keep the box anchored while autoscrolling

    # ---- selection helpers -------------------------------------------
    def _selected_row_set(self):
        sm = self.selectionModel()
        return {i.row() for i in sm.selectedIndexes()} if sm is not None else set()

    def _apply_rows(self, rows):
        """Make exactly `rows` the selection (empty clears it)."""
        sm, model = self.selectionModel(), self.model()
        if sm is None or model is None:
            return
        sel, last = QItemSelection(), model.columnCount() - 1
        for r in sorted(rows):
            sel.select(model.index(r, 0), model.index(r, last))
        sm.select(sel, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        if rows:   # the first row is the Shift+Click anchor afterwards
            sm.setCurrentIndex(model.index(min(rows), 0), QItemSelectionModel.NoUpdate)

    def _reset_press(self):
        self._press_pos = None
        self._press_kind = None
        self._press_id = None
        self._press_event = None
        self._press_mods = Qt.NoModifier
        self._base_rows = set()
        self._live_rows = None
        self.live_selecting = False
        self._edge_timer.stop()
        self._band_end()

    def _live_box_select(self):
        """Highlight the rows under the box while it is dragged."""
        rows = self._band_rows()
        target = (self._base_rows | rows) if self._press_mods else rows
        if target != self._live_rows:
            self._live_rows = target
            self._apply_rows(target)

    # ---- mouse -------------------------------------------------------
    def mousePressEvent(self, e):
        self._reset_press()
        if e.button() != Qt.LeftButton:
            super().mousePressEvent(e)       # right-click etc.: standard handling (context menu)
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        mods = e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
        items = getattr(self.model(), "items", [])
        self._press_pos = pos
        self._press_mods = mods
        self._press_kind = "band"
        self._base_rows = self._selected_row_set()
        self._band_begin(pos)
        self._edge_timer.start()
        if mods:
            # Ctrl/Shift+Click: Qt's own toggle / range selection, unchanged.
            # If this turns into a drag, the box rows are added on release.
            super().mousePressEvent(e)
            return
        # Plain press: leave the selection alone (a multi-row selection must
        # survive until we know if this is a file drag, a box or a click).
        self.setFocus(Qt.MouseFocusReason)
        if idx.isValid():
            self.selectionModel().setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
        if idx.isValid() and idx.row() < len(items):
            j = items[idx.row()]
            if (j.get("status") == "done"        # only finished files can be dragged out
                    and self.selectionModel().isRowSelected(idx.row(), idx.parent())
                    and not (e.modifiers() & Qt.AltModifier)):   # Alt: box from a selected row
                self._press_kind = "file"
                self._press_id = j.get("id")
        self._press_event = QMouseEvent(e)   # replayed on release if this was only a click
        e.accept()

    def mouseMoveEvent(self, e):
        if self._press_pos is None or not (e.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(e)        # hover etc.
            return
        pos = e.position().toPoint()
        if self._press_kind == "file":
            if (pos - self._press_pos).manhattanLength() >= self.DRAG_THRESHOLD:
                pid = self._press_id
                self._reset_press()
                self._start_file_drag(pid)
            return
        # Box select: draw the box and highlight the rows it touches.
        self._rb_last = pos
        self._band_update()
        if self._band_active():
            self.live_selecting = True
            self._live_box_select()
        e.accept()

    def mouseReleaseEvent(self, e):
        kind, mods, base = self._press_kind, self._press_mods, set(self._base_rows)
        press_event = self._press_event
        boxed = self._band_active()
        rows = self._band_rows() if boxed else None
        self._reset_press()
        if e.button() != Qt.LeftButton or kind is None:
            super().mouseReleaseEvent(e)
            return
        if mods:
            super().mouseReleaseEvent(e)     # Ctrl/Shift+Click result stands
            if rows is not None:
                self._apply_rows(base | rows)
                self.boxFinished.emit()
            return
        if rows is not None:                 # box finished: it is the selection
            self._apply_rows(rows)
            super().mouseReleaseEvent(e)
            self.boxFinished.emit()
            e.accept()
            return
        # A plain click: let Qt select (row, anchor and current row included).
        if press_event is not None:
            super().mousePressEvent(press_event)
        super().mouseReleaseEvent(e)

    def restore_selection(self, ids, current_id=None):
        """Re-select rows by job id after the model was rebuilt, and put the
        current row back. A model reset clears both. Without the current row
        Qt has no Shift+Click anchor, so the next Shift+Click extended the
        selection from the FIRST row instead of the row clicked last. Ctrl and
        Shift clicks themselves already reach Qt's selection model untouched
        (see mousePressEvent), and the drag-out of selected finished rows is
        unchanged."""
        sm, model = self.selectionModel(), self.model()
        if sm is None or model is None:
            return
        ids = set(ids or ())
        sel, cur = QItemSelection(), None
        last = model.columnCount() - 1
        for r, j in enumerate(getattr(model, "items", [])):
            jid = j.get("id")
            if jid in ids:
                sel.select(model.index(r, 0), model.index(r, last))
            if current_id is not None and jid == current_id:
                cur = model.index(r, 0)
        if not sel.isEmpty():
            sm.select(sel, QItemSelectionModel.Select | QItemSelectionModel.Rows)   # one signal
        if cur is not None:
            sm.setCurrentIndex(cur, QItemSelectionModel.NoUpdate)

    def _drag_pixmap(self, names):
        """Small rounded badge shown under the cursor during a file drag:
        the file name for one file, "N files" for several. Returns a null
        QPixmap on any failure so the drag still starts without it."""
        try:
            from PySide6.QtGui import QFontMetrics
            if not names:
                return QPixmap()
            text = names[0] if len(names) == 1 else f"{len(names)} files"
            if len(text) > 40:
                text = text[:37] + "..."
            font = QFont(self.font())
            fm = QFontMetrics(font)
            w, h = fm.horizontalAdvance(text) + 28, fm.height() + 14
            ratio = self.devicePixelRatioF()
            pm = QPixmap(int(w * ratio), int(h * ratio))
            pm.setDevicePixelRatio(ratio)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QColor(255, 255, 255, 90))
            p.setBrush(QColor(32, 36, 44, 235))
            p.drawRoundedRect(QRect(0, 0, w - 1, h - 1), 8, 8)
            p.setPen(QColor(255, 255, 255))
            p.setFont(font)
            p.drawText(QRect(0, 0, w, h), Qt.AlignCenter, text)
            p.end()
            return pm
        except Exception:
            return QPixmap()

    def _start_file_drag(self, pressed_id):
        model = self.model()
        rows = sorted({i.row() for i in self.selectionModel().selectedIndexes()})
        selected = [model.items[r].get("id") for r in rows if r < len(model.items)]
        # Press only starts on a selected row, so this carries the whole selection.
        ids = selected if pressed_id in selected else [pressed_id]
        mime = model.mimeDataForJobs(ids)
        if not mime.hasUrls():
            return
        # Windows drop fix: Explorer/Discord want plain text alongside the URLs.
        # Test switch: set VG_DRAG_NOTEXT=1 to drop the text/plain part and see
        # whether Discord's stop icon goes away (urls only, like Explorer).
        if os.environ.get("VG_DRAG_NOTEXT") != "1":
            mime.setText("\n".join(u.toLocalFile() for u in mime.urls()))
        # Preferred path on Windows: a drag that carries only the native CF_HDROP
        # file list, like Explorer's (Discord refuses drags that advertise
        # text/uri-list, which Qt always adds). Set VG_DRAG_QT=1 to force the Qt
        # drag below instead. Any failure here falls through to the Qt drag.
        if sys.platform == "win32" and os.environ.get("VG_DRAG_QT") != "1":
            native = None
            try:
                native = _native_file_drag(
                    int(self.window().winId()),
                    [u.toLocalFile() for u in mime.urls()])
            except Exception:
                native = None
            try:
                from settings import log as _nlog
                try:
                    _nadmin = bool(ctypes.windll.shell32.IsUserAnAdmin())
                except Exception:
                    _nadmin = None
                _nlog(f"file drag: native CF_HDROP drag, {len(mime.urls())} file(s), "
                      f"effect={native} (1=copied, 0=refused, -1=Esc, None=fell back to Qt), "
                      f"admin={_nadmin}")
            except Exception:
                pass
            if native is not None:
                try:
                    self.setState(QAbstractItemView.NoState)   # the drag swallowed the mouse release
                except Exception:
                    pass
                self._band_end()
                self.viewport().update()
                return
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Electron/Chromium targets (Discord, Slack, Telegram Desktop) behave
        # better when the drag carries a real image, so give it a small badge.
        pm = self._drag_pixmap([Path(u.toLocalFile()).name for u in mime.urls()])
        if not pm.isNull():
            drag.setPixmap(pm)
            ratio = pm.devicePixelRatio() or 1.0
            drag.setHotSpot(QPoint(14, int(pm.height() / ratio / 2)))
        # Copy only, on purpose: offering MoveAction lets Explorer MOVE the file
        # out of the download folder when you drop on the same drive.
        res = drag.exec(Qt.CopyAction)
        try:
            # What the drop target answered (CopyAction = accepted, IgnoreAction
            # = the stop icon), plus elevation, since an admin-run app cannot
            # drop onto a normal-user app (Windows UIPI).
            try:
                _admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                _admin = None
            from settings import log as _dlog
            _dlog(f"file drag: {len(mime.urls())} file(s), result={res}, "
                  f"admin={_admin}, text={'no' if os.environ.get('VG_DRAG_NOTEXT') == '1' else 'yes'}")
        except Exception:
            pass
        try:
            self.setState(QAbstractItemView.NoState)   # the drag swallowed the mouse release
        except Exception:
            pass
        self._band_end()
        self.viewport().update()


_LIVE_DRAG_OBJECTS = set()   # COM objects (and their callbacks) stay alive until the last Release


def _native_file_drag(hwnd, paths):
    """Windows only. Start a drag that carries exactly what Explorer carries
    for a file drag: the native CF_HDROP file list and nothing else. Qt cannot
    do this, because it only makes CF_HDROP from text/uri-list, and Discord
    refuses any drag that advertises text/uri-list (drag test bench, test 3).
    Built from a small hand-made IDataObject and IDropSource run through the
    standard OLE DoDragDrop, with no shell helper calls.
    Returns None when it could not start (the caller then falls back to Qt's
    QDrag), otherwise the final drop effect: 1 = copied, 0 = refused by the
    target, -1 = cancelled with Esc. hwnd is accepted for the caller's sake
    and is not needed here.
    Copy only, on purpose: no Move, so a drop can never take the file out of
    the download folder. Runs on the GUI thread, which Qt already put in OLE."""
    if sys.platform != "win32" or not paths:
        return None
    import struct
    import uuid
    c_void_p, HR, WINF = ctypes.c_void_p, ctypes.c_long, ctypes.WINFUNCTYPE

    def _hr(v):                      # unsigned HRESULT constant to the signed value ctypes returns
        return v - (1 << 32) if v & 0x80000000 else v
    E_NOINTERFACE, E_NOTIMPL, E_FAIL = _hr(0x80004002), _hr(0x80004001), _hr(0x80004005)
    DV_E_FORMATETC, DV_E_TYMED = _hr(0x80040064), _hr(0x80040069)
    ADVISE_NOTSUPPORTED = _hr(0x80040003)
    DATA_S_SAMEFORMATETC = 0x40130
    DRAGDROP_S_DROP, DRAGDROP_S_CANCEL, DRAGDROP_S_USEDEFAULTCURSORS = 0x40100, 0x40101, 0x40102
    CF_HDROP, TYMED_HGLOBAL, DVASPECT_CONTENT = 15, 1, 1
    IID_UNK = uuid.UUID("00000000-0000-0000-c000-000000000046").bytes_le
    IID_DATAOBJ = uuid.UUID("0000010e-0000-0000-c000-000000000046").bytes_le
    IID_DROPSRC = uuid.UUID("00000121-0000-0000-c000-000000000046").bytes_le

    class _FORMATETC(ctypes.Structure):
        _fields_ = [("cfFormat", ctypes.c_ushort), ("ptd", c_void_p),
                    ("dwAspect", ctypes.c_uint32), ("lindex", ctypes.c_int32),
                    ("tymed", ctypes.c_uint32)]

    class _STGMEDIUM(ctypes.Structure):
        _fields_ = [("tymed", ctypes.c_uint32), ("hGlobal", c_void_p),
                    ("pUnkForRelease", c_void_p)]

    class _Obj(ctypes.Structure):    # a COM object is just a pointer to its vtable
        _fields_ = [("vtbl", c_void_p)]

    def _make_vtbl(names):
        return type("_Vtbl", (ctypes.Structure,), {"_fields_": [(n, c_void_p) for n in names]})()

    FE, SM = ctypes.POINTER(_FORMATETC), ctypes.POINTER(_STGMEDIUM)
    PPV = ctypes.POINTER(c_void_p)

    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32 = ctypes.WinDLL("user32")
    ole32.OleInitialize.argtypes = [c_void_p]
    ole32.OleInitialize.restype = HR
    ole32.OleUninitialize.argtypes = []
    ole32.OleUninitialize.restype = None
    ole32.DoDragDrop.argtypes = [c_void_p, c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    ole32.DoDragDrop.restype = HR
    shell32.SHCreateStdEnumFmtEtc.argtypes = [ctypes.c_uint32, FE, PPV]
    shell32.SHCreateStdEnumFmtEtc.restype = HR
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint32, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = c_void_p
    kernel32.GlobalLock.argtypes = [c_void_p]
    kernel32.GlobalLock.restype = c_void_p
    kernel32.GlobalUnlock.argtypes = [c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [c_void_p]
    kernel32.GlobalFree.restype = c_void_p
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = ctypes.c_int

    # DROPFILES header (pFiles, pt.x, pt.y, fNC, fWide) + UTF-16 paths, double-null ended.
    blob = struct.pack("<IiiII", 20, 0, 0, 0, 1) + b"".join(
        os.path.normpath(q).encode("utf-16-le") + b"\x00\x00" for q in paths) + b"\x00\x00"

    class _Holder:
        pass
    h = _Holder()
    h.refs = 1

    def _drop_ref():
        h.refs -= 1
        if h.refs <= 0:
            _LIVE_DRAG_OBJECTS.discard(h)
        return max(h.refs, 0)

    # ---- IDataObject ---------------------------------------------------
    def d_qi(this, riid, ppv):
        try:
            if ctypes.string_at(riid, 16) in (IID_UNK, IID_DATAOBJ):
                ppv[0] = ctypes.addressof(h.dobj)
                h.refs += 1
                return 0
            ppv[0] = None
        except Exception:
            pass
        return E_NOINTERFACE

    def d_addref(this):
        h.refs += 1
        return h.refs

    def d_release(this):
        return _drop_ref()

    def _check(fe):
        if fe.cfFormat != CF_HDROP or not (fe.dwAspect & DVASPECT_CONTENT):
            return DV_E_FORMATETC
        if not (fe.tymed & TYMED_HGLOBAL):
            return DV_E_TYMED
        return 0

    def d_getdata(this, pfe, pmed):
        try:
            bad = _check(pfe.contents)
            if bad:
                return bad
            hg = kernel32.GlobalAlloc(0x2, len(blob))      # GMEM_MOVEABLE
            if not hg:
                return E_FAIL
            ptr = kernel32.GlobalLock(hg)
            if not ptr:
                kernel32.GlobalFree(hg)
                return E_FAIL
            ctypes.memmove(ptr, blob, len(blob))
            kernel32.GlobalUnlock(hg)
            m = pmed.contents
            m.tymed = TYMED_HGLOBAL
            m.hGlobal = hg
            m.pUnkForRelease = None
            return 0
        except Exception:
            return E_FAIL

    def d_getdatahere(this, pfe, pmed):
        return E_NOTIMPL

    def d_query(this, pfe):
        try:
            return _check(pfe.contents)
        except Exception:
            return E_FAIL

    def d_canonical(this, pin, pout):
        try:
            if pout:
                pout.contents.ptd = None
        except Exception:
            pass
        return DATA_S_SAMEFORMATETC

    def d_setdata(this, pfe, pmed, release):
        return E_NOTIMPL

    def d_enum(this, direction, ppenum):
        try:
            if direction != 1:                       # DATADIR_GET only
                return E_NOTIMPL
            fe = _FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
            pen = c_void_p()
            hr = shell32.SHCreateStdEnumFmtEtc(1, ctypes.byref(fe), ctypes.byref(pen))
            if hr >= 0:
                ppenum[0] = pen.value
            return hr
        except Exception:
            return E_FAIL

    def d_dadvise(this, pfe, flags, padv, pconn):
        return ADVISE_NOTSUPPORTED

    def d_dunadvise(this, conn):
        return ADVISE_NOTSUPPORTED

    def d_enumdadvise(this, ppenum):
        return ADVISE_NOTSUPPORTED

    # ---- IDropSource ---------------------------------------------------
    def s_qi(this, riid, ppv):
        try:
            if ctypes.string_at(riid, 16) in (IID_UNK, IID_DROPSRC):
                ppv[0] = ctypes.addressof(h.dsrc)
                return 0
            ppv[0] = None
        except Exception:
            pass
        return E_NOINTERFACE

    def s_addref(this):
        return 2

    def s_release(this):
        return 1

    def s_continue(this, esc, keys):
        if esc:
            return DRAGDROP_S_CANCEL
        if not (keys & 1):                           # MK_LBUTTON released: drop
            return DRAGDROP_S_DROP
        return 0

    def s_feedback(this, effect):
        return DRAGDROP_S_USEDEFAULTCURSORS

    d_protos = [
        (WINF(HR, c_void_p, c_void_p, PPV), d_qi),
        (WINF(ctypes.c_ulong, c_void_p), d_addref),
        (WINF(ctypes.c_ulong, c_void_p), d_release),
        (WINF(HR, c_void_p, FE, SM), d_getdata),
        (WINF(HR, c_void_p, FE, SM), d_getdatahere),
        (WINF(HR, c_void_p, FE), d_query),
        (WINF(HR, c_void_p, FE, FE), d_canonical),
        (WINF(HR, c_void_p, FE, SM, ctypes.c_int), d_setdata),
        (WINF(HR, c_void_p, ctypes.c_uint32, PPV), d_enum),
        (WINF(HR, c_void_p, FE, ctypes.c_uint32, c_void_p, ctypes.POINTER(ctypes.c_uint32)), d_dadvise),
        (WINF(HR, c_void_p, ctypes.c_uint32), d_dunadvise),
        (WINF(HR, c_void_p, PPV), d_enumdadvise),
    ]
    s_protos = [
        (WINF(HR, c_void_p, c_void_p, PPV), s_qi),
        (WINF(ctypes.c_ulong, c_void_p), s_addref),
        (WINF(ctypes.c_ulong, c_void_p), s_release),
        (WINF(HR, c_void_p, ctypes.c_int, ctypes.c_uint32), s_continue),
        (WINF(HR, c_void_p, ctypes.c_uint32), s_feedback),
    ]
    d_names = ["QueryInterface", "AddRef", "Release", "GetData", "GetDataHere",
               "QueryGetData", "GetCanonicalFormatEtc", "SetData", "EnumFormatEtc",
               "DAdvise", "DUnadvise", "EnumDAdvise"]
    s_names = ["QueryInterface", "AddRef", "Release", "QueryContinueDrag", "GiveFeedback"]
    h.callbacks = [proto(fn) for proto, fn in d_protos + s_protos]   # keep alive
    h.dvt, h.svt = _make_vtbl(d_names), _make_vtbl(s_names)
    for i, n in enumerate(d_names):
        setattr(h.dvt, n, ctypes.cast(h.callbacks[i], c_void_p).value)
    for i, n in enumerate(s_names):
        setattr(h.svt, n, ctypes.cast(h.callbacks[len(d_names) + i], c_void_p).value)
    h.dobj, h.dsrc = _Obj(ctypes.addressof(h.dvt)), _Obj(ctypes.addressof(h.svt))

    hr = ole32.OleInitialize(None)           # S_OK / S_FALSE both count; balanced below
    if hr < 0:
        return None
    _LIVE_DRAG_OBJECTS.add(h)
    try:
        user32.ReleaseCapture()              # let OLE take the mouse for the drag
        effect = ctypes.c_uint32(0)
        hr = ole32.DoDragDrop(ctypes.addressof(h.dobj), ctypes.addressof(h.dsrc),
                              1, ctypes.byref(effect))               # 1 = DROPEFFECT_COPY
        if hr < 0:
            return None
        if hr == DRAGDROP_S_CANCEL:
            return -1
        return int(effect.value)
    finally:
        _drop_ref()                          # our own reference; a target may still hold one
        try:
            ole32.OleUninitialize()
        except Exception:
            pass


def _playlist_dir_for(job):
    """Folder to open for a legacy whole-playlist job whose own file path
    does not exist, or None for any other job.

    Before Session D, download_playlist=True meant one job that walks a whole
    playlist. Session D queues playlists as one job per item
    (download_playlist=False, playlist_id set). download_playlist=True now
    appears only on pre-Session-D jobs and on jobs made with "Legacy
    whole-playlist mode" or by API callers - all share the batch-1 layout."""
    if not job.get("download_playlist") or job.get("playlist_id"):
        return None
    cat_dir = _path_for(job).parent
    name = job.get("playlist_dir")
    if name and (cat_dir / name).is_dir():
        return cat_dir / name
    return cat_dir if cat_dir.is_dir() else None


def _copy_files_to_clipboard(jobs):
    """Put finished files on the clipboard so Ctrl+V pastes the actual file
    into Explorer/Discord/Slack. Qt maps text/uri-list to CF_HDROP on
    Windows itself. Returns the number of files copied."""
    urls = []
    for j in jobs:
        if j.get("status") != "done":
            continue
        try:
            p = _path_for(j)
            if p.exists():
                urls.append(QUrl.fromLocalFile(str(p)))
        except Exception:
            continue
    if not urls:
        return 0
    mime = QMimeData()
    mime.setUrls(urls)
    mime.setText("\n".join(u.toLocalFile() for u in urls))
    QApplication.clipboard().setMimeData(mime)
    return len(urls)


def _progress_pct(job):
    """Numeric progress straight from the job dict (no string parsing):
    conversion progress while converting, otherwise size_done/size_total
    with a zero-total guard. Queued/paused/done jobs report 0."""
    if job.get("phase") == "converting":
        return float(job.get("conversion_progress") or 0)
    total = job.get("size_total") or 0
    return (job.get("size_done") or 0) / total * 100 if total else 0.0


def _ffmpeg_dir():
    """Folder with the app's own ffmpeg, if the backend module that knows it is
    already loaded. Never imports anything new (server.py runs as __main__)."""
    for mod in ("engines", "downloader", "jobs", "api"):
        fn = getattr(sys.modules.get(mod), "find_ffmpeg_dir", None)
        if fn:
            try:
                d = fn()
                if d:
                    return str(d)
            except Exception:
                pass
    return None


def _detail_info(j):
    """Everything the detail pane shows, as plain strings, from one job dict."""
    from urllib.parse import urlparse
    status = j.get("status") or ""
    converting = j.get("phase") == "converting"
    total, done = j.get("size_total") or 0, j.get("size_done") or 0
    pct = 100.0 if (status == "done" and not converting) else _progress_pct(j)
    eta = _eta_seconds(j)
    try:
        path = _path_for(j)
    except Exception:
        path = None
    ext = (path.suffix if path else Path(str(j.get("filename") or "")).suffix).lower()
    if converting:
        progress_text = f"Converting to {j.get('converting_fmt', '')}: {j.get('conversion_progress', 0)}%"
    elif total:
        progress_text = f"{fmt_bytes(done)} / {fmt_bytes(total)} ({pct:.0f}%)"
    else:
        progress_text = "Done" if status == "done" else status
    speed = j.get("speed") or ""
    remaining = _fmt_duration(eta) if eta is not None else ""
    downloaded = fmt_bytes(done) if done else ""
    note, note_color = [], ""
    retry = _retry_text(j)
    if retry:
        note.append(f"Auto-retry: {retry}")
        note_color = gui_palette.WARN
    if j.get("error"):
        note.append(str(j["error"])[:300])
        note_color = gui_palette.DANGER
    host = ""
    try:
        host = urlparse(str(j.get("url") or "")).hostname or ""
    except Exception:
        pass
    general = [
        ("URL", j.get("url")), ("Saved as", j.get("filename")), ("Category", j.get("category")),
        ("Size", fmt_bytes(total) if total else ""), ("Created", fmt_ts(j.get("created_ts"))),
        ("Status", f"{status} (converting)" if converting else status),
        ("Saved to", str(path) if path else ""), ("Source", j.get("source_site")),
    ]
    if j.get("completed_ts"):
        general.append(("Completed", fmt_ts(j.get("completed_ts"))))
    progress = [
        ("Progress", f"{pct:.0f}%"),
        ("Downloaded", f"{fmt_bytes(done)} of {fmt_bytes(total)}" if total else downloaded),
        ("Speed", speed), ("Time remaining", remaining),
    ]
    if converting:
        progress.append(("Conversion", f"to {j.get('converting_fmt', '')}, {j.get('conversion_progress', 0)}%"))
    if j.get("completed_ts"):
        progress.append(("Completed", fmt_ts(j.get("completed_ts"))))
    connections = [("Source site", j.get("source_site")), ("Host", host)]
    # Only fields the job actually carries are listed (no placeholders).
    for label, key in (("Engine", "engine"), ("Connections", "connections"),
                       ("Segments", "segments"), ("Retries", "retry_count")):
        if j.get(key) not in (None, "", 0):
            connections.append((label, j.get(key)))
    if retry:
        connections.append(("Next retry", retry))
    return {
        "id": j.get("id"), "title": str(j.get("filename") or ""), "pct": pct,
        "progress_text": progress_text, "speed": speed, "remaining": remaining,
        "downloaded": downloaded, "note": "\n".join(note), "note_color": note_color,
        "can_pause": status == "downloading",
        "can_resume": status in ("paused", "stopped", "error"),
        "can_stop": status in ("downloading", "paused", "queued"),
        "done": status == "done",
        "path": str(path) if path else "",
        "path_exists": bool(status == "done" and path and path.exists()),
        "is_video": ext in VIDEO_EXT, "thumb_label": ext.lstrip(".").upper() or "FILE",
        "general": general, "progress": progress, "connections": connections,
    }


_PROGRESS_EASE = QEasingCurve(QEasingCurve.OutQuart)


class ProgressDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 12.2: per-row interpolation toward the newest value over 150 ms.
        self._shown = {}  # job id -> [from_value, to_value, QElapsedTimer]
        self.animations_enabled = True  # 12.5: refreshed by MainWindow

    def _animated_pct(self, row, target):
        entry = self._shown.get(row)
        if entry is None:
            if len(self._shown) > 2000:
                self._shown.clear()  # long sessions: bound the cache
            timer = QElapsedTimer()
            timer.start()
            self._shown[row] = [target, target, timer]
            return target
        old, current, timer = entry
        if target != current:
            entry[0], entry[1] = current, target
            timer.restart()
        if not self.animations_enabled:
            entry[0] = target
            return target
        f = min(1.0, timer.elapsed() / 150.0)
        f = _PROGRESS_EASE.valueForProgress(f)  # OutQuart across the 150 ms window
        return entry[0] + (entry[1] - entry[0]) * f

    def paint(self, painter, option, index):
        if index.column() != 2:
            super().paint(painter, option, index)
            return
        job = index.model().items[index.row()]
        converting = job.get("phase") == "converting"
        # Keyed by job id, not row index: rows shift when jobs are added or
        # filtered, and a row-keyed cache animated the wrong job's bar.
        pct = self._animated_pct(job.get("id", index.row()), _progress_pct(job))
        painter.save()
        rect = option.rect.adjusted(6, 14, -6, -14)
        if rect.height() < 4:
            # Very short rows (Compact preset): keep the bar drawable and
            # centred in the row instead of negative-height.
            rect = QRect(option.rect.left() + 6,
                         option.rect.top() + (option.rect.height() - 4) // 2,
                         option.rect.width() - 12, 4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(gui_palette.TRACK))
        painter.drawRoundedRect(rect, 5, 5)
        fill = int(rect.width() * max(0, min(100, pct)) / 100)
        if fill > 0:
            painter.setBrush(QColor(gui_palette.WARN if converting else gui_palette.ACCENT))
            painter.drawRoundedRect(
                rect.adjusted(0, 0, -(rect.width() - fill), 0), 5, 5
            )
        painter.setPen(QColor(gui_palette.TEXT))
        painter.drawText(option.rect.adjusted(rect.width() + 14, 0, 0, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, f"{pct:.0f}%")
        painter.restore()
