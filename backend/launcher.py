# launcher.py - the Supervisor. Frozen into VideoGrabber.exe (onedir).
#
# Supervisor Architecture (build contract v3): this process is what the
# user double-clicks. It NEVER loads application code. It spawns the app
# host (VideoGrabberApp.exe - see app_host.py, the former launcher) as a
# child, watches the launch-scoped sentinel markers, owns crash_count,
# supervises the updater, and launches recovery.exe after 3 consecutive
# startup failures or on request (boot flag / --recovery / Shift).
#
# Adaptation recorded in DECISIONS.md: this install has no
# runtime\python.exe - the interpreter is the PyInstaller host exe - so
# the supervisor spawns VideoGrabberApp.exe, which runs app\server.py
# in-process exactly as the old single-exe launcher did.
#
# Stdlib only. The state contract below is shared verbatim (field names
# and rules) with recovery_ui.py, updater_ui.py and the gui_qt hooks:
#   %LOCALAPPDATA%\VideoGrabber\recovery_state.json
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------- paths ----
def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _root()
APP_DIR = ROOT / "app"
HOST_EXE = ROOT / "VideoGrabberApp.exe"
HOST_SCRIPT = Path(__file__).resolve().parent / "app_host.py"
RECOVERY_EXE = ROOT / "recovery" / "recovery.exe"
RECOVERY_SCRIPT = Path(__file__).resolve().parent / "recovery_ui.py"
UPDATER_BAT = ROOT / "updater.bat"
INSTALL_VERSION = ROOT / "install-version.txt"
LOG = ROOT / "launcher.log"

MUTEX_NAME = "Local\\VideoGrabber_SingleInstance_v1"
_lock_handle = None

# The GUI gives its backend up to 120 s to answer /health (its health
# watch deadline), so the first check must not latch before that: 130 s.
# A run that shows app_alive but no startup_ok is NOT latched while the
# child is alive; it is judged at the hard cap, at child exit, or the
# moment startup_ok lands (fix 7).
FIRST_CHECK_SECONDS = 130.0
HARD_CAP_SECONDS = 300.0
# app_host exits with this code when its own host mutex refuses it
# (post-audit fix 1). It must match app_host.DUPLICATE_HOST_EXIT.
HOST_DUPLICATE_EXIT = 42
POLL_SECONDS = 0.25


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} supervisor pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def _message_box(text: str, title: str = "Video Grabber") -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text[:1500], title, 0x40)
        except Exception:
            pass


# ------------------------------------------------------- state contract ----
# The state contract lives in vg_state.py: one stdlib-only copy shared
# with recovery_ui.py, updater_ui.py and the gui_qt hooks (fix 10).
import vg_state
from vg_state import (DEFAULT_STATE, StateLock, load_state, mutate_state,
                      pid_alive, save_state, state_dir)

STATE_DIR = state_dir()
STATE_FILE = vg_state.state_file()
STATE_LOCK = vg_state.lock_file()
RECOVERY_LOG = vg_state.log_file()
BOOT_FLAG = vg_state.boot_flag()
APP_ALIVE = STATE_DIR / "app_alive.tmp"
STARTUP_OK = STATE_DIR / "startup_ok.tmp"

_pid_alive = pid_alive


def recovery_log(msg: str) -> None:
    vg_state.recovery_log(msg, "supervisor")


# -------------------------------------------------------------- markers ----
def read_marker(path: Path, launch_id: str):
    """Return the marker dict only if it belongs to this launch (§3:
    sentinel is launch-scoped)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("launch_id") == launch_id:
            return data
    except Exception:
        pass
    return None


def clear_markers() -> None:
    for p in (APP_ALIVE, STARTUP_OK):
        try:
            p.unlink()
        except OSError:
            pass


# -------------------------------------------------------------- judging ----
def decide_outcome(markers_seen, alive_at_deadline: bool) -> str:
    """Pure verdict for a run that has NOT already settled as ok.
    markers_seen: set containing 'app_alive' and/or 'startup_ok'."""
    if "startup_ok" in markers_seen:
        return "ok"
    if alive_at_deadline:
        if "app_alive" in markers_seen:
            return "backend_failed"
        return "legacy_ok"
    if "app_alive" in markers_seen:
        return "backend_failed"
    return "crash"


def judge_step(markers_seen, child_alive: bool, elapsed: float):
    """One pure judging step. Returns a settled outcome, or None to
    keep watching. backend_failed is never latched while the child is
    alive before the hard cap, so a late startup_ok still upgrades
    the run to ok (fix 7)."""
    if "startup_ok" in markers_seen:
        return "ok"
    if not child_alive:
        return None            # the caller finalises from the markers
    if elapsed >= HARD_CAP_SECONDS:
        if not markers_seen:
            # Alive for 300 s and it never even painted a window: that
            # is a hung startup. Every shipped version writes the
            # sentinels, so the old "legacy grace" here only hid hangs
            # (and held the mutex forever, locking the user out).
            return "crash"
        return decide_outcome(markers_seen, alive_at_deadline=True)
    return None


def should_auto_blacklist(current, last_good, blacklist) -> bool:
    """Fix 6: a version that crashes its way to Recovery is never the
    last known good one and is not already listed."""
    return bool(current) and current != last_good \
        and current not in (blacklist or [])


def _auto_blacklist_current() -> None:
    """Fix 6 helper, used on EVERY path that sends the user to Recovery
    (including a host that cannot even be spawned)."""
    def _bl(state):
        cur = state.get("current_version")
        if should_auto_blacklist(cur, state.get("last_good_version"),
                                 state.get("blacklist")):
            state["blacklist"] = list(state.get("blacklist") or []) + [cur]
            return cur
        return None
    bad = mutate_state(_bl)
    if bad:
        _log(f"auto-blacklisted {bad}")
        recovery_log(f"auto-blacklisted {bad} after repeated failed starts")


def apply_outcome(state: dict, outcome: str) -> int:
    """Apply the crash-count rules (§3). Returns the new crash_count."""
    if outcome in ("ok", "legacy_ok"):
        state["crash_count"] = 0
        if outcome == "ok" and state.get("current_version"):
            state["last_good_version"] = state["current_version"]
    elif outcome in ("crash", "backend_failed"):
        state["crash_count"] = int(state.get("crash_count") or 0) + 1
    # "duplicate" (a refused second host) falls through on purpose:
    # the counter does not move (post-audit fix 1).
    return int(state.get("crash_count") or 0)


# -------------------------------------------------------- single instance --
def acquire_instance_lock() -> bool:
    global _lock_handle
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        if handle:
            k32.CloseHandle(handle)
        return False
    _lock_handle = handle
    return True


def _shift_held() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except Exception:
        return False


# ------------------------------------------------------------ job object ---
# Windows Job Object with KILL_ON_JOB_CLOSE: if the supervisor dies,
# every process it assigned (the app host, the updater host) dies with
# it, so an orphaned host can never collide with the next launch on
# port 5757 or the mutex (fix 4). ctypes, stdlib only. Best effort: a
# failure here must never stop the app from starting.
_job_handle = None


def _ensure_job():
    global _job_handle
    if _job_handle is not None or sys.platform != "win32":
        return _job_handle
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation",
                         JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        _job_handle = job
    except Exception as e:
        _log(f"job object unavailable: {e}")
        return None
    return _job_handle


class SupervisorFatal(Exception):
    """The supervisor cannot do its one job (controlling its
    children) and must exit rather than run unsupervised children."""


def _assign_to_job(proc) -> bool:
    """Assign proc to the kill-on-close job. Returns False ONLY when
    assignment was required (Windows) and failed (post-audit fix 2):
    a supervisor whose children can outlive it must not run at all.
    Off Windows there is no job object and True is returned."""
    if sys.platform != "win32":
        return True
    job = _ensure_job()
    if not job:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            _log(f"AssignProcessToJobObject failed for pid {proc.pid}")
            return False
    except Exception as e:
        _log(f"job assign failed: {e}")
        return False
    return True


def _require_job(proc, what: str) -> None:
    if not _assign_to_job(proc):
        try:
            proc.terminate()
        except Exception:
            pass
        raise SupervisorFatal(
            f"could not assign the {what} to the supervisor job object; "
            "exiting rather than running an uncontrolled child")


# ------------------------------------------------------------- recovery ----
def recovery_available() -> bool:
    if getattr(sys, "frozen", False):
        return RECOVERY_EXE.is_file()
    return RECOVERY_SCRIPT.is_file()      # dev: the script stands in


def launch_recovery(reason: str) -> None:
    recovery_log(f"launching recovery ({reason})")
    try:
        if getattr(sys, "frozen", False):
            subprocess.Popen([str(RECOVERY_EXE)], cwd=str(ROOT))
        else:
            subprocess.Popen([sys.executable, str(RECOVERY_SCRIPT)], cwd=str(ROOT))
    except Exception as e:
        recovery_log(f"recovery launch FAILED: {e}")
        _message_box("Video Grabber's recovery tool could not be started.\n"
                     "Please reinstall Video Grabber.")


def verify_recovery_present() -> None:
    """§4: on every normal launch. Missing lifeline -> log + MessageBox,
    but the app still starts."""
    if recovery_available():
        return
    recovery_log("recovery.exe MISSING at supervisor launch check")
    _log("recovery tool missing")
    _message_box("The recovery tool is missing. Please reinstall Video Grabber.\n\n"
                 "The app will still start, but its safety net is gone until you reinstall.")


# -------------------------------------------------------------- updater ----
def updater_bat_cmdline(bat: Path, root: Path, pkg: Path, tag: str,
                        mode: str = "") -> str:
    """The cmd command line for updater.bat (fix 2). Passing a .bat
    path with spaces inside a subprocess LIST makes cmd strip the
    first and last quote of the joined line and mangle every path
    (the real install folder is "video 3 grabber"). The reliable
    form is one pre-built string: cmd /s /c with the whole command
    wrapped in ONE outer pair of quotes."""
    inner = f'"{bat}" "{root}" "{pkg}" "{tag}"'
    if mode:
        inner += f' "{mode}"'
    return f'cmd /s /c "{inner}"'


def _run_bat(pkg: Path, target, mode: str = "") -> int:
    cmdline = updater_bat_cmdline(UPDATER_BAT, ROOT, pkg, target or "", mode)
    _log(f"updater: running fallback bat ({mode or 'full'}): {cmdline}")
    try:
        return subprocess.call(cmdline, cwd=str(ROOT))
    except Exception as e:
        _log(f"updater: updater.bat failed to run: {e}")
        return 1


def run_updater_flow() -> None:
    """Section 5: supervisor runs updater_ui.py (via the app host, from
    a staging copy) and falls back to updater.bat on a non-zero exit.
    The updater swaps the app folder only; host files it staged are swapped by
    updater.bat AFTER the updater host has exited (fix 3)."""
    staging = vg_state.staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    src = APP_DIR / "updater_ui.py"
    staged_script = staging / "updater_ui.py"
    code = 1
    try:
        if not src.is_file():
            raise FileNotFoundError(f"updater_ui.py not found at {src}")
        shutil.copy2(src, staged_script)
        env = dict(os.environ)
        env["VG_ENTRY_SCRIPT"] = str(staged_script)
        env["VG_SUPERVISED"] = "1"   # we hold the mutex; the host must not try
        env["VG_INSTALL_ROOT"] = str(ROOT)
        env["VG_STATE_DIR"] = str(STATE_DIR)
        env.pop("VG_LAUNCH_ID", None)
        cmd = [str(HOST_EXE)] if getattr(sys, "frozen", False) else \
              [sys.executable, str(HOST_SCRIPT)]
        _log(f"updater: launching {cmd} entry={staged_script}")
        proc = subprocess.Popen(cmd, env=env, cwd=str(staging))
        _require_job(proc, "updater host")
        code = proc.wait()
    except SupervisorFatal:
        raise
    except Exception as e:
        _log(f"updater: supervisor-side failure: {e}")
        code = 1
    target = None

    host_flag = False

    def _clear(state):
        nonlocal target, host_flag
        target = (state.get("update_target") or {}).get("tag")
        # Set ONLY by the updater run that just staged host files. A
        # leftover staging\pkg from an earlier update must never
        # trigger a host swap (it would re-install an old, possibly
        # blacklisted, host over a restored one).
        host_flag = bool(state.get("host_swap"))
        state["update_pending"] = False
        state.pop("update_target", None)
        state.pop("host_swap", None)
    mutate_state(_clear)
    pkg = staging / "pkg"
    host_pending = host_flag and (
        (pkg / "VideoGrabberApp.exe").is_file() or
        (pkg / "VideoGrabberApp_internal").is_dir())
    if code != 0 and vg_state.schema_gate_error():
        # The updater refused to run against a newer-schema state
        # (post-audit fix 4). The bat fallback must NOT run either:
        # it would swap program files while every component is
        # refusing to touch the state that describes them.
        _log("updater: blocked by the schema gate; no bat fallback")
        vg_state.set_update_result(
            target, False,
            "Update blocked: the saved state was written by a newer "
            "version of Video Grabber. Run the full setup to update.")
        return
    if code != 0:
        _log(f"updater: updater_ui exited {code}; falling back to updater.bat")
        if UPDATER_BAT.is_file() and (pkg / "app" / "server.py").is_file():
            bat_code = _run_bat(pkg, target)
            if bat_code == 0:
                shutil.rmtree(pkg, ignore_errors=True)
                vg_state.set_update_result(
                    target, True, "Update installed by the fallback updater.")
            else:
                vg_state.set_update_result(
                    target, False,
                    "Update failed in both the updater and its fallback; "
                    "the previous version was kept.")
        else:
            _log("updater: no staged pkg for updater.bat; relaunching as-is (5.5)")
            vg_state.set_update_result(
                target, False,
                "The update could not start (nothing was staged for "
                "the fallback updater); the previous version was kept.")
        return
    _log("updater: updater_ui success")
    if host_pending:
        # The app folder is already swapped by the updater (stamped
        # too, unless this host swap was pending, in which case the
        # bat stamps below); the host files are still staged. The
        # updater host has exited, so the bat can rename them safely.
        _log("updater: host files staged; running bat host swap")
        bat_code = _run_bat(pkg, target, mode="host")
        if bat_code == 0:
            def _cv(state):
                if target:
                    state["current_version"] = target
            mutate_state(_cv)
            shutil.rmtree(pkg, ignore_errors=True)
        else:
            # The bat rolled the host files back, but app\ is already
            # the NEW version: new app on an old host is a mixed install.
            # Put the previous app\ back so the install is consistent.
            _revert_app_after_failed_host_swap()
            vg_state.set_update_result(
                target, False,
                "The update could not finish (the app-host swap failed); "
                "the previous version was kept and the update will be "
                "offered again.")
    else:
        shutil.rmtree(pkg, ignore_errors=True)   # never leave a stale pkg


def _revert_app_after_failed_host_swap() -> None:
    old = ROOT / "app.old"
    app = ROOT / "app"
    try:
        if old.is_dir():
            if app.exists():
                shutil.rmtree(app)
            os.replace(old, app)
            _log("updater: host swap failed; previous app\\ restored")
        else:
            _log("updater: host swap failed and no app.old to restore")
    except Exception as e:
        _log(f"updater: could not restore previous app\\: {e}")


# ----------------------------------------------------------------- child ---
def spawn_child(extra_args, launch_id: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["VG_LAUNCH_ID"] = launch_id
    env["VG_SUPERVISED"] = "1"
    if getattr(sys, "frozen", False):
        cmd = [str(HOST_EXE), *extra_args]
    else:
        cmd = [sys.executable, str(HOST_SCRIPT), *extra_args]
    _log(f"spawn child launch_id={launch_id}: {cmd}")
    proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
    _require_job(proc, "app host")
    return proc


def judge_unjudged_run() -> None:
    """Next-boot adjudication (§1): a previous run with no outcome is
    judged from the markers it managed to record."""
    def _judge(state):
        runs = state.get("runs") or []
        if runs and runs[-1].get("outcome") is None:
            last = runs[-1]
            if last.get("exit_code") == HOST_DUPLICATE_EXIT:
                outcome = "duplicate"
            else:
                markers = set(last.get("markers_seen") or [])
                lid = last.get("launch_id")
                if lid:
                    if read_marker(APP_ALIVE, lid):
                        markers.add("app_alive")
                    if read_marker(STARTUP_OK, lid):
                        markers.add("startup_ok")
                    last["markers_seen"] = sorted(markers)
                outcome = decide_outcome(markers, alive_at_deadline=False)
            last["outcome"] = outcome
            count = apply_outcome(state, outcome)
            _log(f"judged previous run {last.get('launch_id')} as {outcome}; crash_count={count}")
    mutate_state(_judge)


def supervise(extra_args) -> int:
    """One child lifecycle. Returns a flow token:
    'exit' | 'recovery' | 'relaunch' (an update ran; launch again)."""
    launch_id = secrets.token_hex(3)
    clear_markers()

    def _begin(state):
        state["runs"].append({"launch_id": launch_id,
                              "started_at": int(time.time()),
                              "outcome": None, "markers_seen": []})
        paths = state.get("last_paths") or {}
        paths["install_dir"] = str(ROOT)
        state["last_paths"] = paths
        if INSTALL_VERSION.is_file():
            try:
                state["current_version"] = INSTALL_VERSION.read_text(
                    encoding="utf-8").strip() or state.get("current_version")
            except OSError:
                pass
    mutate_state(_begin)

    try:
        child = spawn_child(extra_args, launch_id)
    except SupervisorFatal:
        raise
    except Exception as e:
        # The host itself is missing/unstartable - that IS a startup
        # crash; judge it instead of dying with a traceback.
        _log(f"spawn failed for run {launch_id}: {e}")

        def _fail(state):
            for r in state.get("runs") or []:
                if r.get("launch_id") == launch_id:
                    r["outcome"] = "crash"
            return apply_outcome(state, "crash")
        count = mutate_state(_fail)
        if count >= 3:
            _auto_blacklist_current()
            return "recovery"
        return "exit"
    markers_seen = set()
    settled = None            # 'ok' | 'legacy_ok' | 'backend_failed'
    watch_start = time.monotonic()   # monotonic: an NTP clock jump
    # mid-watch must not stretch or shrink the judging window

    def _record_markers():
        nonlocal markers_seen
        found = set()
        if read_marker(APP_ALIVE, launch_id):
            found.add("app_alive")
        if read_marker(STARTUP_OK, launch_id):
            found.add("startup_ok")
        if found != markers_seen:
            markers_seen |= found
            snapshot = sorted(markers_seen)

            def _upd(state):
                for r in state.get("runs") or []:
                    if r.get("launch_id") == launch_id:
                        r["markers_seen"] = snapshot
            mutate_state(_upd)

    exit_code_seen = None

    def _record_exit_code():
        nonlocal exit_code_seen
        rc = child.poll()
        if rc is None or exit_code_seen is not None:
            return
        exit_code_seen = rc

        def _rec(state):
            for r in state.get("runs") or []:
                if r.get("launch_id") == launch_id:
                    r["exit_code"] = rc
        mutate_state(_rec)

    while True:
        _record_markers()
        exited = child.poll() is not None
        if exited:
            _record_exit_code()
        if settled is None:
            settled = judge_step(markers_seen, not exited,
                                 time.monotonic() - watch_start)
        if exited:
            break
        if settled == "crash":
            _log(f"run {launch_id}: no window after "
                 f"{int(HARD_CAP_SECONDS)}s; terminating the hung host")
            try:
                child.kill()
                child.wait(timeout=10)
            except Exception as e:
                _log(f"could not terminate hung host: {e}")
            break
        if settled in ("ok", "legacy_ok"):
            # Settled: stop polling and block until the child exits
            # (fix 12). The outcome cannot change any more.
            child.wait()
            break
        time.sleep(POLL_SECONDS)

    _record_exit_code()   # the settled path leaves via wait(), not poll
    _record_markers()
    if child.poll() == HOST_DUPLICATE_EXIT:
        # The host refused itself: another host already owns the app
        # (post-audit fix 1). Not a crash, never counted.
        outcome = "duplicate"
    else:
        outcome = settled or decide_outcome(markers_seen, alive_at_deadline=False)
    count = 0

    def _finish(state):
        nonlocal count
        for r in state.get("runs") or []:
            if r.get("launch_id") == launch_id:
                r["outcome"] = outcome
                r["markers_seen"] = sorted(markers_seen)
                if exit_code_seen is not None:
                    r["exit_code"] = exit_code_seen
        count = apply_outcome(state, outcome)
    mutate_state(_finish)
    _log(f"run {launch_id} outcome={outcome} markers={sorted(markers_seen)} crash_count={count}")

    if count >= 3 and outcome in ("crash", "backend_failed"):
        # Fix 6: the version that crashed its way here is blacklisted
        # BEFORE Recovery opens, so a restore or rollback can never
        # leave the updater offering it again.
        _auto_blacklist_current()
        return "recovery"
    if BOOT_FLAG.exists():
        try:
            BOOT_FLAG.unlink()
        except OSError:
            pass
        return "recovery"

    def _pending(state):
        return bool(state.get("update_pending"))
    if mutate_state(_pending):
        run_updater_flow()
        return "relaunch"
    return "exit"


HEALTHY_RUN_SECONDS = 120.0


def last_run_was_healthy(now=None) -> bool:
    """The relaunch budget (fix 8) refills only after a run that
    settled ok AND stayed up for HEALTHY_RUN_SECONDS. A version that
    starts fine and immediately asks for another update would
    otherwise reset the cap every cycle and relaunch forever."""
    try:
        last = (load_state().get("runs") or [{}])[-1]
        if last.get("outcome") not in ("ok", "legacy_ok"):
            return False
        started = float(last.get("started_at") or 0)
        return ((now if now is not None else time.time()) - started) \
            >= HEALTHY_RUN_SECONDS
    except Exception:
        return False


def main(argv=None) -> int:
    try:
        return _main_inner(argv)
    except SupervisorFatal as e:
        # Post-audit fix 2: a supervisor that cannot control its
        # children exits (the child was already terminated).
        _log(f"supervisor exiting: {e}")
        return 1


def _main_inner(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass
    try:
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.unlink()
    except Exception:
        pass

    if not acquire_instance_lock():
        # A supervisor is already running (maybe stuck). A second launch
        # asking for Recovery must still get it: this is the only way
        # out of a hung start that holds the mutex.
        if ("--recovery" in argv or _shift_held()) and recovery_available():
            launch_recovery("explicit request (a supervisor was already running)")
            return 0
        _log("another supervisor instance is running; exiting")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _schema_err = vg_state.schema_gate_error()
    if _schema_err:
        _log(f"refusing to run: {_schema_err}")
        _message_box(_schema_err)
        return 1
    judge_unjudged_run()

    want_recovery = "--recovery" in argv or _shift_held()
    if BOOT_FLAG.exists():
        want_recovery = True
        try:
            BOOT_FLAG.unlink()
        except OSError:
            pass
    if want_recovery:
        if recovery_available():
            launch_recovery("explicit request")
        else:
            recovery_log("recovery requested but recovery tool is MISSING")
            _message_box("The recovery tool is missing. Please reinstall Video Grabber.")
        return 0

    verify_recovery_present()

    # An update may have been requested while no supervisor was resident
    # (parachute case): honour it before launching.
    def _pending(state):
        return bool(state.get("update_pending"))
    if mutate_state(_pending):
        run_updater_flow()

    simulate = [a for a in argv if a.startswith("--simulate")]
    passthrough = [a for a in argv if not a.startswith("--simulate")
                   and a != "--recovery"]
    passthrough += simulate

    relaunches = 0
    while True:
        flow = supervise(passthrough)
        # Fix 8: the cap counts CONSECUTIVE update relaunches. A run
        # that settled ok proves the app is healthy, so the budget
        # refills; without this a long tray session could only ever
        # take 3 updates before the supervisor gave up and exited.
        if last_run_was_healthy():
            relaunches = 0
        if flow == "recovery":
            if recovery_available():
                launch_recovery("flow")
            else:
                recovery_log("recovery needed but recovery tool is MISSING")
                _message_box("The recovery tool is missing. Please reinstall Video Grabber.")
            return 0
        if flow == "relaunch":
            relaunches += 1
            if relaunches > 3:
                _log("too many consecutive update relaunches; stopping")
                return 0
            continue
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        _log("supervisor crashed:\n" + traceback.format_exc()[-1500:])
        raise
