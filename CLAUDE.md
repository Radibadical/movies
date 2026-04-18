# Movie List Maintainer — Claude Context

## Project overview

This project maintains a Google Sheets movie list using OMDb data and exposes a
Telegram bot for on-the-go updates. Two entry points:

- **`main.py`** — CLI script; run locally to bulk-fill OMDb data, merge watch list
  tabs, deduplicate, and fix title casing.
- **`bot.py`** — Telegram bot; runs as a systemd user service and handles all
  interactive commands.

## Credentials & secrets

All secrets live in `.env` (git-ignored). Never commit `.env` or `credentials.json`.

| Variable | Purpose |
|---|---|
| `OMDB_API_KEY` | OMDb API key (free tier: 1,000 req/day) |
| `SHEET_NAME` | Exact name of the Google Sheet |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |

`credentials.json` is a Google service-account key file. Both files are in `.gitignore`.

## Google Sheet structure

One spreadsheet, multiple tabs managed by `WORKSHEET_NAMES` in `main.py`.

**Main list tabs** (columns: Rank, Title, Year, Director, Country, Genre, IMDB Rating, Metascore, Notes):
- Movies, Weird Movies, Dudeist Movies, Documentaries, Horror/Halloween, TV, Christmas

**Watch list tab** (columns: Watch Order, Title, Year, Director, Country, Genre, IMDB Rating, Metascore, Category, Notes):
- Watch List — merged from multiple source tabs, each given a Category value

**TV watch list** (same columns as Watch List but no Category):
- TV Watch List

### Watch list merge

When `main.py` detects multiple watch-list source tabs it calls `merge_watch_lists()`,
which combines them into "Watch List" with a Category column, then deletes the source
tabs. Defined in `WATCH_LIST_TABS`:

```python
WATCH_LIST_TABS = {
    "Watch List": "General",
    "Weird Watch List": "Weird",
    "Dudeist Watch List": "Dudeist",
    "Horror Watch List": "Horror",
    "Documentaries Watch List": "Documentary",
    "Christmas Watch List": "Christmas",
}
```

**Important:** when reading rows from the "Watch List" tab itself during a merge,
existing Category values are preserved (not overwritten with "General").

## Running main.py

```bash
cd /home/jakedog/ghq/github.com/Radibadical/Movie_List_Maintainer
source .venv/bin/activate
python main.py              # full run with OMDb API calls
python main.py --skip-omdb  # normalize/merge/sort only, no API calls
```

`--skip-omdb` skips `collect_changes()` entirely — use when near the daily quota.

## Telegram bot

```bash
# Start / stop / status
systemctl --user start movie-list-bot.service
systemctl --user stop movie-list-bot.service
systemctl --user restart movie-list-bot.service
systemctl --user status movie-list-bot.service

# Live logs
journalctl --user -u movie-list-bot.service -f
```

Service file: `~/.config/systemd/user/movie-list-bot.service`
Runs: `.venv/bin/python bot.py` from the project directory.
Restart policy: `on-failure` with 10s delay.

**Always restart the bot after editing `bot.py`.**

### Bot commands

| Command | Description |
|---|---|
| `/addwatch <title> [category]` | Add to Watch List (fetches OMDb data) |
| `/setorder <title> <number>` | Set Watch Order or Rank across all sheets |
| `/watched <title> [| sheet [| note]]` | Remove from Watch List; optionally move to a main sheet with a note |
| `/note <title> | <note text>` | Add/update Notes field |
| `/find <title>` | Substring search across all sheets, full field display |
| `/omdb <title>` | OMDb lookup without touching any sheet |
| `/watchlist [category]` | Show Watch List, optionally filtered by category |
| `/ranked <start> <end> [category]` | Show rank/watch-order range, grouped by sheet; optional category filter |
| `/help` | Show help message |

Categories for `/watchlist` and `/ranked`: General, Weird, Dudeist, Horror,
Documentary, Christmas, TV.

## Key implementation details

### HTML parse mode (critical)

All `reply_text()` calls use `parse_mode="HTML"`. Dynamic content is always wrapped
in `html()`:

```python
def html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```

**Never use Markdown parse mode** — movie titles with special characters
(`*`, `_`, `[`, etc.) will cause "Can't parse entities" errors.

### Exception variable naming

Use `except Exception as err:` throughout `bot.py`. Do NOT use `as e:` — `e` was
previously the name of the escape helper and shadowing it caused silent bugs.

### `html()` function name safety

The escape helper is named `html()` specifically because Python's `except X as e:`
pattern creates a local binding that shadows outer names. `html` cannot be shadowed
this way.

### Column normalization

`normalize_columns(ws, target_cols)` ensures columns exist in the correct order.
It clears and rewrites the sheet when order/presence differs. Reports missing and
reordered columns to stdout.

### Deduplication

`dedup_titles(ws, headers)` deduplicates per `(title.lower(), category.lower())`.
- One entry has notes → keep the noted one.
- Both have notes → prompt interactively.
- Neither has notes → keep first occurrence.

### Title casing

`check_title_casing(ws, headers)` applies Chicago-style title case with an interactive
accept/reject prompt per title. Preserves acronym casing (e.g. "MCU" stays "MCU").

## Dependencies

```
gspread>=6.0.0
google-auth>=2.0.0
requests>=2.31.0
python-dotenv>=1.0.0
python-telegram-bot>=21.0
```

Install: `pip install -r requirements.txt` (inside `.venv`).

## .gitignore

Sensitive files already ignored: `.env`, `credentials.json`, `__pycache__/`,
`*.pyc`, `.venv/`, `*-context.txt`.
