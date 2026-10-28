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
| Bash | every segment is covered | **all** of: the permission mode is `acceptEdits` (the only mode probe Q6 measured); the record states `permissions_allow` and `add_dirs`; every ambient settings file the harness merges with the record was read; no `permissions_allow` entry of the record **or of those files** matches the call even leniently; a segment writes a resource the record does not cover that is outside the child's cwd and every add-dir, the record's and the settings files' (or is not a file) | plan mode, an unresolved or undeterminable segment, a harness-allowed call, a record without the explicit lists, a settings file that cannot be read or parsed, an error |
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
The entries are not only the record's: at decision time the hook reads the settings files the harness merges with
the child's `--settings` (the user-level `.claude` directory, the `CLAUDE_CONFIG_DIR` / `CLAUDE_AGENT_HOME` roots, `.claude/settings*.json`
under the cwd and the worktree root, the managed file) and adds their `permissions.allow` entries to the lenient
match and their `additionalDirectories` to the child's working directories. A file that exists but cannot be read
or is not a JSON object with well-formed `permissions` leaves the hook without a decision; an absent file is empty.
The price is recall: of the 7 replay cases whose answer key says deny, 3 are still
denied (`uncovered-write-04..06`); the others (a redirect or a multi-line command under a `Bash(...)` entry, and
`git -C <other repository> commit`) fall to the harness. Plan mode makes no decision at all: its behaviour was not
measured.

A deny names the resource and carries a `PERMISSION-REQUEST:` template with `Rule:` and `Resource:` lines. A deny
from another hook or a settings deny rule still wins over this hook's allow (probe notes Q3/Q4), so the hook
cannot widen past an existing guard.

Segments the resolver or the code reader cannot name are `undeterminable` and never allowed. Three further cases
that look covered are also made undeterminable:

- **An environment blocker.** While a `GIT_*` variable (or a `PYTHON*` variable other than the harmless ones) is
  exported in the hook's environment, every segment is undeterminable unless its program is a known non-carrier
  (`NON_CARRIER_PROGRAMS`: programs that neither run git nor python nor a shell nor an interpreter, e.g. `ls`,
  `cat`, `touch`). The alternative, blocking only `git` and `python`, lets a wrapper (`env`, `make`, `bash -c`,
  an arbitrary script) carry the variable into a git or python process; the allow-list errs toward a prompt.
- **A git run, held to a trusted snapshot.** git executes code nobody named: a hook (`post-index-change` runs
  even on `git status`), a symlinked hook entry, a program the config names (`core.fsmonitor`, `filter.*`,
  `diff.external`, `core.sshCommand`, `gpg.program`, an `alias` starting with `!`), attributes, a hook framework
  reading a file of the worktree. Recognising each channel as it appeared was always one channel behind, so the
  spawn takes **one digest of all of them** instead (`scripts/agentctl/git_surface.py`, `snapshot_venues`) for the
  repositories of the child's working directory and delivery venue, and puts it into the handoff record
  (`git_surface`, covered by the record's sha256). It is taken in `spawn-specialist.py`'s record builder, before
  the child can change anything. What it pins, per repository: every hooks directory (the default one and
  `core.hooksPath`; each entry but `*.sample`, its mode and, for a symlink, the realpath and the bytes of the
  target), every config entry that names a program or a hook path together with the bytes of that program
  (`EXEC_KEY_PATTERNS`, `alias.*`/`submodule.*.update` starting with `!`), `.gitattributes` (top level and tracked
  nested), `info/attributes`, `core.attributesFile`, and the framework files `.pre-commit-config.yaml`,
  `lefthook.yml`, `.lefthook.yml`, `lefthook.yaml` and the `.husky/` tree.
  A segment that runs git (a `git` segment of any state, covered or harness-allowed, read-only or writing, or a
  segment whose resolved effects include a ref) is allowed only when the live digest of its repository equals the
  snapshot, and a line that holds such a segment is held to two more rules. First, no other segment may be able to
  change the surface before git runs: a neighbour that is opaque, covered only by a rule pattern (its writes were
  never resolved), harness-allowed and not read-only, a git subcommand that rewrites tracked files (`checkout`,
  `restore`, ...), or a covered write onto a pinned path makes the line undeterminable; a lone git segment has no
  neighbour but must name no git option other than `-C <dir>` (`git -c core.hooksPath=... commit` moves what git
  executes). Second, any difference, a repository the snapshot does not list, a surface that cannot be read, or a
  record without the snapshot (an old format) is undeterminable, never an allow; the hook never denies on this
  basis. A directory that is not in a repository runs no hook. Config entries that name nothing executable are
  not pinned, so the child's own `git push -u` (it writes `branch.*`) leaves the digest alone, and a push of the
  plan-declared branch in a repository with tracked hooks (this repository: `core.hooksPath=githooks`) is
  allowed as long as the tracked hooks are what they were at spawn. A line that only adds and commits beside a
  push, `git add a && git commit -m x && git push origin b`, is allowed too, whether the add and the commit are
  covered or run by an allow entry of the child (each is a git segment held to the same snapshot, and neither
  rewrites tracked files); `git restore hook && git push` and `git checkout -- dir && git push` are not, because
  the restore or checkout may rewrite a hook. So a push never gets an allow behind a neighbour that rewrites
  tracked files, an opaque one, a rule-pattern one or a harness-allowed one that is not read-only.
- **Opaque code that writes nothing the judge can name**, because what it reads is not confined.

Every `cd <relative>` is resolved as if `CDPATH` were set (its value in the Bash shell cannot be read), so the
working directory after it is unknown. The hook's git probes (the surface capture) draw from the same per-call
deadline as the judge calls: when the deadline is spent a probe is not started and the segment is undeterminable.

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
**Six can still yield an ALLOW** and are marked so: the shell snapshot divergence, code a trusted git hook or
filter runs by content, git run by other programs, the cross-call hook chain, opaque code reads, and the parent
scratchpad.

- **Shell snapshot divergence (can yield an ALLOW).** The hook runs in the hook process's environment, not the Bash
  tool's shell snapshot. A `GIT_*` (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*`) or `PYTHON*` variable set only
  inside the snapshot changes what a git or python segment does without the hook seeing it, so such a segment can
  still be allowed. The same variable exported in the hook's own environment makes the segment `undeterminable`.
  `CDPATH` is closed instead: every `cd <relative>` is resolved as if it were set, so it leaves the working
  directory unknown. `HOME` and `XDG_CONFIG_HOME` set only in the snapshot are a further residual of the same kind;
  the resolver reads the push configuration from the hook's own environment.
- **Code a trusted hook or filter runs by content (can yield an ALLOW).** The snapshot pins the hook, the
  program a config entry names and the framework files, not what they go on to execute. A hook that execs a
  file of the worktree, `.husky/` scripts, the `pre-commit` framework reading `.pre-commit-config.yaml`, runs
  whatever that file holds when git runs, including code the child wrote after the spawn. The hook's own file and
  the framework configuration are pinned; the files they run are not. In this repository `githooks/pre-push`
  execs `scripts/instruction-smoke-gate.py` and `githooks/pre-commit` execs `scripts/verify-all.py --staged`, so a
  child that edits those scripts, which its plan resources may allow, gets them run by an allowed `git push` or
  `git commit` without the hook seeing it. Untracked nested `.gitattributes` and config keys outside
  `EXEC_KEY_PATTERNS` are likewise not pinned.
- **Git run by something that is not a git segment (can yield an ALLOW).** An opaque script, an `env`, `make` or
  `bash -c` wrapper starts git itself, and a script the judge saw push is the only such case modelled (its ref
  effect makes the segment a git runner). A wrapper that starts git without a ref effect is held to the
  environment blocker and its own coverage, not to the snapshot.
- **The cross-call chain (can yield an ALLOW).** The snapshot is compared at the time of each call. A harness-allowed
  `git commit` that commits a hook into a tracked hooks directory in call 1 is declined by the hook (the next
  call's digest differs, so call 2 is undeterminable), but call 1 itself ran under the harness, not under this
  hook's allow. The hook can decline to allow; it cannot undo what the harness ran.
- **The snapshot is re-taken per spawn.** A re-dispatch or a resumed child gets a fresh record
  (`refresh_resumed_child_link`) and therefore a fresh snapshot: whatever the child left in the surface before the
  resume is trusted from then on. The global git configuration is read at hook time with the child's
  environment and at spawn time with the parent's; a systematic difference makes every git run undeterminable
  (fail closed), which `scripts/tests/e2e_resource_grant_hook.py --dry-run` would show.
- **Launch directory.** Project settings are read from `launch_dir` in the record (the directory the child was
  started in) and its worktree root, not from the hook's current directory; a record without it falls back to the
  cwd the hook sees.
- **`PYTHONPATH`, `PYTHONHOME`, `PYTHONSAFEPATH` in the hook process** make `code_effects` refuse every opaque
  segment, so such calls are `undeterminable` (no decision). The hook is registered as `python3 -I` and locates
  `agentctl` relative to its own file, never through `PYTHONPATH`.
- **Opaque code reads (can yield an ALLOW).** The isolated judge expresses writes (files, git refs); the files an
  opaque body reads are not confined, and the judge is a model, so a write it fails to name is not seen. An opaque
  segment is allowed only when the judge names at least one write and all of its writes are covered; code that
  writes nothing is `undeterminable`. The read confinement of a judged segment applies to the writes the judge
  named, not to the code's own reads. Non-opaque read-only programs have their path operands checked against the
  record and the scratchpad; git operands are repo-relative and exempt. The reads of an opaque segment are
  never confined, whether the judge named a write or not: only its writes are checked.
- **Ambient settings.** The deny reads the settings files listed under Decisions at decision time, so it follows
  what the harness merges now; it cannot see an `--add-dir` or a permission rule given to the child only on its
  command line beyond what the record states, which is why a record without those lists never denies.
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

`scripts/tests/test_hook_resource_grant_check.py` (unit), `scripts/tests/test_git_surface.py` (the snapshot's
digest and comparison), `scripts/tests/test_resource_grant_replay.py` (every
case of `fixtures/resource_grant_replay/denials.jsonl` through the hook entry point, judge stubbed),
`scripts/tests/e2e_resource_grant_hook.py` (`--dry-run` asserts the registration; `--live` is a root-only run) and
the mutants named `hook-*` in `scripts/tests/resource_grant_mutation_control.py`.

The replay patches the hook's `GIT_PROBE_TIMEOUT_S` (and the other timing constants it lists in
`PATIENT_CONSTANTS`) so a loaded machine does not turn a git probe into an `undeterminable`; three replay cases
failed intermittently under load before that, and their ids were not recorded, so the fix covers the cause rather
than the cases. The deadline tests inject a clock (`JudgeBudget(..., clock=...)`) instead of sleeping.
