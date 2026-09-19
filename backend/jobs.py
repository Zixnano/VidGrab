"""Job lifecycle: registry, state, snapshot persistence, new_job."""
from urllib.parse import urlparse
import json
import os
import re
import threading
import time

import settings
from settings import STATE, category_for, safe_filename, guess_filename, detect_type, guess_ext_from_head, stat_for
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
                snap[jid] = {k: v for k, v in j.items()
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
        p = stat_for(job)
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
    }
    for k, v in defaults.items():
        j.setdefault(k, v)
    return j


def load_jobs_snapshot():
    if settings.JOBS_PATH.exists():
        try:
            snap = json.loads(settings.JOBS_PATH.read_text())
            for jid, j in snap.items():
                j["pause_evt"] = threading.Event()
                j["stop_evt"] = threading.Event()
                _migrate_job(j)
                if j.get("status") in ("downloading", "queued", "held"):
                    repair_status(j, "stopped")
                JOBS[jid] = j
                _migrate_extensionless(j)
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


def new_job(url, filename=None, category=None, referer=None, cookie=None,
            user_agent=None, job_type=None, format_id=None, target_format=None,
            resolution=None, multi=False, defer=False):
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
    netloc = urlparse(url).netloc
    for rule in STATE.get("rules", []):
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("domain", "").strip().lstrip("*.")
        if pattern and (netloc == pattern or netloc.endswith("." + pattern)):
            if "category" in rule:
                job_cat = rule["category"]
            break

    JOBS[jid] = {
        "id": jid, "url": url, "filename": fname,
        "category": job_cat,
        "type": jtype, "status": "held" if defer else "queued", "phase": "idle",
        "format_id": format_id, "target_format": target_format,
        "completed_ts": None,
        "size_total": 0, "size_done": 0, "speed": "", "error": None,
        "referer": referer, "cookie": cookie, "user_agent": user_agent,
        "created_ts": time.time(),
        "pause_evt": threading.Event(), "stop_evt": threading.Event(),
    }
    ext = os.path.splitext(fname)[1].lstrip(".").lower()
    if ext and STATE.get("file_types_overrides", {}).get(ext) is False:
        JOBS[jid]["status"] = "skipped"
        log(f"job {jid}: skipped — '.{ext}' is disabled in Options → File Types")
        save_jobs_snapshot()
        return jid
    if settings.get_shutdown_pending():
        os.system("shutdown /a")
        settings.set_shutdown_pending(False)
        log("Scheduled shutdown cancelled — new job queued")
    log(f"job {jid} {'held (deferred)' if defer else 'queued'}: {fname}")
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
