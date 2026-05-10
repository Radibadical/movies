#!/usr/bin/env python3
"""Movie List Maintainer - Updates a Google Sheet with OMDb movie data."""

import argparse
import os
import sys

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration — set these via environment variables or edit directly below
# ---------------------------------------------------------------------------
CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
SHEET_NAME = os.environ.get("SHEET_NAME", "")  # Exact name of the Google Sheet

# Tabs to process — edit this list or set WORKSHEET_NAMES as a comma-separated env var
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

# Sheet column header → OMDb response field name
OMDB_FIELDS = {
    "Year": "Year",
    "Director": "Director",
    "Country": "Country",
    "Genre": "Genre",
    "IMDB Rating": "imdbRating",
    "Metascore": "Metascore",
}

# These columns are never touched regardless of what OMDb returns
PRESERVE_COLUMNS = {"Rank", "Notes", "Title", "Watch Order", "Date Added"}

# Tabs whose names contain this string are sorted by "Watch Order" after updates
WATCH_LIST_KEYWORD = "Watch List"

# Canonical column order for each sheet type
MOVIE_LIST_COLUMNS = ["Rank", "Title", "Year", "Director", "Country", "Genre", "IMDB Rating", "Metascore", "Notes"]
WATCH_LIST_COLUMNS = ["Watch Order", "Title", "Year", "Director", "Country", "Genre", "IMDB Rating", "Metascore", "Category", "Date Added", "Notes"]

# History log tab
LOG_TAB = "History"
LOG_COLUMNS = ["Date", "Type", "Title", "Detail"]

# Source watch list tabs → category name in merged sheet
WATCH_LIST_TABS = {
    "Watch List": "General",
    "Weird Watch List": "Weird",
    "Dudeist Watch List": "Dudeist",
    "Horror Watch List": "Horror",
    "Documentaries Watch List": "Documentary",
    "Christmas Watch List": "Christmas",
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ---------------------------------------------------------------------------
# Helpers
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
    """Query OMDb by title. Returns the data dict or None if not found.
    Raises OmdbQuotaExceeded if the daily request limit is hit."""
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


def normalize_columns(ws, target_cols: list[str]) -> list[str]:
    """Ensure all target_cols exist and appear first, in order.
    Missing columns are added (empty). Extra columns already in the sheet are
    preserved at the end. No-ops if already correct.
    Returns the final header list."""
    all_values = ws.get_all_values()
    if not all_values:
        ws.update([target_cols], "A1")
        return target_cols

    # Strip whitespace from existing headers to avoid false mismatches
    current_headers = [h.strip() for h in all_values[0]]
    rows = all_values[1:]

    # Columns already in the sheet but not in the target spec — keep at end
    extra_cols = [h for h in current_headers if h and h not in target_cols]
    expected_headers = target_cols + extra_cols

    if current_headers == expected_headers:
        return current_headers  # already correct — nothing to do

    # Report what's changing
    missing = [c for c in target_cols if c not in current_headers]
    reordered = [c for c in target_cols if c in current_headers and current_headers.index(c) != target_cols.index(c)]
    if missing:
        print(f"  Adding missing columns: {', '.join(missing)}")
    if reordered:
        print(f"  Reordering columns: {', '.join(reordered)}")

    col_index = {h: i for i, h in enumerate(current_headers)}

    def reorder_row(row: list[str]) -> list[str]:
        padded = row + [""] * max(0, len(current_headers) - len(row))
        return [padded[col_index[col]] if col in col_index else "" for col in expected_headers]

    new_data = [expected_headers] + [reorder_row(row) for row in rows]
    ws.clear()
    ws.update(new_data, "A1")
    print(f"  Final column order: {', '.join(expected_headers)}")
    return expected_headers


def _pick_duplicate(title: str, candidates: list[list[str]], headers: list[str]) -> list[str]:
    """Interactively ask the user which duplicate row to keep."""
    col_index = {h: i for i, h in enumerate(headers)}
    print(f"\n  Duplicate: '{title}' — both entries have notes. Pick one to keep:\n")
    for i, row in enumerate(candidates, 1):
        padded = row + [""] * max(0, len(headers) - len(row))
        notes = padded[col_index["Notes"]].strip() if "Notes" in col_index else ""
        category = padded[col_index["Category"]].strip() if "Category" in col_index else ""
        label = f"[{category}] " if category else ""
        print(f"    {i}) {label}Notes: {notes!r}")
    while True:
        choice = input(f"  Keep which? [1-{len(candidates)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        print(f"  Please enter a number between 1 and {len(candidates)}.")


def dedup_titles(ws, headers: list[str]) -> list[str]:
    """Remove duplicate titles within each sheet (scoped per Category if present).
    - One entry has notes, other doesn't → keep the one with notes.
    - Both have notes → prompt user interactively.
    - Neither has notes → keep the first occurrence.
    Returns the updated header list (unchanged)."""
    if "Title" not in headers:
        return headers

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return headers

    rows = all_values[1:]
    col_index = {h: i for i, h in enumerate(headers)}
    has_category = "Category" in col_index
    has_notes = "Notes" in col_index

    # Group rows by dedup key, preserving original order via index
    groups: dict[tuple, list[tuple[int, list[str]]]] = {}
    blank_rows: list[tuple[int, list[str]]] = []  # rows with no title — always kept

    for idx, row in enumerate(rows):
        padded = row + [""] * max(0, len(headers) - len(row))
        title = padded[col_index["Title"]].strip()
        if not title:
            blank_rows.append((idx, row))
            continue
        category = padded[col_index["Category"]].strip() if has_category else ""
        key = (title.lower(), category.lower())
        groups.setdefault(key, []).append((idx, row))

    # Resolve each group to a single winner
    resolved: list[tuple[int, list[str]]] = list(blank_rows)
    removed_count = 0

    for (title_lower, _), entries in groups.items():
        if len(entries) == 1:
            resolved.append(entries[0])
            continue

        display_title = entries[0][1][col_index["Title"]].strip()
        removed_count += len(entries) - 1

        if not has_notes:
            print(f"  Duplicate: '{display_title}' — keeping first occurrence, dropping {len(entries) - 1}.")
            resolved.append(entries[0])
            continue

        notes_flags = [
            bool((row + [""] * max(0, len(headers) - len(row)))[col_index["Notes"]].strip())
            for _, row in entries
        ]
        with_notes = [entry for entry, has in zip(entries, notes_flags) if has]

        if not with_notes:
            # None have notes — keep first
            print(f"  Duplicate: '{display_title}' — no notes on any entry, keeping first.")
            resolved.append(entries[0])
        elif len(with_notes) == 1:
            # Exactly one has notes — keep it
            print(f"  Duplicate: '{display_title}' — keeping entry with notes.")
            resolved.append(with_notes[0])
        else:
            # Multiple have notes — ask user
            rows_only = [row for _, row in with_notes]
            winner_row = _pick_duplicate(display_title, rows_only, headers)
            winner_idx = next(idx for idx, row in with_notes if row == winner_row)
            resolved.append((winner_idx, winner_row))

    if removed_count == 0:
        return headers

    # Restore original row order
    resolved.sort(key=lambda x: x[0])
    kept = [row for _, row in resolved]

    scope = "per category" if has_category else "by title"
    print(f"\n  Removed {removed_count} duplicate(s) ({scope}).")
    new_data = [headers] + kept
    ws.clear()
    ws.update(new_data, "A1")
    return headers


# Articles, conjunctions, and short prepositions kept lowercase in title case
# (unless they are the first or last word, or follow a colon/dash)
_LOWERCASE_WORDS = {
    "a", "an", "the",
    "and", "but", "for", "nor", "or", "so", "yet",
    "at", "by", "in", "of", "on", "to", "up", "as", "if", "it", "vs", "via",
}


def _apply_title_case(s: str) -> str:
    """Apply Chicago-style movie title case.
    Capitalises the first letter of each word that isn't a lowercase article/
    preposition/conjunction, except the first and last words are always
    capitalised. Capitalises the word after a colon, em-dash, or hyphen.
    Preserves the casing of letters after the first (so 'MCU' stays 'MCU')."""
    words = s.split()
    if not words:
        return s
    result = []
    force_cap = True
    for i, word in enumerate(words):
        is_last = i == len(words) - 1
        lower = word.lower()
        if force_cap or is_last or lower not in _LOWERCASE_WORDS:
            # Capitalise first letter; leave the rest as-is to preserve acronyms
            fixed = word[0].upper() + word[1:] if word else word
        else:
            fixed = lower
        result.append(fixed)
        force_cap = bool(word) and word[-1] in (":", "\u2014", "-")
    return " ".join(result)


def check_title_casing(ws, headers: list[str]) -> list[str]:
    """Scan every title for casing issues and prompt the user to accept/reject
    each suggested correction. Applies accepted changes in a single batch write."""
    if "Title" not in headers:
        return headers

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return headers

    rows = all_values[1:]
    col_index = {h: i for i, h in enumerate(headers)}
    title_col = col_index["Title"]

    updates: list[gspread.Cell] = []
    checked = 0

    for i, row in enumerate(rows):
        padded = row + [""] * max(0, len(headers) - len(row))
        current = padded[title_col].strip()
        if not current:
            continue
        suggested = _apply_title_case(current)
        if suggested == current:
            continue
        checked += 1
        answer = input(
            f"\n  [{checked}] Current:   {current!r}\n"
            f"       Suggested: {suggested!r}\n"
            f"       Accept? [y/N] "
        ).strip().lower()
        if answer == "y":
            updates.append(gspread.Cell(row=i + 2, col=title_col + 1, value=suggested))

    if updates:
        ws.update_cells(updates)
        print(f"\n  Updated {len(updates)} title(s).")
    elif checked:
        print(f"\n  No title changes accepted.")

    return headers


def renumber_ranks(ws, headers: list[str]) -> list[str]:
    """Reassign integer Rank values sequentially (1, 2, 3 …) based on current row order.
    Only touches rows whose Rank is already an integer; star-rated and blank rows are skipped."""
    if "Rank" not in headers:
        return headers

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return headers

    col_index = {h: i for i, h in enumerate(headers)}
    rank_col = col_index["Rank"]
    updates = []
    counter = 1

    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * max(0, len(headers) - len(row))
        rank_val = padded[rank_col].strip()
        if not rank_val.isdigit():
            continue
        expected = str(counter)
        if rank_val != expected:
            updates.append(gspread.Cell(row=i, col=rank_col + 1, value=expected))
        counter += 1

    if updates:
        ws.update_cells(updates)
        print(f"  Renumbered {len(updates)} rank(s) ({counter - 1} total integer-ranked rows).")
    else:
        print(f"  Ranks already sequential ({counter - 1} rows) — no changes needed.")

    return headers


def sort_star_rated_rows(ws, headers: list[str]) -> list[str]:
    """Sort star-rated rows (those with ★/✮ in Rank) by star value descending, then title asc.
    Integer-ranked rows and blank rows are left in place."""
    if "Rank" not in headers or "Title" not in headers:
        return headers

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return headers

    col_index = {h: i for i, h in enumerate(headers)}
    rank_col = col_index["Rank"]
    title_col = col_index["Title"]

    star_indices = []
    for i, row in enumerate(all_values[1:]):
        padded = row + [""] * max(0, len(headers) - len(row))
        r = padded[rank_col]
        if "★" in r or "✮" in r:
            star_indices.append(i)

    if not star_indices:
        print("  No star-rated rows found.")
        return headers

    original = [all_values[1:][i] for i in star_indices]

    def sort_key(row):
        padded = row + [""] * max(0, len(headers) - len(row))
        r = padded[rank_col]
        star_val = r.count("★") + (0.5 if "✮" in r else 0.0)
        return (-star_val, padded[title_col].strip().lower())

    reordered = sorted(original, key=sort_key)

    if reordered == original:
        print(f"  Star-rated rows already sorted — no changes needed.")
        return headers

    updates = []
    for list_pos, data_row_idx in enumerate(star_indices):
        sheet_row = data_row_idx + 2  # +1 for 0-based, +1 for header
        padded = reordered[list_pos] + [""] * max(0, len(headers) - len(reordered[list_pos]))
        for col_idx in range(len(headers)):
            updates.append(gspread.Cell(row=sheet_row, col=col_idx + 1, value=padded[col_idx]))

    ws.update_cells(updates)
    print(f"  Sorted {len(star_indices)} star-rated rows by rating then title.")
    return headers


def sort_by_watch_order(ws, headers: list[str], num_rows: int):
    """Sort the worksheet (excluding header) by the Watch Order column ascending."""
    if "Watch Order" not in headers:
        print("  No 'Watch Order' column — skipping sort.")
        return
    if num_rows < 2:
        return
    col_num = headers.index("Watch Order") + 1  # 1-indexed
    num_cols = len(headers)
    data_range = f"A2:{rowcol_to_a1(num_rows, num_cols)}"
    ws.sort((col_num, "asc"), range=data_range)
    print("  Sorted by 'Watch Order'.")


def collect_changes(ws) -> tuple[list[dict], list[str], list[str], bool]:
    """Read a worksheet and return (proposed_changes, not_found_titles, headers, quota_hit)."""
    all_values = ws.get_all_values()
    if not all_values:
        return [], [], []

    headers = all_values[0]
    rows = all_values[1:]

    if "Title" not in headers:
        print("  Skipping — no 'Title' column found.")
        return [], [], headers, False

    col_index = {h: i for i, h in enumerate(headers)}
    omdb_cols = [h for h in OMDB_FIELDS if h in col_index]

    needs_fetch = [
        row for row in rows
        if any(
            not clean((row + [""] * (len(headers) - len(row)))[col_index[h]])
            for h in omdb_cols
        )
        and (row + [""] * (len(headers) - len(row)))[col_index["Title"]].strip()
    ]

    print(f"  {len(needs_fetch)} of {len(rows)} movie(s) have missing fields — querying OMDb...\n")

    changes = []
    not_found = []
    quota_hit = False

    for i, row in enumerate(rows):
        row_num = i + 2
        row = row + [""] * (len(headers) - len(row))

        title = row[col_index["Title"]].strip()
        if not title:
            continue

        if all(clean(row[col_index[h]]) for h in omdb_cols):
            continue

        print(f"    Querying: {title}", end="", flush=True)
        try:
            omdb_data = fetch_omdb(title, OMDB_API_KEY)
        except OmdbInvalidKey:
            print(f"\nError: OMDb API key is invalid or not yet activated.")
            print("Check your email for an activation link from OMDb and try again.")
            sys.exit(1)
        except OmdbQuotaExceeded:
            print(f" [quota exceeded — stopping]")
            quota_hit = True
            break

        if omdb_data is None:
            print(" [not found]")
            not_found.append(title)
            continue

        print(" [ok]")

        for col_header, omdb_key in OMDB_FIELDS.items():
            if col_header not in col_index:
                continue
            current = clean(row[col_index[col_header]])
            new_val = clean(omdb_data.get(omdb_key, ""))
            if current != new_val:
                changes.append({
                    "row": row_num,
                    "col": col_index[col_header],
                    "title": title,
                    "field": col_header,
                    "old": current,
                    "new": new_val,
                })

    return changes, not_found, headers, quota_hit


def display_changes(changes: list[dict], not_found: list[str]):
    print(f"\n  Proposed changes ({len(changes)} field(s)):")
    print("  " + "=" * 63)

    current_title = None
    for c in changes:
        if c["title"] != current_title:
            current_title = c["title"]
            print(f"\n    [Row {c['row']}] {c['title']}")
        old_display = c["old"] or "(empty)"
        new_display = c["new"] or "(empty)"
        print(f"      {c['field']:<14}  {old_display!r:28} →  {new_display!r}")

    print()
    if not_found:
        print(f"  Not found on OMDb ({len(not_found)}): {', '.join(not_found)}\n")


# ---------------------------------------------------------------------------
# Watch list merge
# ---------------------------------------------------------------------------

def merge_watch_lists(spreadsheet):
    """Combine all WATCH_LIST_TABS into the 'Watch List' tab with a Category column.
    Deletes the source tabs after merging. No-ops if only 'Watch List' exists."""
    existing_titles = {ws.title for ws in spreadsheet.worksheets()}
    source_tabs = [tab for tab in WATCH_LIST_TABS if tab in existing_titles]

    if not source_tabs:
        print("  No watch list tabs found — nothing to merge.")
        return

    if source_tabs == ["Watch List"]:
        print("  Only 'Watch List' tab exists — no merge needed.")
        return

    print(f"\n{'='*65}")
    print("  Merging watch list tabs:")
    for tab in source_tabs:
        print(f"    - {tab} → Category: {WATCH_LIST_TABS[tab]}")
    print(f"{'='*65}\n")

    answer = input("  Merge and delete source tabs? [y/N] ").strip().lower()
    if answer != "y":
        print("  Skipped merge.")
        return

    combined_rows = []
    for tab_name in source_tabs:
        category = WATCH_LIST_TABS[tab_name]
        ws = spreadsheet.worksheet(tab_name)
        all_values = ws.get_all_values()
        if not all_values:
            continue
        headers = all_values[0]
        rows = all_values[1:]

        if "Title" not in headers:
            print(f"  '{tab_name}' has no Title column — skipping.")
            continue

        col_index = {h: i for i, h in enumerate(headers)}

        for row in rows:
            row = row + [""] * max(0, len(headers) - len(row))
            title = row[col_index.get("Title", -1)].strip() if "Title" in col_index else ""
            if not title:
                continue

            new_row = {}
            for col in WATCH_LIST_COLUMNS:
                if col == "Category":
                    # For the Watch List tab itself, rows may already have a
                    # category set from a previous merge — preserve it.
                    # For source tabs (Weird Watch List etc.) the col won't
                    # exist, so fall back to the tab's default category.
                    existing = row[col_index[col]].strip() if col in col_index else ""
                    new_row[col] = existing if existing else category
                elif col in col_index:
                    new_row[col] = row[col_index[col]]
                else:
                    new_row[col] = ""
            combined_rows.append(new_row)

    if not combined_rows:
        print("  No rows to merge.")
        return

    print(f"  Combined {len(combined_rows)} rows from {len(source_tabs)} tab(s).")

    # Build data to write
    new_data = [WATCH_LIST_COLUMNS] + [
        [row[col] for col in WATCH_LIST_COLUMNS] for row in combined_rows
    ]

    # Write to "Watch List" tab (create if missing)
    if "Watch List" in existing_titles:
        target_ws = spreadsheet.worksheet("Watch List")
        target_ws.clear()
        target_ws.update(new_data, "A1")
    else:
        target_ws = spreadsheet.add_worksheet("Watch List", rows=max(len(new_data) + 10, 100), cols=len(WATCH_LIST_COLUMNS))
        target_ws.update(new_data, "A1")

    print(f"  Wrote {len(combined_rows)} rows to 'Watch List'.")

    # Delete source tabs (except Watch List itself)
    for tab_name in source_tabs:
        if tab_name == "Watch List":
            continue
        ws = spreadsheet.worksheet(tab_name)
        spreadsheet.del_worksheet(ws)
        print(f"  Deleted tab '{tab_name}'.")

    print("  Merge complete.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Movie List Maintainer")
    parser.add_argument(
        "--skip-omdb", action="store_true",
        help="Skip OMDb lookups (normalize columns, merge watch lists, sort — no API calls)",
    )
    args = parser.parse_args()

    missing_cfg = [name for name, val in [("SHEET_NAME", SHEET_NAME)] if not val]
    if not args.skip_omdb:
        missing_cfg += [name for name, val in [("OMDB_API_KEY", OMDB_API_KEY)] if not val]
    if missing_cfg:
        for name in missing_cfg:
            print(f"Error: {name} is not set. Add it to your environment or edit main.py.")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: credentials file '{CREDENTIALS_FILE}' not found.")
        sys.exit(1)

    print(f"Connecting to '{SHEET_NAME}'...")
    spreadsheet = open_spreadsheet(CREDENTIALS_FILE, SHEET_NAME)

    # Merge individual watch list tabs into one if multiple exist
    existing_titles = {ws.title for ws in spreadsheet.worksheets()}
    watch_tabs_present = [tab for tab in WATCH_LIST_TABS if tab in existing_titles]
    if len(watch_tabs_present) > 1:
        merge_watch_lists(spreadsheet)

    for worksheet_name in WORKSHEET_NAMES:
        print(f"\n{'='*65}")
        print(f"  Tab: {worksheet_name}")
        print(f"{'='*65}\n")

        try:
            ws = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            print(f"  Tab '{worksheet_name}' not found — skipping.\n")
            continue

        is_watch_list = WATCH_LIST_KEYWORD in worksheet_name
        target_cols = WATCH_LIST_COLUMNS if is_watch_list else MOVIE_LIST_COLUMNS

        headers = normalize_columns(ws, target_cols)
        dedup_titles(ws, headers)
        check_title_casing(ws, headers)
        if not is_watch_list:
            renumber_ranks(ws, headers)
            sort_star_rated_rows(ws, headers)

        if args.skip_omdb:
            if is_watch_list:
                num_rows = len(ws.get_all_values())
                sort_by_watch_order(ws, headers, num_rows)
            continue

        changes, not_found, headers, quota_hit = collect_changes(ws)

        if not changes:
            print("\n  No changes needed — already up to date.")
            if not_found:
                print(f"  Not found on OMDb: {', '.join(not_found)}")
            if is_watch_list:
                num_rows = len(ws.get_all_values())
                sort_by_watch_order(ws, headers, num_rows)
            if quota_hit:
                print("\n  ⚠️  OMDb daily quota reached. Run again tomorrow to continue.")
                break
            continue

        display_changes(changes, not_found)

        if quota_hit:
            print(f"\n  ⚠️  OMDb daily quota reached mid-tab.")
            print(f"  Auto-applying the {len(changes)} change(s) collected before the limit...")
            answer = "y"
        else:
            answer = input(f"  Apply {len(changes)} change(s) to '{worksheet_name}'? [y/N] ").strip().lower()

        if answer != "y":
            print(f"  Skipped '{worksheet_name}' — no changes made.")
            if quota_hit:
                break
            continue

        print(f"\n  Applying changes to '{worksheet_name}'...")
        cell_updates = [
            gspread.Cell(row=c["row"], col=c["col"] + 1, value=c["new"])
            for c in changes
        ]
        ws.update_cells(cell_updates)
        print(f"  Done! Updated {len(cell_updates)} cell(s) in '{worksheet_name}'.")

        if is_watch_list:
            num_rows = len(ws.get_all_values())
            sort_by_watch_order(ws, headers, num_rows)

        if quota_hit:
            print("\n  ⚠️  OMDb daily quota reached. Run again tomorrow to continue.")
            break

    print(f"\n{'='*65}")
    print("  All tabs processed.")


if __name__ == "__main__":
    main()
