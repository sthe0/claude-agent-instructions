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

- **Refusal and deny read differently.** A hook's deny reads `deny: <script> <reason>`; a refusal the bridge or
  the shim makes itself reads `refused: <class> <detail>`, the class one of `unclassified`, `argument`,
  `refused-tool`, `path-form`, `directory`, `project-config`, `config-dir`, `rows-snapshot`, `gate-setup`
  (`REFUSAL_CLASSES` in `scripts/lib/opencode_events.py`). Only a `deny:` reason is evidence that a denying row
  stopped a call; the adapter probe cases that rest on a denying row count nothing else.
- **Tool ids.** A tool id is judged as the Claude tool whose rows see it: a built-in through
  `BUILTIN_TOOLS` in `scripts/lib/opencode_events.py`, a plugin tool whose id is in `plugin_tools.ids` (the
  parity table, shipped empty, or the table-shaped `AGENT_GATE_EXTRA_ROWS` file), a tool of an MCP server the
  session's own config declares (`mcp` keys of the file `OPENCODE_CONFIG` names and of
  `OPENCODE_CONFIG_DIR/opencode.json`; the tool is `<server>_<tool>`). An id that fits both an MCP server
  and a plugin id is ambiguous and refused. Any other id is refused
  whatever rows exist, because the parity table has no matcherless PreToolUse row, and so is `patch`
  (below). Case A5 of the adapter probe phase re-checks the ids the pinned binary offers.
- **Rows snapshot.** The rows (Core table, layer roots, the extra-rows file) and the declared plugin tools are
  read once, at a session's first event, into `state/rows-snapshots/<session>.json`; the bridge's answer
  carries a `digest` of it and the shim sends it back as `rowsDigest` with every later event. A missing or
  different snapshot is refused (`refused: rows-snapshot`), as is a session with no id, so editing a rows file
  mid-session changes nothing for that session and a new process gets its own snapshot.
- **Project config.** OpenCode loads code from a project's own config (its `plugin` lists, `.opencode/plugin`)
  unless `OPENCODE_DISABLE_PROJECT_CONFIG=1` is set, and a session started without it, or with
  `OPENCODE_CONFIG_PROJECT_DISABLE`, is refused at `session.prompt` and `tool.execute.before`
  (`refused: project-config`). Probe A6 derives the routes from the pinned binary (the directory names its config
  discovery scans, the names its plugin loader lists, the config file names) and records each as `off`,
  `absent`, `unobserved` or `on`; one `on` stops the work (the gates cannot be secured), and
  `project_config_off` is true only when every member is `off` or `absent`.
- **Config directory.** A `write` or `edit` into the session's own config directory
  (`OPENCODE_CONFIG_DIR`) or the integration directory, and a shell command whose write targets
  (`scripts/lib/bash_write_targets.py`) fall there, is refused (`refused: config-dir`), since a plugin or rows
  file placed there would be loaded by a later session. Probe A6 `config_dir_write_refused` checks it with a
  live write, edit and shell redirect. Open limit: an interpreter-inline write (`python3 -c`, `node -e`)
  names no target the bridge reads, and passes.
- **Arguments.** `ARGUMENT_CLASSES` classifies every argument of every enabled built-in tool as mapped to a
  named hook-input field, as a path (resolved, see below), or as irrelevant to every denying row, with a
  one-line reason; an argument that changes where or what a tool reads, writes or runs is never irrelevant.
  A call with an argument the table does not list is refused. A test fails when the recorded schemas carry a
  tool or argument the table does not classify, and case A5 fails live when the pinned binary's schemas differ
  from the recorded ones.
- **Paths.** OpenCode resolves a path argument of a tool call against the directory of the call's
  session, and the bridge reproduces exactly the forms probe A6 recorded (`path_forms`, one resolver in
  `resolve_path` and the same rule in the shim's `resolveForm`): an empty or absent path is the session
  directory; `~`, `~/` and `~/x` are the home directory; a leading `/` is absolute (`//x` is `/x`); `..`,
  `./` and every other form, `~user`, `$HOME`, `${HOME}` and `file://...` included, are taken as ordinary
  relative components under the session directory; a backslash is an ordinary filename character. Any other
  form (`~//x`, a value that is no string, a NUL byte) is refused, as is any relative path when no directory
  is known. The shim asks `ctx.session.get({sessionID})` for the session directory and caches only a
  successful answer; when it fails, every call of that session is refused (logged
  `session directory unknown`) and the setup directory is never used in its place, since under
  `opencode serve` one process holds sessions of several directories. The bridge refuses a before-event that
  names no directory for the same reason, and no hook runs in the bridge's own cwd. A shell call's `workdir`
  is resolved with the same resolver and handed to the hooks as Claude's payload `cwd`. Setup without an
  absolute `ctx.location.directory` fails the marker.
- **Patch.** The `patch` tool (the only file editor the gpt model family is offered) cannot be switched
  off alone: probe A5 `switch_off` records that the permission actions `patch` and `apply_patch`, a
  legacy `permission` key and an agent's `tools: {patch: false}` remove nothing, while the action `edit`
  and a top-level `tools: {patch: false}` remove `edit`, `write` and `patch` together for every family. The
  bridge does not imitate how OpenCode applies a patch (the earlier reader and applier differed from it in
  details); it refuses every `patch` call before any row runs (`REFUSED_TOOLS`, A1 `root:patch`). Documented
  limit: a gpt-family model is offered `patch` and nothing else for editing, so it cannot edit files through
  the editor under the adapter; other families keep `edit` and `write`, which are mapped. Neither outcome is
  a gap the gates leave open. No gate path opens a file a tool names, so the shadow
  transcript (itself a credential path) holds only what the model wrote in the call.
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
- **Unreadable input to a denying hook.** A denying hook that allows because it could not read the call
  turns a refusal into a pass. [docs/operations/hook-unparsable-input-inventory.toml](../../docs/operations/hook-unparsable-input-inventory.toml)
  names every such branch of every PreToolUse/PermissionRequest hook the installer wires. Two hooks,
  `hook-guard-canon-readonly.py` and `hook-guard-permission-surface.py`, now deny a call whose required
  argument (`command`, `file_path`, `notebook_path`, `old_string`/`new_string`, `content`, `edits`) is
  absent, of the wrong type or a relative file path; the tests in `scripts/tests/test_hook_unparsable_input_contract.py` keep the verdicts
  on recorded Claude payloads equal to those of the stage-6 start ref. Every other row is left as it is,
  with its reason: a branch Claude can reach (an unparsable shell string, an `old_string` not found) keeps
  its allow, since a refusal there would change a Claude verdict; a branch fed by a tool argument and
  unreachable from Claude carries a `bridge_call` that the bridge refuses before any row runs.
  Not established: whether Claude Code rejects a relative `file_path` before its hooks run.
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
