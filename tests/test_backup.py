"""Acceptance tests: real script subprocesses against an injected mock transport."""

import base64
import hashlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import time
from email import message_from_bytes
from pathlib import Path

import httpx
import pytest
import zstandard
from beartype.roar import BeartypeCallHintParamViolation
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

import micro_gmail_backup
from tests.mockmail import raw_message, read_log, run_case, write_client_secret, write_config, write_token

ACCOUNT = "u@example.com"
ATTACHMENT_BYTES = bytes(range(256))
RAW_A = (
    b"From: a@example.com\r\n"
    b"To: u@example.com\r\n"
    b"Subject: hello\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="BOUNDARY42"\r\n'
    b"\r\n"
    b"--BOUNDARY42\r\n"
    b'Content-Type: text/plain; charset="utf-8"\r\n'
    b"\r\n"
    b"body A\r\n"
    b"--BOUNDARY42\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b'Content-Disposition: attachment; filename="data.bin"\r\n'
    b"\r\n" + base64.b64encode(ATTACHMENT_BYTES) + b"\r\n--BOUNDARY42--\r\n"
)
RAW_B = b"From: b@example.com\r\nTo: u@example.com\r\nSubject: second\r\n\r\nbody B"


def script_for(mailbox: list, gets: dict, labels: dict | None = None, profile: dict | None = None) -> dict:
    return {
        "profile": profile or {"emailAddress": ACCOUNT, "messagesTotal": sum(len(page.get("messages") or []) for page in mailbox)},
        "labels": labels or {"labels": []},
        "list": mailbox,
        "get": gets,
    }


def _decompressed(path: str) -> bytes:
    """Decompress one checksum-enabled frame written by the backup."""
    with open(path, "rb") as handle:
        return zstandard.decompress(handle.read())


def test_functions_are_typed_and_beartype_rejects_wrong_argument_types(tmp_path: Path) -> None:
    assert tmp_path is not None
    members = [(name, func) for name, func in vars(micro_gmail_backup).items() if inspect.isfunction(func) and getattr(func, "__module__", "") == "micro_gmail_backup"]
    assert len(members) >= 8
    for name, func in members:
        hints = func.__annotations__
        assert set(hints) == set(inspect.signature(func).parameters) | {"return"}, name
    bad_calls = [
        (micro_gmail_backup.atomic_write, (123, b"x")),
        (micro_gmail_backup.write_json, (123, {})),
        (micro_gmail_backup.read_json, (123,)),
        (micro_gmail_backup.valid_id, (123,)),
        (micro_gmail_backup.decode_raw, (42,)),
        (micro_gmail_backup.failure_class, (123,)),
        (micro_gmail_backup.page_ids, (123,)),
        (micro_gmail_backup.unwrap_saved_page, (123, "page-000001.json")),
        (micro_gmail_backup.MessageMetadata.from_dict, (123, Path("/x"))),
        (micro_gmail_backup.MicroGmailBackup.verify_pair, (None, 123)),
        (micro_gmail_backup.MicroGmailBackup, ("u@example.com", "/a", "/b", "c", "/d", "notfloat")),
        (micro_gmail_backup.MicroGmailBackup, ("u@example.com", "/a", "/b", "/c", "/d", "notfloat")),
    ]
    for func, args in bad_calls:
        with pytest.raises(BeartypeCallHintParamViolation):
            func(*args)
    with pytest.raises((BeartypeCallHintParamViolation, TypeError)):
        micro_gmail_backup.main("unexpected")


def test_lists_all_pages_with_spam_trash_and_saves_request_evidence(tmp_path: Path) -> None:
    first = {"messages": [{"id": "a1", "threadId": "f1"}], "nextPageToken": "p2", "resultSizeEstimate": 3}
    second = {"messages": [{"id": "b2", "threadId": "f2"}, {"id": "c3", "threadId": "f3"}], "resultSizeEstimate": 3}
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(
        case, script_for([first, second], {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2"), "c3": raw_message("c3", ["INBOX"], RAW_A, thread="f3")})
    )
    assert proc.returncode == 0, proc.stderr
    log = read_log(case)
    listings = [entry for entry in log if entry["path"] == "/gmail/v1/users/me/messages"]
    assert len(listings) == 2
    assert listings[0]["params"].get("maxResults") == "500"
    assert listings[0]["params"].get("includeSpamTrash") == "true"
    assert "pageToken" not in listings[0]["params"]
    assert listings[1]["params"].get("pageToken") == "p2"
    run_dirs = os.listdir(os.path.join(archive, "listings"))
    assert len(run_dirs) == 1
    pages = sorted(os.listdir(os.path.join(archive, "listings", run_dirs[0])))
    assert pages == ["labels.json", "page-000001.json", "page-000002.json"]
    stored = json.load(open(os.path.join(archive, "listings", run_dirs[0], "page-000001.json"), encoding="utf-8"))
    assert stored["response"] == first
    assert stored["request"]["params"]["maxResults"] == "500"
    assert "observed_at" in stored
    list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=500&includeSpamTrash=true&fields=nextPageToken%2Cmessages%2Fid%2Cmessages%2FthreadId%2CresultSizeEstimate"
    get_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/a1?format=raw&fields=id%2CthreadId%2ClabelIds%2CinternalDate%2Craw"
    assert f"> GET {list_url} (users.messages.list page 1)" in proc.stderr
    assert f"> GET {list_url}&pageToken=p2 (users.messages.list page 2)" in proc.stderr
    assert f"200 {list_url}&pageToken=p2 (users.messages.list page 2)" in proc.stderr
    assert f"> GET {get_url} (users.messages.get)" in proc.stderr
    assert f"200 {get_url} (users.messages.get)" in proc.stderr
    assert "HTTP Request:" not in proc.stderr


def test_empty_listing_is_distinct_from_failed_or_malformed_listing(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "empty")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(case, script_for([{}], {}))
    assert proc.returncode == 0, proc.stderr

    def run_again(name: str, page: object) -> "subprocess.CompletedProcess[str]":
        other = os.path.join(str(tmp_path), name)
        os.makedirs(other)
        write_config(other)
        write_token(os.path.join(other, "auth", f"{ACCOUNT}.json"))
        return run_case(other, script_for([page], {}))

    failing = run_again("failed", {"__status__": 403, "__body__": {"error": {}}})
    assert failing.returncode == 1
    failed_calls = [entry for entry in read_log(os.path.join(str(tmp_path), "failed")) if entry["path"] == "/gmail/v1/users/me/messages"]
    assert len(failed_calls) == 1
    malformed = run_again("malformed", {"messages": "not-a-list"})
    assert malformed.returncode == 1
    assert "not-a-list" not in malformed.stdout
    threadless = run_again("threadless", {"messages": [{"id": "a1"}], "resultSizeEstimate": 1})
    assert threadless.returncode == 1
    assert "KeyError: 'threadId'" in threadless.stderr
    bad_thread = run_again("bad-thread", {"messages": [{"id": "a1", "threadId": "T1"}], "resultSizeEstimate": 1})
    assert bad_thread.returncode == 1
    assert "Gmail ID is not canonical lowercase hex: 'T1'" in bad_thread.stderr


def test_repeated_page_token_fails_without_looping(tmp_path: Path) -> None:
    page = {"messages": [{"id": "a1", "threadId": "f1"}], "nextPageToken": "again", "resultSizeEstimate": 9}
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(case, script_for([page, page, page, page, page], {"a1": raw_message("a1", [], RAW_A)}))
    assert proc.returncode == 1
    log = read_log(case)
    listings = [entry for entry in log if entry["path"] == "/gmail/v1/users/me/messages"]
    assert len(listings) == 2


def test_partial_listing_persists_ids_but_downloads_no_bodies(tmp_path: Path) -> None:
    page1: dict = {"messages": [{"id": "a1", "threadId": "f1"}], "nextPageToken": "p2", "resultSizeEstimate": 2}
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    script = script_for([page1, {"__status__": 403, "__body__": {"error": {}}}], {})
    proc = run_case(case, script)
    assert proc.returncode == 1
    listing_calls = [entry for entry in read_log(case) if entry["path"] == "/gmail/v1/users/me/messages"]
    assert len(listing_calls) == 2
    run_dirs = os.listdir(os.path.join(archive, "listings"))
    assert len(run_dirs) == 1
    pages = os.listdir(os.path.join(archive, "listings", run_dirs[0]))
    assert "page-000001.json" in pages and "page-000002.json" not in pages
    assert os.listdir(os.path.join(archive, "messages")) == []


def test_prior_pages_keep_unattempted_ids_after_interruption_and_remote_deletion(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    gets: dict = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": {"__raise__": "KeyboardInterrupt"}}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 130, proc.stderr
    run2 = script_for(
        [{"messages": [{"id": "c3", "threadId": "f3"}], "resultSizeEstimate": 1}],
        {"c3": raw_message("c3", ["INBOX"], RAW_A, thread="f3"), "b2": {"__status__": 404, "__body__": {}}, "a1": raw_message("a1", ["INBOX"], RAW_A)},
    )
    proc2 = run_case(case, run2)
    assert proc2.returncode == 0, proc2.stderr
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", f"a1.{hashlib.sha256(RAW_A).hexdigest()}.eml.zst"))
    assert any("b2" in line for line in proc2.stderr.splitlines())
    assert "b2 not found (404)" in proc2.stderr


def test_interrupt_still_prints_stats_so_far(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    gets: dict = {"a1": {"__raise__": "KeyboardInterrupt"}, "b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2")}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 130, proc.stderr
    stopped_at = proc.stderr.find("stopped after 1 of 2 messages; the stats below cover only those")
    stats_at = proc.stderr.find("newly found messages: 1")
    interrupted_at = proc.stderr.find(" - ERROR - interrupted")
    assert -1 not in (stopped_at, stats_at, interrupted_at)
    assert stopped_at < stats_at < interrupted_at
    assert " - downloaded now: 1" in proc.stderr
    assert "backup completed" not in proc.stderr


def test_raw_mime_and_attachments_round_trip_exactly(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(case, script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": raw_message("a1", ["INBOX"], RAW_A)}))
    assert proc.returncode == 0, proc.stderr
    body = os.path.join(archive, "messages", "message-a1", f"a1.{hashlib.sha256(RAW_A).hexdigest()}.eml.zst")
    decompressed = _decompressed(body)
    assert decompressed == RAW_A
    assert b"\r\n" in decompressed
    parts = [part for part in message_from_bytes(decompressed).walk() if part.get_content_disposition() == "attachment"]
    assert len(parts) == 1
    assert parts[0].get_payload(decode=True) == ATTACHMENT_BYTES


def test_message_directory_and_body_filename_contain_id_and_raw_sha256(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(case, script_for([{"messages": [{"id": "ab12", "threadId": "f1"}], "resultSizeEstimate": 1}], {"ab12": raw_message("ab12", ["INBOX"], RAW_B)}))
    assert proc.returncode == 0, proc.stderr
    digest = hashlib.sha256(RAW_B).hexdigest()
    assert os.path.isfile(os.path.join(archive, "messages", "message-ab12", f"ab12.{digest}.eml.zst"))
    assert os.path.isfile(os.path.join(archive, "messages", "message-ab12", "metadata.json"))


def test_metadata_replacement_is_atomic_and_preserves_existing_bodies(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    body_path = os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")
    before = open(body_path, "rb").read()
    os.remove(os.path.join(archive, "messages", "message-a1", "metadata.json"))
    assert run_case(case, script_for(listing, gets)).returncode == 0
    assert open(body_path, "rb").read() == before
    assert _decompressed(body_path) == RAW_A


def test_reacquisition_with_different_hash_preserves_both_bodies_and_fails(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    assert run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)})).returncode == 0
    first_digest = hashlib.sha256(RAW_A).hexdigest()
    os.remove(os.path.join(archive, "messages", "message-a1", "metadata.json"))
    # Same ID with different bytes is invalid Gmail-side behavior: both bodies stay on disk, then the run crashes.
    proc = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_B)}))
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    assert "message hash changed" in proc.stderr
    assert "backup completed" not in proc.stderr
    second_digest = hashlib.sha256(RAW_B).hexdigest()
    assert first_digest != second_digest
    names = sorted(os.listdir(os.path.join(archive, "messages", "message-a1")))
    assert names == sorted([f"a1.{first_digest}.eml.zst", f"a1.{second_digest}.eml.zst", "metadata.json"])
    again = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_B)}))
    assert again.returncode == 1
    assert "message hash changed" in again.stderr


def test_unpadded_base64url_is_accepted_and_invalid_encoding_is_rejected(tmp_path: Path) -> None:
    assert micro_gmail_backup.decode_raw(base64.urlsafe_b64encode(b"x").decode("ascii").rstrip("=")) == b"x"
    assert micro_gmail_backup.decode_raw(base64.urlsafe_b64encode(b"xyz").decode("ascii")) == b"xyz"
    for bad in ("ab+c", "ab/c", "abcde", "abcd===", "ab=d", "", "ab=d!!!", "QQ=", "QR==", "QR"):
        try:
            micro_gmail_backup.decode_raw(bad)
        except (AssertionError, ValueError):
            continue
        raise AssertionError(f"accepted invalid base64url: {bad!r}")
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    payload = raw_message("a1", ["INBOX"], RAW_A)
    payload["raw"] = "not valid!!"
    proc = run_case(case, script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": payload}))
    assert proc.returncode == 1


def test_mismatched_account_or_message_id_is_rejected(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    wrong = script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": raw_message("a1", ["INBOX"], RAW_A)})
    wrong["profile"] = {"emailAddress": "someone-else@example.com"}
    proc = run_case(case, wrong)
    assert proc.returncode == 1
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    assert f"token belongs to someone-else@example.com, not {ACCOUNT}; delete {token_file} and run again to sign in" in proc.stderr
    assert os.listdir(os.path.join(archive, "listings")) == []
    assert os.listdir(os.path.join(archive, "messages")) == []
    other = os.path.join(str(tmp_path), "other")
    os.makedirs(other)
    other_archive = os.path.join(other, "archives", ACCOUNT)
    write_config(other)
    write_token(os.path.join(other, "auth", f"{ACCOUNT}.json"))
    payload = raw_message("b2", ["INBOX"], RAW_A, thread="f9")
    proc = run_case(other, script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": payload}))
    assert proc.returncode == 1
    assert os.listdir(os.path.join(other_archive, "messages")) == []


def test_invalid_message_id_cannot_escape_archive_directory(tmp_path: Path) -> None:
    assert tmp_path is not None
    with pytest.raises(AssertionError):
        micro_gmail_backup.valid_id("../evil")
    with pytest.raises(AssertionError):
        micro_gmail_backup.valid_id("NOT-HEX!")
    with pytest.raises(AssertionError):
        micro_gmail_backup.valid_id("AB12")


def test_rerun_verifies_and_skips_existing_bodies(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    before = open(os.path.join(case, "fixture", "requests.jsonl")).read()
    assert '"format": "raw"' in before
    os.remove(os.path.join(case, "fixture", "requests.jsonl"))
    assert run_case(case, script_for(listing, gets)).returncode == 0
    after = open(os.path.join(case, "fixture", "requests.jsonl")).read()
    assert '"format": "raw"' not in after
    assert '"format": "minimal"' not in after


def test_only_message_prefixed_entries_are_message_directories(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    messages = os.path.join(case, "archives", ACCOUNT, "messages")
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    for name in [".DS_Store", "notes.txt"]:
        Path(messages, name).write_text("stray")
    os.makedirs(os.path.join(messages, "b2"))
    assert run_case(case, script_for(listing, gets)).returncode == 0
    os.makedirs(os.path.join(messages, "message-NOT-HEX"))
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "Gmail ID is not canonical lowercase hex: 'NOT-HEX'" in proc.stderr


def test_missing_sidecar_is_redownloaded_and_missing_body_fails(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    os.remove(os.path.join(archive, "messages", "message-a1", "metadata.json"))
    assert run_case(case, script_for(listing, gets)).returncode == 0
    assert _decompressed(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")) == RAW_A
    # A sidecar without its body cannot come from an interrupted write, so it is corruption.
    os.remove(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst"))
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "FileNotFoundError" in proc.stderr
    assert "backup completed" not in proc.stderr
    assert os.listdir(os.path.join(archive, "messages", "message-a1")) == ["metadata.json"]


def test_interruption_between_body_and_sidecar_is_resumable(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    interrupted = script_for(listing, gets)
    interrupted["__interrupt_metadata__"] = True
    proc = run_case(case, interrupted)
    assert proc.returncode == 130, proc.stderr
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst"))
    assert not os.path.isfile(os.path.join(archive, "messages", "message-a1", "metadata.json"))
    assert run_case(case, script_for(listing, gets)).returncode == 0
    assert _decompressed(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")) == RAW_A
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", "metadata.json"))


def test_corrupt_truncated_or_trailing_data_is_preserved_and_fails(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    body_path = os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")
    with open(body_path, "rb") as handle:
        frame = handle.read()
    for tail in range(1, len(frame)):
        with open(os.path.join(str(tmp_path), "probe.eml.zst"), "wb") as handle:
            handle.write(frame[:tail])
        with open(os.path.join(str(tmp_path), "probe.eml.zst"), "rb") as handle:
            probe = handle.read()
        decoder = zstandard.ZstdDecompressor().decompressobj()
        try:
            decoder.decompress(probe)
        except zstandard.ZstdError:
            continue
        assert not decoder.eof, f"truncated prefix of {tail} bytes decoded as a complete frame"
    with open(body_path, "wb") as handle:
        handle.write(frame + b"junk")
    assert run_case(case, script_for(listing, gets)).returncode == 1
    with open(body_path, "rb") as handle:
        assert handle.read() == frame + b"junk"


def test_historical_local_corruption_is_detected(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    body_path = os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")
    with open(body_path, "r+b") as handle:
        handle.seek(10)
        handle.write(b"\xff")
    assert run_case(case, script_for([{}], {})).returncode == 1
    with open(body_path, "rb") as handle:
        corrupted = handle.read()
    assert len(corrupted) > 10 and corrupted[10:11] == b"\xff"


def test_write_fsync_or_replace_failure_exits_nonzero(tmp_path: Path) -> None:
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    for fault in ("__fault_fsync__", "__fault_replace__"):
        case = os.path.join(str(tmp_path), f"case-{fault}")
        os.makedirs(case)
        archive = os.path.join(case, "archives", ACCOUNT)
        write_config(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        script = script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)})
        script[fault] = True
        proc = run_case(case, script)
        assert proc.returncode == 1, (fault, proc.stderr)
        assert "Traceback (most recent call last)" in proc.stderr, (fault, proc.stderr)
        bodies = [os.path.join(root, name) for root, _dirs, files in os.walk(archive) for name in files if name.endswith(".eml.zst")]
        assert bodies == [], (fault, bodies)


def test_message_failure_aborts_run_and_is_retried_next_run(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    failing = {"__status__": 403, "__body__": {"error": {"code": 403, "errors": [{"reason": "forbidden", "message": "no"}]}}}
    proc = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": failing}))
    assert proc.returncode == 1
    assert "Traceback (most recent call last)" in proc.stderr
    assert "backup completed" not in proc.stderr
    b2_calls = [entry for entry in read_log(case) if entry["path"].endswith("/b2")]
    assert len(b2_calls) == 1
    assert not any(entry["path"].endswith("/a1") for entry in read_log(case)), "run continued past the failed message"
    assert not os.path.exists(os.path.join(archive, "messages", "message-a1"))
    assert not os.path.exists(os.path.join(archive, "messages", "message-b2"))
    fixed = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2")}
    assert run_case(case, script_for(listing, fixed)).returncode == 0
    names = sorted(os.listdir(os.path.join(archive, "messages", "message-a1")))
    digest_a = hashlib.sha256(RAW_A).hexdigest()
    assert names == sorted([f"a1.{digest_a}.eml.zst", "metadata.json"])


def _failure_class(status: int | Exception, body: object = None, raw: bytes | None = None, url: str = "https://gmail.googleapis.com/test") -> str | None:
    """Run failure_class on one HTTP error response or transport failure and return the class name."""
    if isinstance(status, Exception):
        return micro_gmail_backup.failure_class(status)[0]
    extra: dict = {"request": httpx.Request("GET", url)}
    if raw is not None:
        extra["content"] = raw
    else:
        extra["json"] = body
    try:
        httpx.Response(status, **extra).raise_for_status()
    except httpx.HTTPError as exc:
        return micro_gmail_backup.failure_class(exc)[0]
    raise AssertionError(f"expected an HTTP error, got status {status}")


def test_failure_class_names_the_five_retry_classes() -> None:
    message_get = "https://gmail.googleapis.com/gmail/v1/users/me/messages/a1?format=raw"
    assert _failure_class(400, {"error": {"errors": [{"reason": "failedPrecondition"}]}}, url=message_get) == "failed_precondition"
    assert _failure_class(httpx.ConnectError("down")) == "network_error"
    assert _failure_class(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}) == "quota_exceeded"
    assert _failure_class(429) == "server_error"
    assert _failure_class(503) == "server_error"
    assert _failure_class(401, {"error": {"errors": [{"reason": "invalid_token"}]}}) == "token_expired"


def test_failure_class_reads_only_the_first_quota_reason() -> None:
    assert _failure_class(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}, {"reason": "forbidden"}]}}) == "quota_exceeded"
    assert _failure_class(403, {"error": {"errors": [{"reason": "forbidden"}, {"reason": "userRateLimitExceeded"}]}}) is None


def test_non_quota_403_logs_why_it_is_not_retried(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    forbidden = {"__status__": 403, "__body__": {"error": {"errors": [{"reason": "forbidden"}]}}}
    proc = run_case(case, script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": forbidden}))
    assert proc.returncode == 1, proc.stderr
    assert "no retry for /gmail/v1/users/me/messages/a1 (HTTP 403 is not a retried status or quota reason)" in proc.stderr


def test_failure_class_daily_and_other_quota_reasons_are_fatal() -> None:
    for reason in ("dailyLimitExceeded", "quotaExceeded"):
        assert _failure_class(403, {"error": {"errors": [{"reason": reason}]}}) is None


def test_failure_class_any_401_body_or_header_refreshes() -> None:
    assert _failure_class(401, {"error": {"errors": [{"reason": "authError"}]}}) == "token_expired"
    assert _failure_class(401, raw=b"<html>denied</html>") == "token_expired"
    assert _failure_class(401) == "token_expired"


def test_failure_class_other_statuses_and_rejections_are_fatal() -> None:
    assert _failure_class(400, {"error": {}}) is None
    assert _failure_class(403, {"error": {"errors": [{"reason": "forbidden"}]}}) is None
    assert _failure_class(403, {"error": {}}) is None
    assert _failure_class(403, raw=b"<html>denied</html>") is None
    assert _failure_class(404, {}) is None
    # A rejected refresh is neither an HTTP error nor a transport failure, so api_get never
    # treats it as a retryable class: it propagates and aborts the run loudly.
    assert not isinstance(RefreshError("invalid_grant"), (httpx.HTTPError, TransportError))


def _expired() -> httpx.Response:
    return httpx.Response(401, json={"error": {"errors": [{"reason": "invalid_token"}]}})


def test_repeated_401s_refresh_each_time_with_no_time_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            now[0] += 3660.0
        if calls["n"] <= 2:
            return _expired()
        return httpx.Response(200, json={"ok": True})

    refreshed: list = []

    def fake_refresh(self, request):
        refreshed.append(True)
        self.token = f"tok-{len(refreshed)}"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = Credentials(token="tok", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token", client_id="cid", client_secret="sek")
    token_path = tmp_path / "token.json"
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com")
    assert _session_for(client, creds, token_path, 7200.0).api_get("/gmail/v1/users/me/profile", {}, "test") == {"ok": True}
    assert refreshed == [True, True]
    assert calls["n"] == 3
    assert json.load(open(token_path, encoding="utf-8"))["token"] == "tok-2"


def test_second_401_refreshes_again_instead_of_aborting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            now[0] += 600.0
        if calls["n"] <= 2:
            return _expired()
        return httpx.Response(200, json={"ok": True})

    refreshed: list = []

    def fake_refresh(self, request):
        refreshed.append(True)
        self.token = f"tok-{len(refreshed)}"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = Credentials(token="tok", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token", client_id="cid", client_secret="sek")
    token_path = tmp_path / "token.json"
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com")
    assert _session_for(client, creds, token_path).api_get("/gmail/v1/users/me/profile", {}, "test") == {"ok": True}
    assert refreshed == [True, True]
    assert calls["n"] == 3
    assert json.load(open(token_path, encoding="utf-8"))["token"] == "tok-2"


def test_refresh_transport_failure_joins_network_backoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = [1000.0]
    sleeps: list = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    monkeypatch.setattr(time, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return _expired()
        return httpx.Response(200, json={"ok": True})

    refreshed: list = []

    def fake_refresh(self, request):
        refreshed.append(True)
        if len(refreshed) == 1:
            raise TransportError("down")
        self.token = "tok-2"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = Credentials(token="tok", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token", client_id="cid", client_secret="sek")
    token_path = tmp_path / "token.json"
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com")
    assert _session_for(client, creds, token_path).api_get("/gmail/v1/users/me/profile", {}, "test") == {"ok": True}
    assert refreshed == [True, True, True]
    assert sleeps == [1.0, 1.0, 1.0]
    assert calls["n"] == 3
    assert json.load(open(token_path, encoding="utf-8"))["token"] == "tok-2"


def test_transport_streak_exhausts_budget(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case, budget=0.5)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    gets = {"a1": {"__raise_transport__": True}, "b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2")}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "retry network_error 1 for /gmail/v1/users/me/messages/a1, waiting 1.0s (no HTTP response: TransportError: injected transport failure)" in proc.stderr
    assert "retry budget exhausted for /gmail/v1/users/me/messages/a1: network_error (no HTTP response: TransportError: injected transport failure)" in proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    assert "message(s) failed" not in proc.stderr
    fetches = [entry for entry in read_log(case) if entry["path"].endswith("/a1")]
    assert len(fetches) == 2
    digest = hashlib.sha256(RAW_B).hexdigest()
    assert os.path.isfile(os.path.join(archive, "messages", "message-b2", f"b2.{digest}.eml.zst"))
    assert not os.path.exists(os.path.join(archive, "messages", "message-a1"))


def test_exhausted_http_retries_log_the_last_response(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case, budget=0.5)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 1}]
    gets = {"b2": {"__status__": 503, "__body__": {"error": {"message": "backend unavailable"}}}}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "retry budget exhausted for /gmail/v1/users/me/messages/b2: server_error (HTTP 503)" in proc.stderr
    assert "request failed: status 503 headers {" in proc.stderr
    assert "backend unavailable" in proc.stderr


def test_quota_403_is_retried_then_succeeds(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    quota = {"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded", "message": "slow down"}]}}
    gets = {"a1": [{"__status__": 403, "__body__": quota}, raw_message("a1", ["INBOX"], RAW_A)]}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 0, proc.stderr
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst"))
    # The mock 403 carries no Retry-After header; two attempts prove plain backoff suffices.
    attempts = [entry for entry in read_log(case) if entry["path"].endswith("/a1")]
    assert len(attempts) == 2
    assert "retry quota_exceeded 1 for /gmail/v1/users/me/messages/a1, waiting 1.0s (rateLimitExceeded; the response names no quota limit)" in proc.stderr


def _clock(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    now = [0.0]
    sleeps: list = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", fake_sleep)
    return now, sleeps


def _quota_response() -> httpx.Response:
    return httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})


def _session_for(client: httpx.Client, creds: Credentials, token_path: Path, budget: float = 3600.0) -> micro_gmail_backup.MicroGmailBackup:
    session = micro_gmail_backup.MicroGmailBackup(
        account_email=ACCOUNT,
        archives_dir=str(token_path.parent),
        auth_dir=str(token_path.parent),
        client_secret_file="sek",
        retry_budget_seconds=budget,
    )
    session.archive = token_path.parent / "archive"
    session.token_path = token_path
    session.client = client
    session.creds = creds
    return session


def _api_client(script: list) -> httpx.Client:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        item = script[min(calls["n"] - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com")


def test_retry_network_streak_then_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, sleeps = _clock(monkeypatch)
    good = httpx.Response(200, json={"ok": True})
    client = _api_client([httpx.TransportError("down"), httpx.TransportError("down"), good])
    assert _session_for(client, Credentials(token="tok"), tmp_path / "token.json").api_get("/gmail/v1/users/me/profile", {}, "test") == {"ok": True}
    assert sleeps == [1.0, 1.5]


def test_retry_class_switch_restarts_backoff(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    _, sleeps = _clock(monkeypatch)
    good = httpx.Response(200, json={"ok": True})
    client = _api_client([_quota_response(), httpx.TransportError("down"), httpx.TransportError("down"), good])
    with caplog.at_level("INFO"):
        assert _session_for(client, Credentials(token="tok"), tmp_path / "token.json").api_get("/gmail/v1/users/me/profile", {}, "test") == {"ok": True}
    assert sleeps == [1.0, 1.0, 1.5]
    texts = [record.getMessage() for record in caplog.records]
    assert any("retry quota_exceeded 1" in text for text in texts)
    assert any("retry network_error 1" in text for text in texts)


def test_retry_budget_exhaustion_fails_whole_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    times = iter([0.0, 9999.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times, 9999.0))
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _quota_response()

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com")
    with pytest.raises(httpx.HTTPStatusError):
        _session_for(client, Credentials(token="tok"), tmp_path / "token.json", 5.0).api_get("/gmail/v1/users/me/profile", {}, "test")
    assert "retry budget exhausted for /gmail/v1/users/me/profile: quota_exceeded" in caplog.text
    assert sleeps == []
    assert calls["n"] == 1


def test_retry_last_request_success_recovers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, sleeps = _clock(monkeypatch)
    good = httpx.Response(200, json={"ok": True})
    client = _api_client([_quota_response(), good])
    assert _session_for(client, Credentials(token="tok"), tmp_path / "token.json", 1.0).api_get("/gmail/v1/users/me/profile", {}, "test") == {"ok": True}
    assert sleeps == [1.0]


def test_refreshable_401_with_spent_budget_raises_immediately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    times = iter([0.0, 9999.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times, 9999.0))
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _expired()

    refreshed: list = []

    def fake_refresh(self, request):
        refreshed.append(True)
        self.token = "tok-2"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = Credentials(token="tok", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token", client_id="cid", client_secret="sek")
    token_path = tmp_path / "token.json"
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com")
    with pytest.raises(httpx.HTTPStatusError):
        _session_for(client, creds, token_path, 5.0).api_get("/gmail/v1/users/me/profile", {}, "test")
    assert "retry budget exhausted for /gmail/v1/users/me/profile: token_expired" in caplog.text
    assert refreshed == []
    assert sleeps == []
    assert calls["n"] == 1
    assert not token_path.exists()


def test_invalid_retry_budget_crashes(tmp_path: Path) -> None:
    for name, value in (("zero", "0"), ("negative", "-5"), ("text", '"hour"')):
        case = os.path.join(str(tmp_path), name)
        os.makedirs(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        with open(os.path.join(case, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                f"account_email = {json.dumps(ACCOUNT)}\narchives_dir = {json.dumps(os.path.join(case, 'archives'))}\nauth_dir = {json.dumps(os.path.join(case, 'auth'))}\nretry_budget_seconds = {value}\n"
            )
        proc = run_case(case, script_for([{}], {}))
        assert proc.returncode == 1, (name, proc.stderr)
        assert "Traceback (most recent call last)" in proc.stderr, (name, proc.stderr)


def test_non_quota_403_fails_without_retry(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    denied = {"__status__": 403, "__body__": {"error": {"code": 403, "errors": [{"reason": "forbidden", "message": "no"}]}}}
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": denied}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    assert "backup completed" not in proc.stderr
    assert not os.path.exists(os.path.join(archive, "messages", "message-a1")), "run continued past the failed message"
    assert not any(entry["path"].endswith("/a1") for entry in read_log(case)), "run continued past the failed message"
    attempts = [entry for entry in read_log(case) if entry["path"].endswith("/b2")]
    assert len(attempts) == 1


def test_server_deletion_never_removes_local_files(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": raw_message("b2", ["INBOX"], b"BBB", thread="f2")}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    gone = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    assert run_case(case, script_for(gone, gets)).returncode == 0
    digest_b = hashlib.sha256(b"BBB").hexdigest()
    assert _decompressed(os.path.join(archive, "messages", "message-b2", f"b2.{digest_b}.eml.zst")) == b"BBB"


def test_overlapping_writer_is_rejected_and_process_exit_releases_lock(tmp_path: Path) -> None:
    import time as _time

    from tests.mockmail import popen_case

    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    profile = {"emailAddress": "u@example.com", "messagesTotal": 1, "__sleep__": 30}
    held = popen_case(case, {"profile": profile, "labels": {"labels": []}, "list": [{}], "get": {}})
    try:
        for _ in range(200):
            _time.sleep(0.05)
            if os.path.isfile(os.path.join(case, "fixture", "requests.jsonl")):
                break
        clash = run_case(case, script_for([{}], {}))
        assert clash.returncode == 1
        assert "lock" in clash.stderr.lower()
    finally:
        held.terminate()
        held.wait(timeout=60)
    assert held.returncode != 0
    third = run_case(case, script_for([{}], {}))
    assert third.returncode == 0


def test_oauth_refresh_is_atomic_private_and_outside_archive(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    write_token(token_file, expired=True)
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)}))
    assert proc.returncode == 0, proc.stderr
    log = read_log(case)
    assert any(entry["path"] == "/__refresh__" for entry in log), "refresh endpoint was never called"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert "fresh-token" in open(token_file, encoding="utf-8").read()
    assert "fresh-token" not in open(os.path.join(archive, "messages", "message-a1", "metadata.json"), encoding="utf-8").read()
    for _root, _dirs, files in os.walk(archive):
        for name in files:
            assert name != f"{ACCOUNT}.json"


def test_401_invalid_token_refreshes_and_recovers_within_one_run(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    write_token(token_file)
    expired = {"error": {"code": 401, "errors": [{"reason": "invalid_token", "message": "Invalid Credentials"}]}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": [{"__status__": 401, "__body__": expired}, raw_message("a1", ["INBOX"], RAW_A)]}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 0, proc.stderr
    log = read_log(case)
    assert sum(1 for entry in log if entry["path"] == "/__refresh__") == 1
    assert "refreshing credentials" in proc.stderr
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert "fresh-token" in open(token_file, encoding="utf-8").read()
    fetches = [entry for entry in log if entry["path"].endswith("/a1")]
    assert len(fetches) == 2
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert sorted(os.listdir(os.path.join(archive, "messages", "message-a1"))) == sorted([f"a1.{digest}.eml.zst", "metadata.json"])
    assert _decompressed(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")) == RAW_A


def test_401_with_rejected_refresh_stops_with_sign_in_hint(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    write_token(token_file)
    expired = {"error": {"code": 401, "errors": [{"reason": "invalid_token", "message": "Invalid Credentials"}]}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": {"__status__": 401, "__body__": expired}}
    script = script_for(listing, gets)
    script["__refresh_fail__"] = True
    proc = run_case(case, script)
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr
    for output in (proc.stderr, open(os.path.join(case, "backup.log"), encoding="utf-8").read()):
        assert "ERROR - Google rejected the stored token (invalid_grant" in output
        assert f"delete {token_file} and run again to sign in" in output
    assert "refreshing credentials" in proc.stderr
    assert "refreshed credentials" not in proc.stderr
    assert "backup completed" not in proc.stderr
    log = read_log(case)
    assert sum(1 for entry in log if entry["path"] == "/__refresh__") == 1
    assert "fresh-token" not in open(token_file, encoding="utf-8").read()
    assert not any(entry["path"].endswith("/a1") for entry in log), "run continued past the failed refresh"
    assert not os.path.exists(os.path.join(archive, "messages", "message-a1"))
    assert not os.path.exists(os.path.join(archive, "messages", "message-b2"))


def test_401_refresh_transport_failure_recovers(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    write_token(token_file)
    expired = {"error": {"code": 401, "errors": [{"reason": "invalid_token", "message": "Invalid Credentials"}]}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": [{"__status__": 401, "__body__": expired}, {"__status__": 401, "__body__": expired}, raw_message("a1", ["INBOX"], RAW_A)]}
    script = script_for(listing, gets)
    script["__refresh_flaky__"] = True
    proc = run_case(case, script)
    assert proc.returncode == 0, proc.stderr
    log = read_log(case)
    assert sum(1 for entry in log if entry["path"] == "/__refresh__") == 3
    assert "refreshing credentials" in proc.stderr
    assert "retry network_error 1" in proc.stderr
    assert "(no HTTP response: TransportError: injected refresh transport failure)" in proc.stderr
    assert "fresh-token" in open(token_file, encoding="utf-8").read()
    fetches = [entry for entry in log if entry["path"].endswith("/a1")]
    assert len(fetches) == 3
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert sorted(os.listdir(os.path.join(archive, "messages", "message-a1"))) == sorted([f"a1.{digest}.eml.zst", "metadata.json"])
    assert _decompressed(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")) == RAW_A


def test_401_without_refresh_token_stops_with_sign_in_hint(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    write_token(token_file, refresh=None)
    expired = {"error": {"code": 401, "errors": [{"reason": "invalid_token", "message": "Invalid Credentials"}]}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": {"__status__": 401, "__body__": expired}}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr
    assert "Google rejected the stored token (The credentials do not contain the necessary fields" in proc.stderr
    assert f"delete {token_file} and run again to sign in" in proc.stderr
    assert "backup completed" not in proc.stderr
    log = read_log(case)
    assert not any(entry["path"] == "/__refresh__" for entry in log)
    assert len([entry for entry in log if entry["path"].endswith("/b2")]) == 1
    assert not any(entry["path"].endswith("/a1") for entry in log), "run continued past the failed refresh"
    assert not os.path.exists(os.path.join(archive, "messages", "message-a1"))


def test_consent_flow_when_token_file_missing(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_client_secret(os.path.join(case, "client-secret.json"))
    write_config(case, secret="client-secret.json")
    token_file = os.path.join(case, "auth", f"{ACCOUNT}.json")
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    script = script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)})
    script["__consent__"] = True
    proc = run_case(case, script)
    assert proc.returncode == 0, proc.stderr
    log = read_log(case)
    assert any(entry["path"] == "/__consent__" for entry in log), "consent flow was never called"
    assert [entry["params"] for entry in log if entry["path"] == "/__consent_server__"] == [{"port": 0, "login_hint": ACCOUNT}]
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert "consented-token" in open(token_file, encoding="utf-8").read()
    assert "consented-token" not in open(os.path.join(archive, "messages", "message-a1", "metadata.json"), encoding="utf-8").read()
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert _decompressed(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")) == RAW_A


def test_unusable_token_crashes_instead_of_consent(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_client_secret(os.path.join(case, "client-secret.json"))
    write_config(case, secret="client-secret.json")
    os.makedirs(os.path.join(case, "auth"), exist_ok=True)
    with open(os.path.join(case, "auth", f"{ACCOUNT}.json"), "w", encoding="utf-8") as handle:
        handle.write("not json{{")
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    script = script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)})
    proc = run_case(case, script)
    assert proc.returncode == 1, proc.stderr
    log = read_log(case)
    assert not any(entry["path"] == "/__consent__" for entry in log), "consent flow must not run for a present file"
    assert os.listdir(os.path.join(archive, "messages")) == []


def test_consent_without_client_secret_fails(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    with open(os.path.join(case, "config.toml"), "w", encoding="utf-8") as handle:
        handle.write(
            f"account_email = {json.dumps(ACCOUNT)}\narchives_dir = {json.dumps(os.path.join(case, 'archives'))}\nauth_dir = {json.dumps(os.path.join(case, 'auth'))}\nclient_secret_file = {json.dumps(os.path.join(case, 'no-such-secret.json'))}\n"
        )
    proc = run_case(case, script_for([{}], {}))
    assert proc.returncode == 1, proc.stderr
    assert f"OAuth client file {os.path.join(case, 'no-such-secret.json')} not found" in proc.stderr
    assert "see Setup in README.md" in proc.stderr
    assert not any(entry["path"] == "/__consent__" for entry in read_log(case)), "consent started without a client file"


def test_malformed_config_toml_crashes(tmp_path: Path) -> None:
    for name, content in (
        ("malformed", "archive_dir = [unclosed\n"),
        ("wrong-type", 'archives_dir = 123\naccount_email = "u@example.com"\n'),
    ):
        case = os.path.join(str(tmp_path), name)
        os.makedirs(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        with open(os.path.join(case, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(content)
        proc = run_case(case, script_for([{}], {}))
        assert proc.returncode == 1, (name, proc.stderr)
        assert "Traceback (most recent call last)" in proc.stderr, (name, proc.stderr)


def test_argv_email_overrides_config(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", "someone-else@example.com.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    script = script_for(listing, gets)
    script["profile"] = {"emailAddress": "someone-else@example.com", "messagesTotal": 1}
    proc = run_case(case, script, args=["someone-else@example.com"])
    assert proc.returncode == 0, proc.stderr


def test_account_email_is_lowercased_for_archive_and_token(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    proc = run_case(case, script_for(listing, gets), args=[ACCOUNT.upper()])
    assert proc.returncode == 0, proc.stderr
    assert os.listdir(os.path.join(case, "archives")) == [ACCOUNT]
    assert os.listdir(os.path.join(case, "auth")) == [f"{ACCOUNT}.json"]


def test_default_archive_is_per_account_subdirectory(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    with open(os.path.join(case, "config.toml"), "w", encoding="utf-8") as handle:
        handle.write(f"account_email = {json.dumps(ACCOUNT)}\nauth_dir = {json.dumps(os.path.join(case, 'auth'))}\n")
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    expected = os.path.join(os.path.dirname(os.path.abspath(micro_gmail_backup.__file__)), "backups", ACCOUNT)
    try:
        proc = run_case(case, script_for(listing, gets))
        assert proc.returncode == 0, proc.stderr
        digest = hashlib.sha256(RAW_A).hexdigest()
        assert os.path.isfile(os.path.join(expected, "messages", "message-a1", f"a1.{digest}.eml.zst"))
    finally:
        shutil.rmtree(expected, ignore_errors=True)


def test_config_defaults_declare_current_dirs_and_budget() -> None:
    base = os.path.dirname(os.path.abspath(micro_gmail_backup.__file__))
    config = micro_gmail_backup.MicroGmailBackup(account_email="u@example.com")
    assert config.account_email == "u@example.com"
    assert config.archives_dir == os.path.join(base, "backups")
    assert config.auth_dir == os.path.join(base, "etc", "auth")
    assert config.client_secret_file == os.path.join(base, "etc", "client-secret.json")
    assert config.logs_dir == os.path.join(base, "logs")
    assert config.retry_budget_seconds == 3600.0
    assert config.archive == Path(config.archives_dir) / "u@example.com"
    assert config.token_path == Path(config.auth_dir) / "u@example.com.json"


def test_default_config_is_etc_and_relative_paths_resolve_against_script_dir(tmp_path: Path) -> None:
    # A copy of the script, so its etc/ and default dirs are under tmp_path; it runs from an unrelated directory.
    repo = tmp_path / "repo"
    (repo / "etc").mkdir(parents=True)
    backup = shutil.copy(micro_gmail_backup.__file__, repo)
    (repo / "etc" / "config.toml").write_text(f'account_email = {json.dumps(ACCOUNT)}\narchives_dir = "data/backups"\nauth_dir = "data/auth"\nlogs_dir = "data/logs"\n', encoding="utf-8")
    write_token(str(repo / "data" / "auth" / f"{ACCOUNT}.json"))
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    # Empty overrides fall back to the defaults: etc/config.toml and <logs_dir>/<account-email>.log.
    proc = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)}), backup=backup, extra_env={"MICRO_GMAIL_BACKUP_CONFIG": "", "MICRO_GMAIL_BACKUP_LOG": ""})
    assert proc.returncode == 0, proc.stderr
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert (repo / "data" / "backups" / ACCOUNT / "messages" / "message-a1" / f"a1.{digest}.eml.zst").is_file()
    log = (repo / "data" / "logs" / f"{ACCOUNT}.log").read_text(encoding="utf-8")
    assert "> GET https://gmail.googleapis.com/gmail/v1/users/me/profile" in log
    assert sorted(os.listdir(case)) == ["fixture"]
    assert not (repo / "logs").exists() and not (repo / "backups").exists()


def test_config_rejects_unknown_keys_and_bad_values(tmp_path: Path) -> None:
    for name, content, needle in (
        ("unknown-key", 'account_email = "u@example.com"\nunknown_key = 1\n', "unexpected keyword argument 'unknown_key'"),
        ("typo-key", 'account_email = "u@example.com"\nretry_budget_s = 10\n', "unexpected keyword argument 'retry_budget_s'"),
        ("no-email", 'archives_dir = "/x/archives"\n', "account email is required"),
        ("bad-email", 'account_email = "not-an-email"\n', "account email is invalid"),
        ("wrong-type", 'account_email = "u@example.com"\narchives_dir = 123\n', "Traceback"),
        ("empty-dir", 'account_email = "u@example.com"\nauth_dir = ""\n', "Traceback"),
        ("zero-budget", 'account_email = "u@example.com"\nretry_budget_seconds = 0\n', "Traceback"),
    ):
        case = os.path.join(str(tmp_path), name)
        os.makedirs(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        with open(os.path.join(case, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(content)
        proc = run_case(case, script_for([{}], {}))
        assert proc.returncode == 1, (name, proc.stderr)
        assert needle in proc.stderr, (name, proc.stderr)


def test_mock_runs_never_touch_repo_token(tmp_path: Path) -> None:
    repo_auth_dir = os.path.join(os.path.dirname(os.path.abspath(micro_gmail_backup.__file__)), "etc", "auth")
    repo_token = os.path.join(repo_auth_dir, f"{ACCOUNT}.json")
    before = None
    if os.path.isfile(repo_token):
        with open(repo_token, "rb") as handle:
            before = (os.stat(repo_token).st_mtime_ns, handle.read())
    auth_before = sorted(os.listdir(repo_auth_dir)) if os.path.isdir(repo_auth_dir) else []
    probe = os.path.join(str(tmp_path), "probe")
    os.makedirs(probe)
    write_config(probe)
    with open(os.path.join(probe, "config.toml"), encoding="utf-8") as handle:
        content = handle.read()
    for key in ("archives_dir", "auth_dir", "client_secret_file"):
        assert f"{key} = " in content
    assert repo_token not in content
    write_token(os.path.join(probe, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    assert run_case(probe, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)})).returncode == 0
    if before is None:
        assert not os.path.isfile(repo_token)
    else:
        mtime, data = before
        assert os.stat(repo_token).st_mtime_ns == mtime
        with open(repo_token, "rb") as handle:
            assert handle.read() == data
    auth_after = sorted(os.listdir(repo_auth_dir)) if os.path.isdir(repo_auth_dir) else []
    assert auth_after == auth_before


def test_mock_runs_never_touch_repo_log(tmp_path: Path) -> None:
    # Run a copy so its default log dir is under tmp_path; a live backup may be writing the real repo log meanwhile.
    repo = tmp_path / "repo"
    repo.mkdir()
    backup = shutil.copy(micro_gmail_backup.__file__, repo)
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    assert run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A)}), backup=backup).returncode == 0
    assert os.path.isfile(os.path.join(case, "backup.log"))
    assert not (repo / "logs").exists()


def test_file_log_captures_run_messages(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    log_path = os.path.join(case, "backup.log")
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    failing = {"__status__": 403, "__body__": {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}}
    proc = run_case(case, script_for(listing, {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": failing}))
    assert proc.returncode == 1, proc.stderr
    assert os.path.isfile(log_path)
    content = open(log_path, encoding="utf-8").read()
    assert "request failed" in content
    assert "1 message(s) failed" not in content
    assert "backup completed" not in content
    assert " - ERROR - request failed" in content
    trace_at = content.find("Traceback (most recent call last)")
    assert trace_at != -1
    assert "application/json" in content
    assert '"error"' in content
    assert content.find("status 403") < trace_at
    assert content.find("application/json") < trace_at
    assert content.find('"error"') < trace_at


def test_crash_traceback_reaches_file_log(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    wrong = raw_message("b2", ["INBOX"], RAW_A)
    proc = run_case(case, script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": wrong}))
    assert proc.returncode == 1, proc.stderr
    content = open(os.path.join(case, "backup.log"), encoding="utf-8").read()
    assert " - ERROR - crashed" in content
    assert "AssertionError: a1: response is for a different message" in content
    assert proc.stderr.count("Traceback (most recent call last)") == 1

    setup = os.path.join(str(tmp_path), "setup")
    os.makedirs(setup)
    with open(os.path.join(setup, "config.toml"), "w", encoding="utf-8") as handle:
        handle.write('archives_dir = "/x/archives"\n')
    proc = run_case(setup, script_for([{}], {}))
    assert proc.returncode == 1, proc.stderr
    # No account means no per-account file log yet, so the error reaches only the terminal.
    assert "account email is required" in proc.stderr
    assert "last response" not in proc.stderr
    assert not os.path.exists(os.path.join(setup, "backup.log"))


def test_crash_after_a_response_logs_that_response(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": raw_message("b2", ["INBOX"], RAW_A, thread="f2")}))
    assert proc.returncode == 1, proc.stderr
    content = open(os.path.join(case, "backup.log"), encoding="utf-8").read()
    crash_at = content.find(" - ERROR - crashed; last response https://gmail.googleapis.com/gmail/v1/users/me/messages/a1?format=raw")
    assert crash_at != -1, content
    crash_line = content[crash_at:].split("\n", 1)[0]
    assert "status 200 headers {" in crash_line and '"id":"b2"' in crash_line and '"threadId":"f2"' in crash_line
    assert crash_at < content.find("Traceback (most recent call last)")
    assert "AssertionError: a1: response is for a different message" in content


def test_non_json_success_response_crashes_and_logs_the_body(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": {"__text__": "<html>captive portal</html>"}}))
    assert proc.returncode == 1, proc.stderr
    content = open(os.path.join(case, "backup.log"), encoding="utf-8").read()
    assert " - ERROR - crashed; last response https://gmail.googleapis.com/gmail/v1/users/me/messages/a1?" in content
    assert "status 200 headers {" in content and "body <html>captive portal</html>" in content
    assert "JSONDecodeError" in content


def test_crash_before_any_response_logs_no_last_response(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    token = os.path.join(case, "auth", f"{ACCOUNT}.json")
    os.makedirs(os.path.dirname(token))
    with open(token, "w", encoding="utf-8") as handle:
        handle.write("{")
    proc = run_case(case, script_for([{}], {}))
    assert proc.returncode == 1, proc.stderr
    content = open(os.path.join(case, "backup.log"), encoding="utf-8").read()
    assert " - ERROR - crashed\n" in content
    assert "last response" not in content
    assert "> GET" not in content


def test_undecodable_body_does_not_log_the_previous_response(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": {"__bad_gzip__": True}}))
    assert proc.returncode == 1, proc.stderr
    content = open(os.path.join(case, "backup.log"), encoding="utf-8").read()
    assert "> GET https://gmail.googleapis.com/gmail/v1/users/me/messages/a1?" in content
    assert " - ERROR - crashed\n" in content
    assert "httpx.DecodingError" in content
    assert "last response" not in content


def test_failed_refresh_does_not_log_the_previous_response(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    script = script_for(listing, {"a1": {"__status__": 401, "__body__": {"error": {"code": 401}}}})
    script["__refresh_text__"] = "<html>not a token</html>"
    proc = run_case(case, script)
    assert proc.returncode == 1, proc.stderr
    content = open(os.path.join(case, "backup.log"), encoding="utf-8").read()
    assert "refreshing credentials for /gmail/v1/users/me/messages/a1" in content
    assert " - ERROR - crashed\n" in content
    assert "last response" not in content


def test_progress_lines_in_order_and_partial_stats_after_failure(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    log_path = os.path.join(case, "backup.log")
    listing = [{"messages": [{"id": "c3", "threadId": "f3"}], "resultSizeEstimate": 1}]
    assert run_case(case, script_for(listing, {"c3": raw_message("c3", ["INBOX"], RAW_A, thread="f3")})).returncode == 0
    failing = {"__status__": 403, "__body__": {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}, {"id": "c3", "threadId": "f3"}], "resultSizeEstimate": 3}]
    gets = {"a1": failing, "b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2"), "c3": raw_message("c3", ["INBOX"], RAW_A, thread="f3")}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    content = open(log_path, encoding="utf-8").read()
    verified_at = content.find("stage 2/2 [1/3] verified_on_disk c3")
    downloaded_at = content.find("stage 2/2 [2/3] downloaded_now b2")
    stopped_at = content.find("WARNING - stopped after 2 of 3 messages; the stats below cover only those")
    stats_at = content.find("previously seen messages: 1")
    trace_at = content.find("Traceback (most recent call last)")
    assert -1 not in (verified_at, downloaded_at, stopped_at, stats_at, trace_at)
    assert verified_at < downloaded_at < stopped_at < stats_at < trace_at
    for line in (" - verified on disk: 1", "newly found messages: 1", " - downloaded now: 1"):
        assert line in content, line
    # The log also holds the first, completed run; only the failed second one must lack the line.
    assert "backup completed" not in content[verified_at:]
    assert "request failed" in content
    assert "stage 1/2 [3/~3] listed" in content
    assert "listing complete: 3 known ids" in content


def test_downloads_go_newest_first(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    ids = ["a1", "c3", "b2"]
    listing = [{"messages": [{"id": mid, "threadId": "f1"} for mid in ids], "resultSizeEstimate": 3}]
    proc = run_case(case, script_for(listing, {mid: raw_message(mid, ["INBOX"], RAW_A) for mid in ids}))
    assert proc.returncode == 0, proc.stderr
    fetched = [entry["path"].rsplit("/", 1)[-1] for entry in read_log(case) if entry["path"].startswith("/gmail/v1/users/me/messages/")]
    assert fetched == ["c3", "b2", "a1"]
    for line in ("[1/3] downloaded_now c3", "[2/3] downloaded_now b2", "[3/3] downloaded_now a1"):
        assert line in proc.stderr, line


def test_malformed_message_response_aborts_run(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    bad = raw_message("b2", ["INBOX"], RAW_B, thread="f2")
    bad["raw"] = "not valid!!"
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": bad}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert not os.path.exists(os.path.join(archive, "messages", "message-a1")), "run continued past malformed data"


def test_archive_has_no_secrets_or_duplicate_raw_payload(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    secret_token = os.path.join(case, "auth", f"{ACCOUNT}.json")
    write_token(secret_token, token="sekrit-value")
    proc = run_case(case, script_for([{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}], {"a1": raw_message("a1", ["INBOX"], RAW_A)}))
    assert proc.returncode == 0, proc.stderr
    hits = 0
    for root, _dirs, files in os.walk(archive):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as handle:
                content = handle.read()
            assert b"sekrit-value" not in content, path
            if name.endswith(".json"):
                assert RAW_A not in content, path
            if name.endswith(".eml.zst") and zstandard.ZstdDecompressor().decompress(content) == RAW_A:
                hits += 1
    assert hits == 1


def test_cli_success_resume_failure_and_interrupt_exit_codes(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    assert run_case(case, script_for(listing, gets)).returncode == 0
    failing = dict(gets)
    failing["a1"] = {"__status__": 403, "__body__": {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}}
    os.remove(os.path.join(archive, "messages", "message-a1", "metadata.json"))
    assert run_case(case, script_for(listing, failing)).returncode == 1
    interrupt = dict(gets)
    interrupt["a1"] = {"__raise__": "KeyboardInterrupt"}
    assert run_case(case, script_for(listing, interrupt)).returncode == 130


def test_strict_list_response_rejects_null_token_and_bad_estimate(tmp_path: Path) -> None:
    pages = {
        "null-token": {"messages": [], "nextPageToken": None, "resultSizeEstimate": 0},
        "null-estimate": {"messages": [], "resultSizeEstimate": None},
        "negative-estimate": {"messages": [], "resultSizeEstimate": -1},
        "null-messages": {"messages": None, "resultSizeEstimate": 0},
    }
    for name, page in pages.items():
        case = os.path.join(str(tmp_path), name)
        os.makedirs(case)
        write_config(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        proc = run_case(case, script_for([page], {}))
        assert proc.returncode == 1, (name, proc.stderr)


def test_omitted_messages_with_page_token_continues(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    first: dict = {"nextPageToken": "p2", "resultSizeEstimate": 1}
    second = {"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}
    proc = run_case(case, script_for([first, second], {"a1": raw_message("a1", ["INBOX"], RAW_A)}))
    assert proc.returncode == 0, proc.stderr
    log = read_log(case)
    listings = [entry for entry in log if entry["path"] == "/gmail/v1/users/me/messages"]
    assert len(listings) == 2
    assert listings[1]["params"].get("pageToken") == "p2"
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", "metadata.json"))


def test_malformed_saved_page_wrapper_fails_loudly(tmp_path: Path) -> None:
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}

    def tamper(kind: str, doc: dict) -> dict:
        if kind == "drop-observed-at":
            del doc["observed_at"]
        elif kind == "unknown-field":
            doc["extra"] = 1
        elif kind == "wrong-path":
            doc["request"]["path"] = "/wrong"
        elif kind == "non-string-param":
            doc["request"]["params"]["maxResults"] = 500
        return doc

    for kind in ("drop-observed-at", "unknown-field", "wrong-path", "non-string-param"):
        case = os.path.join(str(tmp_path), kind)
        os.makedirs(case)
        archive = os.path.join(case, "archives", ACCOUNT)
        write_config(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        assert run_case(case, script_for(listing, gets)).returncode == 0
        run_dirs = os.listdir(os.path.join(archive, "listings"))
        assert len(run_dirs) == 1
        page_path = os.path.join(archive, "listings", run_dirs[0], "page-000001.json")
        with open(page_path, encoding="utf-8") as handle:
            doc = json.load(handle)
        with open(page_path, "w", encoding="utf-8") as handle:
            json.dump(tamper(kind, doc), handle)
        proc = run_case(case, script_for(listing, gets))
        assert proc.returncode == 1, (kind, proc.stderr)


def test_orphan_body_without_metadata_is_replaced_by_download(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    os.makedirs(os.path.join(archive, "messages", "message-a1"))
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    digest = hashlib.sha256(RAW_A).hexdigest()
    orphan = os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")
    with open(orphan, "wb") as handle:
        handle.write(b"not-a-checksummed-frame" * 4)
    # Without metadata the download never finished, so the leftover body is not checked, only overwritten.
    proc = run_case(case, script_for([{}], {"a1": raw_message("a1", ["INBOX"], RAW_A)}))
    assert proc.returncode == 0, proc.stderr
    assert _decompressed(orphan) == RAW_A


def test_token_inside_archive_is_rejected_including_symlink(tmp_path: Path) -> None:
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    direct = os.path.join(str(tmp_path), "direct")
    os.makedirs(direct)
    with open(os.path.join(direct, "config.toml"), "w", encoding="utf-8") as handle:
        handle.write(f"account_email = {json.dumps(ACCOUNT)}\narchives_dir = {json.dumps(os.path.join(direct, 'archives'))}\nauth_dir = {json.dumps(os.path.join(direct, 'archives', ACCOUNT))}\n")
    assert run_case(direct, script_for(listing, gets)).returncode == 1
    linked = os.path.join(str(tmp_path), "linked")
    os.makedirs(linked)
    real = os.path.join(linked, "archives", ACCOUNT, "real.json")
    os.makedirs(os.path.dirname(real))
    write_token(real)
    link = os.path.join(linked, "auth", f"{ACCOUNT}.json")
    os.makedirs(os.path.dirname(link))
    os.symlink(real, link)
    with open(os.path.join(linked, "config.toml"), "w", encoding="utf-8") as handle:
        handle.write(f"account_email = {json.dumps(ACCOUNT)}\narchives_dir = {json.dumps(os.path.join(linked, 'archives'))}\nauth_dir = {json.dumps(os.path.join(linked, 'auth'))}\n")
    assert run_case(linked, script_for(listing, gets)).returncode == 1


def test_incomplete_local_id_absent_from_listings_is_downloaded(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    os.makedirs(os.path.join(archive, "messages", "message-a1"))
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(case, script_for([{}], {"a1": raw_message("a1", ["INBOX"], RAW_A)}))
    assert proc.returncode == 0, proc.stderr
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert _decompressed(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")) == RAW_A
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", "metadata.json"))


def test_account_binding_rejects_type_confusion(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    with open(os.path.join(archive, "account.json"), "w", encoding="utf-8") as handle:
        json.dump({"account_email": ACCOUNT, "format": True}, handle)
    assert run_case(case, script_for(listing, gets)).returncode == 1
    # The binding before the backend key was dropped is an old format and is not accepted.
    with open(os.path.join(archive, "account.json"), "w", encoding="utf-8") as handle:
        json.dump({"account_email": ACCOUNT, "backend": "gmail-api", "format": 1}, handle)
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1
    assert "archive belongs to a different account or format" in proc.stderr


def test_sidecar_stores_message_data_file_and_format(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    with open(os.path.join(archive, "messages", "message-a1", "metadata.json"), encoding="utf-8") as handle:
        meta = json.load(handle)
    assert meta["message_data_file"] == f"a1.{digest}.eml.zst"
    assert meta["message_data_format"] == "eml.zst"
    assert "body_file" not in meta and "body_format" not in meta


def test_sidecar_message_data_mismatch_fails_loudly(tmp_path: Path) -> None:
    for kind in ("message_data_file", "message_data_format"):
        case = os.path.join(str(tmp_path), kind)
        os.makedirs(case)
        archive = os.path.join(case, "archives", ACCOUNT)
        write_config(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
        gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
        assert run_case(case, script_for(listing, gets)).returncode == 0
        sidecar = os.path.join(archive, "messages", "message-a1", "metadata.json")
        with open(sidecar, encoding="utf-8") as handle:
            meta = json.load(handle)
        if kind == "message_data_file":
            meta["message_data_file"] = "a1." + "0" * 64 + ".eml.zst"
        else:
            meta["message_data_format"] = "raw"
        with open(sidecar, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        proc = run_case(case, script_for([{}], {}))
        assert proc.returncode == 1, (kind, proc.stderr)


def test_sidecar_missing_data_keys_fails_loudly_as_corruption(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    body_path = os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")
    with open(body_path, "rb") as handle:
        before = handle.read()
    sidecar = os.path.join(archive, "messages", "message-a1", "metadata.json")
    with open(sidecar, encoding="utf-8") as handle:
        meta = json.load(handle)
    del meta["message_data_file"]
    del meta["message_data_format"]
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    assert "backup completed" not in proc.stderr
    with open(body_path, "rb") as handle:
        assert handle.read() == before


def test_legacy_body_keys_fail_loudly_as_corruption(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    digest = hashlib.sha256(RAW_A).hexdigest()
    sidecar = os.path.join(archive, "messages", "message-a1", "metadata.json")
    with open(sidecar, encoding="utf-8") as handle:
        meta = json.load(handle)
    meta["body_file"] = meta.pop("message_data_file")
    meta["body_format"] = meta.pop("message_data_format")
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    body_path = os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst")
    with open(body_path, "rb") as handle:
        before = handle.read()
    proc = run_case(case, script_for([{}], {}))
    assert proc.returncode == 1, proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    assert "backup completed" not in proc.stderr
    with open(body_path, "rb") as handle:
        assert handle.read() == before


def test_old_and_new_key_disagreement_fails_loudly(tmp_path: Path) -> None:
    for kind in ("file", "format"):
        case = os.path.join(str(tmp_path), kind)
        os.makedirs(case)
        archive = os.path.join(case, "archives", ACCOUNT)
        write_config(case)
        write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
        listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
        gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
        assert run_case(case, script_for(listing, gets)).returncode == 0
        sidecar = os.path.join(archive, "messages", "message-a1", "metadata.json")
        with open(sidecar, encoding="utf-8") as handle:
            meta = json.load(handle)
        if kind == "file":
            meta["body_file"] = "a1." + "0" * 64 + ".eml.zst"
        else:
            meta["body_format"] = "raw"
        with open(sidecar, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        proc = run_case(case, script_for([{}], {}))
        assert proc.returncode == 1, (kind, proc.stderr)


def test_previously_seen_verified_absent_counts_as_kept(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    gets = {"a1": raw_message("a1", ["INBOX"], RAW_A)}
    assert run_case(case, script_for(listing, gets)).returncode == 0
    proc = run_case(case, script_for([{}], {}))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.rstrip().endswith(" - INFO - backup completed")
    assert "stopped after" not in proc.stderr
    assert "collecting previously seen ids from local message directories and saved listings" in proc.stderr
    assert "previously seen ids: 1" in proc.stderr
    digest = hashlib.sha256(RAW_A).hexdigest()
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", f"a1.{digest}.eml.zst"))
    assert os.path.isfile(os.path.join(archive, "messages", "message-a1", "metadata.json"))
    for line in (
        "previously seen messages: 1",
        " - verified on disk: 1",
        "   - deleted on server since download (incl. spam/trash), kept locally in backup: 1",
        " - downloaded now: 0",
        " - were deleted from server before download (incl. spam/trash): 0",
        "newly found messages: 0",
        " - downloaded now: 0",
        " - were deleted from server before download (incl. spam/trash): 0",
    ):
        assert line in proc.stderr, line


def test_404_is_deleted_this_run_and_downloaded_by_the_next(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 1}]
    first = run_case(case, script_for(listing, {}))
    assert first.returncode == 0, first.stderr
    assert " - were deleted from server before download (incl. spam/trash): 1" in first.stderr
    # The saved listing keeps b2 as an obligation, so the next run fetches it again.
    proc = run_case(case, script_for([{}], {"b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2")}))
    assert proc.returncode == 0, proc.stderr
    digest = hashlib.sha256(RAW_B).hexdigest()
    assert _decompressed(os.path.join(archive, "messages", "message-b2", f"b2.{digest}.eml.zst")) == RAW_B
    for line in (
        "previously seen messages: 1",
        " - downloaded now: 1",
        " - were deleted from server before download (incl. spam/trash): 0",
    ):
        assert line in proc.stderr, line


def test_404_counts_deleted_after_one_request(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {}))
    assert proc.returncode == 0, proc.stderr
    fetches = [entry for entry in read_log(case) if entry["path"].endswith("/b2")]
    assert len(fetches) == 1
    for line in (
        "previously seen messages: 0",
        " - verified on disk: 0",
        "newly found messages: 1",
        " - downloaded now: 0",
        " - were deleted from server before download (incl. spam/trash): 1",
    ):
        assert line in proc.stderr, line
    assert "b2 not found (404)" in proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr
    assert "failed b2" not in proc.stderr


def test_deleted_stats_split_previously_seen_and_newly_found(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    archive = os.path.join(case, "archives", ACCOUNT)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    first = [{"messages": [{"id": "a1", "threadId": "f1"}, {"id": "b2", "threadId": "f2"}], "resultSizeEstimate": 2}]
    assert run_case(case, script_for(first, {"a1": raw_message("a1", ["INBOX"], RAW_A), "b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2")})).returncode == 0
    os.remove(os.path.join(archive, "messages", "message-b2", "metadata.json"))
    second = [{"messages": [{"id": "b2", "threadId": "f2"}, {"id": "c3", "threadId": "f3"}, {"id": "d4", "threadId": "f4"}], "resultSizeEstimate": 3}]
    gets = {"b2": raw_message("b2", ["INBOX"], RAW_B, thread="f2"), "c3": raw_message("c3", ["INBOX"], RAW_A, thread="f3")}
    proc = run_case(case, script_for(second, gets))
    assert proc.returncode == 0, proc.stderr
    fetches = [entry for entry in read_log(case) if entry["path"].endswith("/d4")]
    assert len(fetches) == 1
    for line in (
        "previously seen messages: 2",
        " - verified on disk: 1",
        "   - deleted on server since download (incl. spam/trash), kept locally in backup: 1",
        " - downloaded now: 1",
        " - were deleted from server before download (incl. spam/trash): 0",
        "newly found messages: 2",
        " - downloaded now: 1",
        " - were deleted from server before download (incl. spam/trash): 1",
        "d4 not found (404)",
    ):
        assert line in proc.stderr, line
    assert "Traceback (most recent call last)" not in proc.stderr


def test_deleted_before_download_does_not_mask_real_failure(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    listing = [{"messages": [{"id": "e5", "threadId": "f5"}, {"id": "f6", "threadId": "f6"}], "resultSizeEstimate": 2}]
    gets = {"e5": {"__status__": 403, "__body__": {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}}}
    proc = run_case(case, script_for(listing, gets))
    assert proc.returncode == 1, proc.stderr
    assert "f6 not found (404)" in proc.stderr
    assert "Traceback (most recent call last)" in proc.stderr
    assert "request failed" in proc.stderr
    assert "backup completed" not in proc.stderr
    assert "1 message(s) failed" not in proc.stderr
    fetches = [entry for entry in read_log(case) if entry["path"].endswith("/e5")]
    assert len(fetches) == 1


def test_recorded_quota_403_names_the_quota_and_where_to_raise_it(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    recorded = json.load(open(Path(__file__).parent / "fixtures" / "gmail_403_rate_limit.json", encoding="utf-8"))["response"]
    quota = {"__status__": recorded["status"], "__body__": recorded["body"]}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": [quota, raw_message("a1", ["INBOX"], RAW_A)]}))
    assert proc.returncode == 0, proc.stderr
    assert (
        "retry quota_exceeded 1 for /gmail/v1/users/me/messages/a1, waiting 1.0s (rateLimitExceeded; quota totalQueryCostPerMinutePerUser is 6000; raising 'Units per minute per user' to about 15000 almost avoids these waits:"
        " https://console.cloud.google.com/iam-admin/quotas?project=123456789012&service=gmail.googleapis.com)"
    ) in proc.stderr
    assert "[1/1] downloaded_now a1" in proc.stderr


def test_recorded_failed_precondition_400_on_message_get_is_retried(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    recorded = json.load(open(Path(__file__).parent / "fixtures" / "gmail_400_failed_precondition.json", encoding="utf-8"))["response"]
    precondition = {"__status__": recorded["status"], "__body__": recorded["body"]}
    listing = [{"messages": [{"id": "a1", "threadId": "f1"}], "resultSizeEstimate": 1}]
    proc = run_case(case, script_for(listing, {"a1": [precondition, raw_message("a1", ["INBOX"], RAW_A)]}))
    assert proc.returncode == 0, proc.stderr
    assert "retry failed_precondition 1 for /gmail/v1/users/me/messages/a1, waiting 1.0s (HTTP 400 failedPrecondition on users.messages.get)" in proc.stderr
    assert "[1/1] downloaded_now a1" in proc.stderr


def test_failed_precondition_is_only_retried_on_message_get() -> None:
    body = json.load(open(Path(__file__).parent / "fixtures" / "gmail_400_failed_precondition.json", encoding="utf-8"))["response"]["body"]
    assert _failure_class(400, body, url="https://gmail.googleapis.com/gmail/v1/users/me/messages/a1") == "failed_precondition"
    assert _failure_class(400, body, url="https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=500") is None
    assert _failure_class(400, body, url="https://gmail.googleapis.com/gmail/v1/users/me/profile") is None
    assert _failure_class(400, {"error": {"errors": [{"reason": "badRequest"}]}}, url="https://gmail.googleapis.com/gmail/v1/users/me/messages/a1") is None


def test_quota_advice_is_only_given_for_the_measured_limit() -> None:
    recorded = json.load(open(Path(__file__).parent / "fixtures" / "gmail_403_rate_limit.json", encoding="utf-8"))["response"]["body"]["error"]
    assert "about 15000" in micro_gmail_backup.quota_note(recorded)
    other = json.loads(json.dumps(recorded).replace("totalQueryCostPerMinutePerUser", "totalQueryCostPerMinute"))
    note = micro_gmail_backup.quota_note(other)
    assert note.startswith("quota totalQueryCostPerMinute is 6000; raise it: https://console.cloud.google.com/iam-admin/quotas?")
    assert "15000" not in note


def test_extra_arguments_are_rejected(tmp_path: Path) -> None:
    case = os.path.join(str(tmp_path), "case")
    os.makedirs(case)
    write_config(case)
    write_token(os.path.join(case, "auth", f"{ACCOUNT}.json"))
    proc = run_case(case, script_for([{}], {}), args=[ACCOUNT, "other@example.com"])
    assert proc.returncode == 1, proc.stderr
    assert "usage: " in proc.stderr and "[account-email]" in proc.stderr
    assert "> GET" not in proc.stderr
