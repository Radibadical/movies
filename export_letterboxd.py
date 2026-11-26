#!/usr/bin/env python3
"""Export the Movies and Watch List tabs as CSVs for Letterboxd's importer.

Writes two files (git-ignored), which you upload at https://letterboxd.com/import/:
  letterboxd-import.csv     ranked + star-rated movies, with rating, watched date, review
  letterboxd-watchlist.csv  Watch List movies (no rating or review)

Reads the sheet with credentials.json (same as bot.py). Read-only; never writes the sheet.
Incremental sync (Letterboxd re-imports can't be undone, so upload small files):
  python export_letterboxd.py                  full export (both files above)
  python export_letterboxd.py --changed        only movies new or changed since the last upload:
                                               letterboxd-changed.csv, letterboxd-watchlist-new.csv
  python export_letterboxd.py --mark-uploaded  record the current sheet as uploaded
                                               (run after you upload; writes letterboxd-snapshot.json)
"""

import csv
import json
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
CHANGED_FILE = "letterboxd-changed.csv"
WATCHLIST_NEW_FILE = "letterboxd-watchlist-new.csv"
SNAPSHOT_FILE = "letterboxd-snapshot.json"
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


def build_rows(ss) -> tuple[list[dict], list[dict], list[tuple[str, str]]]:
    """Return (rated movies, watchlist movies, unparsed Last Watched cells)."""
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
    return imported, watchlist, bad_dates


def write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, doublequote=False, escapechar="\\")
        writer.writeheader()
        writer.writerows(rows)


def load_snapshot() -> dict | None:
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def main() -> int:
    if not os.path.exists(CREDENTIALS_FILE):
        print("credentials.json not found in this folder")
        return 1
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    ss = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    imported, watchlist, bad_dates = build_rows(ss)

    if "--mark-uploaded" in sys.argv:
        snapshot = {
            "movies": {r["Title"].lower(): r for r in imported},
            "watchlist": [r["Title"].lower() for r in watchlist],
        }
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print(f"Recorded {len(imported)} movies and {len(watchlist)} watchlist titles as uploaded "
              f"in {SNAPSHOT_FILE}")
        return 0

    if "--changed" in sys.argv:
        snapshot = load_snapshot()
        if snapshot is None:
            print(f"No {SNAPSHOT_FILE} yet. Upload a full export, then run --mark-uploaded.")
            return 1
        old = snapshot["movies"]
        changed = [r for r in imported if old.get(r["Title"].lower()) != r]
        new_watch = [r for r in watchlist if r["Title"].lower() not in set(snapshot["watchlist"])]
        write_csv(CHANGED_FILE, IMPORT_COLUMNS, changed)
        write_csv(WATCHLIST_NEW_FILE, WATCHLIST_COLUMNS, new_watch)
        print(f"{CHANGED_FILE}: {len(changed)} new or changed movies")
        for r in changed[:20]:
            before = old.get(r["Title"].lower())
            note = "new" if before is None else f"was {before['Rating']}, now {r['Rating']}" \
                if before["Rating"] != r["Rating"] else "details changed"
            print(f"  {r['Title']} ({note})")
        print(f"{WATCHLIST_NEW_FILE}: {len(new_watch)} new watchlist titles")
        current = {r["Title"].lower() for r in imported} | {r["Title"].lower() for r in watchlist}
        gone = [t for t in list(old) + snapshot["watchlist"] if t not in current]
        if gone:
            print(f"Not synced (Letterboxd can't remove via import; do these by hand): {', '.join(gone[:15])}")
        print("After uploading, run: python export_letterboxd.py --mark-uploaded")
        return 0

    write_csv(IMPORT_FILE, IMPORT_COLUMNS, imported)
    write_csv(WATCHLIST_FILE, WATCHLIST_COLUMNS, watchlist)
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
