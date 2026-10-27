# recovery_ui.py - the Lifeline. Built by PyInstaller into recovery.exe
# (onedir), living at <install root>\recovery\, OUTSIDE app\, updated only
# by the full installer, never by thin updates (contract §1/§6).
#
# Imports ONLY stdlib + PySide6. Never imports app modules: everything it
# needs comes from the shared state contract
# (%LOCALAPPDATA%\VideoGrabber\recovery_state.json), the resolved paths
# recorded there, and the file system.
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTextEdit, QVBoxLayout,
    QWidget, QFileDialog,
)

REPO = "Zixnano/VidGrab"
APP_PORT = 5757

# ------------------------------------------------------------ state --------
# One shared implementation: vg_state.py (fix 10), bundled into
# recovery.exe by PyInstaller like this file.
import vg_state
from vg_state import (StateLock, load_state, mutate_state, save_state,
                      sha256_file, state_dir)

STATE_DIR = state_dir()
STATE_FILE = vg_state.state_file()
STATE_LOCK = vg_state.lock_file()
RECOVERY_LOG = vg_state.log_file()
BACKUPS_DIR = vg_state.backups_dir()
DEFAULT_STATE = vg_state.DEFAULT_STATE
_pid_alive = vg_state.pid_alive


def recovery_log(msg: str) -> None:
    vg_state.recovery_log(msg, "recovery")


# ------------------------------------------------------------- paths -------
def install_root() -> Path:
    return vg_state.install_root(Path(__file__).resolve().parent)


def data_home() -> Path:
    st = load_state()
    recorded = (st.get("last_paths") or {}).get("data_home") or ""
    if recorded:
        return Path(recorded)
    return Path.home() / "Downloads" / "VideoGrabber"


def current_tag(root: Path):
    try:
        tag = (root / "install-version.txt").read_text(encoding="utf-8").strip()
        return tag or None
    except OSError:
        return load_state().get("current_version")


# ----------------------------------------------------------- manifest ------
MANIFEST_SKIP_DIRS = {"__pycache__"}
MANIFEST_SKIP_SUFFIX = {".pyc"}
MANIFEST_SKIP_NAMES = {"manifest.json"}


def build_manifest(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in MANIFEST_SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in MANIFEST_SKIP_SUFFIX or p.name in MANIFEST_SKIP_NAMES:
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        out[rel.as_posix()] = {"sha256": h.hexdigest(), "size": p.stat().st_size}
    return out


def verify_manifest(app_dir: Path):
    """Returns (ok, missing, changed). ok=None when no manifest exists."""
    mf_path = app_dir / "manifest.json"
    if not mf_path.is_file():
        return None, [], []
    try:
        manifest = json.loads(mf_path.read_text(encoding="utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, dict):
            return None, [], []
    except Exception:
        return None, [], []
    missing, changed = [], []
    for rel, info in files.items():
        p = app_dir / rel
        if not p.is_file():
            missing.append(rel)
            continue
        if p.stat().st_size != info.get("size"):
            changed.append(rel)
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != info.get("sha256"):
            changed.append(rel)
    return (not missing and not changed), missing, changed


# ---------------------------------------------------------- processes ------
def process_image_path(pid: int) -> str:
    """ctypes QueryFullProcessImageNameW (contract §5: stdlib only)."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(32768)
        ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        k32.CloseHandle(handle)
        return buf.value if ok else ""
    except Exception:
        return ""


def port_owner(port: int = APP_PORT):
    """(pid, image_path) of the LISTENING owner of the port, or
    (None, ''). Uses netstat -ano for the PID (system tool, same idiom
    as updater.bat's tasklist) and ctypes for the image path."""
    if sys.platform != "win32":
        return None, ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                    and parts[1].endswith(f":{port}"):
                pid = int(parts[4])
                return pid, process_image_path(pid)
    except Exception:
        pass
    return None, ""


def health_identity(port: int = APP_PORT):
    """If OUR backend answers on the port, return its JSON, else None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("app") == "videograbber":
            return data
    except Exception:
        pass
    return None


def port_is_open(port: int = APP_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def app_processes(root: Path):
    """Running processes that belong to this install (ground truth for
    'the app is running' - never a lock file alone, contract §5)."""
    found = []
    pid, _ = port_owner()
    if pid:
        img = process_image_path(pid)
        if img:
            found.append((pid, img))
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                m = re.match(r'"([^"]+)","(\d+)"', line.strip())
                if m and m.group(1).lower() in ("videograbber.exe", "videograbberapp.exe"):
                    p = int(m.group(2))
                    img = process_image_path(p)
                    if img and str(root).lower() in img.lower():
                        if all(p != x[0] for x in found):
                            found.append((p, img))
        except Exception:
            pass
    return found


def kill_pid(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
        return not _pid_alive(pid)
    except Exception:
        return False


# ------------------------------------------------------------ pruning ------
def _prune_prefixed(parent: Path, prefix: str, keep: int = 2) -> None:
    """Fix 14: the *_broken-* safety copies are ~500 MB of runtime or
    a full app each; without pruning they accumulate forever. The
    names carry timestamps, so name order is age order."""
    try:
        if not parent.is_dir():
            return
        dirs = sorted((d for d in parent.iterdir()
                       if d.is_dir() and d.name.startswith(prefix)),
                      key=lambda d: d.name)
        for d in dirs[:-keep] if keep else dirs:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def prune_broken(root: Path) -> None:
    for prefix in ("app_broken-", "runtime_broken-",
                   "VideoGrabberApp_internal_broken-"):
        _prune_prefixed(root, prefix)
    # the host exe's broken copies are files, not dirs
    try:
        files = sorted((f for f in root.glob("VideoGrabberApp_broken-*.exe")),
                       key=lambda f: f.name)
        for f in files[:-2]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def prune_snapshots() -> None:
    _prune_prefixed(BACKUPS_DIR, "app_snapshot-")


# ------------------------------------------------------------ backups ------
def list_backups():
    out = []
    if BACKUPS_DIR.is_dir():
        for d in sorted(BACKUPS_DIR.iterdir(), reverse=True):
            meta = {}
            try:
                meta = json.loads((d / "backup_meta.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            if d.is_dir() and (d / "app").is_dir():
                out.append({"dir": d, "tag": meta.get("tag") or d.name,
                            "created_at": meta.get("created_at", 0)})
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out


def snapshot_app(root: Path, label: str) -> Path:
    """Copy the current install (app + host + settings) into backups
    LABEL, so a restore can undo everything (fix 5)."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS_DIR / label
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root / "app", dest / "app")
    exe = root / "VideoGrabberApp.exe"
    if exe.is_file():
        shutil.copy2(exe, dest / "VideoGrabberApp.exe")
    internal = root / "VideoGrabberApp_internal"
    if internal.is_dir():
        shutil.copytree(internal, dest / "VideoGrabberApp_internal")
    sj = data_home() / "settings.json"
    if sj.is_file():
        shutil.copy2(sj, dest / "settings.json")
    (dest / "backup_meta.json").write_text(json.dumps(
        {"tag": label, "created_at": int(time.time())}), encoding="utf-8")
    prune_snapshots()
    return dest


# Every component a thin update can change. A restore or rollback
# swaps whichever of these the source actually carries.
INSTALL_COMPONENTS = ("app", "VideoGrabberApp.exe", "VideoGrabberApp_internal")


def _broken_path(root: Path, name: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    if name == "VideoGrabberApp.exe":
        stem, suffix = f"VideoGrabberApp_broken-{ts}", ".exe"
    else:
        stem, suffix = f"{name}_broken-{ts}", ""
    out = root / (stem + suffix)
    n = 1
    while out.exists():
        n += 1
        out = root / (f"{stem}-{n}" + suffix)
    return out


def swap_install(root: Path, src_dir: Path) -> None:
    """Rename-dance swap of every install component present in
    src_dir (fix 5): the current component is kept as a *_broken-<ts>
    copy beside the install, never deleted first; on failure, exactly
    this call's changes are rolled back in reverse."""
    changed = []   # (dst, broken_or_None)
    try:
        for name in INSTALL_COMPONENTS:
            src = src_dir / name
            dst = root / name
            if not src.exists():
                continue
            broken = None
            if dst.exists():
                broken = _broken_path(root, name)
                os.replace(dst, broken)
            changed.append((dst, broken))
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    except Exception:
        for dst, broken in reversed(changed):
            try:
                if dst.exists():
                    shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
                if broken is not None and broken.exists():
                    os.replace(broken, dst)
            except Exception:
                pass
        raise
    prune_broken(root)
    recovery_log(f"install components swapped from {src_dir}")


def _blacklist_replaced(old_tag, new_tag) -> None:
    """Fix 6, second half: the version a restore or rollback REPLACED
    stays blacklisted, so the updater can never offer it again."""
    if not old_tag or old_tag == new_tag:
        return

    def _add(state):
        if old_tag != state.get("last_good_version") and \
                old_tag not in (state.get("blacklist") or []):
            state["blacklist"] = list(state.get("blacklist") or []) + [old_tag]
            return old_tag
        return None
    if mutate_state(_add):
        recovery_log(f"blacklisted replaced version {old_tag}")


def restore_backup(backup: dict) -> None:
    root = install_root()
    replaced = current_tag(root)
    swap_install(root, backup["dir"])
    _blacklist_replaced(replaced, str(backup.get("tag") or ""))
    saved_settings = backup["dir"] / "settings.json"
    if saved_settings.is_file():
        sj = data_home() / "settings.json"
        sj.parent.mkdir(parents=True, exist_ok=True)
        if sj.is_file():
            shutil.copy2(sj, data_home() /
                         f"settings.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(saved_settings, sj)
    tag = str(backup.get("tag") or "")
    if re.fullmatch(r"v\d+", tag):
        # The tree on disk is now this tag's code; install-version.txt
        # must say so, or the updater/supervisor reason from the old one.
        try:
            (root / "install-version.txt").write_text(tag, encoding="utf-8")
        except OSError:
            pass
    recovery_log(f"restored backup {backup['tag']}")


def _extract_zip(zpath: Path, dest: Path) -> None:
    """Extract with the 'data' filter where available (Python 3.12+):
    entries that would escape the destination (zip-slip) are refused."""
    with zipfile.ZipFile(zpath) as z:
        try:
            z.extractall(dest, filter="data")
        except TypeError:            # older Python: no filter support
            z.extractall(dest)


# ------------------------------------------------------- downloads ---------
def download_release_asset(tag: str, asset: str, dest: Path) -> None:
    url = f"https://github.com/{REPO}/releases/download/{tag}/{asset}"
    req = urllib.request.Request(url, headers={"User-Agent": "VideoGrabber-Recovery"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 262144)


def verify_zip_sums(tag: str, asset_name: str, zpath: Path) -> None:
    """Fix 11: when the release carries SHA256SUMS-<tag>.txt, the
    downloaded zip must match it. Releases published before the sums
    asset existed fall back to the manifest checks inside the
    package, with the gap logged, never silently."""
    sums = zpath.parent / f"SHA256SUMS-{tag}.txt"
    try:
        download_release_asset(tag, sums.name, sums)
        text = sums.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        recovery_log(f"no SHA256SUMS for {tag} ({e}); manifest checks only")
        return
    expected = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            expected = parts[0].strip().lower()
            break
    if expected is None:
        raise RuntimeError(f"SHA256SUMS-{tag}.txt does not list {asset_name}")
    if sha256_file(zpath) != expected:
        raise RuntimeError(
            f"SHA-256 mismatch for {asset_name}: the download is "
            "corrupt or tampered with; refusing to install it")
    recovery_log(f"SHA-256 verified for {asset_name}")


def rollback_to(tag: str) -> str:
    root = install_root()
    work = STATE_DIR / "rollback"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    zpath = work / f"VideoGrabber-app-{tag}.zip"
    download_release_asset(tag, zpath.name, zpath)
    verify_zip_sums(tag, zpath.name, zpath)
    _extract_zip(zpath, work / "pkg")
    staged_app = None
    # Shallowest match wins: the real app\ sits directly under the
    # package root; a nested app\app\server.py inside it must never
    # be mistaken for the package root.
    for cand in sorted((work / "pkg").rglob("server.py"),
                       key=lambda c: len(c.parts)):
        if cand.parent.name == "app":
            staged_app = cand.parent
            break
    if staged_app is None:
        raise FileNotFoundError("downloaded package has no app\\server.py")
    replaced = current_tag(root)
    # the pkg root carries the host files too; swap every component
    swap_install(root, staged_app.parent)
    _blacklist_replaced(replaced, tag)
    (root / "install-version.txt").write_text(tag, encoding="utf-8")
    recovery_log(f"rolled back to {tag}")
    return tag


def redownload_runtime(tag: str) -> str:
    root = install_root()
    work = STATE_DIR / "runtime-dl"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    zpath = work / f"VideoGrabber-runtime-{tag}.zip"
    download_release_asset(tag, zpath.name, zpath)
    verify_zip_sums(tag, zpath.name, zpath)
    _extract_zip(zpath, work / "pkg")
    staged_rt = None
    for cand in (work / "pkg").rglob("site-packages"):
        if cand.parent.name == "runtime" and cand.is_dir():
            staged_rt = cand.parent
            break
    if staged_rt is None:
        raise FileNotFoundError("runtime package has no runtime\\site-packages")
    old = root / f"runtime_broken-{time.strftime('%Y%m%d-%H%M%S')}"
    if (root / "runtime").exists():
        os.replace(root / "runtime", old)
    try:
        shutil.copytree(staged_rt, root / "runtime")
    except Exception:
        if old.exists() and not (root / "runtime").exists():
            os.replace(old, root / "runtime")
        raise
    prune_broken(root)
    recovery_log(f"runtime re-downloaded from {tag}; previous kept at {old}")
    return tag


# ---------------------------------------------------------- redaction ------
_SECRET_PATTERNS = [
    (re.compile(r'("api_token"\s*:\s*")[^"]*(")'), r"\1***\2"),
    (re.compile(r'("token"\s*:\s*")[^"]{8,}(")'), r"\1***\2"),
    (re.compile(r'((?:https?|socks5?)://[^:/@\s]+:)[^@\s]+(@)'), r"\1***\2"),
    (re.compile(r'("proxy[^"]*"\s*:\s*"[^"]*://[^:/@"]+:)[^@"]+(@)'), r"\1***\2"),
]


def redact_text(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ---------------------------------------------------------- diagnosis ------
def run_diagnosis():
    """Contract §6 order: local checks first. Returns a list of finding
    dicts: {level: ok|warn|fail, title, detail, fix: id or None}."""
    findings = []
    root = install_root()
    home = data_home()
    state = load_state()

    def add(level, title, detail="", fix=None):
        findings.append({"level": level, "title": title,
                         "detail": detail, "fix": fix})

    # 0. last runs, from the state contract
    runs = state.get("runs") or []
    if runs:
        last = runs[-1]
        outcome = last.get("outcome")
        markers = ", ".join(last.get("markers_seen") or []) or "none"
        if outcome == "crash":
            add("fail", "Last launch crashed before the app started",
                f"launch {last.get('launch_id')}: no markers were written ({markers}). "
                "The failure is in the runtime or the app files, before the window.")
        elif outcome == "backend_failed":
            add("fail", "Last launch: window opened but the backend never answered",
                f"launch {last.get('launch_id')}: markers seen: {markers}. "
                "Check the ghost-process result below.")
        elif outcome in ("ok", "legacy_ok"):
            add("ok", f"Last launch outcome: {outcome}",
                f"markers: {markers}; crash_count is {state.get('crash_count')}.")
        elif outcome == "duplicate":
            add("ok", "Last launch was a duplicate, refused by design",
                "Another copy of the app was already running, so the "
                "new host exited at once (exit 42). Not a failure, "
                "and it does not count toward Recovery.")
        else:
            add("warn", "Last launch was never judged",
                "The supervisor was probably killed mid-run.")
    add("ok" if not state.get("blacklist") else "warn",
        f"Blacklisted versions: {', '.join(state.get('blacklist') or []) or 'none'}",
        f"Last known good: {state.get('last_good_version') or 'unknown'}; "
        f"installed: {current_tag(root) or 'unknown'}.")

    # 1. settings.json parses
    sj = home / "settings.json"
    if sj.is_file():
        try:
            parsed = json.loads(sj.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("top level is not an object")
            add("ok", "settings.json parses", str(sj))
        except Exception as e:
            add("fail", "settings.json is corrupt",
                f"{sj}: {e}. Auto-Fix moves it aside to settings.old-<date>.json "
                "and the app starts fresh.", fix="settings")
    else:
        add("warn", "settings.json not found", str(sj))

    # 2. app manifest
    ok, missing, changed = verify_manifest(root / "app")
    if ok is None:
        add("warn", "No manifest.json in app\\",
            "Integrity can't be verified. The build pipeline generates it; "
            "an install without one predates it or lost it.")
    elif ok:
        add("ok", "app\\ matches its manifest", "Every file's SHA-256 verified.")
    else:
        bits = []
        if missing:
            bits.append("missing: " + ", ".join(missing[:8]))
        if changed:
            bits.append("changed: " + ", ".join(changed[:8]))
        add("fail", "app\\ files are missing or corrupted",
            "; ".join(bits), fix="restore")
    if not (root / "app" / "server.py").is_file():
        add("fail", "app\\server.py is missing",
            "The app cannot start at all. Restore from backup or roll back.",
            fix="restore")

    # 3. critical runtime files (existence/size - contract §6.3)
    crit = [
        root / "runtime" / "site-packages" / "PySide6" / "__init__.py",
        root / "runtime" / "site-packages" / "flask" / "__init__.py",
        root / "runtime" / "site-packages" / "yt_dlp" / "__init__.py",
        root / "runtime" / "ffmpeg.exe",
        root / "runtime" / "deno.exe",
        root / "VideoGrabberApp.exe",
        root / "VideoGrabber.exe",
    ]
    bad = [str(p.relative_to(root)) for p in crit
           if not p.is_file() or p.stat().st_size == 0]
    if bad:
        add("fail", "Critical runtime files are missing",
            ", ".join(bad) + ". Re-download the runtime or reinstall.",
            fix="runtime")
    else:
        add("ok", "Critical runtime files present", "")

    # 4. smoke test (the host exe is the interpreter: DECISIONS.md)
    host = root / "VideoGrabberApp.exe"
    if host.is_file() or not getattr(sys, "frozen", False):
        try:
            env = dict(os.environ, VG_SMOKE_TEST="1", VG_SUPERVISED="1")
            cmd = [str(host)] if getattr(sys, "frozen", False) or host.is_file() \
                else [sys.executable, str(Path(__file__).parent / "app_host.py")]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=30, env=env, cwd=str(root))
            if proc.returncode == 0 and "SMOKE_OK" in proc.stdout:
                add("ok", "Runtime smoke test passed",
                    "PySide6.QtWidgets, flask and yt_dlp import cleanly.")
            else:
                tail = (proc.stderr or proc.stdout or "")[-600:]
                add("fail", "Runtime smoke test FAILED",
                    f"exit {proc.returncode}. {tail}", fix="runtime")
        except subprocess.TimeoutExpired:
            add("fail", "Runtime smoke test timed out (30s)",
                "An import hangs - the runtime is damaged.", fix="runtime")
        except Exception as e:
            add("warn", "Smoke test could not run", str(e))

    # 5. ghost process on the app port
    if health_identity():
        add("warn", "Something is already serving on port 5757 - and it IS Video Grabber",
            "A copy of the backend is running without a window (ghost), or the "
            "app is open right now. Auto-Fix can kill the ghost.", fix="ghost")
    elif port_is_open():
        pid, img = port_owner()
        who = img or "unknown program"
        ours = img and ("videograbber" in img.lower() or str(root).lower() in img.lower())
        add("fail" if ours else "warn",
            f"Port 5757 is held by {who}" + (f" (pid {pid})" if pid else ""),
            "It is our process - Auto-Fix can kill it." if ours else
            "It is NOT Video Grabber. Close that program; Video Grabber "
            "cannot use its port while it is held.",
            fix="ghost" if ours else None)
    else:
        add("ok", "Port 5757 is free", "")

    # 6. disk space
    try:
        free = shutil.disk_usage(str(root)).free
        if free < 150 * 1024 * 1024:
            add("fail", f"Disk almost full ({free // 1048576} MB free)",
                "Updates and restores will fail. Free space first.")
        elif free < 600 * 1024 * 1024:
            add("warn", f"Low disk space ({free // 1048576} MB free)",
                "Enough to run, tight for a runtime re-download (~500 MB).")
        else:
            add("ok", f"Disk space fine ({free // 1048576} MB free)", "")
    except Exception as e:
        add("warn", "Disk space check failed", str(e))

    return findings


def export_diagnostics(dest_zip: Path, findings) -> Path:
    root = install_root()
    home = data_home()
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        if STATE_FILE.is_file():
            z.writestr("recovery_state.json",
                       redact_text(STATE_FILE.read_text(encoding="utf-8", errors="replace")))
        if RECOVERY_LOG.is_file():
            z.writestr("recovery.log",
                       RECOVERY_LOG.read_text(encoding="utf-8", errors="replace")[-200_000:])
        sup_log = root / "launcher.log"
        if sup_log.is_file():
            z.writestr("launcher.log",
                       sup_log.read_text(encoding="utf-8", errors="replace")[-200_000:])
        sj = home / "settings.json"
        if sj.is_file():
            z.writestr("settings.redacted.json",
                       redact_text(sj.read_text(encoding="utf-8", errors="replace")))
        for logf in sorted(home.glob("*.log"))[:6]:
            try:
                z.writestr(f"app-logs/{logf.name}",
                           logf.read_text(encoding="utf-8", errors="replace")[-200_000:])
            except OSError:
                pass
        info = {
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "installed_tag": current_tag(root),
            "last_good_version": load_state().get("last_good_version"),
            "install_root": str(root),
            "data_home": str(home),
            "disk_free_mb": shutil.disk_usage(str(root)).free // 1048576,
            "findings": findings,
        }
        z.writestr("diagnosis.json", json.dumps(info, indent=1))
    recovery_log(f"diagnostics exported to {dest_zip}")
    return dest_zip


def _supervisor_mutex_held() -> bool:
    """True when a supervisor instance already holds the single
    instance mutex. CreateMutexW reports ERROR_ALREADY_EXISTS even
    though it hands us a handle; we close ours immediately."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        handle = k32.CreateMutexW(
            None, False, "Local\\VideoGrabber_SingleInstance_v1")
        exists = ctypes.get_last_error() == 183
        if handle:
            k32.CloseHandle(handle)
        return exists
    except Exception:
        return False


# ---------------------------------------------------------------- UI -------
class DiagnosisWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(run_diagnosis())
        except Exception as e:
            self.done.emit([{"level": "fail", "title": "Diagnosis itself failed",
                             "detail": str(e), "fix": None}])


DARK_QSS = """
QWidget { background: #14181f; color: #dbe2ec; font-size: 13px; }
QPushButton { background: #232b38; border: 1px solid #39445a; border-radius: 6px;
              padding: 7px 12px; }
QPushButton:hover { background: #2c3647; }
QPushButton:disabled { color: #69758a; }
QListWidget, QTextEdit, QComboBox { background: #0d1015; border: 1px solid #39445a;
              border-radius: 6px; }
QLabel#title { font-size: 19px; font-weight: bold; color: #ffffff; }
QLabel#sub { color: #8fa1b8; }
"""


class RecoveryWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Grabber - Recovery")
        self.resize(880, 640)
        self.setStyleSheet(DARK_QSS)
        self.findings = []
        self.worker = None

        lay = QVBoxLayout(self)
        title = QLabel("Video Grabber Recovery")
        title.setObjectName("title")
        lay.addWidget(title)
        sub = QLabel("The safety net. Diagnosis is local-first; nothing here "
                     "needs the internet except Rollback and Runtime repair.")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        row = QHBoxLayout()
        self.run_btn = QPushButton("Run Diagnosis")
        self.run_btn.clicked.connect(self._run)
        row.addWidget(self.run_btn)
        self.fix_btn = QPushButton("Auto-Fix (safest first)")
        self.fix_btn.clicked.connect(self._auto_fix)
        row.addWidget(self.fix_btn)
        self.export_btn = QPushButton("Export Diagnostics…")
        self.export_btn.clicked.connect(self._export)
        row.addWidget(self.export_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show_detail)
        lay.addWidget(self.list, 3)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        lay.addWidget(self.detail, 2)

        acts = QHBoxLayout()
        self.backup_combo = QComboBox()
        self._refresh_backups()
        acts.addWidget(self.backup_combo)
        b = QPushButton("Restore Backup")
        b.clicked.connect(self._restore)
        acts.addWidget(b)
        self.rollback_btn = QPushButton("Rollback")
        self.rollback_btn.clicked.connect(self._rollback)
        acts.addWidget(self.rollback_btn)
        self.runtime_btn = QPushButton("Re-download Runtime")
        self.runtime_btn.clicked.connect(self._runtime)
        acts.addWidget(self.runtime_btn)
        self.ghost_btn = QPushButton("Kill Ghost")
        self.ghost_btn.clicked.connect(self._kill_ghost)
        acts.addWidget(self.ghost_btn)
        lay.addLayout(acts)

        bl = QHBoxLayout()
        self.bl_list = QComboBox()
        self._refresh_blacklist()
        bl.addWidget(self.bl_list)
        b = QPushButton("Blacklist Installed Version")
        b.clicked.connect(self._bl_add_current)
        bl.addWidget(b)
        b = QPushButton("Remove Selected from Blacklist")
        b.clicked.connect(self._bl_remove)
        bl.addWidget(b)
        bl.addStretch(1)
        b = QPushButton("Relaunch Video Grabber")
        b.clicked.connect(self._relaunch)
        bl.addWidget(b)
        lay.addLayout(bl)

        self._refresh_rollback_label()
        recovery_log("recovery UI opened")

    # ------------------------------------------------------------ helpers --
    def _refresh_backups(self):
        self.backup_combo.clear()
        for bk in list_backups():
            when = time.strftime("%b %d %H:%M", time.localtime(bk["created_at"])) \
                if bk["created_at"] else "unknown date"
            self.backup_combo.addItem(f"{bk['tag']} - {when}", bk)

    def _refresh_blacklist(self):
        self.bl_list.clear()
        for tag in load_state().get("blacklist") or []:
            self.bl_list.addItem(tag)

    def _refresh_rollback_label(self):
        lkg = load_state().get("last_good_version")
        self.rollback_btn.setText(f"Rollback to {lkg}" if lkg else "Rollback")
        # Stay enabled with no last-known-good: the click explains
        # why there is nothing to roll back to (fix 15), instead of
        # the button silently doing nothing.
        self.rollback_btn.setEnabled(True)

    def _confirm(self, title, text):
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    # ------------------------------------------------------------- actions -
    def _run(self):
        self.run_btn.setEnabled(False)
        self.list.clear()
        self.detail.setPlainText("Running diagnosis…")
        self.worker = DiagnosisWorker()
        self.worker.done.connect(self._on_findings)
        self.worker.start()

    def _on_findings(self, findings):
        self.findings = findings
        self.run_btn.setEnabled(True)
        self.list.clear()
        icons = {"ok": "✓", "warn": "!", "fail": "✗"}
        for f in findings:
            self.list.addItem(QListWidgetItem(f"{icons.get(f['level'], '?')}  {f['title']}"))
        if self.list.count():
            self.list.setCurrentRow(0)
        fails = sum(1 for f in findings if f["level"] == "fail")
        recovery_log(f"diagnosis complete: {len(findings)} findings, {fails} failures")

    def _show_detail(self, row):
        if 0 <= row < len(self.findings):
            f = self.findings[row]
            self.detail.setPlainText(f["title"] + "\n\n" + (f["detail"] or ""))

    def _app_running_guard(self):
        procs = app_processes(install_root())
        if procs:
            QMessageBox.warning(self, "Video Grabber is running",
                                "Close Video Grabber before using this action - "
                                "swapping files under a running app corrupts installs.\n\n"
                                + "\n".join(f"pid {p}: {i}" for p, i in procs))
            return True
        return False

    def _restore(self):
        bk = self.backup_combo.currentData()
        if not bk:
            QMessageBox.information(self, "Restore", "No backup exists yet. "
                                    "Backups are made automatically before each update.")
            return
        if self._app_running_guard():
            return
        if not self._confirm("Restore Backup",
                             f"Restore the app from backup '{bk['tag']}'?\n"
                             "The current app folder is kept as app_broken-<time>."):
            return
        try:
            snapshot_app(install_root(), f"app_snapshot-{time.strftime('%Y%m%d-%H%M%S')}")
            restore_backup(bk)
            QMessageBox.information(self, "Restore", "Backup restored. Relaunch Video Grabber to test it.")
        except Exception as e:
            QMessageBox.critical(self, "Restore failed", str(e))
            recovery_log(f"restore failed: {e}")

    def _rollback(self):
        lkg = load_state().get("last_good_version")
        if not lkg:
            QMessageBox.information(
                self, "Rollback",
                "No last known good version is recorded yet, so there "
                "is nothing to roll back to.\n\nA version becomes the "
                "last known good one after it starts successfully "
                "once. If a backup exists, Restore Backup above can "
                "still help.")
            return
        if self._app_running_guard():
            return
        if not self._confirm("Rollback", f"Download and install {lkg} (the last version "
                                         "that started successfully)?"):
            return
        try:
            rollback_to(lkg)
            QMessageBox.information(self, "Rollback", f"{lkg} installed. Relaunch to test it.")
        except Exception as e:
            QMessageBox.critical(self, "Rollback failed",
                                 f"{e}\n\nIf you are offline, use Restore Backup "
                                 "instead, or Export Diagnostics and reinstall.")
            recovery_log(f"rollback failed: {e}")

    def _runtime(self):
        if self._app_running_guard():
            return
        tag = current_tag(install_root()) or load_state().get("last_good_version")
        if not tag:
            QMessageBox.information(self, "Runtime", "No version tag is known; reinstall instead.")
            return
        if not self._confirm("Re-download Runtime",
                             f"Download the runtime from release {tag} (~500 MB) and "
                             "replace runtime\\? The current one is kept as runtime_broken-<time>."):
            return
        try:
            redownload_runtime(tag)
            QMessageBox.information(self, "Runtime", "Runtime replaced. Relaunch to test it.")
        except Exception as e:
            QMessageBox.critical(self, "Runtime download failed",
                                 f"{e}\n\nIf you are offline: Export Diagnostics and reinstall.")
            recovery_log(f"runtime redownload failed: {e}")

    def _kill_ghost(self):
        pid, img = port_owner()
        if health_identity() and not pid:
            QMessageBox.information(self, "Kill Ghost",
                                    "The backend answers but its PID wasn't found; "
                                    "close Video Grabber normally instead.")
            return
        if not pid:
            QMessageBox.information(self, "Kill Ghost", "Nothing is listening on 5757 now.")
            return
        if not self._confirm("Kill Ghost", f"Kill pid {pid} ({img})?"):
            return
        if kill_pid(pid):
            recovery_log(f"killed ghost pid {pid} ({img})")
            QMessageBox.information(self, "Kill Ghost", "Ghost process killed. Port 5757 is free.")
        else:
            QMessageBox.warning(self, "Kill Ghost", "Could not kill that process.")

    def _bl_add_current(self):
        tag = current_tag(install_root())
        if not tag:
            return
        def _add(state):
            bl = state.get("blacklist") or []
            if tag not in bl:
                bl.append(tag)
            state["blacklist"] = bl
        mutate_state(_add)
        recovery_log(f"blacklisted {tag}")
        self._refresh_blacklist()

    def _bl_remove(self):
        tag = self.bl_list.currentText()
        if not tag:
            return
        def _rm(state):
            state["blacklist"] = [t for t in (state.get("blacklist") or []) if t != tag]
        mutate_state(_rm)
        recovery_log(f"removed {tag} from blacklist")
        self._refresh_blacklist()

    def _auto_fix(self):
        if not self.findings:
            self._run()
            QMessageBox.information(self, "Auto-Fix", "Diagnosis ran first - press Auto-Fix again.")
            return
        fixes = [f for f in self.findings if f.get("fix")]
        if not fixes:
            QMessageBox.information(self, "Auto-Fix", "Nothing auto-fixable was found.")
            return
        order = {"ghost": 0, "settings": 1, "restore": 2, "runtime": 3}
        fixes.sort(key=lambda f: order.get(f["fix"], 9))
        plan = "\n".join(f"• {f['title']} → {f['fix']}" for f in fixes)
        if not self._confirm("Auto-Fix", f"Auto-Fix will try, safest first:\n\n{plan}\n\n"
                                         "Every step is logged to recovery.log, and the "
                                         "current app is snapshotted before any restore."):
            return
        def _attempt(state):
            state.setdefault("fix_attempts", []).append(
                {"at": int(time.time()), "plan": [f["fix"] for f in fixes]})
        mutate_state(_attempt)
        done = []
        for f in fixes:
            kind = f["fix"]
            try:
                if kind == "ghost":
                    pid, img = port_owner()
                    if pid and kill_pid(pid):
                        done.append(f"killed ghost pid {pid}")
                elif kind == "settings":
                    sj = data_home() / "settings.json"
                    aside = data_home() / f"settings.old-{time.strftime('%Y%m%d')}.json"
                    os.replace(sj, aside)
                    done.append(f"settings moved aside to {aside.name}")
                elif kind == "restore":
                    if self._app_running_guard():
                        break
                    snapshot_app(install_root(),
                                 f"app_snapshot-{time.strftime('%Y%m%d-%H%M%S')}")
                    backs = list_backups()
                    if backs:
                        restore_backup(backs[0])
                        done.append(f"restored backup {backs[0]['tag']}")
                    else:
                        done.append("no backup exists - use Rollback")
                elif kind == "runtime":
                    done.append("runtime repair is manual (button below) - "
                                "it downloads ~500 MB")
                recovery_log(f"auto-fix step {kind}: ok")
            except Exception as e:
                recovery_log(f"auto-fix step {kind} FAILED: {e}")
                done.append(f"{kind} failed: {e}")
        QMessageBox.information(self, "Auto-Fix", "\n".join(done) or "Nothing to do.")
        self._run()

    def _export(self):
        default = str(data_home() / f"vg-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Export Diagnostics", default,
                                              "Zip files (*.zip)")
        if not path:
            return
        try:
            export_diagnostics(Path(path), self.findings)
            QMessageBox.information(self, "Export Diagnostics",
                                    f"Saved to {path}\nSecrets (pairing token, proxy "
                                    "password) were redacted.")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _relaunch(self):
        if _supervisor_mutex_held():
            # Without this check the spawned supervisor exits at once
            # on the mutex and the click looks like it did nothing.
            QMessageBox.information(
                self, "Already running",
                "A Video Grabber supervisor is already running (the "
                "app, or an update in progress). It owns the launch; "
                "you can simply close Recovery.")
            return
        exe = install_root() / "VideoGrabber.exe"
        try:
            subprocess.Popen([str(exe)], cwd=str(install_root()))
            self.close()
        except Exception as e:
            QMessageBox.critical(self, "Relaunch failed", str(e))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Video Grabber Recovery")
    _schema_err = vg_state.schema_gate_error()
    if _schema_err:
        # Same freeze rule as the supervisor and the updater: a
        # state file from a newer schema is not ours to interpret,
        # and Recovery's writes (blacklist, fix attempts) would be
        # guesses. Refuse, with the reason on screen.
        recovery_log(f"refusing to run: {_schema_err}")
        QMessageBox.critical(None, "Video Grabber Recovery", _schema_err)
        return 1
    win = RecoveryWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
