"""Subprocess fixture: inject an httpx mock transport via sitecustomize.

micro_gmail_backup.py itself has no test hooks. Each run executes the real script in a
subprocess whose PYTHONPATH provides a sitecustomize that routes all of the
script's httpx traffic through a scripted MockTransport, answers the Google
OAuth refresh endpoint inside the requests transport, and optionally injects
local I/O faults. Every HTTP exchange is logged for assertions.
The script reads <directory>/config.toml through MICRO_GMAIL_BACKUP_CONFIG, and
its file log is redirected by default to <directory>/backup.log through
MICRO_GMAIL_BACKUP_LOG, so tests never touch the real config or log.
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP = os.path.join(ROOT, "micro_gmail_backup.py")

_SITE = """\
import json as _json
import os as _os

_SCRIPT = {script}
_LOG = {log}


def _respond(request):
    with open(_LOG, "a", encoding="utf-8") as _f:
        _f.write(_json.dumps({"method": request.method, "path": request.url.path, "params": dict(request.url.params)}) + "\\n")
    _path = request.url.path
    if _path == "/gmail/v1/users/me/profile":
        return 200, _SCRIPT["profile"]
    if _path == "/gmail/v1/users/me/labels":
        return 200, _SCRIPT["labels"]
    if _path == "/gmail/v1/users/me/messages":
        _calls = _SCRIPT.get("_list_calls", 0)
        _item = _SCRIPT["list"][_calls]
        _SCRIPT["_list_calls"] = _calls + 1
        if isinstance(_item, dict) and "__status__" in _item:
            return _item["__status__"], _item.get("__body__", {"error": "x"})
        return 200, _item
    _mid = _path.rsplit("/", 1)[-1]
    _entry = _SCRIPT["get"].get(_mid, {"__status__": 404, "__body__": {"error": {}}})
    if isinstance(_entry, list):
        _entry = _entry.pop(0) if len(_entry) > 1 else _entry[0]
    if isinstance(_entry, dict) and "__raise__" in _entry:
        raise KeyboardInterrupt("injected abort")
    if isinstance(_entry, dict) and "__raise_transport__" in _entry:
        import httpx as _httpx_err

        raise _httpx_err.TransportError("injected transport failure")
    if isinstance(_entry, dict) and "__status__" in _entry:
        return _entry["__status__"], _entry.get("__body__", {"error": "x"})
    return 200, _entry


def _dispatch(request):
    import httpx as _httpx

    _status, _payload = _respond(request)
    if isinstance(_payload, dict) and "__sleep__" in _payload:
        import time as _time

        _time.sleep(_payload.pop("__sleep__"))
    if isinstance(_payload, dict) and "__bad_gzip__" in _payload:
        # A stream, so the body is decoded while the client reads it, as off the network.
        return _httpx.Response(_status, headers={"content-encoding": "gzip"}, stream=_httpx.ByteStream(b"not gzip"))
    if isinstance(_payload, dict) and "__text__" in _payload:
        return _httpx.Response(_status, text=_payload["__text__"])
    return _httpx.Response(_status, json=_payload)


if _os.environ.get("MICRO_GMAIL_MOCK") == "1":
    import httpx as _httpx

    _orig_init = _httpx.Client.__init__

    def _init(self, *args, **kwargs):
        kwargs.setdefault("transport", _httpx.MockTransport(_dispatch))
        _orig_init(self, *args, **kwargs)

    _httpx.Client.__init__ = _init

    import requests as _rq

    _orig_request = _rq.sessions.Session.request

    def _request(self, method, url, *args, **kwargs):
        if "oauth2.googleapis.com/token" in str(url):
            with open(_LOG, "a", encoding="utf-8") as _f:
                _f.write(_json.dumps({"method": method, "path": "/__refresh__", "params": {}}) + "\\n")
            _resp = _rq.Response()
            _rcalls = _SCRIPT.get("_refresh_calls", 0)
            _SCRIPT["_refresh_calls"] = _rcalls + 1
            if _SCRIPT.get("__refresh_flaky__") and _rcalls == 0:
                raise _rq.exceptions.ConnectionError("injected refresh transport failure")
            if _SCRIPT.get("__refresh_fail__"):
                _resp.status_code = 400
                _resp._content = b'{"error": "invalid_grant"}'
                _resp.headers["Content-Type"] = "application/json"
                _resp.url = url
                return _resp
            if _SCRIPT.get("__refresh_text__"):
                _resp.status_code = 200
                _resp._content = _SCRIPT["__refresh_text__"].encode()
                _resp.url = url
                return _resp
            _resp.status_code = 200
            _resp._content = b'{"access_token": "fresh-token", "expires_in": 3600, "token_type": "Bearer"}'
            _resp.headers["Content-Type"] = "application/json"
            _resp.url = url
            return _resp
        return _orig_request(self, method, url, *args, **kwargs)

    _rq.sessions.Session.request = _request

    if _SCRIPT.get("__fault_fsync__"):

        def _bad_fsync(fd):
            raise OSError("injected fsync failure")

        _os.fsync = _bad_fsync

    if _SCRIPT.get("__fault_replace__"):
        _orig_replace = _os.replace

        def _bad_replace(src, dst):
            raise OSError("injected replace failure")

        _os.replace = _bad_replace

    if _SCRIPT.get("__interrupt_metadata__"):
        _orig_replace = _os.replace

        def _interrupt_replace(src, dst):
            if _os.path.basename(str(dst)) == "metadata.json":
                raise KeyboardInterrupt("injected abort between body and sidecar")
            return _orig_replace(src, dst)

        _os.replace = _interrupt_replace

    if _SCRIPT.get("__consent__"):
        import google_auth_oauthlib.flow as _flow_mod
        from google.oauth2.credentials import Credentials as _Creds

        class _FakeFlow:
            @staticmethod
            def from_client_secrets_file(*args, **kwargs):
                with open(_LOG, "a", encoding="utf-8") as _f:
                    _f.write(_json.dumps({"method": "GET", "path": "/__consent__", "params": {}}) + "\\n")
                return _FakeFlow()

            def run_local_server(self, *args, **kwargs):
                with open(_LOG, "a", encoding="utf-8") as _f:
                    _f.write(_json.dumps({"method": "GET", "path": "/__consent_server__", "params": kwargs}) + "\\n")
                return _Creds(
                    token="consented-token",
                    refresh_token="consented-refresh",
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id="cid",
                    client_secret="sek",
                )

        _flow_mod.InstalledAppFlow = _FakeFlow
"""


def write_token(path: str, expired: bool = False, token: str = "tok", refresh: str = "rt") -> None:
    """Write a pre-authorized token fixture; never a real credential."""
    expiry = "2000-01-01T00:00:00" if expired else "2099-01-01T00:00:00"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "token": token,
        "refresh_token": refresh,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "sek",
        "expiry": expiry,
        "universe_domain": "googleapis.com",
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def write_client_secret(path: str) -> None:
    """Write a dummy installed-app client-secret fixture; never a real secret."""
    payload = {
        "installed": {
            "client_id": "cid",
            "client_secret": "sek",
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def write_config(
    directory: str,
    email: str = "u@example.com",
    secret: str | None = "client-secret.json",
    budget: float | None = None,
) -> str:
    """Write a case-local TOML config with explicit case-local base dirs."""
    path = os.path.join(directory, "config.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"account_email = {json.dumps(email)}\narchives_dir = {json.dumps(os.path.join(directory, 'archives'))}\nauth_dir = {json.dumps(os.path.join(directory, 'auth'))}\n")
        if secret is not None:
            handle.write(f"client_secret_file = {json.dumps(os.path.join(directory, secret))}\n")
        if budget is not None:
            handle.write(f"retry_budget_seconds = {budget!r}\n")
    return path


def run_case(directory: str, script: dict, timeout: int = 120, args: list | None = None, extra_env: dict | None = None, backup: str = BACKUP) -> "subprocess.CompletedProcess[str]":
    """Run the real micro_gmail_backup.py (or a copy at backup) in directory with a scripted mock transport."""
    assert isinstance(directory, str) and isinstance(script, dict)
    fixture = os.path.join(directory, "fixture")
    os.makedirs(fixture, exist_ok=True)
    log = os.path.join(fixture, "requests.jsonl")
    site = _SITE.replace("{script}", repr(script)).replace("{log}", json.dumps(log))
    with open(os.path.join(fixture, "sitecustomize.py"), "w", encoding="utf-8") as handle:
        handle.write(site)
    env = dict(os.environ)
    env["PYTHONPATH"] = fixture + os.pathsep + env.get("PYTHONPATH", "")
    env["MICRO_GMAIL_MOCK"] = "1"
    env["MICRO_GMAIL_BACKUP_CONFIG"] = os.path.join(directory, "config.toml")
    env["MICRO_GMAIL_BACKUP_LOG"] = os.path.join(directory, "backup.log")
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    env.pop("MICRO_GMAIL_BACKUP_DEBUG", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, backup] + list(args or []), cwd=directory, env=env, capture_output=True, text=True, timeout=timeout)


def popen_case(directory: str, script: dict) -> "subprocess.Popen[str]":
    """Start the real micro_gmail_backup.py asynchronously for lock-holding tests."""
    assert isinstance(directory, str) and isinstance(script, dict)
    fixture = os.path.join(directory, "fixture")
    os.makedirs(fixture, exist_ok=True)
    log = os.path.join(fixture, "requests.jsonl")
    site = _SITE.replace("{script}", repr(script)).replace("{log}", json.dumps(log))
    with open(os.path.join(fixture, "sitecustomize.py"), "w", encoding="utf-8") as handle:
        handle.write(site)
    env = dict(os.environ)
    env["PYTHONPATH"] = fixture + os.pathsep + env.get("PYTHONPATH", "")
    env["MICRO_GMAIL_MOCK"] = "1"
    env["MICRO_GMAIL_BACKUP_CONFIG"] = os.path.join(directory, "config.toml")
    env["MICRO_GMAIL_BACKUP_LOG"] = os.path.join(directory, "backup.log")
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    return subprocess.Popen([sys.executable, BACKUP], cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def read_log(directory: str) -> list:
    """Read the recorded HTTP requests for one run."""
    log = os.path.join(directory, "fixture", "requests.jsonl")
    if not os.path.isfile(log):
        return []
    with open(log, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def raw_message(message_id: str, labels: list, body: bytes, thread: str = "f1") -> dict:
    """Build a Gmail raw get payload."""
    import base64

    return {"id": message_id, "threadId": thread, "labelIds": labels, "internalDate": "1761732000000", "raw": base64.urlsafe_b64encode(body).decode("ascii")}
