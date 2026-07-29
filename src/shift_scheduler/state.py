"""State, persistence, and plan snapshot helpers for the shift scheduler.

The functions in this module deliberately operate on plain dictionaries.  That
keeps saved files human-readable and makes the state layer usable from
Streamlit, command-line tools, and tests without framework dependencies.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, MutableMapping


STATE_VERSION = 2
BASELINE_SCHEMA_VERSION = 2
PLAN_SNAPSHOT_SCHEMA = "manufacturing-shift-scheduler-plan"
PLAN_SNAPSHOT_VERSION = 1
STATE_DIRECTORY_ENV = "SHIFT_SCHEDULER_STATE_DIR"
DEFAULT_STATE_DIRECTORY = ".manufacturing_shift_scheduler"
DEFAULT_STATE_FILENAME = "state.json"
MAX_SAVED_JOBS = 50

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAN_KEY_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\|([1-9]\d*)$")
_CLOCK_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_MAX_HOURS = 24.0


class StateValidationError(ValueError):
    """Raised when a state, baseline, overlay, or snapshot is invalid."""


def deep_clone(value: Any) -> Any:
    """Return a recursive clone without requiring JSON serialization."""

    return deepcopy(value)


def normalized_name(value: Any) -> str:
    """Return the canonical representation used for name uniqueness."""

    return " ".join(str(value or "").strip().split()).casefold()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _ui_theme(value: Any) -> str:
    theme = str(value or "light").strip().casefold()
    if theme not in {"light", "dark"}:
        raise StateValidationError("uiTheme must be light or dark.")
    return theme


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StateValidationError(f"{label} must be an object.")
    return {str(key): deep_clone(item) for key, item in value.items()}


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise StateValidationError(f"{label} must be an array.")
    return deep_clone(value)


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise StateValidationError(f"{label} must be a whole number.")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise StateValidationError(f"{label} must be a whole number.") from error
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = float(number)
    if not math.isfinite(numeric_value) or numeric_value != number:
        raise StateValidationError(f"{label} must be a whole number.")
    if minimum is not None and number < minimum:
        raise StateValidationError(f"{label} must be at least {minimum}.")
    return number


def _number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise StateValidationError(f"{label} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise StateValidationError(f"{label} must be a number.") from error
    if not math.isfinite(number):
        raise StateValidationError(f"{label} must be finite.")
    if minimum is not None and number < minimum:
        raise StateValidationError(f"{label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise StateValidationError(f"{label} must be no more than {maximum}.")
    return number


def _optional_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None or value == "":
        return None
    return _number(value, label, minimum=minimum, maximum=maximum)


def _boolean(value: Any, label: str, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        clean = value.strip().casefold()
        if clean in {"yes", "y", "true", "1"}:
            return True
        if clean in {"no", "n", "false", "0"}:
            return False
    raise StateValidationError(f"{label} must be true or false.")


def _valid_date(value: Any, label: str = "productionDate") -> str:
    clean = str(value or "").strip()
    if not _DATE_PATTERN.fullmatch(clean):
        raise StateValidationError(f"{label} must use YYYY-MM-DD.")
    try:
        date.fromisoformat(clean)
    except ValueError as error:
        raise StateValidationError(f"{label} is not a valid calendar date.") from error
    return clean


def _clock(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not _CLOCK_PATTERN.fullmatch(clean):
        raise StateValidationError(f"{label} must use 24-hour HH:MM.")
    return clean


def _clock_to_minutes(value: str) -> int:
    hours, minutes = value.split(":", 1)
    return int(hours) * 60 + int(minutes)


def minutes_to_clock(minutes: float) -> str:
    clean = int(round(minutes)) % 1440
    return f"{clean // 60:02d}:{clean % 60:02d}"


def deterministic_operator_key(
    operator: Mapping[str, Any],
    *,
    fallback_index: int | None = None,
) -> str:
    """Build a stable operator key from public identity fields.

    Employee IDs are preferred because names can be edited.  A row fallback is
    available only for normalization of incomplete legacy data.
    """

    employee_id = _clean_text(operator.get("employeeId"))
    if employee_id:
        return f"id:{employee_id}"
    name = normalized_name(operator.get("name"))
    if name:
        return f"name:{name}"
    if fallback_index is not None:
        return f"row:{fallback_index + 1}"
    raise StateValidationError("An operator needs an Employee ID or name.")


def operator_key(
    operator: Mapping[str, Any],
    *,
    fallback_index: int | None = None,
) -> str:
    """Return an existing scheduler key or create a deterministic one."""

    saved_key = str(operator.get("schedulerKey") or "").strip()
    return saved_key or deterministic_operator_key(
        operator, fallback_index=fallback_index
    )


def _normalize_shift(raw_shift: Any, index: int) -> dict[str, Any]:
    shift = _mapping(raw_shift, f"settings.shifts[{index}]")
    shift_id = _integer(shift.get("id"), f"settings.shifts[{index}].id", minimum=1)
    start_time = _clock(
        shift.get("startTime"), f"settings.shifts[{index}].startTime"
    )
    end_time = _clock(shift.get("endTime"), f"settings.shifts[{index}].endTime")
    available_hours = _number(
        shift.get("availableHours"),
        f"settings.shifts[{index}].availableHours",
        minimum=0,
        maximum=_MAX_HOURS,
    )
    start_minutes = _integer(
        shift.get("startMinutes", _clock_to_minutes(start_time)),
        f"settings.shifts[{index}].startMinutes",
        minimum=0,
    )
    end_minutes = _integer(
        shift.get("endMinutes", _clock_to_minutes(end_time)),
        f"settings.shifts[{index}].endMinutes",
        minimum=0,
    )
    if start_minutes >= 1440 or end_minutes >= 1440:
        raise StateValidationError("Shift minute values must be between 0 and 1439.")
    shift.update(
        {
            "id": shift_id,
            "startMinutes": start_minutes,
            "endMinutes": end_minutes,
            "startTime": start_time,
            "endTime": end_time,
            "availableHours": available_hours,
            "crossesMidnight": _boolean(
                shift.get("crossesMidnight"),
                f"settings.shifts[{index}].crossesMidnight",
                default=end_minutes <= start_minutes,
            ),
        }
    )
    return shift


def _normalize_breaks(raw_breaks: Any) -> dict[str, dict[str, Any]]:
    if raw_breaks is None:
        return {}
    breaks = _mapping(raw_breaks, "settings.defaultBreaks")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_shift_id, raw_break in breaks.items():
        shift_id = str(_integer(raw_shift_id, "break shift ID", minimum=1))
        record = _mapping(raw_break, f"default break for shift {shift_id}")
        record["label"] = _clean_text(record.get("label")) or "Meal"
        record["startTime"] = _clock(
            record.get("startTime"), f"default break for shift {shift_id}.startTime"
        )
        record["durationMinutes"] = _integer(
            record.get("durationMinutes"),
            f"default break for shift {shift_id}.durationMinutes",
            minimum=0,
        )
        normalized[shift_id] = record
    return normalized


def _normalize_skills(
    raw_skills: Any,
    skill_names: list[str],
    *,
    label: str,
) -> dict[str, int]:
    skills = _mapping(raw_skills or {}, label)
    normalized: dict[str, int] = {}
    for skill_name in [*skill_names, *skills.keys()]:
        clean_name = _clean_text(skill_name)
        if not clean_name or clean_name in normalized:
            continue
        value = skills.get(skill_name, skills.get(clean_name, 0))
        level = _integer(value, f"{label}.{clean_name}", minimum=0)
        if level > 3:
            raise StateValidationError(
                f"{label}.{clean_name} must be a whole number from 0 to 3."
            )
        normalized[clean_name] = level
    return normalized


def _normalize_operator(
    raw_operator: Any,
    index: int,
    skill_names: list[str],
    shifts_by_id: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    operator = _mapping(raw_operator, f"operators[{index}]")
    name = _clean_text(operator.get("name"))
    if not name:
        raise StateValidationError(f"operators[{index}].name is required.")
    employee_id = _clean_text(operator.get("employeeId")) or None
    default_shift = _integer(
        operator.get("defaultShift", next(iter(shifts_by_id))),
        f"operators[{index}].defaultShift",
        minimum=1,
    )
    if default_shift not in shifts_by_id:
        raise StateValidationError(
            f"operators[{index}].defaultShift does not match a configured shift."
        )
    source_shift_raw = operator.get("sourceShift")
    source_shift = (
        None
        if source_shift_raw is None or source_shift_raw == ""
        else _integer(source_shift_raw, f"operators[{index}].sourceShift", minimum=1)
    )
    hours_override = _optional_number(
        operator.get("defaultHoursOverride"),
        f"operators[{index}].defaultHoursOverride",
        minimum=0,
        maximum=_MAX_HOURS,
    )
    if "defaultAvailableHours" in operator:
        available_hours = _number(
            operator["defaultAvailableHours"],
            f"operators[{index}].defaultAvailableHours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    elif hours_override is not None:
        available_hours = hours_override
    else:
        available_hours = float(shifts_by_id[default_shift]["availableHours"])
    operator.update(
        {
            "schedulerKey": operator_key(operator, fallback_index=index),
            "employeeId": employee_id,
            "name": name,
            "sourceShift": source_shift,
            "defaultShift": default_shift,
            "defaultScheduled": _boolean(
                operator.get("defaultScheduled"),
                f"operators[{index}].defaultScheduled",
                default=True,
            ),
            "defaultPresent": _boolean(
                operator.get("defaultPresent"),
                f"operators[{index}].defaultPresent",
                default=True,
            ),
            "defaultHoursOverride": hours_override,
            "defaultAvailableHours": available_hours,
            "sourceHours": str(operator.get("sourceHours") or ""),
            "sourceDays": str(operator.get("sourceDays") or ""),
            "skills": _normalize_skills(
                operator.get("skills"),
                skill_names,
                label=f"operators[{index}].skills",
            ),
        }
    )
    return operator


def normalize_baseline(source: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-clone and normalize scheduler master data.

    Baseline schema 1 is accepted as legacy input and upgraded in memory.
    Future schema versions are rejected instead of being interpreted loosely.
    """

    baseline = _mapping(source, "baseline")
    schema_version = _integer(
        baseline.get("schemaVersion", 1), "baseline.schemaVersion", minimum=1
    )
    if schema_version > BASELINE_SCHEMA_VERSION:
        raise StateValidationError(
            f"Unsupported baseline schema version {schema_version}."
        )
    baseline["schemaVersion"] = BASELINE_SCHEMA_VERSION

    settings = _mapping(baseline.get("settings"), "baseline.settings")
    raw_shifts = _list(settings.get("shifts"), "baseline.settings.shifts")
    if not raw_shifts:
        raise StateValidationError("baseline.settings.shifts cannot be empty.")
    shifts = [_normalize_shift(raw_shift, index) for index, raw_shift in enumerate(raw_shifts)]
    shifts_by_id = {shift["id"]: shift for shift in shifts}
    if len(shifts_by_id) != len(shifts):
        raise StateValidationError("Shift IDs must be unique.")
    settings["shifts"] = shifts
    settings["defaultBreaks"] = _normalize_breaks(settings.get("defaultBreaks"))
    baseline["settings"] = settings

    raw_skill_names = _list(baseline.get("skillNames", []), "baseline.skillNames")
    skill_names: list[str] = []
    seen_skills: set[str] = set()
    for index, raw_name in enumerate(raw_skill_names):
        name = _clean_text(raw_name)
        if not name:
            raise StateValidationError(f"skillNames[{index}] cannot be blank.")
        folded = name.casefold()
        if folded in seen_skills:
            raise StateValidationError(f'Duplicate skill name "{name}".')
        seen_skills.add(folded)
        skill_names.append(name)
    baseline["skillNames"] = skill_names

    raw_operators = _list(baseline.get("operators", []), "baseline.operators")
    operators = [
        _normalize_operator(operator, index, skill_names, shifts_by_id)
        for index, operator in enumerate(raw_operators)
    ]
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for operator in operators:
        key = operator["schedulerKey"]
        employee_id = str(operator.get("employeeId") or "").casefold()
        name = normalized_name(operator["name"])
        if key in seen_keys:
            raise StateValidationError(f'Duplicate operator key "{key}".')
        if employee_id and employee_id in seen_ids:
            raise StateValidationError(
                f'Duplicate Employee ID "{operator["employeeId"]}".'
            )
        if name in seen_names:
            raise StateValidationError(f'Duplicate operator name "{operator["name"]}".')
        seen_keys.add(key)
        if employee_id:
            seen_ids.add(employee_id)
        seen_names.add(name)
    baseline["operators"] = operators

    for field_name in ("routes", "parentChild"):
        records = _list(baseline.get(field_name, []), f"baseline.{field_name}")
        if any(not isinstance(record, Mapping) for record in records):
            raise StateValidationError(f"baseline.{field_name} entries must be objects.")
        baseline[field_name] = records
    source_record = baseline.get("source", {})
    baseline["source"] = _mapping(source_record, "baseline.source")
    return baseline


def validate_baseline(source: Mapping[str, Any]) -> None:
    """Validate a baseline, raising :class:`StateValidationError` on failure."""

    normalize_baseline(source)


def empty_employee_overlay() -> dict[str, Any]:
    return {"overrides": {}, "additions": []}


def normalize_employee_overlay(value: Any) -> dict[str, Any]:
    if value is None:
        return empty_employee_overlay()
    overlay = _mapping(value, "employeeOverlay")
    raw_overrides = _mapping(overlay.get("overrides", {}), "employeeOverlay.overrides")
    overrides: dict[str, dict[str, Any]] = {}
    for raw_key, raw_override in raw_overrides.items():
        key = str(raw_key).strip()
        if not key:
            raise StateValidationError("Employee override keys cannot be blank.")
        overrides[key] = _mapping(
            raw_override, f"employeeOverlay.overrides.{key}"
        )
    raw_additions = _list(
        overlay.get("additions", []), "employeeOverlay.additions"
    )
    additions = [
        _mapping(addition, f"employeeOverlay.additions[{index}]")
        for index, addition in enumerate(raw_additions)
    ]
    return {"overrides": overrides, "additions": additions}


def overlay_has_changes(overlay: Any) -> bool:
    normalized = normalize_employee_overlay(overlay)
    return bool(normalized["overrides"] or normalized["additions"])


def compose_baseline(
    factory_baseline: Mapping[str, Any],
    imported_baseline: Mapping[str, Any] | None = None,
    employee_overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the active master data without mutating any input."""

    baseline = normalize_baseline(
        imported_baseline if imported_baseline is not None else factory_baseline
    )
    overlay = normalize_employee_overlay(employee_overlay)
    by_key = {
        operator_key(operator): operator for operator in baseline["operators"]
    }
    for key, override in overlay["overrides"].items():
        operator = by_key.get(key)
        if operator is None:
            # An override can become stale after importing a different workbook.
            continue
        merged = {**operator, **deep_clone(override), "schedulerKey": key}
        merged["skills"] = {
            **operator.get("skills", {}),
            **deep_clone(override.get("skills", {})),
        }
        operator.clear()
        operator.update(merged)
    for index, addition in enumerate(overlay["additions"]):
        candidate = deep_clone(addition)
        candidate["schedulerKey"] = operator_key(
            candidate, fallback_index=len(baseline["operators"]) + index
        )
        key = candidate["schedulerKey"]
        if key in by_key:
            raise StateValidationError(
                f'Employee addition uses duplicate operator key "{key}".'
            )
        baseline["operators"].append(candidate)
        by_key[key] = candidate
    return normalize_baseline(baseline)


def _employee_candidate(
    active_baseline: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    existing_key: str | None,
    require_employee_id: bool,
) -> dict[str, Any]:
    data = normalize_baseline(active_baseline)
    operators_by_key = {
        operator_key(operator): operator for operator in data["operators"]
    }
    existing = operators_by_key.get(existing_key) if existing_key else None
    if existing_key and existing is None:
        raise StateValidationError(f'Unknown operator key "{existing_key}".')
    incoming = _mapping(values, "employee")
    merged = {**(existing or {}), **incoming}
    if existing and "skills" in incoming:
        merged["skills"] = {
            **deep_clone(existing.get("skills", {})),
            **_mapping(incoming.get("skills"), "employee.skills"),
        }
    name = _clean_text(merged.get("name"))
    employee_id = _clean_text(merged.get("employeeId")) or None
    if require_employee_id and not employee_id:
        raise StateValidationError("A unique Employee ID is required.")
    if not name:
        raise StateValidationError("Employee name is required.")

    shifts_by_id = {
        int(shift["id"]): shift for shift in data["settings"]["shifts"]
    }
    default_shift = _integer(
        merged.get("defaultShift", next(iter(shifts_by_id))),
        "employee.defaultShift",
        minimum=1,
    )
    if default_shift not in shifts_by_id:
        raise StateValidationError("Employee default shift is not configured.")

    for key, operator in operators_by_key.items():
        if key == existing_key:
            continue
        same_employee_id = (
            employee_id
            and str(operator.get("employeeId") or "").casefold()
            == employee_id.casefold()
        )
        if same_employee_id:
            raise StateValidationError(f'Employee ID "{employee_id}" is already in use.')
        if normalized_name(operator.get("name")) == normalized_name(name):
            raise StateValidationError(f'Employee name "{name}" is already in use.')

    skills = _normalize_skills(
        merged.get("skills"),
        data["skillNames"],
        label="employee.skills",
    )
    hours_override = _optional_number(
        merged.get("defaultHoursOverride"),
        "employee.defaultHoursOverride",
        minimum=0,
        maximum=_MAX_HOURS,
    )
    if "defaultAvailableHours" in incoming:
        available_hours = _number(
            incoming["defaultAvailableHours"],
            "employee.defaultAvailableHours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    elif hours_override is not None:
        available_hours = hours_override
    elif (
        existing
        and int(existing.get("defaultShift", default_shift)) == default_shift
        and "defaultAvailableHours" in existing
    ):
        available_hours = _number(
            existing["defaultAvailableHours"],
            "employee.defaultAvailableHours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    else:
        available_hours = float(shifts_by_id[default_shift]["availableHours"])
    scheduler_key = (
        existing_key
        if existing_key
        else deterministic_operator_key({"employeeId": employee_id, "name": name})
    )
    candidate = {
        **merged,
        "schedulerKey": scheduler_key,
        "employeeId": employee_id,
        "name": name,
        "sourceShift": merged.get("sourceShift", default_shift),
        "defaultShift": default_shift,
        "defaultScheduled": _boolean(
            merged.get("defaultScheduled"),
            "employee.defaultScheduled",
            default=True,
        ),
        "defaultPresent": _boolean(
            merged.get("defaultPresent"),
            "employee.defaultPresent",
            default=True,
        ),
        "defaultHoursOverride": hours_override,
        "defaultAvailableHours": available_hours,
        "sourceHours": str(merged.get("sourceHours") or ""),
        "sourceDays": str(merged.get("sourceDays") or ""),
        "skills": skills,
    }
    if not existing_key:
        candidate["localOnly"] = True
    return candidate


def validate_employee_addition(
    factory_baseline: Mapping[str, Any],
    employee_overlay: Mapping[str, Any] | None,
    employee: Mapping[str, Any],
    *,
    imported_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active = compose_baseline(
        factory_baseline, imported_baseline, employee_overlay
    )
    return _employee_candidate(
        active, employee, existing_key=None, require_employee_id=True
    )


def validate_employee_edit(
    factory_baseline: Mapping[str, Any],
    employee_overlay: Mapping[str, Any] | None,
    key: str,
    changes: Mapping[str, Any],
    *,
    imported_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active = compose_baseline(
        factory_baseline, imported_baseline, employee_overlay
    )
    return _employee_candidate(
        active,
        changes,
        existing_key=str(key),
        require_employee_id=False,
    )


def add_employee_to_overlay(
    factory_baseline: Mapping[str, Any],
    employee_overlay: Mapping[str, Any] | None,
    employee: Mapping[str, Any],
    *,
    imported_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    overlay = normalize_employee_overlay(employee_overlay)
    candidate = validate_employee_addition(
        factory_baseline,
        overlay,
        employee,
        imported_baseline=imported_baseline,
    )
    overlay["additions"].append(candidate)
    return overlay


def edit_employee_overlay(
    factory_baseline: Mapping[str, Any],
    employee_overlay: Mapping[str, Any] | None,
    key: str,
    changes: Mapping[str, Any],
    *,
    imported_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    overlay = normalize_employee_overlay(employee_overlay)
    candidate = validate_employee_edit(
        factory_baseline,
        overlay,
        key,
        changes,
        imported_baseline=imported_baseline,
    )
    addition_index = next(
        (
            index
            for index, addition in enumerate(overlay["additions"])
            if operator_key(addition) == key
        ),
        None,
    )
    if addition_index is not None:
        overlay["additions"][addition_index] = candidate
    else:
        overlay["overrides"][str(key)] = {
            field_name: deep_clone(field_value)
            for field_name, field_value in candidate.items()
            if field_name not in {"schedulerKey", "localOnly"}
        }
    return overlay


def attendance_plan_key(production_date: Any, shift_id: Any) -> str:
    return f"{_valid_date(production_date)}|{_integer(shift_id, 'shiftId', minimum=1)}"


def parse_attendance_plan_key(value: Any) -> tuple[str, int]:
    clean = str(value or "").strip()
    match = _PLAN_KEY_PATTERN.fullmatch(clean)
    if not match:
        raise StateValidationError(
            "Attendance keys must use YYYY-MM-DD|shift."
        )
    return _valid_date(match.group(1)), int(match.group(2))


def _shift_by_id(
    baseline: Mapping[str, Any], shift_id: Any
) -> dict[str, Any]:
    wanted = _integer(shift_id, "shiftId", minimum=1)
    for shift in baseline["settings"]["shifts"]:
        if int(shift["id"]) == wanted:
            return shift
    raise StateValidationError(f"Shift {wanted} is not configured.")


def default_attendance_record(
    operator: Mapping[str, Any],
    shift: Mapping[str, Any],
    *,
    default_break: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create attendance while honoring every imported attendance default."""

    shift_id = int(shift["id"])
    on_roster = int(operator.get("defaultShift", shift_id)) == shift_id
    scheduled = on_roster and _boolean(
        operator.get("defaultScheduled"),
        "operator.defaultScheduled",
        default=True,
    )
    present = scheduled and _boolean(
        operator.get("defaultPresent"),
        "operator.defaultPresent",
        default=True,
    )
    hours_override = _optional_number(
        operator.get("defaultHoursOverride"),
        "operator.defaultHoursOverride",
        minimum=0,
        maximum=_MAX_HOURS,
    )
    if hours_override is not None:
        available_hours = hours_override
    elif "defaultAvailableHours" in operator:
        available_hours = _number(
            operator["defaultAvailableHours"],
            "operator.defaultAvailableHours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    else:
        available_hours = _number(
            shift["availableHours"],
            "shift.availableHours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    start_time = _clock(shift["startTime"], "shift.startTime")
    if (
        hours_override is None
        and math.isclose(
            available_hours, float(shift["availableHours"]), abs_tol=1e-9
        )
    ):
        end_time = _clock(shift["endTime"], "shift.endTime")
    else:
        end_time = minutes_to_clock(
            _clock_to_minutes(start_time) + available_hours * 60
        )
    meal = _mapping(default_break, "default break") if default_break else None
    return {
        "scheduled": scheduled,
        "present": present,
        "startTime": start_time,
        "endTime": end_time,
        "availableHours": available_hours,
        "hoursOverride": hours_override,
        "mealEnabled": bool(meal and available_hours > 0),
        "mealStartTime": str(meal.get("startTime") if meal else ""),
    }


def default_attendance_for_shift(
    baseline: Mapping[str, Any], shift_id: Any
) -> dict[str, dict[str, Any]]:
    data = normalize_baseline(baseline)
    shift = _shift_by_id(data, shift_id)
    default_break = data["settings"].get("defaultBreaks", {}).get(
        str(shift["id"])
    )
    return {
        operator_key(operator): default_attendance_record(
            operator, shift, default_break=default_break
        )
        for operator in data["operators"]
    }


def default_attendance_by_plan(
    baseline: Mapping[str, Any],
    production_date: Any,
    shift_id: Any,
) -> dict[str, dict[str, dict[str, Any]]]:
    key = attendance_plan_key(production_date, shift_id)
    return {key: default_attendance_for_shift(baseline, shift_id)}


def _normalize_attendance_record(value: Any) -> dict[str, Any]:
    record = _mapping(value, "attendance record")
    normalized = deep_clone(record)
    if "scheduled" in record:
        normalized["scheduled"] = _boolean(
            record.get("scheduled"), "attendance.scheduled", default=True
        )
    normalized["present"] = _boolean(
        record.get("present"), "attendance.present", default=False
    )
    if record.get("startTime"):
        normalized["startTime"] = _clock(
            record["startTime"], "attendance.startTime"
        )
    if record.get("endTime"):
        normalized["endTime"] = _clock(record["endTime"], "attendance.endTime")
    if "availableHours" in record:
        normalized["availableHours"] = _number(
            record["availableHours"],
            "attendance.availableHours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    if "hoursOverride" in record:
        normalized["hoursOverride"] = _optional_number(
            record["hoursOverride"],
            "attendance.hoursOverride",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    if "hours" in record:
        normalized["hours"] = _number(
            record["hours"],
            "attendance.hours",
            minimum=0,
            maximum=_MAX_HOURS,
        )
    if "mealEnabled" in record:
        normalized["mealEnabled"] = _boolean(
            record["mealEnabled"], "attendance.mealEnabled", default=False
        )
    if record.get("mealStartTime"):
        normalized["mealStartTime"] = _clock(
            record["mealStartTime"], "attendance.mealStartTime"
        )
    else:
        normalized["mealStartTime"] = ""
    return normalized


def _normalize_attendance_by_plan(value: Any) -> dict[str, dict[str, Any]]:
    plans = _mapping(value or {}, "attendanceByPlan")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_plan_key, raw_records in plans.items():
        production_date, shift_id = parse_attendance_plan_key(raw_plan_key)
        plan_key = attendance_plan_key(production_date, shift_id)
        records = _mapping(raw_records, f"attendanceByPlan.{plan_key}")
        normalized[plan_key] = {
            str(key): _normalize_attendance_record(record)
            for key, record in records.items()
        }
    return normalized


def ensure_attendance(
    state: MutableMapping[str, Any],
    baseline: Mapping[str, Any],
    production_date: Any | None = None,
    shift_id: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Ensure defaults exist for one plan and return its attendance records.

    The passed state is updated in place to mirror Streamlit session-state use.
    Existing user edits win; defaults only fill missing fields and operators.
    """

    data = normalize_baseline(baseline)
    requested_date = production_date or state.get("productionDate")
    requested_shift = shift_id if shift_id is not None else state.get("shiftId")
    key = attendance_plan_key(requested_date, requested_shift)
    plans = _normalize_attendance_by_plan(state.get("attendanceByPlan", {}))
    defaults = default_attendance_for_shift(data, requested_shift)
    existing = plans.get(key, {})
    merged: dict[str, dict[str, Any]] = {}
    for op_key, default_record in defaults.items():
        current = existing.get(op_key)
        if current is None:
            merged[op_key] = default_record
            continue
        current = _normalize_attendance_record(current)
        legacy_hours = current.get("hours")
        if not current.get("startTime") or not current.get("endTime"):
            migrated = deep_clone(default_record)
            if legacy_hours is not None:
                migrated["availableHours"] = legacy_hours
                migrated["hoursOverride"] = legacy_hours
                migrated["endTime"] = minutes_to_clock(
                    _clock_to_minutes(default_record["startTime"])
                    + legacy_hours * 60
                )
            migrated.update(current)
            current = migrated
        merged[op_key] = {**default_record, **current}
    for op_key, current in existing.items():
        if op_key not in merged:
            merged[op_key] = _normalize_attendance_record(current)
    plans[key] = merged
    state["attendanceByPlan"] = plans
    return merged


def _normalize_job(raw_job: Any, index: int) -> dict[str, Any]:
    job = _mapping(raw_job, f"jobs[{index}]")
    quantity = _integer(job.get("quantity", 1), f"jobs[{index}].quantity", minimum=1)
    priority = _integer(job.get("priority", 3), f"jobs[{index}].priority", minimum=1)
    return {
        **job,
        "uid": str(job.get("uid") or f"job-{index + 1}"),
        "id": str(job.get("id") or ""),
        "part": str(job.get("part") or "").strip(),
        "quantity": quantity,
        "priority": priority,
        "dueTime": str(job.get("dueTime") or ""),
        "materialReady": _boolean(
            job.get("materialReady"), f"jobs[{index}].materialReady", default=True
        ),
    }


def normalize_jobs(value: Any) -> list[dict[str, Any]]:
    jobs = _list(value or [], "jobs")
    if len(jobs) > MAX_SAVED_JOBS:
        raise StateValidationError(
            f"jobs must contain no more than {MAX_SAVED_JOBS} entries."
        )
    return [
        _normalize_job(job, index)
        for index, job in enumerate(jobs)
    ]


def new_state(
    *,
    production_date: str | None = None,
    shift_id: int = 1,
    jobs: list[Mapping[str, Any]] | None = None,
    ui_theme: str = "light",
) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "productionDate": _valid_date(production_date or date.today().isoformat()),
        "shiftId": _integer(shift_id, "shiftId", minimum=1),
        "uiTheme": _ui_theme(ui_theme),
        "attendanceByPlan": {},
        "jobs": normalize_jobs(jobs or []),
        "importedBaseline": None,
        "importedMeta": None,
        "employeeOverlay": empty_employee_overlay(),
        "result": None,
    }


def normalize_state(value: Mapping[str, Any]) -> dict[str, Any]:
    state = _mapping(value, "state")
    version = _integer(state.get("version"), "state.version", minimum=1)
    if version != STATE_VERSION:
        raise StateValidationError(f"Unsupported state version {version}.")
    imported_baseline = state.get("importedBaseline")
    imported_meta = state.get("importedMeta")
    if imported_meta is not None and not isinstance(imported_meta, Mapping):
        raise StateValidationError("state.importedMeta must be an object or null.")
    return {
        "version": STATE_VERSION,
        "productionDate": _valid_date(state.get("productionDate")),
        "shiftId": _integer(state.get("shiftId"), "state.shiftId", minimum=1),
        "uiTheme": _ui_theme(state.get("uiTheme")),
        "attendanceByPlan": _normalize_attendance_by_plan(
            state.get("attendanceByPlan", {})
        ),
        "jobs": normalize_jobs(state.get("jobs", [])),
        "importedBaseline": (
            normalize_baseline(imported_baseline)
            if imported_baseline is not None
            else None
        ),
        "importedMeta": deep_clone(dict(imported_meta)) if imported_meta else None,
        "employeeOverlay": normalize_employee_overlay(
            state.get("employeeOverlay")
        ),
        "result": deep_clone(state.get("result")),
    }


def validate_state(value: Mapping[str, Any]) -> None:
    normalize_state(value)


def resolve_state_directory(directory: os.PathLike[str] | str | None = None) -> Path:
    if directory is not None:
        return Path(directory).expanduser()
    configured = os.environ.get(STATE_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / DEFAULT_STATE_DIRECTORY


def resolve_state_path(
    path: os.PathLike[str] | str | None = None,
    *,
    directory: os.PathLike[str] | str | None = None,
) -> Path:
    if path is not None and directory is not None:
        raise ValueError("Pass either path or directory, not both.")
    return Path(path).expanduser() if path is not None else (
        resolve_state_directory(directory) / DEFAULT_STATE_FILENAME
    )


def _atomic_write_json(payload: Any, path: Path) -> Path:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def save_state(
    state: Mapping[str, Any],
    path: os.PathLike[str] | str | None = None,
    *,
    directory: os.PathLike[str] | str | None = None,
) -> Path:
    """Validate and atomically persist state."""

    target = resolve_state_path(path, directory=directory)
    return _atomic_write_json(normalize_state(state), target)


def load_state(
    path: os.PathLike[str] | str | None = None,
    *,
    directory: os.PathLike[str] | str | None = None,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load state, returning a clean fallback for missing or corrupt files."""

    clean_fallback = normalize_state(fallback) if fallback is not None else new_state()
    target = resolve_state_path(path, directory=directory)
    try:
        with target.open("r", encoding="utf-8") as handle:
            raw_state = json.load(handle)
        return normalize_state(raw_state)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        StateValidationError,
    ):
        return deep_clone(clean_fallback)


def import_baseline(
    state: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate an imported baseline while preserving current job rows."""

    updated = normalize_state(state)
    updated["importedBaseline"] = normalize_baseline(baseline)
    updated["importedMeta"] = deep_clone(dict(metadata)) if metadata else None
    updated["employeeOverlay"] = empty_employee_overlay()
    updated["attendanceByPlan"] = {}
    updated["result"] = None
    return updated


def reset_to_imported_baseline(state: Mapping[str, Any]) -> dict[str, Any]:
    """Discard local employee/attendance edits but retain jobs and the import."""

    updated = normalize_state(state)
    if updated["importedBaseline"] is None:
        raise StateValidationError("There is no imported baseline to restore.")
    updated["employeeOverlay"] = empty_employee_overlay()
    updated["attendanceByPlan"] = {}
    updated["result"] = None
    return updated


def reset_to_factory_baseline(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return to packaged master data while preserving current job rows."""

    updated = normalize_state(state)
    updated["importedBaseline"] = None
    updated["importedMeta"] = None
    updated["employeeOverlay"] = empty_employee_overlay()
    updated["attendanceByPlan"] = {}
    updated["result"] = None
    return updated


def clear_plan(state: Mapping[str, Any]) -> dict[str, Any]:
    """Clear plan inputs/results without changing master data or employee edits."""

    updated = normalize_state(state)
    updated["jobs"] = []
    updated["attendanceByPlan"] = {}
    updated["result"] = None
    return updated


def clear_result(state: Mapping[str, Any]) -> dict[str, Any]:
    updated = normalize_state(state)
    updated["result"] = None
    return updated


def build_plan_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    clean_state = normalize_state(state)
    plan_key = attendance_plan_key(
        clean_state["productionDate"], clean_state["shiftId"]
    )
    return {
        "schema": PLAN_SNAPSHOT_SCHEMA,
        "version": PLAN_SNAPSHOT_VERSION,
        "productionDate": clean_state["productionDate"],
        "shiftId": clean_state["shiftId"],
        "jobs": deep_clone(clean_state["jobs"]),
        "attendance": deep_clone(
            clean_state["attendanceByPlan"].get(plan_key, {})
        ),
    }


def export_plan_snapshot(
    state: Mapping[str, Any],
    *,
    indent: int | None = 2,
) -> str:
    return json.dumps(
        build_plan_snapshot(state),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
        allow_nan=False,
    )


def parse_plan_snapshot(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        snapshot = _mapping(payload, "plan snapshot")
    else:
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload).decode("utf-8")
            snapshot = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError, TypeError) as error:
            raise StateValidationError("Plan snapshot is not valid JSON.") from error
        snapshot = _mapping(snapshot, "plan snapshot")
    if snapshot.get("schema") != PLAN_SNAPSHOT_SCHEMA:
        raise StateValidationError("Unsupported plan snapshot schema.")
    version = _integer(
        snapshot.get("version"), "plan snapshot version", minimum=1
    )
    if version != PLAN_SNAPSHOT_VERSION:
        raise StateValidationError(
            f"Unsupported plan snapshot version {version}."
        )
    production_date = _valid_date(snapshot.get("productionDate"))
    shift_id = _integer(snapshot.get("shiftId"), "snapshot.shiftId", minimum=1)
    attendance = _mapping(snapshot.get("attendance", {}), "snapshot.attendance")
    return {
        "schema": PLAN_SNAPSHOT_SCHEMA,
        "version": PLAN_SNAPSHOT_VERSION,
        "productionDate": production_date,
        "shiftId": shift_id,
        "jobs": normalize_jobs(snapshot.get("jobs", [])),
        "attendance": {
            str(key): _normalize_attendance_record(record)
            for key, record in attendance.items()
        },
    }


def import_plan_snapshot(
    state: Mapping[str, Any],
    payload: str | bytes | bytearray | Mapping[str, Any],
    *,
    active_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace plan inputs from a snapshot while preserving master data."""

    updated = normalize_state(state)
    snapshot = parse_plan_snapshot(payload)
    if active_baseline is not None:
        _shift_by_id(normalize_baseline(active_baseline), snapshot["shiftId"])
    updated["productionDate"] = snapshot["productionDate"]
    updated["shiftId"] = snapshot["shiftId"]
    updated["jobs"] = snapshot["jobs"]
    plan_key = attendance_plan_key(
        snapshot["productionDate"], snapshot["shiftId"]
    )
    updated["attendanceByPlan"] = {plan_key: snapshot["attendance"]}
    updated["result"] = None
    if active_baseline is not None:
        ensure_attendance(
            updated,
            active_baseline,
            snapshot["productionDate"],
            snapshot["shiftId"],
        )
    return updated


def save_plan_snapshot(
    state: Mapping[str, Any], path: os.PathLike[str] | str
) -> Path:
    return _atomic_write_json(build_plan_snapshot(state), Path(path).expanduser())


def load_plan_snapshot(path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            return parse_plan_snapshot(json.load(handle))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StateValidationError("Plan snapshot could not be loaded.") from error


@dataclass(frozen=True)
class StateStore:
    """Small path-bound wrapper around atomic state persistence."""

    directory: os.PathLike[str] | str | None = None
    filename: str = DEFAULT_STATE_FILENAME

    @property
    def path(self) -> Path:
        return resolve_state_directory(self.directory) / self.filename

    def load(self, fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return load_state(self.path, fallback=fallback)

    def save(self, state: Mapping[str, Any]) -> Path:
        return save_state(state, self.path)


@dataclass
class SchedulerState:
    """Convenience wrapper for UI code that wants object-style operations."""

    factory_baseline: Mapping[str, Any]
    value: dict[str, Any] = field(default_factory=new_state)

    def __post_init__(self) -> None:
        self.factory_baseline = normalize_baseline(self.factory_baseline)
        self.value = normalize_state(self.value)

    @property
    def data(self) -> dict[str, Any]:
        return compose_baseline(
            self.factory_baseline,
            self.value["importedBaseline"],
            self.value["employeeOverlay"],
        )

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return self.value["jobs"]

    def ensure_attendance(self) -> dict[str, dict[str, Any]]:
        return ensure_attendance(self.value, self.data)

    def import_master_data(
        self,
        baseline: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.value = import_baseline(self.value, baseline, metadata=metadata)

    def reset_imported_data(self) -> None:
        self.value = reset_to_imported_baseline(self.value)

    def reset_factory_data(self) -> None:
        self.value = reset_to_factory_baseline(self.value)

    def clear_plan(self) -> None:
        self.value = clear_plan(self.value)

    def export_plan(self, *, indent: int | None = 2) -> str:
        return export_plan_snapshot(self.value, indent=indent)

    def import_plan(
        self, payload: str | bytes | bytearray | Mapping[str, Any]
    ) -> None:
        self.value = import_plan_snapshot(
            self.value, payload, active_baseline=self.data
        )

    def save(self, store: StateStore | None = None) -> Path:
        return (store or StateStore()).save(self.value)

    @classmethod
    def load(
        cls,
        factory_baseline: Mapping[str, Any],
        store: StateStore | None = None,
    ) -> "SchedulerState":
        return cls(factory_baseline, (store or StateStore()).load())


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "DEFAULT_STATE_DIRECTORY",
    "DEFAULT_STATE_FILENAME",
    "MAX_SAVED_JOBS",
    "PLAN_SNAPSHOT_SCHEMA",
    "PLAN_SNAPSHOT_VERSION",
    "STATE_DIRECTORY_ENV",
    "STATE_VERSION",
    "SchedulerState",
    "StateStore",
    "StateValidationError",
    "add_employee_to_overlay",
    "attendance_plan_key",
    "build_plan_snapshot",
    "clear_plan",
    "clear_result",
    "compose_baseline",
    "deep_clone",
    "default_attendance_by_plan",
    "default_attendance_for_shift",
    "default_attendance_record",
    "deterministic_operator_key",
    "edit_employee_overlay",
    "empty_employee_overlay",
    "ensure_attendance",
    "export_plan_snapshot",
    "import_baseline",
    "import_plan_snapshot",
    "load_plan_snapshot",
    "load_state",
    "minutes_to_clock",
    "new_state",
    "normalize_baseline",
    "normalize_employee_overlay",
    "normalize_jobs",
    "normalize_state",
    "normalized_name",
    "operator_key",
    "overlay_has_changes",
    "parse_attendance_plan_key",
    "parse_plan_snapshot",
    "reset_to_factory_baseline",
    "reset_to_imported_baseline",
    "resolve_state_directory",
    "resolve_state_path",
    "save_plan_snapshot",
    "save_state",
    "validate_baseline",
    "validate_employee_addition",
    "validate_employee_edit",
    "validate_state",
]
