"""Monthly program designer — MVP WIP.

Reads accumulated weekly health reports from Notion (sub-pages under 2026 Goals),
analyzes multi-week trends, and designs the next training block. Intended to run
monthly (or on-demand) after 4-6 weeks of weekly summaries have been collected.

Status: MVP WIP — not yet wired into a GitHub Action.

Dependencies:
- Anthropic Managed Agent with Notion MCP access
- Weekly summary sub-pages under 2026 Goals (produced by weekly_summary.py)
- Optionally: Google Sheets MCP to write the new program back to the training sheet

Design:
- Single-shot managed agent session (same pattern as weekly_summary.py)
- Prompt reads all weekly sub-pages, extracts trends, and generates a new program
- Output: new Notion page with the program + optional Google Sheets update
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

# import anthropic  # uncomment when ready to run

BETA = "managed-agents-2026-04-01"
GOALS_PAGE_ID = "47325bff-c70b-42cc-8f0d-91ae062156b4"


def build_prompt() -> str:
    today = date.today()

    goals_path = Path(os.environ.get("GOALS_PATH", "data/goals.txt"))
    if not goals_path.exists():
        goals_path = Path(__file__).resolve().parent.parent / "data" / "goals.txt"
    goals_text = goals_path.read_text().rstrip() if goals_path.exists() else "No goals configured."

    return f"""\
You are a strength & nutrition coach designing the next 4-week training block.

TODAY: {today.isoformat()}

STEP 1 — GATHER DATA
Fetch the "2026 Goals" page (ID: {GOALS_PAGE_ID}) via Notion MCP and list all child pages.
Each child titled "Week of YYYY-MM-DD" is a weekly health report. Read every weekly report
that exists (up to the 8 most recent). Extract from each:
- Weight & body composition (trend weight, fat %, lean mass)
- Training volume totals and per-split breakdown
- RPE / "How I Felt" averages
- Nutrition averages (calories, protein, adherence %)
- Steps / activity
- Cycle phase (if present)
- Key insights section

STEP 2 — TREND ANALYSIS
From the collected weekly data, compute and report:

a. Weight trajectory: plot trend weight week-over-week. Compute avg weekly loss rate.
   Flag if rate exceeds 1% bodyweight/week (too aggressive for muscle preservation).
   Flag if weight is flat for 3+ weeks (plateau — may need deficit adjustment).

b. Body composition direction: classify the overall trend as recomp / fat loss / muscle loss / plateau.
   If lean mass is declining, flag immediately — this is the primary risk to watch.

c. Strength progression: for each major lift (Bench, Squat, RDL, Hip Thrust, etc.),
   track weight x reps over time. Flag any lifts that regressed (went down in weight or reps).
   Identify lifts that are progressing well.

d. Volume trend: total weekly volume (sets x reps x weight) over time.
   Flag if volume jumped >20% in a single week (injury risk).
   Note if volume has been flat — may need progressive overload.

e. Recovery signals: RPE trend, avg HR trend if available.
   If RPE is declining over multiple weeks while volume increases, recovery is suffering.

f. Nutrition adherence: avg calorie and protein adherence % over the period.
   If protein is consistently under target, flag — critical for muscle preservation on a deficit.

g. Cycle phase patterns: if cycle data exists across multiple weeks, note any consistent
   patterns (e.g., RPE always drops in luteal phase, weight always spikes pre-period).

STEP 3 — PROGRAM DESIGN
Based on the trend analysis, design the next 4-week training block.

User goals (from data/goals.txt — editable without code changes):
{goals_text}

For the new block, provide:
a. Program philosophy: what's changing and why (based on the data).
b. Split structure: keep current split or modify (with rationale).
c. Exercise selection: for each session, list exercises with sets x reps and starting weight.
   - Keep exercises that are progressing well.
   - Swap exercises that have stalled for variations that target the same muscle group.
   - Adjust rep ranges based on goals (strength: 3-6, hypertrophy: 8-12, endurance: 15+).
d. Progression model: how to add weight/reps week to week.
e. Deload recommendation: when to deload based on accumulated fatigue signals.
f. Nutrition adjustments: any macro target changes (e.g., increase protein, adjust deficit).
g. NEAT target: daily step goal based on trend.

STEP 4 — OUTPUT
a. Print a summary to the session log.
b. Create a Notion page under 2026 Goals titled "Program Block — {today.isoformat()}" with:
   1. **Trend Summary** — the multi-week analysis from step 2.
   2. **Program Overview** — philosophy, split, progression model.
   3. **Nutrition Targets** — updated macro targets with rationale.
   4. **Deload Plan** — when and how.
   5. **Data Sources** — which weekly reports were used.
   6. **Program Sheets** — one table per training day, formatted so the user can copy-paste
      directly into Google Sheets. Each table must use this exact layout:

      Session title as a heading (e.g., "Monday — Upper A (Strength)").
      Then a Notion table with these columns:
      | Exercise | Sets x Reps | Weight (lbs) | Actual | How I Felt |

      Populate Exercise, Sets x Reps, and Weight (lbs) with the prescribed program.
      Leave "Actual" and "How I Felt" blank — those are filled in during training.

      Include a week-by-week progression note under each table showing the planned
      weight/rep increases for weeks 2-4 (e.g., "Week 2: Bench +2.5 lbs, Week 3: +5 lbs").

      Order the tables by training day (Mon, Tue, Thu, Fri, Sat/Sun).
"""


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


# def main() -> int:
#     """Uncomment when ready to run."""
#     api_key = require_env("ANTHROPIC_API_KEY")
#     agent_id = require_env("AGENT_ID")
#     environment_id = require_env("ENVIRONMENT_ID")
#     vault_id = require_env("VAULT_ID")
#
#     client = anthropic.Anthropic(api_key=api_key)
#
#     session = client.beta.sessions.create(
#         agent=agent_id,
#         environment_id=environment_id,
#         vault_ids=[vault_id],
#         title="Monthly Program Design",
#         betas=[BETA],
#     )
#     print(f"session: {session.id}", flush=True)
#
#     client.beta.sessions.events.send(
#         session_id=session.id,
#         events=[
#             {
#                 "type": "user.message",
#                 "content": [{"type": "text", "text": build_prompt()}],
#             }
#         ],
#         betas=[BETA],
#     )
#
#     try:
#         with client.beta.sessions.events.stream(
#             session_id=session.id,
#             betas=[BETA],
#         ) as stream:
#             for event in stream:
#                 event_type = getattr(event, "type", None)
#                 if event_type == "agent.message":
#                     for block in event.content:
#                         if block.type == "text":
#                             print(block.text, end="", flush=True)
#                 elif event_type == "session.status_terminated":
#                     break
#         print()
#     finally:
#         client.beta.sessions.archive(session.id, betas=[BETA])
#         print(f"archived session {session.id}", flush=True)
#
#     return 0
#
#
# if __name__ == "__main__":
#     raise SystemExit(main())
