#!/usr/bin/env python3
"""Sort star-rated rows on the Movies sheet: by star value desc, then title asc."""

import os
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
SHEET_NAME = os.environ.get("SHEET_NAME", "")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _star_val(rank: str) -> float | None:
    """Return numeric star value from a rank string, or None if not a star rank."""
    s = rank.strip()
    if "★" not in s and "✮" not in s:
        return None
    full = s.count("★")
    half = 0.5 if "✮" in s else 0.0
    val = full + half
    return val if val > 0 else None


def main():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    ss = client.open(SHEET_NAME)
    ws = ss.worksheet("Movies")

    all_values = ws.get_all_values()
    if not all_values:
        print("Movies sheet is empty.")
        return

    headers = all_values[0]
    col_index = {h: i for i, h in enumerate(headers)}

    if "Rank" not in col_index or "Title" not in col_index:
        print("Movies sheet missing Rank or Title column.")
        return

    rank_col = col_index["Rank"]
    title_col = col_index["Title"]

    # Split into integer-ranked rows, blank separator rows, and star-rated rows
    int_rows = []
    blank_rows = []
    star_rows = []
    seen_blank = False

    for row in all_values[1:]:
        padded = row + [""] * max(0, len(headers) - len(row))
        rank = padded[rank_col].strip()
        if rank.isdigit():
            int_rows.append(padded)
        elif not rank:
            seen_blank = True
            blank_rows.append(padded)
        else:
            val = _star_val(rank)
            if val is not None:
                star_rows.append(padded)
            else:
                # Unknown rank — preserve in place after blank separator
                star_rows.append(padded)

    if not star_rows:
        print("No star-rated rows found.")
        return

    print(f"Found {len(star_rows)} star-rated rows. Sorting…")

    star_rows.sort(key=lambda r: (
        -(_star_val(r[rank_col]) or 0),
        r[title_col].strip().lower(),
    ))

    # Rebuild full sheet: header + int rows + blank separator(s) + sorted star rows
    new_data = [headers] + int_rows + (blank_rows if blank_rows else [[""] * len(headers)]) + star_rows

    # Pad all rows to the same width
    width = len(headers)
    new_data = [r + [""] * max(0, width - len(r)) for r in new_data]
    new_data = [r[:width] for r in new_data]

    ws.clear()
    ws.update(new_data, "A1")
    print(f"Done. Wrote {len(new_data)} rows ({len(star_rows)} star-rated, sorted).")


if __name__ == "__main__":
    main()
