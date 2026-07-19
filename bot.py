#!/usr/bin/env python3
"""Movie List Maintainer — Telegram bot interface."""

import datetime
import json
import logging
import os
import random
import re

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
SHEET_NAME = os.environ.get("SHEET_NAME", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

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
WORKSHEET_NAMES = [
    w.strip()
    for w in os.environ.get("WORKSHEET_NAMES", ",".join(DEFAULT_WORKSHEETS)).split(",")
    if w.strip()
]

WATCH_LIST_KEYWORD = "Watch List"
MOVIE_LIST_COLUMNS = ["Rank", "Title", "Year", "Director", "Country", "Genre", "Tags", "IMDB Rating", "Metascore", "Last Watched", "Notes"]
WATCH_LIST_COLUMNS = ["Watch Order", "Title", "Year", "Director", "Country", "Genre", "Tags", "IMDB Rating", "Metascore", "Date Added", "Notes"]
LOG_TAB = "History"
LOG_COLUMNS = ["Date", "Type", "Title", "Detail"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def open_spreadsheet(credentials_file: str, sheet_name: str):
    creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open(sheet_name)


class OmdbQuotaExceeded(Exception):
    pass


class OmdbInvalidKey(Exception):
    pass


def fetch_omdb(title: str, api_key: str) -> dict | None:
    """Query OMDb by title. Returns the data dict or None if not found."""
    response = requests.get(
        "https://www.omdbapi.com/",
        params={"t": title, "apikey": api_key},
        timeout=10,
    )
    if response.status_code == 401:
        raise OmdbInvalidKey()
    response.raise_for_status()
    data = response.json()
    if data.get("Response") != "True":
        error = data.get("Error", "")
        if "limit" in error.lower():
            raise OmdbQuotaExceeded()
        return None
    return data


def clean(value: str) -> str:
    """Strip whitespace and replace OMDb's 'N/A' sentinel with empty string."""
    value = value.strip()
    return "" if value == "N/A" else value

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx (used internally by python-telegram-bot) logs every request URL at INFO,
# e.g. "POST https://api.telegram.org/bot<TOKEN>/getUpdates" — which puts the live
# bot token in plaintext in the systemd journal. Silence it to WARNING so only
# actual httpx errors surface, never the request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tags (Genre/Vibe/Style tagging system)
# ---------------------------------------------------------------------------

TAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tags.json")

DEFAULT_TAGS = {
    "vibe": [],
    "style": [],
    "category": {
        "Christmas": "#4ade80",
        "Dudeist": "#d4b96a",
        "Guilty Pleasure": "#2dd4bf",
        "So Bad It's Good": "#fb923c",
        "WTF": "#f87171",
        "Weird": "#c084fc",
    },
}


def _load_tags() -> dict:
    """Tags are grouped by type: vibe/style (color shared by type — see index.html)
    and category (each tag carries its own hex color, stored here)."""
    try:
        with open(TAGS_FILE) as f:
            data = json.load(f)
        return {
            "vibe": list(data.get("vibe", [])),
            "style": list(data.get("style", [])),
            "category": dict(data.get("category", {})),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_TAGS.items()}


def _save_tags() -> None:
    with open(TAGS_FILE, "w") as f:
        json.dump(TAGS, f, indent=2)


def _rebuild_tag_lookup() -> None:
    global VALID_TAGS, VALID_TAGS_LOWER
    VALID_TAGS = TAGS["vibe"] + TAGS["style"] + list(TAGS["category"].keys())
    VALID_TAGS_LOWER = {t.lower(): t for t in VALID_TAGS}


TAGS: dict = _load_tags()
VALID_TAGS: list[str] = []
VALID_TAGS_LOWER: dict[str, str] = {}
_rebuild_tag_lookup()

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

TV_WATCH_TAB = "TV Watch List"

# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

def _help_text() -> str:
    """Built fresh on each call so tag lists reflect live /newtag additions."""
    return (
        "<b>Movie List Maintainer</b>\n\n"
        "/watched <code>&lt;title&gt; [| note [| rank [| tag]]]</code> — Move from Watch List to Movies; adds directly if not on Watch List\n\n"
        "/tag <code>&lt;title&gt; | &lt;tag&gt;</code> — Add a tag to a movie's Tags field (appends; comma-separated)\n\n"
        "/untag <code>&lt;title&gt; | &lt;tag&gt;</code> — Remove a tag from a movie's Tags field\n\n"
        "/newtag <code>&lt;tag&gt; | &lt;vibe|style|category&gt; [| &lt;#hexcolor&gt;]</code> — Register a new tag "
        "(Category tags need a hex color; Vibe/Style share one color per type)\n\n"
        "/addwatch <code>&lt;title&gt; [| tag]</code> — Add to Watch List; use <code>tv</code> as tag for TV Watch List\n"
        f"  Tags: {html(', '.join(VALID_TAGS))}\n\n"
        "/rank <code>&lt;title&gt; | &lt;rank&gt;</code> — Set rank in Movies (<code>42</code>, <code>4stars</code>, <code>4.5stars</code>)\n\n"
        "/reorder — Re-sort Movies by Rank (use after manual edits in Google Sheets)\n\n"
        "/note <code>&lt;title&gt; | &lt;note&gt;</code> — Add or update a movie's Notes\n\n"
        "/find <code>&lt;query&gt;</code> — Search all sheets\n\n"
        "/omdb <code>&lt;title&gt;</code> — Look up OMDb info without modifying any sheet\n\n"
        "/watchlist <code>[tag]</code> — Show the Watch List\n\n"
        "/random <code>[genre [| tag]]</code> — Suggest a random Watch List movie\n\n"
        "/history <code>[n]</code> — Show recent rank changes and watched movies (default 10)\n\n"
        "/trend list — Show active rank trends\n"
        "/trend reset <code>&lt;title&gt;</code> — Clear the trend indicator for a movie\n\n"
        "/help — Show this message"
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def html(text: str) -> str:
    """Escape special characters for Telegram HTML messages."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_spreadsheet():
    return open_spreadsheet(CREDENTIALS_FILE, SHEET_NAME)


def _search_all_sheets(ss, title: str) -> list[tuple[str, int, list[str], list[str]]]:
    """Exact title match (case-insensitive) across all managed worksheets.
    Returns list of (ws_name, row_num, padded_row, headers)."""
    title_lower = title.strip().lower()
    results = []
    for ws_name in WORKSHEET_NAMES:
        try:
            ws = ss.worksheet(ws_name)
        except gspread.WorksheetNotFound:
            continue
        all_values = ws.get_all_values()
        if not all_values:
            continue
        headers = all_values[0]
        if "Title" not in headers:
            continue
        col_index = {h: i for i, h in enumerate(headers)}
        for i, row in enumerate(all_values[1:], start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[col_index["Title"]].strip().lower() == title_lower:
                results.append((ws_name, i, padded, headers))
    return results


def _omdb_row(data: dict, headers: list[str]) -> list[str]:
    """Build a sheet row from OMDb data aligned to the given headers."""
    col_index = {h: i for i, h in enumerate(headers)}
    row = [""] * len(headers)
    field_map = {
        "Title": data.get("Title", ""),
        "Year": clean(data.get("Year", "")),
        "Director": clean(data.get("Director", "")),
        "Country": clean(data.get("Country", "")),
        "Genre": clean(data.get("Genre", "")),
        "IMDB Rating": clean(data.get("imdbRating", "")),
        "Metascore": clean(data.get("Metascore", "")),
    }
    for field, value in field_map.items():
        if field in col_index:
            row[col_index[field]] = value
    return row


async def _fetch_omdb_safe(update: Update, title: str) -> dict | None:
    """Fetch from OMDb with user-facing error messages. Returns data or None."""
    try:
        data = fetch_omdb(title, OMDB_API_KEY)
    except OmdbInvalidKey:
        await update.message.reply_text("OMDb API key is invalid or not yet activated. Check the .env file.")
        return None
    except OmdbQuotaExceeded:
        await update.message.reply_text("OMDb daily quota reached. Try again tomorrow.")
        return None
    except Exception as err:
        await update.message.reply_text(f"OMDb error: {err}")
        return None
    if not data:
        await update.message.reply_text(
            f"'{title}' not found on OMDb. Double-check the title and try again."
        )
        return None
    return data


def _send_chunked(lines: list[str], separator: str = "\n") -> list[str]:
    """Split a list of lines into chunks that fit within Telegram's 4096-char limit."""
    chunks = []
    chunk = ""
    for line in lines:
        addition = (separator if chunk else "") + line
        if len(chunk) + len(addition) > 4000:
            chunks.append(chunk)
            chunk = line
        else:
            chunk += addition
    if chunk:
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Rank / star-rating helpers
#
# The Rank column holds two kinds of values:
#   - Integers 1–200: numbered positional rank (implicitly all 5-star tier)
#   - Star strings like "★ ★ ★ ★ ✮": personal rating for sub-200 movies
#
# Valid star values: 2, 2.5, 3, 3.5, 4, 4.5, 5
# Star string format: "★" per full star, "✮" for the half, space-separated
# ---------------------------------------------------------------------------

VALID_STAR_VALS: frozenset[float] = frozenset({2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0})


def _stars_to_str(val: float) -> str:
    """Convert a numeric star value to the sheet string format."""
    parts = ["★"] * int(val)
    if val % 1:
        parts.append("✮")
    return " ".join(parts)


def _rank_sort_key(s: str) -> tuple[int, float]:
    """Return a sort key so numbered ranks sort before star ratings (desc)."""
    s = s.strip()
    if s.isdigit():
        return (0, int(s))
    full = s.count("★")
    half = 0.5 if "✮" in s else 0.0
    if full or half:
        return (1, -(full + half))
    return (2, 0.0)  # unrecognised — sort last


def _rank_sort_key_with_title(rank_val: str, title: str) -> tuple:
    """Title-aware sort key: integer ranks asc, then star ratings desc + alpha by title."""
    s = rank_val.strip()
    if s.isdigit():
        return (0, int(s), "")
    full = s.count("★")
    half = 0.5 if "✮" in s else 0.0
    if full or half:
        return (1, -(full + half), title.strip().lower())
    return (2, 0.0, "")


def _reorder_group_key(rank_val: str, title: str) -> tuple:
    """Sort key for /reorder: integer ranks, then the blank separator row(s),
    then star ratings desc + alpha, then anything unrecognised.
    Unlike _rank_sort_key_with_title, blank ranks sort between the integer and
    star groups (their normal sheet position) rather than after everything."""
    s = rank_val.strip()
    if s.isdigit():
        return (0, int(s), "")
    if not s:
        return (1, 0.0, "")
    full = s.count("★")
    half = 0.5 if "✮" in s else 0.0
    if full or half:
        return (2, -(full + half), title.strip().lower())
    return (3, 0.0, "")


def _parse_rank_input(s: str) -> tuple[str, str] | None:
    """Parse user-supplied rank input.

    Returns (sheet_value, display_text) or None if invalid.
    Plain integer or decimal  → numeric rank stored as-is.
    "<number>stars" suffix    → converted to star string (e.g. "4stars", "4.5stars").
    """
    s = s.strip()
    if not s:
        return None
    if s.lower().endswith("stars"):
        num_part = s[:-5].strip()
        try:
            val = float(num_part)
        except ValueError:
            return None
        if val not in VALID_STAR_VALS:
            return None
        return (_stars_to_str(val), f"{val:g} stars")
    # Plain number → numeric rank
    try:
        float(s)  # validate it's numeric
    except ValueError:
        return None
    return (s, f"rank #{s}")


def _renumber_ranks(ws) -> None:
    """Reassign integer Rank values (1, 2, 3…) sequentially by current row order. Batch write."""
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return
    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}
    if "Rank" not in col_index:
        return
    rank_col = col_index["Rank"]
    counter = 1
    cells = []
    for row_i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * max(0, len(headers) - len(row))
        val = padded[rank_col].strip()
        if val.isdigit():
            if int(val) != counter:
                cells.append(gspread.Cell(row_i, rank_col + 1, str(counter)))
            counter += 1
    if cells:
        ws.update_cells(cells)


def _enforce_200_limit(ss, ws) -> list[str]:
    """Ensure at most 200 integer-ranked rows exist.  Bumps any excess (highest ranks first)
    to ★★★★★.  Re-reads the sheet each iteration so row numbers stay accurate after moves.
    Returns a list of bumped titles (empty if no overflow)."""
    bumped: list[str] = []
    for _ in range(10):  # safety cap
        all_values = ws.get_all_values()
        if not all_values:
            break
        headers = all_values[0]
        col_index = {h: idx for idx, h in enumerate(headers)}
        rank_col = col_index.get("Rank", 0)
        title_col = col_index.get("Title", 1)

        int_rows: list[tuple[int, list]] = []
        for i, row in enumerate(all_values[1:], start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[rank_col].strip().isdigit():
                int_rows.append((i, padded))

        if len(int_rows) <= 200:
            break

        # Bump the last integer-ranked row (highest rank number)
        bump_row_num, bump_padded = int_rows[-1]
        bump_title = bump_padded[title_col].strip()
        old_rank = bump_padded[rank_col].strip()
        star_rank = "★ ★ ★ ★ ★"

        _reposition_by_rank(ws, bump_row_num, star_rank, bump_title)
        _append_log(ss, "Rank Changed", bump_title, f"Movies: {old_rank} → {star_rank} (list full)")
        bumped.append(bump_title)

    return bumped


def _insert_row_exact(ws, values: list[str], index: int) -> None:
    """Insert a blank row at `index` (1-based) and write `values` into exactly that
    row via an explicit range update.

    gspread's insert_row()/insert_rows() insert the blank row correctly but then
    write data with the Sheets values.append() API, which auto-detects a "table"
    starting at the given cell and appends after its last row — not necessarily the
    blank row just inserted. Near a Sheets Table's boundary this can land the data
    outside the table entirely. Doing the insertDimension and the value write as two
    separate, exact operations avoids that ambiguity.
    """
    ws.spreadsheet.batch_update({
        "requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "ROWS",
                    "startIndex": index - 1,
                    "endIndex": index,
                },
                "inheritFromBefore": False,
            }
        }]
    })
    ws.update([values], f"A{index}")


def _reposition_by_rank(ws, row_num: int, new_rank: str, canonical_title: str) -> bool:
    """Set a row's Rank to new_rank and move it to its correct sorted position.
    Reads the current rank from the sheet — callers must NOT pre-write the rank cell.
    Returns True if the row was physically moved."""
    all_values = ws.get_all_values()
    if not all_values or row_num < 2 or row_num > len(all_values):
        return False

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}
    if "Rank" not in col_index:
        return False

    rank_col = col_index["Rank"]
    title_col = col_index.get("Title", -1)

    current_row = all_values[row_num - 1]
    padded_current = (current_row + [""] * max(0, len(headers) - len(current_row)))[:len(headers)]
    old_rank = padded_current[rank_col].strip()

    if old_rank == new_rank:
        return False

    # Build row data with the new rank already applied
    new_row_data = list(padded_current)
    new_row_data[rank_col] = new_rank

    is_integer_rank = new_rank.strip().isdigit()
    if is_integer_rank:
        new_key = _rank_sort_key(new_rank)
    else:
        new_key = _rank_sort_key_with_title(new_rank, canonical_title)

    # Find the first row (skipping current) whose sort key >= new_key
    target_insert = len(all_values) + 1
    for i, row in enumerate(all_values[1:], start=2):
        if i == row_num:
            continue
        padded = row + [""] * max(0, len(headers) - len(row))
        r = padded[rank_col].strip()
        t = padded[title_col].strip() if title_col >= 0 else ""
        row_key = _rank_sort_key(r) if is_integer_rank else _rank_sort_key_with_title(r, t)
        if r and row_key >= new_key:
            target_insert = i
            break

    # Compute the adjusted insert index after the deletion shifts rows
    if not is_integer_rank and target_insert > row_num:
        adjusted = target_insert - 1
    else:
        adjusted = target_insert

    # Row is already in the correct sorted position — update rank cell in place
    if adjusted == row_num:
        ws.update_cell(row_num, rank_col + 1, new_rank)
        return False

    ws.delete_rows(row_num)
    _insert_row_exact(ws, new_row_data, adjusted)
    return True


def _append_log(ss, event_type: str, title: str, detail: str) -> None:
    """Append an entry to the History tab. Silently swallows errors."""
    try:
        try:
            log_ws = ss.worksheet(LOG_TAB)
        except gspread.WorksheetNotFound:
            log_ws = ss.add_worksheet(LOG_TAB, rows=1000, cols=len(LOG_COLUMNS))
            log_ws.update([LOG_COLUMNS], "A1")
        all_values = log_ws.get_all_values()
        log_ws.insert_row(
            [datetime.date.today().isoformat(), event_type, title, detail],
            len(all_values) + 1,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), parse_mode="HTML")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), parse_mode="HTML")


async def cmd_omdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/omdb <title> — Show OMDb info without touching any sheet."""
    title = " ".join(context.args).strip()
    if not title:
        await update.message.reply_text("Usage: /omdb <title>")
        return

    await update.message.reply_text(f"Looking up '{title}'…")
    data = await _fetch_omdb_safe(update, title)
    if not data:
        return

    lines = [f"<b>{html(data.get('Title', title))}</b> ({html(data.get('Year', '?'))})"]
    for label, key in [
        ("Director", "Director"),
        ("Genre", "Genre"),
        ("Country", "Country"),
        ("IMDB Rating", "imdbRating"),
        ("Metascore", "Metascore"),
        ("Plot", "Plot"),
    ]:
        val = clean(data.get(key, ""))
        if val:
            lines.append(f"{label}: {html(val)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find <query> — Search every tab and every column in the spreadsheet."""
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /find <query>")
        return

    await update.message.reply_text(f"Searching for '{query}'…")
    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    query_lower = query.lower()
    blocks = []

    KNOWN_COLS = {
        "Title", "Tags", "Rank", "Watch Order", "Year", "Director",
        "Genre", "Country", "IMDB Rating", "Metascore", "Notes", "Date Added", "Last Watched",
    }

    for ws in ss.worksheets():
        ws_name = ws.title
        all_values = ws.get_all_values()
        if not all_values:
            continue
        headers = all_values[0]
        col_index = {h: i for i, h in enumerate(headers)}

        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            if not any(query_lower in cell.lower() for cell in padded):
                continue

            def get(col, _padded=padded, _col_index=col_index):
                return clean(_padded[_col_index[col]]) if col in _col_index else ""

            title = get("Title") or padded[0].strip()
            category = get("Tags")
            sheet_label = html(ws_name) + (f" [{html(category)}]" if category else "")
            lines = [f"<b>{html(title)}</b> — {sheet_label}"]

            for label, col in [
                ("Rank", "Rank"),
                ("Watch Order", "Watch Order"),
                ("Year", "Year"),
                ("Director", "Director"),
                ("Genre", "Genre"),
                ("Country", "Country"),
                ("IMDB Rating", "IMDB Rating"),
                ("Metascore", "Metascore"),
                ("Date Added", "Date Added"),
                ("Last Watched", "Last Watched"),
                ("Notes", "Notes"),
            ]:
                val = get(col)
                if val:
                    lines.append(f"{label}: {html(val)}")

            # Show any columns not in the standard set (e.g. History tab's Date/Type/Detail)
            for h, cell in zip(headers, padded):
                if h not in KNOWN_COLS and cell.strip():
                    lines.append(f"{html(h)}: {html(cell.strip())}")

            blocks.append("\n".join(lines))

    if not blocks:
        await update.message.reply_text(f"No results for '{query}'.")
        return

    separator = "\n\n" + "─" * 30 + "\n\n"
    header = f"Found {len(blocks)} result(s) for '{html(query)}':\n\n"
    full = header + separator.join(blocks)

    if len(full) <= 4000:
        await update.message.reply_text(full, parse_mode="HTML")
    else:
        await update.message.reply_text(header.strip(), parse_mode="HTML")
        for chunk in _send_chunked(blocks, separator):
            await update.message.reply_text(chunk, parse_mode="HTML")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watchlist [tag] — Show the Watch List, optionally filtered by tag."""
    category_filter = " ".join(context.args).strip().lower() if context.args else ""

    try:
        ss = get_spreadsheet()
        ws = ss.worksheet("Watch List")
    except gspread.WorksheetNotFound:
        await update.message.reply_text("'Watch List' tab not found in the sheet.")
        return
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    all_values = ws.get_all_values()
    if not all_values or len(all_values) < 2:
        await update.message.reply_text("The Watch List is empty.")
        return

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}
    lines = []

    for row in all_values[1:]:
        padded = row + [""] * max(0, len(headers) - len(row))
        title = padded[col_index["Title"]].strip() if "Title" in col_index else ""
        if not title:
            continue
        category = padded[col_index["Tags"]].strip() if "Tags" in col_index else ""
        if category_filter and category.lower() != category_filter:
            continue
        order = padded[col_index["Watch Order"]].strip() if "Watch Order" in col_index else ""
        line = f"{html(order)}. {html(title)}" if order else f"- {html(title)}"
        if category and not category_filter:
            line += f" [{html(category)}]"
        lines.append(line)

    if not lines:
        label = f" ({category_filter.title()})" if category_filter else ""
        await update.message.reply_text(f"Watch List{label} is empty.")
        return

    label = f" — {category_filter.title()}" if category_filter else ""
    header_line = f"<b>Watch List{html(label)}</b> ({len(lines)} titles)\n\n"

    for chunk in _send_chunked(lines, "\n"):
        await update.message.reply_text(header_line + chunk, parse_mode="HTML")
        header_line = ""  # only show header on first chunk


async def cmd_addwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addwatch <title> [| note] [| tag] — Add a movie to the Watch List."""
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Usage: /addwatch <title> [| note] [| tag]\n"
            "Examples: /addwatch Chinatown\n"
            "          /addwatch Chinatown | recommended by Sam\n"
            "          /addwatch Chinatown | recommended by Sam | weird\n"
            f"Tags: {', '.join(VALID_TAGS)}\n"
            "Use <code>tv</code> as the tag to add to TV Watch List instead.",
            parse_mode="HTML",
        )
        return

    parts = [p.strip() for p in text.split("|", 2)]
    title = parts[0].strip()
    note_text = parts[1].strip() if len(parts) > 1 else ""
    tag_input = parts[2].strip() if len(parts) > 2 else ""

    if not title:
        await update.message.reply_text("Please provide a movie title.")
        return

    tag = ""
    target_tab = "Watch List"

    if tag_input:
        if tag_input.lower() == "tv":
            target_tab = TV_WATCH_TAB
        elif tag_input.lower() in VALID_TAGS_LOWER:
            tag = VALID_TAGS_LOWER[tag_input.lower()]
        else:
            valid_list = ", ".join(VALID_TAGS)
            await update.message.reply_text(
                f"Unknown tag '{html(tag_input)}'. Valid tags: {html(valid_list)}\n"
                "Use <code>tv</code> to add to the TV Watch List.",
                parse_mode="HTML",
            )
            return

    await update.message.reply_text(f"Looking up '{title}' on OMDb…")
    data = await _fetch_omdb_safe(update, title)
    if not data:
        return

    canonical_title = data.get("Title", title)

    try:
        ss = get_spreadsheet()
        ws = ss.worksheet(target_tab)
    except gspread.WorksheetNotFound:
        await update.message.reply_text(f"Tab '{target_tab}' not found in the sheet.")
        return
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    all_values = ws.get_all_values()
    headers = all_values[0] if all_values else WATCH_LIST_COLUMNS
    col_index = {h: i for i, h in enumerate(headers)}

    if "Title" in col_index:
        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[col_index["Title"]].strip().lower() == canonical_title.lower():
                await update.message.reply_text(f"'{canonical_title}' is already in {target_tab}.")
                return

    new_row = _omdb_row(data, headers)
    if "Tags" in col_index:
        new_row[col_index["Tags"]] = tag
    if "Date Added" in col_index:
        new_row[col_index["Date Added"]] = datetime.date.today().isoformat()
    if note_text and "Notes" in col_index:
        new_row[col_index["Notes"]] = note_text

    insert_at = max(2, len(all_values))
    _insert_row_exact(ws, new_row, insert_at)
    tab_label = target_tab + (f" [{tag}]" if tag else "")
    suffix = " (with note)" if note_text else ""
    await update.message.reply_text(
        f"Added <b>{html(canonical_title)}</b> ({html(data.get('Year', '?'))}) to {html(tab_label)}{html(suffix)}.",
        parse_mode="HTML",
    )


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rank <title> | <rank> — Set rank in Movies."""
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /rank <title> | <rank>")
        return

    if "|" not in text:
        await update.message.reply_text("Usage: /rank <title> | <rank>")
        return

    parts = text.split("|", 1)
    title = parts[0].strip()
    raw_value = parts[1].strip()

    if not title or not raw_value:
        await update.message.reply_text("Usage: /rank <title> | <rank>")
        return

    parsed_value = _parse_rank_input(raw_value)
    if parsed_value is None:
        await update.message.reply_text(
            "The rank must be a number or a star rating with the 'stars' suffix.\n"
            "Examples: /rank Alien | 42  or  /rank Alien | 4stars  or  /rank Alien | 4.5stars"
        )
        return

    try:
        ss = get_spreadsheet()
        ws = ss.worksheet("Movies")
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    all_values = ws.get_all_values()
    if not all_values:
        await update.message.reply_text("Movies sheet is empty.")
        return

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}
    if "Title" not in col_index or "Rank" not in col_index:
        await update.message.reply_text("Movies sheet is missing required columns.")
        return

    title_lower = title.lower()
    match = None
    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * max(0, len(headers) - len(row))
        if padded[col_index["Title"]].strip().lower() == title_lower:
            match = (i, padded[col_index["Rank"]].strip(), padded[col_index["Title"]].strip())
            break

    sheet_value, value_display = parsed_value

    if not match:
        # Not in Movies yet — look it up on OMDb and add it fresh, same
        # new-row path /watched uses when a title isn't in any Watch List.
        await update.message.reply_text(
            f"'{html(title)}' not in Movies — looking up on OMDb…", parse_mode="HTML"
        )
        data = await _fetch_omdb_safe(update, title)
        if not data:
            return
        canonical = data.get("Title", title)

        new_row = [""] * len(headers)
        field_values = {
            "Title": canonical,
            "Year": clean(data.get("Year", "")),
            "Director": clean(data.get("Director", "")),
            "Country": clean(data.get("Country", "")),
            "Genre": clean(data.get("Genre", "")),
            "IMDB Rating": clean(data.get("imdbRating", "")),
            "Metascore": clean(data.get("Metascore", "")),
            "Rank": sheet_value,
        }
        for field, value in field_values.items():
            if field in col_index:
                new_row[col_index[field]] = value

        # Insert at correct sort position: numbered ranks first, then star
        # ratings desc + alpha — same scan /watched's new-movie path uses.
        insert_index = len(all_values) + 1
        is_int = sheet_value.strip().isdigit()
        new_key = _rank_sort_key(sheet_value) if is_int else _rank_sort_key_with_title(sheet_value, canonical)
        for i, row in enumerate(all_values[1:], start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            r = padded[col_index["Rank"]].strip()
            t = padded[col_index["Title"]].strip()
            row_key = _rank_sort_key(r) if is_int else _rank_sort_key_with_title(r, t)
            if r and row_key >= new_key:
                insert_index = i
                break

        _insert_row_exact(ws, new_row, insert_index)
        bumped: list[str] = []
        if is_int:
            _renumber_ranks(ws)
            bumped = _enforce_200_limit(ss, ws)
        _append_log(ss, "Rank Changed", canonical, f"Movies: (new) → {sheet_value}")

        msg_lines = [f"Added <b>{html(canonical)}</b> to Movies: Rank → {html(sheet_value)}."]
        for bt in bumped:
            msg_lines.append(f"List full (200) — <b>{html(bt)}</b> moved to ★★★★★.")
        await update.message.reply_text("\n".join(msg_lines), parse_mode="HTML")
        return

    row_num, old_val, canonical = match
    _reposition_by_rank(ws, row_num, sheet_value, canonical)
    _renumber_ranks(ws)
    bumped: list[str] = []
    if sheet_value.isdigit():
        bumped = _enforce_200_limit(ss, ws)
    old_display = old_val or "(blank)"
    _append_log(ss, "Rank Changed", canonical, f"Movies: {old_display} → {sheet_value}")

    msg_lines = [f"Updated <b>{html(canonical)}</b> in Movies: Rank → {html(sheet_value)}."]
    for bt in bumped:
        msg_lines.append(f"List full (200) — <b>{html(bt)}</b> moved to ★★★★★.")
    await update.message.reply_text("\n".join(msg_lines), parse_mode="HTML")


async def cmd_reorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reorder — Physically re-sort the Movies sheet by Rank.

    Manual rank edits made directly in Google Sheets (tracked by
    history_trigger.gs) don't move the row, unlike /rank. Run this after a batch
    of manual edits to restore sorted order: integer ranks ascending, then the
    blank separator row, then star ratings descending + alphabetical."""
    try:
        ss = get_spreadsheet()
        ws = ss.worksheet("Movies")
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    try:
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            await update.message.reply_text("Movies sheet is empty.")
            return

        headers = all_values[0]
        col_index = {h: i for i, h in enumerate(headers)}
        if "Rank" not in col_index or "Title" not in col_index:
            await update.message.reply_text("Movies sheet is missing required columns.")
            return

        rank_col = col_index["Rank"]
        title_col = col_index["Title"]

        data_rows = [
            (row + [""] * max(0, len(headers) - len(row)))[:len(headers)]
            for row in all_values[1:]
        ]

        # Capture pre-reorder ranks per title so we can log a "Rank Changed"
        # entry for anything that shifts as a side effect of the reorder/
        # renumber below — bulk API writes don't fire history_trigger.gs's
        # onEdit, so without this, trend arrows never reflect those shifts.
        old_ranks = {
            r[title_col].strip(): r[rank_col].strip()
            for r in data_rows
            if r[title_col].strip()
        }

        sorted_rows = sorted(
            data_rows,
            key=lambda r: _reorder_group_key(r[rank_col], r[title_col]),
        )

        if sorted_rows == data_rows:
            await update.message.reply_text("Movies sheet is already in order.")
            return

        start_a1 = gspread.utils.rowcol_to_a1(2, 1)
        end_a1 = gspread.utils.rowcol_to_a1(len(sorted_rows) + 1, len(headers))
        ws.update(sorted_rows, f"{start_a1}:{end_a1}")
        _renumber_ranks(ws)
        _append_log(ss, "Reordered", "Movies", f"Re-sorted {len(sorted_rows)} rows by rank")

        final_values = ws.get_all_values()
        for row in final_values[1:]:
            padded = (row + [""] * max(0, len(headers) - len(row)))[:len(headers)]
            title = padded[title_col].strip()
            new_rank = padded[rank_col].strip()
            old_rank = old_ranks.get(title)
            if old_rank is not None and old_rank != new_rank:
                _append_log(ss, "Rank Changed", title, f"Movies: {old_rank} → {new_rank}")

        await update.message.reply_text(
            f"Re-sorted Movies sheet ({len(sorted_rows)} rows) by rank."
        )
    except Exception as err:
        await update.message.reply_text(f"Reorder failed: {err}")


async def cmd_watched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watched <title> [| <note> [| <rank>]] — Remove from Watch List and add to Movies."""
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Usage: /watched <title> [| <note> [| <rank> [| <tag>]]]\n"
            "Examples: /watched Chinatown\n"
            "          /watched Chinatown | great ending | 12\n"
            "          /watched Chinatown | | 4stars\n"
            "          /watched Chinatown | great ending | 12 | SBIG"
        )
        return

    parts = [p.strip() for p in text.split("|")]
    title = parts[0]
    note_text = parts[1] if len(parts) > 1 else ""
    rank_str = parts[2] if len(parts) > 2 else ""
    tag_text = parts[3] if len(parts) > 3 else ""
    target_sheet = "Movies"

    if not title:
        await update.message.reply_text("Please provide a movie title.")
        return

    rank_value = ""
    rank_display = ""
    if rank_str:
        parsed_rank = _parse_rank_input(rank_str)
        if parsed_rank is None:
            await update.message.reply_text(
                "Rank must be a number or a star rating with the 'stars' suffix.\n"
                "Examples: 42  or  4stars  or  4.5stars"
            )
            return
        rank_value, rank_display = parsed_rank

    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    watch_tabs = [w for w in WORKSHEET_NAMES if WATCH_LIST_KEYWORD in w]
    title_lower = title.lower()
    removed_from = []
    saved_row_data: dict = {}

    for ws_name in watch_tabs:
        try:
            ws = ss.worksheet(ws_name)
        except gspread.WorksheetNotFound:
            continue
        all_values = ws.get_all_values()
        if not all_values:
            continue
        headers = all_values[0]
        col_index = {h: i for i, h in enumerate(headers)}
        if "Title" not in col_index:
            continue
        for i, row in enumerate(all_values[1:], start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[col_index["Title"]].strip().lower() == title_lower:
                if not saved_row_data:
                    for field in ["Title", "Year", "Director", "Country", "Genre", "IMDB Rating", "Metascore"]:
                        if field in col_index:
                            saved_row_data[field] = padded[col_index[field]]
                ws.delete_rows(i)
                removed_from.append(ws_name)
                break

    canonical_title = saved_row_data.get("Title", title)

    if not removed_from:
        if not target_sheet:
            await update.message.reply_text(f"'{title}' not found in any Watch List.")
            return
        await update.message.reply_text(f"'{html(title)}' not in any Watch List — looking up on OMDb…", parse_mode="HTML")
        data = await _fetch_omdb_safe(update, title)
        if not data:
            return
        canonical_title = data.get("Title", title)
        saved_row_data = {
            "Title": canonical_title,
            "Year": clean(data.get("Year", "")),
            "Director": clean(data.get("Director", "")),
            "Country": clean(data.get("Country", "")),
            "Genre": clean(data.get("Genre", "")),
            "IMDB Rating": clean(data.get("imdbRating", "")),
            "Metascore": clean(data.get("Metascore", "")),
        }
        msg_lines = []
    else:
        msg_lines = [f"Removed <b>{html(canonical_title)}</b> from {html(', '.join(removed_from))}."]

    if target_sheet:
        try:
            target_ws = ss.worksheet(target_sheet)
            all_values = target_ws.get_all_values()
            headers = all_values[0] if all_values else MOVIE_LIST_COLUMNS
            col_index = {h: i for i, h in enumerate(headers)}

            existing_row_num = None
            if "Title" in col_index:
                for i, row in enumerate(all_values[1:], start=2):
                    padded = row + [""] * max(0, len(headers) - len(row))
                    if padded[col_index["Title"]].strip().lower() == canonical_title.lower():
                        existing_row_num = i
                        break

            if existing_row_num is not None:
                existing_padded = (all_values[existing_row_num - 1] + [""] * max(0, len(headers) - len(all_values[existing_row_num - 1])))
                updates = []
                if "Last Watched" in col_index:
                    target_ws.update_cell(existing_row_num, col_index["Last Watched"] + 1, datetime.date.today().isoformat())
                    updates.append("Last Watched updated")
                if note_text and "Notes" in col_index:
                    target_ws.update_cell(existing_row_num, col_index["Notes"] + 1, note_text)
                    updates.append("note updated")
                if tag_text and "Tags" in col_index:
                    existing_tags_val = existing_padded[col_index["Tags"]].strip() if col_index["Tags"] < len(existing_padded) else ""
                    existing_tags = [t.strip() for t in existing_tags_val.split(",") if t.strip()]
                    if tag_text not in existing_tags:
                        existing_tags.append(tag_text)
                    target_ws.update_cell(existing_row_num, col_index["Tags"] + 1, ", ".join(existing_tags))
                    updates.append(f"tagged {tag_text}")
                bumped_overflow: list[str] = []
                if rank_value and "Rank" in col_index:
                    old_rank = existing_padded[col_index["Rank"]].strip() if col_index["Rank"] < len(existing_padded) else ""
                    _reposition_by_rank(target_ws, existing_row_num, rank_value, canonical_title)
                    _renumber_ranks(target_ws)
                    if rank_value.isdigit():
                        bumped_overflow = _enforce_200_limit(ss, target_ws)
                    updates.append(rank_display)
                    if old_rank != rank_value:
                        _append_log(ss, "Rank Changed", canonical_title, f"{target_sheet}: {old_rank or '(blank)'} → {rank_value}")
                suffix = f" ({', '.join(updates)})" if updates else ""
                msg_lines.append(f"Updated <b>{html(canonical_title)}</b> in {html(target_sheet)}{html(suffix)}.")
                for bt in bumped_overflow:
                    msg_lines.append(f"List full (200) — <b>{html(bt)}</b> moved to ★★★★★.")
                log_detail = f"rewatched in {target_sheet}" + (f" ({rank_display})" if rank_display else "")
                _append_log(ss, "Watched", canonical_title, log_detail)
            else:
                new_row = [""] * len(headers)
                for field, value in saved_row_data.items():
                    if field in col_index:
                        new_row[col_index[field]] = value
                if note_text and "Notes" in col_index:
                    new_row[col_index["Notes"]] = note_text
                if tag_text and "Tags" in col_index:
                    new_row[col_index["Tags"]] = tag_text
                if rank_value and "Rank" in col_index:
                    new_row[col_index["Rank"]] = rank_value
                if "Last Watched" in col_index:
                    new_row[col_index["Last Watched"]] = datetime.date.today().isoformat()

                # Insert at correct sort position: numbered ranks first, then star ratings desc + alpha
                insert_index = len(all_values) + 1
                if rank_value and "Rank" in col_index:
                    is_int = rank_value.strip().isdigit()
                    new_key = _rank_sort_key(rank_value) if is_int else _rank_sort_key_with_title(rank_value, canonical_title)
                    for i, row in enumerate(all_values[1:], start=2):
                        padded = row + [""] * max(0, len(headers) - len(row))
                        r = padded[col_index["Rank"]].strip()
                        t = padded[col_index["Title"]].strip() if "Title" in col_index else ""
                        row_key = _rank_sort_key(r) if is_int else _rank_sort_key_with_title(r, t)
                        if r and row_key >= new_key:
                            insert_index = i
                            break

                _insert_row_exact(target_ws, new_row, insert_index)
                bumped: list[str] = []
                if rank_value and rank_value.isdigit():
                    _renumber_ranks(target_ws)
                    bumped = _enforce_200_limit(ss, target_ws)
                parts_added = []
                if rank_display:
                    parts_added.append(rank_display)
                if note_text:
                    parts_added.append("with note")
                suffix = " (" + ", ".join(parts_added) + ")" if parts_added else ""
                msg_lines.append(f"Added to <b>{html(target_sheet)}</b>{html(suffix)}.")
                for bt in bumped:
                    msg_lines.append(f"List full (200) — <b>{html(bt)}</b> moved to ★★★★★.")
                from_label = ", ".join(removed_from) if removed_from else "OMDb"
                log_detail = f"{from_label} → {target_sheet}" + (f" ({rank_display})" if rank_display else "")
                _append_log(ss, "Watched", canonical_title, log_detail)
        except Exception as err:
            msg_lines.append(f"Could not add to {html(target_sheet)}: {html(str(err))}")
    elif removed_from:
        _append_log(ss, "Watched", canonical_title, f"removed from {', '.join(removed_from)}")
    elif note_text:
        msg_lines.append("(Note ignored — no target sheet specified.)")

    await update.message.reply_text("\n".join(msg_lines), parse_mode="HTML")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/history [n] — Show recent rank changes and watched movies (default 10, max 50)."""
    n = 10
    if context.args:
        if not context.args[0].isdigit():
            await update.message.reply_text("Usage: /history [n]")
            return
        n = min(int(context.args[0]), 50)

    try:
        ss = get_spreadsheet()
        log_ws = ss.worksheet(LOG_TAB)
    except gspread.WorksheetNotFound:
        await update.message.reply_text("No history recorded yet.")
        return
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    all_values = log_ws.get_all_values()
    if len(all_values) < 2:
        await update.message.reply_text("No history recorded yet.")
        return

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}

    def _visible(row: list[str]) -> bool:
        padded = row + [""] * max(0, len(headers) - len(row))
        etype = padded[col_index["Type"]] if "Type" in col_index else ""
        if etype != "Rank Changed":
            return True
        detail = padded[col_index["Detail"]] if "Detail" in col_index else ""
        if WATCH_LIST_KEYWORD in detail:
            return False
        arrow = detail.rfind("→")
        if arrow == -1:
            return True
        new_rank = detail[arrow + 1:].strip()
        return new_rank.isdigit() and 1 <= int(new_rank) <= 200

    data_rows = [r for r in all_values[1:] if _visible(r)]
    recent = list(reversed(data_rows[-n:]))

    lines = [f"<b>History</b> (last {len(recent)} of {len(data_rows)} entries)\n"]
    for row in recent:
        padded = row + [""] * max(0, len(headers) - len(row))
        date = html(padded[col_index["Date"]]) if "Date" in col_index else ""
        etype = html(padded[col_index["Type"]]) if "Type" in col_index else ""
        title_val = html(padded[col_index["Title"]]) if "Title" in col_index else ""
        detail = html(padded[col_index["Detail"]]) if "Detail" in col_index else ""
        lines.append(f"{date} [{etype}] <b>{title_val}</b> — {detail}")

    msg = "\n".join(lines)
    if len(msg) <= 4000:
        await update.message.reply_text(msg, parse_mode="HTML")
    else:
        header = lines[0] + "\n"
        for chunk in _send_chunked(lines[1:], "\n"):
            await update.message.reply_text(header + chunk, parse_mode="HTML")
            header = ""


async def cmd_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/random [genre [| tag]] — Suggest a random movie from the Watch List."""
    text = " ".join(context.args).strip()
    parts = [p.strip() for p in text.split("|")]
    genre_filter = parts[0].lower() if parts[0] else ""
    raw_category = parts[1].lower() if len(parts) > 1 else ""

    category_filter = ""
    use_tv = False
    if raw_category == "tv":
        use_tv = True
    elif raw_category:
        category_filter = VALID_TAGS_LOWER.get(raw_category, raw_category)

    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    tabs = [TV_WATCH_TAB] if use_tv else ["Watch List"]
    candidates = []

    for ws_name in tabs:
        try:
            ws = ss.worksheet(ws_name)
        except gspread.WorksheetNotFound:
            continue
        all_values = ws.get_all_values()
        if not all_values or len(all_values) < 2:
            continue
        headers = all_values[0]
        col_index = {h: i for i, h in enumerate(headers)}
        if "Title" not in col_index:
            continue

        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            title = padded[col_index["Title"]].strip()
            if not title:
                continue
            genre = clean(padded[col_index["Genre"]]) if "Genre" in col_index else ""
            if genre_filter and genre_filter not in genre.lower():
                continue
            category = clean(padded[col_index["Tags"]]) if "Tags" in col_index else ""
            if category_filter and category != category_filter:
                continue
            year = clean(padded[col_index["Year"]]) if "Year" in col_index else ""
            director = clean(padded[col_index["Director"]]) if "Director" in col_index else ""
            imdb = clean(padded[col_index["IMDB Rating"]]) if "IMDB Rating" in col_index else ""
            candidates.append((title, year, director, genre, category, imdb, ws_name))

    if not candidates:
        filters = []
        if genre_filter:
            filters.append(f"genre '{html(genre_filter)}'")
        if use_tv:
            filters.append("TV")
        elif category_filter:
            filters.append(f"category '{html(category_filter)}'")
        suffix = f" matching {' and '.join(filters)}" if filters else ""
        await update.message.reply_text(f"No movies in your Watch List{suffix}.", parse_mode="HTML")
        return

    title, year, director, genre, category, imdb, ws_name = random.choice(candidates)

    header = f"<b>{html(title)}</b>"
    if year:
        header += f" ({html(year)})"
    lines = [header]
    if director:
        lines.append(f"Director: {html(director)}")
    if genre:
        lines.append(f"Genre: {html(genre)}")
    if imdb:
        lines.append(f"IMDB Rating: {html(imdb)}")
    sheet_label = ws_name + (f" [{category}]" if category else "")
    lines.append(f"List: {html(sheet_label)}")
    pool_note = f"({len(candidates)} title(s) in pool)"
    lines.append(f"\n{pool_note}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/trend reset <title> | /trend list — Manage rank trend indicators on the web UI."""
    args = list(context.args)
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/trend reset <title> — Clear the trend indicator for a movie\n"
            "/trend list — Show all active rank trends (last 30 days)"
        )
        return

    subcommand = args[0].lower()

    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    if subcommand == "reset":
        title = " ".join(args[1:]).strip()
        if not title:
            await update.message.reply_text("Usage: /trend reset <title>")
            return
        _append_log(ss, "Trend Reset", title, "manual reset")
        await update.message.reply_text(
            f"Trend cleared for <b>{html(title)}</b>. The web UI will hide the indicator on next load.",
            parse_mode="HTML",
        )

    elif subcommand == "list":
        try:
            log_ws = ss.worksheet(LOG_TAB)
        except gspread.WorksheetNotFound:
            await update.message.reply_text("No history recorded yet.")
            return

        all_values = log_ws.get_all_values()
        if len(all_values) < 2:
            await update.message.reply_text("No history recorded yet.")
            return

        headers = all_values[0]
        col_index = {h: i for i, h in enumerate(headers)}

        cutoff = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()

        # Build most recent rank change per title within 30 days
        rank_changes: dict[str, dict] = {}
        resets: dict[str, str] = {}
        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            etype = padded[col_index["Type"]] if "Type" in col_index else ""
            date = padded[col_index["Date"]] if "Date" in col_index else ""
            title_val = padded[col_index["Title"]] if "Title" in col_index else ""
            detail = padded[col_index["Detail"]] if "Detail" in col_index else ""
            title_key = title_val.lower()

            if etype == "Trend Reset":
                resets[title_key] = date
                continue

            if etype != "Rank Changed" or date < cutoff:
                continue
            if WATCH_LIST_KEYWORD in detail:
                continue
            ai = detail.rfind("→")
            if ai == -1:
                continue
            new_str = detail[ai + 1:].strip()
            if not new_str.isdigit():
                continue
            new_n = int(new_str)
            if not (1 <= new_n <= 200):
                continue
            before = detail[:ai].strip()
            old_str = before.split()[-1] if before.split() else ""
            if not old_str.isdigit():
                continue
            old_n = int(old_str)
            if old_n == new_n:
                continue
            rank_changes[title_key] = {"title": title_val, "old": old_n, "new": new_n, "date": date}

        # Filter out reset titles
        active = {
            k: v for k, v in rank_changes.items()
            if k not in resets or resets[k] < v["date"]
        }

        if not active:
            await update.message.reply_text("No active rank trends in the last 30 days.")
            return

        lines = [f"<b>Active Trends</b> (last year)\n"]
        for v in sorted(active.values(), key=lambda x: x["date"], reverse=True):
            direction = "↑" if v["new"] < v["old"] else "↓"
            delta = abs(v["new"] - v["old"])
            lines.append(f"{html(v['date'])} <b>{html(v['title'])}</b>: {v['old']} → {v['new']} {direction}{delta}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    else:
        await update.message.reply_text(
            "Unknown subcommand. Usage:\n"
            "/trend reset <title>\n"
            "/trend list"
        )


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/note <title> | <note text> — Add or update the Notes field for a movie."""
    text = " ".join(context.args).strip()
    if "|" not in text:
        await update.message.reply_text("Usage: /note <title> | <note text>")
        return

    title, _, note_text = text.partition("|")
    title = title.strip()
    note_text = note_text.strip()

    if not title or not note_text:
        await update.message.reply_text("Usage: /note <title> | <note text>")
        return

    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    results = _search_all_sheets(ss, title)
    if not results:
        await update.message.reply_text(f"'{title}' not found in any sheet.")
        return

    updated = []
    for ws_name, row_num, padded, headers in results:
        col_index = {h: i for i, h in enumerate(headers)}
        if "Notes" not in col_index:
            continue
        ws = ss.worksheet(ws_name)
        ws.update_cell(row_num, col_index["Notes"] + 1, note_text)
        updated.append(ws_name)

    if not updated:
        await update.message.reply_text(f"'{title}' was found but no sheets have a Notes column.")
        return

    await update.message.reply_text(
        f"Updated notes for <b>{html(title)}</b> in {html(', '.join(updated))}.",
        parse_mode="HTML",
    )


async def cmd_newtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newtag <tag> | <vibe|style|category> [| <#hexcolor>] — Register a new tag."""
    text = " ".join(context.args).strip()
    parts = [p.strip() for p in text.split("|")]
    tag = parts[0] if parts and parts[0] else ""
    tag_type = parts[1].lower() if len(parts) > 1 else ""
    color = parts[2] if len(parts) > 2 else ""

    usage = (
        "Usage: /newtag <tag> | <vibe|style|category> [| <#hexcolor>]\n"
        "Vibe and Style tags share one color each; Category tags need their own, "
        "e.g. /newtag Halloween | category | #fb923c"
    )

    if not tag or tag_type not in ("vibe", "style", "category"):
        await update.message.reply_text(usage)
        return

    if tag.lower() in VALID_TAGS_LOWER:
        canonical = VALID_TAGS_LOWER[tag.lower()]
        await update.message.reply_text(f"'{html(canonical)}' is already a valid tag.", parse_mode="HTML")
        return

    if tag_type == "category":
        if not HEX_COLOR_RE.match(color):
            await update.message.reply_text(usage)
            return
        TAGS["category"][tag] = color
    else:
        TAGS[tag_type].append(tag)

    _save_tags()
    _rebuild_tag_lookup()

    color_note = f" ({html(color)})" if tag_type == "category" else ""
    await update.message.reply_text(
        f"Added {tag_type} tag <b>{html(tag)}</b>{color_note}. Current tags: {html(', '.join(VALID_TAGS))}",
        parse_mode="HTML",
    )


async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tag <title> | <tag> — Add a tag to a movie's Tags field (appends, comma-separated)."""
    text = " ".join(context.args).strip()
    if "|" not in text:
        await update.message.reply_text("Usage: /tag <title> | <tag>")
        return

    title, _, tag_text = text.partition("|")
    title = title.strip()
    tag_text = tag_text.strip()

    if not title or not tag_text:
        await update.message.reply_text("Usage: /tag <title> | <tag>")
        return

    # Normalize to the registered tag's casing (e.g. "weird" → "Weird") so the
    # same tag doesn't end up duplicated under different casing; unknown tags
    # are kept as typed.
    tag_text = VALID_TAGS_LOWER.get(tag_text.lower(), tag_text)

    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    results = _search_all_sheets(ss, title)
    if not results:
        await update.message.reply_text(f"'{html(title)}' not found in any sheet.", parse_mode="HTML")
        return

    updated = []
    for ws_name, row_num, padded, headers in results:
        col_index = {h: i for i, h in enumerate(headers)}
        if "Tags" not in col_index:
            continue
        ws = ss.worksheet(ws_name)
        existing = padded[col_index["Tags"]].strip()
        existing_tags = [t.strip() for t in existing.split(",") if t.strip()]
        if tag_text.lower() in {t.lower() for t in existing_tags}:
            updated.append(f"{ws_name} (already tagged)")
            continue
        existing_tags.append(tag_text)
        new_tags = ", ".join(existing_tags)
        ws.update_cell(row_num, col_index["Tags"] + 1, new_tags)
        updated.append(ws_name)

    if not updated:
        await update.message.reply_text(
            f"'{html(title)}' found but no eligible sheets have a Tags column.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"Tagged <b>{html(title)}</b> with <b>{html(tag_text)}</b> in {html(', '.join(updated))}.",
        parse_mode="HTML",
    )


async def cmd_untag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/untag <title> | <tag> — Remove a tag from a movie's Tags field."""
    text = " ".join(context.args).strip()
    if "|" not in text:
        await update.message.reply_text("Usage: /untag <title> | <tag>")
        return

    title, _, tag_text = text.partition("|")
    title = title.strip()
    tag_text = tag_text.strip()

    if not title or not tag_text:
        await update.message.reply_text("Usage: /untag <title> | <tag>")
        return

    try:
        ss = get_spreadsheet()
    except Exception as err:
        await update.message.reply_text(f"Could not connect to sheet: {err}")
        return

    results = _search_all_sheets(ss, title)
    if not results:
        await update.message.reply_text(f"'{html(title)}' not found in any sheet.", parse_mode="HTML")
        return

    tag_lower = tag_text.lower()
    updated = []
    for ws_name, row_num, padded, headers in results:
        col_index = {h: i for i, h in enumerate(headers)}
        if "Tags" not in col_index:
            continue
        ws = ss.worksheet(ws_name)
        existing = padded[col_index["Tags"]].strip()
        existing_tags = [t.strip() for t in existing.split(",") if t.strip()]
        remaining = [t for t in existing_tags if t.lower() != tag_lower]
        if len(remaining) == len(existing_tags):
            updated.append(f"{ws_name} (not tagged)")
            continue
        ws.update_cell(row_num, col_index["Tags"] + 1, ", ".join(remaining))
        updated.append(ws_name)

    if not updated:
        await update.message.reply_text(
            f"'{html(title)}' found but no eligible sheets have a Tags column.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"Removed tag '{html(tag_text)}' from <b>{html(title)}</b> in {html(', '.join(updated))}.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("start", "Welcome message + help"),
    BotCommand("help", "Show all commands"),
    BotCommand("addwatch", "Add a movie to the Watch List"),
    BotCommand("watched", "Move a movie to Movies (watched)"),
    BotCommand("rank", "Set a movie's rank in Movies"),
    BotCommand("watchlist", "Show the Watch List"),
    BotCommand("random", "Suggest a random Watch List pick"),
    BotCommand("find", "Search every sheet and column"),
    BotCommand("omdb", "OMDb lookup only, no sheet changes"),
    BotCommand("tag", "Add a tag to a movie"),
    BotCommand("untag", "Remove a tag from a movie"),
    BotCommand("newtag", "Register a new tag"),
    BotCommand("note", "Add/update a movie's note"),
    BotCommand("history", "Show recent rank changes and watches"),
    BotCommand("trend", "List or reset rank trends"),
    BotCommand("reorder", "Re-sort Movies sheet by rank"),
]


async def _post_init(app: Application):
    # Populate Telegram's "/" menu button — previously unregistered, so the
    # menu was empty. Same fix applied to the meals bot; keep this list in
    # sync with the CommandHandlers registered in main() below.
    await app.bot.set_my_commands(BOT_COMMANDS)


def main():
    missing = [
        name for name, val in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("OMDB_API_KEY", OMDB_API_KEY),
            ("SHEET_NAME", SHEET_NAME),
        ]
        if not val
    ]
    if missing:
        for name in missing:
            print(f"Error: {name} is not set.")
        raise SystemExit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    user_filter = filters.User(user_id=ALLOWED_USER_ID)
    app.add_handler(CommandHandler("start",     cmd_start,     filters=user_filter))
    app.add_handler(CommandHandler("help",      cmd_help,      filters=user_filter))
    app.add_handler(CommandHandler("omdb",      cmd_omdb,      filters=user_filter))
    app.add_handler(CommandHandler("find",      cmd_find,      filters=user_filter))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist, filters=user_filter))
    app.add_handler(CommandHandler("addwatch",  cmd_addwatch,  filters=user_filter))
    app.add_handler(CommandHandler("rank",      cmd_rank,      filters=user_filter))
    app.add_handler(CommandHandler("reorder",   cmd_reorder,   filters=user_filter))
    app.add_handler(CommandHandler("watched",   cmd_watched,   filters=user_filter))
    app.add_handler(CommandHandler("history",   cmd_history,   filters=user_filter))
    app.add_handler(CommandHandler("trend",     cmd_trend,     filters=user_filter))
    app.add_handler(CommandHandler("note",      cmd_note,      filters=user_filter))
    app.add_handler(CommandHandler("tag",       cmd_tag,       filters=user_filter))
    app.add_handler(CommandHandler("untag",     cmd_untag,     filters=user_filter))
    app.add_handler(CommandHandler("newtag",    cmd_newtag,    filters=user_filter))
    app.add_handler(CommandHandler("random",    cmd_random,    filters=user_filter))

    print(f"Bot running. Sheet: {SHEET_NAME}")
    app.run_polling()


if __name__ == "__main__":
    main()
