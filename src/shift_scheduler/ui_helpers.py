from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any


def parse_typed_time(value: Any) -> str | None:
    """Normalize common 12/24-hour clock inputs to HH:MM."""
    text = str(value or "").strip().lower().replace(".", "")
    text = re.sub(r"\s+", "", text)
    if not text:
        return None

    suffix_match = re.search(r"(am|pm|a|p)$", text)
    suffix = suffix_match.group(1)[0] if suffix_match else ""
    clock_text = text[: suffix_match.start()] if suffix_match else text

    if re.fullmatch(r"\d{1,2}:\d{1,2}", clock_text):
        hour_text, minute_text = clock_text.split(":", maxsplit=1)
        hours, minutes = int(hour_text), int(minute_text)
    elif re.fullmatch(r"\d{1,2}", clock_text):
        hours, minutes = int(clock_text), 0
    elif re.fullmatch(r"\d{3,4}", clock_text):
        hours, minutes = int(clock_text[:-2]), int(clock_text[-2:])
    else:
        return None

    if not 0 <= minutes <= 59:
        return None
    if suffix:
        if not 1 <= hours <= 12:
            return None
        hours %= 12
        if suffix == "p":
            hours += 12
    elif not 0 <= hours <= 23:
        return None

    return f"{hours:02d}:{minutes:02d}"


def time_to_minutes(value: Any) -> int | None:
    normalized = parse_typed_time(value)
    if normalized is None:
        return None
    hours, minutes = (int(piece) for piece in normalized.split(":"))
    return hours * 60 + minutes


def minutes_to_clock(minutes: float) -> str:
    clean = round(minutes) % 1440
    return f"{clean // 60:02d}:{clean % 60:02d}"


def format_clock(value: Any) -> str:
    minutes = time_to_minutes(value)
    if minutes is None:
        return str(value or "").strip() or "Time needed"
    hour = minutes // 60
    minute = minutes % 60
    suffix = "PM" if hour >= 12 else "AM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def shift_clock_offset(value: Any, shift: dict[str, Any]) -> int | None:
    minutes = time_to_minutes(value)
    if minutes is None:
        return None
    return (minutes - int(shift["startMinutes"]) + 1440) % 1440


def build_availability_windows(
    record: dict[str, Any],
    shift: dict[str, Any],
    default_break: dict[str, Any] | None,
) -> tuple[list[dict[str, float]], str]:
    """Return schedulable windows and a user-facing validation error."""
    shift_length = float(shift["availableHours"]) * 60
    start = shift_clock_offset(record.get("startTime"), shift)
    end = shift_clock_offset(record.get("endTime"), shift)
    if start is None or end is None:
        return [], "Enter valid work start and end times."
    if start < 0 or start >= shift_length or end <= start or end > shift_length:
        return (
            [],
            f"Work times must stay within {format_clock(shift['startTime'])}"
            f"-{format_clock(shift['endTime'])}.",
        )

    windows: list[dict[str, float]] = [{"start": float(start), "end": float(end)}]
    if not record.get("mealEnabled"):
        return windows, ""

    meal_start = shift_clock_offset(record.get("mealStartTime"), shift)
    duration = int((default_break or {}).get("durationMinutes", 40))
    if meal_start is None or meal_start >= shift_length:
        return [], "Enter a valid meal start time inside the shift."
    meal_end = meal_start + duration
    if meal_start < start or meal_end > end:
        return [], "The full 40-minute meal must fit inside the employee's working time."

    windows = [
        {"start": float(start), "end": float(meal_start)},
        {"start": float(meal_end), "end": float(end)},
    ]
    return [window for window in windows if window["end"] > window["start"]], ""


def offset_datetime(
    production_date: date | str,
    shift: dict[str, Any],
    offset_minutes: float,
) -> datetime:
    if isinstance(production_date, str):
        production_date = date.fromisoformat(production_date)
    midnight = datetime.combine(production_date, time.min)
    return midnight + timedelta(
        minutes=float(shift["startMinutes"]) + float(offset_minutes)
    )


def format_offset(
    production_date: date | str,
    shift: dict[str, Any],
    offset_minutes: float | None,
) -> str:
    if offset_minutes is None:
        return "-"
    moment = offset_datetime(production_date, shift, offset_minutes)
    if isinstance(production_date, str):
        production_date = date.fromisoformat(production_date)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    if moment.date() == production_date:
        return clock
    day = moment.strftime("%b %d").replace(" 0", " ")
    return f"{day}, {clock}"


def format_duration(minutes: float | None) -> str:
    if minutes is None:
        return "-"
    total = max(0, round(minutes))
    hours, remainder = divmod(total, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def safe_filename(value: str, fallback: str = "schedule") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return cleaned or fallback
