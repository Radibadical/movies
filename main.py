#!/usr/bin/env python3
"""Movie List Maintainer - Updates a Google Sheet with OMDb movie data."""

import os
import sys

import gspread
import requests
from google.oauth2.service_account import Credentials

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
]
WORKSHEET_NAMES = [
    w.strip()
    for w in os.environ.get("WORKSHEET_NAMES", ",".join(DEFAULT_WORKSHEETS)).split(",")
    if w.strip()
]

# OMDb field name → your sheet column header
OMDB_FIELDS = {
    "Year": "Year",
    "Director": "Director",
    "Country": "Country",
    "Genre": "Genre",
}

# These columns are never touched regardless of what OMDb returns
PRESERVE_COLUMNS = {"Rank", "Notes", "Title"}

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


def fetch_omdb(title: str, api_key: str) -> dict | None:
    """Query OMDb by title. Returns the data dict or None if not found."""
    response = requests.get(
        "https://www.omdbapi.com/",
        params={"t": title, "apikey": api_key},
        timeout=10,
    )
    if response.status_code == 401:
        print(f"\nError: OMDb API key is invalid or not yet activated.")
        print("Check your email for an activation link from OMDb and try again.")
        sys.exit(1)
    response.raise_for_status()
    data = response.json()
    return data if data.get("Response") == "True" else None


def clean(value: str) -> str:
    """Strip whitespace and replace OMDb's 'N/A' sentinel with empty string."""
    value = value.strip()
    return "" if value == "N/A" else value


def collect_changes(ws) -> tuple[list[dict], list[str]]:
    """Read a worksheet and return (proposed_changes, not_found_titles)."""
    all_values = ws.get_all_values()
    if not all_values:
        return [], []

    headers = all_values[0]
    rows = all_values[1:]

    if "Title" not in headers:
        print("  Skipping — no 'Title' column found.")
        return [], []

    col_index = {h: i for i, h in enumerate(headers)}
    changes = []
    not_found = []

    omdb_cols = [h for h in OMDB_FIELDS if h in col_index]
    needs_fetch = [
        row for row in all_values[1:]
        if any(
            not clean((row + [""] * (len(headers) - len(row)))[col_index[h]])
            for h in omdb_cols
        )
        and (row + [""] * (len(headers) - len(row)))[col_index["Title"]].strip()
    ]

    print(f"  {len(needs_fetch)} of {len(rows)} movie(s) have missing fields — querying OMDb...\n")

    for i, row in enumerate(all_values[1:]):
        row_num = i + 2
        row = row + [""] * (len(headers) - len(row))

        title = row[col_index["Title"]].strip()
        if not title:
            continue

        # Skip rows where all OMDb fields are already filled
        if all(clean(row[col_index[h]]) for h in omdb_cols):
            continue

        print(f"    Querying: {title}", end="", flush=True)
        omdb_data = fetch_omdb(title, OMDB_API_KEY)

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

    return changes, not_found


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
        print(f"      {c['field']:<12}  {old_display!r:28} →  {new_display!r}")

    print()
    if not_found:
        print(f"  Not found on OMDb ({len(not_found)}): {', '.join(not_found)}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Validate config
    missing = [name for name, val in [("OMDB_API_KEY", OMDB_API_KEY), ("SHEET_NAME", SHEET_NAME)] if not val]
    if missing:
        for name in missing:
            print(f"Error: {name} is not set. Add it to your environment or edit main.py.")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: credentials file '{CREDENTIALS_FILE}' not found.")
        sys.exit(1)

    print(f"Connecting to '{SHEET_NAME}'...")
    spreadsheet = open_spreadsheet(CREDENTIALS_FILE, SHEET_NAME)

    for worksheet_name in WORKSHEET_NAMES:
        print(f"\n{'='*65}")
        print(f"  Tab: {worksheet_name}")
        print(f"{'='*65}\n")

        try:
            ws = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            print(f"  Tab '{worksheet_name}' not found — skipping.\n")
            continue

        changes, not_found = collect_changes(ws)

        if not changes:
            print("\n  No changes needed — already up to date.")
            if not_found:
                print(f"  Not found on OMDb: {', '.join(not_found)}")
            continue

        display_changes(changes, not_found)

        answer = input(f"  Apply {len(changes)} change(s) to '{worksheet_name}'? [y/N] ").strip().lower()
        if answer != "y":
            print(f"  Skipped '{worksheet_name}' — no changes made.")
            continue

        print(f"\n  Applying changes to '{worksheet_name}'...")
        cell_updates = [
            gspread.Cell(row=c["row"], col=c["col"] + 1, value=c["new"])
            for c in changes
        ]
        ws.update_cells(cell_updates)
        print(f"  Done! Updated {len(cell_updates)} cell(s) in '{worksheet_name}'.")

    print(f"\n{'='*65}")
    print("  All tabs processed.")


if __name__ == "__main__":
    main()
