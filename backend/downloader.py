"""Download engines (generic HTTP + yt-dlp), ffmpeg helpers, recording watcher."""
from pathlib import Path
import glob
import os
import requests
from yt_dlp import YoutubeDL
from engines import (route_for, DownloadCancelled,
                     _ytdlp_version_check, find_js_runtime)
import shutil
import subprocess
import sys
import threading
import time

import settings
from settings import (STATE, load_settings, save_settings, safe_filename, _unique_path,
                      guess_ext_from_head, stat_for, _dest_for,
                      effective_speed_limit_kbps, LIMITER, get_shutdown_pending,
                      set_shutdown_pending)
from jobs import (JOBS, new_job, save_jobs_snapshot, _fire_new_job_hooks,
                  transition, JobEvent, JobPhase, set_phase)
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
    ffmpeg = "ffmpeg"
    if getattr(sys, "frozen", False):
        ffmpeg = str(Path(sys._MEIPASS) / "ffmpeg.exe")
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


def maybe_convert_to_mp4(job, dest):
    """If auto_mp4 is on and the file isn't already mp4, remux (fast) or
    transcode (fallback) with ffmpeg. Returns the final path."""
    if not STATE.get("auto_mp4"):
        return dest
    ext = dest.suffix.lower()
    video_exts = {".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".flv"}
    # Extensionless files are treated as unknown-video and probed, not skipped.
    if ext == ".mp4" or (ext and ext not in video_exts):
        return dest
    ffmpeg = "ffmpeg"
    if getattr(sys, "frozen", False):
        ffmpeg = str(Path(sys._MEIPASS) / "ffmpeg.exe")
    log(f"auto-mp4: ffmpeg={ffmpeg} exists={os.path.exists(ffmpeg)} "
        f"input={dest.name} (ext={ext or '(none)'})")
    target = dest.with_suffix(".mp4")
    log(f"auto-mp4: output → {target}")
    # Confirm a video stream actually exists before running a full conversion.
    try:
        r = subprocess.run([ffmpeg, "-i", str(dest)], capture_output=True, timeout=60,
                           creationflags=_CREATE_NO_WINDOW)
        if b": Video:" not in r.stderr:
            log(f"auto-mp4: {dest.name} has no video stream, skipping")
            return dest
    except Exception as e:
        log(f"auto-mp4: stream probe failed for {dest.name}: {e}")
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
                          timeout=STATE["connection_timeout"])
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
                           timeout=STATE["connection_timeout"]) as r:
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
                          timeout=STATE["connection_timeout"]) as r:
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

    from engines import route_for
    engine = route_for(job["url"])[0]
    opts = {"headers": headers,
            "job_id": job_id,
            "download_playlist": job.get("download_playlist", False),
            "target_format": (job.get("target_format") or "").lower(),
            "speed_limit_kbps": effective_speed_limit_kbps()}
    transition(job, JobEvent.START)
    try:
        real = engine.download(job["url"], job.get("format_id"), dest, opts, progress_cb)
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
        log(f"job {job_id}: complete ✓ ({job['filename']})")
        try:
            d = stat_for(job)
            if d.exists():
                record_stat(job["size_done"])
                scan_file(d)
                maybe_open_folder(d)
                maybe_play_sound()
        except Exception:
            pass
        maybe_shutdown_if_idle()
    except DownloadCancelled:
        transition(job, JobEvent.STOP)
        log(f"job {job_id}: stopped")
    except Exception as e:
        transition(job, JobEvent.FAIL)
        job["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")
    finally:
        save_jobs_snapshot()


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


def dispatcher_loop():
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
            active = sum(1 for j in list(JOBS.values()) if j["status"] == "downloading")
            slots = STATE["max_concurrent"] - active
            if slots <= 0:
                continue
            started = 0
            for jid, j in list(JOBS.items()):
                if started >= slots:
                    break
                if j["status"] == "queued":
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
    ffmpeg = "ffmpeg"
    if getattr(sys, "frozen", False):
        ffmpeg = str(Path(sys._MEIPASS) / "ffmpeg.exe")
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
    ffprobe = "ffprobe"
    if getattr(sys, "frozen", False):
        ffprobe = str(Path(sys._MEIPASS) / "ffprobe.exe")
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

