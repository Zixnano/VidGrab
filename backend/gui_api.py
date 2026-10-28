"""HTTP client for the local backend."""

import requests

from PySide6.QtCore import QThread, Signal

from gui_common import BACKEND_BASE, load_token


class ProbeError(Exception):
    """/probe failed. `code` is the server's machine-readable error code
    ("auth_required", "playlist_unavailable", ...) or "" when unknown."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


class ApiClient:
    def __init__(self):
        self.base = BACKEND_BASE
        self.session = requests.Session()
        self.token = load_token()

    def _headers(self):
        return {"X-API-Token": self.token} if self.token else {}

    def _req(self, method, path, **kw):
        headers = kw.pop("headers", {})
        headers.update(self._headers())
        timeout = kw.pop("timeout", 5)  # slow calls (batch, probe) pass their own
        r = self.session.request(method, self.base + path, headers=headers,
                                 timeout=timeout, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def reload_token(self):
        self.token = load_token()

    def jobs(self):
        data = self._req("GET", "/jobs") or {}
        return list(data.values())

    def add(self, url, filename=None, category=None, description=None,
            format_id=None, target_format=None, resolution=None, multi=False,
            download_playlist=False, referer=None, cookie=None, user_agent=None,
            page_url=None):
        body = {"url": url}
        if page_url:
            body["page_url"] = page_url
        if filename:
            body["filename"] = filename
        if referer:
            body["referer"] = referer
        if cookie:
            body["cookie"] = cookie
        if user_agent:
            body["user_agent"] = user_agent
        if category:
            body["category"] = category
        if description:
            body["description"] = description
        if format_id:
            body["format_id"] = format_id
        if target_format:
            body["target_format"] = target_format
        if resolution:
            body["resolution"] = resolution
        if multi:
            body["multi"] = True
        if download_playlist:
            body["download_playlist"] = True
        return self._req("POST", "/download", json=body)

    def pause(self, jid):
        return self._req("POST", f"/pause/{jid}")

    def resume(self, jid):
        return self._req("POST", f"/resume/{jid}")

    def stop(self, jid):
        return self._req("POST", f"/stop/{jid}")

    def delete(self, jid):
        return self._req("POST", f"/delete/{jid}")

    def set_category(self, jid, category):
        return self._req("POST", f"/category/{jid}", json={"category": category})

    def convert(self, jid, fmt):
        return self._req("POST", f"/convert/{jid}", json={"format": fmt})

    @staticmethod
    def _error_text(e):
        """Backend error message from a failed response, else the exception text."""
        try:
            return e.response.json().get("error") or str(e)
        except Exception:
            return str(e)

    def delete_file(self, jid):
        """Remove the job and its file (backend refuses paths outside the
        VideoGrabber / download folders)."""
        try:
            return self._req("POST", f"/delete-file/{jid}", timeout=30)
        except requests.HTTPError as e:
            raise RuntimeError(self._error_text(e)) from None

    def toolkit(self, kind, body):
        """Run /toolkit/<compress|trim|gif>. Encodes are slow: long timeout."""
        try:
            return self._req("POST", f"/toolkit/{kind}", json=body, timeout=7500)
        except requests.HTTPError as e:
            raise RuntimeError(self._error_text(e)) from None

    def probe_head(self, url):
        return self._req("POST", "/probe-head", json={"url": url})

    def probe_formats(self, url):
        return self._req("POST", "/probe-formats", json={"url": url})

    def probe_playlist(self, url, start=0, limit=200, referer=None,
                       cookie=None, user_agent=None):
        """POST /probe. Raises ProbeError carrying the server's error text
        and code (a plain raise_for_status would lose both)."""
        body = {"url": url, "start": start, "limit": limit}
        if referer:
            body["referer"] = referer
        if cookie:
            body["cookie"] = cookie
        if user_agent:
            body["user_agent"] = user_agent
        r = self.session.request("POST", self.base + "/probe",
                                 headers=self._headers(), json=body,
                                 timeout=120)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            raise ProbeError(data.get("error") or f"HTTP {r.status_code}",
                             data.get("code") or "")
        return data

    def batch(self, items, referer=None, cookie=None, user_agent=None):
        body = {"items": items}
        if referer:
            body["referer"] = referer
        if cookie:
            body["cookie"] = cookie
        if user_agent:
            body["user_agent"] = user_agent
        return self._req("POST", "/batch", json=body, timeout=60)

    def redownload(self, jid):
        return self._req("POST", f"/redownload/{jid}")

    def logs(self):
        data = self._req("GET", "/logs") or {}
        return data.get("logs", [])

    def version(self):
        data = self._req("GET", "/version") or {}
        return data.get("version", "")

    def changelog(self):
        return self._req("GET", "/changelog", timeout=12) or {}

    def disk_space(self):
        return self._req("GET", "/diskspace") or {}

    def reorder(self, jid, action):
        return self._req("POST", f"/reorder/{jid}", json={"action": action})

    def dup_check(self, url):
        return (self._req("POST", "/dup-check", json={"url": url}) or {}).get("duplicate")

    def queue_status(self):
        return self._req("GET", "/queue-status") or {}

    def update_status(self):
        return self._req("GET", "/update-status", timeout=20) or {}

    def ytdlp_update(self):
        return self._req("POST", "/ytdlp-update", timeout=10)

    def ytdlp_rollback(self):
        return self._req("POST", "/ytdlp-rollback")

    def clear_failed(self, older_than_hours=0):
        return self._req("POST", "/clear-failed",
                         json={"older_than_hours": older_than_hours})

    def save_setting(self, key, value):
        # Errors propagate: callers (toggle_queue, SettingsDialog._save)
        # show them; swallowing here made those handlers dead code.
        self._req("POST", "/settings", json={key: value})

    def save_settings(self, values):
        """Save several settings in ONE request. /settings already accepts any
        number of keys and merges dict values key by key, so this is the same
        result as calling save_setting per key, with one round trip and one
        settings.json write instead of dozens (the Settings dialog used to
        freeze the window for a moment on Save)."""
        if values:
            self._req("POST", "/settings", json=dict(values), timeout=15)

    def set_queue_running(self, running):
        # queue_running is ignored by /settings on purpose; this route is
        # the only writer.
        self._req("POST", "/queue-running", json={"running": bool(running)})


class _ApiFetchWorker(QThread):
    """Runs a blocking ApiClient call off the GUI thread (Session 11.1).
    Queued-signal delivery applies the result back in the GUI thread; the
    window waits for in-flight workers in closeEvent so the app can exit
    cleanly."""
    result = Signal(object)
    failed = Signal()
    error = Signal(str)      # v5.5: why it failed (emitted just before `failed`)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        # Workers are parented to the window so they cannot be collected while
        # running, but nothing ever deleted them afterwards: the refresh loop
        # alone leaked about two QThread objects a second for as long as the
        # app stayed open. Delete each one when it finishes.
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            self.result.emit(self._fn())
        except Exception as e:
            self.error.emit(str(e)[:200])
            self.failed.emit()
