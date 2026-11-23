# OpenCode host: hook parity

Difficulty removed: Claude Code wires a hook by event, matcher and script. OpenCode has plugin
events instead, so a gate that Claude Code enforces can be lost silently on OpenCode. The parity
table says, per wired hook, which OpenCode mechanism takes it over and on which execution paths
that mechanism was seen to refuse.

## What is here

- `parity.toml` — the Core layer's table: one `[[row]]` per row of the installer's `DESIRED` list
  (`scripts/install-reminder-hooks.sh`), 44 rows, preceded by the probe tables copied from the
  pinned mechanisms record.
- `scripts/hook-parity.py` and `scripts/lib/hook_parity.py` — the checker and its library.
- `scripts/tests/test_hook_parity.py` and `scripts/tests/fixtures/hook_parity/` — the tests and the
  one-row fixtures that prove each strict rule fires.

## Row format

| Key | Meaning |
|---|---|
| `script`, `event`, `matcher`, `args`, `timeout_s` | The hook as the layer wires it; the table must equal the wiring, timeout included. |
| `difficulty` | Required, non-empty: what goes wrong on the host if this hook is lost. |
| `opencode` | The OpenCode mechanism that carries the hook (`tool.execute.before`, `tool.execute.after`, `session.prompt`, `session.created`, `session.deleted`, `experimental.idle`, `permission.evaluate`, `child-session idle`) or `none`. |
| `paths` | The probe path ids the mechanism covers. |
| `gaps` | A path the row does not cover: `{ "<id>" = "switched_off:<setting>" }`. Never accepted for `child-run`; `K7` is never accepted. |
| `exempt` | `{ reason, covers_gap }`, only on a non-denying row. |

The class is computed from the event, never declared: only `PreToolUse` and `PermissionRequest`
can refuse a tool call, so only those rows are *denying*. A row for `hook-term-neutrality.py` is
denying although the hook only warns, because its event can refuse.

The record names mechanisms by Claude-style labels; `LABEL_OF` in `scripts/lib/hook_parity.py` is the
one map to the names a row uses (`PreToolUse` = `tool.execute.before`, `PostToolUse` =
`tool.execute.after`, `UserPromptSubmit` = `session.prompt`, `SessionStart` = `session.created`,
`SessionEnd` = `session.deleted`, `Stop` = `experimental.idle`).

`Stop` rows are carried by `experimental.idle`. The plugin shim must bound the continues a blocking
Stop hook can force; the shim is a later stage and sets that bound, because the pinned record
(case E6) shows the host itself has no guard against an unbounded idle loop.

## Checks

```
python3 scripts/hook-parity.py check --layer core            # plain rules C20-C25
python3 scripts/hook-parity.py check --layer core --strict   # plus S1-S4
python3 scripts/hook-parity.py check --layer-root DIR        # another layer: DIR/hook-parity.toml
python3 scripts/hook-parity.py start-notice ROOT...          # the lines shown at session start
```

Plain: C20 rows equal the layer's wiring; C21 `difficulty` present; C22 a layer root has a table
file; C23 an `exempt` entry has `reason` and `covers_gap` and is not on a denying row; C24 the
mechanism is a known one; C25 every listed path is a probe path.

Strict (all gated by `STRICT_DENYING_ROW_RULES`):

- S1 a denying row is exempt or mapped to `none`;
- S2 a denying row misses a probe path with no `switched_off:` gap, or claims a path its mechanism did
  not refuse;
- S3 the mechanism is not in the record with `fired` true (a denying row also needs `can_refuse`);
- S4 a non-denying row is mapped to `none` without `exempt`, or a `PostToolUse` row misses an executed
  path or lists one its mechanism did not fire on.

If a denying row cannot be mapped to a mechanism the record proves, do not weaken the check or
exempt the row: stop and report it with the record path (stopping rule K7).

## Refreshing the table

1. Run the probe against the installed binary and pin the passing mechanisms record
   (`scripts/opencode_probe/`, `pin.toml` `last_pass.mechanisms`).
2. Replace the `probe_paths` / `probe_mechanisms` blocks with `python3 scripts/hook-parity.py probe-header`.
3. Add or remove rows to match the installer; run `check --layer core --strict` and the tests.

`test_probe_paths_match_record` fails when the header differs from the pinned record.

## Other layers

A layer keeps `hook-parity.toml` beside its settings file (the storage directory its settings link
resolves to). `start-notice` names layers without a table, layers whose table fails `--strict`,
exempt rows, and hooks in the agent home's `settings.json` that no layer's table carries.
