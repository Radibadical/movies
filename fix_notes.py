#!/usr/bin/env python3
"""One-off script: copy notes from specialty sheets into Movies sheet.

Reads notes from Weird Movies, Documentaries, Horror/Halloween, Dudeist Movies,
and Christmas sheets. For each title that also appears in Movies with a corrupted
Notes value (a plain number — the old rank — or empty), replaces it with the
correct note from the specialty sheet.
"""

import os
import re
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# Matches corrupted notes like "Weird #35", "Horror #12", "Dudeist #5", etc.
CORRUPTED_NOTE_RE = re.compile(r'^.+ #\d+$')

load_dotenv()

CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
SHEET_NAME = os.environ.get("SHEET_NAME", "")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SOURCE_SHEETS = [
    "Weird Movies",
    "Documentaries",
    "Horror/Halloween",
    "Dudeist Movies",
    "Christmas",
]

# Only fix rows at or below this sheet row number (row 1 = header).
# "Requiem for a Dream" is at row 434; everything above is already correct.
START_ROW = 434


def main():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    ss = client.open(SHEET_NAME)

    # Step 1: Build title → (note, source_sheet) from specialty sheets
    notes_map: dict[str, tuple[str, str]] = {}
    for sheet_name in SOURCE_SHEETS:
        try:
            ws = ss.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            print(f"Sheet '{sheet_name}' not found, skipping.")
            continue

        all_values = ws.get_all_values()
        if not all_values:
            continue
        headers = all_values[0]
        col_index = {h: i for i, h in enumerate(headers)}

        if "Title" not in col_index or "Notes" not in col_index:
            print(f"Sheet '{sheet_name}' missing Title or Notes column, skipping.")
            continue

        count = 0
        for row in all_values[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            title = padded[col_index["Title"]].strip()
            note = padded[col_index["Notes"]].strip()
            if not title or not note:
                continue
            key = title.lower()
            if key not in notes_map:
                notes_map[key] = (note, sheet_name)
                count += 1
            else:
                existing_note, existing_source = notes_map[key]
                if existing_note != note:
                    print(
                        f"  Conflict: '{title}' has note in {existing_source} AND {sheet_name} — keeping {existing_source}"
                    )

        print(f"  {sheet_name}: {count} notes collected")

    print(f"\nTotal: notes for {len(notes_map)} unique titles across specialty sheets.")

    # Step 2: Scan Movies sheet for corrupted Notes
    movies_ws = ss.worksheet("Movies")
    all_values = movies_ws.get_all_values()
    if not all_values:
        print("Movies sheet is empty.")
        return

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}

    if "Title" not in col_index or "Notes" not in col_index:
        print("Movies sheet is missing Title or Notes column.")
        return

    notes_col_1idx = col_index["Notes"] + 1

    # Collect rows to update: Notes is blank or a plain integer (corrupted rank),
    # but only at or below START_ROW (rows above that are already correct).
    updates: list[tuple[int, str, str, str, str]] = []
    for i, row in enumerate(all_values[1:], start=2):
        if i < START_ROW:
            continue
        padded = row + [""] * max(0, len(headers) - len(row))
        title = padded[col_index["Title"]].strip()
        current_note = padded[col_index["Notes"]].strip()
        if not title:
            continue
        key = title.lower()
        if key not in notes_map:
            continue
        correct_note, source = notes_map[key]
        if current_note == "" or CORRUPTED_NOTE_RE.match(current_note):
            updates.append((i, title, current_note, correct_note, source))

    if not updates:
        print("\nNo corrupted Notes found in Movies that match specialty-sheet notes.")
        return

    print(f"\nWould update {len(updates)} rows in Movies:")
    for row_num, title, bad, good, source in updates:
        bad_display = f"'{bad}'" if bad else "(empty)"
        print(f"  Row {row_num}: {title!r}  {bad_display}  →  '{good}'  (from {source})")

    confirm = input("\nApply these changes? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    cells = [
        gspread.Cell(row_num, notes_col_1idx, correct_note)
        for row_num, _, _, correct_note, _ in updates
    ]
    movies_ws.update_cells(cells)
    print(f"\nDone. Updated {len(cells)} rows in Movies.")


if __name__ == "__main__":
    main()
