# telemetry — the machine-local record of agent sessions

**Difficulty removed.** Hooks and commands run as many short processes; each must record
what happened without waiting on a database and without losing events when several run
at once.

**How it works.** An emitter appends one line per event to a private spool; one writer
applies the spool to a SQLite record exactly once.

| Module | Role |
|---|---|
| `emit.py` | `emit(session_id, kind, fields, backref=None)`: one `O_APPEND` write of `"\n" + JSON + "\n"` to `<config root>/telemetry/spool/<session>.jsonl`. Assigns the event id (UUIDv7). Never raises, never imports sqlite3; a failed append is counted in `<session>.drops`. |
| `store.py` | The only module that opens the record (`telemetry.sqlite`, WAL, mode 0600 in a 0700 directory). Creates it, applies `migrations/NNN_name.sql` in order, keeps the origin id (also in `<record>.origin`), and exposes `replay`, `insert_rows`, `origin_id`, `imported_row_id`. |
| `tables.py` + `tables/*.toml` + `tables.snapshot.json` | Open enumerations (`roles`, `phases`, `loss_modes`). A member is added or retired (never deleted) by `add_member` / `retire_member`, each bumping the table version by 1 and recording the new state in the snapshot, so a stored version stays resolvable. Rows store the version they were validated against. |

**Replay.** `store.replay(session_id)` takes the per-spool `flock`, reads complete lines
from the stored byte offset, `INSERT OR IGNORE`s them by event id and moves the offset in
the same transaction, at most 500 lines per transaction. Retry, concurrency or a crash
between transactions can only re-insert the same keys. A torn line (newline-terminated,
unparsable) is counted once in `losses`. A changed spool generation (inode plus sha256 of
the first non-empty line) or an offset past the end restarts at offset 0. When a live event
carries a back-reference to a row the importer wrote from a file ledger, replay deletes
that imported row (live wins).

**Deadline and fail-open.** Every call shares one monotonic 250 ms deadline across the
lock wait and the SQLite busy wait. When it passes the call returns `deferred` and the
spool waits for the next trigger. A store failure (read-only, corrupt, full disk) never
reaches the caller; the spool and its offset stay as they were.

**Run it.** `python3 scripts/telemetry.py sync --session <id> [--budget S]`,
`python3 scripts/telemetry.py path`.

**Tests.** `scripts/tests/test_telemetry_{tables,store,hook_isolation,mutation_catalogue}.py`
(run with `--telemetry-no-skip` to fail on any skip) and the mutation control
`scripts/tests/telemetry_mutation_control.py` (`--control`, `--mutant NAME`; mutants live in
`scripts/tests/telemetry_mutants/`).

**Fault injection (tests only).** `AGENTCTL_TELEMETRY_FAULT` takes comma-separated tokens
`emit_fail`, `enospc_spool`, `enospc_record`, `enospc_marker`, `enospc`.
