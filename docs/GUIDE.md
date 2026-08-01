# Douyin Downloader — Usage Guide

A batch downloader for Douyin: videos, image-notes, collections/mixes, music,
favorites, and whole creator profiles — with progress bars, retries, SQLite
dedup, browser fallback, optional transcription, notifications, and a REST API
mode.

This guide covers install → cookies → config → running. For the HTTP API see
[API.md](./API.md).

---

## 1. Requirements

- **Python 3.9+** (3.12 tested)
- OS: Windows / macOS / Linux
- For browser fallback & automatic cookie capture: a Chromium browser
  (Playwright is installed as part of setup below)

---

## 2. Install

```bash
# from the repo root: d:\douyin-downloader
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

Optional extras (install only what you need):

| Feature | Install |
|---------|---------|
| REST API server mode | `pip install fastapi uvicorn` |
| Browser fallback / auto cookie capture | `pip install playwright` then `playwright install chromium` |

---

## 3. Create your config

Copy the example and edit it:

```bash
# Windows PowerShell
Copy-Item config.example.yml config.yml
# macOS/Linux
# cp config.example.yml config.yml
```

The CLI reads `config.yml` by default (override with `-c <path>`).

---

## 4. Get cookies (required for most downloads)

Douyin needs a logged-in session. **Recommended — automatic capture:**

```bash
python -m tools.cookie_fetcher --config config.yml
```

A browser opens → log into Douyin → return to the terminal and press Enter.
Cookies are written into your `config.yml` automatically.

Manual alternative: fill the `cookies:` block in `config.yml` with values
copied from your browser dev-tools (`msToken`, `ttwid`, `odin_tt`,
`passport_csrf_token`, `sid_guard`, …).

> Without valid cookies many URLs will fail. Favorites modes (`collect` /
> `collectmix`) only work for the account the cookies belong to.

---

## 5. Configure what to download

Edit `config.yml`. Key sections:

```yaml
link:
  - https://www.douyin.com/video/7123456789012345678   # add one or more URLs

path: ./Downloaded/          # output directory

# which assets to save
video: true
music: true                  # download audio track (.mp3)
cover: true
avatar: true
json: true                   # save metadata .json

thread: 5                    # concurrent workers
retry_times: 3
database: true               # SQLite dedup (skip already-downloaded)
```

### Profile / batch modes

When a `link` points to a creator profile (`/user/{sec_uid}`), `mode` decides
what to pull:

```yaml
mode:
  - post          # their posts
number:
  post: 50        # how many (0 = all)
```

| Mode | Meaning |
|------|---------|
| `post` | Creator's posts |
| `like` | Creator's liked posts |
| `mix` | Their collections/mixes |
| `music` | Their music |
| `collect` | Logged-in account's favorites (use **alone**) |
| `collectmix` | Logged-in account's favorite collections (use **alone**) |

`number.<mode>: 0` means "all". `increase.<mode>: true` enables incremental
downloads (stop once you hit already-downloaded items) for
`post/like/mix/music`.

---

## 6. Run

### Simplest — everything from `config.yml`

```bash
python run.py
```

### Pass a URL on the command line (no config edits needed)

```bash
python run.py -u https://www.douyin.com/video/7123456789012345678
python run.py -u <url1> -u <url2>            # multiple
```

### Common flags

| Flag | Purpose |
|------|---------|
| `-u, --url <url>` | Download URL (repeatable) |
| `-c, --config <path>` | Config file (default `config.yml`) |
| `-p, --path <dir>` | Output directory (overrides config) |
| `-t, --thread <n>` | Concurrent workers |
| `-v, --verbose` | Verbose console logs |
| `--show-warnings` | Show warning-level logs |
| `--version` | Print version |

Supported URL types: single video / image-note, `/collection/{id}`,
`/mix/{id}`, `/music/{id}`, `/user/{sec_uid}` profiles, live rooms, and short
links (`v.douyin.com/...`, resolved automatically).

Output lands under `path/` (default `./Downloaded/`), organized per the
`folderstyle` / `filename_template` / `folder_template` settings in config.

---

## 7. Other run modes

### Hot board export (no download, writes JSONL)

```bash
python run.py --hot-board          # all
python run.py --hot-board 20       # top 20
```

### Keyword search export (JSONL)

```bash
python run.py --search "keyword" --search-max 50
```

### REST API server mode

```bash
pip install fastapi uvicorn
python run.py --serve --serve-host 127.0.0.1 --serve-port 8000
```

Then `POST` URLs and poll job status over HTTP — full reference in
[API.md](./API.md).

---

## 8. Optional features (config blocks)

| Feature | Config block | Notes |
|---------|--------------|-------|
| Transcription | `transcript:` | Extracts audio + calls a Whisper-style API; needs `OPENAI_API_KEY` |
| Comments | `comments:` | Saves `*_comments.json` per aweme |
| Live recording | `live:` | Records `live.douyin.com/{room_id}` until offline or a duration cap |
| Notifications | `notifications:` | Bark / Telegram / webhook on success/failure |
| Browser fallback | `browser_fallback:` | Uses a real browser when the API can't paginate (best-validated for `post`) |
| Proxy | `proxy:` | e.g. `http://127.0.0.1:7890` |

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Unsupported URL` | URL type not supported, or a short link resolved to a bare page — check the link |
| Everything fails / empty results | Cookies missing or expired → re-run `python -m tools.cookie_fetcher` |
| `REST server mode requires fastapi + uvicorn` | `pip install fastapi uvicorn` |
| Nothing downloads on re-run | SQLite dedup skipping already-downloaded items; set `database: false` or delete `dy_downloader.db` to force |
| Profile only gets a few items | Raise `number.<mode>` (0 = all); for `like/mix/music`, ensure cookies + API pagination work |
| Want more log detail | add `-v` |

---

## Quick start (TL;DR)

```bash
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.yml config.yml
python -m tools.cookie_fetcher --config config.yml
python run.py -u https://www.douyin.com/video/7123456789012345678
```
