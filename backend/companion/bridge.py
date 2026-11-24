"""Backend bridge shared by Qt and Flask. Initialised only by the desktop app.

Download events arrive on download threads, inside the job state machine. They are only queued
here; a single worker thread does the database work, so a download never waits on the pet.
"""
import queue
import threading
from pathlib import Path

from . import diag
from .core import CompanionCore
from .events import subscribe
from .store import CompanionStore

_lock = threading.RLock()
_core = None
_queue = queue.SimpleQueue()
_worker = None
_subscribed = False
_MAX_BACKLOG = 400


def data_dir():
    import settings
    return Path(settings.DB_PATH).parent


def get_core():
    global _core
    with _lock:
        if _core is None:
            import settings
            store = CompanionStore(data_dir() / "companion.db")
            try:
                store.import_legacy(settings.DB_PATH)     # one-time copy from an earlier patch
            except Exception as exc:
                diag.note("legacy_import", "legacy import skipped", exc)
            _core = CompanionCore(store)
        return _core


def _handle(core, event, payload):
    if not core.state.enabled:
        return                                            # hard gate: OFF processes nothing
    if event == "daily_open":
        core.mark_daily_open(payload.get("day"))
    elif event == "job_finished":
        core.award_download(payload.get("job", {}))
    elif event == "job_failed":
        core.note_failure(str(payload.get("job", {}).get("id", "")))
    elif event == "queue_active":
        core.note_queue_active()


def _run():
    while True:
        item = _queue.get()
        if item is None:
            return
        try:
            _handle(get_core(), *item)
        except Exception as exc:
            diag.note("worker", "worker error while handling %s (isolated)" % (item[0],), exc)


def _on_event(event, payload):
    core = _core
    if core is None or not core.state.enabled:
        return
    if _queue.qsize() > _MAX_BACKLOG:
        diag.note("dropped_events", "download events dropped: worker backlog is over %d" % _MAX_BACKLOG)
        return
    _queue.put((event, payload))


def initialize():
    """Idempotent. Creates the core, subscribes once, starts the worker, counts today's visit."""
    global _subscribed, _worker
    core = get_core()
    with _lock:
        if not _subscribed:
            subscribe(_on_event)
            _subscribed = True
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="companion-worker", daemon=True)
            _worker.start()
    if core.state.enabled:
        core.mark_daily_open()
    return core


def shutdown():
    """Stop the worker (used by tests)."""
    global _worker, _subscribed
    from .events import unsubscribe
    with _lock:
        unsubscribe(_on_event)
        _subscribed = False
        if _worker is not None and _worker.is_alive():
            _queue.put(None)
            _worker.join(timeout=2)
        _worker = None


def reset_for_tests():
    global _core
    shutdown()
    with _lock:
        _core = None
