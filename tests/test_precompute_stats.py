"""Tests for precompute_stats.py — workout and nutrition stat computation."""

import csv
import io
import tempfile
from datetime import date
from pathlib import Path

import pytest

from scripts.precompute_stats import (
    compute_nutrition_stats,
    compute_workout_stats,
    parse_sets_reps,
    parse_weight,
)


# ── parse_sets_reps ──


class TestParseSetsReps:
    def test_simple(self):
        assert parse_sets_reps("3x10") == [(3, 10)]

    def test_varied_reps(self):
        # "3x9,9,8" = three sets of 9, 9, 8
        result = parse_sets_reps("3x9,9,8")
        assert result == [(1, 9), (1, 9), (1, 8)]
        assert sum(s * r for s, r in result) == 26

    def test_varied_reps_increasing(self):
        result = parse_sets_reps("3x15,20,20")
        assert result == [(1, 15), (1, 20), (1, 20)]
        assert sum(s * r for s, r in result) == 55

    def test_two_sets(self):
        assert parse_sets_reps("2x17") == [(2, 17)]

    def test_empty(self):
        assert parse_sets_reps("") == []

    def test_no_x(self):
        assert parse_sets_reps("10 reps") == []

    def test_hiit_format(self):
        # "10 x 30 sec max + 60 sec" — not a standard sets x reps
        result = parse_sets_reps("10 x 30 sec max + 60 sec")
        # Should handle gracefully (30 isn't a clean int after "sec")
        # At minimum, shouldn't crash
        assert isinstance(result, list)

    def test_whitespace(self):
        assert parse_sets_reps("  3x10  ") == [(3, 10)]


# ── parse_weight ──


class TestParseWeight:
    def test_simple_float(self):
        assert parse_weight("77.5") == 77.5

    def test_integer(self):
        assert parse_weight("100") == 100.0

    def test_comma_separated_avg(self):
        # "95,100,100" -> average = 98.33
        result = parse_weight("95,100,100")
        assert result == pytest.approx(98.33, abs=0.01)

    def test_comma_two_values(self):
        result = parse_weight("25,20,20")
        assert result == pytest.approx(21.67, abs=0.01)

    def test_band(self):
        assert parse_weight("Black band") is None

    def test_crazy_band(self):
        assert parse_weight("Crazy Band") is None

    def test_empty(self):
        assert parse_weight("") is None

    def test_comma_with_non_numeric(self):
        # "10,10,8" — all numeric, should average
        result = parse_weight("10,10,8")
        assert result == pytest.approx(9.33, abs=0.01)

    def test_whitespace(self):
        assert parse_weight("  55.0  ") == 55.0


# ── compute_workout_stats ──


def _write_workout_csv(rows: list[dict]) -> Path:
    """Write workout rows to a temp CSV and return the path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    fieldnames = ["Date", "Session", "Exercise", "Sets_x_Reps", "Weight_lbs", "Actual", "How_I_Felt"]
    writer = csv.DictWriter(tmp, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    tmp.close()
    return Path(tmp.name)


class TestComputeWorkoutStats:
    def test_basic_stats(self):
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper A (Strength)", "Exercise": "Bench Press",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
            {"Date": "2026-04-01", "Session": "Mon — Upper A (Strength)", "Exercise": "Row",
             "Sets_x_Reps": "3x10", "Weight_lbs": "80", "Actual": "3", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert len(stats["training_days"]) == 1
        assert stats["total_sets"] == 6
        assert stats["total_volume"] == 3000 + 2400  # 30*100 + 30*80
        # RPE=4 from Bench propagates to Row (same session)
        assert stats["rpe_scores"] == [4.0, 4.0]

    def test_bodyweight_exercise(self):
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Lower A", "Exercise": "Banded Walks",
             "Sets_x_Reps": "2x17", "Weight_lbs": "Crazy Band", "Actual": "2", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert stats["total_sets"] == 2
        assert stats["total_volume"] == 0.0
        assert stats["total_bw_sets"] == 2
        assert stats["rpe_scores"] == []  # blank How_I_Felt

    def test_date_filtering(self):
        path = _write_workout_csv([
            {"Date": "2026-03-30", "Session": "Sun — Glute", "Exercise": "Hip Thrust",
             "Sets_x_Reps": "3x12", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
            {"Date": "2026-04-01", "Session": "Tue — Upper", "Exercise": "Bench",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
        ])
        # Only April 1-7
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert len(stats["training_days"]) == 1
        assert stats["training_days"][0] == date(2026, 4, 1)

    def test_varied_reps_volume(self):
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper", "Exercise": "Shoulder Press",
             "Sets_x_Reps": "3x9,9,8", "Weight_lbs": "55", "Actual": "3", "How_I_Felt": "3"},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        # 9+9+8 = 26 reps * 55 lbs = 1430
        assert stats["total_volume"] == 26 * 55

    def test_comma_weight_volume(self):
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Lower", "Exercise": "Back Squat",
             "Sets_x_Reps": "3x8", "Weight_lbs": "95,100,100", "Actual": "3", "How_I_Felt": "4"},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        # 24 reps * avg(95,100,100) = 24 * 98.33 = 2360
        assert stats["total_volume"] == pytest.approx(2360, abs=1)

    def test_volume_by_split(self):
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper A (Strength)", "Exercise": "Bench",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
            {"Date": "2026-04-03", "Session": "Wed — Lower A", "Exercise": "Squat",
             "Sets_x_Reps": "3x8", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert "Upper A" in stats["volume_by_split"]
        assert "Lower A" in stats["volume_by_split"]

    def test_empty_week(self):
        path = _write_workout_csv([
            {"Date": "2026-03-25", "Session": "Mon — Upper", "Exercise": "Bench",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert len(stats["training_days"]) == 0
        assert stats["total_sets"] == 0
        assert stats["total_volume"] == 0.0

    def test_rpe_propagation_within_session(self):
        """If one exercise in a session has RPE logged, all exercises in that session get it."""
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Bench Press",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Row",
             "Sets_x_Reps": "3x10", "Weight_lbs": "80", "Actual": "3", "How_I_Felt": ""},
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Lat Pulldown",
             "Sets_x_Reps": "3x15", "Weight_lbs": "50", "Actual": "3", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        # All 3 exercises should get RPE=4 from the first exercise
        assert stats["rpe_scores"] == [4.0, 4.0, 4.0]

    def test_rpe_no_scores_in_session(self):
        """If no exercise in a session has RPE, none should be recorded."""
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Bench",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": ""},
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Row",
             "Sets_x_Reps": "3x10", "Weight_lbs": "80", "Actual": "3", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert stats["rpe_scores"] == []

    def test_rpe_different_sessions(self):
        """RPE should propagate within a session but not across sessions."""
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Bench",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Row",
             "Sets_x_Reps": "3x10", "Weight_lbs": "80", "Actual": "3", "How_I_Felt": ""},
            {"Date": "2026-04-03", "Session": "Wed — Lower A", "Exercise": "Squat",
             "Sets_x_Reps": "3x8", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "3"},
            {"Date": "2026-04-03", "Session": "Wed — Lower A", "Exercise": "RDL",
             "Sets_x_Reps": "3x9", "Weight_lbs": "105", "Actual": "3", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        # Upper A: both get 4, Lower A: both get 3
        assert stats["rpe_scores"] == [4.0, 4.0, 3.0, 3.0]

    def test_note_rows_excluded_from_stats(self):
        """Rows without sets/reps (notes, skipped sessions) should not count as exercises."""
        path = _write_workout_csv([
            {"Date": "2026-04-01", "Session": "Mon — Upper A", "Exercise": "Bench Press",
             "Sets_x_Reps": "3x10", "Weight_lbs": "100", "Actual": "3", "How_I_Felt": "4"},
            {"Date": "2026-04-01", "Session": "Mon — Upper A",
             "Exercise": "Dropped the Lower B (Posterior Chain) session this week",
             "Sets_x_Reps": "", "Weight_lbs": "", "Actual": "", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert stats["total_sets"] == 3  # only Bench Press
        assert len(stats["exercises"]) == 1
        assert len(stats["notes"]) == 1
        assert "Dropped" in stats["notes"][0]["note"]

    def test_note_rows_captured_separately(self):
        """Notes should capture date, session, and the note text."""
        path = _write_workout_csv([
            {"Date": "2026-04-05", "Session": "Sun — Glute Day",
             "Exercise": "Pre or Post Session — Glute & Hip Focus Mobility Work",
             "Sets_x_Reps": "", "Weight_lbs": "", "Actual": "", "How_I_Felt": ""},
        ])
        stats = compute_workout_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert stats["total_sets"] == 0
        assert len(stats["exercises"]) == 0
        assert len(stats["notes"]) == 1
        assert stats["notes"][0]["date"] == "2026-04-05"
        assert stats["notes"][0]["session"] == "Sun — Glute Day"
        assert "Mobility" in stats["notes"][0]["note"]


# ── compute_nutrition_stats ──


def _write_nutrition_csv(rows: list[dict]) -> Path:
    """Write nutrition rows to a temp CSV and return the path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    writer = csv.DictWriter(tmp, fieldnames=[
        "Date", "Expenditure", "Trend Weight (lbs)", "Weight (lbs)",
        "Calories (kcal)", "Protein (g)", "Fat (g)", "Carbs (g)",
        "Target Calories (kcal)", "Target Protein (g)", "Target Fat (g)",
        "Target Carbs (g)", "Steps",
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    tmp.close()
    return Path(tmp.name)


class TestComputeNutritionStats:
    def test_basic_averages(self):
        path = _write_nutrition_csv([
            {"Date": "2026-04-01", "Expenditure": "2200", "Trend Weight (lbs)": "150",
             "Weight (lbs)": "150", "Calories (kcal)": "1800", "Protein (g)": "140",
             "Fat (g)": "60", "Carbs (g)": "180", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "8000"},
            {"Date": "2026-04-02", "Expenditure": "2300", "Trend Weight (lbs)": "149.5",
             "Weight (lbs)": "149", "Calories (kcal)": "2000", "Protein (g)": "150",
             "Fat (g)": "70", "Carbs (g)": "200", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "10000"},
        ])
        stats = compute_nutrition_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert stats["days_logged"] == 2
        assert stats["avg_calories"] == 1900  # (1800+2000)/2
        assert stats["avg_protein"] == 145.0
        assert stats["total_calories"] == 3800
        assert stats["total_expenditure"] == 4500

    def test_energy_balance(self):
        path = _write_nutrition_csv([
            {"Date": "2026-04-01", "Expenditure": "2200", "Trend Weight (lbs)": "150",
             "Weight (lbs)": "150", "Calories (kcal)": "1800", "Protein (g)": "140",
             "Fat (g)": "60", "Carbs (g)": "180", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "8000"},
        ])
        stats = compute_nutrition_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert stats["net_kcal"] == -400  # 1800 - 2200
        assert stats["expected_weight_change_lbs"] == pytest.approx(-0.11, abs=0.01)

    def test_trend_weight_change(self):
        path = _write_nutrition_csv([
            {"Date": "2026-04-01", "Expenditure": "2200", "Trend Weight (lbs)": "150",
             "Weight (lbs)": "150", "Calories (kcal)": "1800", "Protein (g)": "140",
             "Fat (g)": "60", "Carbs (g)": "180", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "8000"},
            {"Date": "2026-04-05", "Expenditure": "2100", "Trend Weight (lbs)": "149",
             "Weight (lbs)": "148.5", "Calories (kcal)": "1850", "Protein (g)": "142",
             "Fat (g)": "62", "Carbs (g)": "185", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "9000"},
        ])
        stats = compute_nutrition_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert stats["trend_weight_first"] == 150.0
        assert stats["trend_weight_last"] == 149.0
        assert stats["trend_weight_change"] == -1.0

    def test_adherence_percentages(self):
        path = _write_nutrition_csv([
            {"Date": "2026-04-01", "Expenditure": "2200", "Trend Weight (lbs)": "150",
             "Weight (lbs)": "150", "Calories (kcal)": "1900", "Protein (g)": "145",
             "Fat (g)": "65", "Carbs (g)": "190", "Target Calories (kcal)": "2000",
             "Target Protein (g)": "150", "Target Fat (g)": "70", "Target Carbs (g)": "200",
             "Steps": "8000"},
        ])
        stats = compute_nutrition_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert stats["cal_adherence_pct"] == 95.0  # 1900/2000
        assert stats["protein_adherence_pct"] == pytest.approx(96.7, abs=0.1)

    def test_date_filtering(self):
        path = _write_nutrition_csv([
            {"Date": "2026-03-30", "Expenditure": "2200", "Trend Weight (lbs)": "151",
             "Weight (lbs)": "151", "Calories (kcal)": "2500", "Protein (g)": "100",
             "Fat (g)": "90", "Carbs (g)": "300", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "3000"},
            {"Date": "2026-04-01", "Expenditure": "2200", "Trend Weight (lbs)": "150",
             "Weight (lbs)": "150", "Calories (kcal)": "1800", "Protein (g)": "140",
             "Fat (g)": "60", "Carbs (g)": "180", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "8000"},
        ])
        stats = compute_nutrition_stats(path, date(2026, 4, 1), date(2026, 4, 7))

        assert stats["days_logged"] == 1
        assert stats["avg_calories"] == 1800  # only April 1 data

    def test_empty_week(self):
        path = _write_nutrition_csv([
            {"Date": "2026-03-25", "Expenditure": "2200", "Trend Weight (lbs)": "150",
             "Weight (lbs)": "150", "Calories (kcal)": "1800", "Protein (g)": "140",
             "Fat (g)": "60", "Carbs (g)": "180", "Target Calories (kcal)": "1900",
             "Target Protein (g)": "145", "Target Fat (g)": "65", "Target Carbs (g)": "190",
             "Steps": "8000"},
        ])
        stats = compute_nutrition_stats(path, date(2026, 4, 1), date(2026, 4, 7))
        assert stats["days_logged"] == 0
