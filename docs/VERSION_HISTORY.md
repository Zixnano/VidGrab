# Version history

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

