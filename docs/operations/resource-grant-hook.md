# Resource-grant hook: what it decides, how it fails, what it cannot see

`scripts/hook-resource-grant-check.py` is a PreToolUse hook that runs only in a spawned, linked child. It
removes one difficulty: a headless child stops at an approval prompt nobody can answer for a call whose effect
the plan already sanctioned, because the call's spelling is not one the child's allow list matches. The measured
harness behaviour it rests on is in [resource-grant-hook-harness-probe.md](resource-grant-hook-harness-probe.md).

## Where it runs

`spawn-specialist.py` registers it in the child's `--settings` (`hooks.PreToolUse`, matcher
`Bash|Edit|Write|MultiEdit|NotebookEdit|Read`, command `python3 -I <hook path>` so a `PYTHONPATH` or a planted
module in the working directory cannot shadow the hook's imports, timeout = config row
`resource-grant-hook-timeout-s`, 20 s) when the child is linked and the kill switch is on. The norm-handoff record
the child receives (`AGENTCTL_NORM_HANDOFF`) carries what the hook needs: the stage's typed `resources`, its
wildcard-tail `rule_patterns`, the child's final `permissions_allow` (kind baselines included) and `add_dirs` (every
directory the child is started with). Both lists are copied from the same settings dict the child is launched with,
by `attach_child_permission_surface`, so they cannot drift from it.

The hook acts only when `AGENT_RECURSION_DEPTH >= 1` and the record's bytes match the recorded sha256. At the root,
in an unlinked child, or with a tampered record it makes no decision (a linked-but-unverifiable call is logged as
`inactive` under `resource-uses/_unlinked.jsonl`; a depth-0 call writes nothing).

Kill switch: config row `resource-grant-hook` (`on` / `off`), overridden by `AGENTCTL_RESOURCE_HOOK=1|0`. Anything
other than exactly `on` disables the hook; the harness then decides as it would without it. The spawn registers
nothing when the switch is off, and the hook itself checks it again, so flipping the row stops a running child at
its next call.

## Decisions

| Tool | allow | deny | no decision |
|---|---|---|---|
| Bash | every segment is covered | **all** of: the permission mode is `acceptEdits` (the only mode probe Q6 measured); the record states `permissions_allow` and `add_dirs`; no `permissions_allow` entry matches the call even leniently; a segment writes a resource the record does not cover that is outside the child's cwd and every add-dir (or is not a file) | plan mode, an unresolved or undeterminable segment, a harness-allowed call, a record without the explicit lists, an error |
| Edit, Write, MultiEdit, NotebookEdit | the file is covered by a record resource | never | everything else |
| Read | the path is a record resource or lies in the parent session's scratchpad | never | everything else |

The two directions are deliberately not mirror images. The **allow** reading is strict and fails toward a prompt;
the **deny** is a narrow positive case: the hook denies only where it can show the harness would stop the call, and
anything it cannot show falls to the harness.

A segment is *covered* when it matches a record `rule_pattern`, or when everything it changes lies within the
record's `resources` and everything a read-only segment reads lies within the record's resources or the parent
scratchpad. Allow matching is **per segment** and **strict** (`rule_matches`): an exact spec or a `prefix:*` spec
on a word boundary, and never a segment containing a redirect, substitution or further command. The harness's own
whole-call matcher errs toward covering when any one segment matches; using it for the allow would let an allowed
prefix shelter an uncovered tail (`git status && touch /elsewhere`).

The deny reads `permissions_allow` the other way round, with `permission_entry_match.covers` on the whole call:
that matcher ignores redirects and `$`, counts a newline as an unresolved separator and so lets any `Bash(...)`
entry cover a multi-line call. An entry it accepts may be one the harness accepts too, so the hook does not deny.
The price is recall: of the 7 replay cases whose answer key says deny, 3 are still
denied (`uncovered-write-04..06`); the others (a redirect or a multi-line command under a `Bash(...)` entry, and
`git -C <other repository> commit`) fall to the harness. Plan mode makes no decision at all: its behaviour was not
measured.

A deny names the resource and carries a `PERMISSION-REQUEST:` template with `Rule:` and `Resource:` lines. A deny
from another hook or a settings deny rule still wins over this hook's allow (probe notes Q3/Q4), so the hook
cannot widen past an existing guard.

Segments the resolver or the code reader cannot name are `undeterminable` and never allowed. Three further cases
that look covered are also made undeterminable: a `git`/`python` segment while a `GIT_*` (or `PYTHON*`, apart from
the harmless ones) variable is exported in the hook's environment; a `git commit`/`merge`/`push` whose effective
hooks directory (`git rev-parse --git-path hooks`, which follows `core.hooksPath`) lies inside a writable resource
and holds a hook for that operation, or may receive one from another segment of the same line; and opaque code whose
judged effects contain no write, because what it reads is not confined. Every `cd <relative>` is resolved as if
`CDPATH` were set (its value in the Bash shell cannot be read), so the working directory after it is unknown.

## The decision log

Every call appends one JSON row to `<state root>/resource-uses/<parent session>-<stage>.jsonl`:
`{ts, tool, call_sha, decision, reason_class, resources, venue, matched}` (`call_sha` is the hash of the whole
call text, not of one segment). `decision` is `allow`, `deny` or
`none`. `reason_class` is one of:

| reason_class | meaning |
|---|---|
| `covered` | an allow: the call's changes are inside the record |
| `opaque-judged` | an allow whose changes came from `code_effects` (the isolated judge read the code) |
| `uncovered-write` | a deny (Bash), or an Edit/Write outside the record (no decision) |
| `harness-allowed` | the child's allow list or the permission mode already runs the call; no decision |
| `undeterminable` | the resolver or the code reader cannot name the effect; no decision |
| `plan-mode` | the permission mode is plan; no decision |
| `kill-switch` | the switch is off |
| `inactive` | not a linked child, or the record fails verification |
| `error` | the hook raised; no decision, never an allow |

`matched` names what decided: the allow entry, the rule pattern, `mode:acceptEdits`, `record-resource` or
`parent-scratchpad`. A hook timeout is the harness's: the call falls through to its normal flow (probe Q5).

## Residuals

These are the places where the hook sees less than the Bash tool does. Most fail toward "no decision" or a prompt.
**Two can still yield an ALLOW** and are marked so.

- **Shell snapshot divergence (can yield an ALLOW).** The hook runs in the hook process's environment, not the Bash
  tool's shell snapshot. A `GIT_*` (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*`) or `PYTHON*` variable set only
  inside the snapshot changes what a git or python segment does without the hook seeing it, so such a segment can
  still be allowed. The same variable exported in the hook's own environment makes the segment `undeterminable`.
  `CDPATH` is closed instead: every `cd <relative>` is resolved as if it were set, so it leaves the working
  directory unknown. `HOME` and `XDG_CONFIG_HOME` set only in the snapshot are a further residual of the same kind;
  the resolver reads the push configuration from the hook's own environment.
- **Git hooks.** A resolved `git commit` or `git push` runs git hooks, which can write anywhere. The hook models the
  git ref effect, not the hooks'. It does not allow the operation when the effective hooks directory (follows
  `core.hooksPath`) lies inside a writable resource and holds a hook for that operation, or when another segment
  of the line may write there. A hooks directory outside the writable resources is not examined.
- **`PYTHONPATH`, `PYTHONHOME`, `PYTHONSAFEPATH` in the hook process** make `code_effects` refuse every opaque
  segment, so such calls are `undeterminable` (no decision). The hook is registered as `python3 -I` and locates
  `agentctl` relative to its own file, never through `PYTHONPATH`.
- **Opaque code reads.** The isolated judge expresses writes (files, git refs); the files an opaque body reads are
  not confined. An opaque segment is therefore allowed only when the judge names at least one write and all of its
  writes are covered; code that writes nothing is `undeterminable`. Non-opaque read-only programs have their path
  operands checked against the record and the scratchpad; git operands are repo-relative and exempt.
- **The parent scratchpad (can yield an ALLOW of a Read)** is recognised by layout: a realpath under the OS temp root with consecutive
  components `<parent session id>/scratchpad`. A symlink out of it resolves to where it points and is not scratchpad.
- **Unresolved shapes.** `git push <remote> x:x`, `git -C <other repository> commit` and similar forms stay
  unresolved and therefore make no decision; the child hits the harness's prompt for them.
- **A resumed child** gets its record renewed: the spawn writes a fresh record from the settings the resumed child
  is launched with and adopts it (`refresh_resumed_child_link`), so `resources`, `permissions_allow` and `add_dirs`
  are current. A record that still lacks `permissions_allow` or `add_dirs` is never used to deny.
- **Extra reason class.** `harness-allowed` is a reason class beyond the original brief's list; it records a call
  the hook declined to touch because another part of the permission surface runs it.

## Tests

`scripts/tests/test_hook_resource_grant_check.py` (unit), `scripts/tests/test_resource_grant_replay.py` (every
case of `fixtures/resource_grant_replay/denials.jsonl` through the hook entry point, judge stubbed),
`scripts/tests/e2e_resource_grant_hook.py` (`--dry-run` asserts the registration; `--live` is a root-only run) and
the mutants named `hook-*` in `scripts/tests/resource_grant_mutation_control.py`.
