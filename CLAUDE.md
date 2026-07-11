# Movie List Maintainer — Claude Context

## Project overview

This project maintains a Google Sheets movie list using OMDb data and exposes a
Telegram bot for on-the-go updates. One active entry point:

- **`bot.py`** — Telegram bot; runs as a systemd user service and handles all
  interactive commands including OMDb lookups, rank changes, and watch list management.

`main.py` was deleted after its functions (OMDb bulk-fill, column normalization,
deduplication, title casing, rank renumbering, star sorting) were superseded by
the bot workflow. The Google Apps Script `history_trigger.gs` logs manual rank
edits made directly in the sheet.

## Credentials & secrets

All secrets live in `.env` (git-ignored). Never commit `.env` or `credentials.json`.

| Variable | Purpose |
|---|---|
| `OMDB_API_KEY` | OMDb API key (free tier: 1,000 req/day) |
| `SHEET_NAME` | Exact name of the Google Sheet |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `ALLOWED_USER_ID` | Telegram user ID; bot ignores all other users |

`credentials.json` is a Google service-account key file. Both files are in `.gitignore`.

## Google Sheet structure

One spreadsheet, multiple tabs managed by `WORKSHEET_NAMES` in `bot.py`.

**Main list tabs** (columns: Rank, Title, Year, Director, Country, Genre, Tags, IMDB Rating, Metascore, Last Watched, Notes):
- Movies, Weird Movies, Dudeist Movies, Documentaries, Horror/Halloween, TV, Christmas

**Rank column — two zones per sheet:**
- **Numbered ranks (1–200)**: stored inside a Google Sheets Table object. Inserting within this range via `insertDimension` automatically expands the table.
- **Star ratings**: regular rows below the table, separated by a blank row. Format: `★ ★ ★ ★ ✮` (full stars + optional `✮` half-star, space-separated). Valid values: 5, 4.5, 4, 3.5, 3, 2.5.
- Sort order: numbered ranks ascending first, then star ratings descending, then alphabetical within the same star value.
- Blank Rank cells (the separator row) are skipped during insertion scans — handled by `if r and _rank_sort_key(r) >= new_key`.

**Watch list tab** (columns: Watch Order, Title, Year, Director, Country, Genre, Tags, IMDB Rating, Metascore, Date Added, Notes):
- Watch List — unified watch list; Tags column used for per-movie labels
- Date Added: ISO date (YYYY-MM-DD) auto-filled by `/addwatch`

**TV watch list** (same columns as Watch List, no Tags routing):
- TV Watch List

**History log tab** (columns: Date, Type, Title, Detail):
- History — auto-created on first event; not in WORKSHEET_NAMES
- Type values: "Rank Changed" (from `/rank`, `/watched`, or the Apps Script trigger), "Watched" (from `/watched`), "Trend Reset" (from `/trend reset`)

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
| `/addwatch <title> [| tag]` | Add to Watch List (fetches OMDb data, records Date Added); use `tv` as tag to route to TV Watch List |
| `/watched <title> [| note [| rank [| tag]]]` | Remove from Watch List and add to Movies sheet; rank accepts same format as /rank; falls back to OMDb if not in watch list; stamps Last Watched |
| `/rank <title> | <rank>` | Set rank in Movies; plain number = numeric rank, `4stars`/`4.5stars` = star rating |
| `/reorder` | Physically re-sort the Movies sheet by Rank; use after manual edits made directly in Google Sheets, which change history_trigger.gs logs but don't move the row |
| `/tag <title> | <tag>` | Append a tag to a movie's Tags field across all sheets where it appears |
| `/untag <title> | <tag>` | Remove a tag from a movie's Tags field across all sheets where it appears (case-insensitive match) |
| `/newtag <tag> | <vibe\|style\|category> [| <#hexcolor>]` | Register a new tag with a type (persisted to `tags.json`); `category` tags require a hex color, `vibe`/`style` share one color per type |
| `/note <title> | <note text>` | Add/update Notes field |
| `/find <query>` | Search every tab and every column in the spreadsheet (not just Title) |
| `/omdb <title>` | OMDb lookup without touching any sheet |
| `/watchlist [tag]` | Show Watch List, optionally filtered by tag |
| `/random [genre [| tag]]` | Suggest a random movie from the Watch List; optional genre and tag filters |
| `/history [n]` | Show last n rank changes and watched events (default 10, max 50) |
| `/trend list` | Show active rank trends (last year) |
| `/trend reset <title>` | Clear the trend indicator for a movie |
| `/help` | Show help message |

### Tags (Genre/Vibe/Style system)

The **Genre** column (from OMDb) covers high-level genre. The **Tags** column covers
everything else, split into three types:

- **Vibe** — how the movie feels (e.g. Dark, Absurdist, Surrealist, Satirical). All
  Vibe tags render in one shared color (blue).
- **Style** — how it's made (e.g. Lynchian, atmospheric). All Style tags render in
  one shared color (pink). Empty until tags are added via `/newtag`.
- **Category** — everything that doesn't fit Vibe/Style: Christmas, Dudeist, Guilty
  Pleasure, Overrated, So Bad It's Good, WTF, Weird, Rewatch. Each Category tag has
  its own individual hex color (unlike Vibe/Style, which share one color per type).

There is still only **one Tags column** per sheet (comma-separated, same as before) —
no schema change. The type/color split is a lookup layer on top of the same free-text
field, not a change to how tags are stored per movie.

`tags.json` stores the vocabulary as `{"vibe": [...], "style": [...], "category": {tag:
hexcolor}}`, loaded into `TAGS` at bot startup. `VALID_TAGS` / `VALID_TAGS_LOWER` are
flattened views across all three types, rebuilt by `_rebuild_tag_lookup()` whenever
`/newtag` adds an entry — no restart needed.

`/addwatch` and `/watched` validate the tag against `VALID_TAGS_LOWER` (case-insensitive,
regardless of type) and reject unknown tags with an error listing valid options. `/tag`
and `/untag` do not validate — they accept any string; an untyped tag just renders with
the default grey badge on the web UI instead of a Vibe/Style/Category color.

**`index.html` duplicates the vibe/style/category classification by hand** (`CATEGORY_CLASS`,
`VIBE_TAGS`, `STYLE_TAGS` in the JS) since the static page never fetches `tags.json` —
only the Google Sheets CSV export. Adding or reclassifying a tag in `tags.json` requires
a matching edit in `index.html` for the color to show up on the web UI.

### `/find` behaviour
Searches every worksheet in the spreadsheet (including History) via `ss.worksheets()`,
not just `WORKSHEET_NAMES`. Matches any cell in each row, not just the Title column.
Non-standard columns (e.g. History's Date/Type/Detail) are displayed at the bottom of
each result block.

### `/random` behaviour
Draws only from the "Watch List" tab (not TV Watch List or other sheets). Genre argument
is a case-insensitive substring match against the Genre column (e.g. `horror` matches
"Horror, Thriller"). Optional tag filter via pipe: `/random horror | weird`. Tag filter
is canonicalized against `VALID_TAGS_LOWER` (same as `/addwatch`/`/watched`) so casing
in the command doesn't need to match the sheet exactly.

### `/watched` syntax
4-part pipe syntax: `title | note | rank | tag`. Sheet is always Movies — no sheet parameter.
Falls back to OMDb lookup if the title is not found in any Watch List. Tag is validated against `VALID_TAGS` and appended to existing Tags on update, or set on insert.

### `/history` filtering
Watch list rank changes (Watch Order updates) are excluded. Only "Rank Changed" events
where the sheet name does not contain `WATCH_LIST_KEYWORD` and the new rank is a plain
integer 1–200 are shown.

### `/addwatch` table insertion
Uses `insert_at = max(2, len(all_values))` to insert within the Google Sheets Table
range rather than one row past the end. Inserting within the table range triggers
`insertDimension`, which auto-expands the table boundary. Writes the row via
`_insert_row_exact` (see below), not gspread's `insert_row()`.

Tag is validated against `VALID_TAGS_LOWER` before the OMDb lookup is made. `tv` is a
special routing keyword (not a tag) that sends the movie to the TV Watch List tab instead
of Watch List; it is detected before tag validation.

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
- Called by `/rank` and `/watched` for non-watch-list sheets after updating the rank cell.
- Re-reads all sheet values (capturing the already-updated rank and all other fields).
- Finds the first row (skipping current) whose sort key ≥ new key; deletes current
  row and re-inserts at that position (offset adjusted for the deletion) via
  `_insert_row_exact`.
- Sort key: `(0, int_rank, "")` for integers; `(1, -stars, title_lower)` for star ratings.
- Returns `False` (no-op) if the row is already in the correct position.

### `_insert_row_exact(ws, values, index)` (bot.py)

Inserts a row at an exact position without gspread's `insert_row()`/`insert_rows()`,
which write the values via the Sheets `values.append()` API. `values.append()`
auto-detects a "table" starting at the given cell and appends after its *last* row —
not necessarily the blank row just inserted — which could land data outside a Google
Sheets Table object near its boundary (e.g. bumping rank 200). `_insert_row_exact`
does it in two explicit steps instead: an `insertDimension` request to open a blank
row (this is what triggers Table auto-expansion) followed by an exact-range
`values.update()` write into that row. Used by `_reposition_by_rank`, `/addwatch`,
and the new-movie path of `/watched`.

### `/reorder`

`cmd_reorder` re-sorts the entire Movies sheet in place to fix drift from manual
edits made directly in Google Sheets (see `history_trigger.gs` — it logs the rank
change but never moves the row). Reads all rows, sorts them with
`_reorder_group_key` (integer ranks asc, then the blank separator row(s), then star
ratings desc + alpha — unlike `_rank_sort_key_with_title`, blanks sort between the
two groups instead of after everything), and overwrites `A2:<lastCol><lastRow>` with
the reordered values in a single `ws.update()`. Row count never changes, so this
never touches the Table's row boundaries. Finishes with `_renumber_ranks` to close
any gaps/duplicates left by manual edits.

**Star vs. integer insertion direction:** Integer rank moves insert AFTER the target row
(pushing it to a lower rank). Star rating moves insert BEFORE the target row (to maintain
alphabetical order within the same star level). The branch:

```python
if not stored_rank.strip().isdigit() and target_insert > row_num:
    adjusted = target_insert - 1
else:
    adjusted = target_insert
```

### 200-movie limit enforcement

`_enforce_200_limit(ss, ws) -> list[str]` (bot.py):
- Called after `_renumber_ranks` in BOTH the new-movie path and existing-movie path of
  `cmd_watched`, but only when the rank being assigned is an integer.
- Loops (up to 10 iterations) re-reading the sheet each time; if there are >200 integer-
  ranked rows, bumps `int_rows[-1]` (highest integer rank) to `★ ★ ★ ★ ★`, repositions
  it alphabetically in the star section, and logs the change.
- Returns list of bumped title strings (empty if no overflow).
- Bot reply for each overflow: a separate `msg_lines` entry —
  `"List full (200) — <b>{title}</b> moved to ★★★★★."` — not embedded in the update suffix.

### History log

`_append_log(ss, event_type, title, detail)` (bot.py):
- Lazy-creates the "History" tab with `LOG_COLUMNS = ["Date", "Type", "Title", "Detail"]`
  on first call. Silently swallows all errors so logging never breaks a command.
- Called by `/rank` ("Rank Changed") and `/watched` ("Watched" and "Rank Changed").

### Rank renumbering and star sorting (bot.py)

`_renumber_ranks(ws)`: reassigns integer Rank values (1, 2, 3…) sequentially
by current row order. Skips star-rated and blank Rank cells. Batch `update_cells`.

`sort_star_rated_rows` logic is handled inline during `_reposition_by_rank` and
`_enforce_200_limit` — there is no standalone sort function in bot.py.

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

**Branch:** `main`
**File:** `index.html` (repo root)
**Live URL:** https://radibadical.com/movies/
**Local path:** `/home/jakedog/ghq/github.com/Radibadical/movies`

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

Edit `index.html` on `main` and push. GitHub Pages redeploys automatically within ~1 minute.

```bash
git add index.html && git commit -m "..." && git push
```

### Page structure

Four tabs rendered from a single data fetch:

| Tab | URL hash | Content |
|---|---|---|
| 1–100 | `#top100` | Integer ranks 1–100 |
| 101–200 | `#top200` | Integer ranks 101–200 |
| ★ Rated | `#starred` | Rows with `★`/`✮` in Rank |
| Recently Watched | `#history` | Last 30 Watched events from History tab |

The hash is written to the URL on tab switch, so links like `/movies/#starred`
deep-link to a specific section. All data is fetched once and filtered client-side.

**Search** runs across all sections regardless of which tab is active. Matches
against every rendered column (`TABLE_COLS`) plus Notes and Tags — Tags isn't a
rendered column but is still searchable (`getFiltered` in index.html), so tag
names like "Weird" surface matching movies even though no Tags column is shown.
Clearing the search returns to the active tab's filtered view.

**Bottom nav** — each tab renders a nav row at the bottom (`buildBottomNav()`) with
buttons for all four sections, so you can switch without scrolling back to the top.

Both a desktop table and mobile card layout are rendered simultaneously;
CSS hides the appropriate one at a 700px breakpoint. No JS resize handling needed.

### Recently Watched tab

`buildHistoryContent` builds a `movieLookup` dict keyed by lowercase title from the
Movies CSV cache, then joins each History row to pull movie fields.

- Columns shown: Date Watched, Rank, Title, Year, Director, Country, Genre, IMDB Rating, Metascore. Notes appear as a sub-row (desktop) or inline (mobile cards).
- **Rewatch rank display:** if a Watched event has the string "rewatched" in its Detail
  field and a same-date Rank Changed entry exists for the same title, the Rank column
  shows `<old> → <new>` (old rank in grey, new in gold). Detected in `fetchHistory` via
  `pendingRankChange` tracking; stored in `rewatchChanges["titleKey|date"]`.
- "Type" and "Detail" columns from the History sheet are not shown in this view.

### Trend arrows

Integer-ranked movies show an up/down arrow next to their rank if a "Rank Changed"
event exists in the History tab within the last **year** (previously 30 days).

- Computed in `fetchHistory()` → `rankChanges` object, keyed by lowercase title.
- Suppressed per-title via `localStorage['movieTrends']` — clicking an arrow stores
  the current rank; if History shows the same rank as stored, arrow is hidden.
- `/trend reset <title>` in the bot appends a "Trend Reset" History entry, which
  clears the arrow on next page load (independent of localStorage).
- `/trend list` in the bot also uses a 1-year window.

### CSS style values (index.html)

Rank and title sizes are matched to avoid visual imbalance:

| Class | font-size | color |
|---|---|---|
| `.col-rank` | 1.125rem | `#a07820` (brand gold) |
| `.col-rank.stars` | 1.125rem | `#d4a017` |
| `.rewatch-old` | 0.85em | `#484f58` (grey, for old rank in rewatch display) |
| `.col-title` | 1.125rem | `#e6edf3` |
| `.card-rank` | 1.25rem | `#a07820` (brand gold) |
| `.card-rank.stars` | 1.25rem | `#d4a017` |
| `.card-title` | 1.25rem | `#e6edf3` |

Brand gold `#a07820` matches the "Radibadical" byline color in the header.

### Header and favicon

Title displays as "Radibadical / Top 200 Movies" with a "← radibadical" back link
and an inline gold "R" SVG mark (`<svg class="site-icon">`).

Favicon links point to root-relative paths (`/favicon.svg`, `/favicon.png`,
`/apple-touch-icon.png`) served from `Radibadical/radibadical.github.io`.

Star ratings use a CSS half-star technique: `✮` is replaced with
`<span class="half-star">★</span>` — a grey star with the left 50% overlaid
in gold via `::before`.

### Domain and hosting setup

Custom domain: `radibadical.com` — registered on Porkbun, DNS via Cloudflare.

| Repo | Serves at |
|---|---|
| `Radibadical/radibadical.github.io` | `radibadical.com` (root) |
| `Radibadical/movies` | `radibadical.com/movies/` |

The `radibadical.github.io` repo is a full landing page with cards linking to
each project. Future projects get their own repos and automatically appear at
`radibadical.com/<reponame>` with no extra DNS configuration.

DNS records on Porkbun:
- **ALIAS** `radibadical.com` → `radibadical.github.io`
- **CNAME** `www.radibadical.com` → `radibadical.github.io`
- MX and SPF records left in place for Porkbun email forwarding
