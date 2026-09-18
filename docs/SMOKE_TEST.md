# Video Grabber v4.0.0 — Smoke Test (13 steps)

1. Launch backend (`python server.py`) then GUI; status dot shows green.
2. Add a direct HTTP file URL — downloads at full speed, progress visible.
3. Add a YouTube URL — formats probe; select 1080p+720p+MP3 in the
   checklist → three jobs queue with three distinct filenames.
4. Pause / resume / stop / retry a job — states transition cleanly.
5. Queue a generic M3U8 (works) and an extractor-blob HLS (records).
6. Audio extraction: MP3 (`-q:a 0`), Opus, M4A, FLAC from the pill menu.
7. Right-click a finished job → Convert to mp4/mkv/mp3/wav/m4a/flac/opus.
8. Recorder: pill menu → all four modes; indicator shows timer,
   pause/resume works; notifications appear; file lands in the app.
9. Theme: switch presets, pick an accent, override tokens — preview and
   app update live; restart persists.
10. Export theme → Import theme → identical appearance (round-trip).
11. Settings changes persist across app restart (per-category dirs,
    speed limit, animations toggle — animations visibly turn off).
12. Clipboard monitor picks up a copied URL; tray icon hides/restores;
    right-click tray menu works.
13. `pytest tests/` passes (requires `pip install -r requirements.txt`).

Live-only items (cannot run headless): animation feel, 200-job scroll,
getDisplayMedia prompts, Notification permission, YouTube extraction.
