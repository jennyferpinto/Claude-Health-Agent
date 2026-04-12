"""Weekly health progress summary — runs on a GitHub Actions cron.

Starts a session against a pre-configured Anthropic Managed Agent, which already
has the model, system prompt, and MCP tools (Notion, Withings, etc.)
wired up via its environment and vault. We just send the weekly prompt, stream
the reply to the Actions log, and archive the session when done.
"""

import csv
import io
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import openpyxl

BETA = "managed-agents-2026-04-01"
MACROFACTOR_CORE_COLS = [
    "Date",
    "Expenditure",
    "Trend Weight (lbs)",
    "Weight (lbs)",
    "Calories (kcal)",
    "Protein (g)",
    "Fat (g)",
    "Carbs (g)",
    "Target Calories (kcal)",
    "Target Protein (g)",
    "Target Fat (g)",
    "Target Carbs (g)",
    "Steps",
]


def load_macrofactor_csv(xlsx_path: Path, window_start: date, window_end: date) -> str:
    """Parse MacroFactor xlsx Quick Export sheet, filter to window, return CSV.

    Keeps only the core aggregate columns (defined in MACROFACTOR_CORE_COLS) —
    the full sheet has 64 columns of micronutrient data we don't use.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Quick Export"]
    rows = list(ws.iter_rows(values_only=True))
    headers = list(rows[0])
    col_idx = [headers.index(c) for c in MACROFACTOR_CORE_COLS]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(MACROFACTOR_CORE_COLS)
    for row in rows[1:]:
        raw_date = row[col_idx[0]]
        if raw_date is None:
            continue
        row_date = raw_date.date() if isinstance(raw_date, datetime) else raw_date
        if not (window_start <= row_date <= window_end):
            continue
        out = []
        for i, idx in enumerate(col_idx):
            v = row[idx]
            if i == 0:
                out.append(row_date.isoformat())
            elif v is None:
                out.append("")
            else:
                out.append(v)
        writer.writerow(out)
    return buf.getvalue().rstrip()


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
    withings_start = (prev_start - timedelta(days=1)).isoformat()

    xlsx_path = Path(os.environ.get("MACROFACTOR_XLSX_PATH", "/tmp/macrofactor.xlsx"))
    if not xlsx_path.exists():
        print(f"error: MacroFactor xlsx not found at {xlsx_path}", file=sys.stderr)
        sys.exit(1)
    macrofactor_csv = load_macrofactor_csv(xlsx_path, prev_start, this_end)

    workouts_path = Path(os.environ.get("WORKOUTS_CSV_PATH", "/tmp/workouts.csv"))
    if not workouts_path.exists():
        print(f"error: workouts CSV not found at {workouts_path}", file=sys.stderr)
        sys.exit(1)
    workouts_csv = workouts_path.read_text().rstrip()

    return f"""\
Generate my weekly health progress summary for {this_start.isoformat()} to {this_end.isoformat()} (Mon-Sun, inclusive).

GOALS — frame all analysis and recommendations through these objectives:
- Primary goal: lose 5-8 lbs of fat while preserving all muscle mass (body recomposition on a deficit).
- Currently training on a caloric deficit.
- Key signals to watch: fat mass trending down, lean mass stable or up, strength maintained or progressing, NEAT isn't dropping too much, adequate protein intake, and deficit not too aggressive (risking muscle loss or performance decline).

FETCH WINDOW — for every source below, pull the combined 14-day range {prev_start.isoformat()}..{this_end.isoformat()} in a SINGLE call, then slice the result in code into two buckets: current_week ({this_start.isoformat()}..{this_end.isoformat()}) and prior_week ({prev_start.isoformat()}..{prev_end.isoformat()}). Do NOT make two separate per-week calls to the same source — that doubles token cost on large tool results.

SOURCE MAP — each metric has ONE authoritative source. Use exactly the source listed; do not substitute.

1. Weight + body composition -> Withings MCP.
   Pull every measurement over the full 14-day fetch window. IMPORTANT: the Withings API treats startdate as exclusive, so set startdate={withings_start}, enddate={this_end.isoformat()}. Then bucket results into current_week ({this_start.isoformat()}..{this_end.isoformat()}) and prior_week ({prev_start.isoformat()}..{prev_end.isoformat()}) by measurement date. Report all values in lbs. Report per week: latest weight, weekly average weight, body fat %, muscle mass, water %, and any other fields Withings returns.

2. Daily activity (steps, cardio, calories burned, HR) -> Withings MCP.
   This feed is piped from Apple Health, so it covers steps, distance, active minutes, cardio sessions, and resting/active heart rate. Use the same padded date range as step 1: startdate={withings_start}, enddate={this_end.isoformat()}. Bucket by week. Report per week: daily steps (avg + total), any cardio sessions logged, and activity calories.
   Do NOT look for strength training here — it will not be in Withings.

3. Strength workouts (sets / reps / weights) -> already provided inline below, do NOT fetch it.
   The GitHub Action pre-fetched workout data from Google Sheets, parsed the wide block-structured layout into flat exercise rows, and filtered to the 14-day window. The rows are embedded as CSV in the <workout-data> block below.

   <workout-data>
{workouts_csv}
   </workout-data>

   CSV columns: Date, Session (day + split type), Exercise, Sets_x_Reps, Weight_lbs, Actual (sets completed), How_I_Felt (1-5 scale: 1=terrible, 2=rough, 3=okay, 4=good, 5=great).
   Parse this CSV in code and bucket rows by Date into current_week ({this_start.isoformat()}..{this_end.isoformat()}) and prior_week ({prev_start.isoformat()}..{prev_end.isoformat()}).
   For each week compute:
   - Number of distinct training days (unique Dates).
   - Per-session summary: date, session name, exercise count.
   - Per-exercise volume: parse Sets_x_Reps (e.g. "3x10" -> 3 sets of 10 reps), compute volume = sets * reps * weight_lbs. If weight is blank or non-numeric (e.g. "Black band"), flag as bodyweight/band and compute volume = sets * reps only.
   - Weekly totals: total sets, total volume (lbs), and volume bucketed by session split (e.g. "Upper A", "Lower A", "Glute Day") extracted from the Session column.
   - Average "How I Felt" score per session and overall weekly average (ignore blanks).
   Do NOT call any tool for workout data. It is already in this prompt.

4. Nutrition (calories, macros, adherence) -> already provided inline below, do NOT fetch it.
   The GitHub Action pre-downloaded the latest MacroFactor xlsx export from Notion using the real Notion API, parsed the "Quick Export" sheet, and filtered it to the 14-day window {prev_start.isoformat()}..{this_end.isoformat()}. The daily aggregate rows are embedded as CSV in the <macrofactor-data> block below.

   <macrofactor-data>
{macrofactor_csv}
   </macrofactor-data>

   Parse the CSV above in code (it is already filtered to the 14-day window) and bucket rows into current_week ({this_start.isoformat()}..{this_end.isoformat()}) and prior_week ({prev_start.isoformat()}..{prev_end.isoformat()}) by the Date column. For EACH bucket compute: avg daily kcal, avg daily protein/carbs/fat (g), avg daily steps, number of days logged, and adherence vs target (Calories vs Target Calories, Protein vs Target Protein, etc.).
   Do NOT call any tool for MacroFactor data. It is already in this prompt. If the CSV is empty for one of the weeks, state that explicitly — do not fabricate values.

5. Week-over-week comparison -> using the current_week and prior_week buckets you already produced in steps 1-4 (no additional tool calls), compute deltas for every metric (absolute and %). If you find yourself about to call a source a second time just to get the prior week, STOP — you already have the data in the 14-day bucket.

6. SYNTHESIS — cross-source insights. No additional tool calls. Use data already collected in steps 1-5.

   a. Energy balance reconciliation (MacroFactor CSV).
      For each week bucket: net_kcal = sum(Calories) - sum(Expenditure).
      Expected weight change = net_kcal / 3500 lbs.
      Actual trend weight change = last "Trend Weight (lbs)" of the week minus first.
      Report: net balance (kcal), expected change (lbs), actual trend change (lbs), and the gap.
      If the gap exceeds 0.5 lb, flag possible causes: water retention, logging gaps, or metabolic adaptation.

   b. Body composition signal (Withings, step 1).
      If Withings returned lean mass AND fat mass (not just total weight), compute the WoW change in each.
      Classify: fat down + lean stable/up = recomp (progressing). Fat up + lean down = regressing. Both down = deficit. Both up = surplus.
      If body comp breakdown is unavailable, skip and note "only total weight tracked by Withings".

   c. Training load vs recovery (workout CSV volume + Withings avg HR).
      Compare total weekly strength volume (step 3e) to weekly avg heart rate from Withings (step 2).
      - Volume up + avg HR up WoW -> flag "monitor for accumulated fatigue".
      - Volume up + avg HR stable/down -> note "adapting well to increased load".
      - If avg HR data is unavailable from Withings, skip and note.
      Do NOT look for sleep data — it is not tracked.

   d. NEAT compensation check (MacroFactor CSV).
      If the person is in a deficit (net_kcal from 6a < 0) AND trend weight did not decrease as expected, check if avg daily steps fell WoW.
      If steps dropped >10% WoW while in a deficit, flag: "possible NEAT compensation — body reducing non-exercise activity. Consider adding a short walk or increasing daily movement target."

   e. Session effort trend (workout CSV "How I Felt" column, 1-5 scale).
      Compute per-session avg and overall weekly avg of the "How I Felt" scores parsed in step 3d.
      - Weekly avg dropped >0.5 WoW -> flag "perceived recovery declining — consider deload or extra rest day".
      - Weekly avg rose WoW alongside rising volume -> note "handling increased load well".

OUTPUT:
- Print a concise human-readable summary to the session log with all metrics and week-over-week deltas.
- Then write a structured weekly sub-page directly under the "2026 Goals" Notion page, ID `47325bff-c70b-42cc-8f0d-91ae062156b4`. Each weekly analysis is its own sub-page of 2026 Goals — there is no intermediate "health tracker" container page.
  Upsert procedure (keyed by week_start={this_start.isoformat()}):
    a. Call notion-fetch on the 2026 Goals page and enumerate its child pages.
    b. Look for an existing child titled exactly "Week of {this_start.isoformat()}". If it exists, call notion-update-page to replace its body with this run's summary. If it does NOT exist, call notion-create-pages with parent=`47325bff-c70b-42cc-8f0d-91ae062156b4` and title "Week of {this_start.isoformat()}".
  Do NOT create a duplicate sub-page for the same week_start under any circumstances.
- Body content for the weekly sub-page, as Notion blocks, in this order:
    1. **Key Insights** (from step 6 synthesis): 3-5 bullet points leading with the most actionable finding. Energy balance gap, body comp direction, fatigue/recovery flags, NEAT warnings, RPE trends. This is the section the user reads first — make it punchy and specific, not generic.
    2. **Weight & Body Composition** (Withings) — metrics + WoW deltas.
    3. **Activity** (Withings) — steps, cardio, HR, calories burned + WoW deltas.
    4. **Strength Workouts** (Google Sheets) — session list, volume totals, RPE scores + WoW deltas.
    5. **Nutrition** (MacroFactor) — daily avgs, target adherence, energy balance + WoW deltas.
    6. **Data Sources** — one-line per source confirming what was used (for auditability).

VERIFICATION — before calling the task done, explicitly state each of these in your final message:
- "Activity (steps/cardio) from Withings MCP: <avg daily steps>, <N> cardio sessions".
- "Strength workouts from pre-fetched CSV: <N> exercise rows in current week, <N> distinct training days, total sets=<N>, total volume=<N> lbs".
- "Nutrition from pre-fetched MacroFactor CSV: <N> days in current week, <N> days in prior week".
- "Synthesis: net energy balance = <N> kcal, expected weight change = <N> lbs, actual trend change = <N> lbs, gap = <N> lbs".
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
