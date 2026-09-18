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
