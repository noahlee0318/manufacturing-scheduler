"""Streamlit interface for the manufacturing shift scheduler."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shift_scheduler.scheduler import STATUS, run as run_scheduler  # noqa: E402
from shift_scheduler.state import (  # noqa: E402
    MAX_SAVED_JOBS,
    StateStore,
    StateValidationError,
    add_employee_to_overlay,
    attendance_plan_key,
    compose_baseline,
    edit_employee_overlay,
    ensure_attendance,
    export_plan_snapshot,
    import_baseline,
    import_plan_snapshot,
    new_state,
    normalize_state,
    operator_key,
    reset_to_factory_baseline,
    reset_to_imported_baseline,
)
from shift_scheduler.ui_helpers import (  # noqa: E402
    build_availability_windows,
    format_duration,
    format_offset,
    minutes_to_clock,
    parse_typed_time,
    safe_filename,
)
from shift_scheduler.workbook import (  # noqa: E402
    WorkbookValidationError,
    parse_workbook_bytes,
)


st.set_page_config(
    page_title="Manufacturing Shift Scheduler",
    layout="wide",
    initial_sidebar_state="expanded",
)

STATUS_ACTIONS = {
    "material_hold": "Mark material ready when the entered quantity is available.",
    "quantity_required": "Enter a positive whole-number quantity for the job.",
    "no_route": "Choose an approved complete part or update Approved Routes.",
    "p75_required": "Add an approved P75 override in the workbook and import it.",
    "skill_map_required": "Map the route to an approved skill and import it again.",
    "no_qualified_operator": (
        "Mark a qualified employee present or update a qualification to level 2 or 3."
    ),
    "shift_capacity": (
        "Completed whole-unit operations remain scheduled. Adjust availability, "
        "staffing, or priority, or move the remaining units to another plan."
    ),
    "blocked_child": (
        "Finish every unit of each entered child job before the parent can begin."
    ),
    "previous_unfinished": (
        "Resolve the earlier route operation for the same unit. Other units may "
        "continue after their own preceding steps finish."
    ),
}


def theme_css(theme: str) -> str:
    """Return the light/dark appearance layer used by the in-app toggle."""

    dark = str(theme).casefold() == "dark"
    background = "#0f0c18" if dark else "#f7f6fb"
    surface = "#191425" if dark else "#ffffff"
    raised = "#241c34" if dark else "#f0edf7"
    text = "#f6f2ff" if dark else "#1e1830"
    muted = "#c6bdd7" if dark else "#625a72"
    border = "#44365d" if dark else "#ddd7e8"
    accent = "#bda7ff" if dark else "#5d3fd3"
    return f"""
<style>
html {{
  color-scheme: {"dark" if dark else "light"};
}}
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"] {{
  background: {background} !important;
  color: {text} !important;
}}
[data-testid="stHeader"],
[data-testid="stSidebar"] > div:first-child {{
  background: {surface} !important;
}}
[data-testid="stSidebar"],
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
.stApp p,
.stApp li,
.stApp label,
.stApp h1,
.stApp h2,
.stApp h3 {{
  color: {text};
}}
.stApp [data-testid="stCaptionContainer"] p,
.stApp small {{
  color: {muted} !important;
}}
.stApp div[data-testid="stMetric"],
.stApp [data-testid="stDataFrame"],
.stApp [data-testid="stFileUploader"],
.stApp [data-baseweb="input"] > div,
.stApp [data-baseweb="select"] > div,
.stApp [data-baseweb="textarea"] {{
  background: {surface} !important;
  border-color: {border} !important;
  color: {text} !important;
}}
.stApp [data-baseweb="tab-list"],
.stApp [role="radiogroup"] {{
  background: {raised};
  border-radius: 0.6rem;
}}
.stApp [data-baseweb="tab"][aria-selected="true"] {{
  color: {accent} !important;
}}
.stApp a {{
  color: {accent};
}}
</style>
"""


@st.cache_data(show_spinner=False)
def load_factory_baseline() -> dict[str, Any]:
    """Load the packaged public baseline."""

    with (ROOT / "data" / "public_baseline.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def display_error(error: Exception | str) -> None:
    """Show a concise, user-facing error."""

    st.error(str(error))


def active_baseline(
    factory_baseline: dict[str, Any],
    state_value: dict[str, Any],
) -> dict[str, Any]:
    return compose_baseline(
        factory_baseline,
        state_value.get("importedBaseline"),
        state_value.get("employeeOverlay"),
    )


def initialize_state(factory_baseline: dict[str, Any]) -> tuple[StateStore, dict[str, Any]]:
    """Load persisted application state once per Streamlit session."""

    store = StateStore()
    if "scheduler_state" not in st.session_state:
        fallback = new_state(production_date=date.today().isoformat())
        loaded = store.load(fallback=fallback)
        loaded["result"] = None
        st.session_state.scheduler_state = loaded
        st.session_state.schedule_result = None
        st.session_state.ui_revision = 0

    state_value = normalize_state(st.session_state.scheduler_state)
    data = active_baseline(factory_baseline, state_value)
    valid_shift_ids = {int(shift["id"]) for shift in data["settings"]["shifts"]}
    if int(state_value["shiftId"]) not in valid_shift_ids:
        state_value["shiftId"] = min(valid_shift_ids)
        state_value["attendanceByPlan"] = {}
        state_value["result"] = None
        st.session_state.scheduler_state = state_value
    return store, state_value


def save_state(
    store: StateStore,
    state_value: dict[str, Any],
    *,
    clear_result: bool = True,
) -> bool:
    """Update session state and persist it when local storage is writable."""

    clean = normalize_state(state_value)
    clean["result"] = None
    st.session_state.scheduler_state = clean
    if clear_result:
        st.session_state.schedule_result = None
    try:
        store.save(clean)
    except OSError as error:
        st.warning(
            "The plan is available in this browser session, but local persistence "
            f"failed: {error}"
        )
        return False
    return True


def rerun_with_fresh_widgets() -> None:
    st.session_state.ui_revision = int(st.session_state.get("ui_revision", 0)) + 1
    st.rerun()


def render_theme_control(
    store: StateStore,
    state_value: dict[str, Any],
) -> None:
    """Render and persist the visible light/dark appearance control."""

    current = str(state_value.get("uiTheme") or "light").casefold()
    with st.sidebar:
        st.subheader("Appearance")
        selected = st.radio(
            "Color mode",
            options=["Light", "Dark"],
            index=1 if current == "dark" else 0,
            horizontal=True,
            label_visibility="collapsed",
            key="ui_theme_control",
        )
        st.divider()

    selected_theme = selected.casefold()
    if selected_theme != current:
        updated = deepcopy(state_value)
        updated["uiTheme"] = selected_theme
        save_state(store, updated, clear_result=False)
        state_value.clear()
        state_value.update(updated)
    st.markdown(theme_css(selected_theme), unsafe_allow_html=True)


def scalar(value: Any, fallback: Any = "") -> Any:
    """Turn pandas missing values into ordinary Python values."""

    try:
        if pd.isna(value):
            return fallback
    except (TypeError, ValueError):
        pass
    return value


def shift_label(shift: dict[str, Any]) -> str:
    suffix = " — verify placeholder" if shift.get("placeholder") else ""
    return (
        f"Shift {shift['id']} · {shift['startTime']}–{shift['endTime']}{suffix}"
    )


def rows_by_part(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for route in data["routes"]:
        grouped.setdefault(str(route["part"]), []).append(route)
    for routes in grouped.values():
        routes.sort(key=lambda row: float(row.get("effectiveSequence") or 0))
    return grouped


def demo_jobs(data: dict[str, Any], shift_id: int) -> list[dict[str, Any]]:
    """Choose small, schedulable examples from the active public baseline."""

    threshold = int(data["settings"].get("minimumSoloSkillLevel", 2))
    shift = next(item for item in data["settings"]["shifts"] if int(item["id"]) == shift_id)
    operators = [
        operator
        for operator in data["operators"]
        if int(operator.get("defaultShift") or 0) == shift_id
        and operator.get("defaultPresent", True)
    ]

    def viable(routes: list[dict[str, Any]]) -> bool:
        if not routes:
            return False
        total_minutes = 0.0
        for route in routes:
            standard = route.get("p75HoursPerPiece")
            skill = route.get("skill")
            if standard is None or not skill:
                return False
            total_minutes += float(standard) * 60
            if not any(
                int(operator.get("skills", {}).get(skill, 0)) >= threshold
                for operator in operators
            ):
                return False
        return total_minutes <= float(shift["availableHours"]) * 60 * 0.42

    grouped = rows_by_part(data)
    viable_parts = [part for part, routes in grouped.items() if viable(routes)]
    selected: list[str] = []

    for relationship in data.get("parentChild", []):
        parent = str(relationship.get("parent") or "")
        child = str(relationship.get("child") or "")
        if parent in viable_parts and child in viable_parts:
            selected = [child, parent]
            break
    if not selected:
        selected = viable_parts[:2]
    if not selected:
        selected = sorted(grouped)[:1]

    due_offsets = (0.55, 0.82)
    jobs: list[dict[str, Any]] = []
    for index, part in enumerate(selected):
        due_offset = due_offsets[min(index, len(due_offsets) - 1)]
        due_minutes = int(shift["startMinutes"]) + int(
            float(shift["availableHours"]) * 60 * due_offset
        )
        jobs.append(
            {
                "uid": f"demo-{index + 1}",
                "id": f"DEMO-{index + 1:02d}",
                "part": part,
                "quantity": 1,
                "priority": index + 1,
                "dueTime": minutes_to_clock(due_minutes),
                "materialReady": True,
                "notes": "Demonstration job",
            }
        )
    return jobs


def job_editor_rows(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Job ID": job.get("id", ""),
            "Complete Part": job.get("part", ""),
            "Quantity": int(job.get("quantity", 1)),
            "Priority": int(job.get("priority", 3)),
            "Due Time": job.get("dueTime", ""),
            "Material Ready": bool(job.get("materialReady", True)),
            "Notes": job.get("notes", ""),
        }
        for job in jobs
    ]


def parse_job_editor(table: pd.DataFrame) -> tuple[list[dict[str, Any]], list[str]]:
    jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()

    for row_index, row in enumerate(table.to_dict("records"), start=1):
        raw_values = [
            scalar(row.get("Job ID")),
            scalar(row.get("Complete Part")),
            scalar(row.get("Due Time")),
            scalar(row.get("Notes")),
        ]
        if not any(str(value).strip() for value in raw_values):
            continue

        job_id = str(scalar(row.get("Job ID"))).strip() or f"JOB-{row_index:02d}"
        part = str(scalar(row.get("Complete Part"))).strip()
        due_raw = str(scalar(row.get("Due Time"))).strip()
        due_time = parse_typed_time(due_raw) if due_raw else ""

        try:
            quantity_number = float(scalar(row.get("Quantity"), 0))
            quantity = int(quantity_number)
            if quantity_number != quantity or quantity < 1:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            quantity = 0
            errors.append(f"{job_id}: quantity must be a positive whole number.")

        try:
            priority_number = float(scalar(row.get("Priority"), 0))
            priority = int(priority_number)
            if priority_number != priority or priority < 1 or priority > 5:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            priority = 0
            errors.append(f"{job_id}: priority must be a whole number from 1 to 5.")

        if not part:
            errors.append(f"{job_id}: choose a complete part.")
        if due_raw and due_time is None:
            errors.append(f"{job_id}: enter a valid due time.")
        if job_id.casefold() in seen_ids:
            errors.append(f'Job ID "{job_id}" is duplicated.')
        seen_ids.add(job_id.casefold())

        jobs.append(
            {
                "uid": f"job-{row_index}",
                "id": job_id,
                "part": part,
                "quantity": quantity,
                "priority": priority,
                "dueTime": due_time or "",
                "materialReady": bool(scalar(row.get("Material Ready"), True)),
                "notes": str(scalar(row.get("Notes"))).strip(),
            }
        )
    if len(jobs) > MAX_SAVED_JOBS:
        excess = len(jobs) - MAX_SAVED_JOBS
        errors.append(
            f"Plans support at most {MAX_SAVED_JOBS} jobs. "
            f"Remove {excess} {'row' if excess == 1 else 'rows'} before "
            "saving or building the schedule."
        )
    return jobs, errors


def attendance_editor_rows(
    data: dict[str, Any],
    attendance: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for operator in data["operators"]:
        key = operator_key(operator)
        record = attendance[key]
        rows.append(
            {
                "Key": key,
                "Employee": operator["name"],
                "Employee ID": operator.get("employeeId") or "",
                "Scheduled": bool(record.get("scheduled", True)),
                "Present": bool(record.get("present", False)),
                "Work Start": record.get("startTime", ""),
                "Work End": record.get("endTime", ""),
                "Meal": bool(record.get("mealEnabled", False)),
                "Meal Start": record.get("mealStartTime", ""),
            }
        )
    return rows


def parse_attendance_editor(
    table: pd.DataFrame,
    shift: dict[str, Any],
    default_break: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    records: dict[str, dict[str, Any]] = {}
    scheduler_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for row in table.to_dict("records"):
        key = str(scalar(row.get("Key"))).strip()
        employee = str(scalar(row.get("Employee"))).strip() or key
        start_raw = str(scalar(row.get("Work Start"))).strip()
        end_raw = str(scalar(row.get("Work End"))).strip()
        meal_raw = str(scalar(row.get("Meal Start"))).strip()
        start_time = parse_typed_time(start_raw)
        end_time = parse_typed_time(end_raw)
        meal_enabled = bool(scalar(row.get("Meal"), False))
        meal_start = parse_typed_time(meal_raw) if meal_enabled else (
            parse_typed_time(meal_raw) or ""
        )
        scheduled = bool(scalar(row.get("Scheduled"), False))
        present = bool(scalar(row.get("Present"), False))

        if start_time is None:
            errors.append(f"{employee}: enter a valid work start time.")
        if end_time is None:
            errors.append(f"{employee}: enter a valid work end time.")
        if meal_enabled and meal_start is None:
            errors.append(f"{employee}: enter a valid meal start time.")

        record = {
            "scheduled": scheduled,
            "present": present,
            "startTime": start_time or shift["startTime"],
            "endTime": end_time or shift["endTime"],
            "mealEnabled": meal_enabled,
            "mealStartTime": meal_start or "",
        }
        working = scheduled and present
        windows: list[dict[str, float]] = []
        if start_time is not None and end_time is not None and (
            not meal_enabled or meal_start is not None
        ):
            windows, window_error = build_availability_windows(
                record, shift, default_break
            )
            if window_error:
                errors.append(f"{employee}: {window_error}")

        record["availableHours"] = sum(
            float(window["end"]) - float(window["start"]) for window in windows
        ) / 60
        record["hoursOverride"] = None
        records[key] = record
        scheduler_rows.append(
            {
                "operatorKey": key,
                "present": bool(working and windows),
                "availabilityWindows": windows if working else [],
            }
        )
    return records, scheduler_rows, errors


def operation_rows(
    result: dict[str, Any],
    production_date: str,
) -> list[dict[str, Any]]:
    shift = result["shift"]
    rows: list[dict[str, Any]] = []
    for operation in result.get("operations", []):
        batch = ""
        if operation.get("batchSplit"):
            batch = (
                f"{operation.get('subBatchIndex')}/{operation.get('subBatchCount')}"
            )
        unit_indexes = operation.get("unitIndexes") or []
        units = ", ".join(str(index) for index in unit_indexes)
        rows.append(
            {
                "Status": operation.get("statusLabel", ""),
                "Job": operation.get("jobId", ""),
                "Part": operation.get("part", ""),
                "Qty": operation.get("quantity", ""),
                "Units": units,
                "Sub-batch": batch,
                "Route": operation.get("routeSequence", ""),
                "Operation": operation.get("operation", ""),
                "Work Center": operation.get("workCenter", ""),
                "Skill": operation.get("skill", ""),
                "Employee": operation.get("operatorName") or "—",
                "Skill Level": operation.get("operatorSkill")
                if operation.get("operatorSkill") is not None
                else "—",
                "Start": format_offset(
                    production_date, shift, operation.get("startOffset")
                ),
                "Finish": format_offset(
                    production_date, shift, operation.get("endOffset")
                ),
                "Duration": format_duration(operation.get("durationMinutes")),
                "Explanation": operation.get("explanation", ""),
                "_statusCode": operation.get("statusCode", ""),
                "_startOffset": operation.get("startOffset"),
            }
        )
    return rows


def public_columns(table: pd.DataFrame) -> pd.DataFrame:
    return table[[column for column in table.columns if not column.startswith("_")]]


def render_sidebar(
    factory_baseline: dict[str, Any],
    store: StateStore,
    state_value: dict[str, Any],
    data: dict[str, Any],
) -> None:
    with st.sidebar:
        st.header("Data & files")
        imported_meta = state_value.get("importedMeta") or {}
        if state_value.get("importedBaseline") is not None:
            st.success("Imported workbook baseline active")
            if imported_meta.get("fileName"):
                st.caption(str(imported_meta["fileName"]))
            counts = imported_meta.get("counts") or {}
            if counts:
                st.caption(
                    f"{counts.get('completeParts', 0)} parts · "
                    f"{counts.get('routes', 0)} route rows · "
                    f"{counts.get('operators', 0)} employees"
                )
        else:
            st.info("Packaged public demo baseline active")
            st.caption(
                f"{len({route['part'] for route in data['routes']})} parts · "
                f"{len(data['routes'])} route rows · "
                f"{len(data['operators'])} employees"
            )

        template_path = ROOT / "templates" / "manufacturing_scheduler_template.xlsx"
        manual_path = ROOT / "docs" / "Manufacturing_Shift_Scheduler_Manual.docx"
        st.download_button(
            "Download Excel template",
            data=template_path.read_bytes(),
            file_name=template_path.name,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            use_container_width=True,
        )
        st.download_button(
            "Download user manual",
            data=manual_path.read_bytes(),
            file_name=manual_path.name,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            use_container_width=True,
        )

        uploaded = st.file_uploader(
            "Import scheduler workbook",
            type=["xlsx"],
            help=(
                "Uses the five controlled worksheets in the downloadable template. "
                "The uploaded file is validated before it replaces the active baseline."
            ),
        )
        if st.button(
            "Validate and use workbook",
            disabled=uploaded is None,
            use_container_width=True,
        ):
            try:
                assert uploaded is not None
                parsed = parse_workbook_bytes(uploaded.getvalue(), uploaded.name)
                updated = import_baseline(
                    state_value,
                    parsed["data"],
                    metadata={
                        "fileName": uploaded.name,
                        "counts": parsed["counts"],
                        "warnings": parsed["warnings"],
                    },
                )
                save_state(store, updated)
                st.session_state.import_notice = {
                    "counts": parsed["counts"],
                    "warnings": parsed["warnings"],
                }
                rerun_with_fresh_widgets()
            except (WorkbookValidationError, StateValidationError) as error:
                display_error(error)

        if notice := st.session_state.pop("import_notice", None):
            counts = notice["counts"]
            st.success(
                f"Imported {counts['routes']} route rows and "
                f"{counts['operators']} employees."
            )
            for warning in notice["warnings"]:
                st.warning(str(warning))

        if state_value.get("importedBaseline") is not None:
            if st.button(
                "Reset local edits to import",
                help=(
                    "Keeps the imported workbook and current jobs, but clears local "
                    "employee and attendance edits."
                ),
                use_container_width=True,
            ):
                try:
                    save_state(store, reset_to_imported_baseline(state_value))
                    rerun_with_fresh_widgets()
                except StateValidationError as error:
                    display_error(error)

            if st.button(
                "Use packaged demo data",
                help="Keeps current jobs but removes the workbook import and local edits.",
                use_container_width=True,
            ):
                save_state(store, reset_to_factory_baseline(state_value))
                rerun_with_fresh_widgets()

        st.divider()
        st.subheader("Portable plan")
        st.download_button(
            "Download plan snapshot",
            data=export_plan_snapshot(state_value),
            file_name=safe_filename(
                f"shift-plan-{state_value['productionDate']}.json"
            ),
            mime="application/json",
            use_container_width=True,
        )
        plan_upload = st.file_uploader(
            "Open plan snapshot",
            type=["json"],
            key="plan_snapshot_upload",
        )
        if st.button(
            "Load plan snapshot",
            disabled=plan_upload is None,
            use_container_width=True,
        ):
            try:
                assert plan_upload is not None
                updated = import_plan_snapshot(
                    state_value,
                    plan_upload.getvalue(),
                    active_baseline=data,
                )
                save_state(store, updated)
                rerun_with_fresh_widgets()
            except StateValidationError as error:
                display_error(error)

        st.caption(
            "Workbook imports update master data. Plan snapshots move daily jobs "
            "and attendance between installations."
        )


def render_plan(
    factory_baseline: dict[str, Any],
    store: StateStore,
    state_value: dict[str, Any],
    data: dict[str, Any],
) -> None:
    st.header("Plan a shift")
    st.caption(
        "Choose the shift, confirm attendance, enter complete-part jobs, then build "
        "a deterministic recommendation."
    )
    revision = int(st.session_state.get("ui_revision", 0))
    shifts = list(data["settings"]["shifts"])
    shift_ids = [int(shift["id"]) for shift in shifts]
    current_shift_id = int(state_value["shiftId"])
    selected_shift_index = (
        shift_ids.index(current_shift_id) if current_shift_id in shift_ids else 0
    )

    date_column, shift_column = st.columns([1, 2])
    with date_column:
        selected_date = st.date_input(
            "Production date",
            value=date.fromisoformat(state_value["productionDate"]),
            key=f"production_date_{revision}",
        )
    with shift_column:
        selected_shift = st.selectbox(
            "Shift",
            shifts,
            index=selected_shift_index,
            format_func=shift_label,
            key=f"shift_{revision}",
        )
    selected_shift_id = int(selected_shift["id"])
    selected_date_text = selected_date.isoformat()
    if selected_shift.get("placeholder"):
        st.warning(
            "This shift is a placeholder. Verify its start, finish, and available "
            "hours in the Excel Settings worksheet before operational use."
        )

    draft_state = deepcopy(state_value)
    draft_state["productionDate"] = selected_date_text
    draft_state["shiftId"] = selected_shift_id
    attendance = ensure_attendance(
        draft_state, data, selected_date_text, selected_shift_id
    )
    default_break = data["settings"].get("defaultBreaks", {}).get(
        str(selected_shift_id)
    )

    st.subheader("Attendance and work windows")
    meal_label = (default_break or {}).get("label", "Meal")
    if default_break:
        st.caption(
            f"The default {str(meal_label).lower()} is "
            f"{default_break['startTime']} for "
            f"{default_break['durationMinutes']} minutes. Each operation must fit "
            "inside one uninterrupted employee window."
        )
    else:
        st.caption(
            "No default meal is configured for this shift. Add one in the Excel "
            "Settings worksheet if required."
        )

    attendance_table = st.data_editor(
        pd.DataFrame(attendance_editor_rows(data, attendance)),
        hide_index=True,
        use_container_width=True,
        column_order=[
            "Employee",
            "Employee ID",
            "Scheduled",
            "Present",
            "Work Start",
            "Work End",
            "Meal",
            "Meal Start",
        ],
        disabled=["Employee", "Employee ID"],
        column_config={
            "Employee": st.column_config.TextColumn(width="medium"),
            "Employee ID": st.column_config.TextColumn(width="small"),
            "Scheduled": st.column_config.CheckboxColumn(
                help="Expected on this shift."
            ),
            "Present": st.column_config.CheckboxColumn(
                help="Available for assignment now."
            ),
            "Work Start": st.column_config.TextColumn(
                help="Examples: 5, 5:00 AM, 17:00."
            ),
            "Work End": st.column_config.TextColumn(
                help="May fall after midnight for a cross-midnight shift."
            ),
            "Meal": st.column_config.CheckboxColumn(label=str(meal_label)),
            "Meal Start": st.column_config.TextColumn(width="small"),
        },
        key=f"attendance_editor_{revision}_{selected_date_text}_{selected_shift_id}",
    )

    st.subheader("Jobs")
    st.caption(f"Enter up to {MAX_SAVED_JOBS} jobs in one saved plan.")
    part_options = sorted({str(route["part"]) for route in data["routes"]})
    jobs_table = st.data_editor(
        pd.DataFrame(
            job_editor_rows(state_value["jobs"]),
            columns=[
                "Job ID",
                "Complete Part",
                "Quantity",
                "Priority",
                "Due Time",
                "Material Ready",
                "Notes",
            ],
        ),
        hide_index=True,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Job ID": st.column_config.TextColumn(
                help="Must be unique within this plan.", width="small"
            ),
            "Complete Part": st.column_config.SelectboxColumn(
                options=part_options,
                required=True,
                width="medium",
            ),
            "Quantity": st.column_config.NumberColumn(
                min_value=1,
                step=1,
                format="%d",
                width="small",
            ),
            "Priority": st.column_config.NumberColumn(
                min_value=1,
                max_value=5,
                step=1,
                format="%d",
                help="1 is highest; 5 is lowest.",
                width="small",
            ),
            "Due Time": st.column_config.TextColumn(
                help="Optional. Examples: 2p, 14:00.", width="small"
            ),
            "Material Ready": st.column_config.CheckboxColumn(width="small"),
            "Notes": st.column_config.TextColumn(width="large"),
        },
        key=f"jobs_editor_{revision}",
    )

    jobs, job_errors = parse_job_editor(jobs_table)
    attendance_records, scheduler_attendance, attendance_errors = (
        parse_attendance_editor(attendance_table, selected_shift, default_break)
    )
    draft_errors = [*job_errors, *attendance_errors]

    button_columns = st.columns([1.2, 1.2, 1, 1])
    with button_columns[0]:
        save_clicked = st.button(
            "Save plan",
            use_container_width=True,
            help="Saves the date, shift, attendance, and jobs without scheduling.",
        )
    with button_columns[1]:
        build_clicked = st.button(
            "Build schedule",
            type="primary",
            use_container_width=True,
        )
    with button_columns[2]:
        example_clicked = st.button(
            "Load demo jobs",
            use_container_width=True,
        )
    with button_columns[3]:
        clear_clicked = st.button(
            "Clear jobs",
            use_container_width=True,
        )

    if example_clicked:
        updated = deepcopy(state_value)
        updated["productionDate"] = selected_date_text
        updated["shiftId"] = selected_shift_id
        updated["jobs"] = demo_jobs(data, selected_shift_id)
        updated["attendanceByPlan"] = draft_state["attendanceByPlan"]
        save_state(store, updated)
        rerun_with_fresh_widgets()

    if clear_clicked:
        updated = deepcopy(state_value)
        updated["productionDate"] = selected_date_text
        updated["shiftId"] = selected_shift_id
        updated["jobs"] = []
        updated["attendanceByPlan"] = draft_state["attendanceByPlan"]
        save_state(store, updated)
        rerun_with_fresh_widgets()

    if save_clicked or build_clicked:
        if draft_errors:
            for message in dict.fromkeys(draft_errors):
                st.error(message)
        elif not jobs and build_clicked:
            st.error("Enter at least one job before building a schedule.")
        else:
            updated = deepcopy(state_value)
            updated["productionDate"] = selected_date_text
            updated["shiftId"] = selected_shift_id
            updated["jobs"] = jobs
            plan_key = attendance_plan_key(selected_date_text, selected_shift_id)
            updated.setdefault("attendanceByPlan", {})[plan_key] = attendance_records
            save_state(store, updated, clear_result=not build_clicked)
            state_value.clear()
            state_value.update(updated)

            if build_clicked:
                result = run_scheduler(
                    data,
                    {
                        "shiftId": selected_shift_id,
                        "jobs": jobs,
                        "attendance": scheduler_attendance,
                    },
                )
                st.session_state.schedule_result = result
                if result["errors"]:
                    st.error("The schedule could not be built. Review the input errors below.")
                    for message in result["errors"]:
                        st.error(message)
                else:
                    st.success(
                        "Schedule built. Open the Schedule tab for the full result."
                    )
                    if result.get("transferFlows"):
                        transfer_count = len(result["transferFlows"])
                        st.info(
                            f"Unit-by-unit flow was used at {transfer_count} route "
                            f"{'transition' if transfer_count == 1 else 'transitions'}."
                        )
                    if result["batchSplits"]:
                        st.info(
                            "One or more routes produced multiple whole-unit display "
                            "blocks. They are labeled in the Schedule tab."
                        )
            else:
                st.success("Plan saved.")


def render_transfer_details(
    result: dict[str, Any],
    production_date: str,
) -> None:
    flows = result.get("transferFlows", [])
    if not flows:
        return
    job_count = len({flow["jobId"] for flow in flows})
    st.info(
        f"Unit-by-unit route flow applied: {len(flows)} route "
        f"{'transition released' if len(flows) == 1 else 'transitions released'} "
        "finished units downstream before the full upstream quantity was complete "
        f"across {job_count} {'job' if job_count == 1 else 'jobs'}. "
        "No unit skipped a route step."
    )
    with st.expander("Unit-flow transition details"):
        rows = []
        for flow in flows:
            rows.append(
                {
                    "Job": flow["jobId"],
                    "From operation": flow["priorPartOperation"],
                    "From center": flow["priorWorkCenter"],
                    "To operation": flow["partOperation"],
                    "To center": flow["workCenter"],
                    "Units released early": flow["quantityReleasedEarly"],
                    "First downstream start": format_offset(
                        production_date,
                        result["shift"],
                        flow["firstStartOffset"],
                    ),
                    "Full upstream finish": format_offset(
                        production_date,
                        result["shift"],
                        flow["priorBatchFinishOffset"],
                    ),
                }
            )
        st.dataframe(rows, hide_index=True, use_container_width=True)


def render_split_details(
    result: dict[str, Any],
    production_date: str,
) -> None:
    splits = result.get("batchSplits", [])
    if not splits:
        return
    st.info(
        f"Whole-unit display grouping produced multiple blocks for {len(splits)} "
        f"completed route {'step' if len(splits) == 1 else 'steps'}."
    )
    for split in splits:
        title = (
            f"{split['jobId']} · {split['partOperation']} · "
            f"quantity {split['originalQuantity']}"
        )
        with st.expander(title):
            chunks = []
            for chunk in split["chunks"]:
                chunks.append(
                    {
                        "Chunk": (
                            f"{chunk['index']} of {len(split['chunks'])}"
                        ),
                        "Quantity": chunk["quantity"],
                        "Employee": chunk["operatorName"],
                        "Work Center": split["workCenter"],
                        "Start": format_offset(
                            production_date, result["shift"], chunk["startOffset"]
                        ),
                        "Finish": format_offset(
                            production_date, result["shift"], chunk["endOffset"]
                        ),
                    }
                )
            st.dataframe(chunks, hide_index=True, use_container_width=True)


def render_schedule(state_value: dict[str, Any]) -> None:
    st.header("Schedule")
    result = st.session_state.get("schedule_result")
    if not result:
        st.info(
            "No schedule has been built in this session. Complete the Plan tab and "
            "select Build schedule."
        )
        return

    production_date = state_value["productionDate"]
    summary = result["summary"]
    complete_jobs = sum(
        job.get("statusCode") == "complete" for job in result.get("jobs", [])
    )
    metric_columns = st.columns(6)
    metric_columns[0].metric("Jobs", summary["jobCount"])
    metric_columns[1].metric("Complete jobs", complete_jobs)
    metric_columns[2].metric(
        "Issue rows", summary["unfinishedCount"] + summary["blockedCount"]
    )
    metric_columns[3].metric("Unit-flow transitions", summary["transferCount"])
    metric_columns[4].metric("Multi-block routes", summary["splitCount"])
    metric_columns[5].metric(
        "Last scheduled finish",
        format_offset(
            production_date, result["shift"], summary.get("completionOffset")
        ),
    )

    for message in result.get("errors", []):
        st.error(message)
    for warning in result.get("warnings", []):
        st.warning(str(warning.get("message") or warning))
    render_transfer_details(result, production_date)
    render_split_details(result, production_date)

    rows = operation_rows(result, production_date)
    if not rows:
        st.warning("No operation rows are available for this result.")
        return

    filter_value = st.radio(
        "Rows",
        options=["All", "Scheduled", "Issues"],
        index=0,
        horizontal=True,
    )
    if filter_value == "Scheduled":
        filtered = [row for row in rows if row["_statusCode"] == "scheduled"]
    elif filter_value == "Issues":
        filtered = [row for row in rows if row["_statusCode"] != "scheduled"]
    else:
        filtered = rows

    operations_tab, jobs_tab, centers_tab, employees_tab, issues_tab = st.tabs(
        ["Operations", "Jobs", "Work centers", "Employees", "Unfinished guide"]
    )
    with operations_tab:
        st.dataframe(
            public_columns(pd.DataFrame(filtered)),
            hide_index=True,
            use_container_width=True,
        )

    with jobs_tab:
        job_rows = []
        for job in result.get("jobs", []):
            job_rows.append(
                {
                    "Job": job["id"],
                    "Part": job["part"],
                    "Quantity": job["quantity"],
                    "Priority": job["priority"],
                    "Status": str(job["statusCode"]).replace("_", " ").title(),
                    "Due": job.get("dueTime") or "—",
                    "Completion": format_offset(
                        production_date,
                        result["shift"],
                        job.get("completionOffset"),
                    ),
                    "Due result": job.get("dueResult") or "—",
                    "Active child jobs": ", ".join(
                        job.get("activeChildJobIds", [])
                    )
                    or "—",
                }
            )
        st.dataframe(job_rows, hide_index=True, use_container_width=True)

    with centers_tab:
        center_rows = sorted(
            rows,
            key=lambda row: (
                str(row["Work Center"]),
                float("inf")
                if row["_startOffset"] is None
                else float(row["_startOffset"]),
            ),
        )
        st.dataframe(
            public_columns(pd.DataFrame(center_rows)),
            hide_index=True,
            use_container_width=True,
        )

    with employees_tab:
        employee_rows = sorted(
            [row for row in rows if row["_statusCode"] == "scheduled"],
            key=lambda row: (
                str(row["Employee"]),
                float(row["_startOffset"]),
            ),
        )
        if employee_rows:
            st.dataframe(
                public_columns(pd.DataFrame(employee_rows)),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("No employee assignments were made.")

    with issues_tab:
        issue_rows = [row for row in rows if row["_statusCode"] != "scheduled"]
        if issue_rows:
            st.dataframe(
                public_columns(pd.DataFrame(issue_rows)),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.success("Every operation was scheduled.")

        st.subheader("Status guide")
        guide = [
            {
                "Status": details["label"],
                "Meaning": details["message"],
                "What to do": STATUS_ACTIONS.get(code, ""),
                "Rows in result": sum(
                    row["_statusCode"] == code for row in rows
                ),
            }
            for code, details in STATUS.items()
            if code != "scheduled"
        ]
        st.dataframe(guide, hide_index=True, use_container_width=True)

    st.subheader("Export this result")
    export_rows = public_columns(pd.DataFrame(rows))
    file_stem = safe_filename(f"shift-schedule-{production_date}")
    export_columns = st.columns(2)
    export_columns[0].download_button(
        "Download operations CSV",
        data=export_rows.to_csv(index=False),
        file_name=f"{file_stem}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    export_columns[1].download_button(
        "Download full result JSON",
        data=json.dumps(result, indent=2, ensure_ascii=False),
        file_name=f"{file_stem}.json",
        mime="application/json",
        use_container_width=True,
    )


def render_employees(
    factory_baseline: dict[str, Any],
    store: StateStore,
    state_value: dict[str, Any],
    data: dict[str, Any],
) -> None:
    st.header("Employees and qualifications")
    st.caption(
        "Edits are stored locally as an overlay. The controlled Excel workbook "
        "remains unchanged."
    )
    threshold = int(data["settings"].get("minimumSoloSkillLevel", 2))
    st.info(
        f"Skill scale: 0 not qualified · 1 trainee · 2 independent · 3 trainer. "
        f"The active solo-work threshold is {threshold}."
    )

    mode = st.radio(
        "Action",
        ["Edit existing", "Add employee"],
        horizontal=True,
    )
    shifts = list(data["settings"]["shifts"])
    shift_ids = [int(shift["id"]) for shift in shifts]
    revision = int(st.session_state.get("ui_revision", 0))

    if mode == "Edit existing":
        operators = list(data["operators"])
        selected = st.selectbox(
            "Employee",
            operators,
            format_func=lambda employee: (
                f"{employee['name']} · {employee.get('employeeId') or 'No ID'}"
            ),
        )
        key = operator_key(selected)
        id_column, name_column, shift_column = st.columns([1, 2, 1])
        with id_column:
            employee_id = st.text_input(
                "Employee ID",
                value=selected.get("employeeId") or "",
                key=f"employee_id_{revision}_{key}",
            )
        with name_column:
            employee_name = st.text_input(
                "Display name",
                value=selected["name"],
                key=f"employee_name_{revision}_{key}",
            )
        with shift_column:
            default_shift = st.selectbox(
                "Default shift",
                shift_ids,
                index=shift_ids.index(int(selected["defaultShift"])),
                format_func=lambda value: f"Shift {value}",
                key=f"employee_shift_{revision}_{key}",
            )
        flag_columns = st.columns(2)
        default_scheduled = flag_columns[0].checkbox(
            "Scheduled by default",
            value=bool(selected.get("defaultScheduled", True)),
            key=f"employee_scheduled_{revision}_{key}",
        )
        default_present = flag_columns[1].checkbox(
            "Present by default",
            value=bool(selected.get("defaultPresent", True)),
            key=f"employee_present_{revision}_{key}",
        )
        skills_table = st.data_editor(
            pd.DataFrame(
                [
                    {
                        "Skill": skill,
                        "Level": int(selected.get("skills", {}).get(skill, 0)),
                    }
                    for skill in data["skillNames"]
                ]
            ),
            hide_index=True,
            use_container_width=True,
            disabled=["Skill"],
            column_config={
                "Skill": st.column_config.TextColumn(width="large"),
                "Level": st.column_config.NumberColumn(
                    min_value=0,
                    max_value=3,
                    step=1,
                    format="%d",
                    width="small",
                ),
            },
            key=f"skills_{revision}_{key}",
        )
        if st.button("Save employee changes", type="primary"):
            try:
                skills = {
                    str(row["Skill"]): int(row["Level"])
                    for row in skills_table.to_dict("records")
                }
                overlay = edit_employee_overlay(
                    factory_baseline,
                    state_value["employeeOverlay"],
                    key,
                    {
                        "employeeId": employee_id.strip() or None,
                        "name": employee_name,
                        "defaultShift": int(default_shift),
                        "defaultScheduled": default_scheduled,
                        "defaultPresent": default_present,
                        "skills": skills,
                    },
                    imported_baseline=state_value.get("importedBaseline"),
                )
                updated = deepcopy(state_value)
                updated["employeeOverlay"] = overlay
                updated["attendanceByPlan"] = {}
                save_state(store, updated)
                rerun_with_fresh_widgets()
            except (StateValidationError, TypeError, ValueError) as error:
                display_error(error)

    else:
        next_number = len(data["operators"]) + 1
        used_ids = {
            str(operator.get("employeeId") or "").casefold()
            for operator in data["operators"]
        }
        while f"emp-{next_number:03d}".casefold() in used_ids:
            next_number += 1
        id_column, name_column, shift_column = st.columns([1, 2, 1])
        with id_column:
            employee_id = st.text_input(
                "Employee ID",
                value=f"EMP-{next_number:03d}",
                key=f"new_employee_id_{revision}",
            )
        with name_column:
            employee_name = st.text_input(
                "Display name",
                value=f"Employee {next_number}",
                key=f"new_employee_name_{revision}",
            )
        with shift_column:
            default_shift = st.selectbox(
                "Default shift",
                shift_ids,
                format_func=lambda value: f"Shift {value}",
                key=f"new_employee_shift_{revision}",
            )
        skills_table = st.data_editor(
            pd.DataFrame(
                [{"Skill": skill, "Level": 0} for skill in data["skillNames"]]
            ),
            hide_index=True,
            use_container_width=True,
            disabled=["Skill"],
            column_config={
                "Skill": st.column_config.TextColumn(width="large"),
                "Level": st.column_config.NumberColumn(
                    min_value=0,
                    max_value=3,
                    step=1,
                    format="%d",
                    width="small",
                ),
            },
            key=f"new_skills_{revision}",
        )
        if st.button("Add employee", type="primary"):
            try:
                skills = {
                    str(row["Skill"]): int(row["Level"])
                    for row in skills_table.to_dict("records")
                }
                overlay = add_employee_to_overlay(
                    factory_baseline,
                    state_value["employeeOverlay"],
                    {
                        "employeeId": employee_id,
                        "name": employee_name,
                        "defaultShift": int(default_shift),
                        "defaultScheduled": True,
                        "defaultPresent": True,
                        "skills": skills,
                    },
                    imported_baseline=state_value.get("importedBaseline"),
                )
                updated = deepcopy(state_value)
                updated["employeeOverlay"] = overlay
                updated["attendanceByPlan"] = {}
                save_state(store, updated)
                rerun_with_fresh_widgets()
            except (StateValidationError, TypeError, ValueError) as error:
                display_error(error)


def render_about(data: dict[str, Any]) -> None:
    st.header("How the scheduler works")
    part_count = len({route["part"] for route in data["routes"]})
    metrics = st.columns(4)
    metrics[0].metric("Complete parts", part_count)
    metrics[1].metric("Route rows", len(data["routes"]))
    metrics[2].metric("Employees", len(data["operators"]))
    metrics[3].metric("Skills", len(data["skillNames"]))

    st.subheader("Scheduling model")
    st.write(
        "The engine is a deterministic greedy list scheduler. It expands each "
        "complete-part job into whole units and approved route steps, applies active "
        "child-before-parent dependencies, and reserves one employee plus one exact "
        "work center for every scheduled operation."
    )
    st.write(
        "Each whole unit may advance after its own preceding operation and movement "
        "time; it does not wait for the remaining quantity. Employees must be present "
        "and qualified, and employee windows, meals, work-center capacity, material "
        "holds, route order, and the hard shift cutoff are enforced."
    )

    with st.expander("Dispatch and tie-breaking"):
        st.markdown(
            """
1. Active child jobs before their parents.
2. Job priority, with 1 as highest.
3. Due-time offset.
4. Shorter estimated total route time.
5. Stable input order.
6. Unit number, then that unit's route sequence.

For each unit operation, the engine chooses the earliest finish, then the higher
skill level, then stable employee order. This makes repeated runs auditable.
"""
        )

    with st.expander("Unit-by-unit route flow and display grouping"):
        st.write(
            "The engine schedules each whole unit independently through its route. "
            "Finished units may start downstream while other units remain upstream. "
            "Successful unit operations stay scheduled even if later units exceed "
            "capacity, and adjacent assignments are grouped into compact display "
            "blocks. A parent job still waits for every entered child unit to finish."
        )

    with st.expander("What Excel controls"):
        st.write(
            "The controlled workbook is the portable source for approved routes, "
            "skill mappings, processing standards, dependencies, shift definitions, "
            "shared employee qualifications, and attendance defaults. Daily jobs and "
            "typed work windows live in plan snapshots."
        )

    st.subheader("Model boundary")
    st.warning(
        "This is a recommendation tool, not a global optimizer. It does not model "
        "machine downtime, setup/changeover matrices, employee-dependent speed, "
        "multi-person operations, or automatic cross-shift carryover. Review results "
        "before operational use."
    )


def main() -> None:
    factory_baseline = load_factory_baseline()
    store, state_value = initialize_state(factory_baseline)
    data = active_baseline(factory_baseline, state_value)

    render_theme_control(store, state_value)
    st.title("Manufacturing Shift Scheduler")
    st.caption(
        "Python scheduling, controlled Excel updates, transparent constraints, and "
        "unit-by-unit route flow with whole-unit display grouping."
    )

    render_sidebar(factory_baseline, store, state_value, data)
    plan_tab, schedule_tab, employees_tab, about_tab = st.tabs(
        ["Plan", "Schedule", "Employees", "About"]
    )
    with plan_tab:
        render_plan(factory_baseline, store, state_value, data)
    with schedule_tab:
        render_schedule(state_value)
    with employees_tab:
        render_employees(factory_baseline, store, state_value, data)
    with about_tab:
        render_about(data)


if __name__ == "__main__":
    main()
