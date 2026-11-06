#!/usr/bin/env python3
"""Export one account's backup to a single mbox file, for checking it in Apple Mail or Thunderbird.

    .venv/bin/python export_mbox.py backups/you@example.com you@example.com.mbox

The mbox is not a second copy of the backup: line endings become LF, body lines
starting with "From " are quoted mboxrd-style, and labels and unread/starred
flags are not exported.
"""

import hashlib
import json
import re
import sys
import time
from pathlib import Path

import zstandard


def main() -> None:
    account_dir, mbox_path = Path(sys.argv[1]), Path(sys.argv[2])

    # Directories without metadata.json are incomplete downloads and are skipped.
    sidecars = []
    for path in (account_dir / "messages").glob("message-*/metadata.json"):
        metadata = json.loads(path.read_text())
        sidecars.append((int(metadata["internalDate"]), path.parent / metadata["message_data_file"], metadata["raw_sha256"]))
    sidecars.sort()

    # "x" refuses to overwrite an existing mbox.
    with open(mbox_path, "xb") as mbox:
        for internal_date, body_path, sha256 in sidecars:
            with open(body_path, "rb") as handle:
                raw = zstandard.ZstdDecompressor().stream_reader(handle.read(), read_across_frames=True).read()
            assert hashlib.sha256(raw).hexdigest() == sha256, body_path

            raw = re.sub(rb"^(>*From )", rb">\1", raw.replace(b"\r\n", b"\n"), flags=re.MULTILINE)
            if not raw.endswith(b"\n"):
                raw += b"\n"

            mbox.write(f"From MAILER-DAEMON {time.asctime(time.gmtime(internal_date / 1000))}\n".encode())
            mbox.write(raw + b"\n")

    print(f"exported {len(sidecars)} messages to {mbox_path}")


if __name__ == "__main__":
    main()
