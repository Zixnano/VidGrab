# launcher.py: entry point frozen into VideoGrabber.exe (thin launcher).
#
# Order matters, and the first three steps are the fork-bomb defence:
#   1. multiprocessing.freeze_support()  (frozen children exit cleanly)
#   2. single-instance mutex             (a 2nd copy exits at once)
#   3. self-spawn guard                  (frozen exe can never re-run itself
#                                         through subprocess; logs who tried)
#   4. sys.path / DLL dirs for runtime\site-packages (numpy, PIL, PySide6)
#   5. run app\server.py as __main__
#
# NOTE: this launcher never relaunches itself, on a crash or otherwise.
# If you merge in other launcher features (bgutil provider, self-heal),
# keep them AFTER step 3, and if one truly must start this exe again,
# set VG_ALLOW_SELF_SPAWN=1 in that child's env and close the mutex first
# with release_instance_lock().
import os
import sys
import runpy
import time
import traceback
from pathlib import Path

LOCK_NAME = "Local\\VideoGrabber_SingleInstance_v1"
_lock_handle = None
_dll_handles = []   # keep add_dll_directory handles alive


def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _root()
APP = ROOT / "app" if (ROOT / "app").is_dir() else Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
SITE = RUNTIME / "site-packages"
LOG = ROOT / "launcher.log"


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def _fatal(msg: str) -> None:
    _log("FATAL: " + msg)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, msg[:1500] + f"\n\nDetails: {LOG}", "Video Grabber", 0x10)
        except Exception:
            pass


# ---- 2. single-instance lock ------------------------------------------------
def acquire_instance_lock() -> bool:
    """True if we are the only instance. Windows named mutex."""
    global _lock_handle
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.CreateMutexW(None, False, LOCK_NAME)
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        if handle:
            k32.CloseHandle(handle)
        return False
    _lock_handle = handle
    return True


def release_instance_lock() -> None:
    global _lock_handle
    if _lock_handle and sys.platform == "win32":
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(_lock_handle))
    _lock_handle = None


# ---- 3. self-spawn guard ------------------------------------------------------
def install_spawn_guard() -> None:
    """In a frozen build sys.executable IS this exe. Anything that runs
    [sys.executable, '-m', ...] or [sys.executable, '-c', ...] would start
    a whole new copy of the app. Block it and log the call stack."""
    if not getattr(sys, "frozen", False):
        return
    import subprocess
    own = os.path.normcase(os.path.abspath(sys.executable))
    own_name = os.path.basename(own)
    real_init = subprocess.Popen.__init__

    def _is_self(cmd) -> bool:
        try:
            first = cmd
            if isinstance(cmd, (list, tuple)):
                if not cmd:
                    return False
                first = cmd[0]
            first = os.fsdecode(first).strip()
            n = os.path.normcase(first).lstrip('"')
            if isinstance(cmd, (list, tuple)):
                return (os.path.normcase(os.path.abspath(first.strip('"'))) == own
                        or os.path.basename(n.rstrip('"')) == own_name)
            return n.startswith(own)
        except Exception:
            return False

    def guarded(self, *a, **kw):
        cmd = a[0] if a else kw.get("args")
        if _is_self(cmd) and os.environ.get("VG_ALLOW_SELF_SPAWN") != "1":
            _log("BLOCKED self-spawn: %r\n%s" % (cmd, "".join(traceback.format_stack(limit=12))))
            raise RuntimeError("Blocked: frozen app tried to start itself via subprocess")
        return real_init(self, *a, **kw)

    subprocess.Popen.__init__ = guarded


# ---- 4. sys.path + DLL search path --------------------------------------------
def setup_paths() -> None:
    for p in (str(SITE), str(APP)):          # APP ends up first, SITE second
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)

    dll_dirs = [RUNTIME, SITE, SITE / "PySide6", SITE / "shiboken6",
                SITE / "numpy.libs", SITE / "numpy" / ".libs",
                SITE / "pillow.libs", SITE / "PIL"]
    existing = [d for d in dll_dirs if d.is_dir()]
    if hasattr(os, "add_dll_directory"):
        for d in existing:
            try:
                _dll_handles.append(os.add_dll_directory(str(d)))
            except OSError as e:
                _log(f"add_dll_directory failed for {d}: {e}")
    os.environ["PATH"] = os.pathsep.join(
        [str(d) for d in existing] + [os.environ.get("PATH", "")])

    _log(f"python={sys.version.split()[0]} root={ROOT} app={APP}")
    _log("site-packages: numpy=%s numpy.libs=%s PIL=%s PySide6=%s" % (
        (SITE / "numpy").is_dir(), (SITE / "numpy.libs").is_dir(),
        (SITE / "PIL").is_dir(), (SITE / "PySide6").is_dir()))
    if "numpy" in sys.modules or "PIL" in sys.modules:
        _log("WARNING: numpy/PIL already imported before path setup (bundled copy?)")


def main() -> int:
    # 1. must be first: frozen multiprocessing children are handled here
    import multiprocessing
    multiprocessing.freeze_support()

    try:
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.unlink()
    except Exception:
        pass

    # 2.
    if not acquire_instance_lock():
        _log("another instance holds the lock; exiting")
        return 0
    # 3.
    install_spawn_guard()
    # 4.
    try:
        setup_paths()
        server = APP / "server.py"
        if not server.is_file():
            _fatal(f"app\\server.py not found at {server}")
            return 1
        os.chdir(str(APP))
        sys.argv[0] = str(server)
        # 5.
        runpy.run_path(str(server), run_name="__main__")
        return 0
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else 0
    except BaseException:
        _fatal("Video Grabber crashed on startup:\n" + traceback.format_exc()[-1200:])
        return 1
    finally:
        release_instance_lock()


if __name__ == "__main__":
    sys.exit(main())
