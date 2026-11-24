"""Quiet failure visibility for the companion.

The companion must never break the app, so its error paths swallow exceptions. This module makes
those swallowed failures findable without adding noise: the first occurrence of each kind is
written to the normal app log, repeats are only counted, and one summary line per kind is logged
at most once a minute. Counters are readable at GET /companion/diag (token protected).

note() never raises.
"""
import threading
import time

REPEAT_LOG_SECONDS = 60.0

_lock = threading.Lock()
_counts = {}
_last_log = {}
_last_text = {}


def _emit(line):
    try:
        from logging_setup import log
        log(line)
    except Exception:
        pass


def note(key, message="", exc=None):
    try:
        text = str(message or key)
        if exc is not None:
            text = "%s (%s: %s)" % (text, type(exc).__name__, exc)
        now = time.monotonic()
        with _lock:
            count = _counts.get(key, 0) + 1
            _counts[key] = count
            _last_text[key] = text
            last = _last_log.get(key)
            if last is not None and now - last < REPEAT_LOG_SECONDS:
                return
            _last_log[key] = now
        _emit("companion: %s" % text if count == 1 else "companion: %s [%d so far]" % (text, count))
    except Exception:
        pass


def snapshot():
    with _lock:
        return {"counts": dict(_counts), "last": dict(_last_text)}


def reset_for_tests():
    with _lock:
        _counts.clear()
        _last_log.clear()
        _last_text.clear()
