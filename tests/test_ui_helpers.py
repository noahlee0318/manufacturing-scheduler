"""Edge-case tests for framework-independent UI formatting helpers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shift_scheduler.ui_helpers import (  # noqa: E402
    build_availability_windows,
    format_clock,
    format_duration,
    format_offset,
    minutes_to_clock,
    offset_datetime,
    parse_typed_time,
    safe_filename,
    shift_clock_offset,
    time_to_minutes,
)


DAY_SHIFT = {
    "id": 1,
    "startMinutes": 300,
    "endMinutes": 930,
    "startTime": "05:00",
    "endTime": "15:30",
    "availableHours": 10.5,
    "crossesMidnight": False,
}

OVERNIGHT_SHIFT = {
    "id": 2,
    "startMinutes": 930,
    "endMinutes": 120,
    "startTime": "15:30",
    "endTime": "02:00",
    "availableHours": 10.5,
    "crossesMidnight": True,
}

MEAL = {
    "label": "Meal",
    "startTime": "20:55",
    "durationMinutes": 40,
}


class TypedTimeTests(unittest.TestCase):
    def test_common_twelve_and_twenty_four_hour_inputs(self) -> None:
        cases = {
            "5": "05:00",
            "5:07": "05:07",
            "5:7 p.m.": "17:07",
            "5.07 PM": "17:07",
            "12am": "00:00",
            "12 PM": "12:00",
            "0000": "00:00",
            "1530": "15:30",
            "23:59": "23:59",
        }
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(expected, parse_typed_time(typed))

    def test_invalid_and_blank_time_inputs(self) -> None:
        invalid = (
            None,
            "",
            " ",
            "24:00",
            "2400",
            "12:60",
            "1260",
            "0pm",
            "13am",
            "7:",
            "noon",
        )
        for typed in invalid:
            with self.subTest(typed=typed):
                self.assertIsNone(parse_typed_time(typed))
                self.assertIsNone(time_to_minutes(typed))

    def test_time_to_minutes_uses_normalized_clock(self) -> None:
        self.assertEqual(0, time_to_minutes("12 a.m."))
        self.assertEqual(307, time_to_minutes("5:07"))
        self.assertEqual(1027, time_to_minutes("5:07pm"))
        self.assertEqual(1439, time_to_minutes("2359"))


class OffsetTests(unittest.TestCase):
    def test_cross_midnight_offsets_are_monotonic_through_shift_end(self) -> None:
        cases = {
            "15:30": 0,
            "23:00": 450,
            "00:00": 510,
            "1am": 570,
            "02:00": 630,
        }
        for clock, expected in cases.items():
            with self.subTest(clock=clock):
                self.assertEqual(
                    expected, shift_clock_offset(clock, OVERNIGHT_SHIFT)
                )

    def test_invalid_clock_has_no_offset(self) -> None:
        self.assertIsNone(shift_clock_offset("25:00", OVERNIGHT_SHIFT))

    def test_offset_datetime_rolls_into_next_calendar_day(self) -> None:
        self.assertEqual(
            datetime(2026, 7, 29, 15, 30),
            offset_datetime("2026-07-29", OVERNIGHT_SHIFT, 0),
        )
        self.assertEqual(
            datetime(2026, 7, 30, 1, 0),
            offset_datetime(date(2026, 7, 29), OVERNIGHT_SHIFT, 570),
        )


class AvailabilityWindowTests(unittest.TestCase):
    def test_full_overnight_shift_without_meal_is_one_window(self) -> None:
        windows, error = build_availability_windows(
            {
                "startTime": "15:30",
                "endTime": "02:00",
                "mealEnabled": False,
            },
            OVERNIGHT_SHIFT,
            MEAL,
        )
        self.assertEqual([{"start": 0.0, "end": 630.0}], windows)
        self.assertEqual("", error)

    def test_meal_splits_an_overnight_availability_window(self) -> None:
        windows, error = build_availability_windows(
            {
                "startTime": "16:00",
                "endTime": "01:00",
                "mealEnabled": True,
                "mealStartTime": "20:55",
            },
            OVERNIGHT_SHIFT,
            MEAL,
        )
        self.assertEqual(
            [
                {"start": 30.0, "end": 325.0},
                {"start": 365.0, "end": 570.0},
            ],
            windows,
        )
        self.assertEqual("", error)

    def test_custom_meal_duration_controls_the_second_window(self) -> None:
        windows, error = build_availability_windows(
            {
                "startTime": "05:00",
                "endTime": "15:30",
                "mealEnabled": True,
                "mealStartTime": "11:10",
            },
            DAY_SHIFT,
            {"startTime": "11:10", "durationMinutes": 30},
        )
        self.assertEqual(
            [
                {"start": 0.0, "end": 370.0},
                {"start": 400.0, "end": 630.0},
            ],
            windows,
        )
        self.assertEqual("", error)

    def test_invalid_work_times_are_rejected(self) -> None:
        cases = (
            (
                {"startTime": "", "endTime": "02:00"},
                "Enter valid work start and end times.",
            ),
            (
                {"startTime": "15:29", "endTime": "02:00"},
                "Work times must stay within 3:30 PM-2:00 AM.",
            ),
            (
                {"startTime": "16:00", "endTime": "15:45"},
                "Work times must stay within 3:30 PM-2:00 AM.",
            ),
            (
                {"startTime": "15:30", "endTime": "02:01"},
                "Work times must stay within 3:30 PM-2:00 AM.",
            ),
        )
        for partial_record, expected_error in cases:
            record = {"mealEnabled": False, **partial_record}
            with self.subTest(record=record):
                windows, error = build_availability_windows(
                    record, OVERNIGHT_SHIFT, MEAL
                )
                self.assertEqual([], windows)
                self.assertEqual(expected_error, error)

    def test_invalid_meal_times_are_rejected(self) -> None:
        cases = (
            (
                "",
                "Enter a valid meal start time inside the shift.",
            ),
            (
                "15:45",
                "The full 40-minute meal must fit inside the employee's working time.",
            ),
            (
                "00:30",
                "The full 40-minute meal must fit inside the employee's working time.",
            ),
            (
                "02:30",
                "Enter a valid meal start time inside the shift.",
            ),
        )
        for meal_start, expected_error in cases:
            with self.subTest(meal_start=meal_start):
                windows, error = build_availability_windows(
                    {
                        "startTime": "16:00",
                        "endTime": "01:00",
                        "mealEnabled": True,
                        "mealStartTime": meal_start,
                    },
                    OVERNIGHT_SHIFT,
                    MEAL,
                )
                self.assertEqual([], windows)
                self.assertEqual(expected_error, error)


class FormattingTests(unittest.TestCase):
    def test_clock_and_offset_formatting(self) -> None:
        self.assertEqual("12:00 AM", format_clock("0000"))
        self.assertEqual("12:00 PM", format_clock("12pm"))
        self.assertEqual("5:07 PM", format_clock("17:07"))
        self.assertEqual("Time needed", format_clock(""))
        self.assertEqual("not-a-time", format_clock("not-a-time"))
        self.assertEqual("3:30 PM", format_offset("2026-07-29", OVERNIGHT_SHIFT, 0))
        self.assertEqual(
            "Jul 30, 1:00 AM",
            format_offset("2026-07-29", OVERNIGHT_SHIFT, 570),
        )
        self.assertEqual("-", format_offset("2026-07-29", OVERNIGHT_SHIFT, None))

    def test_duration_formatting_clamps_and_groups_units(self) -> None:
        cases = {
            None: "-",
            -5: "0m",
            0: "0m",
            45: "45m",
            60: "1h",
            90: "1h 30m",
            125: "2h 5m",
        }
        for minutes, expected in cases.items():
            with self.subTest(minutes=minutes):
                self.assertEqual(expected, format_duration(minutes))

    def test_clock_math_wraps_at_midnight(self) -> None:
        self.assertEqual("23:59", minutes_to_clock(-1))
        self.assertEqual("00:00", minutes_to_clock(1440))
        self.assertEqual("01:00", minutes_to_clock(1500))

    def test_safe_filename_removes_path_and_punctuation_characters(self) -> None:
        self.assertEqual(
            "Plan-2026-07-29.json",
            safe_filename(" Plan 2026/07/29.json "),
        )
        self.assertEqual("schedule", safe_filename("///"))
        self.assertEqual("plan", safe_filename("   ", fallback="plan"))


if __name__ == "__main__":
    unittest.main()
