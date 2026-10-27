"""App / yt-dlp update and changelog routes."""

import re
import threading

from flask import Blueprint, jsonify, request

from downloader import queue_status
from settings import APP_VERSION
from api import CHANGELOG_REPO, _CHANGELOG_CACHE, _CHANGELOG_TTL

updates_bp = Blueprint("routes_updates", __name__)


@updates_bp.route("/update-status", methods=["GET"])
def update_status():
    import updater
    import ytdlp_update
    return jsonify({"app": updater.get_update_state(),
                    "ytdlp": ytdlp_update.status(),
                    "queue": queue_status()})


@updates_bp.route("/update-skip", methods=["POST"])
def update_skip():
    import updater
    data = request.get_json(force=True, silent=True) or {}
    updater.skip_version(str(data.get("tag") or ""))
    return jsonify({"ok": True})


@updates_bp.route("/ytdlp-update", methods=["POST"])
def ytdlp_update_now():
    """Stage the newest yt-dlp now (applies on next launch)."""
    import ytdlp_update
    threading.Thread(target=ytdlp_update.update_ytdlp, daemon=True).start()
    return jsonify({"started": True})


@updates_bp.route("/ytdlp-rollback", methods=["POST"])
def ytdlp_rollback():
    import ytdlp_update
    return jsonify({"removed": ytdlp_update.rollback()})


@updates_bp.route("/changelog", methods=["GET"])
def changelog_route():
    import json as _json
    import time as _time
    from urllib.request import Request, urlopen
    now = _time.time()
    cached = _CHANGELOG_CACHE["data"]
    if cached and now - _CHANGELOG_CACHE["ts"] < _CHANGELOG_TTL:
        return jsonify(cached)
    req = Request(
        f"https://api.github.com/repos/{CHANGELOG_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"VideoGrabber/{APP_VERSION}"})
    try:
        with urlopen(req, timeout=8) as r:
            rel = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        if cached:  # stale notes beat none when GitHub is unreachable
            return jsonify(cached)
        return jsonify({"ok": False, "error": str(e)}), 502
    # Batch 3: clean the notes for the app. The release plumbing is noise
    # in the Activity Log, so drop the "Automatic build from commit ..."
    # line and asset lines. Batch 4: an asset line is one that STARTS
    # with a .zip filename (after an optional list marker), even when
    # more text follows it; a line that merely mentions a zip later in
    # prose is a real changelog note and is kept.
    _ZIP_ASSET_RE = re.compile(
        r"^[\s\-\*\+]*[\w.\-]+\.zip\b.*$", re.IGNORECASE)

    def _clean_changelog(body):
        kept = []
        for ln in str(body or "").splitlines():
            low = ln.strip().lower()
            if low.startswith("automatic build from commit"):
                continue
            if _ZIP_ASSET_RE.match(ln):
                continue
            kept.append(ln)
        return "\n".join(kept).strip()

    data = {
        "ok": True,
        "tag": rel.get("tag_name") or "",
        "name": rel.get("name") or "",
        "body": _clean_changelog(rel.get("body") or ""),
        "url": rel.get("html_url") or "",
        "published_at": rel.get("published_at") or "",
    }
    _CHANGELOG_CACHE.update(ts=now, data=data)
    return jsonify(data)
