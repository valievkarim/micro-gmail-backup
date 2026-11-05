"""Throwaway probe of repeated mid-run refreshes with backoff until the deadline."""

import time
from pathlib import Path

import httpx
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from test_backup import _api_client, _expired, _quota_response, _session_for

PATH = "/gmail/v1/users/me/profile"


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: list, budget: float = 36000.0):
    now = [1000.0]
    sleeps: list = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)
    refreshes: list = []

    def fake_refresh(self, request):
        refreshes.append(True)
        self.token = f"tok-{len(refreshes)}"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = Credentials(token="tok", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token", client_id="cid", client_secret="sek")
    return _session_for(_api_client(script), creds, tmp_path / "token.json", budget), refreshes, sleeps


def _down() -> httpx.ConnectError:
    return httpx.ConnectError("down")


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def test_several_refreshes_across_long_network_outage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, refreshes, _ = _setup(monkeypatch, tmp_path, [_down(), _expired(), _down(), _down(), _expired(), _down(), _expired(), _ok()])
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert len(refreshes) == 3
    assert (tmp_path / "token.json").exists()


def test_refresh_during_ratelimit_streak_repeats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, refreshes, _ = _setup(monkeypatch, tmp_path, [_quota_response(), _expired(), _quota_response(), _expired(), _ok()])
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert len(refreshes) == 2


def test_consecutive_401s_back_off_refresh_and_recover(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, refreshes, sleeps = _setup(monkeypatch, tmp_path, [_expired(), _expired(), _expired(), _ok()])
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert len(refreshes) == 3
    assert sleeps == [1.0, 1.5, 2.25]


def test_permanent_401_stops_at_the_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    session, refreshes, sleeps = _setup(monkeypatch, tmp_path, [_expired()], budget=600.0)
    with pytest.raises(httpx.HTTPStatusError):
        session.api_get(PATH, {}, "test")
    assert f"retry budget exhausted for {PATH}: token_expired" in caplog.text
    assert 2 <= len(refreshes) < 30
    assert max(sleeps) <= 120.0


def test_rejected_refresh_aborts_at_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, _, sleeps = _setup(monkeypatch, tmp_path, [_expired(), _ok()])

    def rejected(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", rejected)
    with pytest.raises(RefreshError):
        session.api_get(PATH, {}, "test")
    assert sleeps == [1.0]


def test_any_401_body_or_header_refreshes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bare = [httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}}), httpx.Response(401, content=b"<html>no</html>"), httpx.Response(401)]
    session, refreshes, _ = _setup(monkeypatch, tmp_path, [*bare, _ok()])
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert len(refreshes) == 3


def test_recorded_real_401_refreshes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json

    recorded = json.loads((Path(__file__).parent / "fixtures" / "gmail_401_messages_get.json").read_text())["response"]
    headers = {k: v for k, v in recorded["headers"].items() if k not in ("content-encoding", "transfer-encoding")}
    session, refreshes, _ = _setup(monkeypatch, tmp_path, [httpx.Response(401, headers=headers, json=recorded["body"]), _ok()])
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert len(refreshes) == 1


def test_403_other_than_quota_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    forbidden = httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    session, refreshes, _ = _setup(monkeypatch, tmp_path, [forbidden])
    with pytest.raises(httpx.HTTPStatusError):
        session.api_get(PATH, {}, "test")
    assert refreshes == []
