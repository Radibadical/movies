# Movie List Maintainer — Claude Context

## Project overview

This project maintains a Google Sheets movie list using OMDb data and exposes a
Telegram bot for on-the-go updates. Two entry points:

- **`main.py`** — CLI script; run locally to bulk-fill OMDb data, merge watch list
  tabs, deduplicate, fix title casing, renumber integer ranks, and sort star-rated rows.
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

**Main list tabs** (columns: Rank, Title, Year, Director, Country, Genre, IMDB Rating, Metascore, Last Watched, Notes):
- Movies, Weird Movies, Dudeist Movies, Documentaries, Horror/Halloween, TV, Christmas

**Rank column — two zones per sheet:**
- **Numbered ranks (1–200)**: stored inside a Google Sheets Table object. Inserting within this range via `insertDimension` automatically expands the table.
- **Star ratings**: regular rows below the table, separated by a blank row. Format: `★ ★ ★ ★ ✮` (full stars + optional `✮` half-star, space-separated). Valid values: 5, 4.5, 4, 3.5, 3, 2.5.
- Sort order: numbered ranks ascending first, then star ratings descending, then alphabetical within the same star value.
- Blank Rank cells (the separator row) are skipped during insertion scans — handled by `if r and _rank_sort_key(r) >= new_key`.

**Watch list tab** (columns: Watch Order, Title, Year, Director, Country, Genre, IMDB Rating, Metascore, Category, Date Added, Notes):
- Watch List — merged from multiple source tabs, each given a Category value
- Date Added: ISO date (YYYY-MM-DD) auto-filled by `/addwatch`

**TV watch list** (same columns as Watch List but no Category):
- TV Watch List

**History log tab** (columns: Date, Type, Title, Detail):
- History — auto-created on first event; never in WORKSHEET_NAMES (not processed by main.py)
- Type values: "Rank Changed" (from /setorder), "Watched" (from /watched)

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

**Per-tab processing order (non-watch-list sheets):**
1. `normalize_columns` — add/reorder columns
2. `dedup_titles` — remove duplicates
3. `check_title_casing` — interactive title case prompts
4. `renumber_ranks` — reassign integer ranks sequentially by row order
5. `sort_star_rated_rows` — sort star-rated rows by rating desc, then title asc
6. `collect_changes` / apply OMDb updates (skipped with `--skip-omdb`)

Watch list tabs skip steps 4–5 and instead sort by Watch Order after OMDb updates.

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
| `/addwatch <title> [category]` | Add to Watch List (fetches OMDb data, records Date Added) |
| `/setorder <title> <rank>` | Set Watch Order or Rank; plain number = numeric rank, `4stars`/`4.5stars` = star rating |
| `/watched <title> [| sheet [| note [| rank]]]` | Remove from Watch List; rank accepts same format as /setorder; falls back to OMDb if not in watch list; stamps Last Watched |
| `/history [n]` | Show last n rank changes and watched events (default 10, max 50) |
| `/note <title> | <note text>` | Add/update Notes field |
| `/find <query>` | Search every tab and every column in the spreadsheet (not just Title) |
| `/omdb <title>` | OMDb lookup without touching any sheet |
| `/watchlist [category]` | Show Watch List, optionally filtered by category |
| `/ranked <start> <end> [category]` | Show rank/watch-order range, grouped by sheet; optional category filter |
| `/random [genre]` | Suggest a random movie from the Watch List; optional genre substring filter |
| `/help` | Show help message |

Categories for `/watchlist` and `/ranked`: General, Weird, Dudeist, Horror,
Documentary, Christmas, TV.

### `/find` behaviour
Searches every worksheet in the spreadsheet (including History) via `ss.worksheets()`,
not just `WORKSHEET_NAMES`. Matches any cell in each row, not just the Title column.
Non-standard columns (e.g. History's Date/Type/Detail) are displayed at the bottom of
each result block.

### `/random` behaviour
Draws only from the "Watch List" tab (not TV Watch List or other sheets). Genre argument
is a case-insensitive substring match against the Genre column (e.g. `horror` matches
"Horror, Thriller").

### `/history` filtering
Watch list rank changes (Watch Order updates) are excluded. Only "Rank Changed" events
where the sheet name does not contain `WATCH_LIST_KEYWORD` and the new rank is a plain
integer 1–200 are shown.

### `/addwatch` table insertion
Uses `insert_at = max(2, len(all_values))` to insert within the Google Sheets Table
range rather than one row past the end. Inserting within the table range triggers
`insertDimension`, which auto-expands the table boundary.

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

- **Insert-only path** (no reordering needed): uses `ws.insert_cols([[col_name]], col=pos)`,
  which calls `insertDimension` under the hood. Google Sheets Tables whose range is
  intersected automatically expand — this is the table-safe path.
- **Clear+rewrite path** (existing columns need reordering): clears and rewrites the
  full sheet. Prints a warning to verify the Table range in Sheets afterward, since
  the Table boundary does not automatically adjust on a full rewrite.

### Rank helpers (bot.py)

```python
VALID_STAR_VALS: frozenset[float] = frozenset({2.5, 3.0, 3.5, 4.0, 4.5, 5.0})

def _stars_to_str(val: float) -> str:
    # "★ ★ ★ ★ ✮" for 4.5, etc.

def _rank_sort_key(s: str) -> tuple[int, float]:
    # (0, int_rank) for integers, (1, -star_val) for stars, (2, 0.0) otherwise
    # Used where title is not available (e.g. history filtering)

def _rank_sort_key_with_title(rank_val: str, title: str) -> tuple:
    # (0, int_rank, "") for integers; (1, -star_val, title_lower) for stars
    # Used by _reposition_by_rank and /watched insertion to sort alphabetically
    # within the same star group

def _parse_rank_input(s: str) -> tuple[str, str] | None:
    # "4" → ("4", "rank #4"); "4stars" → ("★ ★ ★ ★", "4 stars"); returns None on bad input
```

Input disambiguation: plain number = numeric rank; `Nstars` or `N.5stars` = star rating.

### Row repositioning after rank change

`_reposition_by_rank(ws, row_num, stored_rank, canonical_title) -> bool` (bot.py):
- Called by `/setorder` for non-watch-list sheets after updating the rank cell.
- Re-reads all sheet values (capturing the already-updated rank and all other fields).
- Finds the first row (skipping current) whose sort key ≥ new key; deletes current
  row and re-inserts at that position (offset adjusted for the deletion).
- Sort key: `(0, int_rank, "")` for integers; `(1, -stars, title_lower)` for star ratings.
- Returns `False` (no-op) if the row is already in the correct position.

### History log

`_append_log(ss, event_type, title, detail)` (bot.py):
- Lazy-creates the "History" tab with `LOG_COLUMNS = ["Date", "Type", "Title", "Detail"]`
  on first call. Silently swallows all errors so logging never breaks a command.
- Called by `/setorder` ("Rank Changed") and `/watched` ("Watched").

### Deduplication

`dedup_titles(ws, headers)` deduplicates per `(title.lower(), category.lower())`.
- One entry has notes → keep the noted one.
- Both have notes → prompt interactively.
- Neither has notes → keep first occurrence.

### Title casing

`check_title_casing(ws, headers)` applies Chicago-style title case with an interactive
accept/reject prompt per title. Preserves acronym casing (e.g. "MCU" stays "MCU").

### Rank renumbering and star sorting (main.py)

`renumber_ranks(ws, headers)`: reassigns integer Rank values (1, 2, 3…) sequentially
by current row order. Skips star-rated and blank Rank cells. Batch `update_cells`.

`sort_star_rated_rows(ws, headers)`: sorts rows containing `★`/`✮` in Rank by star
value descending, then title alphabetically. Single batch write.

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

## Web UI

**Branch:** `feature/web-ui`
**File:** `index.html` (repo root on that branch)
**Live URL:** https://radibadical.github.io/Movie_List_Maintainer/

A single-file static page deployed via GitHub Pages. No backend — reads directly
from Google Sheets. The repo must be public for GitHub Pages to work on the free plan.

### How it works

Fetches the Movies sheet via the Google Sheets CSV export endpoint:
```
https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&sheet=Movies
```

The `gviz/tq` JSON endpoint was tried first but does not work with Google Sheets
Table objects — it concatenates all cell values into the column label field and
truncates rows. The CSV export has no such issue.

The sheet must be shared as **"Anyone with the link — Viewer"** for the fetch to work.
This exposes the spreadsheet ID in the source but does not expose your email address
(the CSV endpoint returns data only, no owner metadata).

### Updating the page

Edit `index.html` on the `feature/web-ui` branch and push. GitHub Pages redeploys
automatically within ~1 minute.

```bash
git checkout feature/web-ui
# edit index.html
git add index.html && git commit -m "..." && git push
```

### Page structure

The page splits Movies into three sections via a tab nav:

| Tab | URL hash | Filter |
|---|---|---|
| 1–100 | `#top100` | Integer ranks 1–100 |
| 101–200 | `#top200` | Integer ranks 101–200 |
| ★ Rated | `#starred` | Rows with `★`/`✮` in Rank |

The hash is written to the URL on tab switch, so links like
`/Movie_List_Maintainer/#starred` deep-link to a specific section.

All data is fetched once and filtered client-side — switching tabs makes no
additional network requests.

**Search** runs across all three sections regardless of which tab is active.
Clearing the search returns to the active tab's filtered view.

Both a desktop table and mobile card layout are rendered simultaneously;
CSS hides the appropriate one at a 700px breakpoint. No JS resize handling needed.

### Adding more sheets

The page currently shows only the Movies sheet. To add other sheets:
1. Add a new `<button class="page-tab">` in the nav HTML
2. Add an entry to the `PAGES` object with a rank filter function
3. Fetch the additional sheet and merge or handle separately

### Branch structure

| Branch | Purpose |
|---|---|
| `main` | Bot-only code (`bot.py`, `main.py`) — stable |
| `feature/web-ui` | Web UI (`index.html`) — also contains all bot code |

To abandon the web UI: delete the `feature/web-ui` branch. `main` is unaffected.
To merge it in when ready: `git checkout main && git merge feature/web-ui`.
