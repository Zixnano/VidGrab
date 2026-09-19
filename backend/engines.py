"""Pluggable download engines (Session 7).

Engine protocol + FormatInfo + YtDlpEngine. Pure refactor of the yt-dlp
logic that lived in downloader.run_ytdlp and api.probe_formats — behavior
must be identical. StreamlinkEngine arrives in Session 8.
"""
import glob
import os
import json
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Optional, Protocol

from yt_dlp import YoutubeDL

from logging_setup import log


# Initialized at module level: find_js_runtime() reads this global BEFORE
# first assignment — without the init the first call raises NameError.
_YTDLP_JS_RUNTIME_CACHE = None

# Cache for ffmpeg-location resolution: same reasoning (avoids re-stat-ing
# on every download). Sentinel "_unresolved" means "not yet resolved".
_FFMPEG_DIR_CACHE = "_unresolved"


class DownloadCancelled(Exception):
    """Raised inside yt-dlp progress hooks when the user pauses/stops."""


@dataclass
class FormatInfo:
    format_id: str
    ext: str
    resolution: str
    fps: int
    vcodec: str
    acodec: str
    filesize: int
    note: str

    def to_dict(self):
        return asdict(self)


def find_ffmpeg_dir():
    """Return the directory containing ffmpeg(.exe), or None if not found.

    yt-dlp needs an external ffmpeg to merge the separate best-video and
    best-audio streams that YouTube serves over DASH. Without it, `bv*+ba/b`
    silently produces video-only files (no audio). Frozen builds bundle
    ffmpeg next to the exe; source runs need it dropped into backend/bin/
    or on PATH. Resolved once and cached.

    Search order:
      1. _MEIPASS  (PyInstaller --onedir's _internal/ folder, frozen)
      2. Next to sys.executable (frozen)
      3. <repo>/bin/  (source runs — drop ffmpeg.exe there)
      4. backend/  (source runs, sibling of this file)
      5. backend/bin/  (source runs)
      6. System PATH via shutil.which
    """
    global _FFMPEG_DIR_CACHE
    if _FFMPEG_DIR_CACHE != "_unresolved":
        return _FFMPEG_DIR_CACHE

    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
        candidates.append(Path(sys.executable).parent)
    else:
        # engines.py lives in backend/; repo root is one up.
        here = Path(__file__).resolve().parent
        candidates.append(here.parent / "bin")
        candidates.append(here)
        candidates.append(here / "bin")

    for d in candidates:
        try:
            if (d / "ffmpeg.exe").is_file() or (d / "ffmpeg").is_file():
                _FFMPEG_DIR_CACHE = str(d)
                log(f"ffmpeg_location resolved: {d}")
                return _FFMPEG_DIR_CACHE
        except OSError:
            continue

    which = shutil.which("ffmpeg")
    if which:
        _FFMPEG_DIR_CACHE = str(Path(which).parent)
        log(f"ffmpeg_location resolved via PATH: {_FFMPEG_DIR_CACHE}")
        return _FFMPEG_DIR_CACHE

    _FFMPEG_DIR_CACHE = None
    log("ffmpeg not found — audio/video merge will fail. Install ffmpeg "
        "on PATH or drop ffmpeg.exe into backend/bin/ (source) or the "
        "app folder (frozen).")
    return None


def find_js_runtime():
    """Return {'deno': path} or {'node': path} for yt-dlp, or {}.

    yt-dlp needs an external JavaScript runtime to solve YouTube's
    signature challenges since v2026.07.04. Deno is preferred (single
    binary); Node 22+ works if Deno is absent.

    Search order:
      1. Next to the exe (frozen) or the project dir (dev) — a bundled copy
      2. _MEIPASS (PyInstaller --onedir's _internal/ folder), if frozen
      3. System PATH
    """
    global _YTDLP_JS_RUNTIME_CACHE
    if _YTDLP_JS_RUNTIME_CACHE is not None:
        return _YTDLP_JS_RUNTIME_CACHE

    candidates = []
    if getattr(sys, "frozen", False):
        bases = [Path(sys.executable).parent]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(Path(meipass))
    else:
        bases = [Path(__file__).parent]

    for base in bases:
        candidates += [
            (base / "deno.exe", "deno"),
            (base / "deno", "deno"),
            (base / "node.exe", "node"),
            (base / "node", "node"),
        ]

    for name, key in (("deno", "deno"), ("node", "node")):
        which = shutil.which(name)
        if which:
            candidates.append((Path(which), key))

    for path, key in candidates:
        try:
            if path.exists() and path.is_file():
                _YTDLP_JS_RUNTIME_CACHE = {key: {"path": str(path)}}
                log(f"yt-dlp JS runtime: {key} at {path}")
                return _YTDLP_JS_RUNTIME_CACHE
        except OSError:
            continue

    log("yt-dlp JS runtime: none found — YouTube formats will be limited. "
        "Install Deno (https://deno.land) or Node 22+ and restart.")
    _YTDLP_JS_RUNTIME_CACHE = {}
    return _YTDLP_JS_RUNTIME_CACHE


def _ytdlp_version_check():
    """Log a warning if yt-dlp is more than 30 days old. YouTube extraction
    breaks regularly; stale versions fail silently."""
    try:
        from importlib.metadata import version as _v, PackageNotFoundError
        try:
            v = _v("yt-dlp")
        except PackageNotFoundError:
            log("yt-dlp not installed as a package — skipping version check")
            return
        try:
            from datetime import date
            parts = v.split(".")
            if len(parts) >= 3 and parts[0].isdigit():
                release = date(int(parts[0]), int(parts[1]), int(parts[2]))
                age = (date.today() - release).days
                if age > 30:
                    log(f"yt-dlp {v} is {age} days old — consider updating "
                        f"(pip install -U yt-dlp) for YouTube support.")
        except (ValueError, IndexError):
            pass
    except Exception as e:
        log(f"yt-dlp version check failed: {e}")


class Engine(Protocol):
    name: str

    def probe(self, url: str, opts: dict) -> Optional[list]:
        ...

    def download(self, url: str, format_id: str, dest: Path,
                 opts: dict, progress_cb: Callable) -> Path:
        ...


class YtDlpEngine:
    name = "yt-dlp"

    # --- probe (moved from api.probe_formats) ---
    def probe(self, url, opts=None):
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
        }
        js_rt = find_js_runtime()
        if js_rt:
            ydl_opts["js_runtimes"] = js_rt
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = []
        for f in (info.get("formats") or []):
            # Storyboards/mhtml thumbnail "formats" aren't real downloadable
            # streams — skip those. Everything else is kept, INCLUDING
            # audio-only and video-only streams: video-only entries are
            # valuable for high resolutions (yt-dlp merges them with an
            # audio-only stream on download), and audio-only entries are
            # what formatLabel() on the extension side is designed to show
            # as "audio only".
            if f.get("ext") == "mhtml" or (f.get("vcodec") == "none" and f.get("acodec") == "none"):
                continue
            note = (f.get("format_note") or "").strip()
            formats.append(FormatInfo(
                format_id=str(f.get("format_id") or ""),
                ext=f.get("ext") or "",
                resolution=f.get("resolution") or "",
                fps=int(f.get("fps") or 0),
                vcodec=(f.get("vcodec") or "").split(".")[0],
                acodec=(f.get("acodec") or "").split(".")[0],
                filesize=int(f.get("filesize") or f.get("filesize_approx") or 0),
                note=note,
            ))

        def _sort_key(fi):
            m = re.match(r"(\d+)x(\d+)", fi.resolution or "")
            height = int(m.group(2)) if m else 0
            is_audio_only = not fi.vcodec or fi.vcodec == "none"
            has_audio = bool(fi.acodec) and fi.acodec != "none"
            muxed = (not is_audio_only) and has_audio
            prefer_mp4_h264 = 0 if (fi.ext == "mp4" and fi.vcodec.startswith("avc1")) else 1
            # video formats first (by descending resolution, muxed before
            # video-only, mp4/h264 preferred), audio-only formats last.
            return (1 if is_audio_only else 0, -height, 0 if muxed else 1, prefer_mp4_h264)

        formats.sort(key=_sort_key)
        return formats

    # --- download (core of downloader.run_ytdlp, moved verbatim) ---
    def download(self, url, format_id, dest: Path, opts, progress_cb):
        opts = opts or {}
        headers = opts.get("headers") or {}
        outtmpl = str(dest.parent / (dest.stem + ".%(ext)s"))

        # Session Q: validate subtitle_languages is a list of strings
        sub_langs = STATE.get("subtitle_languages", ["en"])
        if isinstance(sub_langs, str):
            sub_langs = [s.strip() for s in sub_langs.split(",") if s.strip()]
        if not isinstance(sub_langs, list) or not all(isinstance(s, str) for s in sub_langs):
            sub_langs = ["en"]

        ydl_opts = {
            "outtmpl": outtmpl,
            "http_headers": headers,
            "progress_hooks": [progress_cb],
            "quiet": True, "no_warnings": True,
            "continuedl": True,
            "noplaylist": not opts.get("download_playlist", False),
            "retries": 10,
            "fragment_retries": 10,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": sub_langs,
            "embedsubs": True,
            "socket_timeout": 30,
        }

        js_rt = find_js_runtime()
        if js_rt:
            ydl_opts["js_runtimes"] = js_rt

        # ffmpeg_location: required for the bv*+ba merge on every platform.
        # Before this was only set when frozen — source runs got silent,
        # audio-less mp4s because the merge silently failed.
        ffdir = find_ffmpeg_dir()
        if ffdir:
            ydl_opts["ffmpeg_location"] = ffdir

        if format_id:
            # If the user picked a specific video-only format (e.g. YouTube
            # 137 = 1080p video, no audio), pair it with the best audio so
            # the download actually has sound. When the format_id already
            # specifies audio (or is an audio-only pick), leave it alone.
            fid = str(format_id)
            if "+" in fid or fid.startswith("audio:"):
                ydl_opts["format"] = fid
            else:
                # "137+bestaudio" — yt-dlp merges the two, needs ffmpeg.
                # "/137" fallback: if audio pairing fails, still give the
                # user their video rather than erroring out.
                ydl_opts["format"] = f"{fid}+bestaudio/{fid}"
        else:
            # Playlist mode gets a generic per-item selector; single-video
            # mode keeps the lean best-video+best-audio default.
            if opts.get("download_playlist"):
                ydl_opts["format"] = "bestvideo+bestaudio/best"
            else:
                ydl_opts["format"] = "bv*+ba/b"
        tf = (opts.get("target_format") or "").lower()
        if tf in ("mp3", "flac", "opus", "m4a"):
            ydl_opts["format"] = "bestaudio/best"
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": tf}
            ydl_opts["postprocessors"] = [pp]
            pp_args = []
            if tf == "mp3":
                pp_args = ["-q:a", "0"]
            elif tf == "opus":
                pp_args = ["-b:a", "192k"]
            elif tf == "m4a":
                pp_args = ["-b:a", "256k"]
            if pp_args:
                ydl_opts.setdefault("postprocessor_args", {})
                ydl_opts["postprocessor_args"]["ExtractAudio"] = pp_args
            for k in ("writesubtitles", "writeautomaticsub", "subtitleslangs", "embedsubs"):
                ydl_opts.pop(k, None)
        elif tf == "webm":
            ydl_opts["merge_output_format"] = "webm"
            if not format_id:
                ydl_opts["format"] = ("bestvideo[vcodec^=vp9]+bestaudio[acodec=opus]/"
                                      "bestvideo[ext=webm]+bestaudio[ext=webm]/best")
        elif tf == "mp4":
            ydl_opts["merge_output_format"] = "mp4"
        lim = opts.get("speed_limit_kbps")
        if lim and lim > 0:
            ydl_opts["ratelimit"] = lim * 1024

        # v4.0.5 diagnostics: freeze the environment + full traceback so a
        # [WinError 2] reports exactly which line/subprocess raised it.
        diag_opts = dict(ydl_opts)
        try:
            hdrs = dict(diag_opts.get("http_headers") or {})
            if hdrs.get("Cookie"):
                hdrs["Cookie"] = "<redacted>"
            diag_opts["http_headers"] = hdrs
        except Exception:
            pass
        try:
            diag_opts.pop("progress_hooks", None)
        except Exception:
            pass
        log(f"yt-dlp diagnostics: _MEIPASS={getattr(sys, '_MEIPASS', None)} "
            f"executable={sys.executable} "
            f"PATH={os.environ.get('PATH', '')[:500]} "
            f"opts={json.dumps(diag_opts, default=str)[:1500]}")
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception:
            log(f"job {opts.get('job_id') or dest.name}: FULL TRACEBACK:\n"
                f"{traceback.format_exc()}")
            raise

        # glob.escape(): "[" / "]" are legal in Windows filenames but wildcards
        # to glob() — titles like "Song [Official Video]" would never match.
        matches = [m for m in dest.parent.glob(glob.escape(dest.stem) + ".*")
                   if m.is_file() and not m.name.endswith(".part")]
        real = max(matches, key=lambda p: p.stat().st_size) if matches else dest
        return Path(real)


class StreamlinkEngine:
    """Streamlink CLI wrapper — live/MSE streams (Twitch et al.), Session 8."""
    name = "streamlink"

    def probe(self, url, opts=None):
        """`streamlink --json <url>` -> FormatInfo list. None on failure
        (the router falls through to the next engine)."""
        try:
            r = subprocess.run(["streamlink", "--json", url],
                               capture_output=True, timeout=30)
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout.decode("utf-8", "replace"))
        except Exception:
            return None
        streams = (data or {}).get("streams") or {}
        out = []
        for qname in sorted(streams.keys()):
            meta = streams[qname] or {}
            out.append(FormatInfo(
                format_id=qname,
                ext="mp4",
                resolution=(meta.get("resolution") or ""),
                fps=0, vcodec="", acodec="",
                filesize=0, note=qname,
            ))
        return out or None

    def download(self, url, format_id, dest: Path, opts, progress_cb):
        quality = format_id or "best"
        cmd = ["streamlink", "--force", "--output", str(dest), url, quality]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            # Poll real file growth for progress; the wrapper's progress_cb
            # raises DownloadCancelled on stop.
            while proc.poll() is None:
                time.sleep(0.5)
                try:
                    size = dest.stat().st_size if dest.exists() else 0
                except OSError:
                    size = 0
                progress_cb({"status": "downloading",
                             "downloaded_bytes": size,
                             "total_bytes": 0, "speed": 0})
        except BaseException:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"streamlink exited with code {proc.returncode}")
        return Path(dest)


TWITCH_LIKE_HOSTS = [
    re.compile(r"(^|\.)twitch\.tv$"),
]


from settings import STATE  # late binding is fine; settings has no engine imports

_ENGINE_POOL = {"yt-dlp": YtDlpEngine, "streamlink": StreamlinkEngine}


def route_for(url: str) -> list:
    """Engines in priority order for a URL, honoring settings:

    per_site_engine   domain (exact or subdomain) -> engine name; wins outright
    preferred_engines priority order for the default list
    engines_disabled  names excluded from routing

    Probe falls through the returned list; download does NOT — once a job
    starts on an engine it stays there."""
    disabled = set(STATE.get("engines_disabled") or [])
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    # 1. per-site override
    for dom, name in (STATE.get("per_site_engine") or {}).items():
        cls = _ENGINE_POOL.get(name)
        if cls and name not in disabled and host and \
                (host == dom or host.endswith("." + dom)):
            return [cls()]
    # 2. priority-ordered default pool (twitch-like hosts lead with streamlink)
    names = [n for n in (STATE.get("preferred_engines") or [])
             if n in _ENGINE_POOL and n not in disabled]
    for n in _ENGINE_POOL:
        if n not in names and n not in disabled:
            names.append(n)
    if host and any(rx.search(host) for rx in TWITCH_LIKE_HOSTS):
        names = sorted(names, key=lambda n: 0 if n == "streamlink" else 1)
    return [ _ENGINE_POOL[n]() for n in names ] or [YtDlpEngine()]