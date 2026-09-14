# Video Grabber — extension + desktop app

A two-part downloader: a Chrome extension that finds video on any page
(including the in-page "Download this video" hover button you wanted), and a
local desktop app that does the actual downloading with yt-dlp so large
files never have to pass through the browser's memory.

```
video-grabber/
├── extension/        Chrome (Manifest V3) extension — load unpacked
│   ├── manifest.json
│   ├── background.js   network sniffing + relays downloads to the app
│   ├── content.js      the on-page hover button (direct-download or record)
│   ├── popup.html/js   manual list of everything sniffed on the current tab
│   └── options.html/js pairing-token storage
└── backend/           Desktop companion app (Python)
    ├── server.py        local HTTP server + yt-dlp + Tkinter window
    ├── requirements.txt
    └── build_exe.md     how to freeze it into VideoGrabber.exe
```

## Setup (development / using it yourself right now)

1. **Backend**: `cd backend && pip install -r requirements.txt && python server.py`
   A dark PySide6 download-manager window opens, listening on
   `http://127.0.0.1:5757`. Leave it running. If PySide6 fails to import
   (e.g. it wasn't installed), the app automatically falls back to the
   older Tkinter window and logs why.
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
     handed to yt-dlp, which has a dedicated extractor for the site.
   - **"Record this video"** (red) — the video is playing from a `blob:` URL
     (MSE-based players), which isn't a downloadable file. Clicking starts
     `MediaRecorder` capture; click again to stop and save what was
     captured so far as a `.webm`.
5. The toolbar popup also lists everything the network sniffer has seen on
   the current tab, in case the hover button doesn't appear — "Send to
   Grabber" does the same handoff.

## To get an actual `VideoGrabber.exe`

PyInstaller builds are platform-specific, so it has to be built on Windows.
Full steps are in `backend/build_exe.md`. Short version:

```
cd backend
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name VideoGrabber --collect-all yt_dlp server.py
```

(Or push to GitHub — the workflow in `.github/workflows/build-exe.yml`
builds the exe on Windows and uploads it as an artifact, ffmpeg bundled.)

## What's new in v3.2

- **New PySide6 GUI** (`backend/gui_qt.py`) replaces Tkinter as the primary
  window — dark IDM-style layout, sidebar category filters, sortable table
  with a live progress-bar column, search box, and an activity-log strip.
  Tkinter is kept as an automatic fallback if PySide6 isn't installed.
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
  Video Grabber instead, with cookies/referer/UA attached.
- **Drag finished rows out of the Qt window** into Explorer or another app
  (e.g. drop a finished `.mp4` onto VLC).
- **Auto-convert to MP4**: any video job that lands as `.mkv`/`.webm`/etc.
  gets remuxed (or transcoded if remuxing fails) to `.mp4` automatically.
  Toggle via the `auto_mp4` setting.
- **"Download File Info" dialog**: URL, category, save-as path with a
  "remember this path for &lt;category&gt;" checkbox, description, and a
  live file-size probe before you commit to starting the download.
- **"Grab all media on this page" now shows a checklist** in the popup —
  every sniffed file gets a checkbox (checked by default), with a
  select-all/none toggle, so you can leave out the ones you don't want
  before sending the batch to the app.

## Feature set (v3.1)

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
  behavior, play, convert (mp4/mkv/mp3/wav via ffmpeg), properties.
- **Drag finished downloads out** of the list into Explorer/other apps.
- **Download-finished toast** with Open file / Open folder, auto-dismiss.
- **Minimize-to-bar**: closing the window drops to a small always-on-top
  status strip (green dot = active downloads) instead of quitting.
- **Tabbed Options**: General, File Types, Save-to (per-category folders),
  Downloads, Connection (timeout/retries), Post-Download.
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
  them, and this project doesn't attempt DRM circumvention.
- **Direct `.mp4`/`.m3u8`/`.mpd` links**: this is the strong path. The
  sniffer + yt-dlp combo handles the large majority of "normal" video sites
  (yt-dlp supports 1,800+ sites — that's the engine doing the heavy lifting).
- **`blob:`/MSE players without DRM**: handled via the record button, but
  it's a real-time capture, not a download — it only captures from the
  moment you click forward, at playback speed, re-encoded to webm.
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

## Sensible next steps if you keep building this

- Swap the minimize-to-bar strip for a proper `pystray` tray icon.
- Browser context-menu entry point (see above).
- Smarter batch scanning (merge the network-sniffed list from `background.js`
  into the popup's DOM scan).
