#!/usr/bin/env python3
"""Backfill movies-extra.json with OMDb poster URLs for the ranked Movies list.

Reads the public CSV export of the Movies sheet (same source as index.html), so
no Google credentials are needed. Only OMDB_API_KEY is required (from the
environment or .env). Titles already in movies-extra.json are skipped, so
re-runs only spend OMDb quota on new movies; --refresh re-fetches everything.

Runs daily in GitHub Actions (.github/workflows/update-posters.yml), and can
also be run by hand:  python fetch_posters.py [--refresh]
"""

import csv
import io
import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional (not installed in CI)
    pass

SPREADSHEET_ID = "12ETUXbIPiwozu_M6Y1Z1hmpsevIbBSxyJQNGbKM-22M"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&sheet=Movies"
OUT_FILE = "movies-extra.json"


def main() -> int:
    api_key = os.environ.get("OMDB_API_KEY", "")
    if not api_key:
        print("OMDB_API_KEY not set")
        return 1
    refresh = "--refresh" in sys.argv

    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))

    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            extra = json.load(f)
    except FileNotFoundError:
        extra = {}

    fetched = 0
    for row in rows:
        title = (row.get("Title") or "").strip()
        rank = (row.get("Rank") or "").strip()
        if not title or not rank.isdigit():  # top 200 only; skip stars/blank
            continue
        key = title.lower()
        if key in extra and not refresh:
            continue
        year = (row.get("Year") or "").strip()
        data = {}
        # Try title+year first (disambiguates remakes), then title alone.
        for with_year in ([True, False] if year.isdigit() else [False]):
            params = {"t": title.replace("\u2019", "'"), "apikey": api_key}
            if with_year:
                params["y"] = year
            data = requests.get("https://www.omdbapi.com/", params=params, timeout=10).json()
            if data.get("Response") == "True" or "limit" in data.get("Error", "").lower():
                break
        if data.get("Response") != "True":
            error = data.get("Error", "")
            print(f"  skip {title!r}: {error}")
            if "limit" in error.lower():
                print("OMDb daily limit hit; stopping. Re-run tomorrow.")
                break
            continue
        poster = data.get("Poster", "")
        if poster and poster != "N/A":
            extra[key] = {"poster": poster.replace("http://", "https://")}
            fetched += 1
            print(f"  {title}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(extra.items())), f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"Fetched {fetched} new poster(s); {len(extra)} total in {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
