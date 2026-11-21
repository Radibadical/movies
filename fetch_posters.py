#!/usr/bin/env python3
"""Backfill movies-extra.json with OMDb poster URLs for the ranked Movies list.

Run from the project directory (needs .env + credentials.json). Titles already
in movies-extra.json are skipped, so re-runs only spend OMDb quota on new
movies. Commit and push movies-extra.json afterwards; index.html reads it.

    .venv/bin/python fetch_posters.py [--refresh]
"""

import json
import os
import sys

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

OUT_FILE = "movies-extra.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def main() -> int:
    api_key = os.environ.get("OMDB_API_KEY", "")
    if not api_key:
        print("OMDB_API_KEY not set")
        return 1
    refresh = "--refresh" in sys.argv

    creds = Credentials.from_service_account_file(
        os.environ.get("CREDENTIALS_FILE", "credentials.json"), scopes=SCOPES
    )
    ws = gspread.authorize(creds).open(os.environ["SHEET_NAME"]).worksheet("Movies")
    rows = ws.get_all_records()

    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            extra = json.load(f)
    except FileNotFoundError:
        extra = {}

    fetched = 0
    for row in rows:
        title = str(row.get("Title", "")).strip()
        rank = str(row.get("Rank", "")).strip()
        if not title or not rank.isdigit():  # top 200 only; skip stars/blank
            continue
        key = title.lower()
        if key in extra and not refresh:
            continue
        params = {"t": title, "apikey": api_key}
        year = str(row.get("Year", "")).strip()
        if year.isdigit():
            params["y"] = year
        resp = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
        data = resp.json()
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
