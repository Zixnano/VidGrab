# QUESTIONS.md

1. **Recorder notifications (AUDIT F1):** `content.js` uses the page-level
   `Notification` API but the extension lacks the `notifications`
   permission — in practice nothing will show on most sites. Switch to
   `chrome.notifications` (add `"notifications"` to manifest permissions)?
   Or keep and document?

2. **Dead code (AUDIT F2/F3):** delete background.js `RELAY_BATCH`,
   `RELAY_DOWNLOAD`, `SAVE_RECORDING` handlers and the stranded
   `_YTDLP_JS_RUNTIME_CACHE` global in downloader.py?

3. **Deno version (CI):** the new workflow step downloads Deno
   `releases/latest`. Pin a specific version for reproducible builds?
   **Resolved in v4.0.9:** pinned to `v2.9.6` via a `DENO_VERSION` job-level
   env var in `.github/workflows/build-exe.yml` — bump that one value
   deliberately when you want a newer Deno (e.g. for a YouTube
   signature-challenge fix) instead of getting whatever's newest on an
   unrelated push.
