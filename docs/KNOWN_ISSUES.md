# Known Issues (v4.0.0)

All v3.3.x items verified fixed. Live-verification items (no GUI/network
in the build sandbox — run SMOKE_TEST.md on a real machine):
- 200-job scroll smoothness, animation feel, clean exit under load.
- Recorder: getDisplayMedia prompts, DRM element-capture fallback
  notification, audio-only tab capture, screen mode with audio.
- YouTube format probing with live extractor + JS runtime (Deno/Node).
- `pytest tests/` execution (suite compiles; run where pytest exists).
- Recorder saves WebM by design — container matches MediaRecorder source.
  Users who want MP4 can convert post-download via the existing conversion UI.
- `_job_dest` disambiguates at download start; simultaneous same-name
  starts can still race (registry reservation noted as future work).
- downloader.py retains one stranded dead global (`_YTDLP_JS_RUNTIME_CACHE`
  init at module level — its function moved to engines.py in Session 7).

## v4.0.1
- Intercepted downloads now open the Add dialog (Fix 1); runtime check DEFERRED.
- Recorder indicator re-parents into fullscreen element (Fix 5); live check DEFERRED.
- jobs.json v4 migration added; v3.3 snapshots upgrade on load (Q2).
