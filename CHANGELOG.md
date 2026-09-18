# Changelog

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
