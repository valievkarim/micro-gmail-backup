#!/usr/bin/env python3
"""Sequential Gmail backup to content-hashed files.

Run `python micro_gmail_backup.py [account-email]` with an optional etc/config.toml
(MICRO_GMAIL_BACKUP_CONFIG overrides its path). Paths resolve against the script
directory: etc/ for the token and client-secret files, the archive defaults to
backups/<account-email>/ so each account gets its own subdirectory, and the log
to logs/<account-email>.log. The account email comes from
argv[1] or config.toml account_email (argv wins) and is required. When the
token file does not exist, the script runs the installed-app browser
consent flow itself and saves the token. Exits 0 only when every known
message has a verified body/metadata pair. MICRO_GMAIL_BACKUP_DEBUG=1 adds
response headers and error bodies to the log.
"""

import base64
import fcntl
import hashlib
import itertools
import json
import logging
import os
import re
import sys
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import StrEnum, auto
from pathlib import Path
from typing import Literal

import google.auth.exceptions
import google.auth.transport.requests
import httpx
import zstandard
from beartype import beartype
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

API = "https://gmail.googleapis.com"
LIST_PATH = "/gmail/v1/users/me/messages"
HEX = re.compile(r"[0-9a-f]+")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE = Path(__file__).resolve().parent
HTTP_BODY_PREVIEW = 2000
HTTP_TIMEOUT_SECONDS = 30.0
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


class RetryClass(StrEnum):
    """The failure classes api_get retries; failure_class returns None for anything else."""

    FAILED_PRECONDITION = auto()
    NETWORK_ERROR = auto()
    QUOTA_EXCEEDED = auto()
    SERVER_ERROR = auto()
    TOKEN_EXPIRED = auto()


class MessageOutcome(StrEnum):
    """What backup_all did with one message; the value is the word in its progress line."""

    VERIFIED_ON_DISK = auto()
    DOWNLOADED_NOW = auto()
    DELETED_ON_SERVER_BEFORE_DOWNLOAD = auto()


@beartype
@dataclass
class MessageMetadata:
    """Validated message sidecar; beartype checks types, __post_init__ checks values."""

    id: str
    threadId: str
    labelIds: list[str]
    internalDate: str
    raw_length: int
    raw_sha256: str
    acquired_at: str
    message_data_file: str
    message_data_format: Literal["eml.zst"]

    def __post_init__(self) -> None:
        valid_id(self.id)
        valid_id(self.threadId)
        assert all(type(label) is str for label in self.labelIds), f"sidecar labels invalid for {self.id}"
        assert self.internalDate, f"sidecar date invalid for {self.id}"
        assert self.raw_length > 0, f"sidecar length invalid for {self.id}"
        assert len(self.raw_sha256) == 64 and HEX.fullmatch(self.raw_sha256) is not None, f"sidecar hash invalid for {self.id}"
        assert self.acquired_at, f"sidecar acquisition time invalid for {self.id}"
        assert self.message_data_file == f"{self.id}.{self.raw_sha256}.eml.zst", f"sidecar message_data_file does not match its hash for {self.id}"

    @classmethod
    def from_dict(cls, payload: dict, directory: Path) -> "MessageMetadata":
        """Build validated metadata; missing or extra keys fail in the constructor as corruption."""
        assert f"message-{payload['id']}" == directory.name, f"sidecar ID does not match {directory}"
        return cls(**payload)


@beartype
def atomic_write(path: Path, data: bytes) -> None:
    """Publish bytes atomically: tmp file, flush, fsync, rename, dir sync."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)

    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@beartype
def write_json(path: Path, payload: dict) -> None:
    """Write one JSON document atomically with stable formatting."""
    atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())


@beartype
def read_json(path: Path) -> dict:
    """Read one JSON document; malformed content fails loudly."""
    return json.loads(path.read_bytes())


@beartype
def valid_id(raw: str) -> str:
    """Return the ID when it is canonical lowercase hexadecimal."""
    assert HEX.fullmatch(raw) is not None, f"Gmail ID is not canonical lowercase hex: {raw!r}"
    return raw


@beartype
def decode_raw(raw_b64: str) -> bytes:
    """Decode canonical base64url, fully padded or unpadded."""
    raw = base64.b64decode(raw_b64 + "=" * (-len(raw_b64) % 4), altchars=b"-_", validate=True)

    # b64decode also accepts + and /, a partial pad (QQ=) and nonzero unused bits (QR==); re-encoding rejects them all.
    encoded = base64.urlsafe_b64encode(raw).decode()
    assert raw_b64 and raw_b64 in (encoded, encoded.rstrip("=")), "message raw payload is not canonical base64url"
    return raw


@beartype
def quota_note(error: dict) -> str:
    """Name the exceeded quota and where to raise it, from the details Google attaches to a quota 403."""
    for detail in error.get("details", []):
        match detail:
            case {"metadata": {"quota_limit": str(limit), "quota_limit_value": str(value), "consumer": str(consumer)}}:
                url = f"https://console.cloud.google.com/iam-admin/quotas?project={consumer.removeprefix('projects/')}&service=gmail.googleapis.com"
                # 15000 is measured for this one limit only: the backup uses about 13600-14800 units per minute.
                advice = "raising 'Units per minute per user' to about 15000 almost avoids these waits" if limit == "totalQueryCostPerMinutePerUser" else "raise it"
                return f"quota {limit} is {value}; {advice}: {url}"
    return "the response names no quota limit"


@beartype
def failure_class(error: httpx.HTTPError | google.auth.exceptions.TransportError) -> tuple[RetryClass | None, str]:
    """Name the recoverable class of one HTTP or token-refresh failure, None when it is fatal, with a note for the log."""
    if isinstance(error, (httpx.TransportError, google.auth.exceptions.TransportError)):
        # The error text tells a DNS failure from a rejected certificate, e.g. a captive portal, proxy or wrong clock.
        return RetryClass.NETWORK_ERROR, f"no HTTP response: {type(error).__name__}: {error}"
    if isinstance(error, httpx.HTTPStatusError):
        # Gmail's error guide: on any 401, refresh the access token; Google's auth library does the same.
        response = error.response
        if response.status_code == 401:
            return RetryClass.TOKEN_EXPIRED, "HTTP 401, refreshing the token"
        if response.status_code == 404:
            return None, "HTTP 404 not found"

        # Gmail's error guide: back off and retry on too many requests and server errors.
        if response.status_code == 429 or response.status_code >= 500:
            return RetryClass.SERVER_ERROR, f"HTTP {response.status_code}"

        try:
            payload = response.json()
        except ValueError:
            return None, f"HTTP {response.status_code} with a non-JSON body"

        # Only these per-minute quota reasons are worth waiting out, read from the first error as Google's API client does.
        match response.status_code, payload:
            case 403, {"error": {"errors": [{"reason": "rateLimitExceeded" | "userRateLimitExceeded" as reason}, *_]}}:
                return RetryClass.QUOTA_EXCEEDED, f"{reason}; {quota_note(payload['error'])}"

            # Seen once in about 43,700 message downloads: the same request returned 200 minutes later. Other 400s stay fatal.
            case 400, {"error": {"errors": [{"reason": "failedPrecondition"}, *_]}} if error.request.url.path.startswith(f"{LIST_PATH}/"):
                return RetryClass.FAILED_PRECONDITION, "HTTP 400 failedPrecondition on users.messages.get"
        return None, f"HTTP {response.status_code} is not a retried status or quota reason"
    return None, f"request failed without a retryable cause: {type(error).__name__}"


@beartype
@dataclass
class MicroGmailBackup:
    """Backup configuration plus per-run state; beartype checks types, __post_init__ checks values."""

    account_email: str
    archives_dir: str = str(BASE / "backups")
    auth_dir: str = str(BASE / "etc" / "auth")
    client_secret_file: str = str(BASE / "etc" / "client-secret.json")
    logs_dir: str = str(BASE / "logs")
    retry_budget_seconds: int | float = 3600.0
    archive: Path = field(init=False)
    token_path: Path = field(init=False)
    client: httpx.Client = field(init=False)
    creds: Credentials = field(init=False)
    last_response: httpx.Response | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Gmail addresses are case-insensitive; one spelling keeps one archive and one token per account.
        self.account_email = self.account_email.lower()
        assert re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.account_email) is not None, "account email is invalid"
        assert self.archives_dir and self.auth_dir and self.client_secret_file and self.logs_dir, "config paths must be non-empty"
        assert type(self.retry_budget_seconds) is not bool and 0 < self.retry_budget_seconds < float("inf"), "config must define retry_budget_seconds as a positive number"

        # Relative config paths resolve against the script directory, like the defaults, not the current directory.
        self.archive = BASE / self.archives_dir / self.account_email
        self.token_path = BASE / self.auth_dir / f"{self.account_email}.json"

    def load_credentials(self) -> Credentials:
        """Load stored credentials; only a missing file runs consent. Expired ones are refreshed by the first request."""
        if self.token_path.exists():
            return Credentials.from_authorized_user_info(read_json(self.token_path))

        client_secret = BASE / self.client_secret_file
        assert client_secret.is_file(), f"no token yet and OAuth client file {client_secret} not found; see Setup in README.md"
        # The hint preselects the configured account in the browser, so consent is not given by whichever account is signed in.
        creds = InstalledAppFlow.from_client_secrets_file(client_secret, scopes=SCOPES).run_local_server(port=0, login_hint=self.account_email)
        atomic_write(self.token_path, creds.to_json().encode())
        return creds

    def api_get(self, path: str, params: dict, what: str) -> dict:
        """Send one HTTPS GET; the RetryClass failures back off and retry against a time budget."""
        deadline = time.monotonic() + self.retry_budget_seconds
        active = None
        attempt = 0
        refresh = not self.creds.valid
        while True:
            try:
                # Only a response this attempt received; a failed refresh or send must not report an older one.
                self.last_response = None
                if refresh:
                    logger.info("refreshing credentials for %s", path)
                    self.creds.refresh(google.auth.transport.requests.Request())
                    atomic_write(self.token_path, self.creds.to_json().encode())
                    refresh = False

                request = self.client.build_request("GET", path, params=params, headers={"Authorization": f"Bearer {self.creds.token}"})
                logger.info("> GET %s (%s)", request.url, what)
                response = self.client.send(request)
                self.last_response = response
                logger.info("%s %s (%s)", response.status_code, request.url, what)
                logger.debug("%s %s response headers %s", response.status_code, what, dict(response.headers))
                if not response.is_success:
                    logger.debug("%s %s error body %s", response.status_code, what, response.text[:HTTP_BODY_PREVIEW])
                response.raise_for_status()
            except (httpx.HTTPError, google.auth.exceptions.TransportError) as exc:
                kind, note = failure_class(exc)
                if kind is None:
                    logger.info("no retry for %s (%s)", path, note)
                    raise
                if time.monotonic() >= deadline:
                    # Re-raise the last failure itself, so main logs its response status, headers and body.
                    logger.error("retry budget exhausted for %s: %s (%s)", path, kind, note)
                    raise

                # Sleep 1s * 1.5^(attempt - 1), holding at the 120s cap; a class switch restarts the streak.
                attempt = attempt + 1 if kind == active else 1
                active = kind
                delay = min(1.5 ** min(attempt - 1, 12), 120.0)
                logger.info("retry %s %d for %s, waiting %.1fs (%s)", kind, attempt, path, delay, note)
                time.sleep(delay)
                refresh = refresh or kind == RetryClass.TOKEN_EXPIRED
            else:
                return response.json()

    def download_one(self, mid: str) -> bool:
        """Clear crash leftovers, then fetch, validate, and publish one message; False when a 404 shows it was deleted."""
        directory = self.archive / "messages" / f"message-{mid}"
        for path in directory.glob(".tmp-*"):
            path.unlink()

        try:
            payload = self.api_get(f"{LIST_PATH}/{mid}", {"format": "raw", "fields": "id,threadId,labelIds,internalDate,raw"}, "users.messages.get")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            logger.info("%s not found (404)", mid)
            return False

        assert payload["id"] == mid, f"{mid}: response is for a different message"
        raw = decode_raw(payload["raw"])
        digest = hashlib.sha256(raw).hexdigest()
        meta = MessageMetadata(
            id=mid,
            labelIds=payload.get("labelIds", []),
            internalDate=payload["internalDate"],
            raw_length=len(raw),
            raw_sha256=digest,
            acquired_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            message_data_file=f"{mid}.{digest}.eml.zst",
            message_data_format="eml.zst",
            threadId=payload["threadId"],
        )

        directory.mkdir(exist_ok=True)
        atomic_write(directory / meta.message_data_file, zstandard.ZstdCompressor(level=9, write_checksum=True).compress(raw))
        write_json(directory / "metadata.json", asdict(meta))

        verified = self.verify_pair(mid)
        assert verified, f"{mid}: incomplete right after download"
        return True

    def verify_pair(self, mid: str) -> bool:
        """Return True for a verified pair, False when metadata.json is absent (not yet downloaded, or interrupted before it); crash on corruption, a missing body or a second body."""
        directory = self.archive / "messages" / f"message-{mid}"
        sidecar_path = directory / "metadata.json"
        if not sidecar_path.is_file():
            return False

        meta = MessageMetadata.from_dict(read_json(sidecar_path), directory)
        body_path = directory / meta.message_data_file
        data = body_path.read_bytes()

        # The hash decides integrity, so any valid zstd layout verifies; decoding frame by frame rejects a cut-off file or trailing junk.
        raw = b""
        while data:
            decoder = zstandard.ZstdDecompressor().decompressobj()
            raw += decoder.decompress(data)
            assert decoder.eof, f"truncated body in {directory}"
            data = decoder.unused_data
        assert len(raw) == meta.raw_length and hashlib.sha256(raw).hexdigest() == meta.raw_sha256, f"body does not match its metadata in {directory}"
        assert list(directory.glob("*.eml.zst")) == [body_path], f"extra body beside a complete pair, message hash changed in {directory}"
        return True

    def bind_account(self) -> int:
        """Check the token against the configured account and bind the archive to it; return the mailbox's message count."""
        profile = self.api_get("/gmail/v1/users/me/profile", {"fields": "emailAddress,messagesTotal"}, "users.getProfile")
        assert profile["emailAddress"].lower() == self.account_email, f"token belongs to {profile['emailAddress']}, not {self.account_email}; delete {self.token_path} and run again to sign in"

        account_path = self.archive / "account.json"
        binding = {"account_email": self.account_email, "format": 1}
        if account_path.exists():
            saved = read_json(account_path)
            assert saved == binding and type(saved["format"]) is int, "archive belongs to a different account or format"
        else:
            write_json(account_path, binding)
        return profile["messagesTotal"]

    def save_labels(self, run_dir: Path) -> None:
        """Save this run's label ID/name mapping."""
        labels = self.api_get("/gmail/v1/users/me/labels", {"fields": "labels/id,labels/name,labels/type"}, "users.labels.list")
        mapping = {entry["id"]: entry["name"] for entry in labels["labels"]}
        assert all(type(value) is str for pair in mapping.items() for value in pair), "label entry has no string id/name"
        write_json(run_dir / "labels.json", {"labels": mapping})

    def list_messages(self, run_dir: Path, expected: int) -> set[str]:
        """Persist every listing page in run_dir, then return the listed IDs."""
        params = {"maxResults": "500", "includeSpamTrash": "true", "fields": "nextPageToken,messages/id,messages/threadId,resultSizeEstimate"}
        current: set[str] = set()
        tokens: set[str] = set()
        for page_num in itertools.count(1):
            page = self.api_get(LIST_PATH, params, f"users.messages.list page {page_num}")
            name = f"page-{page_num:06d}.json"
            write_json(run_dir / name, {"observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request": {"params": params, "path": LIST_PATH}, "response": page})

            current |= page_ids(page)
            # Only an estimate: the listing, which includes spam and trash, has ended slightly above messagesTotal.
            logger.info("stage 1/2 [%d/~%d] listed %s", len(current), expected, name)
            if "nextPageToken" not in page:
                return current

            params["pageToken"] = page["nextPageToken"]
            assert params["pageToken"] not in tokens, f"listing repeated page token {params['pageToken']!r}"
            tokens.add(params["pageToken"])

    def previously_seen(self) -> set[str]:
        """Union the IDs in all saved listing pages with the local message directories; call before this run lists."""
        # Reading every saved page of a large archive takes a while, so mark both ends.
        logger.info("collecting previously seen ids from local message directories and saved listings")
        seen = {valid_id(path.name.removeprefix("message-")) for path in (self.archive / "messages").glob("message-*")}
        for path in (self.archive / "listings").glob("*/page-*.json"):
            seen |= page_ids(unwrap_saved_page(read_json(path), path.name))
        logger.info("previously seen ids: %d", len(seen))
        return seen

    def backup_all(self) -> None:
        """Enumerate first, then verify or download every known message and report stats."""
        expected = self.bind_account()
        previous = self.previously_seen()

        run_dir = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-", dir=self.archive / "listings"))
        self.save_labels(run_dir)
        current = self.list_messages(run_dir, expected)
        union = previous | current
        logger.info("listing complete: %d known ids", len(union))

        stats = Counter[tuple[bool, MessageOutcome, bool]]()

        @beartype
        def count(*, prev_seen: bool, outcome: MessageOutcome | None = None, currently_seen: bool | None = None) -> int:
            return sum(n for (p, o, c), n in stats.items() if p == prev_seen and outcome in (None, o) and currently_seen in (None, c))

        try:
            # Newest first: Gmail IDs grow with arrival time, so a long first run saves recent mail early.
            # Text order is time order for IDs of equal length; mail from before November 2004 has 15 hex digits and sorts first.
            for done, mid in enumerate(sorted(union, reverse=True), 1):
                if self.verify_pair(mid):
                    outcome = MessageOutcome.VERIFIED_ON_DISK
                elif self.download_one(mid):
                    outcome = MessageOutcome.DOWNLOADED_NOW
                else:
                    outcome = MessageOutcome.DELETED_ON_SERVER_BEFORE_DOWNLOAD

                stats[mid in previous, outcome, mid in current] += 1

                logger.info("stage 2/2 [%d/%d] %s %s", done, len(union), outcome, mid)
        finally:
            # Also after a crash or interrupt, so a stopped run still shows what it got done; main logs the cause after this.
            if stats.total() < len(union):
                logger.warning("stopped after %d of %d messages; the stats below cover only those", stats.total(), len(union))

            logger.info("previously seen messages: %d", count(prev_seen=True))
            logger.info(" - verified on disk: %d", count(prev_seen=True, outcome=MessageOutcome.VERIFIED_ON_DISK))
            logger.info("   - deleted on server since download (incl. spam/trash), kept locally in backup: %d", count(prev_seen=True, outcome=MessageOutcome.VERIFIED_ON_DISK, currently_seen=False))
            logger.info(" - downloaded now: %d", count(prev_seen=True, outcome=MessageOutcome.DOWNLOADED_NOW))
            logger.info(" - were deleted from server before download (incl. spam/trash): %d", count(prev_seen=True, outcome=MessageOutcome.DELETED_ON_SERVER_BEFORE_DOWNLOAD))

            logger.info("newly found messages: %d", count(prev_seen=False))
            logger.info(" - downloaded now: %d", count(prev_seen=False, outcome=MessageOutcome.DOWNLOADED_NOW))
            logger.info(" - were deleted from server before download (incl. spam/trash): %d", count(prev_seen=False, outcome=MessageOutcome.DELETED_ON_SERVER_BEFORE_DOWNLOAD))

    def run(self) -> None:
        """Hold the archive lock for one full backup."""
        assert not self.token_path.resolve().is_relative_to(self.archive.resolve()), "token file must live outside the archive"

        (self.archive / "messages").mkdir(parents=True, exist_ok=True)
        (self.archive / "listings").mkdir(exist_ok=True)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.archive / ".lock", "a") as lock, httpx.Client(base_url=API, timeout=HTTP_TIMEOUT_SECONDS) as self.client:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OSError("another backup holds the archive lock") from exc

            for path in self.token_path.parent.glob(f".tmp-{self.token_path.name}-*"):
                path.unlink()

            self.creds = self.load_credentials()
            self.backup_all()


@beartype
def page_ids(payload: dict) -> set[str]:
    """Validate one list response and return its IDs; malformed data fails loudly."""
    assert set(payload) <= {"messages", "nextPageToken", "resultSizeEstimate"}, "list response has unexpected keys"

    estimate = payload.get("resultSizeEstimate", 0)
    assert type(estimate) is int and estimate >= 0, "list response has an invalid resultSizeEstimate"

    if "nextPageToken" in payload:
        token = payload["nextPageToken"]
        assert type(token) is str and token, "list response has an invalid page token"

    entries = payload.get("messages", [])
    assert type(entries) is list, "list response messages is not a list; only an omitted key is an empty page"
    for entry in entries:
        valid_id(entry["threadId"])
    return {valid_id(entry["id"]) for entry in entries}


@beartype
def unwrap_saved_page(payload: dict, name: str) -> dict:
    """Validate one saved page wrapper; return its response."""
    assert set(payload) == {"observed_at", "request", "response"}, f"saved page malformed: {name}"
    assert type(payload["observed_at"]) is str and payload["observed_at"], f"saved page malformed: {name}"

    request = payload["request"]
    assert type(request) is dict and set(request) == {"params", "path"} and request["path"] == LIST_PATH, f"saved page malformed: {name}"
    assert type(request["params"]) is dict and all(type(value) is str for value in request["params"].values()), f"saved page malformed: {name}"
    return payload["response"]


@beartype
def main() -> int:
    """Run one sequential backup; return the process exit code."""
    level = logging.DEBUG if os.environ.get("MICRO_GMAIL_BACKUP_DEBUG") == "1" else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    backup = None
    try:
        config_path = Path(os.environ.get("MICRO_GMAIL_BACKUP_CONFIG") or BASE / "etc" / "config.toml")
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        assert len(sys.argv) <= 2, f"usage: {sys.argv[0]} [account-email]"
        if len(sys.argv) > 1:
            config["account_email"] = sys.argv[1]
        assert "account_email" in config, f"account email is required: pass it as argv[1] or set account_email in {config_path}"
        backup = MicroGmailBackup(**config)

        # The file log is per account, so it starts once the config names the account; earlier errors reach only the terminal.
        log_path = Path(os.environ.get("MICRO_GMAIL_BACKUP_LOG") or BASE / backup.logs_dir / f"{backup.account_email}.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_log = logging.FileHandler(log_path)
        file_log.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(file_log)
        backup.run()
        logger.info("backup completed")
    except KeyboardInterrupt:
        logger.error("interrupted")
        return 130
    except google.auth.exceptions.RefreshError as exc:
        # Expired, revoked or missing refresh token: nothing to debug, the user has to sign in again.
        logger.error("Google rejected the stored token (%s); delete %s and run again to sign in", exc.args[0], backup.token_path)
        return 1
    except httpx.HTTPStatusError as exc:
        response = exc.response
        logger.exception("request failed: status %s headers %s body %s", response.status_code, dict(response.headers), response.text[:HTTP_BODY_PREVIEW])
        return 1
    except Exception:
        # The one broad handler the spec allows: log the crash to the file log too, then exit nonzero.
        # A non-JSON body or bad data fails after the response is gone, so log the last one; there is none before the first request.
        last = backup.last_response if backup is not None else None
        if last is None:
            logger.exception("crashed")
        else:
            logger.exception("crashed; last response %s: status %s headers %s body %s", last.request.url, last.status_code, dict(last.headers), last.text[:HTTP_BODY_PREVIEW])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
