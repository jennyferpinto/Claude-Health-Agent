"""Parse Clue export and output cycle phase context for the agent prompt.

Reads the measurements.json from a Clue data export, identifies period start
dates, computes average cycle length, and determines the current cycle phase
for the target date window. Outputs a small text file (~50 tokens) that gets
embedded in the agent prompt.

The Clue export only needs to be re-uploaded occasionally — cycle length is
stable enough that predictions remain accurate for weeks.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

DEFAULT_OUTPUT = "/tmp/clue_context.txt"

PHASES = [
    ("Menstrual", 1, 5),
    ("Follicular", 6, 13),
    ("Ovulatory", 14, 16),
    ("Luteal", 17, 99),  # remainder of cycle
]


def find_cycle_starts(measurements: list[dict]) -> list[date]:
    """Extract period start dates from Clue measurements (gap > 7 days = new cycle)."""
    period = sorted(
        [e for e in measurements if e.get("type") == "period"],
        key=lambda x: x["date"],
    )
    if not period:
        return []

    starts = [date.fromisoformat(period[0]["date"])]
    for i in range(1, len(period)):
        prev = date.fromisoformat(period[i - 1]["date"])
        curr = date.fromisoformat(period[i]["date"])
        if (curr - prev).days > 7:
            starts.append(curr)
    return starts


def avg_cycle_length(starts: list[date], recent_n: int = 6) -> float:
    """Average cycle length from the most recent N cycles."""
    recent = starts[-recent_n - 1:]  # need N+1 starts to get N gaps
    gaps = [(recent[i + 1] - recent[i]).days for i in range(len(recent) - 1)]
    return sum(gaps) / len(gaps) if gaps else 28.0


def get_phase(cycle_day: int) -> str:
    """Return the phase name for a given cycle day."""
    for name, start, end in PHASES:
        if start <= cycle_day <= end:
            return name
    return "Luteal"


def build_context(starts: list[date], window_start: date, window_end: date) -> str:
    """Build a compact cycle context string for the agent prompt."""
    avg_len = avg_cycle_length(starts)
    last_start = starts[-1]

    # Cycle day at window_start and window_end
    day_at_start = (window_start - last_start).days + 1
    day_at_end = (window_end - last_start).days + 1

    # If cycle day is beyond average length, a new cycle likely started
    # Estimate the next period start
    next_period = last_start + timedelta(days=round(avg_len))

    # Handle if we've rolled into a predicted new cycle
    if day_at_start > round(avg_len):
        predicted_start = next_period
        day_at_start = (window_start - predicted_start).days + 1
        day_at_end = (window_end - predicted_start).days + 1
        last_start = predicted_start
        next_period = predicted_start + timedelta(days=round(avg_len))

    phase_start = get_phase(max(1, day_at_start))
    phase_end = get_phase(max(1, day_at_end))

    lines = [
        f"Last period start: {last_start.isoformat()}",
        f"Average cycle length: {avg_len:.0f} days",
        f"Cycle day at week start ({window_start.isoformat()}): {day_at_start} ({phase_start} phase)",
        f"Cycle day at week end ({window_end.isoformat()}): {day_at_end} ({phase_end} phase)",
        f"Next period expected: ~{next_period.isoformat()}",
    ]

    if phase_start != phase_end:
        lines.append(f"Note: week spans {phase_start} -> {phase_end} phase transition.")

    return "\n".join(lines)


def main() -> int:
    clue_path = Path(os.environ.get("CLUE_EXPORT_PATH", "/tmp/clue_data/measurements.json"))
    if not clue_path.exists():
        print(f"error: Clue measurements.json not found at {clue_path}", file=sys.stderr)
        return 1

    output_path = os.environ.get("CLUE_CONTEXT_PATH", DEFAULT_OUTPUT)

    window_start_str = os.environ.get("WINDOW_START")
    window_end_str = os.environ.get("WINDOW_END")
    if not window_start_str or not window_end_str:
        print("error: WINDOW_START and WINDOW_END must be set (YYYY-MM-DD)", file=sys.stderr)
        return 1
    window_start = date.fromisoformat(window_start_str)
    window_end = date.fromisoformat(window_end_str)

    with open(clue_path) as f:
        measurements = json.load(f)

    starts = find_cycle_starts(measurements)
    if not starts:
        print("error: no period data found in Clue export", file=sys.stderr)
        return 1

    print(f"found {len(starts)} cycles, last start: {starts[-1].isoformat()}")
    context = build_context(starts, window_start, window_end)
    print(context)

    with open(output_path, "w") as f:
        f.write(context)
    print(f"\nwrote context to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
