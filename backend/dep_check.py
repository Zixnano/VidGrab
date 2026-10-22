"""dep_check.py: install missing requirements into the Python that is running
Video Grabber, so "pip install ..." is never something you do by hand.

    import dep_check
    dep_check.ensure_requirements()     # as early as possible, before 3rd-party imports

What it does, each launch (a few milliseconds when nothing is missing):
  1. reads requirements.txt next to the app (plus a built-in list of the runtime
     packages, so a package missing from the file is still caught);
  2. checks each one against what THIS interpreter has installed;
  3. installs whatever is missing with `sys.executable -m pip`, quietly.

It never raises and never blocks startup for long: no network, no pip, or a
frozen (PyInstaller) build just means it logs and carries on. A failed attempt
is not retried for an hour, so being offline doesn't slow every launch.
Dev-only packages (pyinstaller, pytest) are skipped. yt-dlp is only installed
when absent; its own updater manages its version.
"""
import importlib
import importlib.metadata as md
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from logging_setup import log
except Exception:                       # standalone use
    def log(msg):
        print(msg)

# Always required, even if requirements.txt is missing or out of date.
BUILTIN = ["flask>=3.0", "flask-cors>=4.0", "yt-dlp", "requests>=2.31", "PySide6>=6.7",
           "Pillow>=10", "numpy>=1.26", "streamlink>=6.0", "watchdog>=4.0"]
DEV_ONLY = {"pyinstaller", "pytest"}
VERSION_UNMANAGED = {"yt-dlp"}          # presence only; the app updates it itself
RETRY_AFTER_S = 3600
STAMP = Path(tempfile.gettempdir()) / "vg_dep_check.json"
_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*([<>=!~][^;#]*)?")


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse(lines):
    """{normalized name: pip spec} from requirements-style lines."""
    out = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name = _norm(m.group(1))
        out[name] = line.split(";", 1)[0].strip()
    return out


def _wanted(req_file=None):
    specs = _parse(BUILTIN)
    here = Path(__file__).resolve().parent
    for cand in ([Path(req_file)] if req_file else [here / "requirements.txt", here.parent / "requirements.txt"]):
        try:
            if cand.is_file():
                specs.update(_parse(cand.read_text(encoding="utf-8").splitlines()))   # the file wins
                break
        except OSError:
            pass
    return {n: s for n, s in specs.items() if n not in DEV_ONLY}


def _satisfied(name, spec):
    try:
        have = md.version(name)
    except md.PackageNotFoundError:
        return False
    if name in VERSION_UNMANAGED:
        return True
    try:                                 # version check only when `packaging` is around
        from packaging.requirements import Requirement
        req = Requirement(spec)
        return not req.specifier or req.specifier.contains(have, prereleases=True)
    except Exception:
        return True


def missing(req_file=None):
    """pip specs that this interpreter still needs."""
    return [spec for name, spec in sorted(_wanted(req_file).items()) if not _satisfied(name, spec)]


def _recently_failed(key):
    try:
        d = json.loads(STAMP.read_text(encoding="utf-8"))
        return d.get("key") == key and time.time() - d.get("ts", 0) < RETRY_AFTER_S
    except Exception:
        return False


def _stamp(key):
    try:
        STAMP.write_text(json.dumps({"key": key, "ts": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _pip(args, timeout):
    return subprocess.run([sys.executable, "-m", "pip", *args], capture_output=True, text=True,
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _install(specs):
    base = ["install", "--disable-pip-version-check", "--timeout", "15", "--retries", "1"]
    r = _pip(base + list(specs), 900)
    if r.returncode != 0 and "externally-managed-environment" in (r.stdout + r.stderr):
        # A distro-managed Python (PEP 668) refuses plain installs; the app owns this one.
        r = _pip(base + ["--break-system-packages"] + list(specs), 900)
    return r


def ensure_requirements(req_file=None, force=False):
    """Install anything missing. Returns the list of specs that were installed."""
    if getattr(sys, "frozen", False):
        return []                        # a packaged build can't pip install into itself
    try:
        need = missing(req_file)
        if not need:
            return []
        key = "|".join(need)
        if not force and _recently_failed(key):
            log(f"dep_check: {len(need)} package(s) still missing, last attempt failed recently: {need}")
            return []
        log(f"dep_check: installing missing packages into {sys.executable}: {need}")
        try:
            have_pip = _pip(["--version"], 30).returncode == 0
        except Exception:
            have_pip = False
        if not have_pip:                 # some installs ship without pip
            try:
                subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], capture_output=True,
                               timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as e:
                log(f"dep_check: ensurepip failed: {e}")
        r = _install(need)
        if r.returncode != 0 and len(need) > 1:      # one bad package must not block the rest
            for spec in need:
                r = _install([spec])
        importlib.invalidate_caches()
        left = missing(req_file)
        done = [s for s in need if s not in left]
        if r.returncode != 0 or left:
            tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
            log(f"dep_check: could not install {left or need} ({tail[0]})")
            _stamp("|".join(left) if left else key)   # next launch will want exactly what is left
        else:
            log(f"dep_check: installed {done}")
        return done
    except Exception as e:               # never take the app down over this
        log(f"dep_check: skipped ({type(e).__name__}: {e})")
        return []


if __name__ == "__main__":
    print("missing:", missing())
    print("installed:", ensure_requirements())
