"""Authenticated localhost API for the paired browser extension.

Auth: add "/companion" to TOKEN_PROTECTED_PATHS in api.py. The existing middleware also guards every
"/companion/..." path below it (it matches the path or the path plus "/"), so these routes need no
auth code of their own. No route here starts a download or touches a destination path.
"""
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from .bridge import data_dir, get_core
from .store import load_registry

companion_bp = Blueprint("companion", __name__)

_ASSETS = Path(__file__).resolve().parent / "assets"


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _sheet_names():
    reg = load_registry()
    names = {s.get("sheet") for s in reg.get("species", []) if s.get("sheet")}
    egg = (reg.get("egg") or {}).get("sheet")
    if egg:
        names.add(egg)
    return names


@companion_bp.get("/companion")
def companion_state():
    return _no_store(jsonify(get_core().snapshot()))


@companion_bp.get("/companion/manifest")
def companion_manifest():
    """Sprite layout and animation timing, so the extension animates exactly like the desktop app."""
    resp = jsonify(load_registry())
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@companion_bp.post("/companion/action")
def companion_action():
    data = request.get_json(silent=True) or {}
    core = get_core()
    action = data.get("action", "")
    if action == "species":
        result = core.set_species(data.get("species", ""))
    elif action == "cosmetic":
        result = core.equip_cosmetic(data.get("cosmetic", "none"))
    elif action in ("enable", "disable"):
        core.set_enabled(action == "enable")
        result = {"ok": True, "state": core.snapshot()}
    elif action == "option":
        result = core.set_option(data.get("name", ""), data.get("value"))
    else:
        result = core.care(action)
    return _no_store(jsonify(result)), (200 if result.get("ok") else 400)


@companion_bp.get("/companion/sprite/<name>")
def companion_sprite(name):
    if name not in _sheet_names():
        return jsonify({"ok": False, "error": "Unknown sprite"}), 404
    path = (_ASSETS / name).resolve()
    if path.parent != _ASSETS.resolve() or not path.is_file():
        return jsonify({"ok": False, "error": "Unknown sprite"}), 404
    resp = send_file(str(path), mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@companion_bp.get("/companion/stickers")
def companion_stickers():
    return _no_store(jsonify({"stickers": get_core().store.list_stickers()}))


@companion_bp.get("/companion/sticker/<sticker_id>")
def companion_sticker(sticker_id):
    core = get_core()
    for item in core.store.list_stickers():
        if item["id"] == sticker_id:
            root = (data_dir() / "stickers").resolve()
            path = (root / Path(item["filename"]).name).resolve()
            if path.parent == root and path.is_file():
                resp = send_file(str(path), mimetype="image/png")
                resp.headers["Cache-Control"] = "private, max-age=3600"
                return resp
    return jsonify({"ok": False, "error": "Unknown sticker"}), 404


@companion_bp.get("/companion/diag")
def companion_diag():
    """Counters for failures the companion swallowed on purpose (see diag.py). Read-only."""
    from . import diag
    return _no_store(jsonify(diag.snapshot()))


@companion_bp.get("/companion/achievements")
def companion_achievements():
    return _no_store(jsonify({"achievements": get_core().achievements()}))
