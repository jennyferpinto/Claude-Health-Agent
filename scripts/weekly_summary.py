"""Weekly health progress summary — runs on a GitHub Actions cron.

Starts a session against a pre-configured Anthropic Managed Agent, which already
has the model, system prompt, and MCP tools (Notion, Withings, etc.)
wired up via its environment and vault. We send three sequential prompts
(data collection, analysis, Notion write) to spread token usage across turns
and avoid rate limiting. Each turn is streamed to the Actions log.
"""

import csv
import io
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import openpyxl

BETA = "managed-agents-2026-04-01"
MAX_RETRIES = 3
RETRY_DELAY = 60  # seconds between retries on rate limit

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


def build_prompts() -> list[str]:
    """Build three sequential prompts: data collection, analysis, Notion write."""
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

    clue_path = Path(os.environ.get("CLUE_CONTEXT_PATH", "/tmp/clue_context.txt"))
    clue_context = clue_path.read_text().rstrip() if clue_path.exists() else ""

    goals_path = Path(os.environ.get("GOALS_PATH", "data/goals.txt"))
    if not goals_path.exists():
        goals_path = Path(__file__).resolve().parent.parent / "data" / "goals.txt"
    goals_text = goals_path.read_text().rstrip() if goals_path.exists() else "No goals configured."

    clue_block = ""
    if clue_context:
        clue_block = f"""
5. Menstrual cycle phase -> already provided inline below, do NOT fetch it.
   The GitHub Action pre-computed cycle phase from a Clue data export:

   <cycle-data>
{clue_context}
   </cycle-data>

   Use this to contextualize weight fluctuations (luteal phase causes water retention of 1-3 lbs), energy levels, and training performance. Do NOT call any tool for cycle data."""
    else:
        clue_block = """
5. Menstrual cycle phase -> No cycle data available for this run — skip cycle-related analysis."""

    # ── PROMPT 1: Data Collection ──
    prompt_1_collect = f"""\
Weekly health summary for {this_start.isoformat()} to {this_end.isoformat()} (Mon-Sun).

GOALS:
{goals_text}

This is STEP 1 of 3. In this step, ONLY fetch data. Do NOT compute analysis or write to Notion yet.

Fetch Withings data for the 14-day window. The Withings API treats startdate as exclusive, so use startdate={withings_start}, enddate={this_end.isoformat()}.

1. Weight + body composition -> Withings MCP.
   Pull all measurements in one call. Report all values in lbs. Bucket results into:
   - current_week: {this_start.isoformat()}..{this_end.isoformat()}
   - prior_week: {prev_start.isoformat()}..{prev_end.isoformat()}

2. Daily activity (steps, cardio, calories burned, HR) -> Withings MCP.
   Pull all activity data in one call using the same date range. Bucket by week.
   Do NOT look for strength training here — it will not be in Withings.
   Do NOT look for sleep data — it is not tracked.

The following data sources are ALREADY pre-fetched and embedded below. Do NOT call any tool for them. Just acknowledge you received them.

3. Strength workouts (pre-fetched from Google Sheets):
   <workout-data>
{workouts_csv}
   </workout-data>

4. Nutrition (pre-fetched from MacroFactor):
   <macrofactor-data>
{macrofactor_csv}
   </macrofactor-data>
{clue_block}

After fetching Withings data, summarize what you received for each source (row counts, date ranges covered). Then STOP and wait for the next instruction.
"""

    # ── PROMPT 2: Analysis ──
    prompt_2_analyze = f"""\
This is STEP 2 of 3. Using ALL the data from step 1 (Withings results + the inline CSV data), compute the full analysis. Do NOT call any tools or fetch any data — everything you need is already in the conversation.

Parse and bucket all data into current_week ({this_start.isoformat()}..{this_end.isoformat()}) and prior_week ({prev_start.isoformat()}..{prev_end.isoformat()}).

A. WORKOUT STATS — parse the workout CSV:
   - CSV columns: Date, Session, Exercise, Sets_x_Reps, Weight_lbs, Actual, How_I_Felt (1-5 scale).
   - Per week: distinct training days, per-session summary, per-exercise volume (sets * reps * weight_lbs; bodyweight/band exercises = sets * reps only), total sets, total volume, volume by split, avg "How I Felt" per session and overall.

B. NUTRITION STATS — parse the MacroFactor CSV:
   - Per week: avg daily kcal, avg protein/carbs/fat (g), avg steps, days logged, adherence vs targets.

C. WEEK-OVER-WEEK DELTAS — compute absolute and % change for every metric.

D. SYNTHESIS — cross-source insights:
   a. Energy balance: net_kcal = sum(Calories) - sum(Expenditure). Expected weight change = net_kcal / 3500 lbs. Actual trend weight change = last minus first "Trend Weight (lbs)". Report gap; if >0.5 lb flag causes.
   b. Body composition: if Withings has lean + fat mass, classify direction (recomp / regressing / deficit / surplus). If unavailable, note it.
   c. Training load vs recovery: compare volume trend to avg HR trend. Volume up + HR up = flag fatigue. Volume up + HR stable = adapting well.
   d. NEAT check: if in deficit but trend weight didn't drop as expected, check if steps fell >10% WoW. Flag if so.
   e. RPE trend: if weekly avg "How I Felt" dropped >0.5 WoW, flag recovery concern. If rising with volume, note positive.
   f. Cycle phase: if cycle data was provided, contextualize weight/energy/performance. Luteal phase weight gain of 1-3 lbs is water retention, not fat. If no cycle data, skip.

Print a concise human-readable summary with all metrics and deltas. Then STOP and wait for the next instruction.
"""

    # ── PROMPT 3: Notion Write ──
    prompt_3_write = f"""\
This is STEP 3 of 3. Write the analysis from step 2 to Notion. Do NOT recompute anything — use the results you already have.

Write a structured weekly sub-page directly under the "2026 Goals" Notion page (ID: 47325bff-c70b-42cc-8f0d-91ae062156b4).

Upsert procedure (keyed by week_start={this_start.isoformat()}):
  a. Search Notion for a page titled exactly "Week of {this_start.isoformat()}" under parent 47325bff-c70b-42cc-8f0d-91ae062156b4. Use notion-search — do NOT fetch the full 2026 Goals page.
  b. If found, call notion-update-page to replace its body. If not found, call notion-create-pages with parent=47325bff-c70b-42cc-8f0d-91ae062156b4 and title "Week of {this_start.isoformat()}".
  Do NOT create a duplicate sub-page.

Page content, in this order:
  1. **Key Insights** — 3-5 bullet points, most actionable first. Energy balance gap, body comp direction, fatigue/recovery flags, NEAT warnings, RPE trends, cycle phase effects. Punchy and specific.
  2. **Weight & Body Composition** (Withings) — metrics + WoW deltas.
  3. **Activity** (Withings) — steps, cardio, HR, calories burned + WoW deltas.
  4. **Strength Workouts** (Google Sheets) — session list, volume totals, RPE scores + WoW deltas.
  5. **Nutrition** (MacroFactor) — daily avgs, target adherence, energy balance + WoW deltas.
  6. **Cycle Phase** (Clue, if available) — current phase, cycle day, expected effects.
  7. **Data Sources** — one line per source confirming what was used.

After writing, confirm:
- "Notion weekly sub-page: <created|updated> at <page URL or ID>, titled 'Week of {this_start.isoformat()}'".
- "Activity from Withings: <avg daily steps>, <N> cardio sessions".
- "Workouts from CSV: <N> exercise rows, <N> training days, total volume=<N> lbs".
- "Nutrition from CSV: <N> days current week, <N> days prior week".
- "Synthesis: net energy balance=<N> kcal, expected change=<N> lbs, actual trend change=<N> lbs, gap=<N> lbs".

If any source had zero rows or errors, state that explicitly.
"""

    return [prompt_1_collect, prompt_2_analyze, prompt_3_write]


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def send_and_stream(client: anthropic.Anthropic, session_id: str, text: str) -> bool:
    """Send a user message and stream the agent response. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client.beta.sessions.events.send(
                session_id=session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
                betas=[BETA],
            )

            with client.beta.sessions.events.stream(
                session_id=session_id,
                betas=[BETA],
            ) as stream:
                for event in stream:
                    event_type = getattr(event, "type", None)
                    if event_type == "agent.message":
                        for block in event.content:
                            if block.type == "text":
                                print(block.text, end="", flush=True)
                    elif event_type == "session.status_terminated":
                        print("\n[session terminated unexpectedly]", flush=True)
                        return False
            print(flush=True)  # trailing newline
            return True

        except anthropic.RateLimitError:
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                print(f"\n[rate limited, retry {attempt}/{MAX_RETRIES} after {wait}s]", flush=True)
                time.sleep(wait)
            else:
                print(f"\n[rate limited, retries exhausted]", flush=True)
                return False

        except anthropic.APIStatusError as e:
            if "rate" in str(e).lower() and attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                print(f"\n[rate limited ({e.status_code}), retry {attempt}/{MAX_RETRIES} after {wait}s]", flush=True)
                time.sleep(wait)
            else:
                raise

    return False


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

    prompts = build_prompts()
    step_names = ["Data Collection", "Analysis", "Notion Write"]

    try:
        for i, (prompt, name) in enumerate(zip(prompts, step_names), 1):
            print(f"\n{'='*60}", flush=True)
            print(f"STEP {i}/3: {name}", flush=True)
            print(f"{'='*60}\n", flush=True)

            ok = send_and_stream(client, session.id, prompt)
            if not ok:
                print(f"\nerror: step {i} ({name}) failed", file=sys.stderr)
                return 1

            # Brief pause between steps to stay under rate limits
            if i < len(prompts):
                print(f"\n[pausing 15s before next step]", flush=True)
                time.sleep(15)

    finally:
        client.beta.sessions.archive(session.id, betas=[BETA])
        print(f"\narchived session {session.id}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
