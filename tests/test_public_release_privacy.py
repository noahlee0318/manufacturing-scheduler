"""Public-release checks for branding and synthetic operator identities."""

from __future__ import annotations

import json
import math
import posixpath
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = PROJECT_ROOT / "data" / "public_baseline.json"
WORKBOOK_PATH = (
    PROJECT_ROOT / "templates" / "manufacturing_scheduler_template.xlsx"
)

TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
OFFICE_TEXT_SUFFIXES = {".csv", ".json", ".rels", ".txt", ".xml"}
TEXT_FILENAMES = {".gitignore", "LICENSE"}
SKIPPED_DIRECTORIES = {"__pycache__", ".git"}

# Keep the release gate itself free of a literal copy of the retired branding.
FORBIDDEN_BRAND_INDICATORS = (
    "".join(("bar", "nes")),
    "".join(("aero", "space")),
)

SHEET_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
OFFICE_RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
RETIRED_WAIT_ALL_TEXT = (
    "next route step "
    + "waits for all prior-step "
    + "sub-batches"
)


def _contains_forbidden_indicator(text: str) -> list[str]:
    folded = text.casefold()
    return [
        indicator
        for indicator in FORBIDDEN_BRAND_INDICATORS
        if indicator in folded
    ]


def _iter_public_files() -> list[Path]:
    return sorted(
        path
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file()
        and not any(part in SKIPPED_DIRECTORIES for part in path.parts)
    )


def _cell_column(reference: str) -> str:
    match = re.match(r"([A-Z]+)", reference)
    if not match:
        raise AssertionError(f"Invalid worksheet cell reference: {reference}")
    return match.group(1)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    member = "xl/sharedStrings.xml"
    if member not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(member))
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{SHEET_NAMESPACE}}}t"))
        for item in root.findall(f"{{{SHEET_NAMESPACE}}}si")
    ]


def _worksheet_member(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationship_id = None
    for sheet in workbook.findall(f".//{{{SHEET_NAMESPACE}}}sheet"):
        if sheet.get("name") == sheet_name:
            relationship_id = sheet.get(
                f"{{{OFFICE_RELATIONSHIP_NAMESPACE}}}id"
            )
            break
    if not relationship_id:
        raise AssertionError(f'Workbook is missing sheet "{sheet_name}".')

    relationships = ET.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    target = None
    for relationship in relationships.findall(
        f"{{{RELATIONSHIP_NAMESPACE}}}Relationship"
    ):
        if relationship.get("Id") == relationship_id:
            target = relationship.get("Target")
            break
    if not target:
        raise AssertionError(
            f'Workbook relationship for "{sheet_name}" is missing.'
        )
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _worksheet_rows(
    workbook_path: Path, sheet_name: str
) -> list[dict[str, str | None]]:
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = _shared_strings(archive)
        root = ET.fromstring(archive.read(_worksheet_member(archive, sheet_name)))

    rows: list[dict[str, str | None]] = []
    for row in root.findall(f".//{{{SHEET_NAMESPACE}}}row"):
        values: dict[str, str | None] = {}
        for cell in row.findall(f"{{{SHEET_NAMESPACE}}}c"):
            column = _cell_column(str(cell.get("r") or ""))
            cell_type = cell.get("t")
            value_node = cell.find(f"{{{SHEET_NAMESPACE}}}v")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or ""
                    for node in cell.findall(
                        f".//{{{SHEET_NAMESPACE}}}t"
                    )
                )
            elif value_node is None:
                value = None
            elif cell_type == "s":
                value = shared_strings[int(value_node.text or "0")]
            else:
                value = value_node.text
            values[column] = value
        rows.append(values)
    return rows


def _records_after_header(
    rows: list[dict[str, str | None]],
    required_headers: set[str],
) -> list[dict[str, str | None]]:
    header_index = None
    header_by_column: dict[str, str] = {}
    for index, row in enumerate(rows):
        values = {str(value) for value in row.values() if value is not None}
        if required_headers <= values:
            header_index = index
            header_by_column = {
                column: str(value)
                for column, value in row.items()
                if value is not None
            }
            break
    if header_index is None:
        raise AssertionError(
            f"Workbook header not found: {sorted(required_headers)}"
        )

    records = []
    for row in rows[header_index + 1 :]:
        record = {
            header: row.get(column)
            for column, header in header_by_column.items()
        }
        if record.get("Employee ID") or record.get("Employee Name"):
            records.append(record)
    return records


class PublicReleasePrivacyTests(unittest.TestCase):
    def test_deliverable_text_and_office_xml_have_no_brand_indicators(self) -> None:
        findings: list[str] = []
        for path in _iter_public_files():
            relative = path.relative_to(PROJECT_ROOT)
            for indicator in _contains_forbidden_indicator(relative.as_posix()):
                findings.append(f"{relative}: path contains {indicator!r}")

            if path.suffix.casefold() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
                text = path.read_text(encoding="utf-8", errors="replace")
                for indicator in _contains_forbidden_indicator(text):
                    findings.append(f"{relative}: text contains {indicator!r}")

            if path.suffix.casefold() not in {".docx", ".xlsx"}:
                continue
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    for indicator in _contains_forbidden_indicator(member):
                        findings.append(
                            f"{relative}!{member}: path contains {indicator!r}"
                        )
                    if Path(member).suffix.casefold() not in OFFICE_TEXT_SUFFIXES:
                        continue
                    text = archive.read(member).decode("utf-8", errors="replace")
                    for indicator in _contains_forbidden_indicator(text):
                        findings.append(
                            f"{relative}!{member}: text contains {indicator!r}"
                        )

        self.assertEqual([], findings, "\n".join(findings))

    def test_release_has_no_retired_wait_for_full_batch_rule(self) -> None:
        findings: list[str] = []
        for path in _iter_public_files():
            relative = path.relative_to(PROJECT_ROOT)
            if path.suffix.casefold() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
                text = path.read_text(encoding="utf-8", errors="replace")
                if RETIRED_WAIT_ALL_TEXT.casefold() in text.casefold():
                    findings.append(str(relative))
            if path.suffix.casefold() not in {".docx", ".xlsx"}:
                continue
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if Path(member).suffix.casefold() not in OFFICE_TEXT_SUFFIXES:
                        continue
                    text = archive.read(member).decode("utf-8", errors="replace")
                    if RETIRED_WAIT_ALL_TEXT.casefold() in text.casefold():
                        findings.append(f"{relative}!{member}")
        self.assertEqual([], findings, "\n".join(findings))

    def test_template_has_only_the_six_public_control_sheets(self) -> None:
        with zipfile.ZipFile(WORKBOOK_PATH) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet_names = [
            str(sheet.get("name"))
            for sheet in workbook.findall(f".//{{{SHEET_NAMESPACE}}}sheet")
        ]
        self.assertEqual(
            [
                "START HERE",
                "Approved Routes",
                "Operator Skills",
                "Attendance",
                "Parent-Child",
                "Settings",
            ],
            sheet_names,
        )

    def test_public_baseline_uses_only_sequential_generic_operators(self) -> None:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        operators = baseline["operators"]
        skill_names = baseline["skillNames"]

        self.assertEqual(14, len(operators))
        self.assertEqual(14, len(skill_names))
        for number, operator in enumerate(operators, start=1):
            expected_id = f"EMP-{number:03d}"
            with self.subTest(employee_id=expected_id):
                self.assertEqual(expected_id, operator["employeeId"])
                self.assertEqual(f"Employee {number}", operator["name"])
                self.assertEqual(f"id:{expected_id}", operator["schedulerKey"])
                self.assertEqual(set(skill_names), set(operator["skills"]))
                self.assertTrue(operator["skills"])
                self.assertTrue(
                    all(
                        isinstance(level, int) and 0 <= level <= 3
                        for level in operator["skills"].values()
                    )
                )

    def test_workbook_operator_rows_match_public_skill_vectors(self) -> None:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        skill_names = baseline["skillNames"]
        rows = _worksheet_rows(WORKBOOK_PATH, "Operator Skills")
        records = _records_after_header(
            rows, {"Employee ID", "Employee Name", *skill_names}
        )
        expected_by_id = {
            operator["employeeId"]: operator for operator in baseline["operators"]
        }

        self.assertEqual(set(expected_by_id), {
            record["Employee ID"] for record in records
        })
        for record in records:
            employee_id = str(record["Employee ID"])
            expected = expected_by_id[employee_id]
            with self.subTest(employee_id=employee_id):
                self.assertEqual(expected["name"], record["Employee Name"])
                workbook_skills: dict[str, int] = {}
                for skill_name in skill_names:
                    raw_level = record.get(skill_name)
                    self.assertIsNotNone(raw_level)
                    numeric_level = float(str(raw_level))
                    self.assertTrue(math.isfinite(numeric_level))
                    self.assertTrue(numeric_level.is_integer())
                    workbook_skills[skill_name] = int(numeric_level)
                self.assertEqual(expected["skills"], workbook_skills)

    def test_workbook_attendance_has_the_same_generic_roster(self) -> None:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        expected_roster = [
            (operator["employeeId"], operator["name"])
            for operator in baseline["operators"]
        ]
        rows = _worksheet_rows(WORKBOOK_PATH, "Attendance")
        records = _records_after_header(
            rows, {"Employee ID", "Employee Name"}
        )
        actual_roster = [
            (record["Employee ID"], record["Employee Name"])
            for record in records
        ]
        self.assertEqual(expected_roster, actual_roster)


if __name__ == "__main__":
    unittest.main()
