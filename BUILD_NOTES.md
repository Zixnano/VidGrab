# Supervisor Architecture: delivery notes (2026-10-23, v8)

Current build: v8 (post-audit cleanup; the six fixes are listed in
the v8 section at the end of this file). Harness: 113/113 assertions
pass; supervisor, updater-flow and GUI smokes green. The version
sections below are kept as the historical record of each build.

Implements the v3 build contract in full. File map (repo destinations):

| File | Goes to | Role |
|---|---|---|
| backend/launcher.py | backend/ | Supervisor → VideoGrabber.exe |
| backend/app_host.py | backend/ | App host (former launcher) → VideoGrabberApp.exe |
| backend/recovery_ui.py | backend/ | Lifeline → recovery\recovery.exe (onedir) |
| backend/updater_ui.py | backend/ (ships inside app\) | Animated updater, run by the supervisor via the host |
| backend/gui_qt.py | backend/ | Current gui_qt + sentinel hooks + 2 menu actions |
| backend/api.py | backend/ | Current api + /health identity payload |
| backend/updater.bat | install root | Last-resort fallback (wait target = host exe) |
| backend/restore.bat | install root | Manual .old rollback (same updates) |
| backend/VideoGrabber.spec | backend/ | Supervisor build (replaces old spec — review) |
| backend/AppHost.spec | backend/ | Host build |
| backend/Recovery.spec | backend/ | Recovery build (bundles PySide6, onedir) |
| pipeline/build-exe.yml | .github/workflows/ | 3 builds, manifest gen, runtime zip, release assets |

## Verified in this environment
- All Python files compile.
- test_harness.py: **113/113 assertions pass** (incl. v3 sweep regressions:
  spawn failure judged as crash, zip-slip containment, restore
  version stamp) — state contract
  (round-trip, unknown-key preservation, runs[] pruning to 20), the
  judging table (ok / backend_failed / crash / legacy grace), the
  counter (increments only pre-sentinel, resets on success, third
  failure reaches the recovery threshold), launch-scoped sentinels,
  stale state-lock takeover, next-boot judging of an unjudged run,
  blacklist candidate selection (v87 blacklisted → v86; all blocked →
  no candidate; never downgrades), manifest build/verify, tamper
  detection, backup restore returning a hash-identical tree with the
  broken copy kept, and diagnostics redaction.
- Supervisor smoke (live, this machine): supervising a child whose
  server.py is missing judged the run `crash` and set crash_count = 1.
- GUI smoke (headless Qt, real gui_qt): first show wrote app_alive.tmp
  with the launch id, recorded last_paths.data_home in state, and the
  two new menu actions exist.

## Not verifiable here (Windows-only paths, code-reviewed instead)
- PyInstaller builds themselves (CI does them) and the spec swap in
  decision 2.
- ctypes process enumeration / QueryFullProcessImageNameW, Shift-key
  check, taskkill — all guarded to no-op or degrade off-Windows.
- The updater's live GitHub download/swap against the real repo.

## First-run checklist on Windows
1. Run test_harness.py against a throwaway tree.
2. Build via the updated workflow; confirm the setup zip contains
   recovery\ and both exes, the app zip contains neither runtime\ nor
   recovery\, and VideoGrabber-runtime-vN.zip exists on the release.
3. `--simulate=crash-before-sentinel` three times → Recovery opens.
4. Normal launch → crash_count back to 0, last_good_version set.

## v4 — the updater got its Discord moment (2026-10-23)
updater_ui.py's window is rebuilt around the app logo (green arrow,
white tray, dark tile — colours in the BRAND block at the top of the
file, sampled from the icon): the arrow bobs into the tray while work
runs, the tray pools green with download progress, and the arrow
lands home on success. Plus: a five-step checklist that ticks off in
brand green, an eased progress bar (indeterminate pulse during
check/extract/install), rotating quips during unmeasured phases, and
a live "MB of MB · MB/s · ~Ns left" stats line. Exit-code contract
and update logic unchanged; harness still 32/32.

## v5 — logo matched to the real icon (2026-10-23)
Nano supplied the actual icon files. The LogoMark and palette now use
the sampled master values — flat #4caf50 arrow (no gradient; the
two-tone read in v4 was blur in the screenshot), tray #e6edf3, tile
#0b1117, tray/arrow geometry measured from the 128px master.
Side-by-side render checked against the original.

## v6 — audit adjudication (2026-10-23)
An external audit raised 4 claims; checked line-by-line against the
shipped code. ONE was real: gui_qt.check_for_updates() (Tools menu,
startup prompt "now", update badge "Install now") still applied
updates via the legacy updater.apply_update — no blacklist, no
backup, no manifest verify. It now delegates to
_request_supervised_update() (update_pending + exit; the supervisor
runs the blacklist-aware updater). The other three claims were
refuted from the code: supervise()'s caller returns "relaunch"
itself after run_updater_flow(); verify_pkg compares sizes only and
the manifest is generated in CI from the very bytes that ship; the
supervisor exits right after Popen-ing recovery, so the mutex is
free when Recovery's Relaunch spawns VideoGrabber.exe.

## v7: audit fix pass (2026-10-23)
All 18 audit fixes implemented; see CHANGELOG.md for the per-fix
list and its test. Harness is 70/70 (32 original + 38 new).
vg_state.py is new and MUST land in app\ when you copy the backend
files (updater_ui and gui_qt import it); CI asserts its presence.

### Updated first-run checklist (Windows, exact commands)
Do the old checklist plus these, in order:
1. Build via push.bat / Actions. Confirm the run is green INCLUDING
   the new "integration" job (it exercises the built exes).
2. Fresh install from the setup zip into a folder WITH A SPACE in
   the name (e.g. "C:\VG test") to prove the quoting fix.
3. Launch normally once; close. (Seeds last_good_version.)
4. Live thin update: wait for the NEXT build (vN+1) to publish,
   then in the app use Tools > Updates > "Install update
   (supervised)...". Watch the updater window: the app swaps, the
   window closes, the host swap runs via updater.bat, the app
   relaunches on vN+1, and a "Update finished" dialog appears once
   on the new launch. Check install-version.txt says vN+1 and
   %LOCALAPPDATA%\VideoGrabber\backups holds the pre-update backup
   (now including VideoGrabberApp.exe and its internal folder).
5. Forced rollback: open Recovery (restart in Recovery mode from
   Tools > App), Restore Backup of the pre-update backup. Relaunch:
   the app is back on vN, and vN+1 is now on the blacklist
   (Recovery's blacklist row shows it) so it is not re-offered.
6. Triple crash: run, from the install folder,
   `VideoGrabber.exe --simulate=crash-before-sentinel` three times.
   Recovery opens after the third, and recovery_state.json shows
   the crashed version in "blacklist".
7. Duplicate host: with the app running, double-click
   VideoGrabberApp.exe in Explorer. It must exit immediately
   (launcher.log: "another HOST already holds the host lock").

## v8: post-audit cleanup (2026-10-23)
The senior audit declared v7 "8/10, shippable with the 4 patched
files" (those patches are the base this build starts from: no legacy
grace for a hung marker-less host, the host_swap state flag gating
the bat host swap, prerelease-then-promote releases, pass-2 harness
regressions). It listed 6 issues as "not fixed (small, ship
anyway)". All 6 are fixed here, with harness tests where a test is
possible (97/97):
1. A refused duplicate host exits 42 (app_host
   DUPLICATE_HOST_EXIT); the supervisor judges exit 42 as outcome
   "duplicate" and never increments crash_count for it.
2. Job Object assignment failure is fatal: the child is terminated
   and the supervisor exits (SupervisorFatal), instead of logging
   and running an uncontrolled child.
3. Documentation only: see "Residual risks (accepted)" below.
4. vg_state gains SCHEMA_VERSION = 1 and a schema gate: the
   supervisor and the updater refuse to run when the on-disk
   state's schema_version is newer than theirs.
5. updater.bat and restore.bat ride in the thin zip; the updater
   copies them to the install root during the swap (option (a); a
   running bat cannot overwrite itself, so self-copy was rejected).
6. These notes: stale harness counts updated (32/32 was v3-era),
   this section added; the stale "already swapped and stamped"
   comment in launcher.py corrected (the stamp is deferred while a
   host swap is pending).

## Residual risks (accepted)
- SHA256SUMS proves integrity, not authenticity. The sums file is
  published in the SAME release as the zips it describes, so it
  reliably detects corruption, truncation and mirror damage, and
  that is all it detects. Anyone who can publish or alter a release
  (a compromised account, token or pipeline) can alter the sums to
  match a malicious zip. Real authenticity needs signing: an
  Authenticode signature on the exes and/or a sums file signed with
  a key held outside the release pipeline, verified before install.
  That is NOT implemented. The risk is accepted knowingly: the
  threat it leaves open is a compromised release channel, not a
  corrupted download.
- The Windows integration job and the Win32-only surfaces (mutexes,
  Job Object, PyInstaller builds, the bat's PowerShell check) are
  verified on the windows-latest CI job and the first-run checklist,
  not from the Linux build VM.

## v9: deep scan fixes (2026-10-23)
A final line-by-line scan of the shipped v8 set found 13 defects,
all fixed, harness 113/113 (16 new checks):
1. vg_state: an empty or unparseable (pid 0) state lock was never
   treated as stale, so one killed-mid-create lock made every
   writer burn its timeout and proceed unlocked, forever. A pid-0
   lock older than 5 s is now taken over like any stale lock.
2. vg_state: mutate_state is now serialised per process with an
   RLock; the file lock alone let GUI threads interleave
   load/modify/save and lose keys.
3. launcher: the child's exit code is persisted into the run record
   the moment it is seen, and next-boot judging uses it (an
   unjudged exit-42 run is a duplicate, not a crash) and reads the
   marker FILES for the run's launch id (the poll loop may have
   died before persisting markers_seen for a healthy run).
4. launcher: a schema-blocked updater no longer falls back to
   updater.bat (the bat would swap files against a state every
   component refuses to touch); the failure is reported instead.
5. launcher: the "nothing staged" updater failure now sets an
   update result like every other failure path.
6. updater_ui: the window-close guard lost in the animated rewrite
   is restored: closing mid-update is refused, so a stray close
   cannot kill the worker mid-swap and report success (exit 0).
7. updater_ui: backup pruning now matches app_*, so app_unknown
   backups no longer accumulate forever.
8. recovery_ui: refuses to start on a newer-schema state, like the
   supervisor and updater.
9. recovery_ui: diagnosis labels a "duplicate" run correctly
   instead of "never judged".
10. recovery_ui: restore creates the data home before copying
    settings back (a fresh home no longer fails mid-restore).
11. recovery_ui: rollback picks the shallowest app\server.py in
    the package, never a nested copy.
12. updater.bat: rollback removes a copied exe/internal folder when
    no .old existed, instead of leaving half of the new host behind.
13. restore.bat: failed exe/internal renames abort with a message
    instead of relaunching a mixed install.
The bat changes (12, 13) are text-verified here; their execution
proof is CI step (b) on windows-latest, as before.
