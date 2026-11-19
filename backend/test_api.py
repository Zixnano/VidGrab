"""pytest safety net for api.py. Run: pytest -v backend/test_api.py

Everything runs against a throwaway state directory; nothing touches the
real settings.json, jobs.json or recovery_state.json.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# Must be set BEFORE importing api / settings: they read these at import time.
_STATE_TMP = tempfile.mkdtemp(prefix="vg_test_state_")
os.environ["VG_STATE_DIR"] = _STATE_TMP
os.environ["VG_EXTENSION_ID"] = "abcdefghijklmnopabcdefghijklmnop"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import api
import jobs as jobs_mod
import settings
import vg_db
import ytdlp_update

TOKEN = "test-token-0123456789abcdef"
EXT_ORIGIN = "chrome-extension://" + os.environ["VG_EXTENSION_ID"]

# Routes that are open on purpose (no X-API-Token header).
OPEN_ROUTES = {"/health", "/ping", "/version", "/pair", "/remote",
               "/remote-login", "/upload"}
# Routes whose upstream (network / external tool) may legitimately fail.
UPSTREAM_ROUTES = {"/changelog", "/favicon/<domain>", "/ytdlp-update",
                   "/ytdlp-rollback", "/probe", "/probe-formats", "/probe-head"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="vg_test_home_"))
    settings.set_paths(home)
    monkeypatch.setitem(settings.STATE, "output_dir", str(home / "out"))
    monkeypatch.setitem(settings.STATE, "api_token", TOKEN)
    monkeypatch.setitem(settings.STATE, "lan_access", False)
    monkeypatch.setattr(api, "_PAIR_GRANTED", False)

    def no_network(*a, **k):
        raise api.requests.ConnectionError("network disabled in tests")
    monkeypatch.setattr(api.requests, "get", no_network)
    monkeypatch.setattr(api.requests, "post", no_network)
    # status() inspects the real installed yt-dlp; keep the test hermetic.
    monkeypatch.setattr(ytdlp_update, "status", lambda: {})
    api.JOBS.clear()
    # v13: every test gets its own database file (set_paths above moved
    # DB_PATH); forget what the previous test's database held.
    jobs_mod.reset_persistence()
    yield
    api.JOBS.clear()
    jobs_mod.reset_persistence()


@pytest.fixture
def client():
    return api.app.test_client()


@pytest.fixture
def auth():
    return {"X-API-Token": TOKEN}


def _routes():
    out = []
    for rule in api.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((rule.rule, method))
    return sorted(out)


def _fill(rule):
    return re.sub(r"<[^>]+>", "nosuchjob", rule)


# ------------------------------------------------------------ every route --
@pytest.mark.parametrize("rule,method", _routes())
def test_route_with_valid_token(client, auth, rule, method):
    if rule == "/pair":
        pytest.skip("covered by the dedicated /pair tests")
    if rule == "/upload":
        pytest.skip("uses a one-time nonce, covered below")
    if rule == "/remote-login":
        pytest.skip("uses its own password login, covered below")
    url = _fill(rule)
    r = client.open(url, method=method, headers=auth, json={} if method == "POST" else None)
    assert r.status_code != 401, f"{method} {rule} rejected a valid token"
    assert r.status_code != 403, f"{method} {rule} unexpectedly forbidden"
    if rule in UPSTREAM_ROUTES:
        assert r.status_code < 600
    else:
        assert r.status_code < 500, f"{method} {rule} -> {r.status_code}: {r.get_data(as_text=True)[:200]}"


# --------------------------------------------------------------- auth ------
def test_diskspace_requires_token(client, auth):
    assert client.get("/diskspace").status_code == 401
    assert client.get("/diskspace", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/diskspace", headers=auth).status_code == 200


def test_rules_not_in_protected_paths():
    assert "/rules" not in api.TOKEN_PROTECTED_PATHS
    assert "/diskspace" in api.TOKEN_PROTECTED_PATHS


@pytest.mark.parametrize("path", sorted(api.TOKEN_PROTECTED_PATHS))
def test_protected_paths_reject_missing_token(client, path):
    r = client.open(path, method="POST")
    assert r.status_code in (401, 404, 405), (path, r.status_code)
    if r.status_code != 401:
        # a GET-only route answers POST with 405 before auth; check GET too
        assert client.get(path).status_code in (401, 404, 405)


def test_query_key_no_longer_unlocks_job_actions(client):
    assert not hasattr(api, "REMOTE_KEY")
    for verb in ("pause", "resume", "stop"):
        assert client.post(f"/{verb}/1?key=anything").status_code == 401


def test_upload_requires_nonce(client, auth):
    assert client.post("/upload").status_code == 401
    assert client.post("/upload", headers={"X-Upload-Nonce": "bogus"}).status_code == 401
    r = client.post("/upload-token", headers=auth, json={})
    assert r.status_code == 200


def test_remote_login_wrong_password_is_refused(client):
    r = client.post("/remote-login", json={"password": "definitely-wrong"})
    assert r.status_code in (400, 401, 403)


# ----------------------------------------------------------- host header ---
@pytest.mark.parametrize("host", ["127.0.0.1:5757", "localhost:5757", "localhost", "127.0.0.1"])
def test_local_host_allowed(client, host):
    assert client.get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "rebind.evil.com:5757", "192.168.1.5:5757", "localhost.evil.com"])
def test_foreign_host_rejected(client, auth, host):
    assert client.get("/health", headers={"Host": host}).status_code == 403
    assert client.get("/diskspace", headers={**auth, "Host": host}).status_code == 403


def test_lan_mode_accepts_ip_literal_but_not_hostnames(client, monkeypatch):
    monkeypatch.setitem(settings.STATE, "lan_access", True)
    assert client.get("/health", headers={"Host": "192.168.1.5:5757"}).status_code == 200
    assert client.get("/health", headers={"Host": "rebind.evil.com"}).status_code == 403
    assert client.get("/health", headers={"Host": "DESKTOP-ABC:5757"}).status_code == 200
    assert client.get("/health", headers={"Host": "mypc.local:5757"}).status_code == 200
    assert client.get("/health", headers={"Host": "localhost.evil.com"}).status_code == 403


# ----------------------------------------------------------------- /pair ---
def test_pair_rejects_fake_origin(client):
    for origin in ("https://evil.example", "null",
                   "chrome-extension://" + "z" * 32):
        r = client.post("/pair", headers={"Origin": origin})
        assert r.status_code == 403, origin
        assert "token" not in r.get_json()


def test_pair_accepts_pinned_extension_once(client):
    r = client.post("/pair", headers={"Origin": EXT_ORIGIN})
    assert r.status_code == 200
    assert r.get_json()["token"] == TOKEN
    again = client.post("/pair", headers={"Origin": EXT_ORIGIN})
    assert again.status_code == 403


# ---------------------------------------------------------------- misc -----
def test_health_identity(client):
    body = client.get("/health").get_json()
    assert body["ok"] is True and body["app"] == "videograbber"


def test_cors_origin_is_pinned():
    assert api._EXT_ORIGIN == EXT_ORIGIN


# ======================================================= v13: SQLite =======
def _mk(name, url=None, **kw):
    """Queue a job without touching the network and return its id."""
    return jobs_mod.new_job(url or f"https://example.com/{name}", filename=name,
                            snapshot=False, **kw)


def _db_rows(sql, args=()):
    jobs_mod.flush_jobs()
    return vg_db.conn().execute(sql, args).fetchall()


def _restart():
    """Simulate quitting and relaunching: flush, drop all in-memory state, reload."""
    jobs_mod.flush_jobs()
    api.JOBS.clear()
    jobs_mod.reset_persistence()
    jobs_mod.load_jobs_snapshot()


def _need_fts():
    if not vg_db.fts_available():
        pytest.skip("this SQLite build has no FTS5")


# ------------------------------------------------------------- schema ------
def test_database_uses_wal_and_has_the_tables():
    c = vg_db.conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"jobs", "download_stats", "meta"} <= names
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert {"id", "url", "filename", "status", "archived", "metadata", "data"} <= cols
    assert vg_db.db_path() == settings.HOME / "videograbber.db"


def test_fts_table_exists_when_available():
    _need_fts()
    names = {r[0] for r in vg_db.conn().execute("SELECT name FROM sqlite_master")}
    assert "jobs_fts" in names


def test_sqlite_runtime_has_what_the_frozen_host_needs():
    # The packaged Host must ship _sqlite3 + sqlite3.dll; this fails loudly in
    # CI if the build's stdlib list ever drops them.
    assert sqlite3.sqlite_version_info >= (3, 24)   # ON CONFLICT ... DO UPDATE


# ---------------------------------------------------------- migration ------
def _legacy_jobs():
    return {
        "1": {"id": "1", "url": "https://example.com/one.mp4", "filename": "one.mp4",
              "category": "Video", "type": "generic", "status": "done", "phase": "done",
              "size_total": 10, "size_done": 10, "created_ts": 1000.0, "completed_ts": 1001.0},
        "2": {"id": "2", "url": "https://example.com/two.mp4", "filename": "two.mp4",
              "category": "Video", "type": "generic", "status": "downloading",
              "created_ts": 2000.0},
        "7": {"id": "7", "url": "https://example.com/seven.zip", "filename": "seven.zip",
              "category": "Compressed", "type": "generic", "status": "error",
              "created_ts": 3000.0, "failed_ts": 3001.0},
    }


def test_first_launch_migrates_jobs_json(monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 0)
    settings.HOME.mkdir(parents=True, exist_ok=True)
    legacy = settings.JOBS_PATH
    legacy.write_text(json.dumps(_legacy_jobs()))
    jobs_mod.load_jobs_snapshot()
    assert sorted(api.JOBS) == ["1", "2", "7"]
    assert not legacy.exists()
    assert (legacy.parent / "jobs.json.migrated").exists()
    assert json.loads((legacy.parent / "jobs.json.migrated").read_text()) == _legacy_jobs()
    assert {r[0] for r in _db_rows("SELECT id FROM jobs")} == {"1", "2", "7"}
    # same repair as before: work that was in flight reloads as stopped
    assert api.JOBS["2"]["status"] == "stopped"
    # the counter continues after the highest id
    assert jobs_mod.next_job_id() == "8"
    # events are rebuilt for restored jobs
    assert api.JOBS["1"]["pause_evt"] is not None


def test_migration_runs_once_and_never_overwrites_newer_history(monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 0)
    settings.HOME.mkdir(parents=True, exist_ok=True)
    settings.JOBS_PATH.write_text(json.dumps(_legacy_jobs()))
    jobs_mod.load_jobs_snapshot()
    api.JOBS["1"]["filename"] = "renamed_after_migration.mp4"
    jobs_mod.flush_jobs()
    # an old copy turns up again (restored backup, crash before rename, ...)
    settings.JOBS_PATH.write_text(json.dumps(_legacy_jobs()))
    _restart()
    assert api.JOBS["1"]["filename"] == "renamed_after_migration.mp4"
    assert not settings.JOBS_PATH.exists()
    assert len(list(settings.HOME.glob("jobs.json.migrated*"))) == 2


def test_corrupt_jobs_json_is_kept_not_overwritten():
    settings.HOME.mkdir(parents=True, exist_ok=True)
    settings.JOBS_PATH.write_text("{ this is not json")
    jobs_mod.load_jobs_snapshot()
    assert api.JOBS == {}
    assert not settings.JOBS_PATH.exists()
    kept = list(settings.HOME.glob("jobs.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text() == "{ this is not json"


def test_no_jobs_json_is_a_clean_first_run():
    jobs_mod.load_jobs_snapshot()
    assert api.JOBS == {}
    assert vg_db.count_jobs() == 0


def test_download_stats_move_out_of_settings(monkeypatch):
    monkeypatch.setitem(settings.STATE, "download_stats",
                        [{"ts": 1.0, "bytes": 5}, {"ts": 2.0, "bytes": 7}, "junk", {"bytes": 1}])
    saved = []
    monkeypatch.setattr(settings, "save_settings", lambda: saved.append(1))
    jobs_mod.load_jobs_snapshot()
    assert "download_stats" not in settings.STATE
    assert saved, "settings.json must be re-saved without the old list"
    assert vg_db.recent_stats() == [{"ts": 1.0, "bytes": 5}, {"ts": 2.0, "bytes": 7}]
    # a second launch must not import again
    monkeypatch.setitem(settings.STATE, "download_stats", [{"ts": 9.0, "bytes": 9}])
    jobs_mod.load_jobs_snapshot()
    assert len(vg_db.recent_stats()) == 2


def test_default_settings_no_longer_carry_download_stats():
    assert "download_stats" not in settings.STATE


# -------------------------------------------------------------- stats ------
def test_record_stat_and_stats_route(client, auth):
    import downloader
    downloader.record_stat(111)
    downloader.record_stat(222)
    samples = client.get("/stats", headers=auth).get_json()["samples"]
    assert [s["bytes"] for s in samples] == [111, 222]
    assert set(samples[0]) == {"ts", "bytes"}


def test_stats_keep_only_the_newest_3600(monkeypatch):
    monkeypatch.setattr(vg_db, "STATS_KEEP", 50)
    for i in range(80):
        vg_db.record_stat(float(i), i)
    assert vg_db.count_stats() == 50
    assert vg_db.recent_stats(600)[0]["bytes"] == 30
    assert [s["bytes"] for s in vg_db.recent_stats(3)] == [77, 78, 79]


def test_stats_route_returns_at_most_600(client, auth):
    for i in range(650):
        vg_db.record_stat(float(i), i)
    assert len(client.get("/stats", headers=auth).get_json()["samples"]) == 600


# ---------------------------------------------------------- persistence ----
def test_new_job_is_written_to_sqlite():
    jid = _mk("holiday_video_one.mp4")
    rows = _db_rows("SELECT id, filename, status, archived FROM jobs")
    assert rows == [(jid, "holiday_video_one.mp4", "queued", 0)]


def test_only_changed_rows_are_rewritten():
    a, b = _mk("first_clip_here.mp4"), _mk("second_clip_here.mp4")
    before = dict(_db_rows("SELECT id, updated_ts FROM jobs"))
    time.sleep(0.02)
    api.JOBS[a]["size_done"] = 123
    jobs_mod.flush_jobs()
    after = dict(_db_rows("SELECT id, updated_ts FROM jobs"))
    assert after[a] > before[a]
    assert after[b] == before[b]
    assert _db_rows("SELECT size_done FROM jobs WHERE id=?", (a,)) == [(123,)]


def test_deleting_a_job_removes_its_row(client, auth):
    jid = _mk("to_be_deleted_clip.mp4")
    jobs_mod.flush_jobs()
    assert client.post(f"/delete/{jid}", headers=auth).status_code == 200
    assert _db_rows("SELECT id FROM jobs") == []


def test_clear_failed_removes_rows(client, auth):
    jid = _mk("this_one_will_fail.mp4")
    jobs_mod.transition(api.JOBS[jid], jobs_mod.JobEvent.START)
    jobs_mod.transition(api.JOBS[jid], jobs_mod.JobEvent.FAIL)
    jobs_mod.flush_jobs()
    assert client.post("/clear-failed", headers=auth, json={}).get_json() == {"removed": 1}
    assert _db_rows("SELECT id FROM jobs") == []


def test_secrets_and_events_are_not_stored():
    jid = _mk("private_session_clip.mp4", cookie="SESSION=secret", referer="https://r.example/")
    api.JOBS[jid]["error"] = "boom"
    jobs_mod.flush_jobs()
    (blob,) = _db_rows("SELECT data FROM jobs")[0]
    assert "secret" not in blob and "boom" not in blob
    assert "pause_evt" not in blob and "stop_evt" not in blob
    assert json.loads(blob)["referer"] == "https://r.example/"


def test_history_survives_a_restart_without_a_cap():
    ids = [_mk(f"bulk_item_number_{i}.mp4") for i in range(2500)]   # old cap was 2000
    _restart()
    assert len(api.JOBS) == 2500
    assert vg_db.count_jobs() == 2500
    assert sorted(api.JOBS, key=int) == sorted(ids, key=int)
    assert all(j["status"] == "stopped" for j in api.JOBS.values())   # queued reloads as stopped


def test_restart_round_trips_fields():
    jid = _mk("round_trip_clip.mp4", referer="https://ref.example/", user_agent="UA/1")
    api.JOBS[jid]["subdir"] = "Some Folder"
    jobs_mod.transition(api.JOBS[jid], jobs_mod.JobEvent.STOP)
    _restart()
    j = api.JOBS[jid]
    assert (j["referer"], j["user_agent"], j["subdir"], j["status"]) == \
        ("https://ref.example/", "UA/1", "Some Folder", "stopped")


def test_a_save_before_the_history_is_loaded_cannot_erase_it():
    _mk("keep_me_around_clip.mp4")
    jobs_mod.flush_jobs()
    api.JOBS.clear()
    jobs_mod.reset_persistence()      # a fresh process: nothing loaded yet
    jobs_mod.save_jobs_snapshot(wait=True)
    assert vg_db.count_jobs() == 1


def test_concurrent_saves_end_consistent():
    ids = [_mk(f"concurrent_clip_{i}.mp4") for i in range(40)]
    errors = []

    def worker(n):
        try:
            for k in range(30):
                jid = ids[(n * 7 + k) % len(ids)]
                api.JOBS[jid]["size_done"] = n * 1000 + k
                jobs_mod.save_jobs_snapshot()
                if k % 10 == 0:
                    jobs_mod.save_jobs_snapshot(wait=True)
        except Exception as e:     # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    jobs_mod.flush_jobs()
    stored = dict(_db_rows("SELECT id, size_done FROM jobs"))
    assert stored == {jid: api.JOBS[jid]["size_done"] for jid in ids}


def test_background_writer_saves_without_being_asked_to_wait():
    jid = _mk("background_writer_clip.mp4")
    api.JOBS[jid]["size_done"] = 42
    jobs_mod.save_jobs_snapshot()
    deadline = time.time() + 5
    while time.time() < deadline:
        if vg_db.conn().execute("SELECT size_done FROM jobs WHERE id=?", (jid,)).fetchone() == (42,):
            return
        time.sleep(0.05)
    pytest.fail("the writer thread never saved the change")


def test_unserializable_value_does_not_block_other_jobs():
    a, b = _mk("good_row_clip.mp4"), _mk("odd_row_clip.mp4")
    api.JOBS[b]["weird"] = object()          # default=str keeps it saveable
    api.JOBS[a]["size_done"] = 5
    jobs_mod.flush_jobs()
    assert _db_rows("SELECT size_done FROM jobs WHERE id=?", (a,)) == [(5,)]
    assert _db_rows("SELECT COUNT(*) FROM jobs") == [(2,)]


# ------------------------------------------------------------- archived ----
def _fail(jid, age_hours=0):
    j = api.JOBS[jid]
    jobs_mod.transition(j, jobs_mod.JobEvent.START)
    jobs_mod.transition(j, jobs_mod.JobEvent.FAIL)
    j["failed_ts"] = time.time() - age_hours * 3600
    return jid


def test_archived_column_exists_and_defaults_false():
    jid = _mk("fresh_unarchived_clip.mp4")
    assert api.JOBS[jid]["archived"] is False
    assert _db_rows("SELECT archived FROM jobs") == [(0,)]


def test_archive_route_and_default_listing(client, auth):
    jid = _fail(_mk("failed_old_clip.mp4"))
    live = _mk("still_queued_clip.mp4")
    r = client.post(f"/archive/{jid}", headers=auth)
    assert r.status_code == 200
    assert _db_rows("SELECT archived FROM jobs WHERE id=?", (jid,)) == [(1,)]
    assert set(client.get("/jobs", headers=auth).get_json()) == {live}
    assert set(client.get("/jobs?archived=1", headers=auth).get_json()) == {jid}
    assert set(client.get("/jobs?archived=all", headers=auth).get_json()) == {jid, live}
    assert client.post(f"/unarchive/{jid}", headers=auth).status_code == 200
    assert set(client.get("/jobs", headers=auth).get_json()) == {jid, live}


def test_cannot_archive_a_live_job(client, auth):
    jid = _mk("running_or_waiting_clip.mp4")
    assert client.post(f"/archive/{jid}", headers=auth).status_code == 409
    assert client.post("/archive/nosuchjob", headers=auth).status_code == 404
    assert client.post("/unarchive/nosuchjob", headers=auth).status_code == 404


def test_archived_flag_survives_restart(client, auth):
    jid = _fail(_mk("archived_then_restarted.mp4"))
    client.post(f"/archive/{jid}", headers=auth)
    _restart()
    assert api.JOBS[jid]["archived"] is True


def test_stale_errors_are_archived_by_the_old_setting(monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 24)
    old = _fail(_mk("failed_long_ago_clip.mp4"), age_hours=30)
    new = _fail(_mk("failed_just_now_clip.mp4"), age_hours=1)
    done = _mk("finished_fine_clip.mp4")
    assert jobs_mod.archive_stale_errors() == 1
    assert api.JOBS[old]["archived"] and not api.JOBS[new]["archived"] and not api.JOBS[done]["archived"]
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 0)    # 0 = never
    api.JOBS[new]["failed_ts"] = 1.0
    assert jobs_mod.archive_stale_errors() == 0


def test_stale_errors_are_archived_on_launch(monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 24)
    old = _fail(_mk("failed_before_restart_clip.mp4"), age_hours=48)
    _restart()
    assert api.JOBS[old]["archived"] is True


def test_manual_unarchive_is_not_undone_by_the_sweep(client, auth, monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 24)
    jid = _fail(_mk("looked_at_again_clip.mp4"), age_hours=100)
    jobs_mod.archive_stale_errors()
    client.post(f"/unarchive/{jid}", headers=auth)
    assert jobs_mod.archive_stale_errors() == 0
    assert api.JOBS[jid]["archived"] is False


def test_retrying_a_failed_job_unarchives_it(monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 24)
    jid = _fail(_mk("retried_failure_clip.mp4"), age_hours=100)
    jobs_mod.archive_stale_errors()
    jobs_mod.transition(api.JOBS[jid], jobs_mod.JobEvent.RESUME)
    assert api.JOBS[jid]["archived"] is False


def test_archive_failed_route(client, auth):
    old = _fail(_mk("old_failure_one_clip.mp4"), age_hours=50)
    new = _fail(_mk("new_failure_two_clip.mp4"), age_hours=1)
    assert client.post("/archive-failed", headers=auth,
                       json={"older_than_hours": 24}).get_json() == {"archived": 1}
    assert api.JOBS[old]["archived"] and not api.JOBS[new]["archived"]
    assert client.post("/archive-failed", headers=auth, json={}).get_json() == {"archived": 1}


# ------------------------------------------------------------------ FTS ----
def _search(client, auth, q, extra=""):
    r = client.get(f"/jobs?q={q}{extra}", headers=auth)
    assert r.status_code == 200, r.get_data(as_text=True)
    return set(r.get_json())


def test_fts_matches_filename_url_and_metadata(client, auth):
    _need_fts()
    a = _mk("sunset_beach_vacation.mp4", url="https://cdn.example.org/a1.mp4")
    b = _mk("quarterly_report.pdf", url="https://files.contoso.net/docs/q3.pdf")
    c = _mk("lecture_one.mp4", url="https://video.example.com/l1.mp4",
            playlist_id="PL1", playlist_title="Quantum Mechanics Course")
    assert _search(client, auth, "sunset") == {a}                  # filename
    assert _search(client, auth, "contoso") == {b}                 # url
    assert _search(client, auth, "quantum") == {c}                 # metadata (playlist title)
    assert _search(client, auth, "nothingmatchesthis") == set()


def test_fts_prefix_and_multi_term_and_case(client, auth):
    _need_fts()
    a = _mk("Summer_Holiday_Photos.zip")
    b = _mk("Summer_Concert.mp4")
    assert _search(client, auth, "SUMM") == {a, b}
    assert _search(client, auth, "summer+holiday") == {a}
    assert _search(client, auth, "summer+zzz") == set()


def test_fts_url_fragments_and_punctuation_are_safe(client, auth):
    _need_fts()
    a = _mk("page_capture.mp4", url="https://www.example.com/watch?v=abc123&t=5")
    for q in ("example.com", "abc123", "watch%3Fv%3Dabc123"):
        assert _search(client, auth, q) == {a}, q
    for hostile in ('%22', "AND", "OR", "NOT", "*", "(", ")", "-", "%27%3B+DROP+TABLE+jobs%3B--",
                    "col%3Aval", "NEAR(a+b)"):
        r = client.get(f"/jobs?q={hostile}", headers=auth)
        assert r.status_code == 200, hostile
    assert vg_db.count_jobs() == 1


def test_fts_follows_renames_and_deletes(client, auth):
    _need_fts()
    jid = _mk("before_rename_clip.mp4", url="https://example.com/opaque123.mp4")
    assert _search(client, auth, "before") == {jid}
    api.JOBS[jid]["filename"] = "after_rename_clip.mp4"
    assert _search(client, auth, "before") == set()
    assert _search(client, auth, "after") == {jid}
    client.post(f"/delete/{jid}", headers=auth)
    assert _search(client, auth, "after") == set()


def test_fts_finds_a_job_added_a_moment_ago(client, auth):
    _need_fts()
    jid = _mk("freshly_added_clip.mp4")      # not flushed yet: the search must catch up
    assert _search(client, auth, "freshly") == {jid}


def test_fts_index_matches_the_table_after_many_changes():
    _need_fts()
    ids = [_mk(f"churn_item_{i}.mp4") for i in range(60)]
    for i, jid in enumerate(ids):
        if i % 3 == 0:
            api.JOBS[jid]["filename"] = f"renamed_item_{i}.mp4"
        if i % 5 == 0:
            api.JOBS.pop(jid)
    jobs_mod.flush_jobs()
    c = vg_db.conn()
    c.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('integrity-check')")   # raises when out of sync


def test_fts_search_respects_archived_and_status(client, auth):
    _need_fts()
    a = _fail(_mk("shared_word_failed.mp4"))
    b = _mk("shared_word_queued.mp4")
    client.post(f"/archive/{a}", headers=auth)
    assert _search(client, auth, "shared") == {b}
    assert _search(client, auth, "shared", "&archived=all") == {a, b}
    assert _search(client, auth, "shared", "&archived=all&status=error") == {a}


def test_search_falls_back_to_like_without_fts(client, auth, monkeypatch):
    a = _mk("fallback_search_clip.mp4")
    _mk("something_else_entirely.mp4")
    monkeypatch.setattr(vg_db, "fts_available", lambda: False)
    assert _search(client, auth, "fallback") == {a}
    assert _search(client, auth, "100%25") == set()      # % is literal, not a wildcard


def test_fts_query_builder():
    assert vg_db.build_fts_query("foo bar") == '"foo"* "bar"*'
    assert vg_db.build_fts_query('a"b') == '"a b"*'
    assert vg_db.build_fts_query("  --  ") == ""
    assert vg_db.search_job_ids("") == [] and vg_db.search_job_ids("   ") == []


# --------------------------------------------------------------- paging ----
def _many(n, prefix="paged_item"):
    ids = []
    for i in range(n):
        jid = _mk(f"{prefix}_{i}.mp4")
        api.JOBS[jid]["created_ts"] = 1_000_000.0 + i
        jobs_mod.transition(api.JOBS[jid], jobs_mod.JobEvent.STOP)    # terminal rows
        ids.append(jid)
    return ids


def test_jobs_returns_100_rows_by_default(client, auth):
    _many(250)
    r = client.get("/jobs", headers=auth)
    assert r.status_code == 200
    assert len(r.get_json()) == 100
    assert r.headers["X-Total-Count"] == "250"
    assert r.headers["X-Limit"] == "100" and r.headers["X-Offset"] == "0"
    assert r.headers["X-Has-More"] == "1"


def test_pages_are_disjoint_complete_and_newest_first(client, auth):
    ids = _many(250)
    seen, order = [], []
    for off in (0, 100, 200):
        r = client.get(f"/jobs?limit=100&offset={off}", headers=auth)
        page = r.get_json()
        seen += list(page)
        order += [j["created_ts"] for j in page.values()]
        assert r.headers["X-Has-More"] == ("1" if off < 200 else "0")
    assert len(seen) == 250 and set(seen) == set(ids)
    assert order == sorted(order, reverse=True)


def test_limit_zero_and_all_archived_is_the_old_payload(client, auth):
    ids = _many(150)
    r = client.get("/jobs?limit=0&archived=all", headers=auth)
    assert set(r.get_json()) == set(ids)
    assert r.headers["X-Has-More"] == "0"


def test_page_shape_is_unchanged_and_secrets_stay_out(client, auth):
    jid = _mk("shape_check_clip.mp4", cookie="SESSION=zzz")
    body = client.get("/jobs", headers=auth).get_json()
    assert list(body) == [jid] and body[jid]["id"] == jid
    assert "cookie" not in body[jid] and "pause_evt" not in body[jid]
    assert "zzz" not in json.dumps(body)


def test_live_jobs_lead_every_page(client, auth):
    old_ids = _many(30)
    live = _mk("live_download_clip.mp4")
    api.JOBS[live]["created_ts"] = 1.0                    # older than everything
    jobs_mod.transition(api.JOBS[live], jobs_mod.JobEvent.START)
    first = client.get("/jobs?limit=5", headers=auth).get_json()
    assert next(iter(first)) == live
    assert len(first) == 5


def test_offset_past_the_end_and_bad_arguments(client, auth):
    _many(5)
    r = client.get("/jobs?offset=50", headers=auth)
    assert r.status_code == 200 and r.get_json() == {} and r.headers["X-Has-More"] == "0"
    for bad in ("limit=abc", "limit=-1", "offset=-5", "offset=1.5", "archived=maybe"):
        assert client.get(f"/jobs?{bad}", headers=auth).status_code == 400, bad


def test_status_filter_and_paging_together(client, auth):
    ids = _many(30)
    for jid in ids[:12]:
        api.JOBS[jid]["status"] = "done"
    r = client.get("/jobs?status=done&limit=5&offset=10", headers=auth)
    assert len(r.get_json()) == 2 and r.headers["X-Total-Count"] == "12"


def test_search_results_are_paged_too(client, auth):
    _need_fts()
    _many(40, prefix="needle_item")
    _many(10, prefix="haystack_item")
    r = client.get("/jobs?q=needle&limit=15&offset=30", headers=auth)
    assert len(r.get_json()) == 10 and r.headers["X-Total-Count"] == "40"


def test_paging_headers_are_exposed_to_the_extension():
    # api.py passes expose_headers to flask-cors; check the names it lists.
    src = (Path(api.__file__)).read_text(encoding="utf-8")
    for h in ("X-Total-Count", "X-Offset", "X-Limit", "X-Has-More"):
        assert h in src


def test_jobs_route_is_fast_with_thousands_of_rows(client, auth):
    for i in range(5000):
        jid = f"{i + 1}"
        api.JOBS[jid] = {"id": jid, "url": f"https://e.com/{i}.mp4", "filename": f"f{i}.mp4",
                         "status": "done", "created_ts": float(i), "archived": False,
                         "pause_evt": threading.Event(), "stop_evt": threading.Event()}
    t0 = time.time()
    for _ in range(5):
        assert len(client.get("/jobs", headers=auth).get_json()) == 100
    assert (time.time() - t0) / 5 < 0.5


# ================================================== v13: bug-sweep fixes ===
def test_short_lived_threads_do_not_leak_connections():
    # Flask serves each request on a new thread; every one used to keep its
    # connection (and file handles) alive forever.
    import gc
    for _ in range(60):
        t = threading.Thread(target=lambda: vg_db.recent_stats(5))
        t.start()
        t.join()
    gc.collect()
    assert len(vg_db._ALL_CONNS) <= 2


def test_other_threads_recover_after_close_all():
    seen = {}
    go = threading.Event()

    def worker():
        vg_db.recent_stats(1)
        go.wait(5)
        try:
            vg_db.recent_stats(1)
            seen["ok"] = True
        except Exception as e:            # pragma: no cover
            seen["err"] = repr(e)
    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.2)
    vg_db.close_all(checkpoint=False)
    go.set()
    t.join()
    assert seen == {"ok": True}


@pytest.mark.parametrize("field,value", [
    ("size_total", 10 ** 30),                  # beyond SQLite's 64-bit integers
    ("filename", "bad\ud800name.mp4"),          # lone surrogate (seen in Windows names)
    ("filename", "nul\x00name.mp4"),
    ("created_ts", float("nan")),
])
def test_one_bad_row_never_blocks_the_others(field, value):
    good, bad = _mk("healthy_row_clip.mp4"), _mk("troubled_row_clip.mp4")
    api.JOBS[bad][field] = value
    api.JOBS[good]["size_done"] = 77
    jobs_mod.flush_jobs()
    rows = dict(_db_rows("SELECT id, size_done FROM jobs"))
    assert rows[good] == 77
    assert bad in rows


def test_a_row_sqlite_refuses_is_skipped_and_logged_not_fatal(monkeypatch):
    good, bad = _mk("fine_row_one_clip.mp4"), _mk("refused_row_two_clip.mp4")
    real = vg_db._job_params

    def picky(jid, snap, blob, now):
        if jid == bad:
            raise sqlite3.InterfaceError("simulated refusal")
        return real(jid, snap, blob, now)
    monkeypatch.setattr(vg_db, "_job_params", picky)
    jobs_mod.flush_jobs()
    assert {r[0] for r in _db_rows("SELECT id FROM jobs")} == {good}


def test_lone_surrogate_filename_round_trips_a_restart():
    jid = _mk("placeholder_clip.mp4")
    api.JOBS[jid]["filename"] = "sur\ud83dname.mp4"
    _restart()
    assert api.JOBS[jid]["filename"] == "sur\ud83dname.mp4"


def test_damaged_timestamps_do_not_break_the_list_or_the_sweep(client, auth, monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 1)
    a, b = _mk("damaged_ts_one_clip.mp4"), _mk("healthy_ts_two_clip.mp4")
    api.JOBS[a]["created_ts"] = "oops"
    assert client.get("/jobs", headers=auth).status_code == 200
    _fail(a)
    api.JOBS[a]["failed_ts"] = "bad"
    assert jobs_mod.archive_stale_errors() == 1        # treated as very old
    assert client.post("/archive-failed", headers=auth, json={}).status_code == 200


def test_corrupt_database_file_is_set_aside_and_a_new_one_started():
    settings.HOME.mkdir(parents=True, exist_ok=True)
    settings.DB_PATH.write_bytes(b"this is not an sqlite database" * 50)
    jobs_mod.load_jobs_snapshot()
    jid = _mk("after_corruption_clip.mp4")
    jobs_mod.flush_jobs()
    assert vg_db.count_jobs() == 1 and jid in api.JOBS
    assert len(list(settings.HOME.glob("videograbber.db.corrupt-*"))) >= 1


def test_unreadable_stored_row_is_skipped_but_kept():
    good = _mk("readable_row_clip.mp4")
    jobs_mod.flush_jobs()
    vg_db.write(lambda c: c.execute("INSERT INTO jobs(id, data) VALUES ('9999', '{not json')"))
    _restart()
    assert good in api.JOBS and "9999" not in api.JOBS
    assert vg_db.conn().execute("SELECT COUNT(*) FROM jobs WHERE id='9999'").fetchone()[0] == 1


def test_stress_concurrent_create_mutate_delete_stays_consistent():
    errors = []

    def churn(n):
        try:
            for i in range(60):
                jid = _mk(f"stress_{n}_{i}.mp4")
                api.JOBS[jid]["size_done"] = i
                if i % 3 == 0:
                    jobs_mod.save_jobs_snapshot()
                if i % 7 == 0:
                    api.JOBS.pop(jid, None)
                    jobs_mod.save_jobs_snapshot()
                if i % 20 == 0:
                    jobs_mod.flush_jobs()
        except Exception as e:             # pragma: no cover
            errors.append(repr(e))
    ts = [threading.Thread(target=churn, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    jobs_mod.flush_jobs()
    assert dict(_db_rows("SELECT id, size_done FROM jobs")) == \
        {k: v["size_done"] for k, v in api.JOBS.items()}
    if vg_db.fts_available():
        vg_db.conn().execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('integrity-check')")


def test_background_sweep_archives_without_a_request(monkeypatch):
    monkeypatch.setitem(settings.STATE, "hide_old_errors_hours", 1)
    jid = _fail(_mk("swept_in_background_clip.mp4"), age_hours=5)
    monkeypatch.setattr(jobs_mod, "_ARCHIVE_SWEEP_SECS", 0)
    jobs_mod.save_jobs_snapshot()
    deadline = time.time() + 5
    while time.time() < deadline and not api.JOBS[jid]["archived"]:
        time.sleep(0.05)
    assert api.JOBS[jid]["archived"] is True


@pytest.mark.parametrize("query", [
    "limit=99999999999999999999999", "offset=99999999999999999999999", "limit=%20",
    "q=" + "a" * 5000, "q=%ED%A0%80", "q=%00", "q=%E4%B8%AD%E6%96%87", "q=-",
    "status=,,,", "archived=ALL&status=QUEUED&q=route",
])
def test_jobs_route_survives_hostile_query_strings(client, auth, query):
    _mk("route_item_clip.mp4")
    assert client.get(f"/jobs?{query}", headers=auth).status_code == 200


def test_search_finds_non_ascii_names(client, auth):
    _need_fts()
    jid = _mk("\u4e2d\u6587\u89c6\u9891_caf\u00e9.mp4")
    assert _search(client, auth, "caf") == {jid}
    assert _search(client, auth, "cafe") == {jid}          # diacritics folded
    assert _search(client, auth, "%E4%B8%AD%E6%96%87%E8%A7%86%E9%A2%91") == {jid}
