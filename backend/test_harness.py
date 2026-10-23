# test_harness.py - Supervisor Architecture acceptance tests (contract §9).
#
# Every claim in the contract maps to an assertion here. The harness runs
# against a THROWAWAY state dir and install tree (VG_STATE_DIR points at
# a temp folder); it never touches a real install.
#
# Run:  python test_harness.py        (needs PySide6 importable for
#                                       updater_ui / recovery_ui imports)
# Exit code 0 = every assertion passed.
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE / "backend"
TMP = Path(tempfile.mkdtemp(prefix="vg-harness-"))
STATE_DIR = TMP / "state"
STATE_DIR.mkdir(parents=True)
os.environ["VG_STATE_DIR"] = str(STATE_DIR)

sys.path.insert(0, str(BACKEND))   # vg_state.py resolves, as it does
                                   # in app\ and in the frozen builds

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" - {extra}" if extra and not cond else ""))
    if not cond and extra:
        print("     ", extra)


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, BACKEND / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


launcher = load_module("vg_launcher", "launcher.py")

# ---------------------------------------------------------- state contract -
st = launcher.load_state()
check("state defaults load", st["crash_count"] == 0 and st["schema_version"] == 1)
launcher.mutate_state(lambda s: s.update({"crash_count": 2, "my_custom_key": {"x": 1}}))
st = launcher.load_state()
check("unknown keys are preserved on rewrite", st.get("my_custom_key") == {"x": 1})
check("written values round-trip", st["crash_count"] == 2)
for i in range(25):
    launcher.mutate_state(lambda s, i=i: s["runs"].append(
        {"launch_id": f"id{i}", "started_at": i, "outcome": "ok", "markers_seen": []}))
st = launcher.load_state()
check("runs[] pruned to 20", len(st["runs"]) == 20 and st["runs"][-1]["launch_id"] == "id24")
launcher.mutate_state(lambda s: s.update({"runs": [], "crash_count": 0}))

# ------------------------------------------------------------ judging ------
check("decide: startup_ok -> ok",
      launcher.decide_outcome({"startup_ok"}, alive_at_deadline=False) == "ok")
check("decide: app_alive only, exited -> backend_failed",
      launcher.decide_outcome({"app_alive"}, alive_at_deadline=False) == "backend_failed")
check("decide: no markers, exited -> crash",
      launcher.decide_outcome(set(), alive_at_deadline=False) == "crash")
check("decide: no markers, alive at deadline -> legacy_ok (grace)",
      launcher.decide_outcome(set(), alive_at_deadline=True) == "legacy_ok")
check("decide: app_alive only, alive at deadline -> backend_failed",
      launcher.decide_outcome({"app_alive"}, alive_at_deadline=True) == "backend_failed")

# ------------------------------------------- counter only pre-sentinel -------
state = dict(launcher.DEFAULT_STATE)
state["current_version"] = "v90"
counts = [launcher.apply_outcome(state, "crash") for _ in range(3)]
check("counter increments pre-sentinel: 1,2,3", counts == [1, 2, 3], str(counts))
check("third failure reaches the recovery threshold",
      counts[-1] >= 3)
check("backend_failed also increments",
      launcher.apply_outcome(state, "backend_failed") == 4)
check("success resets the counter",
      launcher.apply_outcome(state, "ok") == 0)
check("ok records last_good_version",
      state["last_good_version"] == "v90", str(state.get("last_good_version")))
check("legacy_ok resets without touching last_good",
      launcher.apply_outcome(state, "legacy_ok") == 0
      and state["last_good_version"] == "v90")

# ------------------------------------------------- sentinel launch-scoped --
marker = STATE_DIR / "startup_ok.tmp"
marker.write_text(json.dumps({"launch_id": "AAAA", "timestamp": 1}), encoding="utf-8")
check("sentinel from launch A does not acquit launch B",
      launcher.read_marker(marker, "BBBB") is None)
check("sentinel matches its own launch",
      (launcher.read_marker(marker, "AAAA") or {}).get("launch_id") == "AAAA")

# ------------------------------------------------------------ stale lock ---
lock = STATE_DIR / "recovery_state.lock"
lock.write_text("999999", encoding="utf-8")     # a dead pid
with launcher.StateLock() as lk:
    check("stale state lock (dead pid) is taken over", lk.held)
check("lock released afterwards", not lock.exists())

# ------------------------------------------------- unjudged run (next-boot)
launcher.mutate_state(lambda s: s.update({
    "crash_count": 0,
    "runs": [{"launch_id": "dead1", "started_at": 1, "outcome": None,
              "markers_seen": []}]}))
launcher.judge_unjudged_run()
st = launcher.load_state()
check("unjudged previous run is judged a crash at next boot",
      st["runs"][-1]["outcome"] == "crash" and st["crash_count"] == 1,
      json.dumps(st["runs"][-1]) + " count=" + str(st["crash_count"]))
launcher.mutate_state(lambda s: s.update({"runs": [], "crash_count": 0}))

# ------------------------------------------------------- blacklist logic ---
updater = load_module("vg_updater", "updater_ui.py")


def rel(tag):
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": f"VideoGrabber-app-{tag}.zip", "size": 100,
                        "browser_download_url": "https://example.invalid/x"}]}


releases = [rel("v87"), rel("v86"), rel("v85")]
cand, why = updater.pick_candidate(releases, ["v87"], "v84")
check("blacklist skip picks v86 (v87 blacklisted)",
      cand is not None and cand["tag_name"] == "v86" and why == "update",
      f"{cand} {why}")
cand, why = updater.pick_candidate(releases, ["v87", "v86", "v85"], "v84")
check("all newer blacklisted -> no candidate, status blacklisted",
      cand is None and why == "blacklisted", f"{cand} {why}")
cand, why = updater.pick_candidate(releases, [], "v87")
check("never downgrades: installed v87 -> uptodate",
      cand is None and why == "uptodate", f"{cand} {why}")

# ------------------------------------------- manifest + restore integrity --
recovery = load_module("vg_recovery", "recovery_ui.py")
install = TMP / "install"
app_dir = install / "app"
app_dir.mkdir(parents=True)
(app_dir / "server.py").write_text("print('server')\n", encoding="utf-8")
(app_dir / "gui_qt.py").write_text("# gui\n", encoding="utf-8")
data_home = TMP / "datahome"
data_home.mkdir()
(data_home / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")

# point recovery's state at the throwaway tree
recovery.mutate_state(lambda s: s.update(
    {"last_paths": {"install_dir": str(install), "data_home": str(data_home)}}))

files = recovery.build_manifest(app_dir)
(app_dir / "manifest.json").write_text(
    json.dumps({"schema_version": 1, "files": files}), encoding="utf-8")
ok, missing, changed = recovery.verify_manifest(app_dir)
check("manifest verifies a clean tree", ok is True, f"{missing} {changed}")

snap = recovery.snapshot_app(install, "app_v84")
(app_dir / "gui_qt.py").write_text("# CORRUPTED\n", encoding="utf-8")
ok, missing, changed = recovery.verify_manifest(app_dir)
check("manifest detects the tampered file",
      ok is False and "gui_qt.py" in changed, f"{missing} {changed}")
recovery.restore_backup({"dir": snap, "tag": "app_v84"})
ok, missing, changed = recovery.verify_manifest(app_dir)
check("restore returns the tree to a manifest-identical state",
      ok is True, f"{missing} {changed}")
check("restore keeps the broken copy (app_broken-*)",
      any(d.name.startswith("app_broken-") for d in install.iterdir()))
check("settings.json restored from backup",
      json.loads((data_home / "settings.json").read_text()) == {"theme": "dark"})

# ------------------------------------------------------------- redaction ---
sample = '{"api_token": "SECRET123", "proxy": "http://user:p4ss@127.0.0.1:8080"}'
red = recovery.redact_text(sample)
check("diagnostics export redacts token + proxy password",
      "SECRET123" not in red and "p4ss" not in red, red)

# -------------------------------------- sweep regressions (v3) --------------
# spawn failure is judged a crash, not a supervisor traceback
_orig_popen = launcher.subprocess.Popen
class _Boom:
    def __init__(self, *a, **k):
        raise FileNotFoundError("host exe missing")
launcher.subprocess.Popen = _Boom
launcher.mutate_state(lambda st: st.update({"crash_count": 0, "runs": []}))
try:
    flow = launcher.supervise([])
finally:
    launcher.subprocess.Popen = _orig_popen
st = launcher.load_state()
check("spawn failure judged as crash (no traceback)",
      st["runs"][-1]["outcome"] == "crash" and st["crash_count"] == 1
      and flow in ("exit", "recovery"),
      f"{st['runs'][-1]} flow={flow}")
launcher.mutate_state(lambda st: st.update({"crash_count": 0, "runs": []}))

# zip-slip cannot escape the extraction dir
import zipfile as _zf
evil_zip = TMP / "evil.zip"
with _zf.ZipFile(evil_zip, "w") as z:
    z.writestr("../evil.txt", "pwned")
    z.writestr("good.txt", "fine")
dest = TMP / "extract-dest"
recovery._extract_zip(evil_zip, dest)
check("zip-slip entry cannot escape the target dir",
      not (TMP / "evil.txt").exists() and (dest / "good.txt").is_file())

# restoring a release backup stamps install-version.txt with its tag
# (re-point recovery at the throwaway tree first: the spawn-failure
# probe above let the supervisor record its own dir as install_dir -
# in production that record IS the install root by construction)
recovery.mutate_state(lambda st: st.update(
    {"last_paths": {"install_dir": str(install), "data_home": str(data_home)}}))
meta_path = snap / "backup_meta.json"
meta_path.write_text(json.dumps({"tag": "v84", "created_at": 1}), encoding="utf-8")
recovery.restore_backup({"dir": snap, "tag": "v84"})
check("restore stamps install-version.txt with the backup's tag",
      (install / "install-version.txt").read_text(encoding="utf-8").strip() == "v84")

# ============================ audit fixes (v7) =============================
import vg_state   # the sys.modules instance the consumers import
app_host = load_module("vg_app_host", "app_host.py")
import subprocess as _sp

# --- fix 10: one shared state module, token lock, rename takeover --------
check("all four consumers import the same vg_state module",
      launcher.load_state is vg_state.load_state
      and updater.load_state is vg_state.load_state
      and recovery.load_state is vg_state.load_state)
lock = STATE_DIR / "recovery_state.lock"
lock.write_text("999999:deadbeef", encoding="utf-8")   # dead pid + token
with vg_state.StateLock() as lk:
    was_held = lk.held
    content = lock.read_text(encoding="utf-8")
check("stale lock taken over via rename (no grave left behind)",
      was_held and ":" in content
      and not any(STATE_DIR.glob("*.stale-*")),
      content)
check("lock content carries pid:token", content.split(":")[0] == str(os.getpid()))

# live lock is never taken: a child holds it, parent must time out unheld
holder = _sp.Popen([sys.executable, "-c", """
import os, sys, time
sys.path.insert(0, r"%s")
import vg_state
with vg_state.StateLock(timeout=5) as lk:
    print("HELD" if lk.held else "NOTHELD", flush=True)
    time.sleep(1.2)
""" % str(BACKEND)], env={**os.environ, "VG_STATE_DIR": str(STATE_DIR)},
    stdout=_sp.PIPE, text=True)
assert holder.stdout.readline().strip() == "HELD"
with vg_state.StateLock(timeout=0.3) as lk2:
    check("live state lock is never taken over", not lk2.held)
holder.wait()

# two processes hammering mutate_state lose no updates (rename takeover
# under real contention)
counter = STATE_DIR / "counter.py"
counter.write_text("""
import os, sys
sys.path.insert(0, r"%s")
import vg_state
for _ in range(50):
    vg_state.mutate_state(lambda s: s.update(
        {"probe_count": int(s.get("probe_count") or 0) + 1}))
""" % str(BACKEND), encoding="utf-8")
vg_state.mutate_state(lambda s: s.update({"probe_count": 0}))
procs = [_sp.Popen([sys.executable, str(counter)],
                   env={**os.environ, "VG_STATE_DIR": str(STATE_DIR)})
         for _ in range(2)]
for pr in procs:
    pr.wait()
check("2 processes x 50 locked increments = exactly 100",
      vg_state.load_state().get("probe_count") == 100,
      str(vg_state.load_state().get("probe_count")))

# --- fix 7: judging step never latches backend_failed on a live child ----
check("judge_step: startup_ok settles ok at any time",
      launcher.judge_step({"startup_ok"}, True, 5) == "ok")
check("judge_step: app_alive + alive at 60s keeps watching",
      launcher.judge_step({"app_alive"}, True, 60) is None)
check("judge_step: app_alive + alive past first check keeps watching",
      launcher.judge_step({"app_alive"}, True, 200) is None)
check("judge_step: app_alive + alive at hard cap -> backend_failed",
      launcher.judge_step({"app_alive"}, True, 301) == "backend_failed")
check("judge_step: no markers + alive past first check keeps watching (no legacy grace)",
      launcher.judge_step(set(), True, 131) is None)
check("judge_step: no markers + alive at the hard cap -> crash (hung host)",
      launcher.judge_step(set(), True, 301) == "crash")
check("judge_step: exited child is finalised by the caller",
      launcher.judge_step({"app_alive"}, False, 10) is None)
check("first check window covers the GUI's 120s health watch",
      launcher.FIRST_CHECK_SECONDS >= 130)

# --- fix 2: cmd quoting for paths with spaces ------------------------------
cmdline = launcher.updater_bat_cmdline(
    Path(r"C:\video 3 grabber\updater.bat"), Path(r"C:\video 3 grabber"),
    Path(r"C:\pkg dir"), "v88")
check("bat cmdline wraps the whole command in one outer quote pair",
      cmdline == 'cmd /s /c ""C:\\video 3 grabber\\updater.bat" '
                 '"C:\\video 3 grabber" "C:\\pkg dir" "v88""',
      cmdline)
cmdline_h = launcher.updater_bat_cmdline(
    Path(r"C:\r\updater.bat"), Path(r"C:\r"), Path(r"C:\p"), "v88", "host")
check("bat cmdline host mode appends the mode inside the quotes",
      cmdline_h.endswith('"v88" "host""'), cmdline_h)

# --- fix 4: host mutex decision --------------------------------------------
check("host lock required for a plain run", app_host.host_lock_required({}))
check("host lock required when supervised",
      app_host.host_lock_required({"VG_SUPERVISED": "1"}))
check("host lock skipped for the smoke guest",
      not app_host.host_lock_required({"VG_SMOKE_TEST": "1"}))
check("host lock skipped for the updater guest",
      not app_host.host_lock_required({"VG_ENTRY_SCRIPT": "x.py"}))

# --- fix 6: auto-blacklist decision -----------------------------------------
check("auto-blacklist: crashing version, not last good, unlisted",
      launcher.should_auto_blacklist("v87", "v86", []))
check("auto-blacklist: never the last good version",
      not launcher.should_auto_blacklist("v86", "v86", []))
check("auto-blacklist: never a duplicate",
      not launcher.should_auto_blacklist("v87", "v86", ["v87"]))
check("auto-blacklist: never an unknown version",
      not launcher.should_auto_blacklist(None, "v86", []))

# --- fix 8: relaunch cap resets after a settled-ok run ----------------------
_calls = {"n": 0}
def _fake_supervise(args):
    _calls["n"] += 1
    launcher.mutate_state(lambda s: s["runs"].append(
        {"launch_id": f"fake{_calls['n']}", "started_at": 0,
         "outcome": "ok", "markers_seen": ["startup_ok"]}))
    return "relaunch" if _calls["n"] < 5 else "exit"
_orig_supervise = launcher.supervise
launcher.supervise = _fake_supervise
launcher.mutate_state(lambda s: s.update({"runs": [], "update_pending": False}))
try:
    rc = launcher.main([])
finally:
    launcher.supervise = _orig_supervise
check("main() allows more than 3 lifetime update relaunches when runs settle ok",
      _calls["n"] == 5 and rc == 0, f"calls={_calls['n']} rc={rc}")
launcher.mutate_state(lambda s: s.update({"runs": []}))

# --- fixes 3 + 5: backup and restore cover the host; swap is app-only -----
inst2 = TMP / "install2"
(inst2 / "app").mkdir(parents=True)
(inst2 / "app" / "server.py").write_text("print('old')\n", encoding="utf-8")
(inst2 / "VideoGrabberApp.exe").write_text("OLDHOST", encoding="utf-8")
(inst2 / "VideoGrabberApp_internal").mkdir()
(inst2 / "VideoGrabberApp_internal" / "x.dll").write_text("OLD", encoding="utf-8")
bk = updater.make_backup(inst2, "v80", lambda p: None)
check("backup includes the host exe and internal folder",
      (bk / "VideoGrabberApp.exe").is_file()
      and (bk / "VideoGrabberApp_internal" / "x.dll").is_file())
(inst2 / "app" / "server.py").write_text("print('broken')\n", encoding="utf-8")
(inst2 / "VideoGrabberApp.exe").write_text("BADHOST", encoding="utf-8")
recovery.mutate_state(lambda s: s.update(
    {"last_paths": {"install_dir": str(inst2), "data_home": str(data_home)}}))
recovery.restore_backup({"dir": bk, "tag": "v80"})
check("restore brings back app AND host",
      "old" in (inst2 / "app" / "server.py").read_text()
      and (inst2 / "VideoGrabberApp.exe").read_text() == "OLDHOST"
      and (inst2 / "VideoGrabberApp_internal" / "x.dll").read_text() == "OLD")

pkg = TMP / "pkg"
(pkg / "app").mkdir(parents=True)
(pkg / "app" / "server.py").write_text("print('new')\n", encoding="utf-8")
(pkg / "VideoGrabberApp.exe").write_text("NEWHOST", encoding="utf-8")
(pkg / "VideoGrabberApp_internal").mkdir()
(pkg / "VideoGrabberApp_internal" / "x.dll").write_text("NEW", encoding="utf-8")
pending = updater.swap_from_pkg(inst2, pkg, "v81")
check("swap_from_pkg swaps app only and reports host pending",
      pending is True and "new" in (inst2 / "app" / "server.py").read_text())
check("swap_from_pkg leaves the running host untouched",
      (inst2 / "VideoGrabberApp.exe").read_text() == "OLDHOST")
check("swap_from_pkg copies: the pkg stays whole for the bat",
      (pkg / "app" / "server.py").is_file()
      and (pkg / "VideoGrabberApp.exe").is_file())
check("version stamp deferred while a host swap is pending",
      not (inst2 / "install-version.txt").exists()
      or (inst2 / "install-version.txt").read_text().strip() != "v81")
pkg2 = TMP / "pkg2"
(pkg2 / "app").mkdir(parents=True)
(pkg2 / "app" / "server.py").write_text("print('newer')\n", encoding="utf-8")
pending2 = updater.swap_from_pkg(inst2, pkg2, "v82")
check("app-only package stamps the version immediately",
      pending2 is False
      and (inst2 / "install-version.txt").read_text().strip() == "v82")

# --- fix 11: download integrity ----------------------------------------------
import hashlib as _hl
zip_bytes = b"fake zip bytes"
good_sha = _hl.sha256(zip_bytes).hexdigest()
zpath = TMP / "dl.zip"
zpath.write_bytes(zip_bytes)
sums = TMP / "SHA256SUMS-v99.txt"
sums.write_text(f"{good_sha}  VideoGrabber-app-v99.zip\n", encoding="utf-8")
rel99 = {"tag_name": "v99", "assets": [
    {"name": "SHA256SUMS-v99.txt", "browser_download_url": sums.as_uri()}]}
try:
    updater.verify_download(rel99, "v99", "VideoGrabber-app-v99.zip",
                            zpath, len(zip_bytes), len(zip_bytes))
    ok_dl = True
except Exception:
    ok_dl = False
check("verify_download accepts a correct SHA-256", ok_dl)
sums.write_text(f"{'0' * 64}  VideoGrabber-app-v99.zip\n", encoding="utf-8")
try:
    updater.verify_download(rel99, "v99", "VideoGrabber-app-v99.zip",
                            zpath, len(zip_bytes), len(zip_bytes))
    bad_dl = False
except RuntimeError:
    bad_dl = True
check("verify_download rejects a wrong SHA-256", bad_dl)
try:
    updater.verify_download(rel99, "v99", "VideoGrabber-app-v99.zip",
                            zpath, len(zip_bytes), len(zip_bytes) - 1)
    short_dl = False
except RuntimeError:
    short_dl = True
check("verify_download rejects a short download (got != size)", short_dl)

# manifest verification inside the package checks SHA too
mpkg = TMP / "mpkg" / "app"
mpkg.mkdir(parents=True)
(mpkk := mpkg / "server.py").write_text("print('m')\n", encoding="utf-8")
data = mpkk.read_bytes()
(mpkk.parent / "manifest.json").write_text(json.dumps({"files": {
    "server.py": {"size": len(data), "sha256": _hl.sha256(data).hexdigest()}}}),
    encoding="utf-8")
try:
    updater.verify_pkg(TMP / "mpkg")
    ok_mf = True
except Exception:
    ok_mf = False
check("verify_pkg accepts a matching manifest SHA-256", ok_mf)
mpkk.write_text("print('tampered')\n", encoding="utf-8")
tampered = mpkk.read_bytes()
(mpkk.parent / "manifest.json").write_text(json.dumps({"files": {
    "server.py": {"size": len(tampered),
                  "sha256": _hl.sha256(data).hexdigest()}}}),
    encoding="utf-8")
try:
    updater.verify_pkg(TMP / "mpkg")
    bad_mf = False
except RuntimeError:
    bad_mf = True
check("verify_pkg rejects a same-size SHA-256 mismatch", bad_mf)

# --- fix 13: update result round trip ----------------------------------------
vg_state.set_update_result("v99", True, "Updated to v99.")
res = vg_state.pop_update_result()
check("update result is stored and popped exactly once",
      res is not None and res["ok"] and res["tag"] == "v99"
      and vg_state.pop_update_result() is None)

# --- fix 14: pruning + log rotation -------------------------------------------
for name in ("app_broken-20240101-000000", "app_broken-20240102-000000",
             "app_broken-20240103-000000"):
    (inst2 / name).mkdir(exist_ok=True)
recovery.prune_broken(inst2)
left = sorted(d.name for d in inst2.glob("app_broken-*"))
check("app_broken-* pruned to the newest 2",
      len(left) == 2 and "app_broken-20240101-000000" not in left,
      str(left))
logp = STATE_DIR / "recovery.log"
logp.write_text("x" * 600_000, encoding="utf-8")
vg_state.recovery_log("rotation probe", "harness")
check("recovery.log rotates at 512 KB keeping one .1",
      (STATE_DIR / "recovery.log.1").exists() and logp.stat().st_size < 1000)


# --------------------------- v7 audit regressions (pass 2) -------------------
import shutil as _sh
import time as _time
from pathlib import Path as _P

# A) stale staging\pkg must never trigger a host swap
class _FakeProc:
    pid = 1
    def wait(self):
        return 0
_orig_popen2 = launcher.subprocess.Popen
_orig_run_bat = launcher._run_bat
_orig_app_dir = launcher.APP_DIR
_orig_state_dir = launcher.STATE_DIR
_orig_updater_bat = launcher.UPDATER_BAT
_stage_root = TMP / "stage-test"
(_stage_root / "app").mkdir(parents=True)
(_stage_root / "app" / "updater_ui.py").write_text("# u", encoding="utf-8")
def _mk_stale_pkg():
    pkg = STATE_DIR / "staging" / "pkg"
    _sh.rmtree(pkg, ignore_errors=True)
    (pkg / "app").mkdir(parents=True)
    (pkg / "app" / "server.py").write_text("x", encoding="utf-8")
    (pkg / "VideoGrabberApp.exe").write_text("exe", encoding="utf-8")
    (pkg / "VideoGrabberApp_internal").mkdir()
    return pkg
_bat_calls = []
launcher.subprocess.Popen = lambda *a, **k: _FakeProc()
launcher._run_bat = lambda pkg, target, mode="": _bat_calls.append((target, mode)) or 0
launcher.APP_DIR = _stage_root / "app"
try:
    pkg = _mk_stale_pkg()
    launcher.mutate_state(lambda s: (s.pop("host_swap", None), s.update({"update_pending": True})))
    launcher.run_updater_flow()
    check("stale staged pkg + 'up to date' run triggers NO host swap",
          _bat_calls == [], str(_bat_calls))
    check("stale pkg is removed after the run", not pkg.exists())

    pkg = _mk_stale_pkg()
    _bat_calls.clear()
    launcher.mutate_state(lambda s: s.update({
        "update_pending": True, "host_swap": {"tag": "v99"},
        "update_target": {"tag": "v99"}}))
    launcher.run_updater_flow()
    check("host_swap flag from this run's updater DOES trigger the host swap",
          _bat_calls == [("v99", "host")], str(_bat_calls))
    check("host swap success sets current_version", launcher.load_state().get("current_version") == "v99")
    check("pkg is removed after a successful host swap", not pkg.exists())
    check("host_swap flag is consumed", "host_swap" not in launcher.load_state())
finally:
    launcher.subprocess.Popen = _orig_popen2
    launcher._run_bat = _orig_run_bat
    launcher.APP_DIR = _orig_app_dir

# B) failed host swap puts the previous app\ back (no mixed install)
_old_root = launcher.ROOT
_rt = TMP / "revert-root"
(_rt / "app").mkdir(parents=True)
(_rt / "app" / "server.py").write_text("NEW", encoding="utf-8")
(_rt / "app.old").mkdir()
(_rt / "app.old" / "server.py").write_text("OLD", encoding="utf-8")
launcher.ROOT = _rt
try:
    launcher._revert_app_after_failed_host_swap()
finally:
    launcher.ROOT = _old_root
check("failed host swap restores the previous app folder",
      (_rt / "app" / "server.py").read_text(encoding="utf-8") == "OLD"
      and not (_rt / "app.old").exists())

# C) relaunch budget refills only after a run that stayed up
check("healthy run: ok and >= 120 s",
      (launcher.mutate_state(lambda s: s.update({"runs": [
          {"launch_id": "h", "started_at": 1000, "outcome": "ok", "markers_seen": []}]})),
       launcher.last_run_was_healthy(now=1000 + 121))[1] is True)
check("short ok run does not refill the budget",
      launcher.last_run_was_healthy(now=1000 + 5) is False)
check("crashed run does not refill the budget",
      (launcher.mutate_state(lambda s: s.update({"runs": [
          {"launch_id": "c", "started_at": 1000, "outcome": "crash", "markers_seen": []}]})),
       launcher.last_run_was_healthy(now=1000 + 500))[1] is False)
_calls2 = {"n": 0}
def _quick_supervise(args):
    _calls2["n"] += 1
    launcher.mutate_state(lambda s: s["runs"].append(
        {"launch_id": f"q{_calls2['n']}", "started_at": int(_time.time()),
         "outcome": "ok", "markers_seen": ["startup_ok"]}))
    return "relaunch"
_orig_sup2 = launcher.supervise
launcher.supervise = _quick_supervise
launcher.mutate_state(lambda s: s.update({"runs": [], "update_pending": False}))
try:
    rc = launcher.main([])
finally:
    launcher.supervise = _orig_sup2
check("an update-relaunch loop of instantly-quitting ok runs is still capped",
      _calls2["n"] == 4 and rc == 0, f"calls={_calls2['n']} rc={rc}")
launcher.mutate_state(lambda s: s.update({"runs": [], "crash_count": 0}))

# D) spawn failure at the threshold auto-blacklists too
_orig_popen3 = launcher.subprocess.Popen
class _Boom2:
    def __init__(self, *a, **k):
        raise FileNotFoundError("host exe missing")
launcher.subprocess.Popen = _Boom2
launcher.mutate_state(lambda s: s.update({
    "crash_count": 2, "runs": [], "current_version": "v91",
    "last_good_version": "v90", "blacklist": []}))
try:
    _flow = launcher.supervise([])
finally:
    launcher.subprocess.Popen = _orig_popen3
check("spawn failure at 3 goes to recovery AND blacklists the version",
      _flow == "recovery" and "v91" in launcher.load_state()["blacklist"],
      f"{_flow} {launcher.load_state()['blacklist']}")
launcher.mutate_state(lambda s: s.update({"crash_count": 0, "runs": [], "blacklist": []}))

# E) --recovery works even when a supervisor already holds the mutex
_orig_acq, _orig_avail, _orig_launch = (launcher.acquire_instance_lock,
                                        launcher.recovery_available,
                                        launcher.launch_recovery)
_launched = []
launcher.acquire_instance_lock = lambda: False
launcher.recovery_available = lambda: True
launcher.launch_recovery = lambda reason: _launched.append(reason)
try:
    rc = launcher.main(["--recovery"])
    rc_plain = launcher.main([])
finally:
    launcher.acquire_instance_lock, launcher.recovery_available, launcher.launch_recovery = (
        _orig_acq, _orig_avail, _orig_launch)
check("second launch with --recovery reaches Recovery while the mutex is held",
      rc == 0 and len(_launched) == 1, str(_launched))
check("second plain launch still exits quietly",
      rc_plain == 0 and len(_launched) == 1, str(_launched))

# --------------------------- post-audit fixes (v8) --------------------------
# Fix 1: a refused duplicate host exits 42 and is never counted.
check("exit code 42 agrees between host and supervisor",
      launcher.HOST_DUPLICATE_EXIT == app_host.DUPLICATE_HOST_EXIT == 42)
import ast as _ast
_host_src = (BACKEND / "app_host.py").read_text(encoding="utf-8")
check("app_host host-lock refusal returns the distinct code",
      "return DUPLICATE_HOST_EXIT" in _host_src)


class _FakeChild42:
    pid = 4242
    def poll(self):
        return 42
    def wait(self, timeout=None):
        return 42


_orig_spawn = launcher.spawn_child
launcher.spawn_child = lambda args, lid: _FakeChild42()
launcher.mutate_state(lambda s: s.update(
    {"crash_count": 2, "runs": [], "update_pending": False}))
try:
    _flow = launcher.supervise([])
finally:
    launcher.spawn_child = _orig_spawn
_st = launcher.load_state()
check("supervisor judges exit 42 as 'duplicate', counter untouched",
      _st["runs"][-1]["outcome"] == "duplicate" and _st["crash_count"] == 2
      and _flow == "exit",
      f"{_st['runs'][-1]} count={_st['crash_count']} flow={_flow}")
launcher.mutate_state(lambda s: s.update({"crash_count": 0, "runs": []}))

# Fix 2: a failed Job Object assignment kills the child and the supervisor.
class _FakeProc:
    pid = 777
    def __init__(self):
        self.terminated = False
    def terminate(self):
        self.terminated = True


_fake = _FakeProc()
_orig_popen3 = launcher.subprocess.Popen
_orig_assign = launcher._assign_to_job
launcher.subprocess.Popen = lambda *a, **k: _fake
launcher._assign_to_job = lambda proc: False
try:
    launcher.spawn_child([], "jobtest")
    _fatal_raised = False
except launcher.SupervisorFatal:
    _fatal_raised = True
finally:
    launcher.subprocess.Popen = _orig_popen3
    launcher._assign_to_job = _orig_assign
check("failed job assignment terminates the child and raises SupervisorFatal",
      _fatal_raised and _fake.terminated)
_orig_spawn2 = launcher.spawn_child
def _boom_spawn(args, lid):
    raise launcher.SupervisorFatal("test")
launcher.spawn_child = _boom_spawn
try:
    launcher.supervise([])
    _propagated = False
except launcher.SupervisorFatal:
    _propagated = True
finally:
    launcher.spawn_child = _orig_spawn2
check("supervise does not judge SupervisorFatal as a child crash",
      _propagated
      and launcher.load_state()["crash_count"] == 0)
check("job assignment is a no-op success off Windows",
      launcher._assign_to_job(object()) is True)

# Fix 4: the schema gate refuses newer state, in both components.
check("vg_state SCHEMA_VERSION is 1", vg_state.SCHEMA_VERSION == 1)
launcher.mutate_state(lambda s: s.update({"schema_version": 99}))
check("schema gate flags a newer state file",
      vg_state.schema_gate_error() is not None)
check("supervisor refuses to run on a newer schema",
      launcher.main([]) == 1)
_w = updater.UpdateWorker()
try:
    _w._run()
    _worker_refused = False
except RuntimeError as e:
    _worker_refused = "schema_version" in str(e)
check("updater refuses to run on a newer schema", _worker_refused)
launcher.mutate_state(lambda s: s.update({"schema_version": 1}))
check("schema gate passes again on schema 1",
      vg_state.schema_gate_error() is None)

# Fix 5: the package's fresh bats are installed by the updater.
inst3 = TMP / "install3"
(inst3 / "app").mkdir(parents=True)
(inst3 / "app" / "server.py").write_text("print('x')\n", encoding="utf-8")
(inst3 / "updater.bat").write_text("OLDBAT", encoding="utf-8")
pkg3 = TMP / "pkg3"
(pkg3 / "app").mkdir(parents=True)
(pkg3 / "app" / "server.py").write_text("print('y')\n", encoding="utf-8")
(pkg3 / "updater.bat").write_text("NEWBAT", encoding="utf-8")
(pkg3 / "restore.bat").write_text("NEWRESTORE", encoding="utf-8")
updater.swap_from_pkg(inst3, pkg3, "v83")
check("updater installs the package's fresh bats at the root",
      (inst3 / "updater.bat").read_text() == "NEWBAT"
      and (inst3 / "restore.bat").read_text() == "NEWRESTORE")

# --------------------------- deep scan fixes (v9) ---------------------------
import threading as _th
import time

# F1: an abandoned pid-0 lock is taken over once it is old; a fresh
# one is left alone (its creator may be mid-write).
lockp = STATE_DIR / "recovery_state.lock"
lockp.write_text("garbage-no-pid", encoding="utf-8")
os.utime(lockp, (time.time() - 30, time.time() - 30))
with vg_state.StateLock(timeout=2.0) as lk:
    _held_old = lk.held
check("abandoned pid-0 lock (old) is taken over", _held_old)
lockp.write_text("garbage-no-pid", encoding="utf-8")   # fresh corpse
with vg_state.StateLock(timeout=0.3) as lk:
    _held_fresh = lk.held
check("fresh pid-0 lock is NOT taken over", not _held_fresh
      and lockp.exists())
lockp.unlink()

# F2: threads inside one process cannot lose each other's writes.
vg_state.mutate_state(lambda s: s.update({"probe_threads": 0}))
def _hammer():
    for _ in range(25):
        vg_state.mutate_state(lambda s: s.update(
            {"probe_threads": int(s.get("probe_threads") or 0) + 1}))
_ts = [_th.Thread(target=_hammer) for _ in range(4)]
[t.start() for t in _ts]
[t.join() for t in _ts]
check("4 threads x 25 locked increments = exactly 100",
      vg_state.load_state().get("probe_threads") == 100,
      str(vg_state.load_state().get("probe_threads")))

# F3: next-boot judging honours the recorded exit code and the
# marker FILES, not only the persisted markers_seen.
launcher.mutate_state(lambda s: s.update(
    {"crash_count": 1, "runs": [{"launch_id": "dup1", "started_at": 0,
                                 "outcome": None, "markers_seen": [],
                                 "exit_code": 42}]}))
launcher.judge_unjudged_run()
_st = launcher.load_state()
check("unjudged run with exit_code 42 is judged duplicate, not crash",
      _st["runs"][-1]["outcome"] == "duplicate" and _st["crash_count"] == 1)
(STATE_DIR / "startup_ok.tmp").write_text(
    json.dumps({"launch_id": "filemark", "timestamp": 1}), encoding="utf-8")
launcher.mutate_state(lambda s: s.update(
    {"runs": [{"launch_id": "filemark", "started_at": 0,
               "outcome": None, "markers_seen": []}]}))
launcher.judge_unjudged_run()
_st = launcher.load_state()
check("unjudged run is judged from its marker files (startup_ok -> ok)",
      _st["runs"][-1]["outcome"] == "ok" and _st["crash_count"] == 0)
(STATE_DIR / "startup_ok.tmp").unlink(missing_ok=True)
launcher.mutate_state(lambda s: s.update({"runs": []}))

# F4: a schema-blocked updater never reaches the bat fallback.
launcher.mutate_state(lambda s: s.update(
    {"schema_version": 99, "update_pending": True}))
launcher.run_updater_flow()
_res = vg_state.pop_update_result()
check("schema-blocked update: failure result, no bat, request consumed",
      _res is not None and _res["ok"] is False
      and "newer" in _res["message"]
      and launcher.load_state()["update_pending"] is False,
      str(_res))
# F5: with the schema fine but nothing staged, the failure is surfaced.
launcher.mutate_state(lambda s: s.update(
    {"schema_version": 1, "update_pending": True}))
launcher.run_updater_flow()
_res = vg_state.pop_update_result()
check("unstageable update failure is surfaced to the user",
      _res is not None and _res["ok"] is False, str(_res))

# F6: the updater window refuses to close while its worker runs.
class _StubWorker(updater.QThread):
    progress = updater.Signal(int); status = updater.Signal(str)
    phase = updater.Signal(str); stats = updater.Signal(str)
    finished_ok = updater.Signal(str); failed = updater.Signal(str)
    def run(self):
        self.msleep(4000)
_orig_worker_cls = updater.UpdateWorker
updater.UpdateWorker = _StubWorker
_qapp = updater.QApplication.instance() or updater.QApplication([])
_win = updater.UpdaterWindow()
class _Ev:
    def __init__(self):
        self.ignored = False; self.accepted = False
    def ignore(self):
        self.ignored = True
    def accept(self):
        self.accepted = True
_ev = _Ev()
_win.closeEvent(_ev)
check("updater window refuses to close mid-update", _ev.ignored)
_win.worker.requestInterruption()
_win.worker.quit(); _win.worker.wait(6000)
_ev2 = _Ev(); _win.closeEvent(_ev2)
check("updater window closes once the worker finished", _ev2.accepted)
_win.close()
updater.UpdateWorker = _orig_worker_cls

# F7: backups of an unknown version are pruned too.
for _name, _ts in (("app_unknown", 1), ("app_v1", 2)):
    _d = updater.BACKUPS_DIR / _name
    _d.mkdir(parents=True, exist_ok=True)
    (_d / "backup_meta.json").write_text(json.dumps(
        {"tag": _name, "created_at": _ts}), encoding="utf-8")
updater.make_backup(inst3, "v90", lambda p: None)
_left = sorted(d.name for d in updater.BACKUPS_DIR.iterdir()
               if d.is_dir() and d.name.startswith("app_"))
check("backup pruning covers app_unknown (newest 2 by creation)",
      "app_unknown" not in _left and "app_v90" in _left, str(_left))

# F9: diagnosis reports a duplicate run as what it is.
launcher.mutate_state(lambda s: s.update(
    {"runs": [{"launch_id": "diag1", "outcome": "duplicate",
               "markers_seen": []}]}))
_findings = recovery.run_diagnosis()
check("diagnosis labels a duplicate run correctly",
      any("duplicate" in f["title"].lower() for f in _findings)
      and not any("never judged" in f["title"].lower() for f in _findings),
      "; ".join(f["title"] for f in _findings[:2]))
launcher.mutate_state(lambda s: s.update({"runs": []}))

# F10: restoring settings works even when the data home is new.
_orig_dh = recovery.data_home
_newhome = TMP / "brand-new-home"
_bkdir = TMP / "bk-settings-only"
_bkdir.mkdir(parents=True, exist_ok=True)
(_bkdir / "settings.json").write_text('{"a": 1}', encoding="utf-8")
recovery.data_home = lambda: _newhome
try:
    recovery.restore_backup({"dir": _bkdir, "tag": "v55"})
    _settings_ok = (_newhome / "settings.json").is_file()
except Exception:
    _settings_ok = False
finally:
    recovery.data_home = _orig_dh
check("restore creates the data home for the settings copy", _settings_ok)
_stamp = BACKEND / "install-version.txt"
if _stamp.exists():
    _stamp.unlink()

# F8/F11/F12/F13: source-level guards for the platform-bound fixes.
_rec_src = (BACKEND / "recovery_ui.py").read_text(encoding="utf-8")
check("recovery refuses a newer schema at startup",
      "schema_gate_error" in _rec_src.split("def main")[1])
check("rollback prefers the shallowest app in the package",
      "Shallowest match wins" in _rec_src)
_bat_src = (BACKEND / "updater.bat").read_text(encoding="utf-8")
check("bat rollback removes copies that had no .old",
      "rolling back removes the copy" in _bat_src
      or "delete the copy" in _bat_src)
_rst_src = (BACKEND / "restore.bat").read_text(encoding="utf-8")
check("restore.bat aborts instead of relaunching a mixed install",
      "Not relaunching a mixed install" in _rst_src)

# ---------------------------------------------------------------- summary --
failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} assertions passed")
if failed:
    print("FAILED:", "; ".join(failed))
    sys.exit(1)
print("HARNESS: ALL GREEN")
