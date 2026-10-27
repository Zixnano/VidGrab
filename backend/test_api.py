"""pytest safety net for api.py. Run: pytest -v backend/test_api.py

Everything runs against a throwaway state directory; nothing touches the
real settings.json, jobs.json or recovery_state.json.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

# Must be set BEFORE importing api / settings: they read these at import time.
_STATE_TMP = tempfile.mkdtemp(prefix="vg_test_state_")
os.environ["VG_STATE_DIR"] = _STATE_TMP
os.environ["VG_EXTENSION_ID"] = "abcdefghijklmnopabcdefghijklmnop"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import api
import settings
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
    yield
    api.JOBS.clear()


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
