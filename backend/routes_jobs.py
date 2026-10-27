"""Job listing, creation and queue routes."""

import time

from flask import Blueprint, jsonify, request

from downloader import queue_status
from jobs import _fire_show_dialog_hooks, JOBS, new_job, save_jobs_snapshot
from logging_setup import log
from settings import save_settings, STATE
from api import _find_duplicate, cleanup_partial_files

jobs_bp = Blueprint("routes_jobs", __name__)


@jobs_bp.route("/jobs", methods=["GET"])
def jobs():
    # cookie is dropped: the GUI never reads it, and shipping every job's
    # cookie string on each 1 Hz poll bloats the payload and leaks it.
    return jsonify({jid: {k: v for k, v in j.items()
                          if k not in ("pause_evt", "stop_evt", "cookie")}
                     for jid, j in list(JOBS.items())})

@jobs_bp.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    dup = _find_duplicate(url)
    # v5.5 duplicate guard: the exact same URL already queued or downloading is
    # not queued a second time (HLS pills sent the same stream twice). The
    # existing job's id is returned with 200, so callers treat it as success.
    for _j in list(JOBS.values()):
        if _j.get("url") == url and _j.get("status") in ("queued", "downloading"):
            log(f"download: {url} is already {_j['status']} as job {_j['id']}; not queuing a duplicate")
            return jsonify({"job_id": _j["id"], "duplicate_of": _j["id"],
                            "already_queued": True, "status": _j["status"]})
    jid = new_job(
        url, filename=data.get("filename"), category=data.get("category"),
        referer=data.get("referer"), cookie=data.get("cookie"),
        user_agent=data.get("user_agent"), job_type=data.get("type"),
        format_id=data.get("format_id"), target_format=data.get("target_format"),
        resolution=data.get("resolution"), multi=bool(data.get("multi")),
        download_playlist=data.get("download_playlist", False),
        page_url=data.get("page_url"),
    )
    # The job is still created (re-downloading on purpose is legitimate);
    # the hint lets the caller tell the user it was a repeat.
    return jsonify({"job_id": jid, "duplicate_of": dup["id"] if dup else None})

@jobs_bp.route("/batch", methods=["POST"])
def batch():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items", [])
    ids = []
    deferred_save = False
    batch_ts = time.time()  # one timestamp for a whole playlist batch
    for it in items:
        if not it.get("url"):
            continue
        # Session D: playlist-picker items carry playlist_* fields. Those
        # batches skip the per-job snapshot (one save after the loop) and
        # share created_ts so the table can show them in playlist order.
        pid = it.get("playlist_id")
        pl = {}
        if pid:
            idx = it.get("playlist_index")
            pl = {
                "playlist_id": str(pid)[:200],
                "playlist_title": str(it.get("playlist_title") or "")[:300] or None,
                "playlist_index": idx if isinstance(idx, int) and not isinstance(idx, bool) else None,
                "playlist_url": str(it.get("playlist_url") or "")[:2000] or None,
                "snapshot": False,
                "created_ts": batch_ts,
            }
            deferred_save = True
        ids.append(new_job(
            it["url"], filename=it.get("filename"),
            category=it.get("category"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
            job_type=it.get("type"),
            format_id=it.get("format_id"),
            target_format=it.get("target_format"),
            resolution=it.get("resolution"), multi=bool(it.get("multi")),
            download_playlist=it.get("download_playlist", data.get("download_playlist", False)),
            page_url=data.get("page_url") or data.get("referer"),
            **pl,
        ))
    if deferred_save:
        save_jobs_snapshot()
    return jsonify({"job_ids": ids})

@jobs_bp.route("/show-add-dialog", methods=["POST"])
def show_add_dialog():
    """IDM-style: the extension asks the app to show the Add dialog instead
    of queuing directly. If the user checked "don't show again", queue."""
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    # Session D: a playlist request goes to the picker. The auto-queue branch
    # is skipped for it - there is nothing to queue until items are picked,
    # and queueing the URL would recreate the one-row-for-many-files job. An
    # older extension still sending download_playlist lands here too.
    pick = bool(data.get("pick_playlist") or data.get("download_playlist"))
    if pick:
        log("show-add-dialog: playlist picker requested")
    dup = None if pick else _find_duplicate(url)
    dup_id = dup["id"] if dup else None
    if STATE.get("skip_add_dialog") and not pick:
        jid = new_job(
            url, filename=data.get("filename"),
            category=data.get("category"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
            format_id=data.get("format_id"),
            target_format=data.get("target_format"),
            download_playlist=data.get("download_playlist", False),
            page_url=data.get("page_url"),
        )
        log(f"auto-queued {data.get('filename') or url} (skip_add_dialog)")
        return jsonify({"ok": True, "auto_queued": True, "job_id": jid,
                        "duplicate_of": dup_id})
    _fire_show_dialog_hooks({
        "url": url,
        "filename": data.get("filename"),
        "category": data.get("category"),
        "referer": data.get("referer"),
        "cookie": data.get("cookie"),
        "user_agent": data.get("user_agent"),
        "format_id": data.get("format_id"),
        "target_format": data.get("target_format"),
        "download_playlist": data.get("download_playlist", False),
        "pick_playlist": pick,
        "page_url": data.get("page_url"),
        "duplicate_of": dup_id,
        "_req_ts": time.time(),   # lets the GUI log how long the dialog took to open
    })
    return jsonify({"ok": True, "auto_queued": False, "duplicate_of": dup_id})


@jobs_bp.route("/queue-status", methods=["GET"])
def queue_status_route():
    return jsonify(queue_status())

@jobs_bp.route("/queue-running", methods=["POST"])
def set_queue_running():
    """Pause/resume the whole queue. /settings ignores queue_running so a
    stray settings POST can't flip it; this is the one deliberate writer."""
    data = request.get_json(force=True, silent=True) or {}
    if "running" not in data:
        return jsonify({"error": "missing running"}), 400
    running = data.get("running")
    if not isinstance(running, bool):
        # bool("false") is True: a string, number or null must not flip the queue.
        return jsonify({"error": "running must be true or false"}), 400
    STATE["queue_running"] = running
    save_settings()
    return jsonify({"ok": True, "queue_running": STATE["queue_running"]})


@jobs_bp.route("/dup-check", methods=["POST"])
def dup_check():
    data = request.get_json(force=True, silent=True) or {}
    j = _find_duplicate(data.get("url") or "")
    return jsonify({"duplicate": ({"job_id": j["id"], "status": j["status"],
                                   "filename": j.get("filename")} if j else None)})


@jobs_bp.route("/clear-failed", methods=["POST"])
def clear_failed():
    """Remove error rows (and their partial files). Optional JSON body:
    {"older_than_hours": N} limits it to rows at least that old."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        hours = float(data.get("older_than_hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    cutoff = time.time() - hours * 3600
    removed = 0
    for jid, j in list(JOBS.items()):
        if j.get("status") != "error":
            continue
        if hours and (j.get("created_ts") or 0) > cutoff:
            continue
        j["stop_evt"].set()
        cleanup_partial_files(j)
        JOBS.pop(jid, None)
        removed += 1
    if removed:
        save_jobs_snapshot()
    return jsonify({"removed": removed})
