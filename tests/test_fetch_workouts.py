"""Tests for fetch_workouts.py — Google Sheets grid parsing and date handling."""

from datetime import date, timedelta

import pytest

from scripts.fetch_workouts import find_target_tab, parse_date_value, parse_grid


# ── parse_date_value ──


class TestParseDateValue:
    def test_excel_serial_integer(self):
        # 46112 = 2026-04-01 (Excel epoch: 1899-12-30 + 46112 days)
        result = parse_date_value(46112, 2026)
        assert result == date(1899, 12, 30) + timedelta(days=46112)

    def test_excel_serial_float(self):
        result = parse_date_value(46112.0, 2026)
        assert result == date(1899, 12, 30) + timedelta(days=46112)

    def test_excel_serial_string(self):
        result = parse_date_value("46112", 2026)
        assert result == date(1899, 12, 30) + timedelta(days=46112)

    def test_short_date_text(self):
        result = parse_date_value("Apr 1", 2026)
        assert result == date(2026, 4, 1)

    def test_short_date_with_quotes(self):
        result = parse_date_value('"Mar 31"', 2026)
        assert result == date(2026, 3, 31)

    def test_empty_string(self):
        assert parse_date_value("", 2026) is None

    def test_none(self):
        assert parse_date_value(None, 2026) is None

    def test_non_date_text(self):
        assert parse_date_value("Exercise", 2026) is None

    def test_all_months(self):
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        for i, mon in enumerate(months, 1):
            result = parse_date_value(f"{mon} 15", 2026)
            assert result.month == i
            assert result.day == 15


# ── find_target_tab ──


class TestFindTargetTab:
    def _make_sheets(self, titles: list[str]) -> list[dict]:
        return [{"properties": {"title": t}} for t in titles]

    def test_exact_overlap(self):
        sheets = self._make_sheets([
            "2026-03-31 to 2026-04-26 (Phase 1 W1-4)",
            "2026-04-27 to 2026-05-24 (Phase 2 W5-8)",
        ])
        result = find_target_tab(sheets, date(2026, 4, 1), date(2026, 4, 12))
        assert "2026-03-31" in result

    def test_window_spans_two_tabs(self):
        sheets = self._make_sheets([
            "2026-03-31 to 2026-04-26 (Phase 1 W1-4)",
            "2026-04-27 to 2026-05-24 (Phase 2 W5-8)",
        ])
        # Returns first matching tab
        result = find_target_tab(sheets, date(2026, 4, 20), date(2026, 5, 3))
        assert result is not None

    def test_no_matching_tab(self):
        sheets = self._make_sheets([
            "2026-03-31 to 2026-04-26 (Phase 1 W1-4)",
        ])
        result = find_target_tab(sheets, date(2026, 6, 1), date(2026, 6, 14))
        assert result is None

    def test_tab_without_dates(self):
        sheets = self._make_sheets(["Notes", "Template"])
        result = find_target_tab(sheets, date(2026, 4, 1), date(2026, 4, 14))
        assert result is None

    def test_leading_spaces_in_tab_name(self):
        sheets = self._make_sheets([
            "  2026-03-31 to 2026-04-26 (Phase 1 W1-4)",
        ])
        result = find_target_tab(sheets, date(2026, 4, 1), date(2026, 4, 12))
        assert result is not None


# ── parse_grid ──


class TestParseGrid:
    def test_basic_exercise_row(self):
        """A session header followed by exercise rows should produce CSV output."""
        rows = [
            # Session header: day name in col 0, date serial in col 4
            ["Tuesday — Upper A (Strength)", "", "", "", 46112, "", "", "", "", ""],
            # Column headers (skipped)
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", "", "", "", "", ""],
            # Exercise data
            ["Bench Press", "3x10", 100, 3, 4, "", "", "", "", ""],
        ]
        csv_text = parse_grid(rows, 2026, date(2026, 3, 1), date(2026, 12, 31))
        lines = csv_text.strip().split("\n")
        assert len(lines) >= 2  # header + at least 1 data row
        assert "Bench Press" in csv_text
        assert "3x10" in csv_text

    def test_multiple_weeks_side_by_side(self):
        """Week 1 data in columns 0-4, Week 2 data in columns 6-10."""
        rows = [
            # Session headers for week 1 and week 2
            ["Tue — Upper A", "", "", "", 46112, "",
             "Mon — Upper A", "", "", "", 46118, ""],
            # Column headers
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", "",
             "Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", ""],
            # Exercise data for both weeks
            ["Bench Press", "3x10", 80, 3, 4, "",
             "Bench Press", "3x10", 85, 3, 4, ""],
        ]
        csv_text = parse_grid(rows, 2026, date(2026, 3, 1), date(2026, 12, 31))
        # Should find Bench Press from both weeks
        lines = [l for l in csv_text.strip().split("\n") if "Bench Press" in l]
        assert len(lines) == 2

    def test_date_filtering(self):
        """Only rows within the date window should be included."""
        rows = [
            ["Tue — Upper A", "", "", "", 46112, ""],  # some date
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", ""],
            ["Bench Press", "3x10", 100, 3, 4, ""],
        ]
        actual_date = date(1899, 12, 30) + timedelta(days=46112)
        # Window that excludes this date
        csv_text = parse_grid(rows, 2026, date(2099, 1, 1), date(2099, 1, 7))
        lines = csv_text.strip().split("\n")
        # Should only have header, no data rows
        assert len(lines) == 1

    def test_skip_exercise_header_rows(self):
        """Rows where Exercise column literally says 'Exercise' should be skipped."""
        rows = [
            ["Tue — Upper A", "", "", "", 46112, ""],
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", ""],
            ["Bench Press", "3x10", 100, 3, 4, ""],
        ]
        csv_text = parse_grid(rows, 2026, date(2026, 3, 1), date(2026, 12, 31))
        # "Exercise" as a literal value should not appear as a data row
        data_lines = [l for l in csv_text.strip().split("\n")[1:] if l.strip()]
        for line in data_lines:
            assert not line.startswith("Exercise,Exercise")

    def test_empty_rows_skipped(self):
        """Empty cells in the exercise column should be skipped."""
        rows = [
            ["Tue — Upper A", "", "", "", 46112, ""],
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", ""],
            ["Bench Press", "3x10", 100, 3, 4, ""],
            ["", "", "", "", "", ""],  # empty row
            ["Row", "3x10", 80, 3, "", ""],
        ]
        csv_text = parse_grid(rows, 2026, date(2026, 3, 1), date(2026, 12, 31))
        data_lines = [l for l in csv_text.strip().split("\n")[1:] if l.strip()]
        assert len(data_lines) == 2  # Bench + Row, empty row skipped

    def test_bodyweight_exercise(self):
        """Exercises with non-numeric weight (bands) should still be captured."""
        rows = [
            ["Tue — Lower A", "", "", "", 46112, ""],
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", ""],
            ["Banded Walks", "2x17", "Crazy Band", 2, "", ""],
        ]
        csv_text = parse_grid(rows, 2026, date(2026, 3, 1), date(2026, 12, 31))
        assert "Banded Walks" in csv_text
        assert "Crazy Band" in csv_text

    def test_csv_header(self):
        """Output CSV should have the correct header."""
        rows = [
            ["Tue — Upper A", "", "", "", 46112, ""],
            ["Exercise", "Sets x Reps", "Weight (lbs)", "Actual", "How I Felt", ""],
            ["Bench Press", "3x10", 100, 3, 4, ""],
        ]
        csv_text = parse_grid(rows, 2026, date(2026, 3, 1), date(2026, 12, 31))
        header = csv_text.strip().split("\n")[0].strip()
        assert header == "Date,Session,Exercise,Sets_x_Reps,Weight_lbs,Actual,How_I_Felt"
