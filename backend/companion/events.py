"""Small, isolated event bus. Listener failures are swallowed and logged.

publish() is called from download threads (inside the job state machine), so it must stay cheap:
it only walks a tuple of listeners. The real listener (bridge.py) just queues the event for its
own worker thread and returns.
"""
import threading

from . import diag

_lock = threading.RLock()
_listeners = ()


def subscribe(callback):
    global _listeners
    with _lock:
        if callback not in _listeners:
            _listeners = _listeners + (callback,)
    return callback


def unsubscribe(callback):
    global _listeners
    with _lock:
        _listeners = tuple(fn for fn in _listeners if fn != callback)


def publish(event, payload=None):
    listeners = _listeners
    if not listeners:
        return
    for fn in listeners:
        try:
            fn(str(event), payload or {})
        except Exception as exc:
            diag.note("event_listener", "event listener failed (isolated)", exc)
