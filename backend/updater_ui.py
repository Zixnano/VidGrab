# updater_ui.py - the Animated Updater (contract §5).
#
# Runs under the SUPERVISOR, never under the app: the app writes
# update_pending to recovery_state.json and exits; the supervisor copies
# this file to %LOCALAPPDATA%\VideoGrabber\staging\ and runs it through
# the app host (VG_ENTRY_SCRIPT), watching its exit code. Exit 0 =
# success (or no update). Non-zero = the supervisor falls back to
# updater.bat with whatever this updater staged at staging\pkg.
#
# Rules baked in: backup before swap (AppData\backups\app_<tag>, newest
# 2 kept, settings.json included, host exe + internal included),
# blacklist-aware candidate selection, never downgrade, disk-space
# checks before download, rename-dance swaps with rollback of THIS
# run's changes only.
#
# Division of labour (fix 3): THIS updater swaps app\ only, copying
# out of the staged pkg so the pkg stays whole. It must never rename
# VideoGrabberApp.exe or VideoGrabberApp_internal: this very process
# is running inside them, and Windows will not rename a folder whose
# DLLs are loaded. Host files found in the pkg are left staged; the
# supervisor runs updater.bat in "host" mode once this process has
# exited, and the bat swaps them with the same rename discipline.
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, Qt, QThread,
                            QTimer, Signal)
from PySide6.QtGui import (QColor, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

# Brand, sampled from the app logo (the green-and-white download mark on
# its dark tile): pale mint shaft -> vivid green head, white tray.
# Exact values sampled from the shipped icon file (128px master):
# flat material green arrow, soft-white tray, blue-black tile.
BRAND = {
    "green": "#4caf50",
    "green_deep": "#3d8b40",
    "tray": "#e6edf3",
    "tile": "#0b1117",
    "tile_edge": "#1c2530",
}

REPO = "Zixnano/VidGrab"
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"

# ------------------------------------------------------------ state --------
# One shared implementation: vg_state.py (fix 10). It ships in app\
# next to this file, which is also how the staged copy finds it: the
# host puts app\ first on sys.path.
import vg_state
from vg_state import (StateLock, load_state, mutate_state, save_state,
                      set_update_result, sha256_file, state_dir)

STATE_DIR = state_dir()
STATE_FILE = vg_state.state_file()
STATE_LOCK = vg_state.lock_file()
BACKUPS_DIR = vg_state.backups_dir()
STAGING = vg_state.staging_dir()
DEFAULT_STATE = vg_state.DEFAULT_STATE
_pid_alive = vg_state.pid_alive


def log(msg: str) -> None:
    """Append to recovery.log under the 'updater' source tag."""
    vg_state.recovery_log(msg, source="updater")


# -------------------------------------------------------- version logic ----
def tag_num(tag) -> int:
    m = re.search(r"(\d+)", str(tag or ""))
    return int(m.group(1)) if m else -1


def app_asset(release: dict):
    want = f"VideoGrabber-app-{release.get('tag_name')}.zip"
    for a in release.get("assets") or []:
        if a.get("name") == want:
            return a
    return None


def pick_candidate(releases, blacklist, installed_tag):
    """Contract §7: newest release not on the blacklist; NEVER a
    downgrade. Returns (release_or_None, status) where status is
    'update' | 'blacklisted' (newer exist but all blacklisted) |
    'uptodate'."""
    installed = tag_num(installed_tag)
    usable = []
    newer_blocked = False
    downgrades = []
    for rel in releases or []:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        if app_asset(rel) is None:
            continue
        n = tag_num(rel.get("tag_name"))
        if 0 <= n < installed:
            downgrades.append(rel.get("tag_name"))
        if n <= installed:
            continue
        if rel.get("tag_name") in (blacklist or []):
            newer_blocked = True
            continue
        usable.append(rel)
    if usable:
        usable.sort(key=lambda r: tag_num(r.get("tag_name")), reverse=True)
        return usable[0], "update"
    if downgrades and not newer_blocked:
        # Explicit downgrade guard: every usable release on the list is OLDER
        # than what is installed, so nothing is offered. Say why.
        log(f"pick_candidate: refusing to downgrade from {installed_tag}; "
            f"every usable release is older ({', '.join(map(str, downgrades))})")
    return None, ("blacklisted" if newer_blocked else "uptodate")


# -------------------------------------------------------------- helpers ----
def install_root() -> Path:
    return vg_state.install_root()


def data_home() -> Path:
    st = load_state()
    recorded = (st.get("last_paths") or {}).get("data_home") or ""
    return Path(recorded) if recorded else Path.home() / "Downloads" / "VideoGrabber"


def tree_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def copy_tree_progress(src: Path, dst: Path, progress) -> None:
    total = tree_size(src) or 1
    done = 0
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif p.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            done += p.stat().st_size
            progress(int(done * 100 / total))


def make_backup(root: Path, installed_tag: str, progress) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS_DIR / f"app_{installed_tag or 'unknown'}"
    if dest.exists():
        shutil.rmtree(dest)
    copy_tree_progress(root / "app", dest / "app", progress)
    # Fix 5: a backup that cannot undo a bad HOST is half a backup.
    # The host exe and its internal folder ride along, so Recovery's
    # restore covers everything a thin update can change.
    includes_host = False
    exe = root / "VideoGrabberApp.exe"
    if exe.is_file():
        shutil.copy2(exe, dest / "VideoGrabberApp.exe")
        includes_host = True
    internal = root / "VideoGrabberApp_internal"
    if internal.is_dir():
        shutil.copytree(internal, dest / "VideoGrabberApp_internal")
        includes_host = True
    sj = data_home() / "settings.json"
    if sj.is_file():
        shutil.copy2(sj, dest / "settings.json")
    (dest / "backup_meta.json").write_text(json.dumps(
        {"tag": installed_tag, "created_at": int(time.time()),
         "includes_host": includes_host}),
        encoding="utf-8")
    # verify: the copy must contain a runnable app
    if not (dest / "app" / "server.py").is_file():
        raise RuntimeError("backup verification failed: no app\\server.py in the copy")
    # keep the newest 2 release backups (snapshots untouched)
    backups = []
    for d in BACKUPS_DIR.iterdir():
        if d.is_dir() and d.name.startswith("app_"):
            try:
                meta = json.loads((d / "backup_meta.json").read_text(encoding="utf-8"))
                backups.append((meta.get("created_at", 0), d))
            except Exception:
                backups.append((0, d))
    backups.sort(reverse=True)
    for _, old in backups[2:]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def fetch_releases():
    req = urllib.request.Request(API_RELEASES, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "VideoGrabber-Updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        # GitHub answers errors (rate limit!) with a dict. Swallowing
        # that into [] would report "You're up to date" for a check
        # that never happened - fail loudly instead.
        raise RuntimeError(f"GitHub API error: {(data or {}).get('message', data)}")
    return data


def verify_pkg(pkg: Path) -> None:
    app = pkg / "app"
    if not (app / "server.py").is_file():
        raise RuntimeError("staged package has no app\\server.py")
    mf = app / "manifest.json"
    if mf.is_file():
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        files = manifest.get("files") or {}
        for rel, info in files.items():
            p = app / rel
            if not p.is_file() or p.stat().st_size != info.get("size"):
                raise RuntimeError(f"staged package fails manifest check: {rel}")
            want_sha = info.get("sha256")
            if want_sha and sha256_file(p) != want_sha:
                raise RuntimeError(
                    f"staged package fails manifest SHA-256 check: {rel}")


def sums_asset(release: dict, tag: str):
    want = f"SHA256SUMS-{tag}.txt"
    for a in release.get("assets") or []:
        if a.get("name") == want:
            return a
    return None


def verify_download(release: dict, tag: str, asset_name: str,
                      zpath: Path, size: int, got: int) -> None:
    """Fix 11: the byte count must match the asset size exactly, and
    the release must carry a SHA256SUMS asset (CI publishes one per
    release) and the zip's hash must match it. A release without one is
    refused: the size check plus the in-package manifest alone are not
    enough to install an update."""
    if size and got != size:
        raise RuntimeError(
            f"download truncated or overlong: got {got} bytes, "
            f"the asset is {size}")
    sums = sums_asset(release, tag)
    if sums is None:
        log(f"verify_download: {tag} has no SHA256SUMS-{tag}.txt asset; "
            "refusing to install an unverifiable download")
        raise RuntimeError("release has no SHA256SUMS")
    req = urllib.request.Request(sums["browser_download_url"],
                                 headers={"User-Agent": "VideoGrabber-Updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    expected = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            expected = parts[0].strip().lower()
            break
    if expected is None:
        raise RuntimeError(f"SHA256SUMS-{tag}.txt does not list {asset_name}")
    actual = sha256_file(zpath)
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 mismatch for {asset_name}: the download is "
            "corrupt or tampered with; refusing to install it")


def swap_from_pkg(root: Path, pkg: Path, new_tag: str) -> bool:
    """Swap the app folder ONLY, by copy, leaving the pkg whole (fix 3).

    The current app is renamed to app.old first (rename always works
    where delete fails); the new app is copied in; on any failure the
    new copy is removed and app.old renamed back. Host files in the
    pkg are NOT touched: this process is running inside them. They
    stay staged for the supervisor's bat pass.

    Returns True when host files are staged and still need swapping.
    The install-version stamp is written here ONLY when no host swap
    is pending; otherwise the bat stamps after its swap, so a failed
    host swap leaves the old stamp and the update is offered again.
    """
    host_pending = (pkg / "VideoGrabberApp.exe").is_file() or \
                   (pkg / "VideoGrabberApp_internal").is_dir()
    app_dst = root / "app"
    app_old = root / "app.old"
    if app_old.exists():
        shutil.rmtree(app_old) if app_old.is_dir() else app_old.unlink()
    renamed = False
    try:
        if app_dst.exists():
            os.replace(app_dst, app_old)
            renamed = True
        shutil.copytree(pkg / "app", app_dst)
    except Exception:
        if app_dst.exists():
            shutil.rmtree(app_dst, ignore_errors=True)
        if renamed and app_old.exists():
            os.replace(app_old, app_dst)
        raise
    # Post-audit fix 5, option (a): the fresh bats ride in the thin
    # package and the UPDATER installs them here. The alternative
    # (the bat copying itself out of app\) cannot work: the bat is
    # the fallback and may be the very file that needs replacing,
    # and a running batch file cannot safely overwrite itself.
    for bat in ("updater.bat", "restore.bat"):
        src = pkg / bat
        if src.is_file():
            try:
                shutil.copy2(src, root / bat)
            except OSError:
                pass     # a stuck old bat must not fail the update
    if not host_pending:
        (root / "install-version.txt").write_text(new_tag, encoding="utf-8")
    return host_pending


# --------------------------------------------------------------- worker ----
class UpdateWorker(QThread):
    progress = Signal(int)
    status = Signal(str)
    phase = Signal(str)            # check|backup|download|extract|swap|done
    stats = Signal(str)            # download speed / ETA line
    finished_ok = Signal(str)      # message
    failed = Signal(str)

    def run(self):
        try:
            self._run()
        except Exception as e:
            try:
                set_update_result(getattr(self, "_target_tag", None),
                                  False, str(e))
            except Exception as exc:
                log(f"could not record the failed update result: {exc}")
            self.failed.emit(str(e))

    def _run(self):
        # Post-audit fix 4: never touch a state file from a newer
        # schema than this copy of vg_state understands.
        _schema_err = vg_state.schema_gate_error()
        if _schema_err:
            raise RuntimeError(_schema_err)
        root = install_root()
        state = load_state()
        installed = None
        try:
            installed = (root / "install-version.txt").read_text(
                encoding="utf-8").strip() or None
        except OSError:
            installed = state.get("current_version")
        self.phase.emit("check")
        # A staged pkg from an earlier update must never survive into
        # this run: whatever is in staging\\pkg afterwards was put there
        # by THIS run (the supervisor's host swap trusts that).
        shutil.rmtree(STAGING / "pkg", ignore_errors=True)
        mutate_state(lambda s: s.pop("host_swap", None))
        self.status.emit(f"Installed: {installed or 'unknown'}. Checking GitHub…")
        releases = fetch_releases()
        mutate_state(lambda s: s.update(
            {"last_release_list": [{"tag": r.get("tag_name")} for r in releases]}))
        cand, why = pick_candidate(releases, state.get("blacklist") or [], installed)
        if cand is None:
            msg = ("No update available: newer releases are blacklisted."
                   if why == "blacklisted" else "You're up to date.")
            set_update_result(installed, True, msg)
            self.finished_ok.emit(msg)
            return
        tag = cand["tag_name"]
        self._target_tag = tag
        asset = app_asset(cand)
        mutate_state(lambda s: s.update({"update_target": {"tag": tag}}))
        size = int(asset.get("size") or 0)
        # Fix 16: staging and backups live on the STATE drive (usually
        # C: under LOCALAPPDATA), the swap lands on the INSTALL drive.
        # Measure both, for what each actually has to hold.
        app_bytes = tree_size(root / "app") if (root / "app").is_dir() else 0
        need_state = size * 3 + app_bytes + 100 * 1024 * 1024
        free_state = shutil.disk_usage(str(STATE_DIR)).free
        if free_state < need_state:
            raise RuntimeError(
                f"Not enough disk space on the drive holding "
                f"{STATE_DIR}: need ~{need_state // 1048576} MB free "
                f"(download + extraction + backup), have "
                f"{free_state // 1048576} MB.")
        need_root = app_bytes + 50 * 1024 * 1024
        free_root = shutil.disk_usage(str(root)).free
        if free_root < need_root:
            raise RuntimeError(
                f"Not enough disk space on the install drive: need "
                f"~{need_root // 1048576} MB free for the swap, have "
                f"{free_root // 1048576} MB.")
        self.phase.emit("backup")
        self.status.emit(f"Backing up current app ({installed})…")
        make_backup(root, installed, lambda p: self.progress.emit(int(p * 0.25)))
        pkg_dir = STAGING / "pkg"
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
        pkg_dir.mkdir(parents=True)
        zpath = STAGING / asset["name"]
        self.phase.emit("download")
        self.status.emit(f"Downloading {tag} ({size // 1048576} MB)…")
        req = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "VideoGrabber-Updater"})
        got = 0
        started = time.time()
        last_stat = 0.0
        part = zpath.with_name(zpath.name + ".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if size:
                    self.progress.emit(25 + int(got * 45 / size))
                now = time.time()
                if now - last_stat >= 0.25:
                    last_stat = now
                    elapsed = max(now - started, 0.01)
                    speed = got / elapsed
                    eta = int((size - got) / speed) if size and speed > 0 else 0
                    self.stats.emit(
                        f"{got / 1048576:.1f} of {size / 1048576:.1f} MB · "
                        f"{speed / 1048576:.1f} MB/s · ~{eta}s left")
        os.replace(part, zpath)      # only a complete download lands
        verify_download(cand, tag, asset["name"], zpath, size, got)
        self.phase.emit("extract")
        self.stats.emit("")
        self.status.emit("Extracting…")
        with zipfile.ZipFile(zpath) as z:
            try:
                z.extractall(pkg_dir, filter="data")   # refuse zip-slip
            except TypeError:
                z.extractall(pkg_dir)
        # the zip wraps a top folder ("video 3 grabber"); normalize
        inner = [d for d in pkg_dir.iterdir() if (d / "app").is_dir()]
        if len(inner) == 1 and inner[0] != pkg_dir:
            for item in list(inner[0].iterdir()):
                shutil.move(str(item), str(pkg_dir / item.name))
            inner[0].rmdir()
        verify_pkg(pkg_dir)
        self.phase.emit("swap")
        self.status.emit("Swapping files…")
        self.progress.emit(85)
        host_pending = swap_from_pkg(root, pkg_dir, tag)
        if host_pending:
            # The supervisor swaps the staged host files via
            # updater.bat now that this process is about to exit, and
            # stamps the version when that lands. Until then the
            # state keeps the old current_version on purpose.
            mutate_state(lambda s: s.update({"host_swap": {"tag": tag}}))
            self.status.emit("App updated; host swap finishes as we close…")
        else:
            mutate_state(lambda s: s.update({"current_version": tag}))
        set_update_result(tag, True, f"Updated to {tag}.")
        self.phase.emit("done")
        self.progress.emit(100)
        self.finished_ok.emit(f"Updated to {tag}. Relaunching…")


# ------------------------------------------------------------------ UI -----
QSS = """
QWidget { background: #10151d; color: #dbe2ec; font-size: 13px; }
QLabel#title { font-size: 17px; font-weight: bold; color: #fff; }
QLabel#quip { color: #5f7189; font-size: 12px; }
QLabel#stats { color: #8fa1b8; font-size: 12px; }
QLabel.step { color: #69758a; font-size: 12px; }
QLabel.stepActive { color: #ffffff; font-size: 12px; }
QLabel.stepDone { color: #4caf50; font-size: 12px; }
QProgressBar { background: #0d1015; border: 1px solid #39445a; border-radius: 6px;
               height: 16px; text-align: center; color: #dbe2ec; }
QProgressBar::chunk { background: #4caf50; border-radius: 5px; }
QPushButton { background: #232b38; border: 1px solid #39445a; border-radius: 6px;
              padding: 6px 14px; }
"""

QUIPS = [
    "Warming up the engines…",
    "Asking GitHub nicely…",
    "Building a backup cocoon…",
    "Greasing the gears…",
    "Counting every last byte…",
    "Almost there, probably…",
]

PHASES = [("check", "Check for updates"), ("backup", "Back up current version"),
          ("download", "Download"), ("extract", "Extract"),
          ("swap", "Install")]


class LogoMark(QWidget):
    """The app logo, redrawn as vectors and alive: the green arrow
    bobs down into the white tray; while a download runs the tray
    fills green in step with it; on success the arrow lands with a
    soft glow and stays home."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(104, 104)
        self._mode = "working"        # working | downloading | done | failed
        self._fill = 0.0              # tray fill 0..1 (download progress)
        self._t = 0.0
        self._landed = 0.0            # glow strength after landing
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def set_mode(self, mode):
        self._mode = mode
        if mode == "done":
            self._landed = 1.0
        self.update()

    def set_fill(self, frac):
        self._fill = max(0.0, min(1.0, frac))
        self.update()

    def _tick(self):
        self._t += 0.033
        if self._mode == "done" and self._landed > 0:
            self._landed = max(0.0, self._landed - 0.02)
        self.update()

    def _bob(self):
        import math
        if self._mode == "done":
            return 6.0
        if self._mode == "failed":
            return 0.0
        speed = 2.6 if self._mode == "downloading" else 1.8
        return (math.sin(self._t * speed) * 0.5 + 0.5) * 6.0

    def paintEvent(self, event):
        from PySide6.QtCore import QPointF, QRectF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale = min(self.width(), self.height()) / 100.0
        p.scale(scale, scale)
        green = QColor(BRAND["green"])
        tray_col = QColor(BRAND["tray"])
        if self._mode == "failed":
            green = QColor("#c2554f")
            tray_col = QColor("#9aa3ad")

        # tile
        p.setPen(QPen(QColor(BRAND["tile_edge"]), 1.4))
        p.setBrush(QColor(BRAND["tile"]))
        p.drawRoundedRect(QRectF(5, 5, 90, 90), 20, 20)

        bob = self._bob()
        # landing glow (pulses with the bob; flashes on done)
        glow_a = 0
        if self._mode in ("working", "downloading"):
            glow_a = int(40 + 60 * (bob / 6.0))
        elif self._mode == "done":
            glow_a = int(120 * self._landed) + 30
        if glow_a > 0:
            g = QColor(green)
            g.setAlpha(min(glow_a, 140))
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(50, 79), 21, 5.5)

        # tray fill (download progress pooling in the tray)
        if self._fill > 0:
            fh = 12.0 * self._fill
            g = QColor(green)
            g.setAlpha(190)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(34.5, 74 - fh, 31, fh), 3, 3)

        # tray (white U, proportions from the icon master)
        path = QPainterPath()
        path.moveTo(29.5, 55)
        path.lineTo(29.5, 69)
        path.quadTo(29.5, 77, 37.5, 77)
        path.lineTo(62.5, 77)
        path.quadTo(70.5, 77, 70.5, 69)
        path.lineTo(70.5, 55)
        p.setPen(QPen(tray_col, 5.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        # arrow (flat green, geometry from the icon master; the same-
        # colour round-joined stroke softens the corners like the icon)
        arrow = QPainterPath()
        arrow.moveTo(45, 17)
        arrow.lineTo(55, 17)
        arrow.lineTo(55, 37)
        arrow.lineTo(64, 37)
        arrow.lineTo(50, 64)
        arrow.lineTo(36, 37)
        arrow.lineTo(45, 37)
        arrow.closeSubpath()
        p.save()
        p.translate(0, bob)
        p.setPen(QPen(green, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(green)
        p.drawPath(arrow)
        p.restore()
        p.end()


class UpdaterWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Grabber - Updating")
        self.resize(470, 404)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignTop)

        self.logo = LogoMark()
        lay.addWidget(self.logo, alignment=Qt.AlignHCenter)
        t = QLabel("Updating Video Grabber")
        t.setObjectName("title")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        self.quip = QLabel("")
        self.quip.setObjectName("quip")
        self.quip.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.quip)
        self.status = QLabel("Preparing…")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.status)

        self.steps = {}
        for key, label in PHASES:
            row = QLabel(f"○  {label}")
            row.setProperty("class", "step")
            row.setStyleSheet("color: #69758a; font-size: 12px;")
            lay.addWidget(row)
            self.steps[key] = row

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self._bar_anim = QPropertyAnimation(self.bar, b"value", self)
        self._bar_anim.setDuration(240)
        self._bar_anim.setEasingCurve(QEasingCurve.OutCubic)

        self.stats = QLabel("")
        self.stats.setObjectName("stats")
        self.stats.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.stats)

        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.close)
        lay.addWidget(self.close_btn, alignment=Qt.AlignRight)

        self._quip_i = 0
        self._quip_timer = QTimer(self)
        self._quip_timer.timeout.connect(self._next_quip)
        self._quip_timer.start(2800)

        self.exit_code = 0
        self.worker = UpdateWorker()
        self.worker.progress.connect(self._on_progress)
        self.worker.status.connect(self.status.setText)
        self.worker.phase.connect(self._on_phase)
        self.worker.stats.connect(self.stats.setText)
        self.worker.finished_ok.connect(self._ok)
        self.worker.failed.connect(self._fail)
        self.worker.start()

    def closeEvent(self, event):
        # Never let a stray close kill the worker mid-swap and report
        # exit 0 (success) to the supervisor for a half-written app:
        # while an update is running, the window simply refuses to
        # close. (This guard existed in the pre-animation updater and
        # was lost in the rewrite; the Close button below is the way
        # out once the worker has finished.)
        if self.worker.isRunning():
            self.status.setText("Updating... please wait for it to finish.")
            event.ignore()
            return
        event.accept()

    # ------------------------------------------------------------- slots ---
    _busy = False

    def _next_quip(self):
        if self._busy:
            self._quip_i = (self._quip_i + 1) % len(QUIPS)
            self.quip.setText(QUIPS[self._quip_i])

    def _on_phase(self, phase):
        order = [k for k, _ in PHASES]
        idx = order.index(phase) if phase in order else len(order)
        for i, (key, label) in enumerate(PHASES):
            row = self.steps[key]
            if phase == "done" or i < idx:
                row.setText(f"✓  {label}")
                row.setStyleSheet("color: #4caf50; font-size: 12px;")
            elif i == idx:
                row.setText(f"◉  {label}")
                row.setStyleSheet("color: #ffffff; font-size: 12px;")
            else:
                row.setText(f"○  {label}")
                row.setStyleSheet("color: #69758a; font-size: 12px;")
        self._busy = phase in ("check", "extract", "swap")
        if self._busy:
            self._bar_anim.stop()
            self.bar.setRange(0, 0)            # indeterminate pulse
            self.quip.setText(QUIPS[self._quip_i])
        else:
            if self.bar.maximum() == 0:
                self.bar.setRange(0, 100)
            self.quip.setText("")
        if phase == "download":
            self.logo.set_mode("downloading")
        elif phase == "done":
            self.logo.set_mode("done")

    def _on_progress(self, value):
        if value <= 25:
            frac = value / 25.0 * 0.15
        elif value <= 70:
            frac = 0.15 + (value - 25) / 45.0 * 0.85
        else:
            frac = 1.0
        self.logo.set_fill(frac)
        if self.bar.maximum() == 0:
            return
        self._bar_anim.stop()
        self._bar_anim.setStartValue(self.bar.value())
        self._bar_anim.setEndValue(value)
        self._bar_anim.start()

    def _ok(self, msg):
        self.stats.setText("")
        self.status.setText(msg)
        self._on_phase("done")
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.logo.set_mode("done")
        self.exit_code = 0
        self.close_btn.setEnabled(True)
        QTimer.singleShot(1400, self.close)

    def _fail(self, msg):
        self.status.setText(f"Update failed: {msg}\nThe supervisor will try the "
                            "fallback updater, or relaunch the current version.")
        self.logo.set_mode("failed")
        self.quip.setText("")
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
        self.exit_code = 2
        self.close_btn.setEnabled(True)


def main() -> int:
    app = QApplication(sys.argv)
    win = UpdaterWindow()
    win.show()
    app.exec()
    return win.exit_code


if __name__ == "__main__":
    sys.exit(main())
