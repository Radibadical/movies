# Movie List Maintainer

A personal movie tracking system built on Google Sheets, with a Telegram bot for on-the-go updates and a public web UI.

**Web UI:** [radibadical.com/movies](https://radibadical.com/movies/)

---

## What it does

- Maintains a ranked list of up to 200 movies, with star ratings for the rest
- Pulls **Year**, **Director**, **Country**, **Genre**, **IMDB Rating**, and **Metascore** from the [OMDb API](https://www.omdbapi.com/) automatically
- Tracks a **Watch List** (with a separate TV Watch List)
- Logs every rank change and watch event in a History tab
- Exposes everything through a Telegram bot

---

## Bot commands

| Command | Description |
|---|---|
| `/addwatch <title> [| tag]` | Add to Watch List; use `tv` as tag for TV Watch List |
| `/watched <title> [| note [| rank [| tag]]]` | Move from Watch List to Movies; stamps Last Watched |
| `/rank <title> | <rank>` | Set rank (`42`, `4stars`, `4.5stars`) |
| `/reorder` | Re-sort Movies by Rank after manual edits made directly in Google Sheets |
| `/tag <title> | <tag>` | Append a tag to a movie's Tags field |
| `/untag <title> | <tag>` | Remove a tag from a movie's Tags field |
| `/newtag <tag> | <vibe\|style\|category> [| <#hexcolor>]` | Register a new tag (saved to `tags.json`); `category` needs a hex color |
| `/note <title> | <note>` | Add or update the Notes field |
| `/find <query>` | Search all tabs and all columns |
| `/omdb <title>` | OMDb lookup without touching any sheet |
| `/watchlist [tag]` | Show the Watch List, optionally filtered by tag |
| `/random [genre [| tag]]` | Suggest a random movie from the Watch List |
| `/history [n]` | Show last n rank changes and watched events (default 10) |
| `/trend list` | Show movies whose rank changed in the last year |
| `/trend reset <title>` | Clear the trend arrow for a movie on the web UI |
| `/help` | Show help |

---

## Web UI

A single-file static page (`index.html`) served via GitHub Pages. Reads directly from Google Sheets — no backend.

**Tabs:** 1–100 · 101–200 · ★ Rated · Recently Watched

Features: full-text search, responsive desktop table + mobile card layout, rank trend arrows, rewatch before/after rank display, deep-linkable URL hashes (`#top100`, `#starred`, etc.).

---

## Setup (self-hosting)

### 1. OMDb API key

Get a free key at [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx) (1,000 req/day).

### 2. Google Cloud

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** → add a JSON key → save as `credentials.json` in this folder
4. Share your Google Sheet with the service account's `client_email` as **Editor**

### 3. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token into `.env`

### 4. Install and configure

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```ini
OMDB_API_KEY=your_key
SHEET_NAME=Your Sheet Name
TELEGRAM_BOT_TOKEN=your_token
ALLOWED_USER_ID=your_telegram_user_id
```

### 5. Run the bot

```bash
source .venv/bin/activate
python bot.py
```

Or as a systemd user service:

```bash
cp movie-list-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now movie-list-bot.service
```

---

## Sheet structure

| Tab | Columns |
|---|---|
| Movies (and other main tabs) | Rank, Title, Year, Director, Country, Genre, Tags, IMDB Rating, Metascore, Last Watched, Notes |
| Watch List | Watch Order, Title, Year, Director, Country, Genre, Tags, IMDB Rating, Metascore, Date Added, Notes |
| TV Watch List | same as Watch List |
| History | Date, Type, Title, Detail |

Ranks are either integers (1–200, inside a Sheets Table) or star strings (`★ ★ ★ ★ ✮`).

---

## Security

`credentials.json` and `.env` are in `.gitignore` and are never committed.
The bot ignores all Telegram users except the one configured in `ALLOWED_USER_ID`.
