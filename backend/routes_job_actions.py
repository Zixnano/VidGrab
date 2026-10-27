"""Per-job action routes."""

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

from downloader import _CREATE_NO_WINDOW, _WORKERS
from engines import clear_youtube_cooldown, find_ffmpeg_dir
from jobs import JobEvent, JobPhase, JOBS, reorder_job, repair_status, save_jobs_snapshot, set_phase, transition
from logging_setup import log
from settings import _dest_for, _unique_path, stat_for
from api import _safe_in_roots, cleanup_partial_files

job_actions_bp = Blueprint("routes_job_actions", __name__)


def _job_action(job_id, action):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if action == "pause":
        job["pause_evt"].set()
    elif action == "resume":
        if job["status"] == "done":
            return jsonify({"error": "already done"}), 400
        if job["status"] == "downloading":
            return jsonify({"error": "already downloading"}), 400
        job["pause_evt"].clear()
        job["stop_evt"].clear()
        job["error"] = None
        # v5: a manual Resume overrides auto-retry delays and the YouTube
        # hold (e.g. the user just switched VPN after a bot check).
        job["retry_count"] = 0
        job["retry_after"] = 0
        clear_youtube_cooldown()
        w = _WORKERS.get(job_id)
        if (job["status"] == "paused" and job.get("type") != "generic"
                and w is not None and w.is_alive()):
            # yt-dlp keeps its worker thread parked in progress_cb while a
            # job is paused and simply carries on once pause_evt clears.
            # Routing that through "queued" let the dispatcher start a
            # second yt-dlp on the same .part files about one resume in
            # four. Hand the job straight back to the parked worker.
            transition(job, JobEvent.START)
        else:
            transition(job, JobEvent.RESUME)
    elif action == "stop":
        w = _WORKERS.get(job_id)
        if job["status"] == "queued":
            # No worker thread exists yet to observe stop_evt, and the
            # dispatcher clears it when it starts the job — so the old
            # set() was a silent no-op and the job started anyway. Flip the
            # status instead (transition() also sets stop_evt).
            transition(job, JobEvent.STOP)
        elif (job["status"] == "paused"
                and not (w is not None and w.is_alive())):
            # A paused generic job's worker already exited when it paused (and
            # after a restart there is no worker at all), so nothing would
            # ever observe stop_evt and the row stayed "paused" forever. Move
            # it to stopped ourselves (transition() also sets stop_evt).
            transition(job, JobEvent.STOP)
        else:
            job["stop_evt"].set()
        job["pause_evt"].clear()
    elif action == "delete":
        job["stop_evt"].set()
        cleanup_partial_files(job)
        JOBS.pop(job_id, None)
    save_jobs_snapshot()
    return jsonify({"ok": True})

@job_actions_bp.route("/pause/<job_id>", methods=["POST"])
def pause(job_id):
    return _job_action(job_id, "pause")

@job_actions_bp.route("/resume/<job_id>", methods=["POST"])
def resume(job_id):
    return _job_action(job_id, "resume")

@job_actions_bp.route("/stop/<job_id>", methods=["POST"])
def stop(job_id):
    return _job_action(job_id, "stop")

@job_actions_bp.route("/delete/<job_id>", methods=["POST"])
def delete(job_id):
    return _job_action(job_id, "delete")


@job_actions_bp.route("/delete-file/<job_id>", methods=["POST"])
def delete_job_and_file(job_id):
    """Remove the job AND its file on disk. The path must resolve inside the
    VideoGrabber / download folders; anything else is refused and the job is
    left alone. Thumbnails (thumbs/) are never touched."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    cands = []
    if job.get("dest"):
        cands.append(Path(job["dest"]))
    try:
        cands.append(_dest_for(job))
    except Exception as e:
        log(f"delete-file: cannot resolve destination for job {job_id}: {e}")
    uniq = []
    for c in cands:
        if c not in uniq:
            uniq.append(c)
    targets = []
    for c in uniq:
        try:
            exists = c.is_file()
        except OSError:
            exists = False
        if not exists:
            continue
        safe = _safe_in_roots(c)
        # Files only: the resolved target must be a regular file (never a
        # folder) and sit strictly inside the root VideoGrabber directory.
        if safe is None or not safe.is_file() or safe.is_dir():
            log(f"delete-file refused for job {job_id}: {c} is not a file inside the VideoGrabber folder")
            return jsonify({"error": "File is outside the VideoGrabber folder; "
                                     "refusing to delete it. The job was kept."}), 403
        if safe not in targets:
            targets.append(safe)
    # v5.5 safe deletion: another job can point at the same file (duplicate or
    # HLS rows). A shared file is never removed from disk; only this job goes.
    _shared = set()
    for _oid, _other in list(JOBS.items()):
        if _oid == job_id:
            continue
        _oc = []
        if _other.get("dest"):
            _oc.append(Path(_other["dest"]))
        try:
            _oc.append(_dest_for(_other))
        except Exception as e:
            log(f"delete-file: cannot resolve destination for job {_oid}: {e}")
        for _c in _oc:
            try:
                _shared.add(Path(_c).resolve())
            except (OSError, ValueError) as e:
                log(f"delete-file: cannot resolve {_c}: {e}")
    _kept = [t for t in targets if t in _shared]
    if _kept:
        log(f"delete-file: job {job_id} shares {[str(t) for t in _kept]} with another job; "
            "keeping the file on disk and removing only the job")
        targets = [t for t in targets if t not in _shared]
    job["stop_evt"].set()
    try:
        _inside = _safe_in_roots(_dest_for(job)) is not None
    except Exception:
        _inside = False
    if _inside and not _kept:
        cleanup_partial_files(job)   # .part leftovers, only when dest is inside the root
    removed = []
    for t in targets:
        try:
            os.remove(t)
            removed.append(str(t))
        except PermissionError:
            return jsonify({"error": f"{t.name} is in use by another program. Close it and try again."}), 409
        except OSError as e:
            return jsonify({"error": f"Couldn't delete {t.name}: {e}"}), 500
    JOBS.pop(job_id, None)
    save_jobs_snapshot()
    log(f"deleted job {job_id} and file(s): {removed or 'none on disk'}")
    return jsonify({"ok": True, "removed": removed, "kept_shared": [str(t) for t in _kept]})

@job_actions_bp.route("/redownload/<job_id>", methods=["POST"])
def redownload(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] == "downloading":
        # The running download thread only checks stop_evt between chunks.
        # set() immediately followed by clear() here gives it no reliable
        # window to observe the signal, so the old thread can keep writing
        # to the same .part file the freshly-dispatched job also writes to.
        return jsonify({"error": "job is actively downloading — stop it first"}), 400
    w = _WORKERS.get(job_id)
    if (job["status"] == "paused" and job.get("type") != "generic"
            and w is not None and w.is_alive()):
        # A paused yt-dlp job's worker is still parked in progress_cb. The
        # set()/clear() below gives it no window to see a stop, and the
        # dispatcher then started a SECOND yt-dlp on the same files. Stop the
        # parked worker and wait for it to exit before cleaning anything up.
        job["stop_evt"].set()
        job["pause_evt"].clear()
        w.join(timeout=5)
        if w.is_alive():
            return jsonify({"error": "job is still stopping, try again in a moment"}), 409
    if job["status"] == "done":
        # RESUME isn't a legal transition from DONE: this route used to zero
        # size_done/size_total below and THEN raise (HTTP 500), leaving a
        # finished job showing 0 bytes. Reopen it so RESUME -> QUEUED works.
        repair_status(job, "stopped")
    job["stop_evt"].set()
    cleanup_partial_files(job)
    job["pause_evt"].clear()
    job["stop_evt"].clear()
    job["error"] = None
    job["retry_count"] = 0
    job["retry_after"] = 0
    job["no_archive"] = True   # an explicit redownload must not be skipped
    clear_youtube_cooldown()
    job["size_done"] = 0
    job["size_total"] = 0
    transition(job, JobEvent.RESUME)
    save_jobs_snapshot()
    return jsonify({"ok": True})


@job_actions_bp.route("/reorder/<jid>", methods=["POST"])
def reorder(jid):
    """Move a queued job in the start order: {"action": top|up|down|bottom}."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        pos = reorder_job(jid, str(data.get("action") or ""))
    except KeyError:
        return jsonify({"error": "no such job"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if pos is None:
        return jsonify({"error": "only queued jobs can be reordered"}), 409
    save_jobs_snapshot()
    return jsonify({"ok": True, "position": pos})

@job_actions_bp.route("/category/<job_id>", methods=["POST"])
def set_category(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    data = request.get_json(force=True, silent=True) or {}
    new_cat = data.get("category", "Other")
    old_cat = job.get("category", "Other")
    if new_cat == old_cat:
        return jsonify({"ok": True, "moved": False})
    try:
        old_path = _dest_for(job)
    except Exception:
        old_path = None
    job["category"] = new_cat
    try:
        new_path = _dest_for(job)
    except Exception as e:
        job["category"] = old_cat
        return jsonify({"error": str(e)}), 500
    if old_path and old_path.exists() and old_path != new_path:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if new_path.exists():
            new_path = _unique_path(new_path)
            job["filename"] = new_path.name
        try:
            try:
                os.replace(old_path, new_path)
            except OSError:
                import shutil as _sh
                _sh.move(str(old_path), str(new_path))
        except Exception as e:
            job["category"] = old_cat
            return jsonify({"error": f"could not move file: {e}"}), 500
    save_jobs_snapshot()
    return jsonify({"ok": True, "moved": True})


# ---- Open folder: popup "done" link on Recent downloads -------------------
@job_actions_bp.route("/open-folder", methods=["POST"])
def open_folder():
    """Open Explorer on a finished job's file (selected) or, if the file is
    gone, its folder. The path comes only from the job record, never from the
    request, and no shell is involved."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = JOBS.get(job_id) if isinstance(job_id, str) else None
    if not job:
        return jsonify({"error": "unknown job"}), 404
    cands = []
    if job.get("dest"):
        cands.append(Path(job["dest"]))
    try:
        cands.append(_dest_for(job))
    except Exception as e:
        log(f"open-folder: cannot resolve destination for job {job_id}: {e}")
    target = None
    for c in cands:
        try:
            if c.is_file():
                target = c
                break
        except OSError:
            continue
    folder = None
    if target is None:
        for c in cands:
            try:
                if c.parent.is_dir():
                    folder = c.parent
                    break
            except OSError:
                continue
        if folder is None:
            return jsonify({"error": "Folder not found"}), 404
    try:
        if sys.platform == "win32":
            if target is not None:
                subprocess.Popen(["explorer", f"/select,{target}"])
            else:
                os.startfile(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder or target.parent)])
    except Exception as e:
        log(f"open-folder failed for job {job_id}: {e}")
        return jsonify({"error": "Couldn't open the folder"}), 500
    return jsonify({"ok": True})

@job_actions_bp.route("/convert/<job_id>", methods=["POST"])
def convert(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "job not done"}), 400
    if job.get("phase") == "converting":
        return jsonify({"error": "conversion already in progress"}), 400
    data = request.get_json(force=True, silent=True) or {}
    fmt = (data.get("format") or "").lower()
    if fmt not in ("mp4", "mkv", "mp3", "wav", "m4a", "flac", "opus"):
        return jsonify({"error": "unsupported format"}), 400
    src = stat_for(job)
    if not src.exists():
        return jsonify({"error": "source missing"}), 404
    _ffdir = find_ffmpeg_dir()
    ffmpeg = str(Path(_ffdir) / "ffmpeg.exe") if _ffdir else "ffmpeg"
    dst = src.with_suffix("." + fmt)
    if dst.exists():
        # Never overwrite: an earlier conversion, or the source itself when
        # converting mp4 -> mp4, would otherwise be clobbered by ffmpeg -y.
        try:
            dst = _unique_path(dst)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 409
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if fmt in ("mp3", "wav", "m4a", "flac", "opus"):
        cmd += ["-vn"]
        if fmt == "mp3":
            cmd += ["-q:a", "0"]
        elif fmt == "opus":
            cmd += ["-b:a", "192k"]
        elif fmt == "m4a":
            cmd += ["-b:a", "256k"]
    cmd += [str(dst)]

    set_phase(job, JobPhase.CONVERTING)
    job["converting_fmt"] = fmt
    job["conversion_progress"] = 0
    log(f"converting {src.name} → {dst.name}")

    def progress_thread():
        total_sec = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW, text=True, errors="replace",
            )
            dur_re = re.compile(r"Duration: (\d+):(\d+):(\d+)")
            time_re = re.compile(r"time=(\d+):(\d+):(\d+)")
            for line in proc.stdout:
                if total_sec is None:
                    m = dur_re.search(line)
                    if m:
                        total_sec = (int(m.group(1)) * 3600
                                     + int(m.group(2)) * 60 + int(m.group(3)))
                m = time_re.search(line)
                if m and total_sec:
                    done_sec = (int(m.group(1)) * 3600
                                + int(m.group(2)) * 60 + int(m.group(3)))
                    job["conversion_progress"] = min(99, int(done_sec / total_sec * 100))
            proc.wait()
            if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                job["conversion_progress"] = 100
                job["converted_to"] = fmt
                log(f"converted {src.name} → {dst.name}")
            else:
                log(f"conversion failed for {src.name} (rc={proc.returncode})")
        except Exception as e:
            log(f"conversion failed for {src.name}: {e}")
        finally:
            set_phase(job, JobPhase.DONE)
            save_jobs_snapshot()

    threading.Thread(target=progress_thread, daemon=True).start()
    return jsonify({"ok": True})
