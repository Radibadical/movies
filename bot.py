#!/usr/bin/env python3
"""Movie List Maintainer — Telegram bot interface."""

import logging
import os

import gspread
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from main import (
    CREDENTIALS_FILE,
    OMDB_API_KEY,
    SHEET_NAME,
    WATCH_LIST_COLUMNS,
    WATCH_LIST_KEYWORD,
    WATCH_LIST_TABS,
    WORKSHEET_NAMES,
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

# Maps user-supplied strings → canonical Category values used in the sheet
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

# "tv" is special — it targets a separate tab, not the main Watch List
TV_WATCH_TAB = "TV Watch List"

# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "*Movie List Maintainer*\n\n"
    "/addwatch `<title> [category]` — Add to Watch List\n"
    "  Categories: General, Weird, Dudeist, Horror, Documentary, Christmas, TV\n\n"
    "/setorder `<title> <number>` — Set Watch Order (watch lists) or Rank (main lists)\n\n"
    "/watched `<title> [| <sheet> [| <note>]]` — Remove from Watch List; optionally move to a main sheet and add a note\n"
    "  Sheets: Movies, TV, Weird Movies, Documentaries, Horror/Halloween, Christmas\n\n"
    "/note `<title> | <note>` — Add or update the Notes field for a movie\n\n"
    "/find `<title>` — Search all sheets for a movie\n\n"
    "/lookup `<title>` — Look up OMDb info without modifying any sheet\n\n"
    "/watchlist `[category]` — Show the Watch List, optionally filtered by category\n\n"
    "/help — Show this message"
)

# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def get_spreadsheet():
    return open_spreadsheet(CREDENTIALS_FILE, SHEET_NAME)


def _search_all_sheets(ss, title: str) -> list[tuple[str, int, list[str], list[str]]]:
    """Search every managed worksheet for an exact title match (case-insensitive).
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
    """Build a sheet row from OMDb data, aligned to the given headers."""
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
    """Fetch from OMDb and handle errors with user-facing messages.
    Returns the data dict or None on any failure."""
    try:
        data = fetch_omdb(title, OMDB_API_KEY)
    except OmdbQuotaExceeded:
        await update.message.reply_text("OMDb daily quota reached. Try again tomorrow.")
        return None
    except Exception as e:
        await update.message.reply_text(f"OMDb error: {e}")
        return None
    if not data:
        await update.message.reply_text(
            f"'{title}' not found on OMDb. Double-check the title and try again."
        )
        return None
    return data


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lookup <title> — Show OMDb info without touching any sheet."""
    title = " ".join(context.args).strip()
    if not title:
        await update.message.reply_text("Usage: /lookup <title>")
        return

    await update.message.reply_text(f"Looking up '{title}'…")
    data = await _fetch_omdb_safe(update, title)
    if not data:
        return

    lines = [f"*{data.get('Title', title)}* ({data.get('Year', '?')})"]
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
            lines.append(f"{label}: {val}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find <title> — Substring search across all sheets."""
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /find <title>")
        return

    await update.message.reply_text(f"Searching for '{query}'…")
    try:
        ss = get_spreadsheet()
    except Exception as e:
        await update.message.reply_text(f"Could not connect to sheet: {e}")
        return

    query_lower = query.lower()
    results = []  # list of formatted strings, one block per match

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

        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            cell_title = padded[col_index["Title"]].strip()
            if query_lower not in cell_title.lower():
                continue

            def get(col):
                return clean(padded[col_index[col]]) if col in col_index else ""

            # Header line: bold title, sheet, optional category
            category = get("Category")
            sheet_label = f"{ws_name}" + (f" [{category}]" if category else "")
            lines = [f"*{cell_title}* — {sheet_label}"]

            # Rank / Watch Order
            rank = get("Rank")
            watch_order = get("Watch Order")
            if rank:
                lines.append(f"Rank: {rank}")
            if watch_order:
                lines.append(f"Watch Order: {watch_order}")

            # OMDb fields
            for label, col in [
                ("Year", "Year"),
                ("Director", "Director"),
                ("Genre", "Genre"),
                ("Country", "Country"),
                ("IMDB Rating", "IMDB Rating"),
                ("Metascore", "Metascore"),
            ]:
                val = get(col)
                if val:
                    lines.append(f"{label}: {val}")

            # Notes last
            notes = get("Notes")
            if notes:
                lines.append(f"Notes: {notes}")

            results.append("\n".join(lines))

    if not results:
        await update.message.reply_text(f"No results for '{query}'.")
        return

    header = f"Found {len(results)} result(s) for '{query}':\n\n"
    separator = "\n\n" + "─" * 30 + "\n\n"
    full_msg = header + separator.join(results)

    # Chunk at Telegram's 4096-char limit, splitting on separators
    if len(full_msg) <= 4000:
        await update.message.reply_text(full_msg, parse_mode="Markdown")
    else:
        chunk = header
        for i, block in enumerate(results):
            addition = (separator if i > 0 else "") + block
            if len(chunk) + len(addition) > 4000:
                await update.message.reply_text(chunk, parse_mode="Markdown")
                chunk = block
            else:
                chunk += addition
        if chunk:
            await update.message.reply_text(chunk, parse_mode="Markdown")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watchlist [category] — Show the Watch List, optionally filtered."""
    category_filter = " ".join(context.args).strip().lower() if context.args else ""

    try:
        ss = get_spreadsheet()
        ws = ss.worksheet("Watch List")
    except gspread.WorksheetNotFound:
        await update.message.reply_text("'Watch List' tab not found in the sheet.")
        return
    except Exception as e:
        await update.message.reply_text(f"Could not connect to sheet: {e}")
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
        line = f"{order}. {title}" if order else f"- {title}"
        if category and not category_filter:
            line += f" [{category}]"
        lines.append(line)

    if not lines:
        label = f" ({category_filter.title()})" if category_filter else ""
        await update.message.reply_text(f"Watch List{label} is empty.")
        return

    label = f" — {category_filter.title()}" if category_filter else ""
    header_line = f"*Watch List{label}* ({len(lines)} titles)\n\n"

    chunk = header_line
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk, parse_mode="Markdown")


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

    # Check if the last token is a recognised category or "tv"
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
    except Exception as e:
        await update.message.reply_text(f"Could not connect to sheet: {e}")
        return

    all_values = ws.get_all_values()
    headers = all_values[0] if all_values else WATCH_LIST_COLUMNS
    col_index = {h: i for i, h in enumerate(headers)}

    # Duplicate check
    if "Title" in col_index:
        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[col_index["Title"]].strip().lower() == canonical_title.lower():
                await update.message.reply_text(
                    f"'{canonical_title}' is already in {target_tab}."
                )
                return

    # Next Watch Order = current max + 1
    next_order = 1
    if "Watch Order" in col_index and len(all_values) > 1:
        orders = [
            int((row + [""] * max(0, len(headers) - len(row)))[col_index["Watch Order"]])
            for row in all_values[1:]
            if (row + [""] * max(0, len(headers) - len(row)))[col_index["Watch Order"]].strip().isdigit()
        ]
        next_order = max(orders) + 1 if orders else 1

    new_row = _omdb_row(data, headers)
    if "Watch Order" in col_index:
        new_row[col_index["Watch Order"]] = str(next_order)
    if "Category" in col_index:
        new_row[col_index["Category"]] = category

    ws.append_row(new_row)
    tab_label = target_tab + (f" [{category}]" if category else "")
    await update.message.reply_text(
        f"Added *{canonical_title}* ({data.get('Year', '?')}) to {tab_label} at Watch Order #{next_order}.",
        parse_mode="Markdown",
    )


async def cmd_setorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setorder <title> <number> — Set Watch Order or Rank for a movie.
    Works on both watch list tabs (Watch Order column) and regular tabs (Rank column)."""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /setorder <title> <number>")
        return

    args = list(context.args)
    if not args[-1].isdigit():
        await update.message.reply_text(
            "The last argument must be a number.\nUsage: /setorder <title> <number>"
        )
        return

    number = args[-1]
    title = " ".join(args[:-1]).strip()

    try:
        ss = get_spreadsheet()
    except Exception as e:
        await update.message.reply_text(f"Could not connect to sheet: {e}")
        return

    title_lower = title.lower()
    matches = []  # (ws_name, ws, row_num, order_col, col_idx)

    for ws_name in WORKSHEET_NAMES:
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
        is_watch = WATCH_LIST_KEYWORD in ws_name
        order_col = "Watch Order" if is_watch else "Rank"
        if order_col not in col_index:
            continue
        for i, row in enumerate(all_values[1:], start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[col_index["Title"]].strip().lower() == title_lower:
                matches.append((ws_name, ws, i, order_col, col_index[order_col]))

    if not matches:
        await update.message.reply_text(f"'{title}' not found in any sheet.")
        return

    lines = []
    for ws_name, ws, row_num, order_col, col_idx in matches:
        ws.update_cell(row_num, col_idx + 1, number)
        lines.append(f"✓ {ws_name}: {order_col} → {number}")

    prefix = f"Updated *{title}*:\n" if len(matches) > 1 else f"Updated *{title}* in {matches[0][0]}:\n"
    await update.message.reply_text(
        prefix + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_watched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watched <title> [| <sheet> [| <note>]]
    Remove from Watch List. Optionally move to a main sheet and add a note."""
    non_watch_sheets = [w for w in WORKSHEET_NAMES if WATCH_LIST_KEYWORD not in w]
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Usage: /watched <title> [| <sheet> [| <note>]]\n"
            f"Sheets: {', '.join(non_watch_sheets)}"
        )
        return

    # Parse up to three pipe-separated segments: title | sheet | note
    parts = [p.strip() for p in text.split("|")]
    title = parts[0]
    target_sheet = parts[1] if len(parts) > 1 else ""
    note_text = parts[2] if len(parts) > 2 else ""

    if not title:
        await update.message.reply_text("Please provide a movie title.")
        return

    # Validate target sheet if provided
    if target_sheet:
        matched_sheet = next(
            (w for w in non_watch_sheets if w.lower() == target_sheet.lower()), None
        )
        if not matched_sheet:
            await update.message.reply_text(
                f"Sheet '{target_sheet}' not recognised.\n"
                f"Available: {', '.join(non_watch_sheets)}"
            )
            return
        target_sheet = matched_sheet

    try:
        ss = get_spreadsheet()
    except Exception as e:
        await update.message.reply_text(f"Could not connect to sheet: {e}")
        return

    watch_tabs = [w for w in WORKSHEET_NAMES if WATCH_LIST_KEYWORD in w]
    title_lower = title.lower()
    removed_from = []
    saved_row_data: dict = {}  # field name → value, from the watch list row

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
                # Capture field values before deleting
                if not saved_row_data:
                    for field in ["Title", "Year", "Director", "Country", "Genre", "IMDB Rating", "Metascore"]:
                        if field in col_index:
                            saved_row_data[field] = padded[col_index[field]]
                ws.delete_rows(i)
                removed_from.append(ws_name)
                break

    if not removed_from:
        await update.message.reply_text(f"'{title}' not found in any Watch List.")
        return

    canonical_title = saved_row_data.get("Title", title)
    msg_lines = [f"Removed *{canonical_title}* from {', '.join(removed_from)}."]

    # Move to target sheet if requested
    if target_sheet:
        try:
            target_ws = ss.worksheet(target_sheet)
            all_values = target_ws.get_all_values()
            headers = all_values[0] if all_values else MOVIE_LIST_COLUMNS
            col_index = {h: i for i, h in enumerate(headers)}

            # Duplicate check
            already_there = any(
                (row + [""] * max(0, len(headers) - len(row)))[col_index["Title"]].strip().lower()
                == canonical_title.lower()
                for row in all_values[1:]
                if "Title" in col_index
            )
            if already_there:
                msg_lines.append(f"'{canonical_title}' is already in {target_sheet} — not added again.")
            else:
                new_row = [""] * len(headers)
                for field, value in saved_row_data.items():
                    if field in col_index:
                        new_row[col_index[field]] = value
                if note_text and "Notes" in col_index:
                    new_row[col_index["Notes"]] = note_text
                target_ws.append_row(new_row)
                note_suffix = " with note." if note_text else "."
                msg_lines.append(f"Added to *{target_sheet}*{note_suffix} Rank is blank — set it with /setorder.")
        except Exception as e:
            msg_lines.append(f"Could not add to {target_sheet}: {e}")
    elif note_text:
        msg_lines.append("(Note ignored — no target sheet specified.)")

    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")


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
    except Exception as e:
        await update.message.reply_text(f"Could not connect to sheet: {e}")
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
        f"Updated notes for *{title}* in {', '.join(updated)}.",
        parse_mode="Markdown",
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
    app.add_handler(CommandHandler("lookup", cmd_lookup))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("addwatch", cmd_addwatch))
    app.add_handler(CommandHandler("setorder", cmd_setorder))
    app.add_handler(CommandHandler("watched", cmd_watched))
    app.add_handler(CommandHandler("note", cmd_note))

    print(f"Bot running. Sheet: {SHEET_NAME}")
    app.run_polling()


if __name__ == "__main__":
    main()
