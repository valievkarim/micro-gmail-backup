#!/usr/bin/env python3
"""Print one stored .eml.zst message as text, for searching the backup with ripgrep.

Headers are decoded, text parts are converted to UTF-8, and attachments appear
as one line with their type and name. With the virtualenv active:

    rg -u --pre ./eml_text.py --pre-glob '*.eml.zst' -i 'invoice' backups/you@example.com/messages
    ./eml_text.py backups/you@example.com/messages/message-<id>/<id>.<sha256>.eml.zst | less
"""

import email
import email.policy
import signal
import sys

import zstandard

# rg -l stops reading after the first match; exit quietly instead of reporting a broken pipe.
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

with open(sys.argv[1], "rb") as handle:
    raw = zstandard.ZstdDecompressor().stream_reader(handle.read(), read_across_frames=True).read()
message = email.message_from_bytes(raw, policy=email.policy.default)

for key, value in message.items():
    print(f"{key}: {value}")
print()
for part in message.walk():
    if part.get_content_maintype() == "text":
        try:
            print(part.get_content())
        except LookupError:
            # Unknown or empty charset name: decode the transfer encoding and read the bytes as UTF-8.
            print(part.get_payload(decode=True).decode("utf-8", "replace"))
    elif not part.is_multipart():
        print(f"[{part.get_content_type()} {part.get_filename() or ''}]")
