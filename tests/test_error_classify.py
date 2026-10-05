"""Tests for yt-dlp error classification and the RETRY transition (v5)."""
import threading

import pytest

from engines import classify_ytdlp_error
from jobs import JobEvent, transition


@pytest.mark.parametrize("msg,kind", [
    ("Sign in to confirm you're not a bot", "bot_check"),
    ("Sign in to confirm you\u2019re not a bot. Use --cookies", "bot_check"),
    ("Sign in to confirm your age", "fatal"),
    ("HTTP Error 429: Too Many Requests", "rate_limit"),
    ("HTTP Error 503: Service Unavailable", "network"),
    ("The read operation timed out", "network"),
    ("Video unavailable. This video is private", "fatal"),
    ("Unsupported URL: https://example.com", "fatal"),
    ("ffmpeg exited with code 1", "other"),
    ("", "other"),
])
def test_classify(msg, kind):
    assert classify_ytdlp_error(msg) == kind


def _job(status):
    return {"status": status, "pause_evt": threading.Event(),
            "stop_evt": threading.Event()}


def test_retry_from_downloading_goes_to_queued():
    j = _job("downloading")
    transition(j, JobEvent.RETRY)
    assert j["status"] == "queued"


def test_retry_from_other_states_is_illegal():
    for status in ("queued", "paused", "done", "error", "stopped"):
        with pytest.raises(ValueError):
            transition(_job(status), JobEvent.RETRY)
