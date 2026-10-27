"""Pairing, upload-nonce and favicon routes."""

from flask import Blueprint, jsonify, request

from logging_setup import log
from settings import STATE
import api
from api import _EXT_ORIGIN, _FAVICON_DOMAIN_RE, _PNG_MAGIC, _issue_upload_nonce

pair_bp = Blueprint("routes_pair", __name__)


@pair_bp.route("/pair", methods=["POST"])
def pair():
    """Handshake for the extension: hand out the API token, but only to a
    localhost peer, and only once per app session."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        log(f"pair attempt rejected: remote_addr={request.remote_addr}")
        return jsonify({"error": "pairing only allowed from localhost"}), 403
    # Browsers always attach Origin to a cross-origin POST, so a web page
    # cannot pair. Only the pinned extension origin may. (No Origin header =
    # a non-browser local process, which can read settings.json anyway.)
    origin = request.headers.get("Origin")
    if origin is not None and origin != _EXT_ORIGIN:
        log(f"pair attempt rejected: Origin={origin!r}")
        return jsonify({"error": "pairing only allowed from the Video Grabber extension"}), 403
    if api._PAIR_GRANTED:
        log("pair attempt rejected: already granted this session")
        return jsonify({"error": "pairing already granted this session"}), 403
    # module attribute: see the note in api.py
    api._PAIR_GRANTED = True
    log("extension paired automatically")
    return jsonify({"token": STATE.get("api_token", "")})

@pair_bp.route("/upload-token", methods=["POST"])
def upload_token():
    return jsonify({"nonce": _issue_upload_nonce()})


@pair_bp.route("/favicon", methods=["POST"])
def favicon_store():
    """The extension sends each site's favicon once (PNG data URL)."""
    import base64
    from settings import icons_dir
    data = request.get_json(force=True, silent=True) or {}
    domain = str(data.get("domain") or "").lower().strip()
    uri = str(data.get("data_url") or "")
    prefix = "data:image/png;base64,"
    if not _FAVICON_DOMAIN_RE.match(domain) or not uri.startswith(prefix):
        return jsonify({"error": "bad favicon request"}), 400
    try:
        raw = base64.b64decode(uri[len(prefix):], validate=True)
    except Exception:
        return jsonify({"error": "bad base64"}), 400
    if len(raw) > 65536 or not raw.startswith(_PNG_MAGIC):
        return jsonify({"error": "not a small PNG"}), 400
    d = icons_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / (domain + ".png")).write_bytes(raw)
    return jsonify({"ok": True})


@pair_bp.route("/favicon/<domain>", methods=["GET"])
def favicon_get(domain):
    from flask import send_file
    from settings import icons_dir
    domain = domain.lower()
    p = icons_dir() / (domain + ".png")
    if not _FAVICON_DOMAIN_RE.match(domain) or not p.is_file():
        return jsonify({"error": "no icon"}), 404
    return send_file(str(p), mimetype="image/png")
