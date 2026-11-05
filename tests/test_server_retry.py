"""Throwaway probe of the server class: 429 and 5xx back off under the same deadline."""

from pathlib import Path

import httpx
import pytest
from test_backup import ACCOUNT, RAW_A, _quota_response, raw_message, run_case, script_for, write_config, write_token
from test_repeated_refresh import PATH, _down, _ok, _setup


@pytest.mark.parametrize(
    "response", [httpx.Response(503), httpx.Response(500, json={"error": {"errors": [{"reason": "backendError"}]}}), httpx.Response(502, content=b"<html>bad gateway</html>"), httpx.Response(429)]
)
def test_server_errors_back_off_then_succeed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: httpx.Response) -> None:
    session, refreshes, sleeps = _setup(monkeypatch, tmp_path, [response, response, response, _ok()])
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert sleeps == [1.0, 1.5, 2.25]
    assert refreshes == []


def test_class_switch_restarts_server_backoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = [httpx.Response(503), httpx.Response(503), _quota_response(), httpx.Response(503), _down(), _ok()]
    session, _, sleeps = _setup(monkeypatch, tmp_path, script)
    assert session.api_get(PATH, {}, "test") == {"ok": True}
    assert sleeps == [1.0, 1.5, 1.0, 1.0, 1.0]


def test_persistent_server_error_stops_at_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    session, _, sleeps = _setup(monkeypatch, tmp_path, [httpx.Response(503)], budget=600.0)
    with pytest.raises(httpx.HTTPStatusError):
        session.api_get(PATH, {}, "test")
    assert f"retry budget exhausted for {PATH}: server_error (HTTP 503)" in caplog.text
    assert max(sleeps) <= 120.0


@pytest.mark.parametrize("status", [400, 404, 409])
def test_other_4xx_stay_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int) -> None:
    session, _, sleeps = _setup(monkeypatch, tmp_path, [httpx.Response(status, json={"error": {}})])
    with pytest.raises(httpx.HTTPStatusError):
        session.api_get(PATH, {}, "test")
    assert sleeps == []


def test_real_run_survives_503_on_message_get(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    write_config(str(case))
    write_token(str(case / "auth" / f"{ACCOUNT}.json"))
    unavailable = {"__status__": 503, "__body__": {"error": {"code": 503, "message": "The service is currently unavailable."}}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]

    proc = run_case(str(case), script_for(listing, {"a1": [unavailable, raw_message("a1", ["INBOX"], RAW_A)]}))

    assert proc.returncode == 0, proc.stderr
    assert "retry server_error 1 for /gmail/v1/users/me/messages/a1, waiting 1.0s (HTTP 503)" in proc.stderr
    assert "[1/1] downloaded_now a1" in proc.stderr
