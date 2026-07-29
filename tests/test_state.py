from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shift_scheduler import state  # noqa: E402


def sample_baseline() -> dict:
    return {
        "schemaVersion": 2,
        "source": {"dataClassification": "Synthetic demonstration data"},
        "settings": {
            "minimumSoloSkillLevel": 2,
            "defaultBreaks": {
                "1": {
                    "label": "Meal",
                    "startTime": "12:00",
                    "durationMinutes": 30,
                }
            },
            "shifts": [
                {
                    "id": 1,
                    "startMinutes": 480,
                    "endMinutes": 960,
                    "startTime": "08:00",
                    "endTime": "16:00",
                    "availableHours": 8,
                    "crossesMidnight": False,
                },
                {
                    "id": 2,
                    "startMinutes": 960,
                    "endMinutes": 0,
                    "startTime": "16:00",
                    "endTime": "00:00",
                    "availableHours": 8,
                    "crossesMidnight": True,
                },
            ],
        },
        "skillNames": ["Assembly", "Inspection"],
        "operators": [
            {
                "employeeId": "EMP-001",
                "name": "Employee 1",
                "defaultShift": 1,
                "defaultScheduled": True,
                "defaultPresent": True,
                "defaultHoursOverride": None,
                "defaultAvailableHours": 8,
                "skills": {"Assembly": 3, "Inspection": 2},
            },
            {
                "employeeId": "EMP-002",
                "name": "Employee 2",
                "defaultShift": 2,
                "defaultScheduled": True,
                "defaultPresent": True,
                "defaultHoursOverride": None,
                "defaultAvailableHours": 8,
                "skills": {"Assembly": 2, "Inspection": 3},
            },
        ],
        "routes": [
            {
                "part": "PART-001",
                "workCenter": "CENTER-1",
                "skill": "Assembly",
            }
        ],
        "parentChild": [],
    }


def sample_job() -> dict:
    return {
        "uid": "job-1",
        "id": "JOB-01",
        "part": "PART-001",
        "quantity": 2,
        "priority": 1,
        "dueTime": "14:00",
        "materialReady": True,
    }


class BaselineTests(unittest.TestCase):
    def test_normalize_is_a_deep_clone_and_builds_stable_keys(self) -> None:
        original = sample_baseline()
        original["operators"][0].pop("schedulerKey", None)

        normalized = state.normalize_baseline(original)
        normalized["operators"][0]["skills"]["Assembly"] = 0

        self.assertEqual("id:EMP-001", normalized["operators"][0]["schedulerKey"])
        self.assertEqual(3, original["operators"][0]["skills"]["Assembly"])
        self.assertEqual(
            "name:employee 3",
            state.deterministic_operator_key({"name": " Employee   3 "}),
        )

    def test_rejects_future_schema_and_duplicate_identity(self) -> None:
        future = sample_baseline()
        future["schemaVersion"] = 99
        with self.assertRaises(state.StateValidationError):
            state.normalize_baseline(future)

        duplicate = sample_baseline()
        duplicate["operators"][1]["employeeId"] = "emp-001"
        with self.assertRaisesRegex(state.StateValidationError, "Duplicate Employee ID"):
            state.normalize_baseline(duplicate)

    def test_compose_uses_imported_baseline_then_overlay_without_mutation(self) -> None:
        factory = sample_baseline()
        imported = sample_baseline()
        imported["operators"][0]["skills"]["Assembly"] = 1
        overlay = {
            "overrides": {
                "id:EMP-001": {
                    "name": "Employee 10",
                    "skills": {"Inspection": 3},
                }
            },
            "additions": [
                {
                    "employeeId": "EMP-003",
                    "name": "Employee 3",
                    "defaultShift": 1,
                    "defaultScheduled": True,
                    "defaultPresent": True,
                    "defaultHoursOverride": None,
                    "defaultAvailableHours": 8,
                    "skills": {"Assembly": 2, "Inspection": 2},
                }
            ],
        }
        before = deepcopy(overlay)

        composed = state.compose_baseline(factory, imported, overlay)
        by_key = {operator["schedulerKey"]: operator for operator in composed["operators"]}

        self.assertEqual(1, by_key["id:EMP-001"]["skills"]["Assembly"])
        self.assertEqual(3, by_key["id:EMP-001"]["skills"]["Inspection"])
        self.assertIn("id:EMP-003", by_key)
        self.assertEqual(before, overlay)
        self.assertEqual("Employee 1", imported["operators"][0]["name"])


class EmployeeOverlayTests(unittest.TestCase):
    def test_addition_is_validated_and_has_deterministic_key(self) -> None:
        baseline = sample_baseline()
        employee = {
            "employeeId": "EMP-003",
            "name": "Employee 3",
            "defaultShift": 1,
            "skills": {"Assembly": 2, "Inspection": 0},
        }

        first = state.add_employee_to_overlay(baseline, None, employee)
        second = state.add_employee_to_overlay(baseline, None, employee)

        self.assertEqual("id:EMP-003", first["additions"][0]["schedulerKey"])
        self.assertEqual(
            first["additions"][0]["schedulerKey"],
            second["additions"][0]["schedulerKey"],
        )

    def test_addition_rejects_missing_or_duplicate_identity_and_bad_skills(self) -> None:
        baseline = sample_baseline()
        with self.assertRaisesRegex(state.StateValidationError, "Employee ID"):
            state.validate_employee_addition(
                baseline,
                None,
                {"name": "Employee 3", "defaultShift": 1, "skills": {}},
            )
        with self.assertRaisesRegex(state.StateValidationError, "already in use"):
            state.validate_employee_addition(
                baseline,
                None,
                {
                    "employeeId": "emp-001",
                    "name": "Employee 3",
                    "defaultShift": 1,
                    "skills": {},
                },
            )
        with self.assertRaisesRegex(state.StateValidationError, "0 to 3"):
            state.validate_employee_addition(
                baseline,
                None,
                {
                    "employeeId": "EMP-003",
                    "name": "Employee 3",
                    "defaultShift": 1,
                    "skills": {"Assembly": 4},
                },
            )

    def test_partial_edit_keeps_other_skills_and_updates_shift_hours(self) -> None:
        baseline = sample_baseline()
        baseline["settings"]["shifts"][1]["availableHours"] = 6

        overlay = state.edit_employee_overlay(
            baseline,
            None,
            "id:EMP-001",
            {"defaultShift": 2, "skills": {"Inspection": 1}},
        )
        composed = state.compose_baseline(baseline, None, overlay)
        employee = next(
            operator
            for operator in composed["operators"]
            if operator["schedulerKey"] == "id:EMP-001"
        )

        self.assertEqual(3, employee["skills"]["Assembly"])
        self.assertEqual(1, employee["skills"]["Inspection"])
        self.assertEqual(6, employee["defaultAvailableHours"])


class AttendanceTests(unittest.TestCase):
    def test_plan_key_is_strict(self) -> None:
        self.assertEqual(
            "2026-08-01|2", state.attendance_plan_key("2026-08-01", 2)
        )
        self.assertEqual(
            ("2026-08-01", 2),
            state.parse_attendance_plan_key("2026-08-01|2"),
        )
        with self.assertRaises(state.StateValidationError):
            state.attendance_plan_key("2026-02-30", 1)

    def test_imported_false_and_zero_defaults_are_honored(self) -> None:
        baseline = sample_baseline()
        employee = baseline["operators"][0]
        employee["defaultScheduled"] = False
        employee["defaultPresent"] = True
        employee["defaultHoursOverride"] = 0
        employee["defaultAvailableHours"] = 0

        record = state.default_attendance_for_shift(baseline, 1)["id:EMP-001"]

        self.assertFalse(record["scheduled"])
        self.assertFalse(record["present"])
        self.assertEqual(0, record["hoursOverride"])
        self.assertEqual(0, record["availableHours"])
        self.assertEqual("08:00", record["startTime"])
        self.assertEqual("08:00", record["endTime"])
        self.assertFalse(record["mealEnabled"])

    def test_present_default_and_available_hours_are_not_dead_fields(self) -> None:
        baseline = sample_baseline()
        employee = baseline["operators"][0]
        employee["defaultPresent"] = False
        employee["defaultHoursOverride"] = None
        employee["defaultAvailableHours"] = 5.5

        record = state.default_attendance_for_shift(baseline, 1)["id:EMP-001"]

        self.assertTrue(record["scheduled"])
        self.assertFalse(record["present"])
        self.assertEqual(5.5, record["availableHours"])
        self.assertEqual("13:30", record["endTime"])

    def test_ensure_attendance_adds_missing_operators_and_migrates_hours(self) -> None:
        baseline = sample_baseline()
        scheduler_state = state.new_state(
            production_date="2026-08-01", shift_id=1
        )
        scheduler_state["attendanceByPlan"] = {
            "2026-08-01|1": {
                "id:EMP-001": {"present": True, "hours": 3}
            }
        }

        attendance = state.ensure_attendance(scheduler_state, baseline)

        self.assertEqual("11:00", attendance["id:EMP-001"]["endTime"])
        self.assertEqual(3, attendance["id:EMP-001"]["hoursOverride"])
        self.assertIn("id:EMP-002", attendance)
        self.assertIn("2026-08-01|1", scheduler_state["attendanceByPlan"])


class PersistenceTests(unittest.TestCase):
    def test_more_than_maximum_saved_jobs_is_rejected(self) -> None:
        jobs = []
        for index in range(state.MAX_SAVED_JOBS + 1):
            job = sample_job()
            job["uid"] = f"job-{index + 1}"
            job["id"] = f"JOB-{index + 1:02d}"
            jobs.append(job)

        with self.assertRaisesRegex(
            state.StateValidationError,
            f"no more than {state.MAX_SAVED_JOBS}",
        ):
            state.new_state(production_date="2026-08-01", jobs=jobs)

    def test_save_and_load_round_trip_atomically(self) -> None:
        scheduler_state = state.new_state(
            production_date="2026-08-01",
            shift_id=2,
            jobs=[sample_job()],
            ui_theme="dark",
        )
        scheduler_state["result"] = {"scheduled": 2}
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "nested" / "state.json"

            saved_path = state.save_state(scheduler_state, target)
            loaded = state.load_state(target)

            self.assertEqual(target, saved_path)
            self.assertEqual(scheduler_state, loaded)
            self.assertEqual("dark", loaded["uiTheme"])
            self.assertEqual([], list(target.parent.glob("*.tmp")))

    def test_theme_defaults_for_legacy_state_and_rejects_unknown_values(self) -> None:
        legacy = state.new_state(production_date="2026-08-01")
        legacy.pop("uiTheme")

        self.assertEqual("light", state.normalize_state(legacy)["uiTheme"])
        legacy["uiTheme"] = "neon"
        with self.assertRaisesRegex(state.StateValidationError, "light or dark"):
            state.normalize_state(legacy)

    def test_environment_directory_and_store_wrapper(self) -> None:
        scheduler_state = state.new_state(production_date="2026-08-01")
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(
                os.environ,
                {state.STATE_DIRECTORY_ENV: temporary_directory},
                clear=False,
            ):
                store = state.StateStore()
                self.assertEqual(
                    Path(temporary_directory) / state.DEFAULT_STATE_FILENAME,
                    store.path,
                )
                store.save(scheduler_state)
                self.assertEqual(scheduler_state, store.load())

    def test_corrupt_or_wrong_version_file_returns_independent_fallback(self) -> None:
        fallback = state.new_state(
            production_date="2026-08-01", jobs=[sample_job()]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "state.json"
            target.write_text("{broken", encoding="utf-8")
            loaded = state.load_state(target, fallback=fallback)
            loaded["jobs"].clear()
            self.assertEqual(1, len(fallback["jobs"]))

            wrong_version = deepcopy(fallback)
            wrong_version["version"] = 999
            target.write_text(json.dumps(wrong_version), encoding="utf-8")
            loaded = state.load_state(target, fallback=fallback)
            self.assertEqual(fallback, loaded)


class ResetAndSnapshotTests(unittest.TestCase):
    def populated_state(self) -> dict:
        scheduler_state = state.new_state(
            production_date="2026-08-01", shift_id=1, jobs=[sample_job()]
        )
        scheduler_state["attendanceByPlan"] = {
            "2026-08-01|1": {
                "id:EMP-001": {
                    "present": True,
                    "startTime": "08:00",
                    "endTime": "12:00",
                }
            }
        }
        scheduler_state["employeeOverlay"] = {
            "overrides": {"id:EMP-001": {"name": "Employee 10"}},
            "additions": [],
        }
        scheduler_state["result"] = {"scheduled": 1}
        return scheduler_state

    def test_import_and_resets_preserve_jobs_but_clear_local_plan_state(self) -> None:
        original = self.populated_state()
        imported = state.import_baseline(
            original,
            sample_baseline(),
            metadata={"fileName": "updated_data.xlsx"},
        )

        self.assertEqual(original["jobs"], imported["jobs"])
        self.assertEqual({}, imported["attendanceByPlan"])
        self.assertEqual(state.empty_employee_overlay(), imported["employeeOverlay"])
        self.assertIsNone(imported["result"])
        self.assertIsNone(original["importedBaseline"])

        imported["employeeOverlay"] = {
            "overrides": {"id:EMP-001": {"name": "Employee 10"}},
            "additions": [],
        }
        imported["attendanceByPlan"] = deepcopy(original["attendanceByPlan"])
        imported["result"] = {"scheduled": 1}
        reset = state.reset_to_imported_baseline(imported)

        self.assertEqual(original["jobs"], reset["jobs"])
        self.assertIsNotNone(reset["importedBaseline"])
        self.assertEqual({}, reset["attendanceByPlan"])
        self.assertEqual(state.empty_employee_overlay(), reset["employeeOverlay"])
        self.assertIsNone(reset["result"])

    def test_clear_plan_keeps_master_data_and_overlay(self) -> None:
        scheduler_state = state.import_baseline(
            self.populated_state(), sample_baseline()
        )
        scheduler_state["employeeOverlay"] = {
            "overrides": {"id:EMP-001": {"name": "Employee 10"}},
            "additions": [],
        }
        scheduler_state["jobs"] = [sample_job()]
        scheduler_state["attendanceByPlan"] = deepcopy(
            self.populated_state()["attendanceByPlan"]
        )
        scheduler_state["result"] = {"scheduled": 1}

        cleared = state.clear_plan(scheduler_state)

        self.assertEqual([], cleared["jobs"])
        self.assertEqual({}, cleared["attendanceByPlan"])
        self.assertIsNone(cleared["result"])
        self.assertIsNotNone(cleared["importedBaseline"])
        self.assertEqual(
            scheduler_state["employeeOverlay"], cleared["employeeOverlay"]
        )

    def test_snapshot_round_trip_replaces_plan_not_master_data(self) -> None:
        source = self.populated_state()
        source["importedBaseline"] = state.normalize_baseline(sample_baseline())
        exported = state.export_plan_snapshot(source)

        destination = state.new_state(
            production_date="2026-09-01", shift_id=2
        )
        destination["importedBaseline"] = state.normalize_baseline(sample_baseline())
        destination["importedMeta"] = {"fileName": "active_data.xlsx"}
        destination["employeeOverlay"] = {
            "overrides": {"id:EMP-002": {"name": "Employee 20"}},
            "additions": [],
        }
        destination["result"] = {"stale": True}
        imported = state.import_plan_snapshot(
            destination, exported, active_baseline=sample_baseline()
        )

        self.assertEqual("2026-08-01", imported["productionDate"])
        self.assertEqual(1, imported["shiftId"])
        self.assertEqual(source["jobs"], imported["jobs"])
        self.assertIn("2026-08-01|1", imported["attendanceByPlan"])
        self.assertEqual(
            destination["importedBaseline"], imported["importedBaseline"]
        )
        self.assertEqual(
            destination["employeeOverlay"], imported["employeeOverlay"]
        )
        self.assertIsNone(imported["result"])

    def test_snapshot_schema_and_version_are_strict(self) -> None:
        snapshot = state.build_plan_snapshot(self.populated_state())
        snapshot["version"] = 99
        with self.assertRaises(state.StateValidationError):
            state.parse_plan_snapshot(snapshot)
        snapshot["version"] = state.PLAN_SNAPSHOT_VERSION
        snapshot["schema"] = "different-plan"
        with self.assertRaises(state.StateValidationError):
            state.parse_plan_snapshot(snapshot)

    def test_scheduler_state_wrapper(self) -> None:
        wrapper = state.SchedulerState(
            sample_baseline(),
            state.new_state(production_date="2026-08-01", jobs=[sample_job()]),
        )

        attendance = wrapper.ensure_attendance()
        snapshot = wrapper.export_plan(indent=None)
        wrapper.clear_plan()
        wrapper.import_plan(snapshot)

        self.assertIn("id:EMP-001", attendance)
        self.assertEqual(1, len(wrapper.jobs))


if __name__ == "__main__":
    unittest.main()
