# Architecture

## Components

The application separates scheduling logic from the interface:

- `app.py` owns Streamlit widgets and session state.
- `scheduler.py` is a pure function from baseline data plus plan input to a
  result dictionary.
- `workbook.py` validates and converts the controlled Excel workbook into the
  baseline schema.
- `state.py` composes imported baselines with employee overlays and performs
  atomic local persistence.
- `ui_helpers.py` converts typed clock values, meal windows, and minute offsets
  for the interface.

The scheduling engine does not import Streamlit, pandas, or openpyxl.

## Time model

Every shift is represented as minute offsets from its configured start.
Cross-midnight clock values are converted modulo 24 hours. Each working employee
has one or more non-overlapping availability windows. Enabling a meal removes a
fixed 40-minute interval and therefore creates up to two windows.

An operation must fit completely inside one employee window. A single unit is
never divided across windows.

## Resource model

Each scheduled block reserves:

- exactly one employee; and
- exactly one exact work center.

Both resources have capacity one. Blocks on different work centers may overlap
only when assigned to different employees.

An employee is eligible when present and their numeric skill level for the
route's mapped skill is at least the configured threshold. Skill level affects
eligibility and deterministic tie-breaking, not processing speed.

## Precedence

For one job, unit U at route step N waits only for unit U at step N-1 and then
applies the current route row's movement delay. Other units may remain upstream.

When both a parent and one of its configured children are entered, the parent's
first route step waits for every entered matching child job to finish. No
quantity-ratio assumption is inferred.

## Unit scheduling and result aggregation

Jobs are ordered by dependency depth, priority, due-time offset, shorter
estimated route time, and stable input order. Within a job, the engine iterates
unit number and then route sequence. Each unit operation searches the current
employee and work-center calendars for its earliest feasible slot.

Successful unit bookings are committed immediately. If a later unit cannot
fit, the earlier work remains scheduled and the failed unit receives a
shift-capacity record. Only that unit's downstream steps receive
previous-operation-unfinished records.

After scheduling, raw unit records are grouped for display:

- scheduled records merge only for the same employee, consecutive unit numbers,
  and contiguous times;
- issue records merge only for the same status and consecutive unit numbers;
  and
- scheduled and issue quantities together must reconcile to the entered
  quantity for every route.

The result includes `transferFlows` when a downstream unit starts before the
last scheduled upstream unit finishes. Each record identifies the job, route
transition, affected work centers, early-release quantity, first downstream
start, and full upstream finish. `batchSplits` is retained for completed routes
that require more than one scheduled display block.

## Determinism and tradeoffs

This is a greedy list scheduler, not a global optimizer. It deliberately favors
repeatability and explainability. It does not backtrack to preserve scarce
skills for future tasks, globally minimize makespan, or optimize weighted
tardiness.

That means a locally best employee assignment can occasionally prevent a later
task even when a different earlier assignment would make both feasible. The
test suite records this behavior so a future global-optimization upgrade can be
introduced intentionally rather than accidentally.

## Persistence

The default local state directory is outside the repository and can be
overridden with:

```text
SHIFT_SCHEDULER_STATE_DIR=/path/to/state
```

Writes use a temporary file followed by an atomic replace. Schedule results are
session-only; plans, attendance, imported baselines, employee overlays, and the
selected light/dark appearance are persisted.

On a hosted multi-user Streamlit deployment, local disk may be ephemeral or
shared. Use downloadable plan snapshots or replace the state adapter with a
database/object store for durable multi-user use.
