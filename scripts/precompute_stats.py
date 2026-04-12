"""Pre-compute workout and nutrition stats on the runner.

Reads the raw workout CSV and MacroFactor CSV, computes all per-week stats
(volume, RPE, nutrition averages, adherence, energy balance), and writes
compact summary files that get embedded in the agent prompt. This saves the
agent from parsing CSV and doing arithmetic — cutting output tokens significantly.
"""

import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


def parse_sets_reps(s: str) -> list[tuple[int, int]]:
    """Parse Sets_x_Reps like '3x10' or '3x9,9,8' into [(sets, reps), ...]."""
    if not s:
        return []
    s = s.strip()
    # Match patterns like "3x10", "3x9,9,8", "2x17"
    m = re.match(r"(\d+)\s*x\s*(.+)", s, re.IGNORECASE)
    if not m:
        return []
    sets = int(m.group(1))
    reps_part = m.group(2)
    # "3x10" -> 3 sets of 10; "3x9,9,8" -> sets of 9,9,8
    reps_list = [r.strip() for r in reps_part.split(",")]
    result = []
    for r in reps_list:
        try:
            result.append((1, int(r)))
        except ValueError:
            continue
    if len(result) == 1 and sets > 1:
        # "3x10" means 3 sets of 10
        result = [(sets, result[0][1])]
    return result


def parse_weight(s: str) -> float | None:
    """Parse weight value, returning None for bodyweight/band exercises."""
    if not s:
        return None
    s = s.strip()
    # Handle comma-separated weights like "95,100,100" — use average
    if "," in s:
        parts = s.split(",")
        nums = []
        for p in parts:
            try:
                nums.append(float(p.strip()))
            except ValueError:
                return None
        return sum(nums) / len(nums) if nums else None
    try:
        return float(s)
    except ValueError:
        return None  # "Black band", "Crazy Band", etc.


def compute_workout_stats(csv_path: Path, week_start: date, week_end: date) -> dict:
    """Compute workout stats for a single week."""
    stats = {
        "training_days": set(),
        "sessions": [],
        "total_sets": 0,
        "total_volume": 0.0,
        "total_bw_sets": 0,
        "volume_by_split": defaultdict(float),
        "rpe_scores": [],
        "exercises": [],
        "notes": [],
    }

    # First pass: collect all rows and per-session RPE scores
    session_rows: dict[tuple[str, str], list[dict]] = defaultdict(list)
    session_rpe: dict[tuple[str, str], float | None] = {}

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_date = date.fromisoformat(row["Date"])
            if not (week_start <= row_date <= week_end):
                continue

            exercise = row.get("Exercise", "").strip()
            if not exercise:
                continue

            session = row.get("Session", "")
            session_key = (row_date.isoformat(), session)
            session_rows[session_key].append(row)

            # Track RPE: first non-blank score in a session applies to the whole session
            feel = row.get("How_I_Felt", "").strip()
            if feel and session_key not in session_rpe:
                try:
                    session_rpe[session_key] = float(feel)
                except ValueError:
                    pass

    # Second pass: compute stats with propagated RPE
    for session_key, rows in sorted(session_rows.items()):
        row_date = date.fromisoformat(session_key[0])
        session = session_key[1]
        session_feel = session_rpe.get(session_key)

        for row in rows:
            exercise = row.get("Exercise", "").strip()
            sets_reps_str = row.get("Sets_x_Reps", "")
            weight_str = row.get("Weight_lbs", "")

            sr = parse_sets_reps(sets_reps_str)

            # No sets/reps = note row (skipped session, mobility, etc.)
            if not sr:
                stats["notes"].append({
                    "date": row_date.isoformat(),
                    "session": session,
                    "note": exercise,
                })
                continue

            weight = parse_weight(weight_str)
            stats["training_days"].add(row_date)

            total_sets = sum(s for s, r in sr)
            total_reps = sum(s * r for s, r in sr)

            if weight is not None and weight > 0:
                volume = total_reps * weight
                stats["total_volume"] += volume
            else:
                volume = 0.0
                stats["total_bw_sets"] += total_sets

            stats["total_sets"] += total_sets

            # Extract split name from session (e.g. "Tuesday — Upper A (Strength)" -> "Upper A")
            split = ""
            if "—" in session:
                split = session.split("—", 1)[1].strip()
                split = re.sub(r"\s*\(.*\)", "", split).strip()
            if split:
                stats["volume_by_split"][split] += volume

            # Use session-level RPE (propagated from first logged score in session)
            if session_feel is not None:
                stats["rpe_scores"].append(session_feel)

            stats["exercises"].append({
                "date": row_date.isoformat(),
                "session": session,
                "exercise": exercise,
                "sets_reps": sets_reps_str,
                "weight": weight_str,
                "sets": total_sets,
                "volume": volume,
            })

    stats["training_days"] = sorted(stats["training_days"])
    return stats


def compute_nutrition_stats(csv_path: Path, week_start: date, week_end: date) -> dict:
    """Compute nutrition stats for a single week."""
    days = []

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_date = date.fromisoformat(row["Date"])
            if not (week_start <= row_date <= week_end):
                continue
            days.append(row)

    if not days:
        return {"days_logged": 0}

    def avg(key):
        vals = [float(d[key]) for d in days if d.get(key)]
        return sum(vals) / len(vals) if vals else 0.0

    def total(key):
        return sum(float(d[key]) for d in days if d.get(key))

    stats = {
        "days_logged": len(days),
        "avg_calories": round(avg("Calories (kcal)"), 0),
        "avg_protein": round(avg("Protein (g)"), 1),
        "avg_carbs": round(avg("Carbs (g)"), 1),
        "avg_fat": round(avg("Fat (g)"), 1),
        "avg_steps": round(avg("Steps"), 0),
        "avg_target_cal": round(avg("Target Calories (kcal)"), 0),
        "avg_target_protein": round(avg("Target Protein (g)"), 1),
        "avg_target_carbs": round(avg("Target Carbs (g)"), 1),
        "avg_target_fat": round(avg("Target Fat (g)"), 1),
        "total_calories": round(total("Calories (kcal)"), 0),
        "total_expenditure": round(total("Expenditure"), 0),
    }

    # Adherence percentages
    if stats["avg_target_cal"]:
        stats["cal_adherence_pct"] = round(stats["avg_calories"] / stats["avg_target_cal"] * 100, 1)
    if stats["avg_target_protein"]:
        stats["protein_adherence_pct"] = round(stats["avg_protein"] / stats["avg_target_protein"] * 100, 1)

    # Energy balance
    stats["net_kcal"] = round(stats["total_calories"] - stats["total_expenditure"], 0)
    stats["expected_weight_change_lbs"] = round(stats["net_kcal"] / 3500, 2)

    # Trend weight change
    trend_weights = [float(d["Trend Weight (lbs)"]) for d in days if d.get("Trend Weight (lbs)")]
    if len(trend_weights) >= 2:
        stats["trend_weight_first"] = round(trend_weights[0], 2)
        stats["trend_weight_last"] = round(trend_weights[-1], 2)
        stats["trend_weight_change"] = round(trend_weights[-1] - trend_weights[0], 2)

    return stats


def format_workout_summary(label: str, stats: dict) -> str:
    """Format workout stats as compact text."""
    if not stats["training_days"]:
        return f"{label}: no training days logged.\n"

    lines = [f"{label}:"]
    lines.append(f"  Training days: {len(stats['training_days'])} ({', '.join(d.isoformat() for d in stats['training_days'])})")
    lines.append(f"  Total sets: {stats['total_sets']}")
    lines.append(f"  Total weighted volume: {stats['total_volume']:,.0f} lbs")
    if stats["total_bw_sets"]:
        lines.append(f"  Bodyweight/band sets (no weight): {stats['total_bw_sets']}")

    if stats["volume_by_split"]:
        lines.append("  Volume by split:")
        for split, vol in sorted(stats["volume_by_split"].items()):
            lines.append(f"    {split}: {vol:,.0f} lbs")

    if stats["rpe_scores"]:
        avg_rpe = sum(stats["rpe_scores"]) / len(stats["rpe_scores"])
        lines.append(f"  Avg 'How I Felt': {avg_rpe:.1f}/5 ({len(stats['rpe_scores'])} scores)")

    # Per-session + per-exercise breakdown
    sessions = defaultdict(list)
    for ex in stats["exercises"]:
        sessions[(ex["date"], ex["session"])].append(ex)
    lines.append("  Sessions:")
    for (d, session), exercises in sorted(sessions.items()):
        sess_vol = sum(e["volume"] for e in exercises)
        lines.append(f"    {d} {session}: {len(exercises)} exercises, {sess_vol:,.0f} lbs volume")
        for ex in exercises:
            weight_str = f" @ {ex['weight']}" if ex["weight"] else " (bodyweight/band)"
            lines.append(f"      - {ex['exercise']}: {ex['sets_reps']}{weight_str} -> {ex['volume']:,.0f} lbs vol")

    if stats["notes"]:
        lines.append("  Notes:")
        for n in stats["notes"]:
            lines.append(f"    {n['date']} [{n['session']}]: {n['note']}")

    return "\n".join(lines) + "\n"


def format_nutrition_summary(label: str, stats: dict) -> str:
    """Format nutrition stats as compact text."""
    if stats["days_logged"] == 0:
        return f"{label}: no data logged.\n"

    lines = [f"{label} ({stats['days_logged']} days logged):"]
    lines.append(f"  Avg daily: {stats['avg_calories']:.0f} kcal | P {stats['avg_protein']:.0f}g | C {stats['avg_carbs']:.0f}g | F {stats['avg_fat']:.0f}g")
    lines.append(f"  Targets:   {stats['avg_target_cal']:.0f} kcal | P {stats['avg_target_protein']:.0f}g | C {stats['avg_target_carbs']:.0f}g | F {stats['avg_target_fat']:.0f}g")

    if "cal_adherence_pct" in stats:
        lines.append(f"  Adherence: calories {stats['cal_adherence_pct']:.1f}% | protein {stats.get('protein_adherence_pct', 0):.1f}%")

    lines.append(f"  Avg daily steps: {stats['avg_steps']:.0f}")
    lines.append(f"  Energy balance: {stats['net_kcal']:+,.0f} kcal (total intake {stats['total_calories']:,.0f} - expenditure {stats['total_expenditure']:,.0f})")
    lines.append(f"  Expected weight change: {stats['expected_weight_change_lbs']:+.2f} lbs")

    if "trend_weight_change" in stats:
        lines.append(f"  Trend weight: {stats['trend_weight_first']:.1f} → {stats['trend_weight_last']:.1f} lbs (change: {stats['trend_weight_change']:+.2f} lbs)")
        gap = abs(stats["trend_weight_change"] - stats["expected_weight_change_lbs"])
        lines.append(f"  Gap (actual vs expected): {gap:.2f} lbs")

    return "\n".join(lines) + "\n"


def compute_deltas(current: dict, prior: dict, keys: list[tuple[str, str]]) -> str:
    """Compute WoW deltas for a list of (key, label) pairs."""
    lines = []
    for key, label in keys:
        curr_val = current.get(key)
        prev_val = prior.get(key)
        if curr_val is not None and prev_val is not None and prev_val != 0:
            delta = curr_val - prev_val
            pct = (delta / abs(prev_val)) * 100
            lines.append(f"  {label}: {delta:+.1f} ({pct:+.1f}%)")
        elif curr_val is not None and prev_val is not None:
            delta = curr_val - prev_val
            lines.append(f"  {label}: {delta:+.1f}")
    return "\n".join(lines) if lines else "  No comparable data."


def main() -> int:
    workouts_path = Path(os.environ.get("WORKOUTS_CSV_PATH", "/tmp/workouts.csv"))
    macrofactor_path = Path(os.environ.get("MACROFACTOR_CSV_PATH", "/tmp/macrofactor_filtered.csv"))
    output_path = os.environ.get("STATS_PATH", "/tmp/precomputed_stats.txt")

    window_start_str = os.environ.get("WINDOW_START")
    window_end_str = os.environ.get("WINDOW_END")
    this_start_str = os.environ.get("THIS_START")
    this_end_str = os.environ.get("THIS_END")

    if not all([window_start_str, window_end_str, this_start_str, this_end_str]):
        print("error: WINDOW_START, WINDOW_END, THIS_START, THIS_END must be set", file=sys.stderr)
        return 1

    prev_start = date.fromisoformat(window_start_str)
    this_start = date.fromisoformat(this_start_str)
    this_end = date.fromisoformat(this_end_str)
    prev_end = this_start - timedelta(days=1)

    buf = io.StringIO()

    # ── Workout Stats ──
    if workouts_path.exists():
        curr_w = compute_workout_stats(workouts_path, this_start, this_end)
        prev_w = compute_workout_stats(workouts_path, prev_start, prev_end)

        buf.write(format_workout_summary("CURRENT WEEK WORKOUTS", curr_w))
        buf.write("\n")
        buf.write(format_workout_summary("PRIOR WEEK WORKOUTS", prev_w))
        buf.write("\nWORKOUT WoW DELTAS:\n")

        workout_deltas = [
            ("total_sets", "Total sets"),
            ("total_volume", "Total volume (lbs)"),
        ]
        curr_d = {"total_sets": curr_w["total_sets"], "total_volume": curr_w["total_volume"]}
        prev_d = {"total_sets": prev_w["total_sets"], "total_volume": prev_w["total_volume"]}
        if curr_w["rpe_scores"]:
            curr_d["avg_rpe"] = sum(curr_w["rpe_scores"]) / len(curr_w["rpe_scores"])
        if prev_w["rpe_scores"]:
            prev_d["avg_rpe"] = sum(prev_w["rpe_scores"]) / len(prev_w["rpe_scores"])
            workout_deltas.append(("avg_rpe", "Avg RPE"))
        buf.write(compute_deltas(curr_d, prev_d, workout_deltas))
        buf.write("\n")
    else:
        buf.write("WORKOUTS: no data file found.\n")

    buf.write("\n")

    # ── Nutrition Stats ──
    if macrofactor_path.exists():
        curr_n = compute_nutrition_stats(macrofactor_path, this_start, this_end)
        prev_n = compute_nutrition_stats(macrofactor_path, prev_start, prev_end)

        buf.write(format_nutrition_summary("CURRENT WEEK NUTRITION", curr_n))
        buf.write("\n")
        buf.write(format_nutrition_summary("PRIOR WEEK NUTRITION", prev_n))
        buf.write("\nNUTRITION WoW DELTAS:\n")

        nutrition_deltas = [
            ("avg_calories", "Avg daily kcal"),
            ("avg_protein", "Avg daily protein (g)"),
            ("avg_steps", "Avg daily steps"),
            ("cal_adherence_pct", "Calorie adherence (%)"),
            ("protein_adherence_pct", "Protein adherence (%)"),
        ]
        buf.write(compute_deltas(curr_n, prev_n, nutrition_deltas))
        buf.write("\n")
    else:
        buf.write("NUTRITION: no data file found.\n")

    result = buf.getvalue()
    with open(output_path, "w") as f:
        f.write(result)

    print(result)
    print(f"\nwrote precomputed stats to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
