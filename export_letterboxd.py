#!/usr/bin/env python3
"""Export the Movies and Watch List tabs as CSVs for Letterboxd's importer.

Writes two files (git-ignored), which you upload at https://letterboxd.com/import/:
  letterboxd-import.csv     ranked + star-rated movies, with rating, watched date, review
  letterboxd-watchlist.csv  Watch List movies (no rating or review)

Reads the sheet with credentials.json (same as bot.py). Read-only; never writes the sheet.
Usage:  python export_letterboxd.py
"""

import csv
import os
import re
import sys
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SPREADSHEET_ID = "12ETUXbIPiwozu_M6Y1Z1hmpsevIbBSxyJQNGbKM-22M"
CREDENTIALS_FILE = "credentials.json"
IMPORT_FILE = "letterboxd-import.csv"
WATCHLIST_FILE = "letterboxd-watchlist.csv"
IMPORT_COLUMNS = ["Title", "Year", "Directors", "Rating", "WatchedDate", "Review"]
WATCHLIST_COLUMNS = ["Title", "Year", "Directors"]
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y")


def parse_rating(rank: str) -> float | None:
    """Integer rank 1-200 -> 5.0; '★ ★ ★ ✮' -> 3.5; anything else -> None."""
    rank = rank.strip()
    if rank.isdigit():
        return 5.0
    stars = rank.count("★") + (0.5 if "✮" in rank else 0)
    return stars if stars > 0 else None


def parse_date(text: str) -> str:
    """Return YYYY-MM-DD, or '' if the cell is blank or in an unknown format."""
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def clean_year(year: str) -> str:
    match = re.search(r"\d{4}", year)
    return match.group(0) if match else ""


def main() -> int:
    if not os.path.exists(CREDENTIALS_FILE):
        print("credentials.json not found in this folder")
        return 1
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    ss = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

    imported, bad_dates, seen = [], [], set()
    for row in ss.worksheet("Movies").get_all_records():
        title = str(row.get("Title", "")).strip()
        rating = parse_rating(str(row.get("Rank", "")))
        if not title or rating is None or title.lower() in seen:
            continue
        seen.add(title.lower())
        raw_date = str(row.get("Last Watched", ""))
        date = parse_date(raw_date)
        if raw_date.strip() and not date:
            bad_dates.append((title, raw_date))
        imported.append({
            "Title": title,
            "Year": clean_year(str(row.get("Year", ""))),
            "Directors": str(row.get("Director", "")).strip(),
            "Rating": f"{rating:g}",
            "WatchedDate": date,
            "Review": str(row.get("Notes", "")).strip(),
        })

    watchlist = []
    for row in ss.worksheet("Watch List").get_all_records():
        title = str(row.get("Title", "")).strip()
        if not title or title.lower() in seen:  # already watched/rated: don't also watchlist
            continue
        seen.add(title.lower())
        watchlist.append({
            "Title": title,
            "Year": clean_year(str(row.get("Year", ""))),
            "Directors": str(row.get("Director", "")).strip(),
        })

    for path, columns, rows in (
        (IMPORT_FILE, IMPORT_COLUMNS, imported),
        (WATCHLIST_FILE, WATCHLIST_COLUMNS, watchlist),
    ):
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, doublequote=False, escapechar="\\")
            writer.writeheader()
            writer.writerows(rows)

    print(f"{IMPORT_FILE}: {len(imported)} movies "
          f"({sum(1 for r in imported if r['Review'])} with reviews, "
          f"{sum(1 for r in imported if r['WatchedDate'])} with watched dates)")
    print(f"{WATCHLIST_FILE}: {len(watchlist)} movies")
    for title, raw in bad_dates[:10]:
        print(f"  unparsed Last Watched for {title!r}: {raw!r}")
    if len(bad_dates) > 10:
        print(f"  ...and {len(bad_dates) - 10} more unparsed dates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
