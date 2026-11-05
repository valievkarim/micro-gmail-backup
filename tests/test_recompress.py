"""Throwaway end-to-end probe: recompressed bodies verify, damaged bodies still crash."""

import hashlib
from pathlib import Path

import pytest
import zstandard
from test_backup import ACCOUNT, RAW_A, raw_message, run_case, script_for, write_config, write_token

LISTING = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]


def _downloaded(tmp_path: Path) -> tuple[str, Path]:
    case = tmp_path / "case"
    case.mkdir()
    write_config(str(case))
    write_token(str(case / "auth" / f"{ACCOUNT}.json"))
    assert run_case(str(case), script_for(LISTING, {"a1": raw_message("a1", ["INBOX"], RAW_A)})).returncode == 0
    return str(case), case / "archives" / ACCOUNT / "messages" / "message-a1" / f"a1.{hashlib.sha256(RAW_A).hexdigest()}.eml.zst"


def _frames(*parts: bytes) -> bytes:
    return b"".join(zstandard.ZstdCompressor(level=19, write_checksum=True).compress(part) for part in parts)


@pytest.mark.parametrize("split", [False, True])
def test_recompressed_multi_frame_body_verifies(tmp_path: Path, split: bool) -> None:
    case, body = _downloaded(tmp_path)
    half = len(RAW_A) // 2
    body.write_bytes(_frames(RAW_A[:half], RAW_A[half:]) if split else _frames(RAW_A))

    proc = run_case(case, script_for(LISTING, {}))

    assert proc.returncode == 0, proc.stderr
    assert "[1/1] verified_on_disk a1" in proc.stderr


@pytest.mark.parametrize("damage", ["truncate", "garbage", "extra_frame", "empty"])
def test_damaged_body_crashes_and_is_preserved(tmp_path: Path, damage: str) -> None:
    case, body = _downloaded(tmp_path)
    data = body.read_bytes()
    damaged = {"truncate": data[:-5], "garbage": data + b"garbage-bytes", "extra_frame": data + _frames(b"extra"), "empty": b""}[damage]
    body.write_bytes(damaged)

    proc = run_case(case, script_for(LISTING, {}))

    assert proc.returncode != 0
    assert "Traceback (most recent call last)" in proc.stderr
    assert "backup completed" not in proc.stderr
    assert body.read_bytes() == damaged
