"""export_mbox.py writes every complete stored message into one mbox, oldest first."""

import hashlib
import json
import mailbox
import subprocess
import sys
from pathlib import Path

import zstandard

EXPORT_MBOX = Path(__file__).resolve().parent.parent / "export_mbox.py"


def store(account_dir: Path, gmail_id: str, internal_date: str, raw: bytes, with_metadata: bool = True) -> None:
    sha256 = hashlib.sha256(raw).hexdigest()
    message_dir = account_dir / "messages" / f"message-{gmail_id}"
    message_dir.mkdir(parents=True)
    body_name = f"{gmail_id}.{sha256}.eml.zst"
    (message_dir / body_name).write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(raw))
    if with_metadata:
        metadata = {"id": gmail_id, "threadId": gmail_id, "internalDate": internal_date, "message_data_file": body_name, "raw_sha256": sha256}
        (message_dir / "metadata.json").write_text(json.dumps(metadata))


def test_messages_are_exported_oldest_first_with_from_lines_quoted(tmp_path: Path) -> None:
    account_dir = tmp_path / "you@example.com"
    store(account_dir, "1a", "2000000000000", b"Subject: newer\r\n\r\nFrom here on\r\n>From quoted\r\n")
    store(account_dir, "1b", "1000000000000", b"Subject: older\r\n\r\nno trailing newline")
    store(account_dir, "1c", "3000000000000", b"Subject: incomplete\r\n\r\n", with_metadata=False)
    (account_dir / "messages" / ".DS_Store").write_bytes(b"")
    mbox_path = tmp_path / "out.mbox"

    proc = subprocess.run([sys.executable, str(EXPORT_MBOX), str(account_dir), str(mbox_path)], capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, proc.stderr
    assert "exported 2 messages" in proc.stdout
    assert mbox_path.read_bytes().startswith(b"From MAILER-DAEMON Sun Sep  9 01:46:40 2001\nSubject: older\n")
    assert b"\n>From here on\n>>From quoted\n" in mbox_path.read_bytes()

    messages = list(mailbox.mbox(mbox_path))
    assert [message["Subject"] for message in messages] == ["older", "newer"]
    assert messages[0].get_payload() == "no trailing newline\n"


def test_existing_mbox_is_not_overwritten(tmp_path: Path) -> None:
    account_dir = tmp_path / "you@example.com"
    store(account_dir, "1a", "1000000000000", b"Subject: one\r\n\r\nbody\r\n")
    mbox_path = tmp_path / "out.mbox"
    mbox_path.write_bytes(b"keep")

    proc = subprocess.run([sys.executable, str(EXPORT_MBOX), str(account_dir), str(mbox_path)], capture_output=True, text=True, timeout=60)

    assert proc.returncode != 0
    assert "FileExistsError" in proc.stderr
    assert mbox_path.read_bytes() == b"keep"
