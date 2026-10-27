"""URL probe routes."""

from urllib.parse import urlparse

import requests
from flask import Blueprint, jsonify, request

from logging_setup import log
from settings import guess_filename, STATE
from api import _log_probe_headers, _probe_error, _probe_headers, _probe_int

probe_bp = Blueprint("routes_probe", __name__)


@probe_bp.route("/probe", methods=["POST"])
def probe():
    """Playlist/single-video item list (Session D). Request: url, optional
    start/limit and cookie/referer/user_agent. See engines.probe_playlist."""
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url or not isinstance(url, str):
        return _probe_error("missing url", "missing_url", 400)
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return _probe_error("not an http(s) URL", "bad_url", 400)
    start, ok_start = _probe_int(data, "start", 0)
    limit, ok_limit = _probe_int(data, "limit", 200)
    if not (ok_start and ok_limit):
        return _probe_error("start and limit must be integers", "bad_params", 400)
    start = max(0, start)
    limit = max(1, min(1000, limit))
    if "yt-dlp" in (STATE.get("engines_disabled") or []):
        return _probe_error("yt-dlp engine is disabled in settings",
                            "engine_disabled", 503)
    from engines import probe_playlist, PlaylistProbeError
    headers = _probe_headers(data)
    _log_probe_headers("probe", headers)
    try:
        result = probe_playlist(url, headers, start, limit)
    except PlaylistProbeError as e:
        log(f"probe: failed ({e.code}): {e}")
        return _probe_error(str(e), e.code, e.status)
    except Exception as e:
        log(f"probe: failed (extractor_error): {e}")
        return _probe_error(str(e), "extractor_error", 500)
    if result["is_playlist"]:
        pl = result["playlist"]
        log(f"probe: {url} -> playlist '{pl['title']}', "
            f"{len(result['items'])} item(s)"
            + (", truncated" if result["truncated"] else ""))
    else:
        log(f"probe: {url} -> single video")
    return jsonify(result)


@probe_bp.route("/probe-formats", methods=["POST"])
def probe_formats():
    """Thin wrapper over the engine probe (Session 7)."""
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    from engines import route_for
    headers = _probe_headers(data)
    _log_probe_headers("probe-formats", headers)
    formats, last_err = None, None
    for engine in route_for(url):          # probe falls through; download doesn't
        try:
            formats = engine.probe(url, {"headers": headers})
            if formats:
                break
        except Exception as e:
            last_err = e
    if not formats:
        return jsonify({"error": str(last_err) if last_err else "probe failed"}), 500
    # Task 2: formats are already sorted (best-first) by the engine; the
    # cap here is just a sanity ceiling against pathological format lists,
    # not the thing limiting what the user normally sees.
    return jsonify({"formats": [f.to_dict() for f in formats][:40]})

@probe_bp.route("/probe-head", methods=["POST"])
def probe_head():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        r = requests.head(url, allow_redirects=True,
                          timeout=STATE["connection_timeout"])
        return jsonify({
            "size": int(r.headers.get("Content-Length", 0)),
            "content_type": r.headers.get("Content-Type", ""),
            "filename_guess": guess_filename(url, r.headers.get("Content-Disposition")),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
