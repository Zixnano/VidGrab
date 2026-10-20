"""component_updater.py: ffmpeg and deno updates for the installed app (v5.5).

Same idea as updater.bat's rename-then-replace swap, done at launch instead of
by a batch file: the new binary is downloaded, checked, and placed in runtime\\
as <name>.exe.new together with a pending marker. The launcher swaps it in on
the next start, before anything holds the file open (old -> .old, new -> live),
and deletes the .old leftovers after the app has stayed up for a minute.

Python itself is NOT updated here: the frozen app bundles its own interpreter,
so it only changes with a full app update.

status() and update() are plain blocking functions meant to run on a worker
thread. update() never raises; it returns {"ok": bool, "message": str}.
"""
import hashlib
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

FFMPEG_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
DENO_API = "https://api.github.com/repos/denoland/deno/releases/latest"
FFMPEG_ZIP_RE = re.compile(r"^ffmpeg-n(\d+\.\d+)-latest-win64-gpl-(\d+\.\d+)\.zip$")
DENO_ZIP = "deno-x86_64-pc-windows-msvc.zip"
PENDING_FILE = "components-pending.json"
COMPONENTS = ("ffmpeg", "deno")
# component -> files it owns inside runtime\
FILES = {"ffmpeg": ("ffmpeg.exe", "ffprobe.exe"), "deno": ("deno.exe",)}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- helpers
def _runtime_dir():
    rt = os.environ.get("VG_RUNTIME", "")
    return Path(rt) if rt and Path(rt).is_dir() else None


def _staging_dir(name):
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "VideoGrabber" / "components" / name


def _get_json(url):
    req = Request(url, headers={"User-Agent": "VideoGrabber-Updater",
                                "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_text(url):
    req = Request(url, headers={"User-Agent": "VideoGrabber-Updater"})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _vtuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def _exe_path(name):
    rt = _runtime_dir()
    if rt is not None and (rt / FILES[name][0]).is_file():
        return rt / FILES[name][0]
    return None


def installed_version(name):
    """Version string of the runtime's binary, '' when not found/unreadable."""
    exe = _exe_path(name)
    if exe is None:
        return ""
    try:
        r = subprocess.run([str(exe), "-version" if name == "ffmpeg" else "--version"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=_NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        first = out[0] if out else ""
        m = re.search(r"version\s+n?(\d+(?:\.\d+)+)", first) if name == "ffmpeg" \
            else re.search(r"deno\s+(\d+(?:\.\d+)+)", first)
        if m:
            return m.group(1)
        m = re.search(r"version\s+(\S+)", first)      # git builds: N-12345-gabc
        return m.group(1) if m else first[:40]
    except Exception as e:
        log(f"components: couldn't read {name} version: {e}")
        return ""


def pending():
    rt = _runtime_dir()
    if rt is None:
        return {}
    try:
        return json.loads((rt / PENDING_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- latest
def latest_ffmpeg():
    """Newest stable-branch win64 GPL build from BtbN. Returns
    {version, url, sha256, name}. Stable branches (n7.1, n8.0, ...) are used
    because their version numbers can be compared with the installed one."""
    rel = _get_json(FFMPEG_API)
    best = None
    for a in rel.get("assets") or []:
        m = FFMPEG_ZIP_RE.match(a.get("name") or "")
        if m and (best is None or _vtuple(m.group(2)) > _vtuple(best[0])):
            best = (m.group(2), a)
    if best is None:
        raise RuntimeError("no versioned ffmpeg win64 build in the latest BtbN release")
    ver, asset = best
    sha = (asset.get("digest") or "").replace("sha256:", "")
    if not sha:
        for a in rel.get("assets") or []:
            if (a.get("name") or "").lower() == "checksums.sha256":
                for line in _get_text(a["browser_download_url"]).splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1].lstrip("*") == asset["name"]:
                        sha = parts[0]
                        break
    return {"version": ver, "url": asset["browser_download_url"],
            "sha256": sha.lower(), "name": asset["name"]}


def latest_deno():
    rel = _get_json(DENO_API)
    tag = (rel.get("tag_name") or "").lstrip("v")
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == DENO_ZIP), None)
    if not tag or asset is None:
        raise RuntimeError("no win64 deno zip in the latest release")
    sha = (asset.get("digest") or "").replace("sha256:", "")
    if not sha:
        side = next((a for a in rel.get("assets") or []
                     if a.get("name") == DENO_ZIP + ".sha256sum"), None)
        if side:
            sha = (_get_text(side["browser_download_url"]).split() or [""])[0]
    return {"version": tag, "url": asset["browser_download_url"],
            "sha256": sha.lower(), "name": DENO_ZIP}


_LATEST = {"ffmpeg": latest_ffmpeg, "deno": latest_deno}


# ---------------------------------------------------------------- status
def status():
    """{name: {installed, latest, update_available, pending, error}} plus
    'frozen_only' when this isn't the installed exe."""
    out = {"frozen_only": not (getattr(sys, "frozen", False) and _runtime_dir() is not None)}
    pend = pending()
    for name in COMPONENTS:
        d = {"installed": installed_version(name), "latest": "", "update_available": False,
             "pending": (pend.get(name) or {}).get("version", ""), "error": ""}
        try:
            d["latest"] = _LATEST[name]()["version"]
            inst = _vtuple(d["installed"])
            d["update_available"] = (bool(inst) and _vtuple(d["latest"]) > inst
                                     and d["latest"] != d["pending"])
        except Exception as e:
            d["error"] = str(e)
            log(f"components: {name} version check failed: {e}")
        out[name] = d
    return out


# ---------------------------------------------------------------- update
def _download(url, dest, expect_sha):
    h = hashlib.sha256()
    req = Request(url, headers={"User-Agent": "VideoGrabber-Updater"})
    with urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    got = h.hexdigest().lower()
    if expect_sha:
        if got != expect_sha:
            raise RuntimeError(f"checksum mismatch (expected {expect_sha[:12]}..., got {got[:12]}...)")
        log(f"components: sha256 verified for {Path(dest).name}")
    else:
        log("components: no published checksum, falling back to a run check only")


def _extract(zip_path, wanted, dest_dir):
    """Pull the wanted file names out of the zip (any folder depth), with a
    path check; returns {name: Path}."""
    found = {}
    dest_real = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for m in zf.infolist():
            base = Path(m.filename).name
            if m.is_dir() or base.lower() not in wanted:
                continue
            target = (dest_dir / base).resolve()
            if not target.is_relative_to(dest_real):
                raise RuntimeError(f"unsafe path in zip: {m.filename}")
            with zf.open(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            found[base.lower()] = target
    missing = [w for w in wanted if w not in found]
    if missing:
        raise RuntimeError("zip is missing " + ", ".join(missing))
    return found


def _verify_runs(name, exe, version):
    r = subprocess.run([str(exe), "-version" if name == "ffmpeg" else "--version"],
                       capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    out = (r.stdout or r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(f"{exe.name} failed its self-check (exit {r.returncode})")
    if name == "deno" and version not in out:
        raise RuntimeError(f"downloaded deno reports a different version than {version}")


def update(name):
    """Download, verify and stage one component. Applies on next launch."""
    try:
        if name not in COMPONENTS:
            raise RuntimeError(f"unknown component {name}")
        rt = _runtime_dir()
        if not getattr(sys, "frozen", False) or rt is None:
            raise RuntimeError("updates only apply to the installed exe, not a source run")
        info = _LATEST[name]()
        stage = _staging_dir(name)
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, exist_ok=True)
        log(f"components: downloading {name} {info['version']} from {info['url']}")
        zip_path = stage / "pkg.zip"
        _download(info["url"], zip_path, info["sha256"])
        wanted = [f.lower() for f in FILES[name]]
        extracted = _extract(zip_path, wanted, stage)
        for exe_name, exe in extracted.items():
            _verify_runs(name if exe_name == FILES[name][0] else "ffmpeg", exe, info["version"])
        # Verified: place beside the live files as <name>.new.
        for exe_name, exe in extracted.items():
            dest = rt / (exe_name + ".new")
            shutil.copy2(exe, dest)
            if dest.stat().st_size != exe.stat().st_size:
                raise RuntimeError(f"copy to {dest.name} was incomplete")
        marker = pending()
        marker[name] = {"version": info["version"], "files": list(FILES[name])}
        (rt / PENDING_FILE).write_text(json.dumps(marker, indent=2), encoding="utf-8")
        shutil.rmtree(stage, ignore_errors=True)
        log(f"components: {name} {info['version']} staged; applies on next launch")
        return {"ok": True, "message": f"{name} {info['version']} is ready. "
                                       "It applies the next time Video Grabber starts."}
    except Exception as e:
        log(f"components: {name} update failed: {e}")
        return {"ok": False, "message": f"{name} update failed: {e}"}
