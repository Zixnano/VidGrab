"""Pluggable download engines (Session 7).

Engine protocol + FormatInfo + YtDlpEngine. Pure refactor of the yt-dlp
logic that lived in downloader.run_ytdlp and api.probe_formats — behavior
must be identical. StreamlinkEngine arrives in Session 8.
"""
import glob
import hashlib
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
from urllib.parse import parse_qs, urljoin, urlparse
from typing import Callable, Optional, Protocol

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
      0. Split layout (restructure): runtime/ via VG_RUNTIME, exe-parent,
         or app-sibling fallback
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

    # Split-layout search first: the launcher exports VG_RUNTIME pointing
    # at runtime\ before app code imports; the exe-parent and app-sibling
    # entries are fallbacks for frozen/source runs where that export is
    # missing. ffmpeg.exe/deno.exe live there in the new layout.
    runtime_candidates = []
    vg_runtime = os.environ.get("VG_RUNTIME")
    if vg_runtime:
        runtime_candidates.append(Path(vg_runtime))
    runtime_candidates.append(Path(sys.executable).parent / "runtime")
    runtime_candidates.append(Path(__file__).resolve().parent.parent / "runtime")
    for d in runtime_candidates:
        try:
            if (d / "ffmpeg.exe").is_file() or (d / "ffmpeg").is_file():
                _FFMPEG_DIR_CACHE = str(d)
                log(f"ffmpeg_location resolved: {d}")
                return _FFMPEG_DIR_CACHE
        except OSError:
            continue

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
      0. Split layout (restructure): runtime/ via VG_RUNTIME, exe-parent,
         or app-sibling fallback
      1. Next to the exe (frozen) or the project dir (dev) — a bundled copy
      2. _MEIPASS (PyInstaller --onedir's _internal/ folder), if frozen
      3. System PATH
    """
    global _YTDLP_JS_RUNTIME_CACHE
    if _YTDLP_JS_RUNTIME_CACHE is not None:
        return _YTDLP_JS_RUNTIME_CACHE

    # Split-layout search first: the launcher exports VG_RUNTIME pointing
    # at runtime\ before app code imports; the exe-parent and app-sibling
    # entries are fallbacks for frozen/source runs where that export is
    # missing. deno.exe/node.exe live there in the new layout.
    runtime_dirs = []
    vg_runtime = os.environ.get("VG_RUNTIME")
    if vg_runtime:
        runtime_dirs.append(Path(vg_runtime))
    runtime_dirs.append(Path(sys.executable).parent / "runtime")
    runtime_dirs.append(Path(__file__).resolve().parent.parent / "runtime")
    for d in runtime_dirs:
        for fname, key in (("deno.exe", "deno"), ("deno", "deno"),
                           ("node.exe", "node"), ("node", "node")):
            p = d / fname
            try:
                if p.is_file():
                    _YTDLP_JS_RUNTIME_CACHE = {key: {"path": str(p)}}
                    log(f"yt-dlp JS runtime: {key} at {p}")
                    return _YTDLP_JS_RUNTIME_CACHE
            except OSError:
                continue

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
        opts = opts or {}
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
        }
        # Cookie/Referer/User-Agent forwarded by the extension so restricted
        # tweets probe the same way they download.
        hdrs = opts.get("headers")
        if hdrs:
            ydl_opts["http_headers"] = dict(hdrs)
        js_rt = find_js_runtime()
        if js_rt:
            ydl_opts["js_runtimes"] = js_rt
        from yt_dlp import YoutubeDL  # lazy: keeps ~100 MB out of idle RAM
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
            # yt-dlp says vcodec "none" for real audio-only streams. An
            # unknown vcodec (e.g. silent/progressive Twitter MP4s) with a
            # WxH resolution is still video.
            is_audio_only = fi.vcodec == "none" or (not fi.vcodec and not m)
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
        is_playlist = bool(opts.get("download_playlist"))

        # FIX: playlist mode needs a per-item template, otherwise every
        # playlist entry overwrites the same "watch (N).mp4" file. Use the
        # playlist index + video title so each item lands in its own file.
        # %(playlist_index)03d zero-pads to 3 digits so files sort correctly
        # in Explorer (1, 2, ..., 10 instead of 1, 10, 2, ...).
        # yt-dlp treats "%" in outtmpl as a format spec, so a literal "%" in
        # the folder or file stem ("100% Real") must be doubled. Applies to
        # dest.parent too: per-item playlist jobs put the playlist title in
        # the folder name. The post-download glob below keeps the raw stem.
        parent_t = str(dest.parent).replace("%", "%%")
        if is_playlist:
            # Each playlist gets its own subfolder named after the playlist
            # (falls back to "Playlist" when yt-dlp has no title for it).
            outtmpl = (parent_t + os.sep + "%(playlist_title|Playlist)s"
                       + os.sep + "%(playlist_index)03d - %(title)s.%(ext)s")
        else:
            outtmpl = parent_t + os.sep + dest.stem.replace("%", "%%") + ".%(ext)s"

        ydl_opts = {
            "outtmpl": outtmpl,
            "http_headers": headers,
            "progress_hooks": [progress_cb],
            "quiet": True, "no_warnings": True,
            "continuedl": True,
            "noplaylist": not is_playlist,
            "retries": 10,
            "fragment_retries": 10,
            # Subtitles are disabled on purpose: yt-dlp fetches them after
            # the video completes, and YouTube's timedtext endpoint returns
            # HTTP 429 aggressively even at low volume. A 429 there aborts
            # the whole job (and the whole playlist) even though the video
            # itself downloaded fine. We don't use subtitles, so skip the
            # fetch entirely.
            "writesubtitles": False,
            "writeautomaticsub": False,
            "socket_timeout": 30,
        }

        # FIX: playlist mode gets per-item progress output so you can watch
        # "Downloading item 7 of 50" in the CMD. Single-video stays silent.
        if is_playlist:
            ydl_opts["quiet"] = False
            ydl_opts["no_warnings"] = False
            ydl_opts["noprogress"] = False

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
            if is_playlist:
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
        elif tf == "webm":
            ydl_opts["merge_output_format"] = "webm"
            if not format_id:
                ydl_opts["format"] = ("bestvideo[vcodec^=vp9]+bestaudio[acodec=opus]/"
                                      "bestvideo[ext=webm]+bestaudio[ext=webm]/best")
        elif tf == "mp4":
            ydl_opts["merge_output_format"] = "mp4"
        # Session D (O1): per-item playlist jobs queued by the picker rank
        # formats the same way whole-playlist downloads did.
        if (is_playlist or opts.get("playlist_item")) and not format_id and tf not in ("mp3", "flac", "opus", "m4a", "webm"):
            # Playlist polish: keep the format string as-is, but rank ties
            # toward avc1/m4a (resolution still wins) and always merge to
            # mp4 so playlist output plays everywhere. Playlist mode only;
            # single-video selection is untouched.
            ydl_opts["format_sort"] = ["res", "vcodec:avc1", "acodec:m4a"]
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
        from yt_dlp import YoutubeDL  # lazy: keeps ~100 MB out of idle RAM
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception:
            log(f"job {opts.get('job_id') or dest.name}: FULL TRACEBACK:\n"
                f"{traceback.format_exc()}")
            raise

        # Playlist mode: yt-dlp already wrote one file per item using the
        # %(playlist_index)s-%(title)s template; dest.stem glob won't match
        # anything useful here. Return dest unchanged — the caller uses this
        # only as a sanity path; the actual files are on disk already.
        if is_playlist:
            return Path(dest)

        # glob.escape(): "[" / "]" are legal in Windows filenames but wildcards
        # to glob() — titles like "Song [Official Video]" would never match.
        matches = [m for m in dest.parent.glob(glob.escape(dest.stem) + ".*")
                   if m.is_file() and not m.name.endswith(".part")]
        real = max(matches, key=lambda p: p.stat().st_size) if matches else dest
        return Path(real)


# ---------------------------------------------------------------------------
# Playlist probe (Session D). Module-level, NOT on the Engine Protocol: the
# Protocol returns format lists and Streamlink has no playlist notion.
# Headers are forwarded through http_headers only; nothing is written to
# disk for authentication.
# ---------------------------------------------------------------------------
class PlaylistProbeError(Exception):
    """probe_playlist failure carrying the HTTP status and code /probe returns."""

    def __init__(self, message, status=500, code="extractor_error"):
        super().__init__(message)
        self.status = status
        self.code = code


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_UNAVAILABLE_TITLES = ("[private video]", "[deleted video]")
_AUTH_HINTS = ("private video", "private playlist", "this playlist is private",
               "sign in", "log in", "login required", "requires authentication",
               "members-only", "members only")
_MISSING_HINTS = ("does not exist", "not found", "unavailable", "404",
                  "has been removed", "no longer available")


def _classify_probe_error(msg):
    """Best-effort (status, code) from a yt-dlp error message."""
    low = (msg or "").lower()
    if "timed out" in low or "timeout" in low:
        return 504, "timeout"
    if any(h in low for h in _AUTH_HINTS):
        return 403, "auth_required"
    if any(h in low for h in _MISSING_HINTS):
        return 404, "playlist_unavailable"
    return 500, "extractor_error"


def _is_youtube_host(host):
    host = (host or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _canonical_probe_url(url):
    """YouTube URLs carrying list= become playlist?list=<id> so a
    watch?v=X&list=Y URL probes as the playlist. Mix playlists (RD...) are
    generated per video and only resolve through their watch URL, so those
    keep the original URL."""
    try:
        u = urlparse(url)
        if _is_youtube_host(u.hostname) and u.hostname != "youtu.be":
            lid = (parse_qs(u.query).get("list") or [""])[0]
            if lid and not lid.startswith("RD"):
                return "https://www.youtube.com/playlist?list=" + lid
    except Exception:
        pass
    return url


def _as_int_or_none(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def probe_playlist(url, headers=None, start=0, limit=200):
    """List the items of a playlist (or the single item of a video URL)
    without downloading anything. Flat extraction: one request per ~100
    entries instead of one extractor run per entry. Returns the /probe
    response dict; raises PlaylistProbeError on failure."""
    start = max(0, int(start))
    limit = max(1, min(1000, int(limit)))
    probe_url = _canonical_probe_url(url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        # Ask for one entry past the page: seeing it is how we know the
        # list was truncated, without walking the whole playlist.
        "playlist_items": f"{start + 1}:{start + limit + 1}",
    }
    if headers:
        ydl_opts["http_headers"] = dict(headers)
    js_rt = find_js_runtime()
    if js_rt:
        ydl_opts["js_runtimes"] = js_rt
    from yt_dlp import YoutubeDL  # lazy: keeps ~100 MB out of idle RAM
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(probe_url, download=False)
            raw = None
            if info and (info.get("_type") == "playlist" or "entries" in info):
                # list() inside the try: entries can be lazy and raise here.
                raw = list(info.get("entries") or [])
    except Exception as e:
        msg = _ANSI_RE.sub("", str(e)).strip() or e.__class__.__name__
        status, code = _classify_probe_error(msg)
        raise PlaylistProbeError(msg, status, code) from e
    if not info:
        raise PlaylistProbeError("no information returned", 500, "extractor_error")

    page_host = urlparse(probe_url).hostname
    is_yt = _is_youtube_host(page_host)

    if raw is None:
        # Single video: one item, no playlist object.
        return {
            "is_playlist": False,
            "playlist": None,
            "start": start,
            "limit": limit,
            "truncated": False,
            "items": [{
                "index": 1,
                "id": info.get("id"),
                "url": info.get("webpage_url") or url,
                "title": info.get("title") or "untitled",
                "duration": _as_int_or_none(info.get("duration")),
                "uploader": info.get("uploader") or None,
                "available": True,
            }],
        }

    truncated = len(raw) > limit
    items = []
    for pos, e in enumerate(raw[:limit]):
        if not e:
            continue  # position still counted: a gap must not shift later numbers
        idx = e.get("playlist_index")
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 1:
            idx = start + pos + 1
        vid = e.get("id") if isinstance(e.get("id"), str) else None
        title = e.get("title") or "untitled"
        if is_yt and vid and re.fullmatch(r"[\w-]{11}", vid):
            item_url = "https://www.youtube.com/watch?v=" + vid
        else:
            raw_url = e.get("url") or e.get("webpage_url") or ""
            item_url = urljoin(probe_url, raw_url) if raw_url else ""
        available = bool(item_url)
        if str(title).strip().lower() in _UNAVAILABLE_TITLES:
            available = False
        if e.get("availability") == "private":
            available = False
        items.append({
            "index": idx,
            "id": vid,
            "url": item_url,
            "title": title,
            "duration": _as_int_or_none(e.get("duration")),
            "uploader": e.get("uploader") or e.get("channel") or None,
            "available": available,
        })

    ext = str(info.get("extractor") or "").split(":")[0].lower()
    src_id = info.get("id")
    if ext and src_id:
        pid = f"{ext}:{src_id}"
    else:
        pid = "url:" + hashlib.sha1(probe_url.encode("utf-8")).hexdigest()[:12]
    return {
        "is_playlist": True,
        "playlist": {
            "id": pid,
            "title": info.get("title") or "Playlist",
            "url": probe_url,
            "uploader": info.get("uploader") or info.get("channel") or None,
            "total": _as_int_or_none(info.get("playlist_count")),
        },
        "start": start,
        "limit": limit,
        "truncated": truncated,
        "items": items,
    }


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