# Video Grabber v5: what changed

Copy `backend/` and `extension/` over your repo (same filenames), put `tests/`
in your `tests/` folder and `docs/SMOKE_TEST.md` in docs/, then reload the
extension (it asks for two new permissions). All files are complete, not diffs.
New file: `backend/ytdlp_update.py`. Run `pytest tests/` (44 tests).

## Downloads
- **YouTube pacing:** one gate spaces YouTube job starts 5-15 s apart across
  jobs; the first after idle starts at once. Request/subtitle sleeps = 1 s for
  YouTube only. Stop cancels a wait. Settings > Connection.
- **Retry + error classes:** bot check holds YouTube 30 min and is never
  auto-retried; 429 retries at 2/5/10 min; network errors at 15/45/135 s.
  Resume / Redownload / "Retry now" clear holds. New `RETRY` job event.
- **Archive:** whole-playlist jobs skip downloaded videos (Redownload bypasses);
  the playlist picker flags finished items and leaves them unchecked.
- **Proxy:** URL + scope (YouTube only / all). Credentials redacted in logs.
- **Naming:** junk names become the real yt-dlp title or `site_date_time`;
  "(3) " and " - YouTube" noise stripped; "(2) (2)" no longer stacks.
- **Source site:** stored per job; favicons uploaded once per domain.
- **Disk guard:** new jobs wait below `min_free_space_mb` (default 500).
- **Queue order:** `priority` field; the dispatcher starts jobs in that order;
  `/reorder/<id>` {top|up|down|bottom}.
- **Duplicates:** `/dup-check`; `/download` and `/show-add-dialog` return
  `duplicate_of`; the Add dialog shows a warning.

## Updates
- **yt-dlp self-update** (`ytdlp_update.py`): sha256-verified PyPI wheel,
  staged, applied at next launch, rollback in Settings > Updates.
- **Scheduler:** checks the app and yt-dlp every 6 h in the background; the app
  is never installed without asking. Started from the dispatcher thread.

## GUI
- Add / Playlist dialogs no longer always-on-top, so they minimize with the
  main window (the bug).
- Sidebar: Active, Failed, counts. Empty-state messages.
- Settings: page list + search, no red tabs; new Connection, Downloads and
  Updates options.
- Table: ETA and Source (favicon) columns, resizable and remembered; retry
  countdown text; details panel; toolbar collapses to icons when narrow.
- Status bar: YouTube hold, low disk, update badge (Install / Skip).
- Queue position menu + Alt+Up/Down for queued jobs (not drag-and-drop:
  dragging a row already drags the file out to Explorer/Discord).
- Failed rows older than 24 h are hidden from All/Unfinished; Tools > Clear
  failed downloads; one tray notification per finished batch.

## Extension
- Popup: live filter that remembers choices, media-only default, tiny-file
  filter, size probe, "Download (N)", paired/queue/YouTube-hold status, Recent
  downloads with progress, per-site "hide pill" switch.
- Right-click "Download with Video Grabber". Per-site quality memory (starred,
  listed first). "Already in your list" label on every send path.
- Interception: cancel first, parallel lookups, timing in the console. If the
  app rejects the handoff the download is given back to the browser.

## Not done (needs files I never received, or can't be done from here)
- launcher.py, updater.bat, VideoGrabber.spec, build-exe.yml: not provided.
  Nothing in v5 requires changing them.
- The interception delay itself: the app now logs how long its dialog took to
  open; the rest is Chrome waiting for the server before it reports a download.
- Real-Windows verification of window flipping and the full app run.
