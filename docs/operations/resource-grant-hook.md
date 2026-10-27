# Resource-grant hook: what it decides, how it fails, what it cannot see

`scripts/hook-resource-grant-check.py` is a PreToolUse hook that runs only in a spawned, linked child. It
removes one difficulty: a headless child stops at an approval prompt nobody can answer for a call whose effect
the plan already sanctioned, because the call's spelling is not one the child's allow list matches. The measured
harness behaviour it rests on is in [resource-grant-hook-harness-probe.md](resource-grant-hook-harness-probe.md).

## Where it runs

`spawn-specialist.py` registers it in the child's `--settings` (`hooks.PreToolUse`, matcher
`Bash|Edit|Write|MultiEdit|NotebookEdit|Read`, timeout = config row `resource-grant-hook-timeout-s`, 20 s) when
the child is linked and the kill switch is on. The norm-handoff record the child receives
(`AGENTCTL_NORM_HANDOFF`) carries what the hook needs: the stage's typed `resources`, its wildcard-tail
`rule_patterns`, and the child's final `permissions_allow` (kind baselines included).

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
| Bash | every segment is covered | a resolved segment writes a resource the record does not cover **and** no `permissions.allow` entry matches that segment **and** the permission mode does not already run it | an unresolved or undeterminable segment, a harness-allowed call, an error |
| Edit, Write, MultiEdit, NotebookEdit | the file is covered by a record resource | never | everything else |
| Read | the path is a record resource or lies in the parent session's scratchpad | never | everything else |

A segment is *covered* when it matches a record `rule_pattern`, or when everything it changes lies within the
record's `resources` and everything a read-only segment reads lies within the record's resources or the parent
scratchpad. Matching is **per segment** and **strict** (`rule_matches`): an exact spec or a `prefix:*` spec on a
word boundary, and never a segment containing a redirect, substitution or further command. The harness's own
whole-call matcher errs toward covering when any one segment matches; using it here would let an allowed prefix
shelter an uncovered tail (`git status && touch /elsewhere`).

A deny names the resource and carries a `PERMISSION-REQUEST:` template with `Rule:` and `Resource:` lines. A deny
from another hook or a settings deny rule still wins over this hook's allow (probe notes Q3/Q4), so the hook
cannot widen past an existing guard.

## The decision log

Every call appends one JSON row to `<state root>/resource-uses/<parent session>-<stage>.jsonl`:
`{ts, tool, segment_sha, decision, reason_class, resources, venue, matched}`. `decision` is `allow`, `deny` or
`none`. `reason_class` is one of:

| reason_class | meaning |
|---|---|
| `covered` | an allow: the call's changes are inside the record |
| `opaque-judged` | an allow whose changes came from `code_effects` (the isolated judge read the code) |
| `uncovered-write` | a deny (Bash), or an Edit/Write outside the record (no decision) |
| `harness-allowed` | the child's allow list or the permission mode already runs the call; no decision |
| `undeterminable` | the resolver or the code reader cannot name the effect; no decision |
| `kill-switch` | the switch is off |
| `inactive` | not a linked child, or the record fails verification |
| `error` | the hook raised; no decision, never an allow |

`matched` names what decided: the allow entry, the rule pattern, `mode:acceptEdits`, `record-resource` or
`parent-scratchpad`. A hook timeout is the harness's: the call falls through to its normal flow (probe Q5).

## Residuals

These are the places where the hook sees less than the Bash tool does. Each fails toward "no decision" or toward
a prompt, except where noted.

- **Shell snapshot divergence.** The hook runs in the hook process's environment, not the Bash tool's shell
  snapshot. `CDPATH`, `GIT_*` (`GIT_DIR`, `GIT_CONFIG_*`), `HOME`, `XDG_CONFIG_HOME` and `PYTHON*` set only inside
  the snapshot change what a command does without the hook seeing it. The resolver reads the push configuration
  from the hook's own environment.
- **Git hooks.** A resolved `git commit` or `git push` runs the venue's git hooks (`core.hooksPath`,
  `.git/hooks/pre-push`, ...), which can write anywhere. The hook models the git ref effect, not the hooks'.
- **`PYTHONPATH`, `PYTHONHOME`, `PYTHONSAFEPATH` in the hook process** make `code_effects` refuse every opaque
  segment, so such calls are `undeterminable` (no decision). The hook locates `agentctl` relative to its own file,
  never through `PYTHONPATH`.
- **Opaque code reads.** The isolated judge expresses writes (files, git refs); the files an opaque body reads are
  not confined. Only the writes decide an opaque segment. Non-opaque read-only programs have their path operands
  checked against the record and the scratchpad; git operands are repo-relative and exempt.
- **The parent scratchpad** is recognised by layout: a realpath under the OS temp root with consecutive
  components `<parent session id>/scratchpad`. A symlink out of it resolves to where it points and is not scratchpad.
- **Unresolved shapes.** `git push <remote> x:x`, `git -C <other repository> commit` and similar forms stay
  unresolved and therefore make no decision; the child hits the harness's prompt for them.
- **A resumed child** keeps the handoff record it was first spawned with; one written before `permissions_allow`
  existed has no allow list, so the hook cannot see a harness-allowed call there and may deny a write the
  harness would have run.
- **Extra reason class.** `harness-allowed` is a reason class beyond the original brief's list; it records a call
  the hook declined to touch because another part of the permission surface runs it.

## Tests

`scripts/tests/test_hook_resource_grant_check.py` (unit), `scripts/tests/test_resource_grant_replay.py` (every
case of `fixtures/resource_grant_replay/denials.jsonl` through the hook entry point, judge stubbed),
`scripts/tests/e2e_resource_grant_hook.py` (`--dry-run` asserts the registration; `--live` is a root-only run) and
the mutants named `hook-*` in `scripts/tests/resource_grant_mutation_control.py`.
