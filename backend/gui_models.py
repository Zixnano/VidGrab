"""Table model for the download list."""

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, Qt, QUrl
from PySide6.QtGui import QColor

import gui_palette
from gui_common import _eta_seconds, _fmt_duration, _retry_text, fmt_bytes, fmt_ts
from gui_sidebar import _site_icon, _status_icon
from gui_common import _path_for


# Which table columns each job field feeds (see DownloadModel.data). Used by
# update_items() so a progress tick repaints only the columns that changed.
_FIELD_COLS = {
    "filename": (0,), "status": (0, 2, 4, 6, 7), "size_total": (1, 2, 7),
    "size_done": (2, 7), "phase": (2, 4), "conversion_progress": (2,),
    "converting_fmt": (4,), "speed": (3, 7), "category": (5,),
    "completed_ts": (6,), "created_ts": (6,),
    "source_site": (8,), "retry_after": (4,), "retry_count": (4,),
    "error": (4,),
}


def _changed_cols(a, b):
    """(first, last) column span touched by differences between two job
    dicts, or None when no displayed field differs."""
    cols = set()
    for k, mapped in _FIELD_COLS.items():
        if a.get(k) != b.get(k):
            cols.update(mapped)
    return (min(cols), max(cols)) if cols else None


class DownloadModel(QAbstractTableModel):
    HEADERS = ["Name", "Size", "Progress", "Speed", "Status", "Category", "Completed", "ETA", "Source"]

    def __init__(self, parent_window=None):
        super().__init__()
        self.items = []
        self.parent_window = parent_window

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def update_items(self, items):
        """Incremental refresh. Same rows in the same order -> swap the data
        and emit dataChanged for only the rows whose values changed (keeps
        scroll position, selection and hover; no full repaint). Anything
        else (filter, sort, add/remove) falls back to a full reset."""
        old = self.items
        if (len(old) != len(items)
                or any(a.get("id") != b.get("id") for a, b in zip(old, items))):
            self.set_items(items)
            return
        self.items = items
        last_col = self.columnCount() - 1
        for r, (a, b) in enumerate(zip(old, items)):
            if a is b or a == b:
                continue
            span = _changed_cols(a, b)
            if span:
                self.dataChanged.emit(self.index(r, span[0]), self.index(r, span[1]))
        if items:
            self.headerDataChanged.emit(Qt.Horizontal, 0, last_col)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        j = self.items[index.row()]
        col = index.column()
        if role == Qt.DecorationRole and col == 0:
            return _status_icon(j.get("status"))
        if role == Qt.DecorationRole and col == 8:
            return _site_icon(j.get("source_site"))
        if role == Qt.ToolTipRole:
            if col == 0:
                return "\n".join(x for x in (j.get("filename"), j.get("source_site"),
                                             j.get("url")) if x)
            if col == 4 and j.get("error"):
                return str(j["error"])[:600]
            return None
        if role == Qt.BackgroundRole:
            pw = self.parent_window
            fade = getattr(pw, "_recently_done", {}).get(j.get("id"), 0) if pw else 0
            if fade > 0:
                flash = QColor(gui_palette.SEL)
                base = QColor(gui_palette.PANEL)
                t = max(0.0, min(1.0, float(fade)))
                return QColor(
                    int(base.red() + (flash.red() - base.red()) * t),
                    int(base.green() + (flash.green() - base.green()) * t),
                    int(base.blue() + (flash.blue() - base.blue()) * t),
                )
        if role == Qt.DisplayRole:
            pct = "—"
            if j.get("phase") == "converting":
                pct = f"conv {j.get('conversion_progress', 0)}%"
            elif j.get("size_total"):
                pct = f"{(j.get('size_done', 0) / j['size_total'] * 100):.0f}%"
            elif j["status"] == "done":
                pct = "100%"
            status = (f"converting → {j.get('converting_fmt', '')}" if j.get("phase") == "converting" else j["status"])
            retry = _retry_text(j)
            if retry:
                status = retry
            if col == 7:
                eta = _eta_seconds(j)
                return _fmt_duration(eta) if eta is not None else ""
            if col == 8:
                return j.get("source_site") or ""
            if col == 6:
                # Pre-column jobs: fall back to created_ts when done so the
                # column is still useful for sorting.
                ts = j.get("completed_ts")
                if not ts and j.get("status") == "done":
                    ts = j.get("created_ts")
                return fmt_ts(ts)
            return [
                j["filename"],
                fmt_bytes(j.get("size_total")),
                pct,
                j.get("speed") or "",
                status,
                j["category"],
            ][col]
        if role == Qt.ForegroundRole:
            if col == 4:
                if _retry_text(j):
                    return QColor(gui_palette.WARN)
                if j.get("phase") == "converting":
                    return QColor(gui_palette.WARN)
                s = j["status"]
                if s == "done":
                    return QColor(gui_palette.ACCENT)
                if s in ("error", "stopped"):
                    return QColor(gui_palette.DANGER)
                if s == "paused":
                    return QColor(gui_palette.WARN)
                if s == "skipped":
                    return QColor(gui_palette._PAL["text_dim"])
            if col in (1, 3, 6, 7, 8):
                return QColor(gui_palette.SOFT)
            if col == 5:
                return QColor(gui_palette.SOFT)
            return QColor(gui_palette.TEXT)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            label = self.HEADERS[section]
            pw = self.parent_window
            if pw is not None and getattr(pw, "_sort_column", None) == section:
                label += " ▲" if pw._sort_ascending else " ▼"
            return label
        return None

    def flags(self, index):
        base = super().flags(index)
        # Only finished jobs can be dragged out (to Discord/Explorer). Qt
        # skips non-draggable rows in a mixed selection, so dragging a mix
        # of done and pending rows carries just the done ones.
        if (index.isValid() and index.row() < len(self.items)
                and self.items[index.row()].get("status") == "done"):
            return base | Qt.ItemIsDragEnabled
        return base

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes if i.isValid()})
        urls = []
        for r in rows:
            j = self.items[r]
            if j["status"] != "done":
                continue
            path = _path_for(j)
            if path.exists():
                urls.append(QUrl.fromLocalFile(str(path)))
        if not urls:
            return QMimeData()  # empty drag payload, never None
        mime = QMimeData()
        mime.setUrls(urls)
        return mime

    def mimeDataForJobs(self, ids):
        """File-drag payload for the given job ids (in the order given). The
        table calls this from _start_file_drag. Ids, not rows, because the
        model refreshes every second and rows can move before a drag starts.
        Only finished jobs whose file still exists are carried."""
        by_id = {j.get("id"): j for j in self.items}
        urls, seen = [], set()
        for jid in ids:
            j = by_id.get(jid)
            if j is None or j.get("status") != "done":
                continue
            try:
                path = _path_for(j)
                if not path.exists() or str(path) in seen:
                    continue
            except (OSError, ValueError, KeyError):
                continue
            seen.add(str(path))
            urls.append(QUrl.fromLocalFile(str(path)))
        mime = QMimeData()
        if urls:
            mime.setUrls(urls)
        return mime
