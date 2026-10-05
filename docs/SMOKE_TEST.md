# Video Grabber v5 Smoke Test

Run `pytest tests/` first (44 tests), then the live checks below. Steps 1-13
are the original v4 list; 14-30 cover v5.

## Core (v4)
1. Launch backend (`python server.py`) then GUI; status dot shows green.
2. Add a direct HTTP file URL; downloads at full speed, progress visible.
3. Add a YouTube URL; formats probe; select 1080p+720p+MP3 in the checklist;
   three jobs queue with three distinct filenames.
4. Pause / resume / stop / retry a job; states transition cleanly.
5. Queue a generic M3U8 (works) and an extractor-blob HLS (records).
6. Audio extraction: MP3, Opus, M4A, FLAC from the pill menu.
7. Right-click a finished job > Convert to mp4/mkv/mp3/wav/m4a/flac/opus.
8. Recorder: pill menu > all four modes; indicator shows timer; file lands in the app.
9. Theme: switch presets, pick an accent, override tokens; restart persists.
10. Export theme > Import theme; identical appearance.
11. Settings persist across restart (category dirs, speed limit, animations).
12. Clipboard monitor, tray hide/restore, tray menu.
13. `pytest tests/` passes.

## Throttle, retry, holds (v5)
14. Queue 3 YouTube videos at once. Log shows `pacing: waiting Ns` for the 2nd
    and 3rd; the 1st starts immediately. A non-YouTube file starts at once.
15. Stop a job while it is waiting on `pacing:`; it cancels.
16. Unplug the network mid-download. Log shows `network error, auto-retry 1/3
    in 15s`; the row says "retrying in ..."; it resumes when the network is back.
17. Bot check (if it happens): status bar shows "YouTube held N min", other
    sites keep downloading, no auto-retry. Switch VPN, press Resume on the
    YouTube job; it starts immediately and the hold message disappears.
18. Settings > Connection: set delay 0 to 0 (off), save, queue 2 YouTube
    videos; no `pacing:` lines. Set a bad proxy ("abc"); save is refused.

## Names, sources, archive (v5)
19. Add a YouTube URL from the pill with a junk page title; the finished file
    is named after the real video title, with no "(2) (2)" stacking.
20. The Source column shows the site with its favicon (a letter tile until the
    extension has sent the icon once).
21. Queue a whole playlist, finish it, queue it again via the picker: finished
    items show "already downloaded" and start unchecked.
22. Add the same URL twice: the Add dialog shows a "second copy" warning.

## GUI (v5)
23. Open Add dialog from the browser, then minimize the main window. The
    dialog goes with it and comes back with it.
24. Sidebar shows counts; Active and Failed filters work; an empty filter
    shows a message instead of a blank table.
25. Right-click a queued job > Queue position > Move to top; Alt+Up/Down
    move it. The next job to start follows the new order.
26. Settings: search "proxy" jumps to Connection; every page opens; Updates
    page shows yt-dlp and app versions; Save persists.
27. A failed job older than 24 h disappears from All but stays under Failed;
    Tools > Clear failed downloads removes them.
28. Finish a batch of 3 downloads; one tray notification (not three).
29. Window narrower than 1240 px: toolbar shows icons only; columns resize
    and remember their widths after a restart.
30. Low disk (set "Pause new downloads below" very high): status bar warns
    and no new job starts; set it back and they resume.

## Extension (v5)
31. Reload the extension (new permissions: contextMenus, favicon). Popup shows
    paired state, counts, and Recent downloads with live progress.
32. Filter panel is open, ticks apply instantly, choices persist, tiny files
    are hidden, the Filter button works on the first click.
33. Right-click a link > "Download with Video Grabber".
34. Popup > "Hide pill on this site": the pill disappears at once on that site;
    "Show pill" brings it back.
35. Pick a quality from the pill; next time on that site it is listed first
    with a star.
36. Interception on: download a .zip; the service-worker console shows
    `[VG] intercept: browser lag ...ms, handoff ...ms`. Turn off IDM's browser
    extension first or it may win the race.

## Updates
37. Settings > Updates > Check yt-dlp now, then Refresh: shows the staged
    version; restart applies it; Roll back returns to the bundled one.
38. With a newer GitHub release published, the status bar badge appears within
    the check interval; Skip this version hides it.

Live-only (cannot run headless): animation feel, 200-job scroll, Windows
window flipping, getDisplayMedia prompts, YouTube extraction.
