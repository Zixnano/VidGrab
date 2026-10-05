"""Tests for URL routing, the job state machine, queue reordering and the
duplicate-URL key (v5)."""
import threading

import pytest

import engines
import jobs
import settings
from jobs import JobEvent, JobStatus, reorder_job, start_order_key, transition


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "HOME", tmp_path)
    jobs.JOBS.clear()
    for k, v in (("per_site_engine", {}), ("preferred_engines", []),
                 ("engines_disabled", [])):
        monkeypatch.setitem(settings.STATE, k, v)
    yield
    jobs.JOBS.clear()


# ---- router -----------------------------------------------------------------
def test_default_route_lists_ytdlp():
    names = [type(e).__name__ for e in engines.route_for("https://www.youtube.com/watch?v=1")]
    assert "YtDlpEngine" in names


def test_twitch_leads_with_streamlink():
    first = engines.route_for("https://www.twitch.tv/somechannel")[0]
    assert type(first).__name__ == "StreamlinkEngine"


def test_per_site_override_wins(monkeypatch):
    monkeypatch.setitem(settings.STATE, "per_site_engine", {"example.com": "streamlink"})
    routes = engines.route_for("https://media.example.com/x")
    assert len(routes) == 1 and type(routes[0]).__name__ == "StreamlinkEngine"


def test_disabled_engine_is_excluded(monkeypatch):
    monkeypatch.setitem(settings.STATE, "engines_disabled", ["streamlink"])
    names = [type(e).__name__ for e in engines.route_for("https://www.twitch.tv/x")]
    assert "StreamlinkEngine" not in names


# ---- state machine ------------------------------------------------------------
def _job(status):
    return {"status": status, "pause_evt": threading.Event(), "stop_evt": threading.Event()}


@pytest.mark.parametrize("start,event,end", [
    ("queued", JobEvent.START, "downloading"),
    ("downloading", JobEvent.COMPLETE, "done"),
    ("downloading", JobEvent.FAIL, "error"),
    ("downloading", JobEvent.PAUSE, "paused"),
    ("downloading", JobEvent.STOP, "stopped"),
    ("downloading", JobEvent.RETRY, "queued"),
    ("error", JobEvent.RESUME, "queued"),
])
def test_valid_transitions(start, event, end):
    j = _job(start)
    transition(j, event)
    assert j["status"] == end


@pytest.mark.parametrize("start,event", [
    ("done", JobEvent.START), ("queued", JobEvent.COMPLETE), ("done", JobEvent.RETRY),
])
def test_illegal_transitions_raise(start, event):
    with pytest.raises(ValueError):
        transition(_job(start), event)


def test_fail_stamps_time_and_resume_clears_it():
    j = _job("downloading")
    transition(j, JobEvent.FAIL)
    assert j["failed_ts"]
    transition(j, JobEvent.RESUME)
    assert j["failed_ts"] is None


def test_every_status_has_a_name():
    assert {s.value for s in JobStatus} >= {"queued", "downloading", "done", "error", "paused", "stopped"}


# ---- queue order ---------------------------------------------------------------
def _queue(n):
    ids = [jobs.new_job(f"https://example.com/f{i}.zip", snapshot=False) for i in range(n)]
    return ids


def _order():
    return [j["url"][-5] for j in sorted(
        (j for j in jobs.JOBS.values() if j["status"] == "queued"), key=start_order_key)]


def test_reorder_moves_and_new_jobs_go_last():
    ids = _queue(4)
    assert _order() == ["0", "1", "2", "3"]
    assert reorder_job(ids[3], "top") == 0
    assert _order() == ["3", "0", "1", "2"]
    reorder_job(ids[0], "bottom")
    assert _order() == ["3", "1", "2", "0"]
    reorder_job(ids[2], "up")
    assert _order() == ["3", "2", "1", "0"]
    reorder_job(ids[3], "down")
    assert _order() == ["2", "3", "1", "0"]
    jobs.new_job("https://example.com/f9.zip", snapshot=False)
    assert _order()[-1] == "9"


def test_reorder_edges_and_errors():
    ids = _queue(2)
    assert reorder_job(ids[0], "up") == 0          # already first
    assert reorder_job(ids[1], "down") == 1        # already last
    jobs.JOBS[ids[0]]["status"] = "downloading"
    assert reorder_job(ids[0], "top") is None      # only queued jobs
    with pytest.raises(KeyError):
        reorder_job("nope", "top")
    with pytest.raises(ValueError):
        reorder_job(ids[1], "sideways")


# ---- duplicate key ---------------------------------------------------------------
def test_duplicate_key_normalizes_youtube_urls():
    import api
    a = api._norm_url_key("https://www.youtube.com/watch?v=abcdefghijk&t=30s")
    b = api._norm_url_key("https://youtu.be/abcdefghijk")
    c = api._norm_url_key("https://www.youtube.com/watch?v=zzzzzzzzzzz")
    assert a == b != c
    assert api._norm_url_key("https://example.com/a.zip#x") == api._norm_url_key("https://example.com/a.zip")
