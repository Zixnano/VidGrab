"""updater.py — thin-update check, download, stage, handoff (Session 9).

No UI yet: check_for_update() / apply_update() are plain functions a
future menu item can call. Compares the latest release tag against the
version actually installed on disk (install-version.txt, stamped by
updater.bat after each successful swap) numerically — v42 vs 4.0.9 —
falling back to APP_VERSION on first install. Downloads
VideoGrabber-app-v*.zip, stages it under %LOCALAPPDATA%\\VideoGrabber\\
update, then spawns updater.bat and returns. updater.bat polls until
VideoGrabber.exe exits, swaps app\\, the launcher exe, and its internal
folder via rename-then-replace, stamps install-version.txt, relaunches,
and exits. The *.old leftovers are deleted by the launcher on its next
successful startup — never here, never mid-swap.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from logging_setup import log
from settings import APP_VERSION


REPO = "Zixnano/VidGrab"

API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_RE = re.compile(r"^VideoGrabber-app-v(\d+)\.zip$")
STAGING_PARENT = "VideoGrabber"   # under %LOCALAPPDATA%
VERSION_STAMP = "install-version.txt"


def compare_versions(tag: str, current: str) -> int:
    """Return >0 if tag is newer than current, 0 if equal, <0 if
    older. Handles 'v42' vs '4.0.9' by extracting digit groups and
    comparing numerically. Unparseable -> 0."""
    def _nums(s: str):
        return [int(x) for x in re.findall(r"\d+", s or "")]

    a, b = _nums(tag), _nums(current)
    if not a or not b:
        return 0
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    for x, y in zip(a, b):
        if x != y:
            return x - y
    return 0


def _install_root() -> Path:
    """Folder holding VideoGrabber.exe / app/ / runtime\\."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Source run: updater.py sits in backend/, one below the repo root.
    return Path(__file__).resolve().parents[1]


def _installed_tag(root: Path) -> str:
    """Version actually on disk. updater.bat stamps install-version.txt
    after each successful swap; without it (first install from the setup
    zip) fall back to the baked-in APP_VERSION."""
    try:
        tag = (root / VERSION_STAMP).read_text(encoding="utf-8").strip()
        if tag:
            return tag
    except OSError:
        pass
    return APP_VERSION


def _staging_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / STAGING_PARENT / "update"


def _http_get_json(url: str):
    req = Request(url, headers={
        "User-Agent": "VideoGrabber-Updater",
        "Accept": "application/vnd.github+json",
    })
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def check_for_update(current: str = None):
    """Return (tag, download_url) for the newest app zip, or (None, None).
    `current` is the installed version; defaults to APP_VERSION."""
    current = current or APP_VERSION
    try:
        rel = _http_get_json(API_URL)
    except Exception as e:
        log(f"updater: release check failed: {e}")
        return None, None
    tag = rel.get("tag_name") or ""
    # Numeric compare — never string-compare: "v9" > "v42" lexically, which
    # is exactly the bug this guard exists to prevent.
    if compare_versions(tag, current) <= 0:
        log(f"updater: {tag or 'unversioned'} not newer than {current} — up to date")
        return None, None
    for asset in rel.get("assets") or []:
        name = asset.get("name") or ""
        if ASSET_RE.match(name) and asset.get("browser_download_url"):
            return tag, asset["browser_download_url"]
    log(f"updater: release {tag} has no app zip asset")
    return None, None


def download_and_stage(url: str) -> Path:
    """Download the thin zip, verify and extract it; return the pkg dir
    (the single top-level folder inside the zip, e.g. 'video 3 grabber')."""
    stage = _staging_root()
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    zip_path = stage / "update.zip"
    log(f"updater: downloading {url}")
    req = Request(url, headers={"User-Agent": "VideoGrabber-Updater"})
    with urlopen(req, timeout=60) as r, open(zip_path, "wb") as f:
        shutil.copyfileobj(r, f)
    stage_real = stage.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for m in zf.infolist():
            target = (stage / m.filename).resolve()
            # Path.is_relative_to beats str.startswith: no string-prefix
            # false positives (e.g. "stage2" vs "stage").
            if not target.is_relative_to(stage_real):
                raise RuntimeError(f"updater: unsafe path in zip: {m.filename}")
        zf.extractall(stage)
    zip_path.unlink(missing_ok=True)
    for child in stage.iterdir():
        if child.is_dir() and (child / "app").is_dir():
            return child
    return stage


def apply_update() -> bool:
    """Check, download, stage, hand off to updater.bat. Returns True if an
    update was staged; the bat finishes the job after this process exits."""
    if not getattr(sys, "frozen", False):
        log("updater: source run — updates only apply to the installed exe")
        return False
    root = _install_root()
    current = _installed_tag(root)
    tag, url = check_for_update(current)
    if not url:
        return False
    pkg = download_and_stage(url)
    bat = root / "updater.bat"
    if not bat.is_file():
        log(f"updater: {bat} missing — can't apply update")
        return False
    log(f"updater: staged {tag} (current {current}); handing off to {bat}")
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: DETACHED makes every
        # tasklist/timeout inside the bat pop its own console window and
        # the wait loop spins hot.
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # Third arg is the tag: updater.bat stamps install-version.txt with it
    # after the swap, so the next check compares against the NEW version
    # instead of the APP_VERSION baked into the just-replaced source.
    subprocess.Popen(
        [str(bat), str(root), str(pkg), tag],
        cwd=str(root),
        close_fds=True,
        creationflags=creationflags,
    )
    return True


if __name__ == "__main__":
    apply_update()
