"""Local sticker import/export. Images are decoded and re-encoded as metadata-free PNGs.

Safety: 8 MB file cap, 4096 px side cap checked before decoding (a small PNG can describe a huge
bitmap), 200 sticker cap, zip pack size and path checks. Everything stays on this PC.
"""
from __future__ import annotations

import json
import re
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QImageReader

MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_PACK_BYTES = 32 * 1024 * 1024
MAX_SIDE_PX = 4096
MAX_STICKERS = 200
ALLOWED = {"png", "gif", "webp"}

BUILTIN_DIR = Path(__file__).resolve().parent / "assets" / "stickers_builtin"
# (id, label, unlock key or None). Locked ones appear after the matching achievement.
BUILTIN_STICKERS = (
    ("puppy_idle", "Mochi", None),
    ("puppy_happy", "Giggles", None),
    ("puppy_sleeping", "Nap time", None),
    ("puppy_curious", "Hmm?", None),
    ("puppy_sad", "Aww", None),
    ("puppy_watching", "Watching", None),
    ("puppy_celebrating", "Party pup", "sticker_first_fetch"),
    ("puppy_wave", "Hello!", "sticker_arcade"),
)


class StickerError(ValueError):
    pass


class StickerManager:
    def __init__(self, store, data_dir):
        self.store = store
        self.root = Path(data_dir) / "stickers"
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- library ---------------------------------------------------------
    def builtin_items(self, is_unlocked=lambda key: True):
        items = []
        for sid, label, unlock in BUILTIN_STICKERS:
            path = BUILTIN_DIR / (sid + ".png")
            if path.is_file() and (unlock is None or is_unlocked(unlock)):
                items.append({"id": "builtin:" + sid, "name": label, "path": str(path), "custom": False})
        return items

    def custom_items(self):
        items = []
        for row in self.store.list_stickers():
            path = self.root / Path(row["filename"]).name
            items.append({"id": row["id"], "name": row["manifest"].get("name", "Sticker"),
                          "path": str(path), "custom": True})
        return items

    def library(self, is_unlocked=lambda key: True):
        return self.builtin_items(is_unlocked) + self.custom_items()

    def path_for(self, sticker_id, is_unlocked=lambda key: True):
        for item in self.library(is_unlocked):
            if item["id"] == sticker_id:
                return item["path"]
        return None

    # ---- import ------------------------------------------------------------
    def import_image(self, source_path, name=None, pixelate=True):
        source = Path(source_path)
        if source.suffix.lower().lstrip(".") not in ALLOWED:
            raise StickerError("Choose a PNG, GIF, or WebP image.")
        if not source.is_file() or source.stat().st_size > MAX_IMPORT_BYTES:
            raise StickerError("Image is missing or exceeds the 8 MB limit.")
        if len(self.store.list_stickers()) >= MAX_STICKERS:
            raise StickerError("Sticker limit reached.")
        reader = QImageReader(str(source))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > MAX_SIDE_PX or size.height() > MAX_SIDE_PX):
            raise StickerError("Image is larger than %d pixels on a side." % MAX_SIDE_PX)
        image = reader.read()                      # the first frame of a GIF or WebP
        if image.isNull() or image.width() < 1 or image.height() < 1:
            raise StickerError("That image could not be decoded.")
        side = min(image.width(), image.height())
        image = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
        if pixelate:
            # nearest-neighbour reduction makes deliberate chunky pixels, then scales back up
            tiny = image.scaled(24, 24, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            image = tiny.scaled(128, 128, Qt.IgnoreAspectRatio, Qt.FastTransformation)
        else:
            image = image.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        image = image.convertToFormat(QImage.Format_ARGB32)
        sticker_id = uuid.uuid4().hex[:12]
        filename = sticker_id + ".png"
        if not image.save(str(self.root / filename), "PNG"):
            raise StickerError("Could not save the sticker.")
        safe_name = re.sub(r"[^\w -]", "", name or source.stem).strip()[:40] or "Custom sticker"
        meta = {"name": safe_name, "width": image.width(), "height": image.height(), "custom": True}
        self.store.add_sticker(sticker_id, filename, meta)
        return {"id": sticker_id, "filename": filename, "manifest": meta}

    def delete(self, sticker_id):
        filename = self.store.delete_sticker(sticker_id)
        if not filename:
            return False
        path = (self.root / Path(filename).name).resolve()
        if path.parent == self.root.resolve() and path.exists():
            path.unlink()
        return True

    # ---- packs ---------------------------------------------------------------
    def export_pack(self, destination):
        rows = self.store.list_stickers()
        manifest = {"format": "video-grabber-stickers", "version": 1, "stickers": []}
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
            for row in rows:
                path = (self.root / Path(row["filename"]).name).resolve()
                if path.parent != self.root.resolve() or not path.is_file():
                    continue
                arc = "stickers/" + path.name
                z.write(path, arc)
                manifest["stickers"].append({"id": row["id"], "filename": arc, "manifest": row["manifest"]})
            z.writestr("manifest.json", json.dumps(manifest, indent=2))

    def import_pack(self, archive_path):
        archive_path = Path(archive_path)
        if not archive_path.is_file() or archive_path.stat().st_size > MAX_PACK_BYTES:
            raise StickerError("Sticker pack is missing or too large.")
        added = []
        with zipfile.ZipFile(archive_path) as z:
            infos = z.infolist()
            if len(infos) > MAX_STICKERS + 1 or sum(i.file_size for i in infos) > MAX_PACK_BYTES:
                raise StickerError("Sticker pack exceeds safety limits.")
            if "manifest.json" not in z.namelist():
                raise StickerError("This is not a Video Grabber sticker pack.")
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != "video-grabber-stickers" or manifest.get("version") != 1:
                raise StickerError("Unsupported sticker pack version.")
            for item in manifest.get("stickers", []):
                member = item.get("filename", "")
                pp = PurePosixPath(member)
                if pp.is_absolute() or ".." in pp.parts or pp.suffix.lower() != ".png" or member not in z.namelist():
                    continue
                temp = self.root / (uuid.uuid4().hex + ".png")
                temp.write_bytes(z.read(member))
                try:
                    added.append(self.import_image(temp, item.get("manifest", {}).get("name", "Imported sticker"), pixelate=False))
                except StickerError:
                    continue
                finally:
                    temp.unlink(missing_ok=True)
        return added
