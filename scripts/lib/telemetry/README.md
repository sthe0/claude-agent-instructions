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
| `tables.py` + `tables/*.toml` + `tables.snapshot.json` | Open enumerations (`roles`, `phases`, `loss_modes`, `categories`, `severity`, `places`, `registry`). A member is added or retired (never deleted) by `add_member` / `retire_member`, each bumping the table version by 1 and recording the new state in the snapshot, so a stored version stays resolvable. Rows store the version they were validated against. A member may carry extra attributes (`Member.attrs`): the `severity` bands and cases, and every `registry` entry. |
| `cause.py` | The structured cause of a critique (`validate_cause`, `complete_cause`): the rules between `cause_category`, `machinery_induced`, `mechanism` and `functional_place`, checked against the tables with no member list in code. Stdlib and `tables` only. |
| `importer.py` | Fills the record from what predates live emission: the gate log, decision log, judge ledger, spawn/cost ledger, engine histories (`import:*` sources) and, with `--transcripts`, session transcripts as a fallback. Also `rebuild` (new record, same origin, from the spools and sources) and `prune`. |
| `phases.py` | The one definition of a task's phase at a time: from live events when the session has any, else from the transcript reconstruction. `scripts/lib/time_profile.py` reads the record through it and falls back to transcripts when there is no record, and, for one session, to its engine state file when the record holds a loss for it. |
| `profile_check.py` | `check-profile-unchanged`: recomputes the time-profile aggregate from a frozen transcript snapshot, pinned clock and frozen ledgers and compares it with the stored reference. |
| `tokens.py` | Token accounting. A collector reads root, spawned-child and subagent transcripts and the judge ledger incrementally. Each file keeps its own offset under consumer `tokens`, and the offset commits in the same transaction as the rows. The collector writes one `token_usage` row per message id across the record: the winning line is the earliest timestamp, then the original before a copy, then the earliest session start, path and offset. Each row carries its task (null with no open epoch), role, phase, model and four counts. It also writes `context_births` (spawn, compaction, resume; content-based ids), compaction pre/post sizes, `judge_usage`, and one `standing_surface` row per origin and canonical HEAD (UTF-8 bytes of the always-loaded files plus SessionStart hook output). `sync` and `import` call it. `check-tokens` checks a task's conservation and the imported coverage. |
| `substrate.py` | Substrate adequacy for `improvement-scan.py substrate`: the severity mass of difficulties per functional place, attributed through the registry and rolled up through `part_of`, plus the calibration walk. Reads only through `report.tasks()` / `report.difficulties()`; writes nothing. |

**Mechanism registry** (`tables/registry.toml`). One entry per mechanism that emits an event: every hook of
`install-reminder-hooks.sh`, every engine gate, one `judge:<name>` per `judge_ledger.JUDGE_NAMES` (with
`calling_modules`), every `agentctl` command, the spawner, the landing events, the pre-commit hook and the four
telemetry parts. An entry has an `id` (the member name), a `kind` (`elementary` or `composite`), `part_of` (a list of
composite ids, acyclic), a `place` from `places.toml` (the functional place the mechanism serves) and `since`; an
elementary entry also lists its `source_keys` (the keys its events carry), so every emitted source key resolves to
exactly one entry. `unknown` is reserved for rows that name no member. The composites are the order phase, the
plan-review cycle, the effort trigger, the landing path and telemetry itself. `categories.toml` holds the cause
categories of a critique and `severity.toml` the severity bands and cases of a difficulty's duration.
`scripts/tests/test_telemetry_registry.py` derives the expected entries from the live sources, so a new hook, gate,
judge or command fails there until it is registered.

**Structured cause** (`cause.py`, table `causes`, migration `004_cause_fields.sql`). the `critique` command of
`agentctl` takes four required flags: `--cause-category` (a `categories` member), `--machinery-induced true|false`, `--mechanism` (a
`registry` id, or `none` exactly when the cause was not machinery-induced) and `--functional-place` (a `places`
member, or `none`; omitted for a machinery-induced cause, whose place is the registry entry's). `other` needs
`--cause-note`. `unknown` is a mechanism only an imported row may carry, with a place other than `none`. A critique
that breaks a rule is refused with the violated fields named; a retired member is refused on a new critique, while a
stored row stays valid because each enumerated field is stored with the version of its table. The record is kept on
the difficulty (`Difficulty.cause`, absent on records written before the flags existed) and on the history line, and
emitted as a `critique` event under `cli:critique`; the sync files one `causes` row per such event, keyed by the
event id, with the three table versions, the note and the record origin. The place vocabulary is one list:
`agentctl/mandate.py` names its item place (`ITEM_PLACE`) as a constant because that module reads no file, and
`test_registry_places_and_mandate_vocabulary_are_one_list` holds it to a live `places` member.
`test_telemetry_cause.py` also scans every git-tracked file under `scripts/`, `skills/`, `docs/`, `cursor/` plus
`CLAUDE.md` and `README.md` (`CRITIQUE_SCAN_TREE`) and fails on a call of the `critique` command — a fenced line, an
inline code span or a Python argv list — that does not show the four cause flags.

**Import.** `python3 scripts/telemetry.py import [--transcripts] [--all] [--max-rss-mib N] [--stats-json F]`.
Each source is read in streaming batches of 1000 lines; a batch and its offset commit in one
`store.transact`, so a crash or a deferral (the batch is retried with backoff, then the run
reports `complete: false` and exits nonzero) never moves the offset past unwritten rows. The
offset is kept per (file, generation, consumer); the generation is the sha256 of the first
line, and a truncated or rewritten file is detected by size and by a hash of the last line
read. Imported rows get a UUIDv5 id over (origin, path, offset, line sha256), so a second
run adds nothing and the same file under a new inode adds nothing. A live event wins: a
session that has live events (or a spool or end marker) gets no transcript rows and no
imported history, and a line a live event back-references is not inserted (a live event that
arrives later deletes the imported row). A child session's contour attaches to its parent's
task row. Counted losses (malformed lines, unreadable sources) are per session in the report;
the modes that cannot be counted per session are named in `uncounted_modes`.

**Rebuild and prune.** `telemetry.py rebuild [--transcripts]` moves a corrupt record aside as
`<name>.corrupt-<stamp>`, builds a new one that keeps the origin from the `.origin` side file,
replays every spool and re-imports the sources, and lists the sessions whose live-only events
did not come back (`rebuilt_sessions`). `telemetry.py prune [--retention-days N]` (default
`telemetry-prune-retention-days` in `config.md`) deletes sync and decision rows past the
window; task and session aggregates stay, and the first commit and trigger rows that bound a
kept latency interval, and the `sync.deferred` row of a spool still holding unreplayed bytes,
are kept.

A spool is removed only when its session has ended and the spool is fully replayed, and both
are re-read under the spool's lock (`sync.prune_spool`). An unreplayed byte count is taken
against the spool's generation: an offset stored for another generation (the spool was
removed and the session resumed) counts the new spool as unreplayed whole. In that one locked
step prune removes the spool, its `spool_offsets` row, its `.end` marker and its `.epoch`
sidecar; `.lock` files are never removed. A session has ended when its end marker is current
(the spool was not appended to more than `sync.END_GRACE_S` after the marker was written; the
SessionEnd hooks themselves record a decision event after it) or when the spool has been idle
for `LIVE_TTL_S`. Emitters take no lock, so a writer that opened the spool before the unlink
and writes after it is not excluded; the window is one open-to-write interval of an idle session.

**Replay.** `store.replay(session_id)` takes the per-spool `flock`, reads complete lines
from the stored byte offset, `INSERT OR IGNORE`s them by event id and moves the offset in
the same transaction, at most 500 lines per transaction. Retry, concurrency or a crash
between transactions can only re-insert the same keys. A torn line (newline-terminated,
unparsable, or parsable with a field of the wrong type or a time that is no date) is counted once
in `losses` and the offset moves past it, so one bad line never rolls back its batch. A changed spool generation (inode plus sha256 of
the first non-empty line) or an offset past the end restarts at offset 0. When a live event
carries a back-reference to a row the importer wrote from a file ledger, replay deletes
that imported row (live wins).

**Deadline and fail-open.** Every call shares one monotonic 250 ms deadline across the
lock wait and the SQLite busy wait. When it passes the call returns `deferred` and the
spool waits for the next trigger. A store failure (read-only, corrupt, full disk) never
reaches the caller; the spool and its offset stay as they were. The two hook-path ledger
writers, `hook_telemetry` and `judge_ledger`, import `live` guarded (a failed import turns
their event emission off and leaves their own ledger line as it was) and evaluate every
emit argument inside their `try`.

**Export, merge and round trip** (`portability.py`). `telemetry.py export --out F [--with-text]`
writes a gzip JSON-lines file, created 0600, from the record opened read-only: a header (origin,
`taken_at`, every enumeration table's version, the fixture revision, per-table counts), then one
line per row with the origin of the machine that recorded it (null when the record cannot resolve
it). Text artifacts go out only with `--with-text`; a failed export leaves no file. `telemetry.py
merge F` reads the export twice, one line at a time, so memory does not grow with its size: the
first pass refuses (exit 2, nothing written) an unknown table, column or table version, another
fixture revision, a malformed header, or line counts that differ from the header's (a truncated
file); the second applies rows by each table's merge key (`MERGE_KEYS`), 50 000 rows per
transaction so hook writers get the lock between chunks. An upsert-mode row replaces a stored row
only when both belong to the export's origin, that origin is not the receiver's, and the export is
newer than that origin's last merged one; a row of unresolved origin is only inserted when absent.
`telemetry.py check-roundtrip --workdir D` copies the record into D, exports the copy, merges it
twice into a new record under D and exits 1 on a count mismatch, a row added by the second merge
or a text row; it refuses when D's disk has less than three times the record's size free, and
writes nothing outside D. On a multi-GB record it takes about as much disk and time as two merges.

**Run it.** `python3 scripts/telemetry.py sync --session <id> [--budget S]`,
`python3 scripts/telemetry.py path`,
`python3 scripts/telemetry.py check-tokens (--task <id> [--require-spawn] [--require-compaction] | --imported-coverage)`.

**Tests.** `scripts/tests/test_telemetry_{tables,store,hook_isolation,mutation_catalogue,importer,transition,registry,cause,tokens,portability}.py`
(run with `--telemetry-no-skip` to fail on any skip) and the mutation control
`scripts/tests/telemetry_mutation_control.py` (`--control`, `--mutant NAME`; mutants live in
`scripts/tests/telemetry_mutants/`).

**Fault injection (tests only).** `AGENTCTL_TELEMETRY_FAULT` takes comma-separated tokens
`emit_fail`, `enospc_spool`, `enospc_record`, `enospc_marker`, `enospc`.
