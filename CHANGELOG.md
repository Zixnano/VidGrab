# Changelog

## v4.0.9 — the two "your call" items from BUGS.md, resolved
- **Deno version pinned in CI** (`BUGS.md` #6 / `QUESTIONS.md` #3):
  `.github/workflows/build-exe.yml` no longer downloads
  `releases/latest` for Deno. A job-level `DENO_VERSION: "2.9.6"` env var
  (current stable as of this pass) now drives the download URL — bump
  that one value deliberately when you want a newer Deno instead of
  silently getting whatever's newest on the next unrelated push.
- **Empty-cookie warning for YouTube downloads** (`BUGS.md` #7):
  `engines.py::YtDlpEngine.download()` now logs a clear warning when a
  YouTube/youtu.be URL has no cookie string available from either
  `opts["cookie"]` or the `Cookie` header — explains that public videos
  are unaffected but age-restricted/private/member-only ones will fail,
  and to check that the browser the extension runs in is logged into
  YouTube. Non-blocking: the download still proceeds exactly as before,
  it just no longer fails silently with an unexplained 403 later. 4 new
  tests in `test_engines.py` cover: warns for youtube.com and youtu.be
  with no cookie, doesn't warn when a cookie is present, doesn't warn for
  non-YouTube URLs.
- Full suite: 33 passed, 2 pre-existing skips, 0 failed.

## v4.0.8 — version display, disk-space awareness, a friendlier Theme tab, push.bat
- **`push.bat`** added at the repo root: automates `git add -A` → commit →
  `pull --rebase` → `push` in one double-click, so pushing doesn't have to
  be done step by step. Accepts an optional commit message as an argument,
  otherwise prompts (blank is fine — auto-generates one), and surfaces
  merge conflicts / missing-upstream errors plainly instead of failing
  silently.
- **Version is now easy to check.** `settings.APP_VERSION` is the single
  source of truth (kept in sync with `extension/manifest.json` — a test
  now guards against them drifting the way `CATEGORIES` once did). It
  shows up in: the main window's title bar, a permanent status-bar label,
  and a new Tools → "About Video Grabber…" dialog. A new `GET /version`
  endpoint exposes it to anything else that wants it.
- **Disk-space monitor.** A new `GET /diskspace` endpoint
  (`settings.disk_usage_for()`) reports free/used/total bytes for the
  downloads drive. The main window's status bar shows free space,
  refreshed every 15s, turning red when free space drops under 2 GB or
  the drive is over 95% full. The Add Download dialog now also shows free
  space next to the probed file size and warns (orange near the limit,
  red if the file won't fit) before you commit to a download that's
  likely to fail partway through from running out of room.
- **Theme tab redesigned to be approachable, not an engineer's token
  table.** Preset and accent picker (previously split oddly across the
  General tab and a separate raw table) now live together in the Theme
  tab: preset as a named dropdown, accent as a row of one-click color
  swatches plus a custom picker. A real live preview (a mock title,
  buttons, and progress bar restyled on the fly) replaced the old
  read-only QSS-code-dump preview. The 14-row raw hex-token table is
  still there for anyone who wants it, just hidden by default behind a
  "Show advanced color overrides" checkbox instead of being the first
  thing anyone sees. Added a "Reset to defaults" button. `CATEGORIES`-style
  de-duplication applied here too: `self.theme_preset`/`self.accent`/
  `self._theme_tokens` are still the only state `_save_inner` reads, so
  saving/loading/export/import all work exactly as before.
- **Quality-of-life additions:** "Retry all failed" (Tools menu — retries
  every job currently in an error state in one click) and "Copy URL"
  (per-job right-click context menu).
- Test suite: added coverage for `disk_usage_for()` and the
  version/manifest consistency check. Full suite: 29 passed, 2 pre-existing
  skips, 0 failed.

## v4.0.7 — BUGS.md follow-through (backend only, no extension changes)
Everything below was found and reported (not fixed) in the v4.0.6 pass's
`BUGS.md`; this pass fixes 1–5 and closes them out. 6–7 are new ideas from
this same pass, left open pending a product decision — see `BUGS.md`.
- **Screen-recording auto-import now creates a real job.** Previously
  `_RecordingHandler._import_after_delay` copied the finished recording to
  disk and only fired the GUI's "new job" hook with a bare synthetic dict —
  nothing was ever added to `JOBS`, so the recording never showed up in
  `/jobs`, the GUI table, or `jobs.json`. Fixed to mirror the `/upload`
  route: `new_job()` → `_repair_recording()` → `_probe_recording()` →
  `transition(..., JobEvent.COMPLETE)` (or `FAIL` if corrupt) →
  `maybe_convert_to_mp4()`.
- **`JOBS` dict iteration made thread-safe** at every call site that wasn't
  already doing it (`api.py` ×2, `downloader.py` ×2, `jobs.py` ×2,
  `server.py` ×1) — all now copy via `list(...)` before iterating, matching
  the one call site (`dispatcher_loop`) that already did. Prevents an
  intermittent `RuntimeError: dictionary changed size during iteration`
  under concurrent job creation (most reachable via `/jobs`, which the GUI
  polls every second).
- **LAN remote-key check is now constant-time** (`secrets.compare_digest`
  instead of `==`), matching the API token check two lines below it.
- **`job_id` added to the `opts` dict** passed to `engine.download()`, so
  yt-dlp diagnostic/traceback/cookie-file log lines are keyed by job id
  instead of falling back to the destination filename.
- **Stale test/implementation drift resolved:** `test_settings.py` and
  `test_jobs.py` now match actual behavior (`safe_filename` replaces
  illegal characters with `_`; `category_for("song.mp3")` returns
  `"Music"`, matching `CATEGORY_MAP` and the README everywhere). Also
  de-duplicated `CATEGORIES` — it now lives once, as a tuple, in
  `settings.py`; `gui_qt.py`'s two separate copies (a module-level list and
  an unrelated class-level tuple) were removed in favor of importing it.
- **Stale cookie-file sweep added at startup** (`server.py`): any
  `vg_cookies_*.txt` left in the OS temp dir older than 6 hours gets
  deleted. These are normally cleaned up in a `finally` block right after
  each download, but a hard kill mid-download skips that — and each file
  holds a user's session cookies in plaintext, so leaving them indefinitely
  was worth closing even though it's minor.
- **Test coverage added** for everything introduced in v4.0.6's Task 1/2:
  cookie-file writing/cleanup/fallback-priority in `YtDlpEngine.download()`,
  and format sorting/filtering in `YtDlpEngine.probe()` (audio-only kept,
  storyboards dropped, mp4/h264 preferred at equal resolution). Full suite:
  26 passed, 2 pre-existing skips, 0 failed (up from 3 failures at the end
  of v4.0.6).

## v4.0.6 — cookie handoff, full format list, dialog focus, intercept-all
- **YouTube cookies (Task 1):** `cookiesfrombrowser` removed for good —
  it cannot work on Chrome 127+ / Vivaldi 6.9+ (App-Bound Encryption,
  yt-dlp/yt-dlp#10927). `YtDlpEngine.download()` now takes the
  extension-supplied cookie string (already sent by `background.js`'s
  `cookieHeaderFor()` and already threaded through `api.py` → `jobs.py` →
  `downloader.py`), writes it to a Netscape-format cookie file per job in
  the OS temp dir, and points yt-dlp at it via `cookiefile`, cleaning up
  afterward. The rest of the pipeline needed no changes — the bug was
  isolated to `engines.py`.
- **Full quality list (Task 2):** `YtDlpEngine.probe()` was using a
  narrow `player_client` list that only surfaced a handful of progressive
  YouTube formats; broadened to match `download()`'s client list and
  stopped discarding audio-only formats. Formats are now sorted
  (resolution descending, muxed-before-video-only, mp4/h264 preferred).
  `content.js` had two separate `formats.slice(0, 12)` truncations in the
  pill's quality menu — both removed; the backend's own sanity cap moved
  from 30 to 40.
- **Add-dialog focus (Task 3):** `AddDownloadDialog` now sets
  `WindowStaysOnTopHint` and raises/activates itself on every `showEvent`;
  `_on_show_dialog` also raises and activates the main window first, so
  the dialog can no longer open behind other windows.
- **Intercept-all downloads (Task 4):** `background.js`'s
  `chrome.downloads.onCreated` handler no longer gates on a known-extension
  regex or a HEAD-probe fallback — every intercepted download (other than
  the app's own localhost/IP traffic and the extension's own downloads)
  now opens the Add Download dialog via `/show-add-dialog`. The
  `skip_add_dialog` escape hatch remains server-side only, as before.
- **`BUGS.md`** added: 5 findings from this pass's audit (screen-recording
  auto-import never registers a real job; several unprotected `JOBS` dict
  iterations across threads; a non-constant-time LAN remote-key
  comparison; a missing `job_id` in yt-dlp diagnostics; 3 pre-existing
  test/implementation mismatches in `backend/tests/test_settings.py` and
  `test_jobs.py`). None of these were fixed — logged for review.

## v4.0.0 — full application rewrite (Sessions 0–13)
- Modular backend: server/api/jobs/downloader/engines split; jobs.json
  snapshot + restore; status state machine; engine routing (yt-dlp,
  streamlink) honoring per-site/preference/disabled settings.
- Download quality: H.264/mp4-first format string, MKV container
  preservation, mp3 `-q:a 0` / opus 192k / m4a 256k, `-crf 18 -preset
  medium` fallback, validated subtitle languages.
- Multi-quality checklist downloads with resolution-suffixed filenames;
  on-disk collision disambiguation at download start.
- Responsiveness: all per-tick HTTP and settings reads on worker threads;
  closeEvent stops workers; animations run only while active.
- Animations: row fades, 100 ms progress interpolation, dialog fades,
  pill open/close opacity — all gated by `animations_enabled`.
- Extension: pill recorder upgraded — 4 capture modes with tab fallback,
  quality presets, floating indicator (timer/pause/resume), notifications,
  timestamped naming.
- Theming: three presets, accent picker, per-token overrides with live
  preview, JSON export/import; startup honors saved theme.
- Tests: `backend/tests/` (conftest + 6 files), `requirements.txt`.
- Defect fixes found by gates/sweeps: jobs snapshot JOBS_PATH NameError,
  JS-runtime cache init, watchdog fallback base, download-dest collisions.

## v3.3.x — see FIXES_NOTES.txt (61 items, all verified fixed herein).
