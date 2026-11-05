"""Throwaway end-to-end probe of crash-leftover cleanup through the real script."""

import os
from pathlib import Path

from test_backup import ACCOUNT, RAW_A, raw_message, run_case, script_for, write_config, write_token


def _case(tmp_path: Path) -> tuple[str, Path, Path]:
    case = tmp_path / "case"
    case.mkdir()
    write_config(str(case))
    write_token(str(case / "auth" / f"{ACCOUNT}.json"))
    return str(case), case / "archives" / ACCOUNT, case / "auth"


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"partial")
    return path


def test_incomplete_message_and_own_token_leftovers_are_removed(tmp_path: Path) -> None:
    case, archive, auth = _case(tmp_path)
    body_tmp = _touch(archive / "messages" / "message-a1" / ".tmp-a1.abc.eml.zst-xyz")
    own_token_tmp = _touch(auth / f".tmp-{ACCOUNT}.json-xyz")
    other_token_tmp = _touch(auth / ".tmp-other@example.com.json-xyz")

    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)}))

    assert proc.returncode == 0, proc.stderr
    assert not body_tmp.exists()
    assert not own_token_tmp.exists()
    assert other_token_tmp.exists()
    assert sorted(p.name.split(".")[-1] for p in (archive / "messages" / "message-a1").iterdir()) == ["json", "zst"]


def test_message_deleted_from_server_has_leftover_removed(tmp_path: Path) -> None:
    case, archive, _ = _case(tmp_path)
    body_tmp = _touch(archive / "messages" / "message-b2" / ".tmp-b2.abc.eml.zst-xyz")

    missing = {"__status__": 404, "__body__": {"error": {"code": 404}}}
    listing = [{"messages": [{"id": "b2", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"b2": [missing, missing, missing, missing]}))

    assert proc.returncode == 0, proc.stderr
    assert "b2 not found (404)" in proc.stderr
    assert not body_tmp.exists()


def test_verified_message_is_not_downloaded_again_because_of_cleanup(tmp_path: Path) -> None:
    case, archive, _ = _case(tmp_path)
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    assert run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)})).returncode == 0

    proc = run_case(case, script_for(listing, {}))

    assert proc.returncode == 0, proc.stderr
    assert "[1/1] verified_on_disk a1" in proc.stderr
    assert not [name for _, _, names in os.walk(archive) for name in names if name.startswith(".tmp-")]
