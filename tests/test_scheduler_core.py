"""Parity and invariant tests for the pure-Python scheduler engine."""

from __future__ import annotations

import json
import math
import sys
import unittest
from collections import defaultdict
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shift_scheduler import (  # noqa: E402
    EPSILON,
    STATUS,
    due_offset_minutes,
    operator_key,
    parse_typed_time,
    run,
    time_to_minutes,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "golden_scheduler_cases.json"
)
GOLDEN = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in GOLDEN["cases"]}


def _data_for(case: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(GOLDEN["base"])
    data["routes"] = deepcopy(case["routes"])
    data["parentChild"] = deepcopy(case["parentChild"])
    return data


def _run_case(name: str) -> dict[str, Any]:
    case = CASES[name]
    return run(_data_for(case), deepcopy(case["input"]))


def _inflate_expected(case: dict[str, Any]) -> dict[str, Any]:
    """Restore job operation lists omitted from the compact golden file."""

    expected = deepcopy(case["expected"])
    for job in expected["jobs"]:
        job["operations"] = sorted(
            (
                operation
                for operation in expected["operations"]
                if operation["jobId"] == job["id"]
            ),
            key=lambda operation: (
                operation["routeSequence"] or 0,
                operation["subBatchIndex"],
            ),
        )
    return expected


def _assert_equivalent(
    actual: Any,
    expected: Any,
    path: str = "result",
) -> None:
    """Deep equality with tolerance for JavaScript/Python float arithmetic."""

    actual_is_number = isinstance(actual, (int, float)) and not isinstance(
        actual, bool
    )
    expected_is_number = isinstance(expected, (int, float)) and not isinstance(
        expected, bool
    )
    if actual_is_number and expected_is_number:
        assert math.isclose(
            actual,
            expected,
            rel_tol=1e-10,
            abs_tol=1e-7,
        ), f"{path}: {actual!r} != {expected!r}"
        return

    assert type(actual) is type(expected), (
        f"{path}: {type(actual).__name__} != "
        f"{type(expected).__name__}"
    )
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), (
            f"{path}: key mismatch "
            f"{sorted(actual.keys() ^ expected.keys())}"
        )
        for key in expected:
            _assert_equivalent(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), (
            f"{path}: length {len(actual)} != {len(expected)}"
        )
        for index, (actual_item, expected_item) in enumerate(
            zip(actual, expected, strict=True)
        ):
            _assert_equivalent(
                actual_item,
                expected_item,
                f"{path}[{index}]",
            )
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_matches_commonjs_golden_output() -> None:
    """Every representative result matches the retired CommonJS core."""

    for case in GOLDEN["cases"]:
        actual = run(_data_for(case), deepcopy(case["input"]))
        try:
            _assert_equivalent(actual, _inflate_expected(case))
        except AssertionError as error:
            raise AssertionError(f"{case['name']}: {error}") from error


def test_unit_flow_dispatches_priority_and_each_unit_route_order() -> None:
    result = _run_case("normal_schedule")

    assert result["errors"] == []
    assert [operation["jobId"] for operation in result["operations"]] == [
        "JOB-B",
        "JOB-A",
        "JOB-A",
        "JOB-A",
        "JOB-A",
    ]
    job_a = next(job for job in result["jobs"] if job["id"] == "JOB-A")
    assert [operation["routeSequence"] for operation in job_a["operations"]] == [
        1,
        1,
        2,
        2,
    ]
    first_upstream = next(
        operation
        for operation in job_a["operations"]
        if operation["routeIndex"] == 0
        and operation["unitIndexes"] == [1]
    )
    first_downstream = next(
        operation
        for operation in job_a["operations"]
        if operation["routeIndex"] == 1
        and operation["unitIndexes"] == [1]
    )
    upstream_batch_finish = max(
        operation["endOffset"]
        for operation in job_a["operations"]
        if operation["routeIndex"] == 0
    )
    assert math.isclose(
        first_downstream["startOffset"],
        first_upstream["endOffset"] + 5,
        abs_tol=EPSILON,
    )
    assert first_downstream["startOffset"] < upstream_batch_finish
    assert result["summary"]["transferCount"] == 1
    assert result["transferFlows"] == [
        {
            "jobId": "JOB-A",
            "part": "PART-A",
            "routeIndex": 1,
            "priorPartOperation": "PART-A-10",
            "partOperation": "PART-A-20",
            "priorWorkCenter": "CENTER-A",
            "workCenter": "CENTER-B",
            "quantityReleasedEarly": 1,
            "firstStartOffset": 50,
            "priorBatchFinishOffset": 75,
        }
    ]
    assert job_a["statusCode"] == "complete"


def test_lunch_window_forces_whole_unit_sub_batches() -> None:
    result = _run_case("lunch_window_split")

    assert result["summary"]["splitCount"] == 1
    chunks = result["batchSplits"][0]["chunks"]
    assert [chunk["quantity"] for chunk in chunks] == [2, 2]
    assert [(chunk["startOffset"], chunk["endOffset"]) for chunk in chunks] == [
        (0, 120),
        (190, 310),
    ]
    assert sum(chunk["quantity"] for chunk in chunks) == 4
    assert all(isinstance(chunk["quantity"], int) for chunk in chunks)


def test_split_is_used_when_it_finishes_before_a_later_whole_batch() -> None:
    result = _run_case("split_finishes_earlier")

    chunks = result["batchSplits"][0]["chunks"]
    assert [chunk["quantity"] for chunk in chunks] == [1, 2]
    assert [chunk["startOffset"] for chunk in chunks] == [0, 120]
    assert math.isclose(
        result["summary"]["completionOffset"],
        240,
        abs_tol=EPSILON,
    )
    # A whole three-unit batch first fits at offset 120 and would end at 300.
    assert result["summary"]["completionOffset"] < 300


def test_shift_capacity_keeps_feasible_whole_units() -> None:
    result = _run_case("shift_capacity")

    scheduled = [
        operation
        for operation in result["operations"]
        if operation["statusCode"] == "scheduled"
    ]
    unfinished = [
        operation
        for operation in result["operations"]
        if operation["statusCode"] == "shift_capacity"
    ]
    assert sum(operation["quantity"] for operation in scheduled) == 2
    assert len(unfinished) == 1
    assert unfinished[0]["quantity"] == 2
    assert unfinished[0]["unitIndexes"] == [3, 4]
    assert unfinished[0]["startOffset"] is None
    assert unfinished[0]["endOffset"] is None
    assert result["batchSplits"] == []
    assert result["summary"]["scheduledCount"] == 2
    assert result["summary"]["unfinishedCount"] == 1


def test_contiguous_unit_operations_aggregate_into_one_display_block() -> None:
    result = _run_case("overnight_shift")
    operation = result["operations"][0]

    assert operation["quantity"] == 6
    assert operation["unitIndexes"] == [1, 2, 3, 4, 5, 6]
    assert operation["batchSplit"] is False
    assert result["batchSplits"] == []


def test_distinct_resources_pipeline_units_between_route_steps() -> None:
    case = CASES["normal_schedule"]
    data = _data_for(case)
    data["operators"][0]["skills"]["HP/IP Subassembly"] = 0
    data["operators"][1]["skills"]["Tube"] = 0
    for route in data["routes"]:
        if route["part"] == "PART-A":
            route["p75HoursPerPiece"] = 1
            route["moveMinutes"] = 0

    result = run(
        data,
        {
            "shiftId": 1,
            "jobs": [
                {
                    "id": "JOB-PIPELINE",
                    "part": "PART-A",
                    "quantity": 3,
                    "priority": 1,
                    "dueTime": "",
                    "materialReady": True,
                }
            ],
        },
    )
    by_route = {
        operation["routeIndex"]: operation
        for operation in result["operations"]
    }

    assert (by_route[0]["startOffset"], by_route[0]["endOffset"]) == (0, 180)
    assert (by_route[1]["startOffset"], by_route[1]["endOffset"]) == (60, 240)
    assert by_route[0]["unitIndexes"] == [1, 2, 3]
    assert by_route[1]["unitIndexes"] == [1, 2, 3]
    assert result["batchSplits"] == []
    assert result["transferFlows"][0]["quantityReleasedEarly"] == 2
    assert result["transferFlows"][0]["firstStartOffset"] == 60
    assert result["transferFlows"][0]["priorBatchFinishOffset"] == 180


def test_parent_waits_for_every_unit_of_an_entered_child() -> None:
    case = CASES["parent_child_barrier"]
    data = _data_for(case)
    scheduler_input = deepcopy(case["input"])
    child_job = next(
        job for job in scheduler_input["jobs"] if job["id"] == "JOB-CHILD"
    )
    child_job["quantity"] = 2

    result = run(data, scheduler_input)
    child = next(job for job in result["jobs"] if job["id"] == "JOB-CHILD")
    parent = next(job for job in result["jobs"] if job["id"] == "JOB-PARENT")

    assert child["statusCode"] == "complete"
    assert parent["statusCode"] == "complete"
    assert math.isclose(
        parent["operations"][0]["startOffset"],
        child["completionOffset"],
        abs_tol=EPSILON,
    )


def test_no_qualified_operator_preserves_exact_status_text() -> None:
    result = _run_case("no_qualified_operator")
    operation = result["operations"][0]

    assert operation["statusCode"] == "no_qualified_operator"
    assert operation["statusLabel"] == STATUS["no_qualified_operator"]["label"]
    assert operation["severity"] == STATUS["no_qualified_operator"]["severity"]
    assert operation["explanation"] == STATUS["no_qualified_operator"]["message"]


def test_parent_waits_for_every_entered_child() -> None:
    result = _run_case("parent_child_barrier")
    child = next(job for job in result["jobs"] if job["id"] == "JOB-CHILD")
    parent = next(job for job in result["jobs"] if job["id"] == "JOB-PARENT")

    assert math.isclose(child["completionOffset"], 60, abs_tol=EPSILON)
    assert math.isclose(
        parent["operations"][0]["startOffset"],
        child["completionOffset"],
        abs_tol=EPSILON,
    )
    assert parent["activeChildJobIds"] == ["JOB-CHILD"]
    assert parent["statusCode"] == "complete"


def test_unfinished_child_blocks_parent_first_operation() -> None:
    result = _run_case("parent_blocked_by_unfinished_child")
    by_job = {operation["jobId"]: operation for operation in result["operations"]}

    assert by_job["JOB-CHILD"]["statusCode"] == "no_qualified_operator"
    assert by_job["JOB-PARENT"]["statusCode"] == "blocked_child"
    assert result["summary"]["blockedCount"] == 1
    assert next(
        job for job in result["jobs"] if job["id"] == "JOB-PARENT"
    )["statusCode"] == "blocked"


def test_material_hold_applies_to_every_route_operation() -> None:
    result = _run_case("material_hold")

    assert [operation["statusCode"] for operation in result["operations"]] == [
        "material_hold",
        "material_hold",
    ]
    assert result["jobs"][0]["statusCode"] == "held"
    assert result["summary"]["unfinishedCount"] == 2


def test_missing_route_p75_and_skill_are_distinct() -> None:
    result = _run_case("missing_route_p75_and_skill")
    statuses = {
        operation["jobId"]: operation["statusCode"]
        for operation in result["operations"]
    }

    assert statuses == {
        "JOB-NO-ROUTE": "no_route",
        "JOB-P75": "p75_required",
        "JOB-SKILL": "skill_map_required",
    }
    for operation in result["operations"]:
        status = STATUS[operation["statusCode"]]
        assert operation["statusLabel"] == status["label"]
        assert operation["explanation"] == status["message"]


def test_missing_p75_blocks_the_following_route_operation() -> None:
    case = CASES["missing_route_p75_and_skill"]
    data = _data_for(case)
    data["routes"].append(
        {
            "id": "P75-PART|2",
            "part": "P75-PART",
            "sequence": 2,
            "effectiveSequence": 2,
            "partOperation": "P75-PART-20",
            "operation": 20,
            "workCenter": "CENTER-M2",
            "description": "Tube operation",
            "skill": "Tube",
            "p75HoursPerPiece": 0.25,
            "moveMinutes": 0,
        }
    )
    result = run(
        data,
        {
            "shiftId": 1,
            "jobs": [
                {
                    "id": "JOB-P75",
                    "part": "P75-PART",
                    "quantity": 1,
                    "priority": 1,
                    "dueTime": "",
                    "materialReady": True,
                }
            ],
        },
    )

    assert [operation["statusCode"] for operation in result["operations"]] == [
        "p75_required",
        "previous_unfinished",
    ]


def test_overnight_shift_uses_offset_time_for_due_result() -> None:
    result = _run_case("overnight_shift")

    assert result["shift"]["crossesMidnight"] is True
    assert due_offset_minutes("01:00", result["shift"]) == 570
    assert math.isclose(
        result["jobs"][0]["completionOffset"],
        540,
        abs_tol=EPSILON,
    )
    assert result["jobs"][0]["dueResult"] == "On time"


def test_stable_ties_and_booking_rules_allow_parallel_centers() -> None:
    result = _run_case("stable_dispatch_and_parallel_centers")
    operations = result["operations"]

    assert [operation["jobId"] for operation in operations] == [
        "JOB-TWO",
        "JOB-ONE",
    ]
    assert [operation["dispatchIndex"] for operation in operations] == [0, 1]
    assert [operation["startOffset"] for operation in operations] == [0, 0]
    assert operations[0]["workCenter"] != operations[1]["workCenter"]
    assert operations[0]["operatorId"] != operations[1]["operatorId"]


def test_scheduled_output_invariants() -> None:
    for case_name in [
        "normal_schedule",
        "lunch_window_split",
        "split_finishes_earlier",
        "shift_capacity",
        "parent_child_barrier",
        "overnight_shift",
        "stable_dispatch_and_parallel_centers",
    ]:
        result = _run_case(case_name)
        scheduled = [
            operation
            for operation in result["operations"]
            if operation["statusCode"] == "scheduled"
        ]

        for operation in scheduled:
            assert isinstance(operation["quantity"], int)
            assert operation["quantity"] > 0
            assert operation["startOffset"] >= 0
            assert operation["endOffset"] <= (
                result["shift"]["availableHours"] * 60 + EPSILON
            )
            assert math.isclose(
                operation["endOffset"] - operation["startOffset"],
                operation["durationMinutes"],
                abs_tol=EPSILON,
            )
            assert math.isclose(
                operation["durationMinutes"],
                operation["quantity"]
                * operation["p75HoursPerPiece"]
                * 60,
                abs_tol=EPSILON,
            )

        for resource_field in ("operatorId", "workCenter"):
            bookings: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for operation in scheduled:
                bookings[operation[resource_field]].append(
                    (operation["startOffset"], operation["endOffset"])
                )
            for resource_bookings in bookings.values():
                resource_bookings.sort()
                for previous, current in pairwise(resource_bookings):
                    assert previous[1] <= current[0] + EPSILON

        route_groups: dict[
            tuple[str, int], list[dict[str, Any]]
        ] = defaultdict(list)
        for operation in result["operations"]:
            if operation["routeSequence"] is not None:
                route_groups[
                    (operation["jobId"], operation["routeIndex"])
                ].append(operation)
        for groups in route_groups.values():
            groups.sort(key=lambda operation: operation["subBatchIndex"])
            assert sum(group["quantity"] for group in groups) == groups[0][
                "originalQuantity"
            ]
            if groups[0]["batchSplit"]:
                assert [group["subBatchIndex"] for group in groups] == list(
                    range(1, groups[0]["subBatchCount"] + 1)
                )
                assert all(
                    group["subBatchCount"] == len(groups) for group in groups
                )
            all_unit_indexes = sorted(
                unit_index
                for group in groups
                for unit_index in group["unitIndexes"]
            )
            assert all_unit_indexes == list(
                range(1, int(groups[0]["originalQuantity"]) + 1)
            )

        assert result["summary"]["scheduledCount"] == len(scheduled)
        assert result["summary"]["operationCount"] == len(result["operations"])
        assert result["summary"]["transferCount"] == len(
            result["transferFlows"]
        )
        if scheduled:
            assert math.isclose(
                result["summary"]["completionOffset"],
                max(operation["endOffset"] for operation in scheduled),
                abs_tol=EPSILON,
            )


def test_time_helpers_and_operator_key_match_browser_contract() -> None:
    assert parse_typed_time("5") == "05:00"
    assert parse_typed_time("5:7 p.m.") == "17:07"
    assert parse_typed_time("1530") == "15:30"
    assert parse_typed_time("13pm") is None
    assert parse_typed_time("24:00") is None
    assert time_to_minutes("5:07") == 307
    assert time_to_minutes("05:7") is None
    assert time_to_minutes("24:00") is None
    assert operator_key(
        {
            "schedulerKey": "stable-key",
            "employeeId": "ignored-id",
            "name": "Ignored Name",
        }
    ) == "stable-key"
    assert operator_key({"employeeId": "EMP-007", "name": "Employee 7"}) == (
        "EMP-007"
    )
    assert operator_key({"name": "Employee 8"}) == "name:Employee 8"


def test_invalid_input_returns_errors_without_partial_schedule() -> None:
    case = CASES["normal_schedule"]
    invalid_input = {
        "shiftId": 1,
        "jobs": [
            {
                "id": "BAD-JOB",
                "part": "PART-A",
                "quantity": 1.5,
                "priority": 9,
                "dueTime": "25:00",
            }
        ],
    }
    result = run(_data_for(case), invalid_input)

    assert result["errors"] == [
        "Job BAD-JOB: quantity must be a positive whole number.",
        "Job BAD-JOB: priority must be from 1 (highest) to 5.",
        "Job BAD-JOB: due time is invalid.",
    ]
    assert result["transferFlows"] == []
    assert result["summary"]["transferCount"] == 0
    assert result["operations"] == []
    assert result["jobs"] == []


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    """Expose the function-style suite to the standard-library runner."""

    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite


if __name__ == "__main__":
    unittest.main()
