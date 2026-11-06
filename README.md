# micro-gmail-backup

Backs up a Gmail account through the Gmail REST API, one message at a time. It never changes or deletes anything in Gmail, and never deletes messages from the backup, even ones later deleted from Gmail.

## Setup

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # installs uv; or: brew install uv

git clone https://github.com/valievkarim/micro-gmail-backup.git
cd micro-gmail-backup
uv sync
cp etc/config.sample.toml etc/config.toml   # optional; see the file for keys and defaults
```

You need your own Google Cloud OAuth client. In the Cloud console:

1. [Create a project](https://console.cloud.google.com/projectcreate) with the ID `micro-gmail-backup` (or `gcloud projects create micro-gmail-backup`). Project IDs are unique across Google Cloud; if it's taken, pick another and use it below.
2. [Enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com), with `micro-gmail-backup` chosen in the project picker at the top (or `gcloud services enable gmail.googleapis.com --project micro-gmail-backup`).

Steps 3–8 have no `gcloud` command for personal accounts; keep `micro-gmail-backup` chosen in the project picker.

3. [Google Auth Platform overview](https://console.cloud.google.com/auth/overview): click **Get started**, enter an app name and support email, and choose **External**.
4. [Clients](https://console.cloud.google.com/auth/clients): create a client of type **Desktop app** and download its JSON as `etc/client-secret.json`.
5. [Branding](https://console.cloud.google.com/auth/branding): publishing needs a home page URL and a privacy policy URL besides the app name and support email. For an app only you use, `https://github.com/<username>` works for both; add `github.com` under **Authorized domains**. Leave the logo empty, since a logo requires Google's review.
6. [Audience](https://console.cloud.google.com/auth/audience): click **Publish app**. In Testing mode Google expires the sign-in after 7 days. Google doesn't need to review an app used only by you.
7. Google Workspace accounts only, as the Workspace admin: on [Manage App Access Control](https://admin.google.com/ac/owl/list?tab=configuredApps) click **Configure new app**, search for the `installed.client_id` value from `etc/client-secret.json` (searching by app name doesn't find it), and set its access to **Trusted**.
8. Optional, [Quotas](https://console.cloud.google.com/iam-admin/quotas?service=gmail.googleapis.com): raise the Gmail API limit **Units per minute per user** (`totalQueryCostPerMinutePerUser` in the log) to 15000. At the default the backup keeps hitting the per-minute quota and pausing briefly; 15000 is about what the backup uses. When it hits the quota, the log names the limit and links to this page.

On the first run the script opens a browser to sign in and grant read-only access (`gmail.readonly`). Google shows **"Google hasn't verified this app"**; that's expected for your own unreviewed app. Click **Advanced**, then **Go to micro-gmail-backup (unsafe)** (with your app name), then **Continue**. The token is saved to `etc/auth/<account-email>.json`.

## Running

```sh
.venv/bin/python micro_gmail_backup.py you@example.com
```

The email can also come from `account_email` in `etc/config.toml`; the argument wins. The config is read from the script's `etc/` (override with `MICRO_GMAIL_BACKUP_CONFIG`), and relative paths in it resolve against the script directory, so the script behaves the same from any working directory. Each run:

1. locks the archive and checks the token belongs to that account,
2. saves the label names and every listing page,
3. verifies each message already on disk and downloads the rest.

Exit codes: `0` when the backup is complete, `1` on any failure, `130` when interrupted. After a failure or interruption, run it again: messages already saved are verified and skipped, and the rest are downloaded. A message deleted from Gmail after it was listed but before it was downloaded can't be backed up; that is logged and doesn't count as a failure.

Network errors, per-minute quota 403s, 429/5xx responses, expired tokens and the occasional 400 `failedPrecondition` on a message download are retried with backoff (1s, ×1.5, up to 120s) for up to `retry_budget_seconds` per request. Everything else, including corruption on disk, stops the run with a traceback.

## Archive

```text
backups/<account-email>/
  account.json
  listings/<timestamp>-<suffix>/
    labels.json
    page-000001.json ...
  messages/message-<gmail-id>/
    <gmail-id>.<sha256>.eml.zst
    metadata.json
```

Messages are never deleted locally, even after they disappear from Gmail.

## Searching

`eml_text.py` prints a stored message as text: headers with encoded words decoded, and text parts with base64, quoted-printable or uuencode undone and converted to UTF-8; attachments appear as one line with their type and name. Use it as ripgrep's preprocessor, with the virtualenv active:

```sh
source .venv/bin/activate
rg -u --pre ./eml_text.py --pre-glob '*.eml.zst' -i 'invoice' backups/you@example.com/messages
./eml_text.py backups/you@example.com/messages/message-<id>/<id>.<sha256>.eml.zst | less
```

`-u` is needed because `backups/` is in `.gitignore`. Each message starts a Python process, so a full search of 60,000 messages takes several minutes.

## Exporting to mbox

`export_mbox.py` is for checking a backup only: it writes every complete stored message of one account into a single mbox file, oldest first by Gmail's internal date, so the archive can be browsed in Apple Mail (File > Import Mailboxes) or Thunderbird (with the ImportExportTools NG add-on).

```sh
.venv/bin/python export_mbox.py backups/you@example.com you@example.com.mbox
```

The mbox is not a second copy of the backup:

- Messages are not preserved byte for byte: line endings become LF and body lines starting with `From ` are quoted mboxrd-style.
- Labels and unread/starred flags are not exported; everything lands in one folder.
- Importing a large archive into Apple Mail can be slow, and its progress bar may not move for a long time.

## Logs

The log goes to the terminal and to `logs/<account-email>.log`, one file per account (set `logs_dir` in `etc/config.toml`, or a full file path with `MICRO_GMAIL_BACKUP_LOG`). Errors before the account is known, such as a malformed config, appear only in the terminal. Set `MICRO_GMAIL_BACKUP_DEBUG=1` to add response headers and error bodies.
