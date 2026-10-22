# Plan dashboard

## Purpose

Several agentctl tasks run at once, and the only way to see where each one stands is to read its state file by hand. The plan dashboard shows every live task in one place: how far its stages have got, what it has spent, and where the time went, phase by phase. It only reads. It never writes to agentctl state, bindings, the spawn-cost ledger or the transcripts, and it takes no lock on any of them.

The dashboard is built in layers. This page describes the data layer that the other views will sit on.

## Data layer

Code: `scripts/plan_dashboard/model.py` (the model) and `scripts/plan-dashboard.py` (the command line). Tests: `scripts/tests/test_plan_dashboard_model.py`, with fixtures in `plan_dashboard_fixtures.py`, a mutation plugin in `plan_dashboard_mutations.py` and the mutation driver `test_plan_dashboard_mutation_catalogue.py` (which covers `MUTATIONS`; the console view's family is described under Console view).

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

A live terminal view of the same tasks: a Tasks tab with one row per task and a Detail tab with the selected task's stages, phases, difficulties, child sessions and recent events. It reads through the data layer and writes nothing.

```
python3 scripts/plan-dashboard.py tui [--once] [--interval S]
        [--all] [--session ID]... [--window-hours N] [--state-root DIR] [--cost-log FILE] [--transcripts-root DIR]
```

- `tui` starts the interactive view by running `uv run` on `scripts/plan_dashboard/tui_app.py`, whose PEP 723 block pins `textual==8.2.8`. Without `uv` on the path it prints `interactive console needs uv; use 'plan-dashboard.py tui --once'` to stderr and exits 4.
- `tui --once` prints the task table once and exits 0, using only the standard library. Each `--session` adds that task's detail tree under the table, and a `--session` that names no state file prints `plan-dashboard: no such session: <id>` to stderr and exits 3, as `snapshot` does. It is the fallback where uv or a terminal is missing, and what scripts and tests read.
- `--interval` is the number of seconds between reads (default 2). The screen is read again once that long has passed since the last read, and at once on `r`. A re-read keeps the cursor on the task it was on, even when the new order moves that task to another row; the cursor moves only if the task is gone.

Keys: Up/Down (or `k`/`j`) move the cursor, Enter or a click opens the row, Esc or Backspace goes back to the Tasks tab, Tab switches tabs, `a` switches between live and all tasks, `r` reads again, `q` quits. The mouse works wherever textual supports it.

The Tasks tab and `tui --once` open with a header line: the host name, the time of the read, and the counts of `active`, `idle` and all listed tasks. The table below it has one row per task, with the columns `session` (the first 8 characters of the session id), `task` (the task id, cut to 24 characters), `live` (the liveness), `node`, `stage`, `diff`, `replans`, `age` (time since the last activity), `wall`, `cost` and `goal`. `stage` reads `stage k/n` with an 8-cell progress bar (`stage 1/3 [###-----]`), or `no plan` when the task has no stages. `diff` is the number of difficulties the task has recorded, open, stacked and closed together, with a `!` before it while one is open (`!2`). `replans` is the replan count. Rows come active first, then by last activity, most recent first. A task whose state file cannot be read keeps its row as `stage ?/?` with `unreadable` and the reason. When the task selected on the Detail tab disappears, the tab shows a single `no such session` node. What the screen shows follows the data layer's honesty rules: a `~` before a figure marks an estimate, `≥` marks a total that is only a lower bound (a task row never counts the root session's own cost), `not counted` stands for a cost that was never measured and is never shown as 0, a phase that has not ended reads `open (<time> so far)` (`?` when its start cannot be read), and a nested spawn whose cost cannot be attributed reads `spawn not attributed`. A span that would be negative (a start later than the clock, from clock skew) reads as zero. A stage line starts with its status (`[done]`, `[active]`, `[failed]`, `[skipped]`, `[pending]`, `[?]` for any other).

Code: `scripts/plan_dashboard/tui.py` holds every row, label, key decision and refresh decision as pure functions of the model and an explicit `now`; `scripts/plan_dashboard/tui_app.py` only maps them onto textual widgets and reads in a worker thread, so the screen never blocks on the disk. Tests: `scripts/tests/test_plan_dashboard_tui.py` (plain pytest: layout, keys, the `tui` subcommand, the confinement check, this section) and `scripts/tests/test_plan_dashboard_tui_app.py` (textual's headless pilot). Both build their tasks with `scripts/tests/plan_dashboard_support.py`, which also holds the confinement scan.

Dependency decision. Textual is the only third-party package the dashboard uses. It is pinned exactly, run through `uv run` with no global install, and imported by `tui_app.py` alone (and by its pilot test); a plain test scans the dashboard's own sources and tests (`scripts/plan_dashboard/`, `plan-dashboard.py` and the `*plan_dashboard*` files under `scripts/tests/`) and fails if any file other than those two imports it. The data layer, the command line, `tui --once` and the web view stay standard library. Run the pilot test with `PLAN_DASHBOARD_REQUIRE_TEXTUAL=1 uv run --no-project --python 3.12 --with textual==8.2.8 --with pytest python -m pytest -q scripts/tests/test_plan_dashboard_tui_app.py`; with the variable set a missing textual fails the run, without it the file is skipped.

Mutation control. `TUI_MUTATIONS` in the mutation plugin holds the console view's seven mutations; they are selected by name like the model's, but the model tests cannot see them. `test_plan_dashboard_mutation_catalogue.py` runs six of them against `test_plan_dashboard_tui.py` and requires that file to go red, with the system `pytest`. The seventh, `tui-app-no-refresh`, breaks `tui_app`, so the catalogue runs it against the pilot file, and only where textual can be imported: it is skipped under the system `pytest`. To include it, run the catalogue under the pilot command above with `scripts/tests/test_plan_dashboard_mutation_catalogue.py -k pilot`; with `PLAN_DASHBOARD_REQUIRE_TEXTUAL=1` a missing textual fails it instead of skipping.

## Web page

Not written yet.

## Status digest

Not written yet.
