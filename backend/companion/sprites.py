"""Qt sprite loading. Sheets and scaled frames are cached, so animating costs almost nothing."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap

from .animation import sheet_cell
from .store import load_registry

ASSETS = Path(__file__).resolve().parent / "assets"


class SpriteBank:
    def __init__(self, assets_dir=ASSETS):
        self.assets = Path(assets_dir)
        self._sheets = {}
        self._frames = {}

    # ---- lookup --------------------------------------------------------
    @staticmethod
    def entry_for(species_id, stage):
        """The species entry, or the egg entry while the pet is still an egg."""
        reg = load_registry()
        if stage == "egg" and reg.get("egg"):
            return reg["egg"]
        for entry in reg.get("species", []):
            if entry.get("id") == species_id:
                stage_sheets = entry.get("stage_sheets") or {}
                if stage in stage_sheets:
                    merged = dict(entry)
                    merged.update(stage_sheets[stage])
                    return merged
                return entry
        return (reg.get("species") or [{}])[0]

    @staticmethod
    def frame_size():
        return int(load_registry().get("frame_size", 64))

    # ---- pixmaps -----------------------------------------------------------
    def sheet(self, name):
        pm = self._sheets.get(name)
        if pm is None:
            pm = QPixmap(str(self.assets / name))
            self._sheets[name] = pm
        return pm

    def frame(self, entry, index, scale=1):
        """One frame of an entry's sheet, scaled by a whole number with no smoothing."""
        key = (entry.get("sheet"), int(index), int(scale))
        pm = self._frames.get(key)
        if pm is None:
            size = self.frame_size()
            sheet = self.sheet(entry.get("sheet", "puppy.png"))
            if sheet.isNull():
                pm = QPixmap(size * scale, size * scale)
                pm.fill(Qt.transparent)
            else:
                x, y = sheet_cell(int(index), int(entry.get("columns", 8)), size)
                pm = sheet.copy(x, y, size, size)
                if scale != 1:
                    pm = pm.scaled(size * scale, size * scale, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            self._frames[key] = pm
        return pm

    def canvas(self, entry, index, dy, scale, pad=3):
        """A frame placed on a transparent canvas with a vertical bob (dy is in sprite pixels)."""
        size = self.frame_size() * scale
        out = QPixmap(size, size + pad * scale)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.drawPixmap(0, (pad - dy) * scale, self.frame(entry, index, scale))
        p.end()
        return out
