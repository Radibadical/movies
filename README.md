# Movie List Maintainer

Keeps a Google Sheets movie list up to date with OMDb data and provides a
Telegram bot for on-the-go additions and edits.

Two entry points:

- **`main.py`** — CLI script; bulk-fills OMDb fields, merges watch list tabs,
  deduplicates titles, fixes title casing, renumbers integer ranks, sorts
  star-rated rows.
- **`bot.py`** — Telegram bot; runs as a persistent background service and
  handles all interactive commands.

---

## Features

- Fills **Year**, **Director**, **Country**, **Genre**, **IMDB Rating**, and
  **Metascore** via the [OMDb API](https://www.omdbapi.com/)
- Never touches **Rank**, **Notes**, **Title**, **Watch Order**, **Date Added**,
  or **Last Watched**
- Supports multiple worksheet tabs in a single spreadsheet
- Merges category-specific watch list tabs (Weird, Horror, etc.) into one
  unified **Watch List** tab with a **Category** column
- Deduplicates titles per sheet (keeps noted entries; prompts when both have notes)
- Chicago-style title case correction with interactive prompts
- Renumbers integer ranks sequentially based on row order
- Sorts star-rated rows by rating (descending) then title (alphabetical)
- `--skip-omdb` flag to run without API calls (normalize, merge, sort only)

---

## Sheet structure

### Main list tabs

Columns: `Rank | Title | Year | Director | Country | Genre | IMDB Rating | Metascore | Last Watched | Notes`

Default tabs: Movies, Weird Movies, Dudeist Movies, Documentaries,
Horror/Halloween, TV, Christmas

**Rank column — two zones:**

- **Numbered ranks (1–200)**: stored inside a Google Sheets Table. Rows are
  renumbered sequentially by `main.py` based on their current row order.
- **Star ratings**: regular rows below the table, separated by a blank row.
  Format: `★ ★ ★ ★ ✮` (full stars + optional `✮` half-star, space-separated).
  Valid values: 5, 4.5, 4, 3.5, 3, 2.5.
- Sort order: numbered ranks ascending first, then star ratings descending, then
  alphabetical within the same star value.

### Watch List tab

Columns: `Watch Order | Title | Year | Director | Country | Genre | IMDB Rating | Metascore | Category | Date Added | Notes`

A single **Watch List** tab holds everything, with a **Category** column to
distinguish General / Weird / Dudeist / Horror / Documentary / Christmas entries.
The CLI will detect and merge separate category-specific watch list tabs
(e.g. "Weird Watch List") automatically on first run.

**Date Added** is auto-filled (ISO format `YYYY-MM-DD`) when `/addwatch` is used.

### TV Watch List tab

Same column layout as Watch List (no Category), managed separately.

### History tab

Columns: `Date | Type | Title | Detail`

Auto-created on first rank change or `/watched` event. Logs:
- **Rank Changed** — title, old rank → new rank
- **Watched** — title, sheet it was added to

---

## Setup

### 1. Get a free OMDb API key

1. Go to https://www.omdbapi.com/apikey.aspx
2. Choose the **Free** tier (1,000 requests/day)
3. Check your email and click the activation link

### 2. Google Cloud setup

#### 2a. Create a project

1. Go to https://console.cloud.google.com
2. Click the project dropdown → **New Project** → name it → **Create**

#### 2b. Enable APIs

1. **APIs & Services → Library**
2. Enable **Google Sheets API**
3. Enable **Google Drive API**

#### 2c. Create a Service Account

1. **APIs & Services → Credentials → Create Credentials → Service Account**
2. Give it a name → **Create and Continue → Done**
3. Click the service account → **Keys → Add Key → Create new key → JSON**
4. Move the downloaded `credentials.json` into this project folder

> `credentials.json` is in `.gitignore` and will never be committed.

#### 2d. Share your Google Sheet with the service account

1. Copy the `client_email` from `credentials.json`
2. Open your Google Sheet → **Share** → paste the email → **Editor** → **Share**

### 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure environment

Copy `.env.example` to `.env` (or create it) and fill in your values:

```ini
OMDB_API_KEY="your_omdb_key"
SHEET_NAME="Your Google Sheet Name"
TELEGRAM_BOT_TOKEN="your_bot_token"   # only needed for the bot
```

### 5. Edit worksheet tabs (optional)

Open `main.py` and update `DEFAULT_WORKSHEETS` to match your tab names:

```python
DEFAULT_WORKSHEETS = [
    "Movies",
    "Weird Movies",
    "Dudeist Movies",
    "Documentaries",
    "Horror/Halloween",
    "TV",
    "Watch List",
    "TV Watch List",
    "Christmas",
]
```

---

## Running the CLI

```bash
source .venv/bin/activate

# Full run — fetch OMDb data for every sheet
python main.py

# Skip OMDb calls — only normalize columns, merge watch lists, sort
python main.py --skip-omdb
```

For each tab the script will:

1. Normalize column order (add missing columns, report reorders)
2. Deduplicate titles (prompt when both copies have notes)
3. Check title casing and prompt to accept/reject each suggestion
4. For main list tabs: renumber integer ranks and sort star-rated rows
5. Fetch OMDb data for rows with empty fields (unless `--skip-omdb`)
6. Preview all proposed changes and ask `[y/N]` before writing
7. Sort watch list tabs by Watch Order

---

## Telegram Bot

### Register with BotFather

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token into your `.env` as `TELEGRAM_BOT_TOKEN`

### Run as a systemd service (Linux)

```bash
# Install service file
cp movie-list-bot.service ~/.config/systemd/user/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable movie-list-bot.service
systemctl --user start movie-list-bot.service

# Check status / logs
systemctl --user status movie-list-bot.service
journalctl --user -u movie-list-bot.service -f
```

### Bot commands

| Command | Description |
|---|---|
| `/addwatch <title> [category]` | Add to Watch List (fetches OMDb data, records Date Added). Categories: General, Weird, Dudeist, Horror, Documentary, Christmas, TV |
| `/setorder <title> <rank>` | Set Watch Order or Rank; plain number = numeric rank (`4`), `4stars`/`4.5stars` = star rating. Repositions the row to its sorted position. |
| `/watched <title> [| sheet [| note [| rank]]]` | Remove from Watch List; optionally move to a main sheet with a note and rank. Falls back to OMDb if movie isn't in the watch list. Stamps Last Watched date. |
| `/history [n]` | Show last n rank changes and watched events (default 10) |
| `/note <title> | <note text>` | Add or update the Notes field |
| `/find <title>` | Substring search across all sheets with full field display |
| `/omdb <title>` | OMDb lookup without modifying any sheet |
| `/watchlist [category]` | Show the Watch List, optionally filtered by category |
| `/ranked <start> <end> [category]` | Show movies in a rank/watch-order range, grouped by sheet. Optional category filter (e.g. `/ranked 1 10 Weird`) |
| `/help` | Show command reference |

---

## Security notes

- `.env` and `credentials.json` are in `.gitignore` — never commit them
- The bot responds to any Telegram user by default; restrict access by chat ID
  if you want to keep it private
