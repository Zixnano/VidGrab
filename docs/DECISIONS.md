# DECISIONS.md — Supervisor Architecture implementation

The contract (v3) was implemented as specified. Where the contract met
the real install and a literal reading was impossible, the most
reasonable choice was made and is recorded here, per the contract's own
instruction.

1. **There is no `runtime\python.exe`.** The contract's child process
   ("runtime\python.exe app\server.py") doesn't exist in this install:
   the interpreter is the PyInstaller host exe and `runtime\` holds
   site-packages + ffmpeg/deno. Realization: the supervisor spawns
   **VideoGrabberApp.exe**, which runs `app\server.py` in-process —
   exactly what the old single-exe launcher did. No behavior changes
   for the app itself.

2. **File naming.** Per the contract, `launcher.py` is the supervisor.
   The former launcher.py (mutex, self-spawn guard, DLL setup, runpy)
   is preserved almost verbatim as **`app_host.py`**, frozen by the new
   `AppHost.spec` into VideoGrabberApp.exe. `VideoGrabber.spec` now
   builds the supervisor (the old spec built the host under that name;
   it is replaced — review before committing). `Recovery.spec` is new.

3. **The mutex moved up one level.** The supervisor holds the existing
   single-instance mutex (`Local\VideoGrabber_SingleInstance_v1`). The
   host skips its own mutex when `VG_SUPERVISED=1` — otherwise it would
   refuse to start under its own supervisor. Unsupervised (dev) runs
   behave exactly as before.

4. **Smoke test.** `python.exe -I -c "import …"` is impossible (see 1),
   so the host gained a `VG_SMOKE_TEST=1` mode: after its own path/DLL
   setup it imports `PySide6.QtWidgets, flask, yt_dlp` and prints
   `SMOKE_OK`. Recovery runs the host exe with that env — which tests
   the *real* runtime resolution path, arguably better than the
   contract's version.

5. **Updater interpreter.** `updater_ui.py` is run by the host
   (`VG_ENTRY_SCRIPT=<staging copy>`), the only interpreter the install
   has. The supervisor stages the copy, watches the exit code, and
   falls back to `updater.bat` — unchanged contract semantics.

6. **The supervisor is never thin-updated.** An early draft staged
   supervisor files for a self-swap; the self-audit withdrew it —
   contract §1 puts the supervisor on the full-installer-only list,
   exactly like Recovery. Thin zips contain host + app only, the
   updater doesn't touch supervisor files, and `updater.bat`'s job is
   the host swap. `updater.bat`'s wait target is now
   **VideoGrabberApp.exe** (the supervisor is alive and is the caller —
   the old target would have waited forever). `restore.bat` got the
   same wait-target and file-name updates; it still relaunches
   VideoGrabber.exe (the supervisor), which is now correct.

7. **Critical runtime files** (diagnosis step 3) are the real layout's:
   `runtime\site-packages\{PySide6, flask, yt_dlp}\__init__.py`,
   `runtime\ffmpeg.exe`, `runtime\deno.exe`, and both exes at the root.

8. **`current_version` added to the state file.** The contract schema
   didn't name a field for the installed tag, and three components
   need it. It is written from `install-version.txt` by the supervisor
   and updated by the updater after a swap. `last_good_version` is set
   by the supervisor when a run judges `ok`. (The contract's
   preserve-unknown-keys rule makes the addition safe for older
   readers, including the installer-frozen Recovery.)

9. **Update trigger in gui_qt.** `updater.py` (the app's existing
   update module) was not among the supplied files, so its internals
   were not rewired. gui_qt gained Tools > Updates → "Install update
   (supervised)…", which writes `update_pending` and quits — and the
   supervisor honors that flag from *any* writer, so `updater.py` can
   adopt the same one-line trigger later. Existing check/skip flows
   are untouched.

10. **Simulations live in app_host.py**, the process that wraps the
    app: `crash-before-sentinel` (exit 3 before anything runs),
    `missing-module` (raises ModuleNotFoundError in-process),
    `port-occupied` (the host holds a dummy listener in the *same*
    process, so the app's bind genuinely fails and the run judges as
    backend_failed). `corrupt-settings` backs up settings.json,
    corrupts it, and restores the original in a `finally` — the
    contract's copy-redirection would need settings.py cooperation;
    transient corruption with guaranteed restore was chosen instead.

11. **Ghost PID lookup** uses `netstat -ano` for the PID (a system
    tool, the same idiom as updater.bat's `tasklist`) and ctypes
    `QueryFullProcessImageNameW` for the image path, keeping
    Recovery's imports stdlib + PySide6 as the contract requires.

12. **push.bat was not supplied.** All pipeline changes are in
    `build-exe.yml`. If push.bat performs its own local staging, it
    needs the same exclusions (launcher.py / app_host.py /
    recovery_ui.py stay out of `app\`).

13. **sys.executable changed identity.** App code now runs with
    sys.executable = VideoGrabberApp.exe instead of VideoGrabber.exe.
    The host's self-spawn guard is self-relative, so it keeps working.
    Anything in app code that respawns "VideoGrabber.exe" by name now
    reaches the *supervisor* — which is the desired restart path under
    this architecture — but hardcoded references are worth a grep when
    this lands.

## Self-audit fixes (v2, before first ship)

A hostile re-read of v1 found and fixed:
- **Critical:** the supervisor holds the single-instance mutex, but the
  updater's host process wasn't told it was supervised — it would have
  exited 0 on the mutex and every supervised update would have
  silently no-opped while reporting success. The supervisor now sets
  VG_SUPERVISED=1 for the updater spawn, and the host's smoke/entry
  guest modes never take the mutex at all.
- Smoke-test failures could park on a `_fatal` MessageBox until
  Recovery's 30s timeout; they now return 1 with the error on stderr.
- gui_qt's state writes now run under the contract PID lock (v1 used a
  bare atomic replace; a race with the supervisor at startup could
  have lost a crash_count reset).
- Recovery's app_broken-<timestamp> folder could collide on two
  restores inside one second; it now suffixes until unique.
- The .supervisor_new self-swap mechanism (see 6) was withdrawn.

## Advanced bug sweep (v3)

A second, deeper pass (static analysis + adversarial probes) found:
- **Spawn failure crashed the supervisor.** If the host exe was
  missing/unstartable, supervise() died with a traceback and the run
  was only judged retroactively at next boot. It is now judged a
  startup crash on the spot (harness regression test included).
- **Restore forgot the version stamp.** Restoring backup v84 left
  install-version.txt claiming the newer broken tag, so the updater
  and supervisor reasoned from a version that wasn't on disk. Restore
  now stamps the backup's tag (release backups only).
- **GitHub API errors masqueraded as "up to date".** fetch_releases
  swallowed the API's error dicts (e.g. rate limit) into an empty
  list. It now raises, so the update fails visibly and the
  supervisor's fallback path engages honestly.
- **Zip-slip hardening.** All three release-zip extractions (updater,
  rollback, runtime repair) now use the `data` filter, refusing
  entries that would escape the target folder.
- **Wall-clock judging deadline.** The 60-second window used
  time.time(); an NTP jump mid-watch could stretch or shrink it.
  Now time.monotonic().
- **Updater window could be closed mid-download**, killing the worker
  thread with the widget. closeEvent is ignored while work runs.
- Verified clean by probe: the state lock under real two-process
  contention (2 x 50 locked increments = exactly 100, no lost
  updates); pyflakes over all delivered files.

## Addendum (v7 audit pass, 2026-10-23)
- Host files in a thin pkg are swapped by updater.bat in "host" mode
  after the updater host exits; updater_ui swaps app\ only, by copy.
  The bat also copies (never moves), so the staged pkg survives any
  rollback.
- SHA256SUMS-<tag>.txt is a release asset from CI. Downloads verify
  against it when present; older releases without it fall back to
  size + manifest checks (logged), so Recovery can still fetch them.
- State code is a single module, vg_state.py, bundled into the
  supervisor and Recovery builds and shipped in app\ for the updater
  and the GUI.
- The judging window grew past the GUI's 120s health watch (130s
  first check, 300s hard cap while the child lives), and supervise()
  blocks on the child once a run settles.
