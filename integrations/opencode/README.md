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
- `plugin/` — the gate shim (`agent-gates.ts`) that OpenCode loads: it forwards each tool, prompt,
  shell and idle event to `scripts/opencode-gate-bridge.py`, which runs Core's unchanged hook
  scripts and answers allow or deny. A deny makes the shim throw, so the call is refused. The
  directory's `package.json` must carry both `main` and `exports`; with `main` alone OpenCode
  2.3.2 does not load the plugin and nothing is refused. Tests:
  `scripts/tests/test_opencode_gate_bridge.py`; live cases: `opencode-probe.py run --phase adapter`.
  Limits: a `shell` call run by the user or from the TUI (`!`) is not bound to an engine session,
  and Code Mode `execute` reaches no shell.

### What the shim refuses, and what it reports

- **Tool ids.** A tool id is judged as the Claude tool whose rows see it: a built-in through
  `BUILTIN_TOOLS` in `scripts/lib/opencode_events.py` (the gpt model family is offered `patch`, not
  `edit` and `write`; the shim judges each file of a patch as a `Write` or `Edit` call), a plugin
  tool named in `AGENT_GATE_PLUGIN_TOOLS`, a tool of an MCP server named in `AGENT_GATE_MCP_SERVERS`
  (comma separated; the launcher passes the servers of the session's config). Any other id is refused
  whatever rows exist, because the parity table has no matcherless PreToolUse row. Case A5 of the adapter
  probe phase re-checks the ids the pinned binary offers.
- **Paths.** OpenCode resolves a relative path of a tool call against the directory of the call's
  session (probe A6, `ctx`). The shim asks `ctx.session.get({sessionID})` for that directory per session
  (a hook input names none), falls back to `ctx.location.directory` of setup, and never to the process cwd;
  the bridge joins every path it hands a hook (read, write, edit, patch files, grep and glob paths) to it, so a
  row always sees an absolute path. A relative path with no known directory is refused. Setup without an
  absolute `ctx.location.directory` fails the marker. Limit: under `opencode serve` one process holds sessions
  of several directories; the per-session lookup covers them, but a failed `ctx.session.get` falls back to the
  setup directory, which may not be the session's.
- **Patch.** Each file of a `patch` call reaches the rows as the Claude call that makes the same change: a
  `Write` for an added file; an `Edit` whose `old_string` is the file as it is and `new_string` the file as the
  patch leaves it for an update; an `Edit` that empties the file for a delete; for a move a `Write` of the new
  text to the target plus an `Edit` that empties the source. The bridge reads the file and applies the hunks the
  way OpenCode 2.3.2 does (exact match, then right-trimmed, then fully trimmed; probe A6 `patch_tool.files`);
  a patch it cannot read or apply is refused, never passed content-blind. OpenCode trims an operation line and
  its path before reading them (an indented `*** Add File:` is an operation, A6 `indent-add`, `tab-add`,
  `trailing-add`), and the bridge does the same, so no operation line escapes the rows.
- **Grep.** The credential check compares real paths (symlinks resolved on both sides, a tail that does
  not exist yet kept), against the session directory above.
  A result is filtered per file block; when the search root holds a credential path and the text is not
  the recognised `Found N matches` shape (the exact `No matches found` of a search with no hit passes), the
  whole text is withheld. Every field of the result is filtered or, under such a root, emptied whatever its
  type (string, object, array, a non-array `content`, any part field other than `type`); what OpenCode 2.3.2
  sends to the model is `content[].text`. Facts (probe A6 `search_and_output`): grep does not follow
  symlinks below its root (a word held only behind a directory link and a file link is "No matches found"),
  so a link to a credential file under a searched root is not read by grep; a long shell output is cut in the text a plugin sees and its full text saved to
  `xdg/data/opencode/shell/<project>/sh_*.out`; the unfiltered grep text a tool produced is stored in
  `opencode.db` before `tool.execute.after` runs. Those two stores hold text the filter cannot reach: the
  stages 9/10 must keep them out of the model's reach (a documented limit of this shim, not closed here).
- **Launcher duty.** The setup marker is only ever overwritten; the launcher (stages 9/10) deletes the one of an
  earlier launch before it starts OpenCode.
- **Setup.** OpenCode skips, silently, a plugin whose setup throws, so setup does not throw. When
  `AGENT_GATE_SETUP_MARKER` names a file, the shim writes `{"status": "ok" | "failed", "failures": [...]}`
  to it once setup ran; a launcher (the stage-9/10 preflight) treats a missing marker or `failed` as an
  unusable gate. A failed bridge leaves hooks that refuse every tool call; a `shell.create.before` that
  cannot register fails the marker and refuses `shell`. An unwritable `AGENT_GATE_LOG` is ignored.
- **Parity with Claude's hook protocol, on purpose.** A hook that times out or exits with a code other than
  0 or 2 allows the call, and a bridge failure on the turn-end event ends the turn.

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
