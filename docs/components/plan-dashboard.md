# Plan dashboard

## Purpose

Several agentctl tasks run at once, and the only way to see where each one stands is to read its state file by hand. The plan dashboard shows every live task in one place: how far its stages have got, what it has spent, and where the time went, phase by phase. It only reads. It never writes to agentctl state, bindings, the spawn-cost ledger or the transcripts, and it takes no lock on any of them.

The dashboard is built in layers. This page describes the data layer that the other views will sit on.

## Data layer

Code: `scripts/plan_dashboard/model.py` (the model) and `scripts/plan-dashboard.py` (the command line). Tests: `scripts/tests/test_plan_dashboard_model.py`, with fixtures in `plan_dashboard_fixtures.py`, a mutation plugin in `plan_dashboard_mutations.py` and the mutation driver `test_plan_dashboard_mutation_catalogue.py`.

Mutation control. The plugin breaks one property of the model per named mutation; the driver runs the model tests in a subprocess once with no mutation (must be green) and once per mutation (must be red), so a mutation that no test notices fails the driver. To add a mutation, add a row to `MUTATIONS` in the plugin; the driver picks it up. To run one by hand: `PLAN_DASHBOARD_MUTATION=<name> PYTHONPATH=scripts/tests:scripts python3 -m pytest -q -rf -p no:cacheprovider -p plan_dashboard_mutations scripts/tests/test_plan_dashboard_model.py`; the last line of the output names the mutation applied.

```
python3 scripts/plan-dashboard.py snapshot [--format json|text] [--detail]
        [--all] [--session ID]... [--window-hours N] [--state-root DIR] [--cost-log FILE] [--transcripts-root DIR]
```

`--session` with an id that names no state file exits 3.

### Schema

The snapshot is `{"schema": "plan-dashboard/v1", "generated_at": …, "hostname": …, "tasks": […]}`. Each task is a summary: `session_id`, `task_id`, `goal`, `node`, `weight_class`, `liveness`, `current_stage`, `stages_done`, `stages_total`, `open_difficulty` (the open difficulty's title, or null), `difficulties` (open / stacked / closed counts), `replans`, `review_rounds`, `plan_review_rounds`, `effort`, `parent_session`, `last_activity`, `readable`, `reason`, `totals`. With `--detail` each task also carries a `detail` object: the stages, open and closed difficulties, linked child sessions, the last 20 history events, the bound harness sessions, `plan_path`, `tracker_key`, `repo_root`, `totals` and the `phases` tree. In the command-line output the detailed `totals` replace the summary `totals` on the task and are not repeated under `detail`.

Every time field is a recorded timestamp, never an age, so a snapshot says the same thing whenever it is read.

### Readable and unreadable tasks

State files are written by other processes without any lock, so a read can catch one half-written. The model reads such a file a second time, after a pause of 50 ms for the writer to finish; if it still does not parse, the task stays in the listing with `readable: false`, a `reason`, and null values elsewhere (`detail` keeps its full key set). A task is never dropped for being unreadable. A state file that is valid but old, with keys missing, is still summarized.

Files in the state directory that are not tasks are skipped: `<session>.delivery.json` sidecars, and any file whose `session_id` is not its own file name.

### Liveness

A task is classified by the first rule that matches:

1. `resolved` — the node is RESOLVED.
2. `stub` — the node is CLASSIFIED, with no weight class and no plan path (a session that was classified and then abandoned).
3. `active` — the last recorded activity is within 30 minutes, or a child run is open and its dispatcher process is alive.
4. `idle` — the last activity is within `--window-hours` (default 24).
5. `stale` — anything older.

The default listing shows `active` and `idle` tasks. `--all` shows every task. `--session` adds the named tasks whatever their liveness. A task inside the window whose parent is outside it still brings its parent in; no other file outside the window is opened.

### Phases and totals

The phase tree is built only from the history of events. Its windows are:

- planning, from the first event to the first plan submission;
- one window per plan version, with one child per review round;
- one window per stage, with one child per child run and an `attempts` count;
- one window per difficulty, paired by its uid;
- resolution, from the final verification to the resolve;
- an `unattributed` window for cost that belongs to no other.

A window that has not ended has an `end` and `wall_clock_s` of null. No clock is read, so the same data gives the same answer at any time.

Limits of the windows:

- There are no replan windows. After the plan is approved, time spent on a replan falls into whichever window holds its timestamp (usually the stage that was running), and the `replans` count is the only trace of it.
- A stage window runs from the stage's first `next_stage` to its last `record_result`. A retry reopens it, so the gap between a failed result and the retry (diagnosis, replanning) stays inside the stage window; `attempts` says how many results the stage took.
- Spawn cost is attributed to top-level windows only. A review round or child run under a window shows `spawn_cost_usd` and `spawn_count` as null, which the text view prints as `spawn not attributed`; 0 would read as "free".

Spawn cost comes from the spawn-cost ledger. A ledger row counts for a task when its `spawner_session` is the task, or when it names none and its `plan_path` is the task's. A row goes to the stage named by its `stage_index`; a review row goes to the plan window; any other row goes to the window that started latest at or before its timestamp (an open window holds every later timestamp); a row before the first event goes to `unattributed`.

Root cost (the coordinating session's own tokens) is **an estimate**. It is priced from the usage rows of the session's transcript and cut into phases by timestamp. Rows before the task's first event are dropped, as are rows after the resolve of a resolved task; rows with no timestamp go to `unattributed`. When no transcript is found the root cost is null, never 0.

Whose transcript. A task is priced from its own session transcript and from the transcripts of the harness sessions bound to it. A linked child (its state carries a `parent_link`) reads only its own transcript: the harness session it was started from is the parent's conversation, and counting it would charge the parent's tokens to the child. A harness session can drive several tasks in turn; its transcript then belongs to all of them, so the root cost is shared. `totals.root_shared_with` lists the other tasks (linked children excluded) bound to the same conversations: null when the root is not counted, an empty list when the conversation is the task's alone.

Every `phases` item and `totals` carries an `estimate` object flagging which of its figures are inferred: wall-clock of the planning and review-round windows, and every root cost. `totals` is `{wall_clock_s, spawn_cost_usd, root_cost_usd, cost_usd, estimate, partial, root_counted, root_shared_with}`. A summary reads no transcript, so its totals are `partial` with a null root cost and `cost_usd` equal to the spawn cost; the text view marks that as a lower bound (`≥`) and estimates with `~`, and cuts each goal to one line of 100 characters.

### Serializer and digest

`to_json` writes sorted keys, so equal snapshots are equal bytes. `content_digest` is the sha256 of that text without `generated_at`: it is the same for two reads inside one liveness band and changes when a file does. A transcript growing does not move the digest of a summary, since a summary never reads one.

### Cost of reading

Cold, the text snapshot over the live state directory takes about a second and the detailed JSON snapshot about 16 s. After that the ledger is re-read only when its file changes (keyed by path, inode, size and mtime), and a transcript is read from where the last read stopped; it is read whole again when it shrinks, is replaced, or changes without growing. State files are never cached. Both caches sit behind one lock, so concurrent callers see the same numbers. The linked-child index (`child_index`) is built in one pass over the state directory, once per command, instead of once per task.

### Read-only guarantee

`collect`, `detail` and the command line open files only for reading. The tests run them under `read_only_guard`, which refuses any write, rename, delete or lock under the agent home, over a root that holds bindings, a linked child, a ledger and transcripts.

## Console view

Not written yet.

## Web page

Not written yet.

## Status digest

Not written yet.
