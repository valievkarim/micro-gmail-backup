"""Throwaway end-to-end probe: response headers and error bodies reach the log file only."""

import base64
from pathlib import Path

from test_backup import ACCOUNT, RAW_A, raw_message, run_case, script_for, write_config, write_token


def test_debug_lines_are_off_by_default(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    write_config(str(case))
    write_token(str(case / "auth" / f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]

    proc = run_case(str(case), script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)}))

    assert proc.returncode == 0, proc.stderr
    assert " - DEBUG - " not in (case / "backup.log").read_text()
    assert " - DEBUG - " not in proc.stderr


def test_headers_logged_to_file_error_body_too_but_never_raw_mime(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    write_config(str(case))
    write_token(str(case / "auth" / f"{ACCOUNT}.json"))
    quota = {"__status__": 403, "__body__": {"error": {"errors": [{"reason": "rateLimitExceeded", "message": "Rate Limit Exceeded"}]}}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]

    proc = run_case(str(case), script_for(listing, {"a1": [quota, raw_message("a1", ["INBOX"], RAW_A)]}), args=["--debug"])

    assert proc.returncode == 0, proc.stderr
    log = (case / "backup.log").read_text()
    assert "DEBUG - 200 users.getProfile response headers {" in log
    assert "DEBUG - 200 users.messages.get response headers {" in log
    assert "DEBUG - 403 users.messages.get response headers {" in log
    assert "DEBUG - 403 users.messages.get error body" in log and "rateLimitExceeded" in log
    assert "200 users.messages.get error body" not in log
    assert base64.urlsafe_b64encode(RAW_A).decode()[:40] not in log
    assert "DEBUG - 200 users.messages.get response headers {" in proc.stderr
    assert "INFO - 200 https://gmail.googleapis.com/gmail/v1/users/me/messages/a1?" in proc.stderr
    assert "Authorization" not in log and "Bearer" not in log
