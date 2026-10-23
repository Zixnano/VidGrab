# vg_state.py: the ONE shared state module for the Supervisor
# Architecture (contract section 2). Stdlib only. Imported by
# launcher.py (bundled into the supervisor exe), updater_ui.py and
# gui_qt.py (shipped in app\), and recovery_ui.py (bundled into
# recovery.exe). Before this module existed, the state and lock code
# lived in four hand-synced copies; they had already started to drift.
#
# The contract, in one place:
#   state file  %LOCALAPPDATA%\VideoGrabber\recovery_state.json
#               (VG_STATE_DIR overrides the directory, for tests)
#   lock        recovery_state.lock next to it, content "pid:token"
#   writes      atomic: write tmp file, os.replace over the real one
#   unknown     keys in the JSON are preserved on rewrite
#   runs[] and  fix_attempts[] are pruned to the newest 20 on save
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

# The state format is FROZEN at this version (post-audit fix 4):
# vg_state.py is bundled into the long-lived supervisor AND shipped
# in app\ where thin updates replace it, so the two copies can be
# different ages. A copy that meets a state file written by a NEWER
# schema refuses to run instead of guessing at fields it does not
# know. Raising this number is a deliberate, breaking act.
SCHEMA_VERSION = 1

STATE_FILENAME = "recovery_state.json"
LOCK_FILENAME = "recovery_state.lock"
LOG_FILENAME = "recovery.log"
LOG_ROTATE_BYTES = 512_000

DEFAULT_STATE = {
    "schema_version": SCHEMA_VERSION,
    "crash_count": 0,
    "last_good_version": None,
    "blacklist": [],
    "runs": [],
    "update_pending": False,
    "last_paths": {"data_home": "", "install_dir": ""},
    "last_release_list": [],
    "current_version": None,
    "fix_attempts": [],
    "last_update_result": None,
}


# ---------------------------------------------------------------- paths ----
def state_dir() -> Path:
    override = os.environ.get("VG_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoGrabber"
    return Path.home() / "AppData" / "Local" / "VideoGrabber"


def state_file() -> Path:
    return state_dir() / STATE_FILENAME


def lock_file() -> Path:
    return state_dir() / LOCK_FILENAME


def log_file() -> Path:
    return state_dir() / LOG_FILENAME


def backups_dir() -> Path:
    return state_dir() / "backups"


def staging_dir() -> Path:
    return state_dir() / "staging"


def boot_flag() -> Path:
    return state_dir() / "boot_recovery.flag"


def install_root(dev_fallback=None) -> Path:
    """Where the install lives. VG_INSTALL_ROOT wins (the supervisor
    sets it for the updater); then the path recorded in state; then,
    frozen, the exe's folder (or its parent for the recovery folder, which sits
    one level down); in source runs the caller's own folder stands in.
    """
    override = os.environ.get("VG_INSTALL_ROOT")
    if override:
        return Path(override)
    try:
        recorded = (load_state().get("last_paths") or {}).get("install_dir") or ""
    except Exception:
        recorded = ""
    if recorded and Path(recorded).is_dir():
        return Path(recorded)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "recovery" else exe_dir
    if dev_fallback is not None:
        return Path(dev_fallback)
    return Path.cwd()


# ------------------------------------------------------------ processes ----
def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
            if not handle:
                return False
            code = ctypes.c_ulong(0)
            k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------- lock ----
class StateLock:
    """PID + token lock for the state file.

    Stale takeover (the owner pid is dead) is done by ATOMIC RENAME,
    never by unlinking the live path: the old code unlinked the path
    directly, and in the gap another process could create a FRESH lock
    that the unlink then deleted. Here the stale file is renamed to a
    private grave name first; its content is re-read from the grave
    and compared with what we decided was stale; only an exact match
    is deleted. If the grave holds someone else's fresh lock, it is
    renamed back untouched and we simply wait.

    A lock whose owner is alive is never taken. If a live writer holds
    the lock past the timeout we proceed WITHOUT it (and say so via
    the logger): a lock must never brick the app it protects.
    """

    def __init__(self, timeout: float = 2.5, path: Path = None, logger=None):
        self.timeout = timeout
        self.path = Path(path) if path else lock_file()
        self.logger = logger
        self.held = False
        self._borrowed = False
        self._token = uuid.uuid4().hex

    def _content(self) -> str:
        return f"{os.getpid()}:{self._token}"

    @staticmethod
    def _parse(raw: str):
        raw = (raw or "").strip()
        if ":" in raw:
            pid_s, _, token = raw.partition(":")
        else:
            pid_s, token = raw, ""
        try:
            return int(pid_s or 0), token
        except ValueError:
            return 0, token

    def _read(self):
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self._content().encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                raw = self._read()
                if raw is None:
                    continue  # vanished between create and read; retry
                pid, token = self._parse(raw)
                if pid == 0:
                    # An empty or unparseable lock: either a creator
                    # caught mid-write (a window of microseconds) or
                    # the corpse of one killed between create and
                    # write. Only the corpse is safe to take, so age
                    # decides: untouched for 5 s means abandoned.
                    # (Before this, a pid-0 lock was NEVER stale, and
                    # every writer burned its full timeout and then
                    # proceeded unlocked, forever.)
                    try:
                        age = time.time() - self.path.stat().st_mtime
                    except OSError:
                        age = 99.0
                    if age > 5.0 and self._take_over_stale(raw):
                        continue
                if pid == os.getpid():
                    # Same process already holds it (nested mutate):
                    # borrow it; our exit must not release the outer lock.
                    self.held = True
                    self._borrowed = True
                    return self
                if pid and not pid_alive(pid):
                    if self._take_over_stale(raw):
                        continue  # stale lock disposed of; retry create
                if time.time() >= deadline:
                    if self.logger:
                        self.logger(
                            f"state lock held by live pid {pid}; "
                            "proceeding unlocked")
                    return self
                time.sleep(0.05)
            except OSError:
                return self

    def _take_over_stale(self, expected_raw: str) -> bool:
        """Rename the stale lock aside and verify it really is the
        stale file we read. Returns True when the path is now clear
        (or the stale file is disposed of), False to keep waiting."""
        grave = self.path.with_name(
            self.path.name + f".stale-{os.getpid()}-{self._token[:8]}")
        try:
            os.replace(self.path, grave)
        except OSError:
            return False  # someone else moved it first; re-read and wait
        try:
            grave_raw = grave.read_text(encoding="utf-8")
        except OSError:
            grave_raw = None
        if grave_raw is not None and grave_raw.strip() != (expected_raw or "").strip():
            # We renamed a FRESH lock that replaced the stale one in
            # the gap. Put it back untouched; never delete it.
            try:
                if not self.path.exists():
                    os.replace(grave, self.path)
            except OSError:
                pass
            return False
        try:
            grave.unlink()
        except OSError:
            pass
        return True

    def __exit__(self, *exc):
        if self.held and not self._borrowed:
            try:
                raw = self._read()
                if raw is not None:
                    _, token = self._parse(raw)
                    if token == self._token:
                        self.path.unlink()
            except OSError:
                pass
        self.held = False
        return False


def schema_gate_error():
    """None when the on-disk state is safe for this copy to touch; a
    message when its schema_version is NEWER than SCHEMA_VERSION
    (post-audit fix 4). A missing or unreadable file is fine: the
    load path already falls back to defaults."""
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        v = data.get("schema_version")
        if isinstance(v, int) and v > SCHEMA_VERSION:
            return (f"recovery_state.json has schema_version {v}, but "
                    f"this build only understands {SCHEMA_VERSION}. "
                    "Update Video Grabber (full setup) before running "
                    "this component.")
    return None


# ---------------------------------------------------------------- state ----
def load_state() -> dict:
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = dict(DEFAULT_STATE)
            merged.update(data)          # unknown keys are preserved
            return merged
    except Exception:
        pass
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    state["runs"] = list(state.get("runs") or [])[-20:]
    state["fix_attempts"] = list(state.get("fix_attempts") or [])[-20:]
    tmp = state_dir() / f"{STATE_FILENAME}.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, state_file())        # atomic


_MUTATE_RLOCK = threading.RLock()


def mutate_state(fn, logger=None):
    """Read-modify-write under the state lock. fn(state) -> result."""
    with _MUTATE_RLOCK:
        with StateLock(logger=logger):
            state = load_state()
            result = fn(state)
            save_state(state)
            return result


# ------------------------------------------------------- update results ----
def set_update_result(tag, ok: bool, message: str) -> None:
    def _set(state):
        state["last_update_result"] = {
            "tag": tag, "ok": bool(ok), "message": str(message),
            "time": int(time.time())}
    mutate_state(_set)


def pop_update_result():
    """Read last_update_result and clear it in the same locked pass,
    so the GUI shows it exactly once."""
    def _pop(state):
        result = state.get("last_update_result")
        state["last_update_result"] = None
        return result
    return mutate_state(_pop)


# ------------------------------------------------------------------ log ----
def recovery_log(msg: str, source: str = "recovery") -> None:
    """Append to recovery.log, rotating at 512 KB (one .1 kept)."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        path = log_file()
        try:
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {source} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- hashes ---
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
