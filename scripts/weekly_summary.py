"""Weekly health progress summary — runs on a GitHub Actions cron.

Starts a session against a pre-configured Anthropic Managed Agent, which already
has the model, system prompt, and MCP tools (Notion, Coupler.io, Withings, etc.)
wired up via its environment and vault. We just send the weekly prompt, stream
the reply to the Actions log, and archive the session when done.
"""

import os
import sys
from datetime import date, timedelta

import anthropic

BETA = "managed-agents-2026-04-01"


def week_range(today: date) -> tuple[date, date]:
    """Return the Mon-Sun week most recently completed relative to `today`.

    If today is Monday, returns last Mon..Sun (yesterday). If today is any
    other day, returns the most recent full Mon..Sun that has already ended.
    """
    days_since_sunday = (today.weekday() + 1) % 7 or 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday


def build_prompt() -> str:
    this_start, this_end = week_range(date.today())
    prev_start = this_start - timedelta(days=7)
    prev_end = this_end - timedelta(days=7)
    return f"""\
Generate my weekly health progress summary for {this_start.isoformat()} to {this_end.isoformat()} (Mon-Sun, inclusive).

FETCH WINDOW — for every source below, pull the combined 14-day range {prev_start.isoformat()}..{this_end.isoformat()} in a SINGLE call, then slice the result in code into two buckets: current_week ({this_start.isoformat()}..{this_end.isoformat()}) and prior_week ({prev_start.isoformat()}..{prev_end.isoformat()}). Do NOT make two separate per-week calls to the same source — that doubles token cost on large tool results.

SOURCE MAP — each metric has ONE authoritative source. Use exactly the source listed; do not substitute.

1. Weight + body composition -> Withings MCP.
   Pull every measurement over the full 14-day fetch window {prev_start.isoformat()}..{this_end.isoformat()} in one call, then bucket into current_week / prior_week. Report per week: latest weight, weekly average weight, body fat %, muscle mass, water %, and any other fields Withings returns.

2. Daily activity (steps, cardio, calories burned, HR) -> Withings MCP.
   This feed is piped from Apple Health, so it covers steps, distance, active minutes, cardio sessions, and resting/active heart rate. Pull the full 14-day window in one call and bucket by week. Report per week: daily steps (avg + total), any cardio sessions logged, and activity calories.
   Do NOT look for strength training here — it will not be in Withings.

3. Strength workouts (sets / reps / weights) -> Coupler.io MCP (backed by a Google Sheet).
   This is the ONLY source for lifting sessions. The sheet is NOT a flat relational table — it is a block-structured human-readable log. Parse it as described below.

   Sheet structure:
   - The spreadsheet has multiple TABS. Each tab holds ~4 weeks of training and specifies the dates covered by the tab. You must first pick the right tab for the target date window.
   - Within a tab, column A contains header rows that delimit the blocks. Columns B-E contain exercise data.
   - Layout (walking top to bottom in a tab):
       Row: [A] "WEEK N — Phase X — <label> (Week N of 4)"        <- week block header
       Row: [A] "<Day> — <Split> (<Type>)    <Mon Day>"           <- session header, e.g. "Tuesday — Upper A (Strength) Mar 31"
       Row: [A]"Exercise" [B]"Sets x Reps" [C]"Weight (lbs)" [D]"Actual" [E]"How I Felt"   <- column header row
       Row: [A]<exercise name> [B]<sets x reps, e.g. "3 x 8"> [C]<weight lbs> [D]<actual performed> [E]<scale from 1-5 (1 = terrible, 2 = rough, 3 = okay, 4 = good, 5 = great)>
       ... more exercise rows ...
       Row: [A] "<next Day> — <next Split> ... <Mon Day>"         <- next session header
       ... and so on. Then the next WEEK block, then the next.

   Procedure:
   a. Call list-dataflows to enumerate the Coupler dataflows. Identify the one whose name references the strength / workout sheet. Then call get-schema to see its tabs.
   b. Pick the tab(s) that cover the combined 14-day window {prev_start.isoformat()}..{this_end.isoformat()}. Usually one tab (since each tab spans 4 weeks) will cover both weeks; if the 14-day window straddles a tab boundary, pull both tabs.
   c. Use get-data to fetch the chosen tab(s) ONCE, then parse in code (not by eyeballing):
      - Walk column A. A row starting with "WEEK " opens a new week block. A row whose column A contains a day-of-week + dash + date-at-end (regex roughly `^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*.* [A-Z][a-z]{{2}} \\d{{1,2}}$`) opens a new session block. A row whose column A equals "Exercise" is the column-header row; the rows immediately following it (until the next session or week header, or a blank row) are exercise entries.
      - Session dates are "Mon DD" with NO YEAR. Infer the year: assume the session is in the current year ({this_start.year}); if that would place the date more than 60 days in the FUTURE relative to today, subtract one year.
      - Keep sessions whose inferred date falls inside the 14-day fetch window {prev_start.isoformat()}..{this_end.isoformat()} (inclusive). Then bucket each kept session into current_week or prior_week based on its date.
   d. For each in-window session (both weeks) report: date, day/split label, exercise type, and every exercise row (name, sets x reps, weight, actual, notes). Parse "Sets x Reps" as two integers (e.g. "3 x 8" -> 3 sets of 8 reps). Compute per-exercise volume = sets * reps * weight_lbs (if weight is blank/bodyweight, record volume as sets * reps with a bodyweight flag).
   e. Weekly totals PER BUCKET (current_week and prior_week independently): number of distinct training days, total sets, total volume (lbs), and if the session header's split label implies a muscle group (e.g. "Upper A", "Lower A", "Push", "Pull"), bucket volume by that label.

4. Nutrition (calories, macros, adherence) -> the latest .xlsx attachment on the Notion page titled "MacroFactor exports".
   Procedure:
   a. Use notion-search to find the page "MacroFactor exports" and open it with notion-fetch.
   b. Identify the most recently uploaded .xlsx attachment on that page.
   c. Download it ONCE and parse with code execution (pandas / openpyxl).
   d. Filter rows to the 14-day fetch window {prev_start.isoformat()}..{this_end.isoformat()}, then split into current_week and prior_week buckets. For EACH bucket compute: avg daily kcal, avg daily protein/carbs/fat (g), number of days logged, and adherence vs target if target columns exist.
   Do NOT pull MacroFactor data from Google Sheets / Coupler — MacroFactor lives only in the Notion xlsx. Do NOT re-download the file for the prior week; one download covers both buckets.

5. Week-over-week comparison -> using the current_week and prior_week buckets you already produced in steps 1-4 (no additional tool calls), compute deltas for every metric (absolute and %). If you find yourself about to call a source a second time just to get the prior week, STOP — you already have the data in the 14-day bucket.

OUTPUT:
- Print a concise human-readable summary to the session log with all metrics and week-over-week deltas.
- Then write a structured weekly sub-page directly under the "2026 Goals" Notion page, ID `47325bff-c70b-42cc-8f0d-91ae062156b4`. Each weekly analysis is its own sub-page of 2026 Goals — there is no intermediate "health tracker" container page.
  Upsert procedure (keyed by week_start={this_start.isoformat()}):
    a. Call notion-fetch on the 2026 Goals page and enumerate its child pages.
    b. Look for an existing child titled exactly "Week of {this_start.isoformat()}". If it exists, call notion-update-page to replace its body with this run's summary. If it does NOT exist, call notion-create-pages with parent=`47325bff-c70b-42cc-8f0d-91ae062156b4` and title "Week of {this_start.isoformat()}".
  Do NOT create a duplicate sub-page for the same week_start under any circumstances.
- Body content for the weekly sub-page, as Notion blocks: a short prose summary at the top, then sections for Weight & Body Composition, Activity (Withings), Strength Workouts (Coupler), and Nutrition (MacroFactor) — each section listing the metrics and the week-over-week deltas.

VERIFICATION — before calling the task done, explicitly state each of these in your final message:
- "Activity (steps/cardio) from Withings MCP: <avg daily steps>, <N> cardio sessions".
- "Strength workouts from Coupler MCP: tab=<tab name>, WEEK block=<WEEK N label>, session headers found in block=<N>, sessions inside target window=<N>, total sets=<N>, total volume=<N> lbs". The "sessions inside target window" count must match the number of session blocks you actually enumerated above.
- "Nutrition from Notion .xlsx attachment on 'MacroFactor exports', file named <filename>, <N> days logged".
- "Date range used: {this_start.isoformat()} to {this_end.isoformat()}".
- "Notion weekly sub-page under 2026 Goals: <created|updated> at <page URL or ID>, titled 'Week of {this_start.isoformat()}'".

If any source returns zero rows or an error, STOP and report the failure explicitly instead of silently substituting another source or making up numbers. A legitimately empty week is fine to report — just say so and cite the source you checked.
"""


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> int:
    api_key = require_env("ANTHROPIC_API_KEY")
    agent_id = require_env("AGENT_ID")
    environment_id = require_env("ENVIRONMENT_ID")
    vault_id = require_env("VAULT_ID")

    client = anthropic.Anthropic(api_key=api_key)

    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        vault_ids=[vault_id],
        title="Weekly Progress Summary",
        betas=[BETA],
    )
    print(f"session: {session.id}", flush=True)

    client.beta.sessions.events.send(
        session_id=session.id,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": build_prompt()}],
            }
        ],
        betas=[BETA],
    )

    try:
        with client.beta.sessions.events.stream(
            session_id=session.id,
            betas=[BETA],
        ) as stream:
            for event in stream:
                event_type = getattr(event, "type", None)
                if event_type == "agent.message":
                    for block in event.content:
                        if block.type == "text":
                            print(block.text, end="", flush=True)
                elif event_type == "session.status_terminated":
                    break
        print()  # trailing newline after streamed output
    finally:
        client.beta.sessions.archive(session.id, betas=[BETA])
        print(f"archived session {session.id}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
