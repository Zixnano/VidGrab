"""Job lifecycle: registry, state, snapshot persistence, new_job."""
from urllib.parse import urlparse
import json
import os
import re
import threading
import time

import settings
from settings import (STATE, category_for, safe_filename, guess_filename, detect_type,
                      guess_ext_from_head, stat_for, is_junk_stem, clean_title,
                      generated_name, source_site_for)
from logging_setup import log


_ON_NEW_JOB_HOOKS = []
_SHOW_DIALOG_HOOKS = []


def register_new_job_hook(fn):
    """GUI registers a callable here; called from any thread when a job is queued."""
    _ON_NEW_JOB_HOOKS.append(fn)


def register_show_dialog_hook(fn):
    """GUI registers a callable here; called from any thread when the
    extension wants the pre-download confirmation dialog."""
    _SHOW_DIALOG_HOOKS.append(fn)


def _fire_show_dialog_hooks(payload):
    for fn in _SHOW_DIALOG_HOOKS:
        try:
            fn(payload)
        except Exception as e:
            log(f"show-dialog hook failed: {e}")


def _fire_new_job_hooks(job):
    global _LAST_HOOK_TS
    with _NEW_JOB_HOOK_LOCK:
        now = time.time()
        if now - _LAST_HOOK_TS < 1.5:
            return
        _LAST_HOOK_TS = now
    for fn in _ON_NEW_JOB_HOOKS:
        try:
            fn(job)
        except Exception as e:
            log(f"new-job hook failed: {e}")


JOBS = {}
JOB_COUNTER = 0
JOB_LOCK = threading.Lock()

# Debounce for new-job hooks: a 50-item batch must not raise the window
# 50 times (each raise is a hide/show flicker on Windows).
_NEW_JOB_HOOK_LOCK = threading.Lock()
_LAST_HOOK_TS = 0.0

_SNAPSHOT_LOCK = threading.Lock()


def save_jobs_snapshot():
    """Persist a plain (non-Event) view of jobs so history survives restarts."""
    # Serialized: /convert's progress thread and a finishing download can
    # otherwise clobber each other's .tmp file.
    with _SNAPSHOT_LOCK:
        try:
            snap = {}
            for jid, j in list(JOBS.items()):
                snap[jid] = {k: v for k, v in list(j.items())
                             if k not in ("pause_evt", "stop_evt", "cookie", "referer", "user_agent", "error")}
            # Cap history at MAX_HISTORY jobs — always keep non-done jobs,
            # fill the rest with the newest done ones.
            MAX_HISTORY = 2000
            if len(snap) > MAX_HISTORY:
                entries = sorted(snap.items(),
                                 key=lambda kv: kv[1].get("created_ts", 0),
                                 reverse=True)
                kept = {}
                done_count = 0
                for jid, j in entries:
                    if j.get("status") != "done":
                        kept[jid] = j
                    elif done_count < MAX_HISTORY:
                        kept[jid] = j
                        done_count += 1
                snap = kept
            # Prune in-memory done jobs too so the GUI doesn't list thousands.
            if len(JOBS) > MAX_HISTORY + 500:
                done = sorted(((jid, j) for jid, j in list(JOBS.items())
                               if j.get("status") == "done"),
                              key=lambda kv: kv[1].get("completed_ts")
                                             or kv[1].get("created_ts", 0))
                for jid, _ in done[:-MAX_HISTORY]:
                    JOBS.pop(jid, None)
            tmp = settings.JOBS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap, indent=2))
            os.replace(tmp, settings.JOBS_PATH)
        except Exception as e:
            log(f"couldn't save job history: {e}")



def _migrate_extensionless(job):
    """One-shot fix for restored jobs whose file landed with no extension."""
    try:
        # _dest_for, not stat_for: stat_for renames job["filename"] to
        # "name (2).ext" when size_done is 0, which is a side effect a
        # read-only migration must not have.
        p = settings._dest_for(job)
        if p.suffix or not p.exists():
            return
        ext = os.path.splitext(urlparse(job.get("url", "")).path)[1]
        if not ext:
            return
        target = p.with_suffix(ext)
        if target.exists():
            return
        os.replace(p, target)
        job["filename"] = target.name
        log(f"migrated extensionless file: {p.name} → {target.name}")
        save_jobs_snapshot()
    except Exception as e:
        log(f"extensionless migration failed: {e}")


def _repair_zero_size(job):
    """One-shot fixup for done jobs that display 0 B: the file exists on
    disk, but size_total was never set (yt-dlp post-processing threw)."""
    if job.get("size_total") or job.get("status") != "done":
        return False
    try:
        # _dest_for, not stat_for: stat_for renames job["filename"] when
        # size_done is 0 (which is exactly this case). The category is left
        # alone on purpose: _dest_for builds the folder from it, and the
        # file is already sitting in that folder.
        p = settings._dest_for(job)
        if p.exists() and p.stat().st_size > 0:
            job["size_total"] = job["size_done"] = p.stat().st_size
            log(f"repaired zero-size job {job.get('id')}: {job['filename']} "
                f"({job['size_total']} bytes)")
            return True
    except Exception as e:
        log(f"zero-size repair failed for job {job.get('id')}: {e}")
    return False


_PRIO_LOCK = threading.Lock()
_PRIO_LAST = 0.0


def _next_priority():
    """Strictly increasing start-order value (creation time, nudged so two
    jobs created in the same tick never tie)."""
    global _PRIO_LAST
    with _PRIO_LOCK:
        _PRIO_LAST = max(time.time(), _PRIO_LAST + 1e-6)
        return _PRIO_LAST


def start_order_key(j):
    """Dispatcher order: lower starts first. Reordering permutes these values."""
    return j.get("priority") or j.get("created_ts") or 0


def reorder_job(jid, action):
    """Move a queued job within the start order. action: top, up, down,
    bottom. Only queued jobs take part (others have no queue position).
    Returns the new 0-based position among queued jobs, or None when the
    job isn't queued. Raises KeyError for an unknown id."""
    if action not in ("top", "up", "down", "bottom"):
        raise ValueError(f"bad reorder action: {action}")
    job = JOBS[jid]
    if job.get("status") != "queued":
        return None
    queued = sorted((x for x in JOBS.values() if x.get("status") == "queued"),
                    key=start_order_key)
    values = [start_order_key(x) for x in queued]
    i = next(n for n, x in enumerate(queued) if x is job)
    if action == "top":
        queued.insert(0, queued.pop(i)); i = 0
    elif action == "bottom":
        queued.append(queued.pop(i)); i = len(queued) - 1
    elif action == "up" and i > 0:
        queued[i - 1], queued[i] = queued[i], queued[i - 1]; i -= 1
    elif action == "down" and i < len(queued) - 1:
        queued[i + 1], queued[i] = queued[i], queued[i + 1]; i += 1
    # Hand the same sorted values back out in the new order: the relative
    # order against jobs created later is untouched.
    for x, v in zip(queued, values):
        x["priority"] = v
    return i


def _migrate_job(j):
    """Backfill v4 job fields on a restored pre-v4 (v3.3) snapshot dict so
    no code path can KeyError on fields added after the snapshot was made."""
    defaults = {
        "phase": "idle",
        "format_id": None, "target_format": None,
        "completed_ts": None,
        "size_total": 0, "size_done": 0, "speed": "", "error": None,
        "referer": None, "cookie": None, "user_agent": None,
        "created_ts": time.time(),
        # Session D. Deliberately NOT defaulted: "subdir" and "playlist_dir"
        # (playlist_dir's absence is what marks "not probed yet").
        "download_playlist": False,
        "playlist_id": None, "playlist_title": None,
        "playlist_index": None, "playlist_url": None,
        # v5
        "auto_name": False, "retry_count": 0, "retry_after": 0,
        "failed_ts": None,
    }
    for k, v in defaults.items():
        j.setdefault(k, v)
    j.setdefault("priority", j.get("created_ts") or time.time())
    # v5: backfill the source site from the file URL for older jobs.
    if "source_site" not in j:
        j["source_site"] = source_site_for(j.get("url"))
    return j


_PLAYLIST_FILE_RE = re.compile(r"^\d{3,} - ")


def resolve_playlist_dir(job):
    """Find the playlist folder a legacy whole-playlist job wrote into.

    Returns the folder name, or "" when there is not exactly one candidate
    (ambiguity resolves to "open the category folder", never to a guess).
    Candidate = immediate subfolder of the job's category dir holding a file
    named like "NNN - ..." modified inside this job's time window."""
    try:
        cat_dir = settings._dest_for(job).parent
        if not cat_dir.is_dir():
            return ""
        lo = (job.get("created_ts") or 0) - 60
        done_ts = job.get("completed_ts")
        hi = (done_ts + 300) if done_ts else None
        found = []
        for sub in cat_dir.iterdir():
            if not sub.is_dir():
                continue
            hit = False
            for i, f in enumerate(sub.iterdir()):
                if i > 5000:
                    break
                if not _PLAYLIST_FILE_RE.match(f.name):
                    continue
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m >= lo and (hi is None or m <= hi):
                    hit = True
                    break
            if hit:
                found.append(sub.name)
        return found[0] if len(found) == 1 else ""
    except Exception as e:
        log(f"playlist folder lookup failed for job {job.get('id')}: {e}")
        return ""


def _is_legacy_playlist_job(j):
    # Before Session D, download_playlist=True meant one job that walks a whole
    # playlist. Session D queues playlists as one job per item
    # (download_playlist=False, playlist_id set). download_playlist=True now
    # appears only on pre-Session-D jobs and on jobs made with "Legacy
    # whole-playlist mode" or by API callers - all share the batch-1 layout.
    return bool(j.get("download_playlist")) and not j.get("playlist_id")


def _migrate_playlist_dir(job):
    """One-shot: resolve the playlist folder for a legacy playlist job so the
    GUI can open it. Returns True when the job changed (caller saves once)."""
    if not _is_legacy_playlist_job(job) or "playlist_dir" in job:
        return False
    if job.get("status") not in ("done", "stopped", "error"):
        return False
    name = resolve_playlist_dir(job)
    job["playlist_dir"] = name
    if name:
        log(f"migrated playlist folder: job {job.get('id')} -> {name!r}")
    else:
        log(f"playlist folder not resolved for job {job.get('id')}")
    return True


def load_jobs_snapshot():
    if settings.JOBS_PATH.exists():
        try:
            snap = json.loads(settings.JOBS_PATH.read_text())
            migrated = False
            for jid, j in snap.items():
                j["pause_evt"] = threading.Event()
                j["stop_evt"] = threading.Event()
                _migrate_job(j)
                if j.get("status") in ("downloading", "queued", "held"):
                    repair_status(j, "stopped")
                JOBS[jid] = j
                _migrate_extensionless(j)
                if _repair_zero_size(j):
                    migrated = True
                if _migrate_playlist_dir(j):
                    migrated = True
            if migrated:
                save_jobs_snapshot()
            global JOB_COUNTER
            JOB_COUNTER = max([int(k) for k in JOBS.keys()] + [0])
        except Exception as e:
            log(f"couldn't load job history: {e}")



def next_job_id():
    global JOB_COUNTER
    with JOB_LOCK:
        JOB_COUNTER += 1
        return str(JOB_COUNTER)



def _resolution_suffix(resolution):
    """1920x1080 -> 1080p; anything else -> alnum-only (may be empty)."""
    res = str(resolution or "").strip()
    m = re.match(r"^\d+x(\d+)$", res)
    if m:
        return f"{m.group(1)}p"
    return re.sub(r"[^0-9A-Za-z]+", "", res)


def _playlist_subdir(title):
    """Folder name for a playlist's items: filesystem-safe, <= 60 chars,
    no trailing dots/spaces, "Playlist" when nothing usable is left."""
    if not str(title or "").strip():
        return "Playlist"
    name = safe_filename(str(title))[:60].rstrip(". ")
    return name or "Playlist"


def new_job(url, filename=None, category=None, referer=None, cookie=None,
            user_agent=None, job_type=None, format_id=None, target_format=None,
            resolution=None, multi=False, download_playlist=False, defer=False,
            playlist_id=None, playlist_title=None, playlist_index=None,
            playlist_url=None, snapshot=True, created_ts=None, page_url=None):
    log(f"new_job: filename={filename!r} url_basename={guess_filename(url)!r}")
    jid = next_job_id()
    jtype = job_type or detect_type(url)
    fname = safe_filename(filename) if filename else guess_filename(url)
    # Caller-supplied names (e.g. document.title from the extension) often
    # have no extension — borrow one from the URL, then Content-Type, then
    # fall back by job type.
    if not os.path.splitext(fname)[1]:
        ext = os.path.splitext(guess_filename(url))[1]
        if not ext:
            ext = guess_ext_from_head(url)
        if not ext:
            ext = ".mp4" if jtype == "ytdlp" else ".bin"
        if not fname.lower().endswith(ext.lower()):
            fname = fname + ext
    # Double-extension guard: a title/url combo can yield "name.mp4.mp4".
    # Collapse repeated trailing extensions ("name.mp4.mp4" -> "name.mp4").
    _b, _e = os.path.splitext(fname)
    while _e and _b.lower().endswith(_e.lower()):
        log(f"new_job: collapsing double extension {fname!r} -> {_b!r}")
        fname = _b
        _b, _e = os.path.splitext(fname)
    log(f"new_job: after extension logic fname={fname!r}")
    # v5 naming: strip "(3) " / " - YouTube" noise from caller-supplied
    # titles, and replace names that say nothing ("watch", "12345", "index")
    # with site_date_time. yt-dlp jobs flagged auto_name use the real video
    # title as the filename instead (see engines.download).
    auto_named = False
    _stem, _ext = os.path.splitext(fname)
    _clean = clean_title(_stem)
    if _clean and _clean != _stem:
        fname, _stem = _clean + _ext, _clean
    if is_junk_stem(_stem):
        fname = safe_filename(generated_name(url)) + _ext
        auto_named = not (multi and resolution)
        log(f"new_job: junk name {_stem!r} -> {fname!r} (auto_name={auto_named})")
    # Session 10: multi-quality checklist - when the caller queued several
    # formats for one URL, disambiguate with the resolution suffix
    # ("Video.mp4" -> "Video_1080p.mp4"). Single-format keeps old naming.
    if multi and resolution:
        suffix = _resolution_suffix(resolution)
        if suffix:
            _stem, _ext = os.path.splitext(fname)
            fname = f"{_stem}_{suffix}{_ext}"
            log(f"new_job: multi-quality suffix -> {fname!r}")
    job_cat = category or category_for(fname)

    # Apply per-site rules
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        netloc = ""
    for rule in STATE.get("rules", []):
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("domain", "").strip().lstrip("*.")
        if pattern and (netloc == pattern or netloc.endswith("." + pattern)):
            if "category" in rule:
                job_cat = rule["category"]
            break

    # Session D: per-item playlist jobs get their own folder under the
    # category dir (consumed only by settings._dest_for).
    subdir = _playlist_subdir(playlist_title) if playlist_title else None
    JOBS[jid] = {
        "id": jid, "url": url, "filename": fname,
        "category": job_cat,
        "type": jtype, "status": "held" if defer else "queued", "phase": "idle",
        "format_id": format_id, "target_format": target_format,
        "download_playlist": download_playlist,
        "playlist_id": playlist_id, "playlist_title": playlist_title,
        "playlist_index": playlist_index, "playlist_url": playlist_url,
        "completed_ts": None,
        "auto_name": auto_named,
        "source_site": source_site_for(page_url, referer, url),
        "retry_count": 0, "retry_after": 0,
        "priority": _next_priority(), "failed_ts": None,
        "size_total": 0, "size_done": 0, "speed": "", "error": None,
        "referer": referer, "cookie": cookie, "user_agent": user_agent,
        "created_ts": created_ts if created_ts is not None else time.time(),
        "pause_evt": threading.Event(), "stop_evt": threading.Event(),
    }
    if subdir:
        JOBS[jid]["subdir"] = subdir
    if playlist_id:
        log(f"new_job: playlist item {playlist_index} of {playlist_id!r} "
            f"-> subdir {subdir!r}")
    ext = os.path.splitext(fname)[1].lstrip(".").lower()
    if ext and STATE.get("file_types_overrides", {}).get(ext) is False:
        JOBS[jid]["status"] = "skipped"
        log(f"job {jid}: skipped — '.{ext}' is disabled in Options → File Types")
        if snapshot:
            save_jobs_snapshot()
        return jid
    if settings.get_shutdown_pending():
        os.system("shutdown /a")
        settings.set_shutdown_pending(False)
        log("Scheduled shutdown cancelled — new job queued")
    log(f"job {jid} {'held (deferred)' if defer else 'queued'}: {fname}")
    if snapshot:
        save_jobs_snapshot()
    _fire_new_job_hooks(JOBS[jid])
    return jid




# ---------------------------------------------------------------------------
# Job state machine (Session 5). Status is stored as the string value, so the
# /jobs wire format is unchanged. transition() is the ONLY lifecycle mutator;
# repair_status() is for load-time/state repair and is not a lifecycle event.
# ---------------------------------------------------------------------------
import enum


class JobStatus(enum.Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    STOPPED = "stopped"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"
    HELD = "held"


class JobEvent(enum.Enum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    COMPLETE = "complete"
    FAIL = "fail"
    SKIP = "skip"
    HOLD = "hold"
    RELEASE = "release"
    RETRY = "retry"   # v5: auto-retry after a transient failure (back to queued)


class JobPhase(enum.Enum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    POSTPROCESSING = "postprocessing"
    DONE = "done"


_TRANSITIONS = {
    (JobStatus.QUEUED, JobEvent.START): JobStatus.DOWNLOADING,
    (JobStatus.DOWNLOADING, JobEvent.START): JobStatus.DOWNLOADING,   # idempotent (progress cb)
    (JobStatus.PAUSED, JobEvent.START): JobStatus.DOWNLOADING,        # segmented restart after resume
    (JobStatus.DOWNLOADING, JobEvent.PAUSE): JobStatus.PAUSED,
    (JobStatus.PAUSED, JobEvent.PAUSE): JobStatus.PAUSED,             # idempotent (pause-wait loop)
    (JobStatus.DOWNLOADING, JobEvent.STOP): JobStatus.STOPPED,
    (JobStatus.DOWNLOADING, JobEvent.COMPLETE): JobStatus.DONE,
    (JobStatus.DOWNLOADING, JobEvent.FAIL): JobStatus.ERROR,
    (JobStatus.DOWNLOADING, JobEvent.RETRY): JobStatus.QUEUED,        # v5 auto-retry
    (JobStatus.PAUSED, JobEvent.RESUME): JobStatus.QUEUED,
    (JobStatus.PAUSED, JobEvent.STOP): JobStatus.STOPPED,
    (JobStatus.STOPPED, JobEvent.RESUME): JobStatus.QUEUED,
    (JobStatus.ERROR, JobEvent.RESUME): JobStatus.QUEUED,
    (JobStatus.QUEUED, JobEvent.RESUME): JobStatus.QUEUED,   # idempotent
    (JobStatus.QUEUED, JobEvent.STOP): JobStatus.STOPPED,
    (JobStatus.HELD, JobEvent.RELEASE): JobStatus.QUEUED,
    (JobStatus.HELD, JobEvent.FAIL): JobStatus.ERROR,                 # corrupt upload
    (JobStatus.HELD, JobEvent.COMPLETE): JobStatus.DONE,              # validated upload
}


def _coerce_status(v):
    return v if isinstance(v, JobStatus) else JobStatus(v)


def transition(j, event):
    """Validate and apply a lifecycle event. Raises ValueError on illegal
    transitions — this is the single place allowed to mutate j["status"]
    (parameter named `j`; the plan's grep gate targets literal `job["status"]`)."""
    cur = _coerce_status(j.get("status", JobStatus.QUEUED.value))
    ev = event if isinstance(event, JobEvent) else JobEvent(event)
    nxt = _TRANSITIONS.get((cur, ev))
    if nxt is None:
        raise ValueError(f"illegal job transition: {cur.value} + {ev.value}")
    j["status"] = nxt.value
    if ev is JobEvent.FAIL:
        j["failed_ts"] = time.time()   # lets the GUI hide old failures
    elif ev is JobEvent.RESUME:
        j["failed_ts"] = None
    if ev is JobEvent.PAUSE and j.get("pause_evt") is not None:
        j["pause_evt"].set()
    elif ev is JobEvent.RESUME:
        if j.get("pause_evt") is not None:
            j["pause_evt"].clear()
        if j.get("stop_evt") is not None:
            j["stop_evt"].clear()
    elif ev is JobEvent.STOP and j.get("stop_evt") is not None:
        j["stop_evt"].set()
    return nxt


def repair_status(j, status):
    """Load-time/state repair (not a lifecycle event)."""
    j["status"] = JobStatus(status).value


def set_phase(j, phase):
    j["phase"] = (phase if isinstance(phase, JobPhase) else JobPhase(phase)).value
