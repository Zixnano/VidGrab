"""Recording upload and file toolkit routes."""

import os
import time

from flask import Blueprint, jsonify, request

from downloader import _probe_recording, _repair_recording, maybe_convert_to_mp4
from jobs import _fire_new_job_hooks, JobEvent, JOBS, new_job, repair_status, save_jobs_snapshot, transition
from logging_setup import log
from settings import _dest_for, _unique_path, category_for, MAX_UPLOAD_BYTES, safe_filename
from api import _MEDIA_UPLOAD_EXTS, _ff_bins, _parse_clock, _probe_duration, _run_toolkit, _toolkit_out, _toolkit_source

files_bp = Blueprint("routes_files", __name__)


@files_bp.route("/upload", methods=["POST"])
def upload():
    """Direct handoff for extension uploads (multipart, one file): screen
    recordings (media: repaired and validated) and arbitrary files from the
    blob-download hook (registered as-is, no ffmpeg/ffprobe)."""
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"}), 413
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "missing 'file' field"}), 400
    # An upload with no name at all is a recording. A NAMED upload keeps its
    # name as-is: a blob download called "README" must not become .webm, it
    # would then be probed as media and fail.
    filename = safe_filename(f.filename or "recording.webm")
    ext = os.path.splitext(filename)[1].lower()
    _is_media = ext in _MEDIA_UPLOAD_EXTS
    category = "Video" if _is_media else category_for(filename)
    # _dest_for resolves the folder exactly like downloads do (per-category
    # folders, sanitised names, path-length cap).
    dest = _dest_for({"category": category, "filename": filename})
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest = _unique_path(dest)   # never overwrite an existing file
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    try:
        f.save(str(dest))
    except Exception as e:
        try:
            dest.unlink()
        except OSError:
            pass
        log(f"upload save failed: {dest.name}: {e}")
        return jsonify({"error": f"couldn't save upload: {e}"}), 500
    log(f"recording upload: {dest.name} ({dest.stat().st_size} bytes)")

    if _is_media:
        dest = _repair_recording(dest)

    jid = new_job(url=f"upload:///{dest.name}", filename=dest.name,
                  category=category, job_type="generic", defer=True)
    job = JOBS[jid]
    # The file is already on disk under this name in this folder: new_job's
    # naming cleanup and per-site rules must not point the job anywhere else.
    job["filename"] = dest.name
    job["category"] = category
    if job.get("status") == "skipped":
        # File type disabled in Options: the file is already saved, so keep
        # it. A skipped job has no COMPLETE transition; reopen it first.
        repair_status(job, "held")

    if _is_media:
        ok, reason = _probe_recording(dest)
        if not ok:
            transition(job, JobEvent.FAIL)
            job["error"] = f"corrupt recording: {reason}"
            log(f"uploaded recording is corrupt: {reason}")
            save_jobs_snapshot()
            _fire_new_job_hooks(job)
            return jsonify({"ok": False, "error": f"corrupt recording: {reason}"}), 400
        log(f"recording validated: {dest.name} ({dest.stat().st_size} bytes)")
    else:
        log(f"upload registered as-is: {dest.name} ({category}, {dest.stat().st_size} bytes)")
    transition(job, JobEvent.COMPLETE)
    job["size_total"] = job["size_done"] = dest.stat().st_size
    if _is_media:
        dest = maybe_convert_to_mp4(job, dest)  # .webm → .mp4 when auto_mp4 is on
    job["completed_ts"] = time.time()
    save_jobs_snapshot()
    _fire_new_job_hooks(job)
    # "category" lets the extension say where the file went ("Sent to Compressed").
    return jsonify({"ok": True, "job_id": jid, "category": category})


@files_bp.route("/toolkit/compress", methods=["POST"])
def toolkit_compress():
    data = request.get_json(force=True, silent=True) or {}
    try:
        src = _toolkit_source(data)
        ffmpeg, _ = _ff_bins()
        out = _toolkit_out(src, "compressed", "mp4")
        cmd = [ffmpeg, "-y", "-i", str(src), "-c:v", "libx264", "-preset", "medium",
               "-pix_fmt", "yuv420p"]
        if (data.get("mode") or "quality") == "size":
            mb = float(data.get("target_mb") or 0)
            if mb <= 0:
                raise ValueError("target size must be above 0 MB")
            dur = _probe_duration(src)
            audio_k = 128
            v_k = int(mb * 8192 / dur - audio_k)      # MB -> kilobit, minus audio
            if v_k < 50:
                raise ValueError("target size is too small for this video's length")
            cmd += ["-b:v", f"{v_k}k", "-maxrate", f"{int(v_k * 1.5)}k",
                    "-bufsize", f"{v_k * 3}k"]
        else:
            crf = int(data.get("crf") if data.get("crf") is not None else 28)
            cmd += ["-crf", str(max(16, min(40, crf)))]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return _run_toolkit(cmd, out, "compress")


@files_bp.route("/toolkit/trim", methods=["POST"])
def toolkit_trim():
    data = request.get_json(force=True, silent=True) or {}
    try:
        src = _toolkit_source(data)
        ffmpeg, _ = _ff_bins()
        start = _parse_clock(data.get("start") or "0", "start")
        end_raw = str(data.get("end") or "").strip()
        end = _parse_clock(end_raw, "end") if end_raw else None
        if end is not None and end <= start:
            raise ValueError("end must be after start")
        out = _toolkit_out(src, "trim", src.suffix or ".mp4")
        # -ss / -to before -i + -c copy: no re-encode, so it is instant and
        # lossless (cuts land on the nearest keyframe).
        cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}"]
        if end is not None:
            cmd += ["-to", f"{end:.3f}"]
        cmd += ["-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return _run_toolkit(cmd, out, "trim")


@files_bp.route("/toolkit/gif", methods=["POST"])
def toolkit_gif():
    data = request.get_json(force=True, silent=True) or {}
    try:
        src = _toolkit_source(data)
        ffmpeg, _ = _ff_bins()
        start = _parse_clock(data.get("start") or "0", "start")
        dur = float(data.get("duration") or 5)
        if not 0.1 <= dur <= 60:
            raise ValueError("duration must be between 0.1 and 60 seconds")
        fps = max(1, min(30, int(data.get("fps") or 12)))
        width = max(64, min(1280, int(data.get("width") or 480)))
        out = _toolkit_out(src, "gif", "gif")
        # Two-stage palette in one pass: palettegen builds the best 256 colors
        # for this clip, paletteuse maps frames onto it. Far cleaner than the
        # default GIF palette.
        vf = (f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];"
              f"[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5")
        cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
               "-vf", vf, "-loop", "0", str(out)]
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return _run_toolkit(cmd, out, "gif")
