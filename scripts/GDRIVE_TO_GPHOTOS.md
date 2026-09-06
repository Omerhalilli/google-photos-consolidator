# Drive → Google Photos Uploader

Uploads every image/video inside three Google Drive folders
(`2025 fotolari`, `2026 fotolari`, `Consolidated`), including subfolders,
into the Google Photos account `omerhalilli1234@gmail.com` — **skipping
anything already uploaded** so you never get duplicates.

Script: **`scripts/gdrive_to_gphotos.py`**

## ⚠️ Important: the 2026 Google Photos API limitation

Since **2025-03-31** Google removed the old Photos library scopes. Today the
Photos Library API can only:
- **upload** new items (`appendonly`), and
- **see items that This App previously uploaded** (`readonly.appcreateddata`).

It **cannot** see photos you already have in your library that were added via
the Photos app/website or by other apps. That means the API itself cannot tell
you "this already exists in my library".

**How this script avoids duplicates anyway**: it keeps its own local index
(`gphotos_index.json`) of every file it has uploaded (Drive id, filename,
size, **SHA-256 content hash**, and the resulting photo id). Before uploading
it checks the index by Drive id, by filename, and by content hash, and skips
anything already recorded. Renamed files are still caught by content hash.

> Consequence: if a file already exists in your library *but was never
> uploaded by this script*, the script cannot know and will upload it (a
> duplicate you can delete once in the Photos app). This is a hard Google
> platform limit — no third-party tool can do better in 2026.

---

## Step 1 — Enable the two Google APIs

1. Go to [console.cloud.google.com](https://console.cloud.google.com) (a
   **free** Google Cloud account works).
2. Top-left project selector → **New Project** → name it (e.g. `drive-gphotos`) → **Create**.
3. Make sure the new project is selected.
4. **APIs & Services → Library** and **Enable**:
   - **Google Drive API**
   - **Google Photos Library API**
   (No credit card is needed; both are free.)

## Step 2 — Create OAuth 2.0 credentials (Desktop app)

1. **APIs & Services → OAuth consent screen** → create the consent screen:
   - User type: **External** (fine for your own account).
   - App name: `drive-gphotos`, your email address.
   - On the **Audience / Test users** step, add `omerhalilli1234@gmail.com`
     (so the browser authorization is allowed).
   - Save.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app** → **Create**.
   - Click **Download JSON** → save it as **`client_secret.json`** somewhere safe.
   - **Never commit or share this file.**

## Step 3 — Install the required Python packages

From inside this project folder:

```bash
# Linux / macOS
python3 -m venv .venv
.venv/bin/pip install --upgrade google-auth google-auth-oauthlib \
    google-auth-httplib2 google-api-python-client requests

# Windows (Command Prompt / PowerShell)
py -m venv .venv
.venv\Scripts\pip install --upgrade google-auth google-auth-oauthlib ^
    google-auth-httplib2 google-api-python-client requests
```

(These are the packages that matter; `requirements.txt` in this repo already
has them if you prefer `pip install -r requirements.txt`.)

## Step 4 — Run the script and authorize

```bash
# Linux / macOS
.venv/bin/python scripts/gdrive_to_gphotos.py \
    --client-secret /full/path/to/client_secret.json \
    --token-dir ./drive_gphotos_state

# Windows
.venv\Scripts\python scripts\gdrive_to_gphotos.py ^
    --client-secret C:\path\to\client_secret.json ^
    --token-dir .\drive_gphotos_state
```

- The token dir is a scratch folder that will hold your OAuth token and the
  upload index. Create it or let the script.
- **First run:** a browser tab opens asking you to sign in as
  `omerhalilli1234@gmail.com` and click **Allow**. Do this once.
- The script then locates the three folders, lists every media file
  recursively, downloads each one, content-hashes it, skips anything already
  uploaded, uploads new files in batches, and prints progress like:
  ```
  [INFO] Found 3 folder(s) named "2025 fotolari"
  [INFO] Found 1250 media files in Drive
  [INFO] Files to process after dedupe: 1183
  [INFO] Creating 10 media items in Photos (batch)...
  [INFO] Progress: 10/1183 uploaded, 0 failed
  ...
  [INFO] ALL DONE. New uploads this run: 1183  (failed: 0)
  ```
- **Re-running is safe**: files it already recorded are skipped.

### Useful flags
| flag | meaning |
|---|---|
| `--client-secret PATH` | (required) path to `client_secret.json` |
| `--token-dir DIR` | folder for token + index (default is current folder) |
| `--limit N` | upload at most N files (safety trial) |
| `--log-level DEBUG` | verbose output for troubleshooting |

## Step 5 — Avoid Google Photos upload quota limitations

Google Photos doesn't have a hard daily upload cap for normal amounts, but to
be polite and avoid `429 too many requests`:

- The script uploads in **batches of 10** and waits a short moment between
  batches. This is intentional pacing, not a bug.
- If you get **rate-limit / 429** errors, they are logged and the failed files
  are simply skipped — **re-run the script** later; it will only upload what's
  still missing (nothing is double-uploaded).
- **Large runs**: run it overnight (`nohup ... &` on Linux/macOS) and re-run
  it a few times until every batch succeeds. The index guarantees no dupes.
- If a *single* file is huge (many GB), the whole download + hash + upload are
  done one file at a time, so it still works but takes a while — be patient.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `This app is blocked` / no **Allow** button | On the consent screen set status to **Testing** and add your email under **Test users**. |
| `HttpError 403 ... access not configured` | Both **Drive API** and **Photos Library API** must be enabled for the project you selected. |
| `client_secret.json` not found | Pass the full path with `--client-secret`. |
| Token expired / `RefreshError` | Delete the token file inside `--token-dir` and authorize again. |
| Drive folder "not found" | Make sure the folder names are `exactly` `2025 fotolari`, `2026 fotolari`, `Consolidated`, and that the authorized account can see them. |
| Nothing gets uploaded | The index says everything is already uploaded. To start fresh, delete `gphotos_index.json` (you may get duplicates if the photos truly exist). |

## Layout of the state files (keep them private)
- `gphotos_index.json` — record of every upload (Drive id, name, size, sha256, photo id).
- `gdrive_to_gphotos_tokens.json` — your OAuth token.

Both live in `--token-dir` and should never be committed to git.
