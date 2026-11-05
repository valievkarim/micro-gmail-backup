"""Throwaway end-to-end probe of the changed-hash crash through the real script."""

import hashlib
from pathlib import Path

import zstandard
from test_backup import ACCOUNT, RAW_A, raw_message, run_case, script_for, write_config, write_token

LISTING = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]


def _case(tmp_path: Path) -> tuple[str, Path]:
    case = tmp_path / "case"
    case.mkdir()
    write_config(str(case))
    write_token(str(case / "auth" / f"{ACCOUNT}.json"))
    return str(case), case / "archives" / ACCOUNT / "messages" / "message-a1"


def _body(raw: bytes) -> str:
    return f"a1.{hashlib.sha256(raw).hexdigest()}.eml.zst"


def test_same_hash_redownload_after_lost_sidecar_succeeds(tmp_path: Path) -> None:
    case, directory = _case(tmp_path)
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(LISTING, gets)).returncode == 0
    (directory / "metadata.json").unlink()

    proc = run_case(case, script_for(LISTING, gets))

    assert proc.returncode == 0, proc.stderr
    assert "[1/1] downloaded_now a1" in proc.stderr
    assert sorted(path.name for path in directory.iterdir()) == sorted([_body(RAW_A), "metadata.json"])


def test_damaged_body_with_lost_sidecar_is_replaced_by_redownload(tmp_path: Path) -> None:
    case, directory = _case(tmp_path)
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(LISTING, gets)).returncode == 0
    (directory / "metadata.json").unlink()
    body = directory / _body(RAW_A)
    body.write_bytes(body.read_bytes()[:-1])

    proc = run_case(case, script_for(LISTING, gets))

    assert proc.returncode == 0, proc.stderr
    assert "[1/1] downloaded_now a1" in proc.stderr
    assert zstandard.decompress(body.read_bytes()) == RAW_A
