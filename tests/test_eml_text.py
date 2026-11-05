"""eml_text.py prints a stored message with headers and text parts MIME-decoded."""

import subprocess
import sys
from pathlib import Path

import zstandard

EML_TEXT = Path(__file__).resolve().parent.parent / "eml_text.py"

MESSAGE = (
    b"From: =?UTF-8?B?0JjQstCw0L0=?= <ivan@example.com>\r\n"
    b"Subject: =?KOI8-R?B?097F1A==?= 42\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=windows-1251\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"8fe48iDu7+vg9+Xt\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"soft=\r\nbreak caf=C3=A9\r\n"
    b"--B\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="invoice.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERi0=\r\n"
    b"--B--\r\n"
)


def test_headers_and_text_parts_are_decoded(tmp_path: Path) -> None:
    body = tmp_path / "m.eml.zst"
    half = len(MESSAGE) // 2
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    body.write_bytes(compressor.compress(MESSAGE[:half]) + compressor.compress(MESSAGE[half:]))

    proc = subprocess.run([sys.executable, str(EML_TEXT), str(body)], capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, proc.stderr
    assert "From: Иван <ivan@example.com>" in proc.stdout
    assert "Subject: счет 42" in proc.stdout
    assert "счёт оплачен" in proc.stdout
    assert "softbreak café" in proc.stdout
    assert "[application/pdf invoice.pdf]" in proc.stdout
    assert "JVBERi0" not in proc.stdout
