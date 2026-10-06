"""Download engines (generic HTTP + yt-dlp), ffmpeg helpers, recording watcher."""
from pathlib import Path
import glob
import os
import requests

# v5: must run before yt_dlp is imported so a staged/override yt-dlp wins.
import ytdlp_update
ytdlp_update.activate()

from yt_dlp import YoutubeDL
from engines import (route_for, DownloadCancelled,
                     _ytdlp_version_check, find_js_runtime,
                     classify_ytdlp_error, set_youtube_cooldown,
                     youtube_cooldown_remaining, _is_youtube_url,
                     find_ffmpeg_dir)
import shutil
import subprocess
import sys
import threading
import time

import settings
from settings import (STATE, load_settings, save_settings, safe_filename, _unique_path,
                      guess_ext_from_head, stat_for, _dest_for,
                      effective_speed_limit_kbps, LIMITER, get_shutdown_pending,
                      set_shutdown_pending, detect_type)
from jobs import (start_order_key, JOBS, new_job, save_jobs_snapshot, _fire_new_job_hooks,
                  transition, JobEvent, JobPhase, set_phase, resolve_playlist_dir,
                  repair_status)
from logging_setup import log, LOG_QUEUE


# Keep ffmpeg / Defender from flashing console windows when frozen on Windows.
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
# --- yt-dlp YouTube runtime helpers -----------------------------------------



def scan_file(path):
    if not STATE.get("defender_scan"):
        return
    defender = r"C:\Program Files\Windows Defender\MpCmdRun.exe"
    if not os.path.exists(defender):
        return
    try:
        r = subprocess.run([defender, "-Scan", "-ScanType", "3", "-File", str(path)],
                           capture_output=True, timeout=120,
                           creationflags=_CREATE_NO_WINDOW)
        if r.returncode != 0:
            log(f"Defender flagged {path.name} (rc={r.returncode})")
    except Exception as e:
        log(f"Defender scan failed: {e}")


def maybe_shutdown_if_idle():
    if not STATE.get("auto_shutdown"):
        return
    if any(j["status"] in ("downloading", "queued") for j in list(JOBS.values())):
        return
    if settings.get_shutdown_pending():
        return
    settings.set_shutdown_pending(True)
    log("Queue empty — shutting down in 60s (cancel with `shutdown /a`)")
    os.system("shutdown /s /t 60")


def maybe_extract_archive(job, dest):
    if not STATE.get("auto_extract"):
        return
    if dest.suffix.lower() != ".zip":
        return
    try:
        import zipfile
        out = dest.with_suffix("")
        out.mkdir(exist_ok=True)
        with zipfile.ZipFile(dest) as z:
            out_resolved = out.resolve()
            for member in z.infolist():
                target = (out / member.filename).resolve()
                # zip-slip guard: reject any member whose extracted path
                # would land outside `out` (e.g. "../../etc/passwd").
                if target != out_resolved and out_resolved not in target.parents:
                    log(f"auto-extract: skipped unsafe path in {dest.name}: "
                        f"{member.filename!r}")
                    continue
                z.extract(member, out)
        log(f"extracted {dest.name} → {out.name}/")
    except Exception as e:
        log(f"auto-extract failed for {dest.name}: {e}")


def _playlist_group_pending(job):
    """True while another job from the same playlist batch is still waiting
    or running. Open-folder and the completion sound fire once per batch (on
    the last item), not once per item. Jobs without a playlist_id are never
    grouped."""
    pid = job.get("playlist_id")
    if not pid:
        return False
    for other in list(JOBS.values()):
        if other is job:
            continue
        if (other.get("playlist_id") == pid
                and other.get("status") in ("queued", "downloading", "paused", "held")):
            return True
    return False


def maybe_open_folder(dest):
    if STATE.get("open_folder_on_complete") and dest.exists():
        try:
            os.startfile(str(dest.parent))
        except Exception:
            pass


def maybe_play_sound():
    if not STATE.get("play_sound_on_complete"):
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def record_stat(nbytes):
    stats = STATE.setdefault("download_stats", [])
    stats.append({"ts": time.time(), "bytes": nbytes})
    if len(stats) > 3600:
        del stats[: len(stats) - 3600]


AUDIO_ONLY_TARGETS = {"mp3", "flac", "opus", "m4a"}



def maybe_convert_target(job, dest):
    """Convert a finished direct download to the job's requested target
    format; when no target was requested, fall back to the legacy
    auto-mp4 behavior. Audio-only targets strip the video track."""
    target = (job.get("target_format") or "").lower().lstrip(".")
    if not target:
        return maybe_convert_to_mp4(job, dest)
    if dest.suffix.lower().lstrip(".") == target:
        return dest
    try:
        target_path = dest.with_suffix("." + target)
    except ValueError:
        log(f"target-format convert: invalid target '{target}', skipping")
        return dest
    _ffdir = find_ffmpeg_dir()
    ffmpeg = str(Path(_ffdir) / "ffmpeg.exe") if _ffdir else "ffmpeg"
    cmd = [ffmpeg, "-y", "-i", str(dest)]
    if target in AUDIO_ONLY_TARGETS:
        cmd += ["-vn"]
        if target == "mp3":
            cmd += ["-q:a", "0"]
        elif target == "opus":
            cmd += ["-b:a", "192k"]
        elif target == "m4a":
            cmd += ["-b:a", "256k"]
    cmd += [str(target_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600,
                           creationflags=_CREATE_NO_WINDOW)
        if r.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
            try:
                dest.unlink()
            except OSError:
                pass
            job["filename"] = target_path.name
            log(f"converted {dest.name} → {target_path.name}")
            return target_path
        err_tail = (r.stderr or b"")[-300:].decode(errors="replace")
        log(f"target-format convert failed for {dest.name} (rc={r.returncode}): {err_tail}")
    except Exception as e:
        log(f"target-format convert failed for {dest.name}: {e}")
    return dest


def _job_dest(job):
    """Registry filename -> on-disk destination, disambiguated against files
    that already exist (two jobs can queue with the same name — e.g. the
    multi-quality checklist's same-resolution double-check). Keeps
    job["filename"] in sync with the real file."""
    dest = _unique_path(stat_for(job))
    if dest.name != job["filename"]:
        job["filename"] = dest.name
    return dest


def _ffprobe_video_stream(path):
    """True when ffprobe reports a video stream in the file.

    Content-based check: this is the ground truth for "is this a video",
    regardless of the file's extension. Non-media files (.py, .zip, .pdf,
    .json, ...) return False and are left untouched. Returns False on any
    error (probe failed, file missing, ffprobe not found)."""
    _ffdir = find_ffmpeg_dir()
    ffprobe = str(Path(_ffdir) / "ffprobe.exe") if _ffdir else "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             str(path)],
            capture_output=True, timeout=30, text=True, errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        return r.returncode == 0 and "video" in (r.stdout or "").lower()
    except Exception as e:
        log(f"auto-mp4: ffprobe failed on {path.name}: {e}")
        return False


def maybe_convert_to_mp4(job, dest):
    """Convert a finished download to MP4 when it is actually a video.

    Content-based decision: ffprobe is asked whether the file has a video
    stream. Extensions are trusted only for the trivial "already .mp4"
    short-circuit. Nothing else in this function depends on the filename,
    so a .py script, a .zip archive, or a .pdf document passes through
    untouched. Never deletes the source until the output is confirmed."""
    if not STATE.get("auto_mp4"):
        return dest
    if dest.suffix.lower() == ".mp4":
        return dest
    if not dest.exists() or dest.stat().st_size == 0:
        return dest
    if not _ffprobe_video_stream(dest):
        log(f"auto-mp4: {dest.name} has no video stream, leaving as-is")
        return dest
    _ffdir = find_ffmpeg_dir()
    ffmpeg = str(Path(_ffdir) / "ffmpeg.exe") if _ffdir else "ffmpeg"
    log(f"auto-mp4: ffmpeg={ffmpeg} exists={os.path.exists(ffmpeg)} "
        f"input={dest.name}")
    target = dest.with_suffix(".mp4")
    log(f"auto-mp4: output → {target}")
    for args in (
        ["-c", "copy", "-movflags", "+faststart"],
        ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac"],
    ):
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", str(dest)] + args + [str(target)],
                capture_output=True, timeout=600,
                creationflags=_CREATE_NO_WINDOW,
            )
            # Only trust the conversion if the output exists and is non-zero —
            # a rc=0 run that wrote nothing must not cost us the source file.
            if r.returncode == 0 and target.exists() and target.stat().st_size > 0:
                try:
                    dest.unlink()
                except OSError:
                    pass
                log(f"converted {dest.name} → {target.name}")
                if Path(job["filename"]).suffix.lower() != ".mp4":
                    job["filename"] = target.name
                return target
            err_tail = (r.stderr or b"")[-500:].decode(errors="replace")
            log(f"auto-mp4: ffmpeg rc={r.returncode} for {dest.name} — stderr tail: {err_tail}")
        except Exception as e:
            log(f"ffmpeg pass failed for {dest.name}: {e}")
    # Both passes failed: leave the original file alone. Do NOT delete it.
    log(f"auto-mp4: all passes failed for {dest.name}, keeping original")
    return dest



try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except ImportError:
    _WATCHDOG_OK = False

    class FileSystemEventHandler:  # fallback base: _RecordingHandler below
        pass  # is defined unconditionally; without this the module
              # NameErrors at import time when watchdog is absent



class _RecordingHandler(FileSystemEventHandler):
    VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix.lower() not in self.VIDEO_EXTS:
            return
        threading.Thread(target=self._import_after_delay,
                         args=(p,), daemon=True).start()

    def _import_after_delay(self, p):
        last = -1
        stable = 0
        for _ in range(60):
            time.sleep(1)
            if not p.exists():
                return
            try:
                size = p.stat().st_size
            except OSError:
                return
            if size == last and size > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = size
        try:
            dest_dir = Path(STATE["output_dir"]) / "Video"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe_filename(p.name)
            if dest.exists():
                return
            shutil.copy2(p, dest)
            log(f"auto-imported screen recording: {p.name}")

            # BUGS.md #1 fix: this used to only fire _fire_new_job_hooks
            # with a synthetic dict, so the recording was copied to disk
            # but never registered anywhere (/jobs, GUI table, jobs.json).
            # Mirrors the /upload route's pattern for a file that's
            # already fully written on disk.
            dest = _repair_recording(dest)
            jid = new_job(url=f"watch:///{dest.name}", filename=dest.name,
                          category="Video", job_type="generic", defer=True)
            job = JOBS[jid]
            ok, reason = _probe_recording(dest)
            if not ok:
                transition(job, JobEvent.FAIL)
                job["error"] = f"corrupt recording: {reason}"
                log(f"auto-imported recording is corrupt: {reason}")
                save_jobs_snapshot()
                _fire_new_job_hooks(job)
                return
            transition(job, JobEvent.COMPLETE)
            job["size_total"] = job["size_done"] = dest.stat().st_size
            dest = maybe_convert_to_mp4(job, dest)  # .webm → .mp4 when auto_mp4 is on
            job["completed_ts"] = time.time()
            save_jobs_snapshot()
            _fire_new_job_hooks(job)
        except Exception as e:
            log(f"couldn't import recording {p.name}: {e}")


def start_recording_watcher():
    if not _WATCHDOG_OK:
        log("watchdog not installed — screen-recording auto-import disabled")
        return
    if sys.platform != "win32":
        return
    if not STATE.get("watch_recording_folder", True):
        return
    custom = STATE.get("recording_watch_dirs")
    if custom:
        candidates = [Path(x) for x in custom]
    else:
        candidates = [
            Path.home() / "Videos" / "Captures",
            Path.home() / "Videos",
            Path.home() / "Videos" / "Screen Recordings",
        ]
    seen = set()
    observer = Observer()
    for folder in candidates:
        if folder.exists() and str(folder) not in seen:
            observer.schedule(_RecordingHandler(), str(folder), recursive=False)
            seen.add(str(folder))
            log(f"watching for screen recordings: {folder}")
    if seen:
        observer.daemon = True
        observer.start()


# ---------------------------------------------------------------- generic downloader ------


SEGMENT_THRESHOLD = 1 * 1024 * 1024
SEGMENT_COUNT = 8
_segment_lock = threading.Lock()


def _probe_ranges(url, headers):
    try:
        h = requests.head(url, headers=headers, allow_redirects=True,
                          timeout=STATE["connection_timeout"],
                          proxies=settings.proxies_for(url))
        size = int(h.headers.get("Content-Length", 0))
        ranges = "bytes" in h.headers.get("Accept-Ranges", "").lower()
        return size, ranges
    except Exception:
        return 0, False


def _download_segment(job, url, idx, start, end, headers, part_path):
    h = dict(headers)
    h["Range"] = f"bytes={start}-{end}"
    try:
        with requests.get(url, headers=h, stream=True,
                           timeout=STATE["connection_timeout"],
                           proxies=settings.proxies_for(url)) as r:
            if r.status_code != 206:
                raise RuntimeError(f"segment {idx} got HTTP {r.status_code}, expected 206")
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if job["stop_evt"].is_set():
                        return
                    if job["pause_evt"].is_set():
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    LIMITER.consume(len(chunk))
                    with _segment_lock:
                        job["size_done"] += len(chunk)
    except Exception as e:
        job["error"] = str(e)
        raise


def _generic_headers(job):
    headers = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if job.get("user_agent"):
        headers["User-Agent"] = job["user_agent"]
    return headers


def run_generic_segmented(job_id):
    job = JOBS[job_id]
    dest = _job_dest(job)
    headers = _generic_headers(job)

    total, ranges_ok = _probe_ranges(job["url"], headers)
    if not ranges_ok or total < SEGMENT_THRESHOLD:
        return _run_generic_single(job_id)

    seg_size = total // SEGMENT_COUNT
    parts = []
    threads = []
    job["size_total"] = total
    job["size_done"] = 0
    transition(job, JobEvent.START)
    for i in range(SEGMENT_COUNT):
        start = i * seg_size
        end = total - 1 if i == SEGMENT_COUNT - 1 else start + seg_size - 1
        part = dest.with_suffix(dest.suffix + f".part{i}")
        parts.append(part)
        t = threading.Thread(target=_download_segment,
                             args=(job, job["url"], i, start, end, headers, part),
                             daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    if job["stop_evt"].is_set():
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        transition(job, JobEvent.STOP)
        log(f"job {job_id}: stopped")
        save_jobs_snapshot()
        return
    if job.get("error"):
        transition(job, JobEvent.FAIL)
        log(f"job {job_id}: FAILED (segmented) — {job['error']}")
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        return
    if job["pause_evt"].is_set():
        # Segments can't pause mid-flight without corrupting the merge —
        # drop the partial parts and let Resume restart the download cleanly.
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        transition(job, JobEvent.PAUSE)
        job["size_done"] = 0
        log(f"job {job_id}: paused (segmented downloads restart on resume)")
        save_jobs_snapshot()
        return
    with open(dest, "wb") as out:
        for p in parts:
            with open(p, "rb") as inp:
                shutil.copyfileobj(inp, out)
            p.unlink()
    # Many CDNs misreport Content-Length — warn, don't fail.
    actual = dest.stat().st_size
    if total and actual < total - 1:
        log(f"job {job_id}: WARNING — size mismatch (got {actual}, expected {total})")
        job["error"] = f"size mismatch: {actual}/{total}"
    dest = maybe_convert_target(job, dest)
    transition(job, JobEvent.COMPLETE)
    job["completed_ts"] = time.time()
    job["speed"] = ""
    record_stat(job["size_done"])
    log(f"job {job_id}: complete ✓ ({dest.name}, {SEGMENT_COUNT}x)")
    scan_file(dest)
    maybe_extract_archive(job, dest)
    maybe_open_folder(dest)
    maybe_play_sound()
    maybe_shutdown_if_idle()
    save_jobs_snapshot()


def run_generic(job_id):
    job = JOBS[job_id]
    dest = _job_dest(job)
    part = dest.with_suffix(dest.suffix + ".part")
    existing = part.stat().st_size if part.exists() else 0
    # Multi-connection segmented download only makes sense from byte 0 —
    # a resumed .part file always continues single-stream with a Range header.
    if existing == 0:
        try:
            total, ok = _probe_ranges(job["url"], _generic_headers(job))
            if ok and total >= SEGMENT_THRESHOLD:
                return run_generic_segmented(job_id)
        except Exception:
            pass
    return _run_generic_single(job_id)


def _run_generic_single(job_id):
    job = JOBS[job_id]
    dest = _job_dest(job)
    part = dest.with_suffix(dest.suffix + ".part")
    headers = _generic_headers(job)

    existing = part.stat().st_size if part.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"

    try:
        with requests.get(job["url"], headers=headers, stream=True,
                          timeout=STATE["connection_timeout"],
                          proxies=settings.proxies_for(job["url"])) as r:
            if r.status_code not in (200, 206):
                transition(job, JobEvent.FAIL)
                job["error"] = f"HTTP {r.status_code}"
                log(f"job {job_id}: server returned {r.status_code}")
                return
            if r.status_code == 200 and existing:
                log(f"job {job_id}: server ignored Range, restarting from 0")
                existing = 0
            total = int(r.headers.get("Content-Length", 0)) + existing
            job["size_total"] = total
            job["size_done"] = existing
            mode = "ab" if existing else "wb"
            last_report = time.time()
            last_bytes = existing
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if job["stop_evt"].is_set():
                        transition(job, JobEvent.STOP)
                        log(f"job {job_id}: stopped")
                        return
                    if job["pause_evt"].is_set():
                        transition(job, JobEvent.PAUSE)
                        log(f"job {job_id}: paused")
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    LIMITER.consume(len(chunk))
                    job["size_done"] += len(chunk)
                    now = time.time()
                    if now - last_report >= 1:
                        rate = (job["size_done"] - last_bytes) / (now - last_report)
                        job["speed"] = f"{rate/1024:.0f} KB/s"
                        last_report, last_bytes = now, job["size_done"]
            try:
                os.replace(part, dest)
            except OSError:
                # Per-category overrides can point at a different drive,
                # where os.replace fails across filesystems.
                import shutil as _sh
                _sh.move(str(part), str(dest))
            # Many CDNs misreport Content-Length — warn, don't fail.
            actual = dest.stat().st_size
            if job["size_total"] and actual < job["size_total"] - 1:
                log(f"job {job_id}: WARNING — size mismatch (got {actual}, "
                    f"expected {job['size_total']})")
                job["error"] = f"size mismatch: {actual}/{job['size_total']}"
            dest = maybe_convert_target(job, dest)
            transition(job, JobEvent.COMPLETE)
            job["completed_ts"] = time.time()
            job["speed"] = ""
            record_stat(job["size_done"])
            log(f"job {job_id}: complete ✓ ({dest.name})")
            scan_file(dest)
            maybe_extract_archive(job, dest)
            maybe_open_folder(dest)
            maybe_play_sound()
            maybe_shutdown_if_idle()
    except Exception as e:
        transition(job, JobEvent.FAIL)
        job["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")
    finally:
        save_jobs_snapshot()



def run_ytdlp(job_id):
    """Thin orchestrator: routes through the engine interface (Session 7)."""
    job = JOBS[job_id]
    headers = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if job.get("user_agent"):
        headers["User-Agent"] = job["user_agent"]
    dest = _job_dest(job)

    def progress_cb(d):
        while job["pause_evt"].is_set():
            transition(job, JobEvent.PAUSE)
            if job["stop_evt"].is_set():
                raise DownloadCancelled()
            time.sleep(0.5)
        if job["stop_evt"].is_set():
            raise DownloadCancelled()
        if d["status"] == "downloading":
            transition(job, JobEvent.START)
            job["size_total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            job["size_done"] = d.get("downloaded_bytes", 0)
            job["speed"] = f"{(d.get('speed') or 0)/1024:.0f} KB/s"
        elif d["status"] == "finished":
            job["speed"] = ""

    def cancel_check():
        # Lets a Stop/Delete abort the YouTube pacing wait.
        if job["stop_evt"].is_set():
            raise DownloadCancelled()

    from engines import route_for
    engine = route_for(job["url"])[0]
    opts = {"headers": headers,
            "job_id": job_id,
            "cancel_check": cancel_check,
            "use_title": bool(job.get("auto_name")) and not job.get("download_playlist"),
            "no_archive": bool(job.get("no_archive")),
            "download_playlist": job.get("download_playlist", False),
            "playlist_item": bool(job.get("playlist_id")),
            "target_format": (job.get("target_format") or "").lower(),
            "speed_limit_kbps": effective_speed_limit_kbps()}
    transition(job, JobEvent.START)
    try:
        real = engine.download(job["url"], job.get("format_id"), dest, opts, progress_cb)
    except DownloadCancelled:
        transition(job, JobEvent.STOP)
        log(f"job {job_id}: stopped")
        save_jobs_snapshot()
        return
    except Exception as e:
        # Fall-through: if yt-dlp has no extractor for this URL, don't
        # fail the job — hand it to the generic HTTP engine instead.
        # Almost any URL that returns bytes can be fetched that way.
        raw = str(e)
        low = raw.lower()
        is_unsupported = (
            "unsupported url" in low
            or "no suitable extractor" in low
            or "extractor_error" in low
        )
        # Only fall through when the URL itself looks like a file fetch,
        # not when yt-dlp failed on a real media site (e.g. a private
        # YouTube video) — a generic retry would just 403 there.
        if is_unsupported and detect_type(job["url"]) == "generic":
            log(f"job {job_id}: yt-dlp has no extractor, falling through "
                f"to generic HTTP ({raw[:120]})")
            # Reset state: engine.download may have touched counters.
            job["error_kind"] = None
            job["error"] = None
            transition(job, JobEvent.FAIL)  # move out of downloading cleanly
            try:
                transition(job, JobEvent.RESUME)  # -> queued
            except ValueError:
                # If FAIL->RESUME isn't legal from this state, force-requeue.
                repair_status(job, "queued")
            job["type"] = "generic"
            # Generic single-shot downloader runs synchronously here.
            run_generic(job_id)
            return
        _handle_ytdlp_failure(job_id, job, e)
        return
    try:
        job["filename"] = real.name
        # maybe_convert_target falls back to auto-mp4 when the job has no
        # explicit target, and honors target_format even when auto_mp4 is off.
        real = maybe_convert_target(job, real)
        job["filename"] = real.name
        job["size_total"] = job["size_done"] = real.stat().st_size
    except Exception as e:
        log(f"job {job_id}: post-download filename fixup failed: {e}")
    transition(job, JobEvent.COMPLETE)
    job["completed_ts"] = time.time()
    job["retry_count"] = 0
    job["retry_after"] = 0
    log(f"job {job_id}: complete ✓ ({job['filename']})")
    if job.get("download_playlist"):
        # Legacy whole-playlist job: remember which folder it wrote into
        # so the GUI's Open can find it.
        job["playlist_dir"] = resolve_playlist_dir(job)
    try:
        d = stat_for(job)
        if d.exists():
            record_stat(job["size_done"])
            scan_file(d)
            # D3: once per playlist batch, on the last item.
            if not _playlist_group_pending(job):
                maybe_open_folder(d)
                maybe_play_sound()
    except Exception:
        pass
    maybe_shutdown_if_idle()
    save_jobs_snapshot()


# Auto-retry schedule (seconds), indexed by attempt number. rate_limit waits
# long because the IP is being throttled; network blips retry quickly.
_RETRY_BACKOFF = {"rate_limit": (120, 300, 600), "network": (15, 45, 135)}


def _handle_ytdlp_failure(job_id, job, exc):
    """Classify a yt-dlp failure and either auto-retry, hold YouTube, or fail."""
    raw = str(exc)
    kind = classify_ytdlp_error(raw)
    job["error_kind"] = kind
    youtube = _is_youtube_url(job["url"])

    if kind == "bot_check":
        # Never auto-retry: retrying a bot check digs the hole deeper.
        mins = max(1, int(STATE.get("ytdlp_bot_cooldown_minutes", 30) or 30))
        if youtube:
            set_youtube_cooldown(mins * 60, "bot check")
        transition(job, JobEvent.FAIL)
        job["error"] = (f"YouTube flagged this connection (bot check). YouTube jobs "
                        f"are held for {mins} min. Switch network/VPN, then press "
                        f"Resume to try again now. [{raw[:200]}]")
        log(f"job {job_id}: FAILED (bot check) — YouTube held {mins} min")
        return

    schedule = _RETRY_BACKOFF.get(kind)
    attempt = int(job.get("retry_count") or 0)
    max_retries = max(0, int(STATE.get("max_retries", 3) or 0))
    if (schedule and attempt < max_retries and not job["stop_evt"].is_set()):
        delay = schedule[min(attempt, len(schedule) - 1)]
        try:
            transition(job, JobEvent.RETRY)
        except ValueError:
            transition(job, JobEvent.FAIL)
            job["error"] = raw
            log(f"job {job_id}: FAILED — {raw}")
            return
        job["retry_count"] = attempt + 1
        job["retry_after"] = time.time() + delay
        job["speed"] = ""
        job["error"] = None
        if kind == "rate_limit" and youtube:
            set_youtube_cooldown(delay, "rate limited (429)")
        log(f"job {job_id}: {kind} error, auto-retry {attempt + 1}/{max_retries} "
            f"in {delay}s — {raw[:160]}")
        return

    transition(job, JobEvent.FAIL)
    job["error"] = raw
    log(f"job {job_id}: FAILED ({kind}) — {raw}")


# job_id -> worker Thread. Private to this module and never serialized (job
# dicts go straight to JSON, so the Thread can't live on the job itself).
# api.py's resume route uses it to tell a paused job whose yt-dlp thread is
# still parked in progress_cb from one whose thread has already exited.
_WORKERS = {}


def start_job_thread(job_id):
    job = JOBS[job_id]
    job["pause_evt"].clear()
    job["stop_evt"].clear()
    job["error"] = None
    transition(job, JobEvent.START)
    target = run_generic if job["type"] == "generic" else run_ytdlp
    t = threading.Thread(target=target, args=(job_id,), daemon=True)
    _WORKERS[job_id] = t
    t.start()


_V5_SERVICES_STARTED = False
_DISK_STATE = {"checked": 0.0, "low": False, "free_mb": 0}


def _start_v5_services():
    """Background update scheduler; started once from the dispatcher thread."""
    global _V5_SERVICES_STARTED
    if _V5_SERVICES_STARTED:
        return
    _V5_SERVICES_STARTED = True
    try:
        import updater
        updater.start_update_scheduler()
    except Exception as e:
        log(f"update scheduler not started: {e}")


def low_disk():
    """True when free space on the output drive is below min_free_space_mb.
    Cached for 10 s so the 1 Hz dispatcher doesn't stat the disk each tick."""
    now = time.time()
    if now - _DISK_STATE["checked"] < 10:
        return _DISK_STATE["low"]
    _DISK_STATE["checked"] = now
    try:
        floor = int(STATE.get("min_free_space_mb", 500) or 0)
    except (TypeError, ValueError):
        floor = 0
    free_mb = settings.disk_usage_for()["free"] // (1024 * 1024)
    low = bool(floor) and free_mb < floor
    if low != _DISK_STATE["low"]:
        log(f"low disk space: {free_mb} MB free (limit {floor} MB), new downloads paused"
            if low else f"disk space recovered: {free_mb} MB free, downloads resume")
    _DISK_STATE.update(low=low, free_mb=free_mb)
    return low


def queue_status():
    """Summary for /queue-status (extension badge, future GUI indicators)."""
    jobs = list(JOBS.values())
    now = time.time()
    low_disk()  # refresh cache
    return {
        "active": sum(1 for j in jobs if j["status"] == "downloading"),
        "queued": sum(1 for j in jobs if j["status"] == "queued"),
        "retrying": sum(1 for j in jobs if j["status"] == "queued"
                        and (j.get("retry_after") or 0) > now),
        "youtube_hold_s": int(youtube_cooldown_remaining()),
        "low_disk": _DISK_STATE["low"],
        "free_mb": _DISK_STATE["free_mb"],
    }


def dispatcher_loop():
    _start_v5_services()
    while True:
        time.sleep(1)
        # An uncaught exception here used to end this thread silently and
        # the queue never started another job until the app was restarted
        # (e.g. a job deleted or stopped between the status check and
        # start_job_thread).
        try:
            for jid in [k for k, t in list(_WORKERS.items()) if not t.is_alive()]:
                _WORKERS.pop(jid, None)
            if not STATE["queue_running"]:
                continue
            if low_disk():
                continue  # don't start new jobs on a nearly full drive
            active = sum(1 for j in list(JOBS.values()) if j["status"] == "downloading")
            slots = STATE["max_concurrent"] - active
            if slots <= 0:
                continue
            started = 0
            yt_hold = youtube_cooldown_remaining() > 0
            for jid, j in sorted(list(JOBS.items()), key=lambda kv: start_order_key(kv[1])):
                if started >= slots:
                    break
                if j["status"] == "queued":
                    # v5: honor auto-retry delays and the YouTube hold.
                    # Skipped jobs stay queued and don't use up a slot.
                    if (j.get("retry_after") or 0) > time.time():
                        continue
                    if (yt_hold and j.get("type") != "generic"
                            and _is_youtube_url(j.get("url"))):
                        continue
                    try:
                        start_job_thread(jid)
                        started += 1
                    except Exception as e:
                        log(f"dispatcher: couldn't start job {jid}: {e}")
        except Exception as e:
            log(f"dispatcher loop error (continuing): {e}")




def _repair_recording(path):
    """MediaRecorder produces fragmented WebM that Windows Media Player
    rejects. Remux with ffmpeg, forcing it to synthesize missing timestamps.
    Returns the path to the repaired file (equals `path` on failure)."""
    _ffdir = find_ffmpeg_dir()
    ffmpeg = str(Path(_ffdir) / "ffmpeg.exe") if _ffdir else "ffmpeg"
    fixed = path.with_name(path.stem + ".fixed" + path.suffix)
    cmd = [ffmpeg, "-y", "-fflags", "+genpts", "-i", str(path),
           "-c", "copy", str(fixed)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120,
                           creationflags=_CREATE_NO_WINDOW)
        if r.returncode == 0 and fixed.exists() and fixed.stat().st_size > 0:
            os.replace(str(fixed), str(path))
            log(f"recording repaired: {path.name}")
            return path
        err = (r.stderr or b"")[-300:].decode(errors="replace")
        log(f"recording repair failed (rc={r.returncode}): {err}")
    except Exception as e:
        log(f"recording repair failed: {e}")
    if fixed.exists():
        try:
            fixed.unlink()
        except OSError:
            pass
    return path


def _probe_recording(path):
    """Run ffprobe on an uploaded recording. Returns (ok, reason)."""
    _ffdir = find_ffmpeg_dir()
    ffprobe = str(Path(_ffdir) / "ffprobe.exe") if _ffdir else "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=30, text=True, errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:200] or "ffprobe failed"
        try:
            dur = float((r.stdout or "").strip())
            if dur <= 0:
                return False, "zero duration"
        except (TypeError, ValueError):
            return False, "unreadable duration"
        return True, ""
    except Exception as e:
        return False, str(e)

