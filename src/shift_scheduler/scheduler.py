"""Deterministic manufacturing shift scheduling engine.

This module is a direct Python port of the project's original
``scheduler-core.js``.  Its public functions intentionally retain the
JavaScript engine's camelCase dictionary schema so existing inputs and saved
schedule snapshots can be used without translation.
"""

from __future__ import annotations

import math
import re
from functools import cmp_to_key
from typing import Any, Iterable


EPSILON = 1e-7

STATUS: dict[str, dict[str, str]] = {
    "scheduled": {
        "label": "Scheduled",
        "severity": "success",
        "message": "Assigned to a qualified, available operator.",
    },
    "material_hold": {
        "label": "Material hold",
        "severity": "warning",
        "message": "Material was marked not ready.",
    },
    "quantity_required": {
        "label": "Quantity required",
        "severity": "danger",
        "message": "Enter a positive whole-number quantity.",
    },
    "no_route": {
        "label": "No approved route",
        "severity": "danger",
        "message": "No approved orange-bucket route exists for this complete part.",
    },
    "p75_required": {
        "label": "Missing P75 time",
        "severity": "danger",
        "message": (
            "This operation has no approved P75 time. Enter an override in the "
            "Excel file for updating."
        ),
    },
    "skill_map_required": {
        "label": "Skill map required",
        "severity": "danger",
        "message": "This operation has not been mapped to a qualification.",
    },
    "no_qualified_operator": {
        "label": "Unfinished — no qualified operator",
        "severity": "danger",
        "message": "No present operator on this shift meets the minimum skill level.",
    },
    "shift_capacity": {
        "label": "Unfinished — shift capacity",
        "severity": "warning",
        "message": (
            "One or more whole-unit operations still cannot fit after "
            "unit-flow scheduling."
        ),
    },
    "blocked_child": {
        "label": "Blocked — child unfinished",
        "severity": "danger",
        "message": "At least one entered child job did not finish.",
    },
    "previous_unfinished": {
        "label": "Unfinished — previous operation",
        "severity": "warning",
        "message": (
            "The same whole unit did not finish its preceding route operation."
        ),
    },
}


class _Undefined:
    pass


UNDEFINED = _Undefined()


def _js_truthy(value: Any) -> bool:
    """Return JavaScript Boolean(value), including truthy empty arrays/objects."""

    if value is UNDEFINED or value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return bool(value)
    return True


def _js_string(value: Any) -> str:
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer():
            return str(int(value))
    return str(value)


def _js_number(value: Any) -> int | float:
    """Implement the subset of JavaScript Number() used by scheduler-core.js."""

    if value is UNDEFINED:
        return math.nan
    if value is None:
        return 0
    if value is True:
        return 1
    if value is False:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            parsed = float(text)
        except ValueError:
            return math.nan
        return int(parsed) if math.isfinite(parsed) and parsed.is_integer() else parsed
    return math.nan


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_integer(value: Any) -> bool:
    return _is_finite_number(value) and float(value).is_integer()


def _get(mapping: dict[str, Any] | None, key: str) -> Any:
    if mapping is None:
        return UNDEFINED
    return mapping.get(key, UNDEFINED)


def operator_key(operator: dict[str, Any]) -> str:
    """Return the same stable operator key as the browser scheduler."""

    scheduler_key = _get(operator, "schedulerKey")
    if _js_truthy(scheduler_key):
        return _js_string(scheduler_key)
    employee_id = _get(operator, "employeeId")
    if _js_truthy(employee_id):
        return _js_string(employee_id)
    return f"name:{_js_string(_get(operator, 'name'))}"


def as_number(value: Any, fallback: int | float) -> int | float:
    number = _js_number(value)
    return number if _is_finite_number(number) else fallback


def time_to_minutes(value: Any) -> int | None:
    """Parse a strict 24-hour ``H:MM`` or ``HH:MM`` clock value."""

    if not _js_truthy(value):
        return None
    text = _js_string(value)
    if not re.fullmatch(r"\d{1,2}:\d{2}", text, flags=re.ASCII):
        return None
    hours, minutes = (int(item) for item in text.split(":"))
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        return None
    return hours * 60 + minutes


def parse_typed_time(value: Any) -> str | None:
    """Normalize the flexible clock formats accepted by the original UI."""

    text = _js_string("" if value is None or value is UNDEFINED else value)
    text = re.sub(r"\s+", "", text.strip().lower().replace(".", ""))
    if not text:
        return None

    suffix_match = re.search(r"(am|pm|a|p)$", text)
    suffix = suffix_match.group(1)[0] if suffix_match else ""
    clock_text = text[: -len(suffix_match.group(1))] if suffix_match else text

    if re.fullmatch(r"\d{1,2}:\d{1,2}", clock_text, flags=re.ASCII):
        hours, minutes = (int(item) for item in clock_text.split(":"))
    elif re.fullmatch(r"\d{1,2}", clock_text, flags=re.ASCII):
        hours, minutes = int(clock_text), 0
    elif re.fullmatch(r"\d{3,4}", clock_text, flags=re.ASCII):
        hours, minutes = int(clock_text[:-2]), int(clock_text[-2:])
    else:
        return None

    if minutes < 0 or minutes > 59:
        return None
    if suffix:
        if hours < 1 or hours > 12:
            return None
        hours %= 12
        if suffix == "p":
            hours += 12
    elif hours < 0 or hours > 23:
        return None

    return f"{hours:02d}:{minutes:02d}"


def due_offset_minutes(value: Any, shift: dict[str, Any]) -> int:
    due = time_to_minutes(value)
    if due is None:
        return 1440
    return (due - _js_number(_get(shift, "startMinutes")) + 1440) % 1440


def _compare_tuple(left: Iterable[Any], right: Iterable[Any]) -> int:
    left_items = list(left)
    right_items = list(right)
    length = max(len(left_items), len(right_items))
    for index in range(length):
        left_value = left_items[index] if index < len(left_items) else UNDEFINED
        right_value = right_items[index] if index < len(right_items) else UNDEFINED
        if left_value < right_value:
            return -1
        if left_value > right_value:
            return 1
    return 0


def _normalize_availability_windows(
    attendance: dict[str, Any] | None,
    shift_length_minutes: int | float,
    fallback_hours: int | float,
) -> list[dict[str, int | float]]:
    windows_value = _get(attendance, "availabilityWindows")
    if attendance is not None and isinstance(windows_value, list):
        windows: list[dict[str, int | float]] = []
        for raw_window in windows_value:
            window = raw_window if isinstance(raw_window, dict) else {}
            start = max(0, as_number(_get(window, "start"), 0))
            end = min(
                shift_length_minutes,
                as_number(_get(window, "end"), shift_length_minutes),
            )
            if end > start + EPSILON:
                windows.append({"start": start, "end": end})
        windows.sort(key=lambda item: (item["start"], item["end"]))

        merged: list[dict[str, int | float]] = []
        for window in windows:
            previous = merged[-1] if merged else None
            if previous and window["start"] <= previous["end"] + EPSILON:
                previous["end"] = max(previous["end"], window["end"])
            else:
                merged.append(dict(window))
        return merged

    hours_value = _get(attendance, "hours")
    if (
        attendance is not None
        and hours_value != ""
        and hours_value is not None
    ):
        hours = max(0, as_number(hours_value, fallback_hours))
    else:
        hours = fallback_hours
    end = min(hours * 60, shift_length_minutes)
    return [{"start": 0, "end": end}] if end > EPSILON else []


def _add_booking(
    calendar: list[dict[str, int | float]],
    booking: dict[str, int | float],
) -> None:
    calendar.append(booking)
    calendar.sort(key=lambda item: (item["start"], item["end"]))


def _find_earliest_slot(
    earliest: int | float,
    duration: int | float,
    windows: list[dict[str, int | float]],
    operator_bookings: list[dict[str, int | float]],
    center_bookings: list[dict[str, int | float]],
) -> int | float | None:
    conflicts = sorted(
        operator_bookings + center_bookings,
        key=lambda item: (item["start"], item["end"]),
    )
    for window in windows:
        start = max(earliest, window["start"])
        while start + duration <= window["end"] + EPSILON:
            conflict = next(
                (
                    booking
                    for booking in conflicts
                    if booking["start"] < start + duration - EPSILON
                    and booking["end"] > start + EPSILON
                ),
                None,
            )
            if conflict is None:
                return start
            start = max(start, conflict["end"])
    return None


def _make_operation(
    task: dict[str, Any],
    status_code: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = STATUS[status_code]
    details = extra or {}
    sub_batch_index = _js_number(details.get("subBatchIndex", UNDEFINED))
    sub_batch_index = sub_batch_index if _js_truthy(sub_batch_index) else 1
    sub_batch_count = _js_number(details.get("subBatchCount", UNDEFINED))
    sub_batch_count = sub_batch_count if _js_truthy(sub_batch_count) else 1
    route = task["route"]
    job = task["job"]
    sequence = route.get("effectiveSequence") if route is not None else 0
    operation_id = (
        f"{job['id']}|{_js_string(sequence)}|{task['routeIndex']}"
        + (f"|{_js_string(sub_batch_index)}" if sub_batch_count > 1 else "")
    )
    operation = {
        "id": operation_id,
        "dispatchIndex": task["dispatchIndex"],
        "jobIndex": job["inputIndex"],
        "jobId": job["id"],
        "part": job["part"],
        "quantity": job["quantity"],
        "originalQuantity": job["quantity"],
        "priority": job["priority"],
        "dueTime": job["dueTime"],
        "routeIndex": task["routeIndex"],
        "routeSequence": route.get("effectiveSequence") if route is not None else None,
        "operation": route.get("operation") if route is not None else None,
        "partOperation": (
            route.get("partOperation") if route is not None else job["part"]
        ),
        "workCenter": route.get("workCenter") if route is not None else None,
        "description": route.get("description") if route is not None else None,
        "skill": route.get("skill") if route is not None else None,
        "p75HoursPerPiece": (
            route.get("p75HoursPerPiece") if route is not None else None
        ),
        "moveMinutes": (
            as_number(route.get("moveMinutes", UNDEFINED), 0)
            if route is not None
            else 0
        ),
        "durationMinutes": (
            task["durationMinutes"]
            if route is not None and _is_finite_number(task["durationMinutes"])
            else None
        ),
        "startOffset": None,
        "endOffset": None,
        "operatorId": None,
        "operatorName": None,
        "operatorSkill": None,
        "statusCode": status_code,
        "statusLabel": status["label"],
        "severity": status["severity"],
        "explanation": status["message"],
        "batchSplit": sub_batch_count > 1,
        "subBatchIndex": sub_batch_index,
        "subBatchCount": sub_batch_count,
    }
    operation.update(details)
    return operation


def run(data: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    """Run the scheduler with JavaScript-compatible dictionaries."""

    if (
        not _js_truthy(data)
        or not isinstance(_get(data, "routes"), list)
        or not _js_truthy(_get(data, "settings"))
    ):
        raise ValueError("Scheduler data is missing or invalid.")

    input_data = input_data or {}
    raw_shift_id = _get(input_data, "shiftId")
    shift_id = _js_number(raw_shift_id if _js_truthy(raw_shift_id) else 1)
    shifts = data["settings"].get("shifts", [])
    shift = next(
        (
            item
            for item in shifts
            if _js_number(_get(item, "id")) == shift_id
        ),
        None,
    )
    if shift is None:
        raise ValueError(f"Shift {_js_string(shift_id)} is not defined.")

    shift_length_minutes = (
        as_number(_get(shift, "availableHours"), 0) * 60
    )
    minimum_skill = as_number(
        _get(data["settings"], "minimumSoloSkillLevel"),
        2,
    )
    routes_by_part: dict[Any, list[dict[str, Any]]] = {}
    dependencies_by_parent: dict[Any, list[Any]] = {}
    attendance_by_operator: dict[str, dict[str, Any]] = {}
    attendance_input = _get(input_data, "attendance")
    if isinstance(attendance_input, list):
        for record in attendance_input:
            attendance_by_operator[_js_string(_get(record, "operatorKey"))] = record

    for route in data["routes"]:
        routes_by_part.setdefault(route.get("part"), []).append(route)
    for routes in routes_by_part.values():
        routes.sort(
            key=lambda route: _js_number(
                route.get("effectiveSequence", UNDEFINED)
            )
        )

    for relationship in data["parentChild"]:
        dependencies_by_parent.setdefault(relationship.get("parent"), []).append(
            relationship.get("child")
        )

    errors: list[str] = []
    warnings: list[dict[str, Any]] = []
    jobs_input = _get(input_data, "jobs")
    raw_jobs = jobs_input if isinstance(jobs_input, list) else []
    used_ids: set[str] = set()
    jobs: list[dict[str, Any]] = []

    for input_index, raw_job in enumerate(raw_jobs):
        fallback_id = f"JOB-{input_index + 1:02d}"
        raw_id = _get(raw_job, "id")
        job_id = _js_string(raw_id if _js_truthy(raw_id) else fallback_id).strip()
        raw_part = _get(raw_job, "part")
        part = _js_string(raw_part if _js_truthy(raw_part) else "").strip()
        quantity = _js_number(_get(raw_job, "quantity"))
        priority = _js_number(_get(raw_job, "priority"))
        raw_due_time = _get(raw_job, "dueTime")
        due_time = _js_string(
            raw_due_time if _js_truthy(raw_due_time) else ""
        ).strip()
        raw_material_ready = _get(raw_job, "materialReady")
        material_ready = (
            raw_material_ready is not False
            and _js_string(
                raw_material_ready if _js_truthy(raw_material_ready) else "Yes"
            ).lower()
            != "no"
        )

        if not part:
            errors.append(f"Job {job_id or fallback_id}: choose a complete part.")
        if job_id in used_ids:
            errors.append(f"Job ID {job_id} is duplicated.")
        used_ids.add(job_id)
        if not _is_integer(quantity) or quantity <= 0:
            errors.append(
                f"Job {job_id}: quantity must be a positive whole number."
            )
        if not _is_integer(priority) or priority < 1 or priority > 5:
            errors.append(
                f"Job {job_id}: priority must be from 1 (highest) to 5."
            )
        if due_time and time_to_minutes(due_time) is None:
            errors.append(f"Job {job_id}: due time is invalid.")

        routes = routes_by_part.get(part, [])
        estimated_minutes: int | float = 0
        for route in routes:
            p75 = route.get("p75HoursPerPiece")
            if not _is_finite_number(p75):
                estimated_minutes = math.inf
                break
            estimated_minutes += p75 * quantity * 60

        raw_notes = _get(raw_job, "notes")
        notes = _js_string(raw_notes if _js_truthy(raw_notes) else "").strip()
        jobs.append(
            {
                "id": job_id,
                "part": part,
                "quantity": quantity,
                "priority": priority,
                "dueTime": due_time,
                "dueOffset": due_offset_minutes(due_time, shift),
                "materialReady": material_ready,
                "notes": notes,
                "inputIndex": input_index,
                "routes": routes,
                "estimatedMinutes": estimated_minutes,
                "activeChildJobIds": [],
                "depth": 0,
            }
        )

    jobs_by_part: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_part.setdefault(job["part"], []).append(job)

    for job in jobs:
        child_parts = dependencies_by_parent.get(job["part"], [])
        for child_part in child_parts:
            for child_job in jobs_by_part.get(child_part, []):
                job["activeChildJobIds"].append(child_job["id"])
        if child_parts and not job["activeChildJobIds"]:
            warnings.append(
                {
                    "code": "inactive_dependency",
                    "jobId": job["id"],
                    "message": (
                        f"{job['id']} ({job['part']}) has linked child parts, but "
                        "none are entered. Its parent-child dependency is inactive."
                    ),
                }
            )

    job_by_id = {job["id"]: job for job in jobs}
    visiting: set[str] = set()
    visited: set[str] = set()

    def calculate_depth(job: dict[str, Any]) -> int:
        if job["id"] in visited:
            return job["depth"]
        if job["id"] in visiting:
            errors.append(
                "The entered jobs contain a parent-child cycle involving "
                f"{job['id']}."
            )
            return 0
        visiting.add(job["id"])
        depth = 0
        for child_id in job["activeChildJobIds"]:
            child = job_by_id.get(child_id)
            if child:
                depth = max(depth, calculate_depth(child) + 1)
        visiting.discard(job["id"])
        visited.add(job["id"])
        job["depth"] = depth
        return depth

    for job in jobs:
        calculate_depth(job)

    if _js_truthy(shift.get("placeholder", UNDEFINED)):
        warnings.insert(
            0,
            {
                "code": "placeholder_shift",
                "message": (
                    "Shift 3 is a placeholder in the source workbook. Verify its "
                    "hours before using this schedule."
                ),
            },
        )

    if errors:
        return {
            "shift": shift,
            "errors": errors,
            "warnings": warnings,
            "batchSplits": [],
            "transferFlows": [],
            "operations": [],
            "jobs": [],
            "summary": {
                "jobCount": len(jobs),
                "operationCount": 0,
                "scheduledCount": 0,
                "unfinishedCount": 0,
                "blockedCount": 0,
                "splitCount": 0,
                "transferCount": 0,
                "warningCount": len(warnings),
                "completionOffset": None,
            },
        }

    active_operators: list[dict[str, Any]] = []
    for operator_index, operator in enumerate(data["operators"]):
        key = operator_key(operator)
        attendance = attendance_by_operator.get(key)
        default_on_shift = (
            _js_number(_get(operator, "defaultShift")) == shift_id
        )
        if attendance is not None:
            present = _js_truthy(_get(attendance, "present"))
        else:
            present = (
                default_on_shift
                and _get(operator, "defaultPresent") is not False
            )
        availability_windows = _normalize_availability_windows(
            attendance,
            shift_length_minutes,
            shift.get("availableHours"),
        )
        active_operator = dict(operator)
        active_operator.update(
            {
                "key": key,
                "operatorIndex": operator_index,
                "present": present and bool(availability_windows),
                "availabilityWindows": availability_windows,
            }
        )
        active_operators.append(active_operator)

    work_center_bookings: dict[Any, list[dict[str, int | float]]] = {}
    operator_bookings: dict[str, list[dict[str, int | float]]] = {
        operator["key"]: [] for operator in active_operators
    }
    raw_operations_by_job: dict[str, list[dict[str, Any]]] = {
        job["id"]: [] for job in jobs
    }
    operations: list[dict[str, Any]] = []
    operations_by_job: dict[str, list[dict[str, Any]]] = {
        job["id"]: [] for job in jobs
    }
    completion_by_job: dict[str, int | float] = {}
    job_finished: dict[str, bool] = {}
    batch_splits: list[dict[str, Any]] = []
    transfer_flows: list[dict[str, Any]] = []
    dispatch_index = 0

    def record_unit(
        task: dict[str, Any],
        status_code: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = _make_operation(task, status_code, extra)
        raw_operations_by_job[task["job"]["id"]].append(operation)
        return operation

    def raw_route_operations(
        job: dict[str, Any],
        route_index: int,
    ) -> list[dict[str, Any]]:
        return [
            operation
            for operation in raw_operations_by_job[job["id"]]
            if operation["routeIndex"] == route_index
        ]

    def raw_job_is_finished(job: dict[str, Any]) -> bool:
        if not job["routes"]:
            return False
        recorded = raw_operations_by_job[job["id"]]
        return all(
            any(
                operation.get("unitIndex") == unit_index
                and operation["routeIndex"] == route_index
                and operation["statusCode"] == "scheduled"
                for operation in recorded
            )
            for unit_index in range(1, int(job["quantity"]) + 1)
            for route_index, _route in enumerate(job["routes"])
        )

    def group_consecutive(
        records: list[dict[str, Any]],
        can_merge: Any,
    ) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for record in records:
            previous = groups[-1] if groups else None
            if previous is not None and can_merge(previous, record):
                previous["records"].append(record)
                previous["endOffset"] = record["endOffset"]
                previous["lastUnitIndex"] = record["unitIndex"]
            else:
                groups.append(
                    {
                        "records": [record],
                        "startOffset": record["startOffset"],
                        "endOffset": record["endOffset"],
                        "firstUnitIndex": record["unitIndex"],
                        "lastUnitIndex": record["unitIndex"],
                    }
                )
        return groups

    def aggregate_job_operations(job: dict[str, Any]) -> None:
        if not job["routes"]:
            no_route = (
                raw_operations_by_job[job["id"]][0]
                if raw_operations_by_job[job["id"]]
                else None
            )
            if no_route is not None:
                operations.append(no_route)
                operations_by_job[job["id"]].append(no_route)
            return

        for route_index, route in enumerate(job["routes"]):
            route_records = raw_route_operations(job, route_index)
            scheduled_records = sorted(
                (
                    operation
                    for operation in route_records
                    if operation["statusCode"] == "scheduled"
                ),
                key=lambda operation: (
                    operation["startOffset"],
                    operation["unitIndex"],
                ),
            )
            scheduled_groups = group_consecutive(
                scheduled_records,
                lambda previous, record: (
                    previous["records"][-1]["operatorId"]
                    == record["operatorId"]
                    and previous["lastUnitIndex"] + 1 == record["unitIndex"]
                    and abs(previous["endOffset"] - record["startOffset"])
                    <= EPSILON
                ),
            )

            issue_records = sorted(
                (
                    operation
                    for operation in route_records
                    if operation["statusCode"] != "scheduled"
                ),
                key=lambda operation: operation["unitIndex"],
            )
            issue_groups = group_consecutive(
                issue_records,
                lambda previous, record: (
                    previous["records"][-1]["statusCode"]
                    == record["statusCode"]
                    and previous["lastUnitIndex"] + 1 == record["unitIndex"]
                ),
            )

            groups = scheduled_groups + issue_groups
            split_applied = job["quantity"] > 1 and len(groups) > 1
            for index, group in enumerate(groups):
                first = group["records"][0]
                quantity = len(group["records"])
                scheduled = first["statusCode"] == "scheduled"
                p75 = route.get("p75HoursPerPiece")
                task = {
                    "job": job,
                    "route": route,
                    "routeIndex": route_index,
                    "dispatchIndex": min(
                        operation["dispatchIndex"]
                        for operation in group["records"]
                    ),
                    "durationMinutes": (
                        p75 * quantity * 60
                        if _is_finite_number(p75)
                        else None
                    ),
                }
                operation = _make_operation(
                    task,
                    first["statusCode"],
                    {
                        "quantity": quantity,
                        "originalQuantity": job["quantity"],
                        "durationMinutes": task["durationMinutes"],
                        "startOffset": (
                            group["startOffset"] if scheduled else None
                        ),
                        "endOffset": group["endOffset"] if scheduled else None,
                        "operatorId": first["operatorId"] if scheduled else None,
                        "operatorName": (
                            first["operatorName"] if scheduled else None
                        ),
                        "operatorSkill": (
                            first["operatorSkill"] if scheduled else None
                        ),
                        "batchSplit": split_applied,
                        "subBatchIndex": index + 1 if split_applied else 1,
                        "subBatchCount": len(groups) if split_applied else 1,
                        "unitIndexes": [
                            record["unitIndex"] for record in group["records"]
                        ],
                    },
                )
                operations.append(operation)
                operations_by_job[job["id"]].append(operation)

            if (
                len(scheduled_groups) > 1
                and len(scheduled_records) == job["quantity"]
                and not issue_groups
            ):
                batch_splits.append(
                    {
                        "jobId": job["id"],
                        "part": job["part"],
                        "originalQuantity": job["quantity"],
                        "routeIndex": route_index,
                        "routeSequence": route.get("effectiveSequence"),
                        "operation": route.get("operation"),
                        "partOperation": route.get("partOperation"),
                        "workCenter": route.get("workCenter"),
                        "chunks": [
                            {
                                "index": index + 1,
                                "quantity": len(group["records"]),
                                "startOffset": group["startOffset"],
                                "endOffset": group["endOffset"],
                                "operatorId": (
                                    group["records"][0].get("operatorId") or ""
                                ),
                                "operatorName": group["records"][0].get(
                                    "operatorName"
                                ),
                            }
                            for index, group in enumerate(scheduled_groups)
                        ],
                    }
                )

    def route_operations(
        job: dict[str, Any],
        route_index: int,
    ) -> list[dict[str, Any]]:
        return [
            operation
            for operation in operations_by_job[job["id"]]
            if operation["routeIndex"] == route_index
        ]

    def route_finished(job: dict[str, Any], route_index: int) -> bool:
        recorded = route_operations(job, route_index)
        return (
            bool(recorded)
            and all(
                operation["statusCode"] == "scheduled"
                for operation in recorded
            )
            and sum(operation["quantity"] for operation in recorded)
            == job["quantity"]
        )

    def job_is_finished(job: dict[str, Any]) -> bool:
        return bool(job["routes"]) and all(
            route_finished(job, route_index)
            for route_index, _route in enumerate(job["routes"])
        )

    ordered_jobs = sorted(
        jobs,
        key=lambda job: (
            job["depth"],
            job["priority"],
            job["dueOffset"],
            job["estimatedMinutes"],
            job["inputIndex"],
        ),
    )

    for job in ordered_jobs:
        if not job["routes"]:
            task = {
                "job": job,
                "route": None,
                "routeIndex": 0,
                "durationMinutes": None,
                "dispatchIndex": dispatch_index,
            }
            dispatch_index += 1
            record_unit(
                task,
                "no_route",
                {
                    "quantity": job["quantity"],
                    "originalQuantity": job["quantity"],
                },
            )
            job_finished[job["id"]] = False
            continue

        child_blocked = any(
            job_finished.get(child_id) is not True
            for child_id in job["activeChildJobIds"]
        )
        child_ready = (
            0
            if child_blocked
            else max(
                [
                    0,
                    *(
                        completion_by_job.get(child_id, 0)
                        for child_id in job["activeChildJobIds"]
                    ),
                ]
            )
        )
        unit_plans: list[list[dict[str, Any]]] = [
            [] for _unit_index in range(int(job["quantity"]))
        ]

        for unit_index in range(int(job["quantity"])):
            for route_index, route in enumerate(job["routes"]):
                p75 = route.get("p75HoursPerPiece")
                unit_duration = (
                    p75 * 60 if _is_finite_number(p75) else None
                )
                task = {
                    "job": job,
                    "route": route,
                    "routeIndex": route_index,
                    "durationMinutes": unit_duration,
                    "dispatchIndex": dispatch_index,
                }
                dispatch_index += 1
                base_details = {
                    "quantity": 1,
                    "originalQuantity": job["quantity"],
                    "unitIndex": unit_index + 1,
                    "batchSplit": job["quantity"] > 1,
                    "subBatchIndex": unit_index + 1,
                    "subBatchCount": job["quantity"],
                }
                prior_operation = (
                    unit_plans[unit_index][route_index - 1]
                    if route_index > 0
                    else None
                )

                status_code: str | None = None
                route_skill = route.get("skill")
                if route_index == 0 and child_blocked:
                    status_code = "blocked_child"
                elif not job["materialReady"]:
                    status_code = "material_hold"
                elif route_index > 0 and (
                    prior_operation is None
                    or prior_operation["statusCode"] != "scheduled"
                ):
                    status_code = "previous_unfinished"
                elif (
                    not _js_truthy(route_skill)
                    or _js_string(route_skill).lower() == "unmapped"
                ):
                    status_code = "skill_map_required"
                elif (
                    not _is_finite_number(unit_duration)
                    or unit_duration <= 0
                ):
                    status_code = "p75_required"

                if status_code is not None:
                    failed = record_unit(task, status_code, base_details)
                    unit_plans[unit_index].append(failed)
                    continue

                qualified_operators: list[dict[str, Any]] = []
                for operator in active_operators:
                    skill_level = as_number(
                        (operator.get("skills") or {}).get(
                            route_skill,
                            UNDEFINED,
                        ),
                        0,
                    )
                    if operator["present"] and skill_level >= minimum_skill:
                        qualified_operators.append(operator)

                if not qualified_operators:
                    failed = record_unit(
                        task,
                        "no_qualified_operator",
                        base_details,
                    )
                    unit_plans[unit_index].append(failed)
                    continue

                earliest = (
                    child_ready
                    if route_index == 0
                    else prior_operation["endOffset"]
                    + as_number(route.get("moveMinutes", UNDEFINED), 0)
                )
                work_center = route.get("workCenter")
                center_calendar = work_center_bookings.get(work_center, [])
                best: dict[str, Any] | None = None
                for operator in qualified_operators:
                    start = _find_earliest_slot(
                        earliest,
                        unit_duration,
                        operator["availabilityWindows"],
                        operator_bookings.get(operator["key"], []),
                        center_calendar,
                    )
                    if start is None:
                        continue
                    candidate = {
                        "operator": operator,
                        "start": start,
                        "end": start + unit_duration,
                        "skillLevel": as_number(
                            (operator.get("skills") or {}).get(
                                route_skill,
                                UNDEFINED,
                            ),
                            0,
                        ),
                    }
                    if best is None or _compare_tuple(
                        [
                            candidate["end"],
                            -candidate["skillLevel"],
                            candidate["operator"]["operatorIndex"],
                        ],
                        [
                            best["end"],
                            -best["skillLevel"],
                            best["operator"]["operatorIndex"],
                        ],
                    ) < 0:
                        best = candidate

                if best is None:
                    failed = record_unit(
                        task,
                        "shift_capacity",
                        base_details,
                    )
                    unit_plans[unit_index].append(failed)
                    continue

                work_center_bookings.setdefault(work_center, [])
                booking = {"start": best["start"], "end": best["end"]}
                _add_booking(work_center_bookings[work_center], booking)
                _add_booking(
                    operator_bookings[best["operator"]["key"]],
                    booking,
                )
                scheduled = record_unit(
                    task,
                    "scheduled",
                    {
                        **base_details,
                        "startOffset": best["start"],
                        "endOffset": best["end"],
                        "operatorId": (
                            best["operator"].get("employeeId")
                            if _js_truthy(
                                best["operator"].get(
                                    "employeeId",
                                    UNDEFINED,
                                )
                            )
                            else ""
                        ),
                        "operatorName": best["operator"].get("name"),
                        "operatorSkill": best["skillLevel"],
                    },
                )
                unit_plans[unit_index].append(scheduled)

        complete = raw_job_is_finished(job)
        job_finished[job["id"]] = complete
        if complete:
            final_route_index = len(job["routes"]) - 1
            completion_by_job[job["id"]] = max(
                operation["endOffset"]
                for operation in raw_route_operations(
                    job,
                    final_route_index,
                )
            )

        for route_index in range(1, len(job["routes"])):
            prior_scheduled = [
                operation
                for operation in raw_route_operations(job, route_index - 1)
                if operation["statusCode"] == "scheduled"
            ]
            current_scheduled = [
                operation
                for operation in raw_route_operations(job, route_index)
                if operation["statusCode"] == "scheduled"
            ]
            if not prior_scheduled or not current_scheduled:
                continue
            prior_batch_finish = max(
                operation["endOffset"] for operation in prior_scheduled
            )
            early_downstream = [
                operation
                for operation in current_scheduled
                if operation["startOffset"] < prior_batch_finish - EPSILON
            ]
            if not early_downstream:
                continue
            transfer_flows.append(
                {
                    "jobId": job["id"],
                    "part": job["part"],
                    "routeIndex": route_index,
                    "priorPartOperation": job["routes"][route_index - 1].get(
                        "partOperation"
                    ),
                    "partOperation": job["routes"][route_index].get(
                        "partOperation"
                    ),
                    "priorWorkCenter": job["routes"][route_index - 1].get(
                        "workCenter"
                    ),
                    "workCenter": job["routes"][route_index].get("workCenter"),
                    "quantityReleasedEarly": len(early_downstream),
                    "firstStartOffset": min(
                        operation["startOffset"]
                        for operation in early_downstream
                    ),
                    "priorBatchFinishOffset": prior_batch_finish,
                }
            )

    for job in jobs:
        aggregate_job_operations(job)

    job_summaries: list[dict[str, Any]] = []
    for job in jobs:
        job_operations = list(operations_by_job[job["id"]])
        job_operations.sort(
            key=lambda operation: (
                operation["routeSequence"] or 0,
                operation["subBatchIndex"],
            )
        )
        all_scheduled = job_is_finished(job)
        completion_offset = (
            completion_by_job.get(job["id"]) if all_scheduled else None
        )
        status_code = "unfinished"
        if all_scheduled:
            status_code = "complete"
        elif any(
            operation["statusCode"] == "blocked_child"
            for operation in job_operations
        ):
            status_code = "blocked"
        elif all(
            operation["statusCode"] == "material_hold"
            for operation in job_operations
        ):
            status_code = "held"
        due_result = ""
        if job["dueTime"] and completion_offset is not None:
            due_result = (
                "On time"
                if completion_offset <= job["dueOffset"] + EPSILON
                else "Late"
            )
        job_summaries.append(
            {
                "id": job["id"],
                "part": job["part"],
                "quantity": job["quantity"],
                "priority": job["priority"],
                "dueTime": job["dueTime"],
                "materialReady": job["materialReady"],
                "activeChildJobIds": list(job["activeChildJobIds"]),
                "statusCode": status_code,
                "completionOffset": completion_offset,
                "dueResult": due_result,
                "operations": job_operations,
            }
        )

    scheduled_operations = [
        operation
        for operation in operations
        if operation["statusCode"] == "scheduled"
    ]
    blocked_count = sum(
        operation["statusCode"] == "blocked_child"
        for operation in operations
    )
    unfinished_count = sum(
        operation["statusCode"]
        not in {"scheduled", "blocked_child"}
        for operation in operations
    )

    def compare_operations(
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> int:
        return _compare_tuple(
            [
                math.inf if left["startOffset"] is None else left["startOffset"],
                left["dispatchIndex"],
                left["subBatchIndex"],
            ],
            [
                math.inf if right["startOffset"] is None else right["startOffset"],
                right["dispatchIndex"],
                right["subBatchIndex"],
            ],
        )

    return {
        "shift": shift,
        "errors": errors,
        "warnings": warnings,
        "batchSplits": batch_splits,
        "transferFlows": transfer_flows,
        "operations": sorted(operations, key=cmp_to_key(compare_operations)),
        "jobs": job_summaries,
        "summary": {
            "jobCount": len(jobs),
            "operationCount": len(operations),
            "scheduledCount": len(scheduled_operations),
            "unfinishedCount": unfinished_count,
            "blockedCount": blocked_count,
            "splitCount": len(batch_splits),
            "transferCount": len(transfer_flows),
            "warningCount": len(warnings),
            "completionOffset": (
                max(
                    operation["endOffset"]
                    for operation in scheduled_operations
                )
                if scheduled_operations
                else None
            ),
        },
    }


# JavaScript-style aliases make migration of existing callers mechanical.
operatorKey = operator_key
parseTypedTime = parse_typed_time
timeToMinutes = time_to_minutes
dueOffsetMinutes = due_offset_minutes


__all__ = [
    "EPSILON",
    "STATUS",
    "due_offset_minutes",
    "dueOffsetMinutes",
    "operator_key",
    "operatorKey",
    "parse_typed_time",
    "parseTypedTime",
    "run",
    "time_to_minutes",
    "timeToMinutes",
]
