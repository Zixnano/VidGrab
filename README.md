# Video Grabber — extension + desktop app

A two-part downloader: a Chrome extension that finds video on any page
(including the in-page "Download this video" hover button you wanted), and a
local desktop app that does the actual downloading with yt-dlp so large
files never have to pass through the browser's memory.

```
video-grabber/
├── extension/          Chrome (Manifest V3) extension — load unpacked
│   ├── manifest.json
│   ├── background.js     network sniffing + relays downloads to the app
│   ├── content.js        the on-page hover pill (download / record)
│   ├── popup.html/js     manual list of everything sniffed on the current tab
│   ├── options.html/js   pairing-token storage
│   └── icons/            extension + notification icons (16/32/48/128 PNG)
├── backend/            Desktop companion app (Python, PySide6)
│   ├── server.py         Flask entry point (registers routes, serves the GUI)
│   ├── api.py            HTTP routes (thin; delegates to jobs/downloader)
│   ├── jobs.py           job registry, status state machine, snapshot/restore
│   ├── downloader.py     worker pool; generic segmented downloads; conversions
│   ├── engines.py        yt-dlp / streamlink engines + route_for() routing
│   ├── settings.py       persistent settings, categories, rules
│   ├── palette.py        theme tokens + presets
│   ├── gui_style.py      QSS builder
│   ├── gui_qt.py         the PySide6 download-manager window
│   ├── logging_setup.py  rotating log file
│   ├── requirements.txt
│   ├── VideoGrabber.ico  app icon (CI --icon expects it here)
│   └── tests/            pytest suite (run: pytest tests/)
├── docs/               SMOKE_TEST.md and other guides
├── .github/workflows/  CI — builds VideoGrabber.exe on Windows
└── (repo root)         README, CHANGELOG, FIXES, ARCHITECTURE, icons…
```

## Setup (development / using it yourself right now)

1. **Backend**: `cd backend && pip install -r requirements.txt && python server.py`
   A dark PySide6 download-manager window opens, listening on
   `http://127.0.0.1:5757`. Leave it running. PySide6 is required — there
   is no fallback window anymore (the old Tkinter fallback was removed in
   v4.0; the `tkinterdnd2` dependency went with it).
   Files are saved under `%USERPROFILE%\Downloads\VideoGrabber\`.
2. **Extension**: open `chrome://extensions`, enable Developer Mode, "Load
   unpacked", select the `extension/` folder.
3. Pair them: in the app, **Tools → Copy pairing token for extension**, then
   paste it into the extension's Options page (right-click toolbar icon → Options).
4. Browse normally. When a page has a playable `<video>`, a small pill
   appears over it:
   - **"Download this video"** (green) — the video has a real network URL.
     Clicking sends it to the backend, which runs yt-dlp with the page's
     cookies/referer/user-agent attached, merges audio+video, and saves to
     `~/Downloads/VideoGrabber`.
   - **"Send page URL to Grabber"** (purple, MSE-only sites like Twitch) —
     the player exposes no capture-able stream, so the page URL itself is
     handed to yt-dlp (or streamlink, for live sites), which has a dedicated
     extractor for the site.
   - **"Record this video"** (red) — the video is playing from a `blob:` URL
     (MSE-based players), which isn't a downloadable file. The pill menu
     offers four capture modes — element (video+audio), tab (video+audio),
     tab audio-only, and screen — with quality presets (Low/Medium/High/
     Source). A floating indicator shows the elapsed time with pause/resume
     and stop; stopping saves what was captured as `.webm` and hands it to
     the app (desktop notification on start/stop).
5. The toolbar popup also lists everything the network sniffer has seen on
   the current tab, in case the hover button doesn't appear — "Send to
   Grabber" does the same handoff.

## To get an actual `VideoGrabber.exe`

PyInstaller builds are platform-specific, so it has to be built on Windows.
Full context is in `backend/build_exe.md`; the canonical command (matching
`.github/workflows/build-exe.yml`) is:

```
cd backend
pip install -r requirements.txt
pyinstaller --onedir --noconsole --name VideoGrabber --icon=VideoGrabber.ico --collect-all yt_dlp --collect-all PySide6 --collect-all streamlink --collect-all watchdog --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." --add-binary "deno.exe;." server.py
```

Notes: `streamlink` is bundled for live-site (Twitch) routing; `watchdog`
for the screen-recording auto-import watcher; `deno.exe` lets yt-dlp solve
YouTube's JavaScript challenges. Or just push to GitHub — the CI workflow
downloads ffmpeg/ffprobe/Deno itself and uploads the built exe as an
artifact. See PACKAGING.md for the release-zip layout rules.

## Pushing changes

`push.bat` (repo root, Windows) automates the usual `git add` → commit →
pull --rebase → push cycle so pushing doesn't have to be done step by step
each time. Double-click it, or run it from a terminal with an optional
commit message: `push.bat "fixed the cookie thing"`. With no message it
prompts for one (blank is fine — it'll auto-generate one). It pulls with
`--rebase` before pushing so your commit lands on top of anything new on
the remote, and it tells you plainly if something needs manual attention
(a merge conflict, no upstream branch yet, etc.) rather than failing silently.

## What's new in v4.0

- **Modular backend**: the old monolithic `server.py` is split into
  `api` / `jobs` / `downloader` / `engines` / `settings` (+ theme modules),
  with a proper job status state machine and jobs.json snapshot/restore.
- **Pluggable engines**: yt-dlp and streamlink behind one interface;
  `route_for()` picks per URL (Twitch prefers streamlink) and honors
  per-site engine overrides, engine priority, and disabled engines
  (Settings → Engines tab).
- **Multi-quality checklist downloads**: the "Download File Info" dialog
  shows a checkable format list (plus audio-only MP3/FLAC/Opus/M4A rows) —
  tick several qualities and one job per selection queues, with the
  resolution in each filename.
- **Download quality tuning**: mp4/H.264-preferred format selection, MKV
  container preservation, transparent-quality MP3 (`-q:a 0`), Opus/M4A
  bitrate targets, `-crf 18 -preset medium` fallback transcode.
- **4-mode recorder** (extension): element / tab / tab-audio / screen, with
  tab fallback when element capture fails, quality presets, floating
  indicator, and notifications (v4.0.2).
- **Theming**: three presets + accent picker + per-token color overrides
  with a live preview (Settings → Theme tab), QSS export/import.
- **Responsiveness**: all periodic HTTP and settings reads moved off the
  GUI thread; the app exits cleanly with in-flight fetches.
- **Animations**: row fades, interpolated progress bars, dialog fades —
  all gated by an "Enable animations" setting.
- **Test suite**: `backend/tests/` (pytest) covering palette/QSS, engine
  options, job state, migrations, and the progress helper.

## v3.2 (historical)

- **PySide6 GUI** (`backend/gui_qt.py`) replaced Tkinter as the primary
  window — dark IDM-style layout, sidebar category filters, sortable table
  with a live progress-bar column, search box, and an activity-log strip.
  (The Tkinter fallback itself was removed in v4.0; PySide6 is required.)
- **Force-on-top on new downloads**: the window un-minimizes and briefly
  flashes to the front whenever a job is queued from any source (extension,
  clipboard, GUI, or an auto-imported screen recording). Toggle via the
  `force_on_top` setting.
- **Screen-recording auto-import** (Windows only): a background folder
  watcher (`watchdog`) watches `~/Videos/Captures` (and similar folders) and
  automatically copies finished recordings into the Video category once the
  file size stops changing.
- **Chrome download interception** (opt-in, off by default): a checkbox in
  the extension's Options page — when enabled, clicking any downloadable
  link in Chrome cancels the browser's own download and hands the URL to
  Video Grabber instead, with cookies/referer/UA attached (v4.0.1: opens
  the Add dialog rather than silently queueing).
- **Drag finished rows out of the Qt window** into Explorer or another app
  (e.g. drop a finished `.mp4` onto VLC).
- **Auto-convert to MP4**: any video job that lands as `.mkv`/`.webm`/etc.
  gets remuxed (or transcoded if remuxing fails) to `.mp4` automatically.
  Toggle via the `auto_mp4` setting.
- **"Download File Info" dialog**: URL, category, save-as path with a
  "remember this path for <category>" checkbox, description, and a
  live file-size probe before you commit to starting the download.
- **"Grab all media on this page" checklist** in the popup — every sniffed
  file gets a checkbox (checked by default), with a select-all/none toggle,
  so you can leave out the ones you don't want before sending the batch to
  the app.

## Feature set (v3.1 — still current)

Downloads:
- **Queue** with pause/resume/stop/delete, per-item or in bulk.
- **Resumable downloads** for direct files via HTTP `Range` requests (.part files).
- **Segmented (8-connection) downloads** for large direct files when the
  server supports range requests (files ≥ 1 MB). Pausing a segmented job
  restarts it cleanly on resume rather than corrupting the merge.
- **yt-dlp engine** for HLS/DASH/page URLs (YouTube etc.), with cookies,
  referer and UA from the browser, subtitles embedded, ffmpeg merging.
- **Speed limiter** (global) and **max concurrent downloads**.
- **Categories** (Video/Music/Compressed/Documents/Programs/Other) with
  auto-sort into subfolders, per-category folder overrides, sidebar filters,
  Finished/Unfinished views, and per-site rules that assign categories by domain.
- **Batch add**, **clipboard monitor**, **scheduler** (arm a start time),
  **export/import queue** as JSON, job history persisted across restarts.

GUI:
- Dark download-manager window with live progress, speed, status.
- **Right-click context menu**: open/open-with/open-folder, rename/move,
  redownload, resume/stop, refresh URL, remove, category change, double-click
  behavior, play, convert (mp4/mkv/mp3/wav/m4a/flac/opus via ffmpeg), properties.
- **Download-finished toast** with Open file / Open folder, auto-dismiss.
- **Minimize-to-bar / system tray**: closing the window drops to a small
  always-on-top status strip or tray icon (green = active downloads)
  instead of quitting.
- **Tabbed Settings**: General, Downloads, Connection, Save To, File Types,
  Post-Download, Rules, **Engines**, **Theme**, Pairing.
- **Post-download actions**: Windows Defender scan, auto-extract zips,
  open containing folder, play sound, shut down PC when queue empties.
- **Portable mode**: drop a `portable.txt` next to the exe and settings/jobs
  live alongside it instead of in the user profile.
- **LAN web UI** at `http://<pc-ip>:5757/remote` (enable "Allow LAN access"
  in Options) — view and control the queue from a phone/other device on your
  network. Action links carry a random per-launch key so strangers on the
  LAN can't touch your queue without it.
- **Pairing token** shared with the extension (Tools → Copy pairing token).

## What this will and won't do (being straight with you)

- **DRM'd content (Netflix, Disney+, most paid streaming platforms)**: won't
  work, on purpose. Those streams are encrypted with Widevine/PlayReady;
  there's no legal or reliable technical way for a general tool to decrypt
  them, and this project doesn't attempt DRM circumvention. (The recorder's
  element capture fails on DRM for the same reason; it falls back to tab
  capture with a notification.)
- **Direct `.mp4`/`.m3u8`/`.mpd` links**: this is the strong path. The
  sniffer + yt-dlp combo handles the large majority of "normal" video sites
  (yt-dlp supports 1,800+ sites — that's the engine doing the heavy lifting).
- **`blob:`/MSE players without DRM**: handled via the recorder, but
  it's a real-time capture, not a download — it only captures from the
  moment you click forward, at playback speed, saved as webm.
- **Sites that scramble/obfuscate the URL in custom JS**: the generic
  sniffer won't catch these; yt-dlp's per-site extractors cover many of them
  when you hand the page URL to the app directly.
- **Only use this on content you actually have the right to download** —
  your own uploads, permissively-licensed material, or sites whose terms
  allow it. It doesn't check that for you.

## Deliberately left out

- **IDM's own icons/branding** — built with its own look rather than copying IDM's UI.
- **"Tell a Friend"** — a referral/marketing feature, not something useful for personal use.
- **Multi-language UI** — English-only for now.
- **Browser context-menu hooks** ("Download this link with Video Grabber"
  on right-click in Chrome) — doable, just not built yet; the hover pill,
  popup list and page scanner cover the same ground.
