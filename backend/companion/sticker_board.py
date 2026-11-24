"""Sticker board: drag, wheel to scale, R to rotate, Delete to remove. The layout is saved on close."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QDialog, QFileDialog, QGraphicsItem, QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsView, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QVBoxLayout)

from .stickers import StickerError

STYLE = ("QDialog{background:#171522;color:#eee9ff;} QLabel{color:#b9b2d9;} "
         "QPushButton,QListWidget{background:#242139;color:#eee9ff;border:1px solid #514a78;border-radius:7px;padding:6px;} "
         "QPushButton:hover{background:#332e52;}")


class _BoardView(QGraphicsView):
    def wheelEvent(self, event):
        item = self.scene().itemAt(self.mapToScene(event.position().toPoint()), self.transform())
        if item is not None and item.isSelected():
            factor = 1.1 if event.angleDelta().y() > 0 else 0.9
            item.setScale(max(0.25, min(4.0, item.scale() * factor)))
            event.accept()
            return
        super().wheelEvent(event)


class StickerBoardDialog(QDialog):
    def __init__(self, core, manager, parent=None):
        super().__init__(parent)
        self.core, self.store, self.manager = core, core.store, manager
        self.setWindowTitle("Video Grabber \u2022 Sticker Board")
        self.resize(780, 560)
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Drag stickers around. Click one, then use the wheel to resize, R to rotate, Delete to remove."))
        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 640, 380)
        self.view = _BoardView(self.scene)
        self.view.setStyleSheet("QGraphicsView{background:#242139;border:1px solid #514a78;border-radius:8px;}")
        root.addWidget(self.view, 1)

        self.library = QListWidget()
        self.library.setIconSize(QSize(40, 40))
        self.library.setMaximumHeight(110)
        self.library.itemDoubleClicked.connect(lambda _item: self._add())
        root.addWidget(self.library)

        row = QHBoxLayout()
        for label, callback in (("Add to board", self._add), ("Import image\u2026", self._import),
                                ("Delete from library", self._delete_from_library), ("Save layout", self._save),
                                ("Export pack\u2026", self._export), ("Import pack\u2026", self._import_pack)):
            b = QPushButton(label)
            b.clicked.connect(callback)
            row.addWidget(b)
        root.addLayout(row)

        self._load_library()
        self._restore_layout()
        QShortcut(QKeySequence("R"), self, activated=self._rotate)
        QShortcut(QKeySequence("Delete"), self, activated=self._remove_selected)

    # ---- library --------------------------------------------------------------
    def _items(self):
        return self.manager.library(self.core.is_unlocked)

    def _load_library(self):
        self.library.clear()
        for sticker in self._items():
            pm = QPixmap(sticker["path"])
            item = QListWidgetItem(QIcon(pm) if not pm.isNull() else QIcon(), sticker["name"])
            item.setData(Qt.UserRole, sticker)
            self.library.addItem(item)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import sticker", "", "Images (*.png *.gif *.webp)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Sticker name", "Name:")
        if not ok:
            return
        try:
            self.manager.import_image(path, name or None, pixelate=True)
            self._load_library()
        except StickerError as exc:
            QMessageBox.warning(self, "Sticker import", str(exc))

    def _delete_from_library(self):
        item = self.library.currentItem()
        sticker = item.data(Qt.UserRole) if item else None
        if not sticker or not sticker.get("custom"):
            QMessageBox.information(self, "Sticker", "Built-in stickers cannot be deleted.")
            return
        if QMessageBox.question(self, "Delete sticker", "Delete \"%s\" from your library?" % sticker["name"]) != QMessageBox.Yes:
            return
        for graphic in list(self.scene.items()):
            if graphic.data(0) == sticker["id"]:
                self.scene.removeItem(graphic)
        self.manager.delete(sticker["id"])
        self._load_library()

    # ---- board ------------------------------------------------------------------
    def _place(self, sticker_id, path, x, y, scale=1.0, rotation=0.0):
        pix = QPixmap(path)
        if pix.isNull():
            return None
        graphic = QGraphicsPixmapItem(pix)
        graphic.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        graphic.setTransformationMode(Qt.FastTransformation)
        graphic.setTransformOriginPoint(pix.width() / 2, pix.height() / 2)
        graphic.setPos(x, y)
        graphic.setScale(max(0.25, min(4.0, float(scale))))
        graphic.setRotation(float(rotation))
        graphic.setData(0, sticker_id)
        self.scene.addItem(graphic)
        return graphic

    def _add(self):
        item = self.library.currentItem()
        if not item:
            return
        sticker = item.data(Qt.UserRole)
        n = len([g for g in self.scene.items() if g.data(0)])
        graphic = self._place(sticker["id"], sticker["path"], 40 + (n % 6) * 80, 40 + (n % 4) * 80)
        if graphic is not None:
            self.scene.clearSelection()
            graphic.setSelected(True)

    def _remove_selected(self):
        for graphic in self.scene.selectedItems():
            self.scene.removeItem(graphic)

    def _rotate(self):
        for graphic in self.scene.selectedItems():
            graphic.setRotation((graphic.rotation() + 15) % 360)

    def _save(self):
        layout = []
        for graphic in self.scene.items():
            sid = graphic.data(0)
            if sid:
                pos = graphic.pos()
                layout.append({"sticker_id": sid, "x": pos.x(), "y": pos.y(),
                               "scale": graphic.scale(), "rotation": graphic.rotation()})
        self.store.save_sticker_layout(layout)

    def _restore_layout(self):
        by_id = {s["id"]: s for s in self._items()}
        for entry in self.store.load_sticker_layout():
            sticker = by_id.get(entry.get("sticker_id"))
            if sticker:
                self._place(sticker["id"], sticker["path"], float(entry.get("x", 0)), float(entry.get("y", 0)),
                            entry.get("scale", 1), entry.get("rotation", 0))

    def done(self, result):
        self._save()
        super().done(result)

    # ---- packs ----------------------------------------------------------------------
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export sticker pack", "stickers.zip", "ZIP files (*.zip)")
        if not path:
            return
        try:
            self.manager.export_pack(path)
            QMessageBox.information(self, "Sticker pack", "Sticker pack exported (your own stickers only).")
        except Exception as exc:
            QMessageBox.warning(self, "Sticker pack", str(exc))

    def _import_pack(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import sticker pack", "", "ZIP files (*.zip)")
        if not path:
            return
        try:
            added = self.manager.import_pack(path)
            self._load_library()
            QMessageBox.information(self, "Sticker pack", "Imported %d sticker(s)." % len(added))
        except Exception as exc:
            QMessageBox.warning(self, "Sticker pack", str(exc))
