"""Pre-fetch workout data from Google Sheets and flatten to CSV.

The sheet is a 12-week cut program with a wide layout: each tab holds
4 weeks side-by-side in column groups. This script identifies the right
tab for the target date window, fetches the raw cells via Google Sheets
API v4, parses the wide block structure into flat exercise rows, and
writes a CSV the agent prompt can embed directly.

Requires GOOGLE_SHEETS_API_KEY (free, read-only) from a Google Cloud
project with the Sheets API enabled.
"""

import csv
import io
import os
import re
import sys
from datetime import date, timedelta

import requests

SPREADSHEET_ID = "1FLNGRCvJ280ttyvMsbDeWE8lqpISi7J6NQWTpgSMLBQ"
DEFAULT_OUTPUT = "/tmp/workouts.csv"

# Each week occupies 5 data columns + 1 null separator = 6 columns.
# In the raw Sheets API response, column A is the first data column of Week 1.
# Week offsets (0-indexed into each row, after we strip the raw grid):
WEEK_OFFSETS = [0, 6, 12, 18]  # start index of each week's 5-col group
COLS_PER_WEEK = 5  # Exercise, Sets x Reps, Weight, Actual, How I Felt

# Regex to extract dates from tab names like "2026-03-31 to 2026-04-26 ..."
TAB_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})")

# Regex to detect session header rows (day name + session type)
SESSION_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)", re.IGNORECASE
)

# Month abbreviation -> number
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_date_value(v, year: int) -> date | None:
    """Parse a date from a Sheets cell — handles Excel serial numbers and 'Mar 31' text."""
    if v is None or v == "":
        return None
    # Excel serial number (integer or float)
    if isinstance(v, (int, float)):
        try:
            return date(1899, 12, 30) + timedelta(days=int(v))
        except (ValueError, OverflowError):
            return None
    s = str(v).strip().strip('"')
    # Try Excel serial as string
    if s.isdigit():
        try:
            return date(1899, 12, 30) + timedelta(days=int(s))
        except (ValueError, OverflowError):
            return None
    parts = s.split()
    if len(parts) != 2:
        return None
    mon = MONTHS.get(parts[0])
    if mon is None:
        return None
    try:
        day = int(parts[1])
    except ValueError:
        return None
    d = date(year, mon, day)
    if (d - date.today()).days > 60:
        d = d.replace(year=year - 1)
    return d


def find_target_tab(sheets: list[dict], window_start: date, window_end: date) -> str | None:
    """Return the title of the tab whose date range overlaps the target window."""
    for sheet in sheets:
        title = sheet["properties"]["title"]
        m = TAB_DATE_RE.search(title)
        if not m:
            continue
        tab_start = date.fromisoformat(m.group(1))
        tab_end = date.fromisoformat(m.group(2))
        # Check overlap
        if tab_start <= window_end and tab_end >= window_start:
            return title
    return None


def clean(v: str | None) -> str:
    """Strip surrounding quotes that Sheets sometimes adds."""
    if v is None:
        return ""
    v = str(v).strip()
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
    return v.strip()


def parse_grid(rows: list[list], year: int, window_start: date, window_end: date) -> str:
    """Parse the wide grid into flat CSV rows filtered to the date window."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Session", "Exercise", "Sets_x_Reps", "Weight_lbs", "Actual", "How_I_Felt"])

    # Track current session name + date per week column group
    current_sessions: dict[int, tuple[str, date | None]] = {}

    for row in rows:
        # Pad row to ensure we can index all week groups
        padded = row + [""] * (24 - len(row))

        for week_idx, offset in enumerate(WEEK_OFFSETS):
            ex_cell = clean(padded[offset])
            sets_cell = clean(padded[offset + 1])
            weight_cell = clean(padded[offset + 2])
            actual_cell = clean(padded[offset + 3])
            feel_cell = clean(padded[offset + 4])

            if not ex_cell:
                continue

            # Skip column header rows
            if ex_cell == "Exercise":
                continue

            # Detect session header: Exercise col has day+session, date is in feel_cell position
            if SESSION_RE.match(ex_cell) and not sets_cell and not weight_cell:
                raw_date = padded[offset + 4]  # use raw value before clean()
                session_date = parse_date_value(raw_date, year)
                current_sessions[week_idx] = (ex_cell, session_date)
                continue

            # Otherwise it's an exercise data row
            session = current_sessions.get(week_idx)
            if session is None:
                continue
            session_name, session_date = session
            if session_date is None:
                continue
            if not (window_start <= session_date <= window_end):
                continue

            writer.writerow([
                session_date.isoformat(),
                session_name,
                ex_cell,
                sets_cell,
                weight_cell,
                actual_cell,
                feel_cell,
            ])

    return buf.getvalue().rstrip()


def main() -> int:
    api_key = os.environ.get("GOOGLE_SHEETS_API_KEY")
    if not api_key:
        print("error: GOOGLE_SHEETS_API_KEY is not set", file=sys.stderr)
        return 1

    output_path = os.environ.get("WORKOUTS_CSV_PATH", DEFAULT_OUTPUT)

    window_start_str = os.environ.get("WINDOW_START")
    window_end_str = os.environ.get("WINDOW_END")
    if not window_start_str or not window_end_str:
        print("error: WINDOW_START and WINDOW_END must be set (YYYY-MM-DD)", file=sys.stderr)
        return 1
    window_start = date.fromisoformat(window_start_str)
    window_end = date.fromisoformat(window_end_str)
    year = window_start.year

    base = "https://sheets.googleapis.com/v4/spreadsheets"

    # 1. List sheets to find the right tab
    r = requests.get(
        f"{base}/{SPREADSHEET_ID}",
        params={"key": api_key, "fields": "sheets.properties"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"error: sheets metadata: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    sheets = r.json().get("sheets", [])
    tab_name = find_target_tab(sheets, window_start, window_end)
    if tab_name is None:
        print(f"error: no tab covers {window_start}..{window_end}", file=sys.stderr)
        print(f"  tabs found: {[s['properties']['title'] for s in sheets]}", file=sys.stderr)
        return 1
    print(f"target tab: {tab_name}")

    # 2. Fetch the raw cell values
    r = requests.get(
        f"{base}/{SPREADSHEET_ID}/values/{tab_name}",
        params={"key": api_key, "majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"error: sheet values: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    grid = r.json().get("values", [])
    print(f"fetched {len(grid)} rows from '{tab_name}'")

    # 3. Skip the first row (week headers like "WEEK 1 — Phase 1 — ...")
    # and parse the rest
    data_rows = grid[1:] if grid else []
    csv_text = parse_grid(data_rows, year, window_start, window_end)

    with open(output_path, "w") as f:
        f.write(csv_text)
    line_count = len(csv_text.splitlines()) - 1  # minus header
    print(f"wrote {line_count} exercise rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
