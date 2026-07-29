# Manufacturing Shift Scheduler User Manual

Release 1.1 — Python unit-flow portfolio edition

## 1. Purpose

The scheduler recommends who should perform each routed operation during one
manufacturing shift. It combines jobs, quantities, priority, material status,
employee availability, 0-3 skill levels, route standards, work-center capacity,
and active parent-child dependencies.

The recommendation is deterministic: the same baseline and the same inputs
produce the same result.

## 2. Start the application

From the repository folder:

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

Open the local address shown in the terminal, normally
`http://localhost:8501`.

## 3. Daily workflow

1. Select the production date and shift.
2. Confirm which employees are working.
3. Change an employee's start, finish, or meal only when it differs from the
   default.
4. Enter each complete-part job, total quantity, priority, optional due time,
   and material-ready status.
5. Select **Build schedule**.
6. Review the summary, unit-flow transitions, issues, and grouped result views.
7. Download a result table or portable plan snapshot when needed.

## 4. Employee availability and meals

The default shift clocks come from the active baseline. Shift 1 defaults to a
40-minute lunch beginning at 11:10. Shift 2 defaults to a 40-minute dinner
beginning at 20:55. Shift 3 has no default meal and is marked as a placeholder
until its hours are verified.

Work start, work finish, and meal start accept familiar clock formats such as:

- `5`
- `500`
- `5:00`
- `5a`
- `5:00 AM`
- `17:00`

An operation must fit entirely inside one contiguous employee availability
window. It cannot cross that employee's meal or shift finish. Meals are
employee-specific; another qualified employee may use the same work center
during that time when the center is free.

## 5. Skills and qualifications

Skill values use this controlled scale:

- 0 — not qualified
- 1 — trainee
- 2 — independent
- 3 — trainer

The default solo-work threshold is 2. The imported Settings worksheet may set
the threshold from 1 to 3. An employee is eligible only when their level for
the operation's mapped skill meets the active threshold.

Use the **Employees** screen for local additions and skill edits. Local edits
persist on the computer running the app, but they do not modify the Excel file.
For a shared baseline, update both **Operator Skills** and **Attendance** in the
controlled workbook, then import it again.

## 6. Job fields

| Field | Rule |
| --- | --- |
| Job ID | Required and unique within the plan |
| Complete part | Must exist in Approved Routes |
| Quantity | Positive whole number |
| Priority | 1 (highest) through 5 |
| Due time | Optional clock time used for dispatch order and on-time status |
| Material ready | If false, every route row for the job is held |

Enter complete parts, not individual route operations. The application expands
each job into its full approved route.

## 7. Unit-by-unit route flow

Unit flow applies to every multi-unit job, not a particular example quantity.
The scheduler treats the entered quantity as indivisible whole units and
processes each unit through the approved route in sequence.

As soon as one unit finishes its preceding operation plus movement time, that
unit may begin its next route operation. It does not wait for the remaining
quantity to finish upstream. The same unit never skips a route step, and every
scheduled operation still reserves one employee and one exact work center.

The Schedule screen reports **Unit-by-unit route flow applied** when a
downstream unit starts before the full upstream quantity is complete. It also
groups adjacent unit operations into compact display blocks when they use the
same employee, cover consecutive unit numbers, and touch in time. Multiple
blocks are labeled `Sub-batch i/n`; their quantities and any issue rows always
reconcile to the original entered quantity.

When capacity runs out, every already-feasible whole-unit operation remains
scheduled. The remaining units appear as unfinished shift-capacity rows, and
only those units' later route steps are marked previous-operation unfinished.

## 8. Parent-child dependencies

A configured dependency becomes active only when both the parent and at least
one matching child job are entered. Every entered matching child job must
complete before the parent's first operation begins.

If a parent has configured children but none are entered, the application shows
an inactive-dependency warning and schedules the parent normally.

## 9. Result screens

The Schedule screen provides:

- complete jobs;
- unfinished jobs;
- issue rows;
- unit-flow transition count and details;
- last scheduled finish;
- chronological operations;
- views grouped by job, work center, and employee;
- automatic split details; and
- a guide for every unfinished status.

Use the status filter to show all rows, scheduled rows, or issues.

## 10. Unfinished status guide

| Status | What to do |
| --- | --- |
| Material hold | Make material ready or move the job to another controlled plan |
| Quantity required | Enter a positive whole-number quantity |
| No approved route | Add the part to Approved Routes and import the workbook |
| Missing P75 time | Enter a positive P75 override and import again |
| Skill map required | Map the route to one of the controlled skills |
| No qualified employee | Adjust attendance or qualification data |
| Shift capacity | Keep the completed unit operations; adjust staffing, availability, priority, or the remaining units |
| Blocked by child | Finish every entered child job first |
| Previous operation unfinished | Resolve the earlier route issue for the same unit; other units may continue |

## 11. Updating from Excel

Use only an `.xlsx` copy of the supplied template. The import limit is 10 MiB.
These five worksheets are vital and must retain their exact names, A1 titles,
and required headers:

- Approved Routes
- Operator Skills
- Attendance
- Parent-Child
- Settings

Excel is required to change portable route definitions, operation sequence,
work centers, P75 and movement standards, parent-child relationships, shift
definitions, the minimum skill threshold, or the shared employee baseline.

Daily job rows, typed employee work windows, meal settings, local employee
overlays, and a built schedule are not imported from Excel.

Importing a valid workbook:

- replaces the active baseline;
- clears local employee overlays and saved attendance;
- preserves current job rows; and
- never modifies the uploaded file.

**Reset to imported baseline** removes later local employee and attendance
changes while preserving job rows.

## 12. Persistence and snapshots

Plans and the selected light/dark appearance are stored on the computer running
the application. Clearing browser data does not remove the server-side local
state file. Use a downloaded plan snapshot to move a plan to another computer
or protect against an ephemeral hosted deployment.

Schedule results are not written into the saved state. Build the schedule again
after reopening a plan.

## 13. Model limits

The engine is a deterministic greedy recommendation, not a global optimizer. It
does not model setup/changeover time, employee-dependent speed, machine
downtime, multi-person operations, automatic cross-shift carryover, or
multi-shift continuity. Feasible units remain visible in the current result,
but rebuilding starts from the entered quantities unless they are updated.

Review the result before using it operationally.
