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

The Tasks tab and `tui --once` open with a header line: the host name, the time of the read, and the counts of `active`, `idle` and all listed tasks. The table below it has one row per task, with the columns `session` (the first 8 characters of the session id), `task` (the task id, cut to 24 characters), `live` (the liveness), `node`, `stage`, `diff`, `replans`, `age` (time since the last activity), `wall`, `cost` and `goal`. `stage` reads `stage k/n` with an 8-cell progress bar (`stage 1/3 [###-----]`), or `no plan` when the task has no stages. `diff` is the number of difficulties the task has recorded, open, stacked and closed together, with a `!` before it while one is open (`!2`). `replans` is the replan count.

Rows come active first, then by last activity, most recent first. A task whose state file cannot be read keeps its row as `stage ?/?` with `unreadable` and the reason. When the task selected on the Detail tab disappears, the tab shows a single `no such session` node.

What the screen shows follows the data layer's honesty rules: a `~` before a figure marks an estimate, `≥` marks a total that is only a lower bound (a task row never counts the root session's own cost), `not counted` stands for a cost that was never measured and is never shown as 0, a phase that has not ended reads `open (<time> so far)` (`?` when its start cannot be read), and a nested spawn whose cost cannot be attributed reads `spawn not attributed`. A span that would be negative (a start later than the clock, from clock skew) reads as zero. A stage line starts with its status (`[done]`, `[active]`, `[failed]`, `[skipped]`, `[pending]`, `[?]` for any other).

Code: `scripts/plan_dashboard/tui.py` holds every row, label, key decision and refresh decision as pure functions of the model and an explicit `now`; `scripts/plan_dashboard/tui_app.py` only maps them onto textual widgets and reads in a worker thread, so the screen never blocks on the disk. Tests: `scripts/tests/test_plan_dashboard_tui.py` (plain pytest: layout, keys, the `tui` subcommand, the confinement check, this section) and `scripts/tests/test_plan_dashboard_tui_app.py` (textual's headless pilot). Both build their tasks with `scripts/tests/plan_dashboard_support.py`, which also holds the confinement scan.

Dependency decision. Textual is the only third-party package the dashboard uses. It is pinned exactly, run through `uv run` with no global install, and imported by `tui_app.py` alone (and by its pilot test); a plain test scans the dashboard's own sources and tests (`scripts/plan_dashboard/`, `plan-dashboard.py` and the `*plan_dashboard*` files under `scripts/tests/`) and fails if any file other than those two imports it. The data layer, the command line, `tui --once` and the web view stay standard library. Run the pilot test with `PLAN_DASHBOARD_REQUIRE_TEXTUAL=1 uv run --no-project --python 3.12 --with textual==8.2.8 --with pytest python -m pytest -q scripts/tests/test_plan_dashboard_tui_app.py`; with the variable set a missing textual fails the run, without it the file is skipped.

Mutation control. `TUI_MUTATIONS` in the mutation plugin holds the console view's seven mutations and `WEB_MUTATIONS` the web page's six; they are selected by name like the model's, but the model tests cannot see them. `test_plan_dashboard_mutation_catalogue.py` runs six of them against `test_plan_dashboard_tui.py` and requires that file to go red, with the system `pytest`. The seventh, `tui-app-no-refresh`, breaks `tui_app`, so the catalogue runs it against the pilot file, and only where textual can be imported: it is skipped under the system `pytest`. To include it, run the catalogue under the pilot command above with `scripts/tests/test_plan_dashboard_mutation_catalogue.py -k pilot`; with `PLAN_DASHBOARD_REQUIRE_TEXTUAL=1` a missing textual fails it instead of skipping.

## Web page

A read-only page with the same tasks, served from the standard library alone: a task table, and the selected task's phase tree below it. It reads through the data layer and writes nothing.

```
python3 scripts/plan-dashboard.py web [--port N] [--interval S] [--self-test]
        [--all] [--session ID]... [--window-hours N] [--state-root DIR] [--cost-log FILE] [--transcripts-root DIR]
```

- The server listens on `127.0.0.1` only; there is no option to bind another address. `--port` picks the port (default 8765, `0` for any free one); a port that cannot be bound prints `plan-dashboard: cannot listen on …` to stderr and exits 5. The first line printed is the URL.
- To open the page from another machine, forward the port: `ssh -L 8765:127.0.0.1:8765 <host>`, then browse `http://127.0.0.1:8765/` locally.
- `check_bind` refuses any non-loopback address and `serve` calls it, so no caller can widen the bind. A request whose `Host` header is not a loopback name (`127.0.0.1`, `localhost`, `[::1]`, with or without a port), or is missing, gets 421, so a page on another origin cannot read the data through the browser (DNS rebinding). Only `GET` and `HEAD` are served; `POST`, `PUT`, `DELETE`, `PATCH` and `OPTIONS` get 405 with `Allow: GET, HEAD`, any other method gets 501. `HEAD` sends headers only on every route, `/api/events` included. No endpoint changes anything.
- The options `--all`, `--session`, `--window-hours`, `--cost-log` and `--transcripts-root` apply to every read the server makes, the page's included.
- Routes: `/` the page, `/api/snapshot` the task list as JSON, `/api/task/<session id>` one task's detail (404 for an unknown or malformed id), `/api/events` a Server-Sent Events stream. The stream sends a `snapshot` event on connect and then only when the content (everything but the read time) has changed, with a `: keepalive` comment every 15 s while nothing does; `--interval` is the read tick (default 2 s). The page falls back to polling `/api/snapshot` when the stream is unavailable. Each open stream runs its own read every tick, and a closed client is noticed at the next write, up to 15 s later; that is acceptable for a loopback tool.
- The page lists the tasks with their total wall-clock and total cost; a click opens the task's stages and a phases table (wall-clock, spawn cost, root cost) whose rows expand to their children. It builds its elements with `textContent` only, loads no external asset, and follows the data layer's honesty rules: `~` marks an estimate, `≥` a lower bound, a cost that was never measured reads `not counted`, and a phase that has not ended reads `open`.
- `--self-test` serves the selected state directory on a free port and probes the page, snapshot, `HEAD` on the stream, the first stream event, the host check (wrong and missing `Host`) and the method check; it prints `web self-test: ok` and exits 0, or names the failed probes and exits 1. It needs no particular task to be live.

Code: `scripts/plan_dashboard/web.py` (`serve`, `check_bind`, `host_allowed`, `make_handler`, `event_due`, `self_test`) and `scripts/plan_dashboard/static/index.html`. Tests: `scripts/tests/test_plan_dashboard_web.py`.

## Status digest

A short text summary of the live tasks, kept in one chat message that is edited in place. Core does not know which messenger carries it (ADR-0008 § 7): it produces the text, a stable key per machine (`plan-dashboard:<host>`), and calls a machine-local publisher plugin that owns the transport.

```
python3 scripts/plan-dashboard.py publish (--dry-run | --check-publisher | --once | --watch)
        [--plugin-dir DIR] [--publish-state FILE] [--interval S] [--min-interval S]
        [--all] [--session ID]... [--window-hours N] [--state-root DIR] [--cost-log FILE]
```

- `--dry-run` prints the digest and exits; it loads no plugin and writes no file. `--session` with an id that names no state file exits 3.
- `--check-publisher` loads the plugin and runs its `health()` with the network and process spawning stubbed to raise, so a check that reaches out fails instead of passing quietly. It exits 0 when healthy, 1 otherwise.
- `--once` makes one pass and prints `sent`, `edited`, `unchanged` or `skipped`. `--watch` repeats it every `--interval` seconds (default 60) until interrupted. `publish --watch` is the long-running form. `--once` and `--watch` apply the same `--session` check as `--dry-run` (exit 3).
- A failed send is not free: its time is saved in the state like a sent one, so the rate bound throttles failures too, and a plugin may attach a `retry_after` (seconds) to its exception, which holds the next attempt back. `--watch` prints the failure, backs off (doubling the interval up to 15 minutes, never below a retry-after) and exits 1 after 5 consecutive failed passes. If the plugin returned but the state cannot be saved, the command stops with exit 1 rather than send a duplicate next pass. Each pass holds an exclusive lock on `<state file>.lock`, so two processes cannot send at once; the second one reports `busy` (`--watch`) or exits 1 (`--once`).
- The text has a header (host name, task count) and one line per task: session, liveness, node, stages done/total, `!` while a difficulty is open, wall-clock, cost, goal. Estimates and lower bounds carry `~` and `≥` as everywhere else. It holds no clock reading, so it changes only when a shown figure does. It is cut to 4096 characters with `… and N more` for the rest.
- Sending is change-driven on the sha256 of that text and rate-bounded: a changed text is sent only when `--min-interval` (default 30 s) has passed since the last send, so the message is edited at most about twice a minute. The first send creates the message; later ones edit it through the handle the plugin returned, kept with the hash in the publish state file (`$XDG_STATE_HOME/plan-dashboard/publish-state.json`, or `--publish-state`). That file and its lock file are the only files the command writes, and it lies outside the engine's state directory. A plugin that raises leaves the handle and hash unchanged.
- The plugin is `<plugin dir>/publishers/<name>.py` with `NAME`, `publish(key, text, handle) -> handle` (send when `handle` is None, otherwise edit that message and return its handle) and `health() -> bool | (bool, detail)`. Plugin dir: `--plugin-dir`, else `$PLAN_DASHBOARD_PLUGIN_DIR`, else `<agent home>/plan-dashboard-plugins`. Exactly one plugin must be present: none, several, one that fails to import or one without the two functions is an error (exit 6), never a guess. A plugin uses absolute imports only and keeps its credentials out of argv and output.

Code: `scripts/plan_dashboard/publish.py` (`digest`, `should_send`, `run_once`, `watch`, `select_publisher`, `check_health`). Tests: `scripts/tests/test_plan_dashboard_publish.py`, with fake plugins only; `PUBLISH_MUTATIONS` in the mutation plugin holds seven mutations (`publish-no-cap`, `publish-always-send`, `publish-watch-once`, `publish-writes-state`, `publish-health-net-open`, `publish-no-lock`, `publish-attempt-unrecorded`) and is run with the command of the data layer's mutation control against that test file.
