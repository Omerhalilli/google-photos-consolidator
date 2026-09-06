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

## Install (step by step, for a normal person)

### 1. Get the files
Download this repository (green **Code ▾ → Download ZIP**) and unzip it, or:
```bash
git clone https://github.com/Omerhalilli/google-photos-consolidator.git
cd google-photos-consolidator
```

### 2. Install Python 3.9+ if you don't have it
- **Windows**: install from [python.org](https://www.python.org/downloads/). 
  ⚠️ **Check "Add Python to PATH" during install** — if you don't, `python` will not work in your terminal.
- **macOS / Linux**: usually already installed. Check with `python3 --version`.

### 3. Create a virtual environment and install dependencies
Run these commands *inside the `google-photos-consolidator` folder*.

**Linux / macOS:**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Windows (Command Prompt / PowerShell):**
```bat
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
> If `py` is missing, try `python -m venv .venv` and `.venv\Scripts\pip install -r requirements.txt`.

### 4. Verify everything installed correctly (no credentials needed)
```bash
# Linux / macOS:
.venv/bin/python main.py --self-test

# Windows:
.venv\Scripts\python main.py --self-test
```
You should see `OK` with **14 tests passing**. If that works, the code is sound and the only remaining step is giving it real Google credentials.

## Configure (OAuth backend) — the long version

This is the only part that needs a bit of setup. It's a one-time cost per Google account.

### A. Create a Google Cloud project
1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Click the project dropdown top-left → **New Project** → name it (e.g. `photos-consolidator`) → **Create**.

### B. Enable the two required APIs
1. **APIs & Services → Library**.
2. Search and **Enable** both:
   - **Photos Library API**
   - **Google Drive API**
   (Free space is read from Drive because Google Photos shares the same storage pool — no extra cost.)

### C. Create the OAuth client (one per Google account)
This step must be repeated for **each** account you want to consolidate.
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID.**
2. If asked, configure the **OAuth consent screen** first:
   - External, app name `photos-consolidator`, your email. (Still in "Testing" mode is fine for your own accounts.)
3. Application type: **Desktop app** → name it → **Create**.
4. Click **Download JSON** → this is your `client_secret.json`. Save it somewhere safe. **Never commit it to a repo.**
5. While in the consent screen, add your Gmail address(es) to **Test users** so the browser authorization is allowed.

### D. Tell the tool about the credentials
Set two environment variables (per shell session):
```bash
# Linux / macOS:
export GPC_OAUTH_CLIENT_SECRET=/full/path/to/client_secret.json
export GPC_OAUTH_TOKEN_DIR=/full/path/where/tokens/are/saved

# Windows (PowerShell):
$env:GPC_OAUTH_CLIENT_SECRET="C:\path\to\client_secret.json"
$env:GPC_OAUTH_TOKEN_DIR="C:\path\to\token\folder"
```
`GPC_OAUTH_TOKEN_DIR` can be any empty folder — the tool stores your login tokens there locally and never uploads them.

### E. Put your accounts in config.yaml
Open `config.yaml` and make sure you have one entry per account with a unique `id`:
```yaml
accounts:
  - id: 1
    oauth_token: account_1.json
  - id: 2
    oauth_token: account_2.json
```
The token filename can be anything; it just needs to differ per account.

### F. Verify the connection (this is the "did it actually work?" test)
```bash
.venv/bin/python main.py --verify --log-level DEBUG
```
The first time, a browser tab opens and asks you to **log in and click Allow** for each account in turn. Afterwards:
- `account 1: CONNECTED`  and
- `account 2: CONNECTED`
means your credentials are valid. If an account prints `FAILED`, follow the troubleshooting table below.

> `--verify` only *lists* what the API can see — it uploads and changes nothing.

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
Then verify with:
```bash
.venv/bin/python main.py --backend rclone --verify
```

## Run

```bash
# Preview only — lists + hashes, plans, changes nothing:
.venv/bin/python main.py --dry-run

# Real run: uploads verified copies, writes the removal manifest:
.venv/bin/python main.py
```

## Troubleshooting (when something "doesn't work")

| Symptom | Cause | Fix |
|---|---|---|
| `python: command not found` / `py: command not found` | Python not on PATH | Reinstall Python and tick **"Add Python to PATH"**; reopen the terminal. |
| `env 'GPC_OAUTH_CLIENT_SECRET' not set` | env var not exported in this shell | Run the `export`/`$env:` lines again in the SAME terminal window before `main.py`. |
| `Permission denied` on download / token dir | token dir unwritable | `GPC_OAUCT_TOKEN_DIR` must point to a folder you can write to; create it first (`mkdir -p`). |
| Browser opens but says **"This app is blocked"** / no Allow button | OAuth consent screen not finished | Go to consent screen → **Publishing status: Testing** → add your email under **Test users**. |
| `--verify` prints `FAILED` with HTTP 403 / "access not configured" | Photos Library API not enabled for that project | APIs & Services → Library → enable **Photos Library API** (and Drive API) for the *same* project. |
| `--verify` prints `account N: FAILED` with a refresh-token error | Token expired / revoked | Delete the token file in `GPC_OAUTH_TOKEN_DIR` for that account and run `--verify` again to re-authorize. |
| `config not found: config.yaml` | You ran it from the wrong folder | `cd` into the `google-photos-consolidator` folder first (the `--config` flag / `GPC_CONFIG` env overrides the path). |
| API sees **0 items** | You uploaded photos via the Photos app/website; the 2026 API only exposes app-created content | This is expected and unavoidable (see "What Google actually allows"). The tool still uploads & dedupes everything the API exposes. |
| `No module named google.auth` or `ImportError` | Dependencies not installed | Rerun the `pip install -r requirements.txt` step in step 3. |
| Process hangs on `run_local_server` / port | OAuth needs a browser on the same machine as the tool | Run on your local machine (not a headless server/SSH-only box) for the one-time authorization. |

If your symptom isn't listed, run:
```bash
.venv/bin/python main.py --verify --log-level DEBUG
```
and send the top of the output (it is redacted — no emails, tokens or filenames).

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
| `--verify` | test the connection to every account (no uploads, no changes) |
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