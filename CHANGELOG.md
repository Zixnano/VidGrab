# CHANGELOG: audit fix pass (v7, 2026-10-23)

Every numbered fix from the audit, what changed, and how to test it on
its own. Harness tests live in test_harness.py (run:
`python test_harness.py`, needs PySide6 importable). Windows-only
behaviour is covered by the CI integration job in
pipeline/build-exe.yml and the first-run checklist in BUILD_NOTES.md.

## Phase 1: ship blockers

1. **Specs set contents_directory** (VideoGrabber_internal,
   VideoGrabberApp_internal, VideoGrabberRecovery_internal). CI now
   asserts all three folders exist right after the builds and the
   thin check throws if one is missing.
   Test: CI "Assert PyInstaller internal folders exist" step; locally,
   grep `contents_directory` in the three .spec files.
2. **updater.bat quoting**: the supervisor builds one pre-quoted
   string (`cmd /s /c ""bat" "root" "pkg" "tag""`) via
   `updater_bat_cmdline()` instead of a subprocess list that cmd
   mangled on paths with spaces.
   Test: harness "bat cmdline wraps the whole command in one outer
   quote pair" (exact string, path with spaces) and the host-mode
   variant.
3. **Updater swaps app only**: `swap_from_pkg` copies app\ out of the
   pkg (pkg stays whole) and never touches the host exe or internal
   folder it is running inside. Host files stay staged; the
   supervisor runs updater.bat in "host" mode after the updater host
   exits. The bat copies (never moves or deletes the pkg), so a
   failed rollback cannot destroy the staged files. The version
   stamp is deferred until the host swap lands.
   Test: harness "swap_from_pkg swaps app only and reports host
   pending", "leaves the running host untouched", "copies: the pkg
   stays whole", "version stamp deferred"; CI step (b).

## Phase 2: high

4. **Host mutex + Job Object**: the host always takes its own
   Local\VideoGrabber_Host_v1 except in smoke and updater guest
   modes (`host_lock_required`). The supervisor assigns the host
   (and the updater host) to a Windows Job Object with
   JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
   Test: harness host-lock matrix (4 checks); CI steps (c) and (d).
5. **Backups and restore cover the host**: `make_backup`,
   `snapshot_app`, and the new component-wise `swap_install` include
   VideoGrabberApp.exe and VideoGrabberApp_internal; restore still
   stamps install-version.txt.
   Test: harness "backup includes the host exe and internal folder",
   "restore brings back app AND host".
6. **Auto-blacklist**: at crash_count >= 3 the supervisor blacklists
   current_version (unless it is the last known good) before Recovery
   opens; Recovery restore/rollback blacklist the version they
   replaced. Manual buttons unchanged.
   Test: harness `should_auto_blacklist` matrix (4 checks); CI step
   (a) asserts the blacklist in state after 3 simulated crashes.
7. **Judging window**: FIRST_CHECK_SECONDS is 130 (the GUI health
   watch allows 120). A pure `judge_step` never latches
   backend_failed while the child is alive before the 300 s hard cap;
   a late startup_ok upgrades the run to ok.
   Test: harness judge_step matrix (6 checks).
8. **Relaunch cap resets**: main() zeroes the consecutive-relaunch
   counter whenever the last run settled ok or legacy_ok, so a tray
   session can take more than 3 updates in its lifetime.
   Test: harness "main() allows more than 3 lifetime update
   relaunches when runs settle ok" (scripted supervise, 5 calls).
9. **updater.bat waitloop cap**: about 30 tries then exit /b 1, and
   the wait matches the host by full image path under the install
   root via PowerShell, with the name-only tasklist check as
   fallback.
   Test: read the :waitloop block; CI step (b) runs the bat end to
   end on a path with spaces.

## Phase 3: hardening

10. **vg_state.py**: state dir, StateLock, load/save/mutate,
    pid_alive, recovery log and update-result helpers now exist in
    exactly one stdlib-only module, imported by launcher.py,
    updater_ui.py, recovery_ui.py and gui_qt.py. Stale-lock takeover
    is by atomic rename with content re-verification, never by
    unlinking the live path.
    Test: harness module-identity check, stale-takeover check,
    live-lock refusal, and 2 processes x 50 locked increments = 100.
11. **Download integrity**: CI publishes SHA256SUMS-vN.txt for every
    zip. The updater downloads to .part, renames on completion,
    checks bytes received == asset size, verifies the zip against
    SHA256SUMS, and verify_pkg now compares SHA-256 as well as size.
    Recovery verifies its rollback and runtime downloads the same
    way. Releases older than the sums asset fall back to size plus
    manifest checks, logged.
    Test: harness verify_download accept/reject-short/reject-hash and
    verify_pkg same-size SHA mismatch (5 checks).
12. **Blocking wait**: once a run settles ok or legacy_ok, supervise()
    blocks on child.wait() instead of polling every 0.25 s.
    Test: code path in supervise(); the supervisor smoke still judges
    a dead child correctly.
13. **Update result surfaced in-app**: the updater and supervisor
    write state["last_update_result"]; the GUI pops it (read and
    clear in one locked pass) about 1.5 s after first show and
    displays it once.
    Test: harness update-result round trip; the GUI smoke constructs
    the window and confirms the hook exists.
14. **Pruning and log rotation**: app_broken-*, runtime_broken-*,
    host broken copies and app_snapshot-* are pruned to the newest 2;
    recovery.log rotates at 512 KB with one .1 kept (the rotation
    lives in vg_state.recovery_log, shared by both writers).
    Test: harness pruning and rotation checks.
15. **Recovery messages**: Relaunch checks the supervisor mutex and
    explains instead of spawning a supervisor that exits silently;
    Rollback with no last known good version explains why, and the
    button stays enabled so the click can say so.
    Test: code paths in recovery_ui (_supervisor_mutex_held,
    _rollback); Windows-only mutex probe, covered by inspection and
    the first-run checklist.
16. **Cleanup**: restore.bat defaults ROOT to its own folder (%~dp0),
    no hardcoded user path; the dead _on_update_result slot and its
    signal are removed from gui_qt.py; AUTO_LAUNCH is gone from
    updater.bat; the stale CI comment above the runtime zip is fixed;
    the updater's disk-space check measures the state drive
    (download, extraction, backup) and the install drive (swap)
    separately.
    Test: grep the delivered files; harness still green.

## Phase 4: verification

17. **Harness**: 32 existing assertions kept; 38 added across the
    fixes above. Total 70/70.
18. **Windows CI integration job**: runs the built exes on
    windows-latest: (a) triple crash simulation opens Recovery and
    auto-blacklists; (b) bat full swap and restore.bat rollback on a
    path with spaces; (c) a second host launch is refused; (d)
    killing the supervisor kills the host via the Job Object.

## Residual risks (honest list)

- The integration job is new and has never run; expect to tune it
  once against its first real run (process timing on the runner is
  the usual suspect). Treat a red job as do-not-ship until read.
- Job Object assignment, both mutexes, the PowerShell path check in
  the bat, and the PyInstaller builds themselves cannot be executed
  from the Linux build VM; they are covered by the CI job and the
  Windows checklist, not by local runs.
- gui_qt.py still contains em dashes in its pre-existing UI strings
  (the app's own text, not added by this pass); the files authored
  for the Supervisor Architecture are em-dash free.
- SHA256SUMS verification falls back to size plus manifest checks
  for releases published before the sums asset existed; that is
  deliberate (Recovery must still be able to fetch old releases).
- The internal manifest generator still exists in two places (the
  workflow step and recovery.build_manifest); they hash identical
  inputs the same way, but a single shared script would be tidier.

## Post-audit cleanup (v8, 2026-10-23)
Built on the audit's 4 patched files (85/85 baseline). Six fixes:
(1) duplicate host exits 42 and is judged "duplicate", never
counted; (2) Job Object assignment failure terminates the child and
exits the supervisor; (3) BUILD_NOTES gains a "Residual risks
(accepted)" section stating SHA256SUMS is integrity, not
authenticity, and signing is not implemented; (4) vg_state
SCHEMA_VERSION = 1 with a refusal gate in the supervisor and the
updater; (5) the bats ride in the thin zip and the updater installs
them during the swap; (6) stale BUILD_NOTES counts and the stale
launcher.py stamp comment corrected. Harness: 97/97 (12 new checks
across fixes 1, 2, 4 and 5; fixes 3 and 6 are documentation and
have no executable test). One-line tests: fix 1, run the harness
and read the "duplicate" checks; fix 2, the SupervisorFatal checks;
fix 4, the schema gate checks; fix 5, the "fresh bats" check.

## Deep scan fixes (v9, 2026-10-23)
Thirteen defects from a final full read of the v8 set: pid-0 state
locks never went stale; mutate_state had no per-process thread
serialisation; unjudged runs ignored recorded exit codes and the
marker files; a schema-blocked updater still fell back to the bat;
one updater failure path told the user nothing; the updater window
could be closed mid-swap and report success; app_unknown backups
were never pruned; Recovery had no schema gate; diagnosis mislabelled
duplicate runs; restore could fail on a missing data home; rollback
could pick a nested server.py; both bats had rollback holes (copied
components left behind, unchecked renames before relaunch). Harness
113/113. Full list in BUILD_NOTES, v9 section.
