# Google Photos Consolidator

Consolidate duplicate / unique photos across several Google Photos accounts
into the account with the most free storage.

Two interchangeable backends, switch with one line in `config.yaml` (or
`--backend`/`GPC_BACKEND`):

| backend | docs | how it talks to Google |
|---|---|---|
| `oauth` | Google Photos Library API (official REST) | first-party Python client |
| `rclone` | rclone `google photos` remote (`rclone config`) | subprocess |

Both implement the same flow:
list items -> SHA-256 everything -> group duplicates by hash -> pick the
account with the most free space as target -> upload the kept copy there
(verified by comparing the hash of the bytes actually stored on the target)
-> record every remaining source copy for manual removal.

## ⚠️ What Google actually allows (read before you run)

Since **2025-03-31** Google made hard policy changes to the Photos Library
API ([official update](https://developers.google.com/photos/support/updates)):

1. The scopes `photoslibrary`, `photoslibrary.readonly` and
   `photoslibrary.sharing` were **removed**. Only `appendonly`,
   `readonly.appcreateddata` and `edit.appcreateddata` remain. The API can
   now only see items **created by your app** — photos you uploaded with the
   Photos app/website are *invisible* to it (and to rclone).
2. The Library API has **no delete endpoint at all**. Google closed the
   feature request ([issuetracker #109759781](https://issuetracker.google.com/issues/109759781));
   there is no API, no scope, no app that can delete library items.

**Consequences for this tool (kept honest):**

- **Upload & dedupe** work for everything the app can read (app-created
  items). Uploaded copies are placed into an app-created album
  (`gpc_consolidation`) so they can later be removed from it, which is the
  max the API allows.
- **Deleting** source photos after a successful, hash-verified upload **is
  not possible through any Google API**. Instead the tool appends each photo
  to a private local manifest (`removal_manifest.jsonl`, permission `0600`,
  gitignored) containing the page links, truncated hash and account id.
  You open the links in the Photos web UI and delete them there — the only
  sanctioned channel. This is the same limitation every tool (including
  rclone) hits; the difference is we don't pretend to delete.
- The API (and rclone) cannot see user-uploaded originals, so full-library
  consolidation of photos uploaded through the app/website is **not
  automatable** by any third-party tool in 2026. The only sanctioned full
  library access is Google's [Photos Partner Program](https://developers.google.com/photos/overview/partners)
  (a business application process).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure (OAuth backend)

1. Create an OAuth client in [Google Cloud Console](https://console.cloud.google.com)
   (Desktop app type), enable **Photos Library API** + **Drive API**, and
   download `client_secret.json`. Never commit it.
2. Edit `config.yaml`:
   - one `accounts` entry per Google account (numeric `id`, token filename)
   - keep the current 2026 scopes listed under `oauth.photos_scopes`
3. Point the tool at your secret and a token dir (tokens are stored locally,
   gitignored):

```bash
export GPC_OAUTH_CLIENT_SECRET=/path/to/client_secret.json
export GPC_OAUTH_TOKEN_DIR=/path/to/local/token/dir
```

## Configure (rclone backend)

```bash
rclone config          # create one "google photos" remote per account
```

Use rclone's current scopes when creating your own client id:
`photoslibrary.appendonly`,
`photoslibrary.readonly.appcreateddata`,
`photoslibrary.edit.appcreateddata`.

Set each account's `rclone_remote` (and a Drive remote in `storage_rclone`
so free space can be read — `rclone about` is unsupported on photos remotes).

## Run

```bash
# Preview only — lists + hashes, plans, changes nothing:
.venv/bin/python main.py --dry-run

# Real run: uploads verified copies, writes the removal manifest:
.venv/bin/python main.py
```

### What happens at the end

```
[summary] photos_hashed=1200
[summary] duplicate_groups=340
[summary] unique_groups=860
[summary] bytes_to_target=1.8 GB        # what must fit in the target
[summary] bytes_freeable_from_sources=4.5 GB
[summary] queued_or_would_queue_removal=860
[summary] errors=0
```

If the plan exceeds the target's free space the tool **aborts before
anything is moved** (dry-run just warns). Open `removal_manifest.jsonl`,
click the links, and delete the source copies in the Photos web UI.

### Flags

| flag | meaning |
|---|---|
| `--dry-run` | plan only, no mutations |
| `--backend` | override backend (`oauth`\|`rclone`); env `GPC_BACKEND` |
| `--accounts` | comma-separated account ids to process; env `GPC_ACCOUNTS` |
| `--config` | config file path (env `GPC_CONFIG` also works; default `config.yaml`) |
| `--jobs N` | hash-worker parallelism (env `GPC_JOBS` also works; default `concurrency` in config) |
| `--limit N` | process at most N distinct hashes (dry-run safety net) |
| `--self-test` | run unit tests, then exit (no credentials required) |
| `--log-level` | DEBUG / INFO / WARNING / ERROR |

## Safety & privacy

- **No photo bytes are ever persisted** by this tool: hashing and copying
  stream bytes through pipes; uploads are spooled only in memory (or a
  deleted temp file), never to a photo store.
- **Self-checking**: every upload is re-read from the TARGET and compared
  (SHA-256) against the source before anything is queued for removal. If a
  check fails the source copy is preserved and an error recorded.
- **Idempotent**: re-runs detect earlier `gpc_*` marker copies and skip
  re-uploading.
- **Private logging**: only numeric account ids and 8-char hash prefixes are
  logged. Emails, tokens, filenames and content never leave the machine
  (log entries are additionally redacted).
- **No secrets in the repo**: tokens, manifest, client secrets and config
  are gitignored.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Layout

```
google_photos_consolidator/
├── config.yaml            # accounts, backend, scopes, tuning
├── main.py                # orchestrator
├── backend/
│   ├── base.py            # abstract backend + capability model
│   ├── google_oauth.py    # Library API implementation
│   └── rclone_backend.py  # rclone implementation
├── utils/
│   ├── hashing.py         # streaming SHA-256
│   ├── storage.py         # free-space + target selection
│   ├── logger.py          # privacy-safe logging
│   ├── manifest.py        # manual-removal manifest (local, 0600)
│   └── validator.py       # self-checking helpers
└── tests/
```