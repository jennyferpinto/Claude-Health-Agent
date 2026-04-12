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

    clue_path = Path(os.environ.get("CLUE_CONTEXT_PATH", "/tmp/clue_context.txt"))
    clue_context = clue_path.read_text().rstrip() if clue_path.exists() else ""

    stats_path = Path(os.environ.get("STATS_PATH", "/tmp/precomputed_stats.txt"))
    precomputed_stats = stats_path.read_text().rstrip() if stats_path.exists() else ""

    goals_path = Path(os.environ.get("GOALS_PATH", "data/goals.txt"))
    if not goals_path.exists():
        goals_path = Path(__file__).resolve().parent.parent / "data" / "goals.txt"
    goals_text = goals_path.read_text().rstrip() if goals_path.exists() else "No goals configured."

    clue_block = ""
    if clue_context:
        clue_block = f"""
Cycle phase (pre-computed from Clue export):
{clue_context}
Use this to contextualize weight fluctuations (luteal phase = 1-3 lbs water retention), energy, and performance."""
    else:
        clue_block = "No cycle data available — skip cycle-related analysis."

    # ── PROMPT 1: Data Collection (Withings only) ──
    prompt_1_collect = f"""\
Weekly health summary for {this_start.isoformat()} to {this_end.isoformat()} (Mon-Sun).

GOALS:
{goals_text}

This is STEP 1 of 3. ONLY fetch Withings data. Do NOT compute analysis or write to Notion yet.

The Withings API treats startdate as exclusive, so use startdate={withings_start}, enddate={this_end.isoformat()}.

1. Weight + body composition -> Withings MCP.
   Pull all measurements in one call. Report all values in lbs. Bucket into:
   - current_week: {this_start.isoformat()}..{this_end.isoformat()}
   - prior_week: {prev_start.isoformat()}..{prev_end.isoformat()}

2. Daily activity (steps, cardio, calories burned, HR) -> Withings MCP.
   Pull all activity in one call, same date range. Bucket by week.
   Do NOT look for strength training or sleep — neither is in Withings.

All other data (workouts, nutrition, cycle phase) has been pre-computed by the GitHub Action runner — you will receive it in the next step. Do NOT call any tools for workout, nutrition, or cycle data.

After fetching Withings data, report what you received (metrics, date range, row count per week). Then STOP and wait.
"""

    # ── PROMPT 2: Analysis (all stats pre-computed) ──
    prompt_2_analyze = f"""\
This is STEP 2 of 3. Do NOT call any tools. All data is provided below.

WITHINGS DATA: you fetched this in step 1 — use those results.

PRE-COMPUTED STATS (workout volume, nutrition, energy balance, WoW deltas — computed by the runner, not by you):
<precomputed-stats>
{precomputed_stats}
</precomputed-stats>

{clue_block}

Your job is SYNTHESIS ONLY — cross-reference the Withings data with the pre-computed stats above:
a. Energy balance: the runner computed net_kcal, expected weight change, actual trend weight change, and gap. Cross-reference with Withings weight data. If gap >0.5 lb, flag causes (water retention, logging gaps, metabolic adaptation).
b. Body composition: if Withings returned lean + fat mass, classify WoW direction (recomp / regressing / deficit / surplus). If unavailable, note it.
c. Training load vs recovery: compare pre-computed volume trend to Withings avg HR. Volume up + HR up = flag fatigue. Volume up + HR stable = adapting well.
d. NEAT check: if in deficit but weight didn't drop as expected, check if steps fell >10% WoW from the stats above. Flag if so.
e. RPE trend: check pre-computed avg "How I Felt" scores. If dropped >0.5 WoW, flag recovery concern.
f. Cycle phase: if cycle data above, contextualize weight/energy. Luteal weight gain = water retention, not fat.

Print a concise summary with: Withings metrics + WoW deltas, pre-computed workout/nutrition stats (just relay them, don't recompute), and your synthesis insights. Then STOP and wait.
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
