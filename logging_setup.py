"""Logging configuration: in-memory queue (GUI log strip) + rotating file log."""
import logging
import queue
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

USE_STRUCTURED_LOGGING = True  # Session 4: enabled

LOG_QUEUE = queue.Queue()

_logger = None
_logger_lock = threading.Lock()


def _get_logger():
    global _logger
    if _logger is None:
        with _logger_lock:
            if _logger is None:
                lg = logging.getLogger("videograbber")
                lg.setLevel(logging.DEBUG)
                try:
                    # Late import: settings depends on this module at load time.
                    from settings import HOME
                    log_dir = Path(HOME) / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    h = RotatingFileHandler(
                        log_dir / "videograbber.log",
                        maxBytes=2 * 1024 * 1024, backupCount=3)
                    h.setFormatter(logging.Formatter(
                        "%(asctime)s %(levelname)s %(message)s"))
                    lg.addHandler(h)
                except Exception:
                    pass  # file logging is best-effort; the queue always works
                _logger = lg
    return _logger


def log(*args):
    msg = " ".join(str(a) for a in args)
    ts = time.strftime("%H:%M:%S")
    LOG_QUEUE.put(f"{ts} {msg}")
    if USE_STRUCTURED_LOGGING:
        try:
            _get_logger().info(msg)
        except Exception:
            pass
