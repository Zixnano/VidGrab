# FIXES.md — v4.0.1 fix pass

## Fix 1 — background.js: intercepted downloads open the Add dialog
**File:** extension/background.js
**Change:** in the `chrome.downloads.onCreated` handler, `authedFetch("/download", …)`
→ `authedFetch("/show-add-dialog", …)`. Response shape differs
(`{ok, auto_queued, job_id}` vs `{job_id}`); the existing `handedOff = res.ok`
check is unchanged and works for both. The pill's `sendToBackend` path
(its own `/download` call) was NOT touched.
**Verify:** `node --check background.js` → PASS. Runtime: the three scenarios
(intercept+dialog, skip_add_dialog auto-queue, intercept off) require a
live browser — **DEFERRED — needs browser verification**.

## Fix 2 — gui_qt.py: ProgressDelegate.paint reads job fields directly
**File:** gui_qt.py
**Change:** removed the `re.search(r"(\d+)", display_string)` extraction;
added module-level `_progress_pct(job)` (conversion_progress while
converting; size_done/size_total with a zero-total guard otherwise) and
`paint()` now calls it with the job dict from `model.items[row]`. The
`converting` phase check is preserved.
**Verify:**
```
py_compile gui_qt.py: PASS
sweep gui_qt.py: unresolved = []
test_gui_progress::test_download_pct_from_sizes: PASS
test_gui_progress::test_converting_uses_conversion_progress: PASS
```

## Fix 3 — gui_qt.py: "Connected" uses SUCCESS (green), not ACCENT (cyan)
**File:** gui_qt.py
**Change:** added `SUCCESS = _PAL["success"]` beside the other palette
constants; `_on_refreshed` sets the status label to `SUCCESS`, and the
startup `QSS #StatusLabel` rule was switched too (same fix, the static
representation — without it the label shows cyan for ~1s at launch).
`ACCENT` is untouched for its real uses.
**Verify:**
```
py_compile gui_qt.py: PASS
grep: 'color: {SUCCESS}; padding-left: 10px;' present; ACCENT no longer
colors the status label (sweep clean).
```
Visual confirmation (green dot at startup): **DEFERRED — needs GUI**.

## Fix 4 — gui_qt.py: duplicate "Engines" tab registration
**File:** gui_qt.py (Settings dialog)
**Change:** deleted the duplicated `en = QWidget()` / `enl = QVBoxLayout(en)`
pair and the second `tabs.addTab(en, "Engines")` call. One tab remains.
**Verify:**
```
py_compile gui_qt.py: PASS
grep -c 'addTab(en, "Engines")' gui_qt.py → 1
```

## Fix 5 — content.js: recorder indicator in fullscreen (DEFERRED-verified)
**File:** extension/content.js (`makeRecIndicator`)
**Change (defensive — no browser available to confirm):** the indicator is
now appended to `document.fullscreenElement` when one exists (the
Fullscreen API top layer ignores ordinary-DOM z-index), with a
`fullscreenchange` listener that re-parents it on enter/exit; the listener
is removed in the recorder's `onstop` cleanup.
**Verify:** `node --check content.js` → PASS.
**Status: DEFERRED — needs browser verification** (per spec option 2; the
fix is harmless when no element is fullscreen).

## Fix 6 — backend/requirements.txt complete
**File:** backend/requirements.txt
**Change:** was `pytest>=7` only; now declares every runtime import:
flask>=3.0, flask-cors>=4.0, yt-dlp>=2024.8.6, requests>=2.31,
PySide6>=6.7, streamlink>=6.0, pytest>=7.
**Verify:** file contents reviewed against the tree's imports.
`pip install -r` in a fresh venv: **DEFERRED — no network in sandbox**.

## Question 1 — "mic+tab" vs tab-audio
**Answer: doc mismatch only — and the docs in this tree are already
correct.** Session R implemented four modes: element, tab (video+audio),
tab-audio (tab's own audio), screen — tab-audio was the deliberate third
mode per the Session R spec; no `getUserMedia`/mic path was ever in scope.
`ARCHITECTURE.md` says "4-mode recorder engine" and `CHANGELOG.md` says
"4 capture modes with tab fallback" — neither mentions "mic+tab" (grep
confirmed zero occurrences). No code or doc change required; if the
reviewer's tree showed "mic+tab", it was a different doc revision.

## Question 2 — jobs.json v4 migration
**Answer: real gap, fixed defensively (as instructed).** `load_jobs_snapshot`
did not backfill v4 job fields. While current readers mostly use `.get()`,
the spec's instruction was to add the migration regardless:
- `jobs.py` gained `_migrate_job(j)` — `setdefault` backfill of
  phase="idle", format_id/target_format/completed_ts/error=None,
  size_total/size_done=0, speed="", referer/cookie/user_agent=None,
  created_ts=now — called from `load_jobs_snapshot`; the status check in
  the repair path now uses `.get("status")`.
- New test `backend/tests/test_jobs_migration.py` (backfill unit + full
  snapshot-load upgrade).
**Verify:**
```
py_compile jobs.py + tests: PASS
test_jobs_migration::test_migrate_job_backfills_v4_fields: PASS
test_jobs_migration::test_load_snapshot_upgrades_v33_job: PASS
```

## SESSION_LOG entry — v4.0.1 fix pass
**Completed:** iteration 26. Six fixes applied (intercept dialog, progress
field read, SUCCESS status color, Engines tab dedup, fullscreen indicator
re-parenting, requirements completeness); Q1 answered (docs already
consistent — tab-audio intentional); Q2 fixed (jobs.json v4 migration +
test). All gates green: py_compile, node --check, AST sweep, four targeted
logic tests. DEFERRED: Fix 1 runtime (browser), Fix 3 visual (GUI),
Fix 5 live (browser), Fix 6 fresh-venv pip install (no network).

---

# v4.0.2 fix pass (audit findings applied)

## Q1 — Recorder notifications actually work now (AUDIT F1)
**Files:** extension/manifest.json, extension/content.js, extension/background.js
**Change:** added `"notifications"` to manifest permissions; `recNotify()`
now sends `{type: "SHOW_NOTIFICATION", title, message}` to the background
page, which calls `chrome.notifications.create()` (icon: icons/icon128.png).
**Why not the literal instruction:** content scripts cannot call
`chrome.notifications` — only background can — so a direct call would have
silently no-op'd exactly like the Web Notification API did. The message
route is the same intent, implemented the only way that functions.
**Verify:** node --check PASS; manifest valid (4.0.2, notifications
permitted); SHOW_NOTIFICATION sender + handler cross-checked; zero Web
Notification usage remains. Live display: DEFERRED (needs browser).

## Q2 — Dead code removed — PARTIALLY: audit F2 was wrong (corrected during gates)
**Removed (truly dead):** `SAVE_RECORDING` handler (zero senders anywhere);
`_YTDLP_JS_RUNTIME_CACHE = None` stranded global in downloader.py.
**RETAINED (audit F2 was incorrect):** `RELAY_BATCH` and `RELAY_DOWNLOAD`
handlers — the v4.0.2 verification gate found live senders my audit's
narrow sender regex had missed: popup.js sends RELAY_BATCH from both the
media-checklist send button and "Grab all media" (lines ~293, ~454), and
RELAY_DOWNLOAD is referenced from popup.js (~135) and content.js (~263,
~587). Deleting them would have broken the popup's core send features —
caught before shipping, handlers restored byte-identical.
**Verify:** node --check PASS; handler/sender matrix: every sent type has
a handler, every retained handler has a sender; SAVE_RECORDING and the
stranded global confirmed absent.

## Q3 — Deno stays on releases/latest
No change (decision recorded).

## F5 — WebM naming resolved
**File:** KNOWN_ISSUES.md — replaced the open R.7/R.8 question with:
"Recorder saves WebM by design — container matches MediaRecorder source.
Users who want MP4 can convert post-download via the existing conversion UI."

## Carried in this zip from the v4.0.1 CI fix
- backend/requirements.txt: pyinstaller>=6.0 restored.
- workflows/build-exe.yml AND .github/workflows/build-exe.yml: "Download
  and bundle Deno" step added; PyInstaller line exact (yt_dlp+PySide6+
  streamlink collect-alls, ffmpeg/ffprobe/deno add-binaries, no
  tkinterdnd2).

## F4 / F6 — confirmed no action (correctly assessed).

## SESSION_LOG entry — v4.0.2
**Completed:** iteration 27. Q1 applied (notifications via background-routed
chrome.notifications). Q2 corrected during verification: audit F2's
"RELAY_BATCH/RELAY_DOWNLOAD dead" claim was wrong — live senders found;
only SAVE_RECORDING + the downloader global removed. F5 closed. Gates:
node --check, py_compile, JSON validity, full sender/handler matrix — green.
Zip: VideoGrabber_v4.0.2.

## Process note
The audit pass (iteration 26) used sender-detection regexes that required
`{` immediately after `sendMessage(` — multi-line call sites escaped
detection. The v4.0.2 gate used a broader pattern and caught both live
senders. AUDIT.md F2 is corrected in this zip.

---

# v4.0.3 fix pass — frozen-runtime dependency completeness

## Task 1 — watchdog restored to requirements
`watchdog>=4.0` added (position: after streamlink, before pyinstaller, per spec).
**Honest code note:** in this tree the ONLY watchdog import is the guarded
one in downloader.py:225 (`try/except ImportError` + fallback base from the
earlier defect fix), so the v4.0.2 code would *launch* without the package
and only lose the recording auto-import feature. The crash as reported
(`server.py` line 38, hard ModuleNotFoundError) is not reproducible from
this tree's source — the guarded import logs "watchdog not installed —
screen-recording auto-import disabled" instead. Your CI build (commit
bca98d2) may carry local changes. The requirements/collect-all fix stands
regardless: the feature needs the package.

## Task 2 — PyInstaller collects watchdog
`.github/workflows/build-exe.yml` pyinstaller line now reads exactly:
```
pyinstaller --onedir --noconsole --name VideoGrabber --icon=VideoGrabber.ico --collect-all yt_dlp --collect-all PySide6 --collect-all streamlink --collect-all watchdog --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." --add-binary "deno.exe;." server.py
```

## Task 3 — requirements vs actual imports (authoritative: static AST scan)
Imports found across backend/*.py (non-stdlib, non-local):
```
api.py:        flask, flask_cors, requests, yt_dlp
downloader.py: requests, watchdog, yt_dlp   (watchdog guarded)
engines.py:    yt_dlp
gui_qt.py:     PySide6, requests, yt_dlp
settings.py:   requests
```
Resolution: flask→flask ✅ flask_cors→flask-cors ✅ requests→requests ✅
yt_dlp→yt-dlp ✅ PySide6→PySide6 ✅ watchdog→**watchdog>=4.0 (ADDED this
pass)**. `streamlink` is declared but not imported — intentional: it is
invoked as a CLI subprocess by engines.py, and the collect-all bundles its
plugins. No unresolvable import names → QUESTIONS.md has no new entries.

## Task 4 — collect-all audit
`--collect-all` present for: yt_dlp ✅ PySide6 ✅ streamlink ✅ watchdog ✅
(added this pass). flask/flask-cors/requests are plain packages with no
runtime submodule loading — correctly without collect-all. Each collected
package is in requirements ✅.

## Task 5 — stray workflows/ folder
Removed from this zip (was added in v4.0.1 to mirror disk; your disk copy is
already deleted). Only `.github/workflows/build-exe.yml` ships. Run
`git rm -r workflows/` on your side if git still tracks it.

## Task 6 — clean-venv import simulation: DEFERRED (no network in sandbox)
A true venv + `pip install -r` cannot run here (no network). What WAS run:
1. Static completeness proof (authoritative): after this pass, every
   non-stdlib import maps 1:1 to a declared requirement — complete by
   construction.
2. Sandbox-interpreter dynamic probe (NOT the clean venv; sandbox lacks
   most packages by design):
```
flask: FAIL ModuleNotFoundError   flask_cors: FAIL ModuleNotFoundError
requests: OK                      yt_dlp: FAIL ModuleNotFoundError
PySide6: FAIL ModuleNotFoundError watchdog: FAIL ModuleNotFoundError
streamlink: FAIL ModuleNotFoundError
```
   These failures are sandbox-environment artifacts, not requirements gaps.
RUN THIS ON YOUR MACHINE (or in CI) to satisfy the Task 6 gate:
```
python -m venv vgsim && vgsim\Scripts\activate
pip install -r backend/requirements.txt
cd backend && python -c "import server; print('OK: server.py imports cleanly')"
```

## Task 7 — verification
```
requirements.txt: watchdog>=4.0 ✅ pyinstaller>=6.0 ✅ streamlink>=6.0 ✅
pyinstaller line: --collect-all watchdog ✅ streamlink ✅ yt_dlp ✅ PySide6 ✅ | no tkinterdnd2 ✅
"Download and bundle Deno" step present above Build exe ✅
workflows/ (no dot): ABSENT from zip ✅
```

## Dependencies added this pass: watchdog>=4.0
## --collect-all flags added this pass: watchdog

## SESSION_LOG entry — v4.0.3
**Completed:** iteration 28. Restored watchdog to requirements (Task 1),
added --collect-all watchdog (Task 2), full requirements↔imports audit —
watchdog was the only gap (Task 3), collect-all audit clean (Task 4),
stray workflows/ removed from zip (Task 5), venv simulation DEFERRED with
honest sandbox probe + user-side commands (Task 6). Reported mismatch:
watchdog crash not reproducible from this tree (guarded import); fix stands
on feature-correctness grounds.
