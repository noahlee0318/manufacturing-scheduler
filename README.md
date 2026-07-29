# Manufacturing Shift Scheduler

A portfolio-ready Python application that builds deterministic, constraint-aware
manufacturing shift schedules from jobs, employee qualifications, availability,
route standards, work-center capacity, and parent-child dependencies.

The user interface and scheduling implementation are written in Python. The app
uses Streamlit for the UI and a standalone scheduling engine under
`src/shift_scheduler/`.

## What it does

- Assigns only present employees whose 0-3 skill level meets the configured
  solo-work threshold.
- Prevents employee and work-center overlaps.
- Enforces route order, movement delays, material holds, hard shift cutoffs, and
  active parent-child dependencies.
- Supports cross-midnight shifts and per-employee start, finish, and 40-minute
  meal windows.
- Schedules each whole unit through its route so a finished unit can move
  downstream after its own preceding operation and movement time without
  waiting for the rest of the entered quantity.
- Keeps every feasible whole-unit operation scheduled when capacity runs out,
  while clearly grouping and flagging the remaining unfinished units.
- Groups adjacent unit assignments into compact whole-unit display blocks and
  reports each route transition that used early downstream release.
- Imports a controlled `.xlsx` workbook containing the five vital worksheets.
- Persists local plans, attendance, imported baselines, and employee skill edits.
- Exports result tables and portable plan snapshots.
- Includes a persistent in-app light/dark appearance control.

## Public demo data

This repository contains synthetic part numbers, work centers, routing
descriptions, processing standards, and dependency identifiers. Employees use
generic labels (`Employee 1`, `Employee 2`, and so on). The employee roster is
pseudonymized rather than anonymous: it retains the original 0-3 skill vectors
and stable source order so assignment behavior remains representative.

No production-history sheet, real employee names or IDs, company logo, source
workbook hash, or original proprietary identifier is included.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
```

Activate the environment:

```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS or Linux
source .venv/bin/activate
```

Install and run:

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

The app opens at `http://localhost:8501`.

For development and testing, install the package and development dependencies
instead:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Repository layout

```text
app.py                         Streamlit application
src/shift_scheduler/
  scheduler.py                 Pure-Python scheduling engine
  workbook.py                  Strict Excel import and validation
  state.py                     Local persistence and baseline overlays
  ui_helpers.py                Time, availability, and result formatting
data/public_baseline.json      Synthetic demonstration baseline
templates/
  manufacturing_scheduler_template.xlsx
docs/
  USER_MANUAL.md
  Manufacturing_Shift_Scheduler_Manual.docx
  ARCHITECTURE.md
tests/                         Regression and privacy tests
LICENSE                        MIT license
```

## Excel import contract

Imports are limited to `.xlsx` files up to 10 MiB. The workbook must contain
these exact worksheets and A1 titles:

| Worksheet | Required A1 title |
| --- | --- |
| Approved Routes | Approved Complete-Part Route Master |
| Operator Skills | Operator Qualification Matrix |
| Attendance | Daily Attendance and Shift Selection |
| Parent-Child | Approved Parent-Child Dependencies |
| Settings | Complete-Shift Scheduler Settings |

Use the template in `templates/` or download it from the app. The application
validates headers, types, duplicates, route references, skill levels, shift
definitions, and dependency cycles before replacing the active baseline.

## Scheduling approach

The engine is a deterministic greedy list scheduler. Jobs are dispatched by:

1. active dependency depth;
2. job priority;
3. due-time offset;
4. shorter estimated route time; and
5. stable input order.

Within an ordered job, the engine processes unit 1 through its route, then unit
2, and so on. A downstream operation becomes eligible when that same unit's
preceding operation and movement time are complete. For each unit operation,
the engine chooses the qualified employee producing the earliest finish, then
breaks ties by higher skill and stable employee order. Successful bookings are
retained even if a later unit cannot fit.

After scheduling, adjacent unit operations are grouped only when they use the
same employee, cover consecutive unit numbers, and touch in time. The result
includes `transferFlows` records whenever downstream work begins before the full
upstream quantity has finished. These rules make the recommendation
reproducible and auditable, but it is not a global mixed-integer or CP-SAT
optimizer. See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full model and
known tradeoffs.

Each scheduled block reserves one employee and one exact work center; both
resources currently have capacity one, and the workbook's
`Work-center capacity` setting must remain `1`. Due times affect dispatch order
and the reported on-time/late status, but they are planning targets rather than
hard deadlines: the engine may schedule a job after its due time.

An active parent-child relationship remains a full-job barrier: a parent does
not start until every entered child job has completed every unit and route step.

## Persistence and data handling

Plans, attendance, imported baselines, employee skill edits, and the selected
light/dark mode are stored as local JSON on the computer running Streamlit. By
default, the state file is:

```text
~/.manufacturing_shift_scheduler/state.json
```

Set `SHIFT_SCHEDULER_STATE_DIR` to use a different directory. The application
does not encrypt this state file, so do not import sensitive operational data
on a shared or untrusted computer. Clearing browser data does not remove the
server-side file.

The interface supports at most 50 jobs per saved plan and shows an error instead
of saving or scheduling additional rows. Portable plan snapshots with more than
50 jobs are also rejected. On a hosted multi-user Streamlit deployment, local
disk may be shared or temporary; use non-sensitive demo data and an appropriate
external state service for production deployment.

## Current model limits

- The deterministic greedy scheduler does not guarantee a globally optimal
  schedule and does not backtrack when an earlier assignment blocks a later
  one.
- Exact work-center capacity is fixed at one, and each operation uses one
  employee.
- Due times are soft ordering and status targets, not hard constraints.
- The model does not include setup or changeover time, machine downtime,
  employee-dependent processing speed, multi-person operations, automatic
  cross-shift carryover, or multi-shift continuity.
- One unit cannot be divided, and a unit operation cannot cross an employee's
  meal or shift finish. Feasible units can remain scheduled while other units
  are unfinished, but rebuilding the same plan starts from the original entered
  quantities unless the user updates them.

For operating instructions, field definitions, automatic split behavior, and
unfinished-status guidance, see the
[User Manual](docs/USER_MANUAL.md). A Word version is also included at
[`docs/Manufacturing_Shift_Scheduler_Manual.docx`](docs/Manufacturing_Shift_Scheduler_Manual.docx).
Implementation details and tradeoffs are documented in
[ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
```

The suite includes golden parity fixtures generated from the 2026.07.29
unit-flow JavaScript engine, unit-flow and partial-capacity invariants, workbook
validation, state persistence, theme persistence, and a public-release privacy
scan.

## License

This project is available under the [MIT License](LICENSE).
