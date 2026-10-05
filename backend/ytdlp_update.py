"""ytdlp_update.py: update yt-dlp without shipping a new app release (v5).

How it works
  * update_ytdlp() downloads the newest yt-dlp wheel from PyPI, verifies its
    sha256, and stages the `yt_dlp` package in <data>/ytdlp-pending/.
    Nothing the running process has imported is touched.
  * activate() runs at startup, BEFORE yt_dlp is imported. It promotes a
    ready pending update to <data>/ytdlp-override/ and, when that override
    is newer than the bundled copy, puts it first on sys.path so it shadows
    the bundled package. Plugins (bgutil etc.) and yt-dlp's optional extras
    keep coming from the bundled site-packages.
  * rollback() deletes the override so the bundled version is used again.

Updates take effect on the next app launch. Source runs without a
LOCALAPPDATA directory use ~/.videograbber instead.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from logging_setup import log

PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
_VER_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")

# Last result of check/update, for /update-status.
STATUS = {"checked_ts": 0, "latest": "", "staged": "", "error": ""}


def _data_dir():
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "VideoGrabber" if base else Path.home() / ".videograbber"
    return root / "ytdlp"


def _override_dir():
    return _data_dir() / "ytdlp-override"


def _pending_dir():
    return _data_dir() / "ytdlp-pending"


def version_key(v):
    """'2026.08.19' / '2026.8.19.123' -> comparable tuple of ints."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def _read_pkg_version(root):
    try:
        text = (Path(root) / "yt_dlp" / "version.py").read_text(encoding="utf-8")
        m = _VER_RE.search(text)
        return m.group(1) if m else ""
    except OSError:
        return ""


def bundled_version():
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("yt-dlp")
        except PackageNotFoundError:
            return ""
    except Exception:
        return ""


def override_version():
    return _read_pkg_version(_override_dir())


def active_version():
    """Version yt-dlp will actually run (or is running)."""
    if "yt_dlp" in sys.modules:
        try:
            from yt_dlp.version import __version__
            return __version__
        except Exception:
            pass
    ov, bv = override_version(), bundled_version()
    if ov and version_key(ov) > version_key(bv):
        return ov
    return bv or ov


def activate():
    """Call once at startup before anything imports yt_dlp."""
    try:
        _promote_pending()
        ov = override_version()
        if not ov:
            return
        bv = bundled_version()
        if bv and version_key(ov) <= version_key(bv):
            log(f"yt-dlp override {ov} is not newer than bundled {bv}; ignoring")
            return
        if "yt_dlp" in sys.modules:
            log(f"yt-dlp override {ov} found but yt_dlp is already imported; "
                f"it will apply after the next restart")
            return
        sys.path.insert(0, str(_override_dir()))
        log(f"yt-dlp override active: {ov} (bundled: {bv or 'unknown'})")
    except Exception as e:
        log(f"yt-dlp override activation failed (using bundled): {e}")


def _promote_pending():
    pend, over = _pending_dir(), _override_dir()
    if not (pend / "READY").is_file() or not (pend / "yt_dlp").is_dir():
        return
    old = over.with_name(over.name + ".old")
    try:
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        if over.exists():
            over.rename(old)
        pend.rename(over)
        (over / "READY").unlink(missing_ok=True)
        log(f"yt-dlp update applied: {override_version()}")
    except OSError as e:
        log(f"yt-dlp update could not be applied: {e}")
        # Put the previous override back if the swap half-completed.
        try:
            if old.exists() and not over.exists():
                old.rename(over)
        except OSError:
            pass


def rollback():
    """Remove the override (and any staged update); bundled yt-dlp is used
    again after restart. Returns True if something was removed."""
    removed = False
    for d in (_override_dir(), _pending_dir(),
              _override_dir().with_name("ytdlp-override.old")):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed = True
    if removed:
        log("yt-dlp override removed; bundled version applies after restart")
    return removed


def _get(url, timeout=30):
    req = Request(url, headers={"User-Agent": "VideoGrabber-ytdlp-update"})
    return urlopen(req, timeout=timeout)


def check_latest():
    """Return (latest_version, wheel_url, sha256) from PyPI."""
    with _get(PYPI_JSON, 20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    latest = (data.get("info") or {}).get("version") or ""
    for f in data.get("urls") or []:
        name = f.get("filename") or ""
        if f.get("packagetype") == "bdist_wheel" and name.endswith("py3-none-any.whl"):
            return latest, f.get("url"), (f.get("digests") or {}).get("sha256", "")
    raise RuntimeError("no universal wheel found on PyPI")


def update_ytdlp(force=False):
    """Stage the newest yt-dlp if it is newer than the active version.
    Returns a status dict; never raises."""
    STATUS["checked_ts"] = time.time()
    STATUS["error"] = ""
    try:
        latest, url, sha = check_latest()
        STATUS["latest"] = latest
        current = active_version()
        if not force and version_key(latest) <= version_key(current):
            log(f"yt-dlp {current} is up to date (latest {latest})")
            return {"updated": False, "current": current, "latest": latest}
        if not url or not sha:
            raise RuntimeError("PyPI response missing wheel url or checksum")
        pend = _pending_dir()
        if pend.exists():
            shutil.rmtree(pend, ignore_errors=True)
        pend.mkdir(parents=True, exist_ok=True)
        whl = pend / "yt_dlp.whl"
        log(f"yt-dlp: downloading {latest}")
        h = hashlib.sha256()
        with _get(url, 120) as r, open(whl, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest().lower() != sha.lower():
            raise RuntimeError("checksum mismatch; download discarded")
        pend_real = pend.resolve()
        with zipfile.ZipFile(whl) as zf:
            members = [m for m in zf.infolist()
                       if m.filename.startswith("yt_dlp/") and not m.is_dir()]
            if not members:
                raise RuntimeError("wheel has no yt_dlp package")
            for m in members:
                target = (pend / m.filename).resolve()
                if not target.is_relative_to(pend_real):
                    raise RuntimeError(f"unsafe path in wheel: {m.filename}")
            for m in members:
                zf.extract(m, pend)
        whl.unlink(missing_ok=True)
        got = _read_pkg_version(pend)
        if got != latest:
            raise RuntimeError(f"staged version {got!r} does not match {latest!r}")
        (pend / "READY").write_text(latest, encoding="utf-8")
        STATUS["staged"] = latest
        log(f"yt-dlp {latest} staged; applies on next app launch")
        return {"updated": True, "current": current, "latest": latest}
    except Exception as e:
        STATUS["error"] = str(e)
        log(f"yt-dlp update failed: {e}")
        shutil.rmtree(_pending_dir(), ignore_errors=True)
        return {"updated": False, "error": str(e)}


def status():
    return {
        "bundled": bundled_version(),
        "override": override_version(),
        "active": active_version(),
        "staged": STATUS["staged"] or (
            (_pending_dir() / "READY").read_text(encoding="utf-8").strip()
            if (_pending_dir() / "READY").is_file() else ""),
        "latest": STATUS["latest"],
        "checked_ts": STATUS["checked_ts"],
        "error": STATUS["error"],
    }
