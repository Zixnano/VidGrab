# app_host.py: entry point frozen into VideoGrabberApp.exe (thin app host).
# This is the former launcher.py, unchanged in its defences; under the
# Supervisor Architecture it is spawned BY the supervisor (launcher.py,
# frozen as VideoGrabber.exe), which owns the single-instance mutex and
# crash counting. Added here: VG_SUPERVISED (skip the mutex - the
# supervisor holds it), VG_SMOKE_TEST (runtime health probe for
# Recovery), VG_ENTRY_SCRIPT (run updater_ui.py instead of server.py),
# and the --simulate matrix used by test_harness.py.
#
# (Original header follows.)
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
HOST_LOCK_NAME = "Local\\VideoGrabber_Host_v1"
# Exit code for "refused: another host already has the host mutex".
# Distinct on purpose (post-audit fix 1): the supervisor recognises it
# and does NOT count the run as a crash. A duplicate launch is not a
# failure, and counting it could blacklist a healthy version after 3
# stray double-clicks against an orphaned host.
DUPLICATE_HOST_EXIT = 42
_lock_handle = None
_host_lock_handle = None
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


_stderr_file = None


def _ensure_std_streams() -> None:
    """console=False builds start with sys.stdout / sys.stderr = None.
    Anything that writes to them (print, werkzeug, yt-dlp, click) can
    then die with AttributeError, and every traceback vanishes. Give
    them real sinks (stdout -> devnull, stderr -> launcher.log) and
    switch on faulthandler so a native crash (Qt / DLL abort) leaves a
    trace instead of a silent exit."""
    global _stderr_file
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            _stderr_file = open(LOG, "a", encoding="utf-8", buffering=1)
            sys.stderr = _stderr_file
        import faulthandler
        faulthandler.enable(file=sys.stderr, all_threads=True)
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


def host_lock_required(env) -> bool:
    """Fix 4 (pure, unit-tested): the host mutex is required for
    every real run. Only the two guest modes skip it: the smoke probe
    (Recovery must be able to test the runtime WHILE the app runs)
    and the updater entry script (a guest of the supervisor)."""
    return not (env.get("VG_SMOKE_TEST") or env.get("VG_ENTRY_SCRIPT"))


def acquire_host_lock() -> bool:
    """The host's OWN mutex, Local\\VideoGrabber_Host_v1, taken
    regardless of VG_SUPERVISED. The supervisor's mutex proves a
    supervisor is running; it does not prove only one HOST is. With
    VG_SUPERVISED inherited by every grandchild, the main mutex alone
    left the fork-bomb defence resting on the subprocess patch; this
    lock means a second host exits at once, supervised or not, and an
    orphaned host blocks (visibly) instead of colliding silently."""
    global _host_lock_handle
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.CreateMutexW(None, False, HOST_LOCK_NAME)
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        if handle:
            k32.CloseHandle(handle)
        return False
    _host_lock_handle = handle
    return True


def release_host_lock() -> None:
    global _host_lock_handle
    if _host_lock_handle and sys.platform == "win32":
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(_host_lock_handle))
    _host_lock_handle = None


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


def _simulate_mode() -> str:
    for a in sys.argv[1:]:
        if a.startswith("--simulate="):
            return a.split("=", 1)[1].strip()
    return os.environ.get("VG_SIMULATE", "").strip()


_held_sockets = []   # --simulate=port-occupied keeps 5757 bound during the run


def _apply_simulation(mode: str) -> None:
    """test_harness hooks (contract §7). Non-destructive: corrupt-settings
    restores the original bytes in main()'s finally."""
    if mode == "crash-before-sentinel":
        _log("SIMULATE crash-before-sentinel: exiting before the app starts")
        raise SystemExit(3)
    if mode == "missing-module":
        _log("SIMULATE missing-module: raising ModuleNotFoundError")
        raise ModuleNotFoundError("No module named 'vg_simulated_missing' (simulated)")
    if mode == "port-occupied":
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 5757))
        sock.listen(1)
        _held_sockets.append(sock)
        _log("SIMULATE port-occupied: dummy listener bound to 5757")
    if mode == "corrupt-settings":
        sj = Path.home() / "Downloads" / "VideoGrabber" / "settings.json"
        if sj.is_file():
            backup = sj.with_suffix(".json.simbackup")
            backup.write_bytes(sj.read_bytes())
            sj.write_text("{ this is not json", encoding="utf-8")
            _log(f"SIMULATE corrupt-settings: {sj} corrupted (backup at {backup})")


def _restore_simulated_settings() -> None:
    sj = Path.home() / "Downloads" / "VideoGrabber" / "settings.json"
    backup = sj.with_suffix(".json.simbackup")
    try:
        if backup.is_file():
            sj.write_bytes(backup.read_bytes())
            backup.unlink()
            _log("SIMULATE corrupt-settings: original settings.json restored")
    except Exception:
        pass


def main() -> int:
    # 1. must be first: frozen multiprocessing children are handled here
    import multiprocessing
    multiprocessing.freeze_support()

    try:
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.unlink()
    except Exception:
        pass

    _ensure_std_streams()
    _log("host start argv=%r supervised=%s smoke=%s entry=%s" % (
        sys.argv[1:], os.environ.get("VG_SUPERVISED"),
        os.environ.get("VG_SMOKE_TEST"), os.environ.get("VG_ENTRY_SCRIPT")))

    # 2. Under the supervisor, the supervisor holds the mutex (§1).
    # Smoke-test and updater-entry modes never take it either: they are
    # probes/guests, and blocking them on the app's mutex would make a
    # healthy install look broken (and silently no-op the updater).
    supervised = os.environ.get("VG_SUPERVISED") == "1"
    guest_mode = bool(os.environ.get("VG_SMOKE_TEST")
                      or os.environ.get("VG_ENTRY_SCRIPT"))
    if not supervised and not guest_mode and not acquire_instance_lock():
        _log("another instance holds the lock; exiting")
        return 0
    # 2b. Our own host mutex, supervised or not (fix 4). Guests skip
    # it: see host_lock_required.
    if host_lock_required(os.environ) and not acquire_host_lock():
        _log("another HOST already holds the host lock; exiting (42)")
        return DUPLICATE_HOST_EXIT
    # 3.
    install_spawn_guard()
    # 4.
    mode = _simulate_mode()
    try:
        setup_paths()
        if os.environ.get("VG_SMOKE_TEST") == "1":
            # Recovery's runtime probe: importing from runtime\ ONLY
            # (paths were just set up) proves the environment is whole.
            # Quiet by design: no _fatal MessageBox - the caller reads
            # the exit code and stderr.
            failures = []
            for modname in ("PySide6.QtWidgets", "requests", "flask",
                            "yt_dlp", "yt_dlp.extractor", "server"):
                try:
                    __import__(modname)
                except BaseException:
                    failures.append(modname)
                    _log(f"SMOKE import {modname} FAILED:\n"
                         + traceback.format_exc()[-1500:])
            if failures:
                print("SMOKE_FAIL: " + ", ".join(failures))
                return 1
            print("SMOKE_OK")
            return 0
        _apply_simulation(mode)
        entry = os.environ.get("VG_ENTRY_SCRIPT", "")
        if entry:
            # Updater mode: the supervisor staged updater_ui.py and asks
            # the host (the only interpreter this install has) to run it.
            script = Path(entry)
            if not script.is_file():
                _fatal(f"VG_ENTRY_SCRIPT not found: {script}")
                return 1
            os.chdir(str(script.parent))
            sys.argv = [str(script)]
            runpy.run_path(str(script), run_name="__main__")
            return 0
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
        _log(f"SystemExit code={code!r}")
        return code if isinstance(code, int) else 0
    except BaseException:
        _fatal("Video Grabber crashed on startup:\n" + traceback.format_exc()[-1200:])
        return 1
    finally:
        if mode == "corrupt-settings":
            _restore_simulated_settings()
        for sock in _held_sockets:
            try:
                sock.close()
            except Exception:
                pass
        release_instance_lock()
        release_host_lock()


if __name__ == "__main__":
    sys.exit(main())
