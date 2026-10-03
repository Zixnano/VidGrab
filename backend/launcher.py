# launcher.py — thin entry point for VideoGrabber.exe
#
# Locates runtime/ and app/, exports VG_RUNTIME so engines.py can find
# ffmpeg/deno inside runtime\, sets up the DLL search path so PySide6
# can load Qt, then hands off to app/server.py.
#
# Also starts the bgutil PO-token provider (a small local Deno server on
# 127.0.0.1:4416) that yt-dlp's bgutil plugin asks for YouTube proof-of-origin
# tokens, and stops it when the app exits. See _start_pot_provider().
#
# Works both frozen and from source (python launcher.py).
import atexit
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def _root() -> Path:
    """Where VideoGrabber.exe (or launcher.py from source) lives."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _log_error(msg: str) -> None:
    """Report a launcher error. Frozen --noconsole builds have
    sys.stderr=None, so a plain sys.stderr.write would raise and the
    user would see nothing — always persist to a log beside the exe,
    and mirror to stderr only when a console actually exists."""
    try:
        log_path = _root() / "launcher-error.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass
    if sys.stderr is not None:
        try:
            sys.stderr.write(msg + "\n")
        except (OSError, ValueError):
            pass


def _setup_dll_search(runtime: Path) -> None:
    """Python 3.8+ requires explicit DLL directories for anything
    outside the default search path. PySide6 ships its Qt DLLs in a
    subfolder that isn't on PATH, so we add it."""
    if sys.platform != "win32":
        return
    candidates = [
        # pip --target layout: PySide6 stays inside site-packages
        runtime / "site-packages" / "PySide6",
        runtime / "site-packages" / "PySide6" / "Qt" / "bin",
        # moved layout, if someone insists on hoisting it to runtime\
        runtime / "PySide6",
        runtime / "PySide6" / "Qt" / "bin",
        runtime,
    ]
    for d in candidates:
        if d.is_dir():
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# bgutil PO-token provider
#
# yt-dlp's bgutil plugin (runtime\site-packages\yt_dlp_plugins) asks
# http://127.0.0.1:4416 for YouTube PO tokens. This starts the provider
# server (runtime\bgutil-pot-server, run by the runtime's own Deno) and stops
# it on exit. It is a mitigation, not a guarantee: the provider's own README
# says a token "does not guarantee bypassing 403 errors or bot checks".
#
# Everything here is best-effort: a missing, crashing or slow provider must
# never stop the app from starting. No cookie files are involved.
# ---------------------------------------------------------------------------
_POT_PORT = 4416   # the plugin's default base_url; another port needs a yt-dlp extractor arg too
_POT_LOG_CAP = 1_000_000
_POT = {"proc": None, "job": None}


def _pot_ping(timeout: float = 1.5) -> bool:
    """True when a POT provider answers GET /ping on the port (checks the
    reply shape so some unrelated service on 4416 doesn't count)."""
    try:
        import json
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{_POT_PORT}/ping", timeout=timeout) as r:
            return r.status == 200 and "server_uptime" in json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False


def _find_deno(runtime: Path):
    for name in ("deno.exe", "deno"):
        p = runtime / name
        if p.is_file():
            return str(p)
    return shutil.which("deno")


def _pot_log_writer(root: Path):
    """Return write(line): appends to pot-provider.log beside the exe
    (truncated each start, capped at ~1 MB). Never raises."""
    lock = threading.Lock()
    state = {"f": None, "n": 0}
    try:
        state["f"] = open(root / "pot-provider.log", "w", encoding="utf-8", errors="replace")
    except OSError:
        pass

    def write(line: str) -> None:
        with lock:
            f = state["f"]
            if f is None or state["n"] > _POT_LOG_CAP:
                return
            try:
                stamp = time.strftime("%H:%M:%S")
                text = f"{stamp} {line.rstrip()}\n"
                f.write(text)
                f.flush()
                state["n"] += len(text)
            except (OSError, ValueError):
                state["f"] = None
    return write


def _bind_to_kill_on_close_job(proc) -> bool:
    """Windows: put the child in a Job Object that kills it when this
    process dies for ANY reason (task kill, crash, power button on the
    window). atexit alone is skipped by a hard kill, which would leave
    deno.exe running and holding files open. Returns False when it
    couldn't (the atexit/finally path still stops it on a normal exit)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32, u64, c_void_p = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, u64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", u32),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", u32),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", u32),
                        ("SchedulingClass", u32)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = c_void_p
        k32.CreateJobObjectW.argtypes = [c_void_p, c_void_p]
        k32.SetInformationJobObject.argtypes = [c_void_p, ctypes.c_int, c_void_p, u32]
        k32.OpenProcess.restype = c_void_p
        k32.OpenProcess.argtypes = [u32, ctypes.c_int, u32]
        k32.AssignProcessToJobObject.argtypes = [c_void_p, c_void_p]
        k32.CloseHandle.argtypes = [c_void_p]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        h = k32.OpenProcess(0x0100 | 0x0001, 0, proc.pid)
        if not h:
            return False
        try:
            ok = bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
        if ok:
            _POT["job"] = job  # keep the handle open for the life of the process
        return ok
    except Exception:
        return False


def _stop_pot_provider() -> None:
    """Stop the provider we started (never one we merely found running)."""
    proc = _POT.get("proc")
    _POT["proc"] = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def _start_pot_provider(root: Path, runtime: Path) -> None:
    """Start runtime\\bgutil-pot-server on 127.0.0.1:4416 unless one is
    already answering. Never raises, never blocks startup. Set
    VG_NO_POT_PROVIDER=1 to skip it."""
    try:
        if os.environ.get("VG_NO_POT_PROVIDER", "").strip().lower() in ("1", "true", "yes", "on"):
            return
        server_dir = runtime / "bgutil-pot-server"
        main_ts = server_dir / "src" / "main.ts"
        modules = server_dir / "node_modules"
        if not (main_ts.is_file() and modules.is_dir()):
            return  # not bundled (source run or an older install): nothing to do
        write = _pot_log_writer(root)
        if _pot_ping():
            write(f"launcher: a provider already answers on port {_POT_PORT}; reusing it "
                  "(it is not ours, so it won't be stopped on exit)")
            return
        deno = _find_deno(runtime)
        if not deno:
            write("launcher: deno not found; provider not started")
            _log_error("pot provider: deno not found next to the runtime or on PATH; "
                       "YouTube proof-of-origin tokens are unavailable")
            return
        # Same command the provider documents for Deno. It runs from
        # node_modules so --allow-ffi/--allow-read=. cover the native canvas
        # module and nothing else. --no-prompt: a missing permission fails
        # instead of waiting for a keypress nobody can give.
        cmd = [deno, "run", "--no-prompt", "--allow-env", "--allow-net",
               "--allow-ffi=.", "--allow-read=.", "../src/main.ts",
               "--port", str(_POT_PORT)]
        env = dict(os.environ, DENO_NO_UPDATE_CHECK="1")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd, cwd=str(modules), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        _POT["proc"] = proc
        atexit.register(_stop_pot_provider)
        bound = _bind_to_kill_on_close_job(proc)
        write(f"launcher: started provider pid {proc.pid} on port {_POT_PORT} "
              f"(kill-on-exit job: {'yes' if bound else 'no, atexit only'})")

        def _pump() -> None:
            try:
                for raw in proc.stdout:
                    write(raw.decode("utf-8", "replace"))
            except Exception:
                pass

        def _watch() -> None:
            deadline = time.time() + 45
            while time.time() < deadline:
                if proc.poll() is not None:
                    write(f"launcher: provider exited early with code {proc.returncode}")
                    _log_error(f"pot provider exited early (code {proc.returncode}); "
                               "see pot-provider.log")
                    return
                if _pot_ping():
                    write(f"launcher: provider ready on port {_POT_PORT}")
                    return
                time.sleep(0.5)
            write("launcher: provider has not answered after 45 s (still running)")

        threading.Thread(target=_pump, daemon=True).start()
        threading.Thread(target=_watch, daemon=True).start()
    except Exception as e:
        _log_error(f"pot provider: could not start: {e}")


def _cleanup_update_leftovers(root: Path) -> None:
    """Delete the previous update's rollback leftovers (app.old\\, the
    renamed-out launcher exe, the renamed-out internal folder). Never
    during the swap — only after the new app has been up long enough to
    prove it starts, otherwise restore.bat / the rename pattern have
    nothing to fall back to."""
    leftovers = [
        root / "app.old",
        root / "VideoGrabber.exe.old",
        root / "VideoGrabber_internal.old",
    ]
    leftovers = [p for p in leftovers if p.exists()]
    if not leftovers:
        return

    def _later() -> None:
        time.sleep(60)  # survived this long => new app started fine
        for p in leftovers:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    threading.Thread(target=_later, daemon=True).start()


def main() -> int:
    root = _root()
    app_dir = root / "app"
    runtime_dir = root / "runtime"
    server_py = app_dir / "server.py"

    if not server_py.is_file():
        # Fall back to the flat layout (pre-restructure) so dev
        # runs don't explode.
        flat = root / "server.py"
        if flat.is_file():
            server_py = flat
            app_dir = root
        else:
            _log_error(
                "Video Grabber: can't find server.py.\n"
                f"  Looked in: {server_py}\n"
                f"  And:       {flat}\n"
                "Reinstall or extract the update zip again."
            )
            return 1

    if runtime_dir.is_dir():
        # VG_RUNTIME: engines.py lives in app\ and can't derive the runtime\
        # folder from __file__ — export it before any app code imports.
        os.environ["VG_RUNTIME"] = str(runtime_dir)
        _setup_dll_search(runtime_dir)
        sys.path.insert(0, str(runtime_dir))
        site = runtime_dir / "site-packages"
        if site.is_dir():
            sys.path.insert(0, str(site))

    sys.path.insert(0, str(app_dir))

    _cleanup_update_leftovers(root)
    if runtime_dir.is_dir():
        _start_pot_provider(root, runtime_dir)

    # No os.chdir: sys.path is what makes app/*.py importable. Mutating the
    # process cwd would silently re-base every relative path in future code.
    sys.argv[0] = str(server_py)
    import runpy
    try:
        runpy.run_path(str(server_py), run_name="__main__")
    except Exception:
        import traceback
        _log_error(
            "launcher: unhandled exception while running server.py:\n"
            + traceback.format_exc()
        )
        return 1
    finally:
        # Normal exit, error, or server.py's sys.exit(): stop the provider
        # before the process ends so nothing keeps runtime\ files open
        # while updater.bat swaps folders.
        _stop_pot_provider()
    return 0


if __name__ == "__main__":
    sys.exit(main())
