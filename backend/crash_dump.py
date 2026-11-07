"""crash_dump.py: crash reports for Video Grabber.

Drop-in module. Call install(log_dir, version, role) as early as possible at
startup, before the Qt app is created. Call crumb(...) on user actions.

What it captures, all under <log_dir>/crash-dumps/:
  crash-<stamp>.txt        Python exception on any thread (traceback, last
                           user actions, tail of launcher.log)
  abnormal-<stamp>.txt     the previous run never exited cleanly. Python did
                           not raise, so this is a native crash (access
                           violation, abort), a force kill, or power loss.
  crash-qt-<stamp>.txt     Qt reported a Critical or Fatal message
  native-faults.log        faulthandler output for native crashes
  breadcrumbs are included in every report, not stored on their own.

Privacy: breadcrumbs keep file names only, never full paths. Reports can
still contain URLs that a traceback prints, so review before sharing.
"""

import atexit
import collections
import faulthandler
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

_DIR = None
_VERSION = "unknown"
_ROLE = "app"
_LOG_TAIL_FILE = None
_CRUMBS = collections.deque(maxlen=200)
_CRUMB_LOCK = threading.Lock()
_FAULT_FH = None
_QT_HANDLER = None
_MARKER = None
_PREVIOUS = None          # report written for an abnormal exit of the last run
_INSTALLED = False


_SEQ = 0


def _stamp():
    """Unique per report: time to the second, pid, and a counter."""
    global _SEQ
    _SEQ += 1
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-{_SEQ:03d}"


def crumb(event, **data):
    """Record a user action. Cheap; safe to call from any thread."""
    parts = []
    for k, v in data.items():
        if isinstance(v, str) and ("/" in v or "\\" in v):
            v = os.path.basename(v.replace("\\", "/"))
        parts.append(f"{k}={v!r}")
    line = time.strftime("%H:%M:%S") + " " + event + (" " + " ".join(parts) if parts else "")
    with _CRUMB_LOCK:
        _CRUMBS.append(line)


def _launcher_tail(lines=80):
    try:
        if _LOG_TAIL_FILE and Path(_LOG_TAIL_FILE).is_file():
            text = Path(_LOG_TAIL_FILE).read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except Exception:
        pass
    return ""


def _report(kind, body):
    with _CRUMB_LOCK:
        crumbs = list(_CRUMBS)
    out = [
        "Video Grabber crash report",
        f"kind: {kind}",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"version: {_VERSION}",
        f"process: {_ROLE} (pid {os.getpid()})",
        f"python: {sys.version.split()[0]}",
        f"os: {platform.platform()}",
        "",
        "--- details ---",
        body.rstrip(),
        "",
        f"--- last {len(crumbs)} user actions (oldest first) ---",
    ]
    out += crumbs or ["(none recorded)"]
    tail = _launcher_tail()
    if tail:
        out += ["", "--- launcher.log (tail) ---", tail]
    return "\n".join(out) + "\n"


def _write(name, text):
    """Write a report into crash-dumps/. Never raises: a reporter that
    crashes would hide the crash it was meant to record."""
    global _PREVIOUS
    if _DIR is None:
        return None
    try:
        p = _DIR / name
        p.write_text(text, encoding="utf-8", errors="replace")
        _PREVIOUS = p
        return p
    except Exception:
        return None


def _excepthook(exc_type, exc, tb):
    body = "".join(traceback.format_exception(exc_type, exc, tb))
    _write(f"crash-{_stamp()}.txt", _report("unhandled exception", body))
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "?"
    body = f"thread: {name}\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _write(f"crash-{_stamp()}.txt", _report("unhandled exception in thread", body))
    try:
        threading.__excepthook__(args)
    except Exception:
        pass


def _read_native_faults(max_lines=60):
    try:
        p = _DIR / "native-faults.log"
        if p.is_file() and p.stat().st_size:
            return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:])
    except Exception:
        pass
    return ""


def _check_previous_run():
    """A marker left behind means the last process never reached atexit."""
    global _PREVIOUS
    if not _MARKER.exists():
        return
    try:
        old = _MARKER.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        old = "(unreadable marker)"
    faults = _read_native_faults()
    body = (
        "The previous run did not exit cleanly and Python raised no error.\n"
        "Breadcrumbs from that run are not available: they live in memory.\n"
        "Likely causes: a native crash (for example a Qt or codec access\n"
        "violation), a force kill from Task Manager, or power loss.\n"
        f"Previous run marker: {old}\n\n"
        "--- native-faults.log (tail) ---\n"
        + (faults or "(empty: no native fault was recorded)")
    )
    _write(f"abnormal-{_stamp()}.txt", _report("abnormal exit of previous run", body))


def _qt_message(mode, context, message):
    name = str(mode)
    try:
        if "Critical" in name or "Fatal" in name:
            crumb("qt-critical", msg=str(message)[:300])
            _write(f"crash-qt-{_stamp()}.txt", _report("Qt critical message", str(message)))
        elif "Warning" in name:
            crumb("qt-warning", msg=str(message)[:200])
        # Qt's default stderr output is replaced by this handler, so forward it.
        if sys.stderr is not None:
            print(message, file=sys.stderr)
    except Exception:
        pass


def install_qt_handler():
    """Call after PySide6 is importable (it can be called before QApplication)."""
    global _QT_HANDLER
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except Exception:
        return False
    _QT_HANDLER = _qt_message          # keep a reference so it is not collected
    qInstallMessageHandler(_QT_HANDLER)
    return True


def install(log_dir, version="unknown", role="app", launcher_log=None):
    """Set up reporting. Safe to call twice; the second call does nothing."""
    global _DIR, _VERSION, _ROLE, _LOG_TAIL_FILE, _FAULT_FH, _MARKER, _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _VERSION, _ROLE = version, role
    _LOG_TAIL_FILE = str(launcher_log) if launcher_log else None
    _DIR = Path(log_dir) / "crash-dumps"
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        _DIR = None
        return

    _MARKER = _DIR / f"running-{role}.marker"
    _check_previous_run()
    _MARKER.write_text(f"pid={os.getpid()} version={version} started="
                       f"{time.strftime('%Y-%m-%d %H:%M:%S')}", encoding="utf-8")
    atexit.register(lambda: _MARKER.unlink(missing_ok=True))

    # faulthandler needs its file object to stay open for the process lifetime.
    try:
        _FAULT_FH = open(_DIR / "native-faults.log", "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_FAULT_FH, all_threads=True)
    except Exception:
        pass

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook


def previous_report():
    """Path of the newest report written by an earlier run, or None.
    The GUI can call this at startup and offer to open the folder."""
    if _DIR is None:
        return None
    try:
        seen = _DIR / "last-seen.txt"
        since = seen.stat().st_mtime if seen.exists() else 0.0
        reports = [p for p in _DIR.glob("*.txt")
                   if p.name != "last-seen.txt" and p.stat().st_mtime > since]
        return max(reports, key=lambda p: p.stat().st_mtime) if reports else None
    except Exception:
        return None


def mark_reports_seen():
    """Call after the user has been told about the previous report."""
    if _DIR is None:
        return
    try:
        (_DIR / "last-seen.txt").write_text(time.strftime("%Y-%m-%d %H:%M:%S"),
                                            encoding="utf-8")
    except Exception:
        pass


def report_folder():
    return _DIR


def memory_mb():
    """This process's working set in MB, or None if unavailable.
    Windows uses GetProcessMemoryInfo; elsewhere /proc/self/status."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.WinDLL("psapi").GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
            return pmc.WorkingSetSize / 1048576 if ok else None
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        return None
    return None


_MEMLOG_MAX = 512 * 1024   # keep memory.log small; older lines are dropped


def log_memory(tag):
    """Append one line with the app's memory, labelled (e.g. after each
    finished job). Written to crash-dumps/memory.log, never raises."""
    mb = memory_mb()
    value = f"{mb:.1f} MB" if mb is not None else "unavailable"
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {tag}: {value}\n"
    crumb("memory", tag=tag, mb=None if mb is None else round(mb, 1))
    if _DIR is None:
        return
    try:
        p = _DIR / "memory.log"
        if p.exists() and p.stat().st_size > _MEMLOG_MAX:
            keep = p.read_text(encoding="utf-8", errors="replace")[-_MEMLOG_MAX // 2:]
            p.write_text(keep.split("\n", 1)[-1], encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
