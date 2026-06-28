#!/usr/bin/env python3
"""Movie List Maintainer — Telegram bot interface."""

import datetime
import logging
import os
import random

import gspread
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from main import (
    CREDENTIALS_FILE,
    LOG_COLUMNS,
    LOG_TAB,
    MOVIE_LIST_COLUMNS,
    OMDB_API_KEY,
    SHEET_NAME,
    WATCH_LIST_COLUMNS,
    WATCH_LIST_KEYWORD,
    WORKSHEET_NAMES,
    OmdbInvalidKey,
    OmdbQuotaExceeded,
    clean,
    fetch_omdb,
    open_spreadsheet,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category aliases for /addwatch
# ---------------------------------------------------------------------------

CATEGORY_ALIASES: dict[str, str] = {
    "general": "General",
    "movies": "General",
    "weird": "Weird",
    "dudeist": "Dudeist",
    "horror": "Horror",
    "documentary": "Documentary",
    "documentaries": "Documentary",
    "christmas": "Christmas",
    "xmas": "Christmas",
}

TV_WATCH_TAB = "TV Watch List"

# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "<b>Movie List Maintainer</b>\n\n"
    "/addwatch <code>&lt;title&gt; [category]</code> — Add to Watch List\n"
    "  Categories: General, Weird, Dudeist, Horror, Documentary, Christmas, TV\n\n"
    "/rank <code>&lt;title&gt; &lt;rank&gt;</code> — Set rank in Movies; use <code>4stars</code> / <code>4.5stars</code> for star ratings\n\n"
    "/watched <code>&lt;title&gt; [| note [| rank]]</code> — Remove from Watch List and add to Movies\n"
    "  Rank: plain number (e.g. <code>42</code>) or star rating (e.g. <code>4stars</code>, <code>4.5stars</code>)\n"
    "  If not in the Watch List, looks up on OMDb and adds directly to Movies\n\n"
    "/history <code>[n]</code> — Show recent rank changes and watched movies (default 10, max 50)\n\n"
    "/note <code>&lt;title&gt; | &lt;note&gt;</code> — Add or update the Notes field for a movie\n\n"
    "/find <code>&lt;title&gt;</code> — Search all sheets for a movie\n\n"
    "/omdb <code>&lt;title&gt;</code> — Look up OMDb info without modifying any sheet\n\n"
    "/watchlist <code>[category]</code> — Show the Watch List, optionally filtered by category\n\n"
    "/random <code>[genre [| category]]</code> — Suggest a random movie from your Watch List\n"
    "  Categories: General, Weird, Dudeist, Horror, Documentary, Christmas, TV\n\n"
    "/trend list — Show active rank trends (last 30 days)\n"
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
# Valid star values: 2.5, 3, 3.5, 4, 4.5, 5
# Star string format: "★" per full star, "✮" for the half, space-separated
# ---------------------------------------------------------------------------

VALID_STAR_VALS: frozenset[float] = frozenset({2.5, 3.0, 3.5, 4.0, 4.5, 5.0})


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

        ws.update_cell(bump_row_num, rank_col + 1, star_rank)
        _reposition_by_rank(ws, bump_row_num, star_rank, bump_title)
        _append_log(ss, "Rank Changed", bump_title, f"Movies: {old_rank} → {star_rank} (list full)")
        bumped.append(bump_title)

    return bumped


def _reposition_by_rank(ws, row_num: int, stored_rank: str, canonical_title: str) -> bool:
    """Move a row to its correct sorted position after a rank change.
    Sort order: integer ranks ascending, then star ratings descending + alphabetical.
    Returns True if the row was moved."""
    all_values = ws.get_all_values()
    if not all_values:
        return False

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}
    if "Rank" not in col_index:
        return False

    rank_col = col_index["Rank"]
    title_col = col_index.get("Title", -1)

    new_key = _rank_sort_key_with_title(stored_rank, canonical_title)

    # Find the first row (skipping current) whose sort key >= new_key
    target_insert = len(all_values) + 1
    for i, row in enumerate(all_values[1:], start=2):
        if i == row_num:
            continue
        padded = row + [""] * max(0, len(headers) - len(row))
        r = padded[rank_col].strip()
        t = padded[title_col].strip() if title_col >= 0 else ""
        if r and _rank_sort_key_with_title(r, t) >= new_key:
            target_insert = i
            break

    # Already in the right place?
    if target_insert in (row_num, row_num + 1):
        return False

    row_data = (all_values[row_num - 1] + [""] * max(0, len(headers) - len(all_values[row_num - 1])))[:len(headers)]
    ws.delete_rows(row_num)
    # For integer ranks we push the target row down (insert after it); for star ratings we
    # insert before the target to maintain alphabetical order within each star level.
    if not stored_rank.strip().isdigit() and target_insert > row_num:
        adjusted = target_insert - 1
    else:
        adjusted = target_insert
    ws.insert_row(row_data, adjusted)
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
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


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
        "Title", "Category", "Rank", "Watch Order", "Year", "Director",
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
            category = get("Category")
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
    """/watchlist [category] — Show the Watch List, optionally filtered by category."""
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
        category = padded[col_index["Category"]].strip() if "Category" in col_index else ""
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
    """/addwatch <title> [category] — Add a movie to the Watch List."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /addwatch <title> [category]\n"
            "Categories: General (default), Weird, Dudeist, Horror, Documentary, Christmas, TV"
        )
        return

    args = list(context.args)
    category = "General"
    target_tab = "Watch List"

    last = args[-1].lower()
    if last == "tv":
        target_tab = TV_WATCH_TAB
        category = ""
        args.pop()
    elif last in CATEGORY_ALIASES:
        category = CATEGORY_ALIASES[last]
        args.pop()

    title = " ".join(args).strip()
    if not title:
        await update.message.reply_text("Please provide a movie title.")
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
    if "Category" in col_index:
        new_row[col_index["Category"]] = category
    if "Date Added" in col_index:
        new_row[col_index["Date Added"]] = datetime.date.today().isoformat()

    insert_at = max(2, len(all_values))
    ws.insert_row(new_row, insert_at)
    tab_label = target_tab + (f" [{category}]" if category else "")
    await update.message.reply_text(
        f"Added <b>{html(canonical_title)}</b> ({html(data.get('Year', '?'))}) to {html(tab_label)}.",
        parse_mode="HTML",
    )


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rank <title> <rank> — Set rank in Movies."""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /rank <title> <rank>")
        return

    args = list(context.args)
    raw_value = args[-1]
    parsed_value = _parse_rank_input(raw_value)
    if parsed_value is None:
        await update.message.reply_text(
            "The last argument must be a number (rank) or a star rating with the 'stars' suffix.\n"
            "Examples: /rank Alien 42  or  /rank Alien 4stars  or  /rank Alien 4.5stars"
        )
        return

    title = " ".join(args[:-1]).strip()

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

    if not match:
        await update.message.reply_text(f"'{html(title)}' not found in Movies.", parse_mode="HTML")
        return

    row_num, old_val, canonical = match
    sheet_value, value_display = parsed_value
    ws.update_cell(row_num, col_index["Rank"] + 1, sheet_value)
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


async def cmd_watched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watched <title> [| <note> [| <rank>]] — Remove from Watch List and add to Movies."""
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Usage: /watched <title> [| <note> [| <rank>]]\n"
            "Examples: /watched Chinatown\n"
            "          /watched Chinatown | great ending | 12\n"
            "          /watched Chinatown | | 4stars"
        )
        return

    parts = [p.strip() for p in text.split("|")]
    title = parts[0]
    note_text = parts[1] if len(parts) > 1 else ""
    rank_str = parts[2] if len(parts) > 2 else ""
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
                bumped_overflow: list[str] = []
                if rank_value and "Rank" in col_index:
                    old_rank = existing_padded[col_index["Rank"]].strip() if col_index["Rank"] < len(existing_padded) else ""
                    target_ws.update_cell(existing_row_num, col_index["Rank"] + 1, rank_value)
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
                if rank_value and "Rank" in col_index:
                    new_row[col_index["Rank"]] = rank_value
                if "Last Watched" in col_index:
                    new_row[col_index["Last Watched"]] = datetime.date.today().isoformat()

                # Insert at correct sort position: numbered ranks first, then star ratings desc + alpha
                insert_index = len(all_values) + 1
                if rank_value and "Rank" in col_index:
                    new_key = _rank_sort_key_with_title(rank_value, canonical_title)
                    for i, row in enumerate(all_values[1:], start=2):
                        padded = row + [""] * max(0, len(headers) - len(row))
                        r = padded[col_index["Rank"]].strip()
                        t = padded[col_index["Title"]].strip() if "Title" in col_index else ""
                        if r and _rank_sort_key_with_title(r, t) >= new_key:
                            insert_index = i
                            break

                target_ws.insert_row(new_row, insert_index)
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
    """/random [genre [| category]] — Suggest a random movie from the Watch List."""
    text = " ".join(context.args).strip()
    parts = [p.strip() for p in text.split("|")]
    genre_filter = parts[0].lower() if parts[0] else ""
    raw_category = parts[1].lower() if len(parts) > 1 else ""

    category_filter = ""
    use_tv = False
    if raw_category == "tv":
        use_tv = True
    elif raw_category:
        category_filter = CATEGORY_ALIASES.get(raw_category, raw_category.title())

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
            category = clean(padded[col_index["Category"]]) if "Category" in col_index else ""
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

        cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

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

        lines = [f"<b>Active Trends</b> (last 30 days)\n"]
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("omdb", cmd_omdb))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("addwatch", cmd_addwatch))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("watched", cmd_watched))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("trend", cmd_trend))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("random", cmd_random))

    print(f"Bot running. Sheet: {SHEET_NAME}")
    app.run_polling()


if __name__ == "__main__":
    main()
