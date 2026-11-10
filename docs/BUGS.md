# BUGS.md — findings from the Task 1–4 pass

Items 1–5 were found during that pass and deliberately left unfixed at the
time (per instructions: report, don't auto-apply). A follow-up pass fixed
all five while waiting on the push — see the "Status" column and the
`v4.0.7` entry in `CHANGELOG.md` for what actually changed in each case.
Two additional, lower-priority ideas (6–7) came up during that same
follow-up, needed an explicit product decision rather than a silent pick,
and were resolved on request in v4.0.9 — see the bottom table.

| # | File | Severity | Confidence | Status | Original finding |
|---|---|---|---|---|---|
| 1 | `backend/downloader.py` — `_RecordingHandler._import_after_delay` | High | Certain | **Fixed in v4.0.7** | Auto-imported screen recordings were copied to disk and announced via `_fire_new_job_hooks({...})` with a bare synthetic dict — never passed through `new_job()`, so no entry was ever added to `JOBS`. The recording never appeared in `/jobs`, the GUI table, or `jobs.json`. Fixed by mirroring the `/upload` route's pattern: `new_job()` → `_repair_recording()` → `_probe_recording()` → `transition(job, JobEvent.COMPLETE)` (or `FAIL` if corrupt) → `maybe_convert_to_mp4()`. |
| 2 | `backend/api.py` (276, 531); `backend/downloader.py` (50, 629); `backend/jobs.py` (70, 91); `backend/server.py` (29) | Medium | Likely | **Fixed in v4.0.7** | `JOBS` is a plain dict mutated from many threads with no lock. Every listed call site iterated `JOBS.items()`/`JOBS.values()` directly instead of copying first — only `downloader.py`'s `dispatcher_loop` did it safely. A job added mid-iteration (e.g. via `/download` while the GUI's 1s `/jobs` poll is running) could raise `RuntimeError: dictionary changed size during iteration` — caught-and-logged in `jobs.py::save_jobs_snapshot` (silently skipped snapshot), unhandled (500) in `api.py`'s `/jobs`/`/remote`. Fixed by wrapping every listed site in `list(...)` before iterating. |
| 3 | `backend/api.py` (95) | Low–Medium (security) | Certain | **Fixed in v4.0.7** | The LAN remote-control key check used a plain `==` comparison — a timing side-channel in principle, unlike the API token check two lines below which correctly used `secrets.compare_digest`. Fixed to use `secrets.compare_digest(request.args.get("key", ""), REMOTE_KEY)`. |
| 4 | `backend/downloader.py` — `run_ytdlp` | Low | Certain | **Fixed in v4.0.7** | `opts` built for `engine.download()` never included `"job_id"`, so every yt-dlp diagnostic/traceback/cookie-file log line fell back to `dest.name` instead of the actual job id, making log correlation harder when filenames are similar. Fixed by adding `"job_id": job_id` to the `opts` dict. |
| 5 | `backend/tests/test_settings.py`, `backend/tests/test_jobs.py` vs implementation | Low (test/code drift) | Certain | **Fixed in v4.0.7** | Three test failures against the actual tree: `safe_filename` replaces illegal chars with `"_"` (by design) but the test expected them stripped to nothing; `category_for("song.mp3")` returns `"Music"` (matches `CATEGORY_MAP` and the README) but the test expected `"Audio"`; the test also referenced `settings.CATEGORIES`, which didn't exist anywhere in `settings.py` (only a duplicate list lived in `gui_qt.py`); `test_jobs.py` read a nonexistent `jm.JOBS_PATH` attribute on the dynamically-loaded `jobs` module. **Decision made:** fixed the tests to match the existing, README-consistent implementation rather than changing product behavior. Also de-duplicated `CATEGORIES`: it now lives once, as a tuple, in `settings.py`, and both `gui_qt.py` copies (a module-level list and a separate class-level tuple) were removed in favor of importing it. If `"Audio"`/stripped-filenames were actually what you wanted, that's a separate, deliberate product change to make on your own timeline — flag it and I'll do it. |

## Resolved (were "your call", now decided — v4.0.9)

| # | Idea | Decision made |
|---|---|---|
| 6 | Pin the Deno version in CI instead of tracking `releases/latest` | Pinned to `v2.9.6` (current stable as of this pass) via a `DENO_VERSION` job-level env var in `.github/workflows/build-exe.yml`. Reproducible builds now win by default; bump `DENO_VERSION` deliberately when you want Deno's latest YouTube-signature-challenge fixes rather than getting them for free on an unrelated push. See `QUESTIONS.md` #3. |
| 7 | Warn when a YouTube job proceeds with an *empty* cookie string | Went with a log line (not a toast, not a block): `engines.py::YtDlpEngine.download()` now logs a clear `WARNING` when the URL is a `youtube.com`/`youtu.be` host and no cookie string was available (`opts["cookie"]` and the `Cookie` header both empty), explaining that public videos are unaffected but age-restricted/private/member-only ones will fail. Non-blocking — public-video downloads proceed exactly as before. Covered by 4 new tests in `test_engines.py`. |

## Concerns verified correct (checked, not bugs)

- **Cookie pipeline (Task 1 chain):** `api.py`'s `/download`, `/show-add-dialog`, and `/batch` routes all correctly read `cookie` from the request body and pass it to `new_job()`; `jobs.py::new_job()` stores it as `job["cookie"]`; `downloader.py::run_ytdlp()` correctly builds `headers["Cookie"]` from it.
- **`extension/background.js::cookieHeaderFor()`** — correctly builds a `name=value; ...` string from `chrome.cookies.getAll()`; no bug.
- **Upload nonce handling (`api.py`)** — `_issue_upload_nonce`/`_consume_upload_nonce` are properly locked (`_UPLOAD_NONCE_LOCK`) and self-expire old nonces on every issue; no leak or reuse bug found.
- **`_run_generic_single` / `_download_segment` (`downloader.py`)** — both use `with requests.get(...) as r:` and `with open(...) as f:` correctly; no socket/file-handle leaks on early return (pause/stop) or exception.
- **Extension `innerHTML` usage (`content.js`, `popup.js`)** — every site either clears content (`= ""`) or assigns a static string; no untrusted data is interpolated into HTML. No XSS vector found.
- **`_job_dest` collision handling (`downloader.py`)** — already documented as a known sequential-only disambiguation (see `KNOWN_ISSUES.md`/`AUDIT.md` F6); re-verified, still accurate, not re-listed as new.
- **Screen-recording watcher's dead `FileSystemEventHandler` fallback (`downloader.py`)** — the `except ImportError` fallback class exists solely so the module doesn't `NameError` at import time when `watchdog` is absent; confirmed harmless and intentional, matches the comment.


- **Cookie pipeline (Task 1 chain):** `api.py`'s `/download`, `/show-add-dialog`, and `/batch` routes all correctly read `cookie` from the request body and pass it to `new_job()`; `jobs.py::new_job()` stores it as `job["cookie"]`; `downloader.py::run_ytdlp()` correctly builds `headers["Cookie"]` from it.
- **`extension/background.js::cookieHeaderFor()`** — correctly builds a `name=value; ...` string from `chrome.cookies.getAll()`; no bug.
- **Upload nonce handling (`api.py`)** — `_issue_upload_nonce`/`_consume_upload_nonce` are properly locked (`_UPLOAD_NONCE_LOCK`) and self-expire old nonces on every issue; no leak or reuse bug found.
- **`_run_generic_single` / `_download_segment` (`downloader.py`)** — both use `with requests.get(...) as r:` and `with open(...) as f:` correctly; no socket/file-handle leaks on early return (pause/stop) or exception.
- **Extension `innerHTML` usage (`content.js`, `popup.js`)** — every site either clears content (`= ""`) or assigns a static string; no untrusted data is interpolated into HTML. No XSS vector found.
- **`_job_dest` collision handling (`downloader.py`)** — already documented as a known sequential-only disambiguation (see `KNOWN_ISSUES.md`/`AUDIT.md` F6); re-verified, still accurate, not re-listed as new.
- **Screen-recording watcher's dead `FileSystemEventHandler` fallback (`downloader.py`)** — the `except ImportError` fallback class exists solely so the module doesn't `NameError` at import time when `watchdog` is absent; confirmed harmless and intentional, matches the comment.
