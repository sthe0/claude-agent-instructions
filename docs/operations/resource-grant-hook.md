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
| Bash | every segment is covered **and nothing in the line may run git** | **all** of: the permission mode is `acceptEdits` (the only mode probe Q6 measured); the record states `permissions_allow` and `add_dirs`; every ambient settings file the harness merges with the record was read; no `permissions_allow` entry of the record **or of those files** matches the call even leniently; a segment writes a resource the record does not cover that is outside the child's cwd and every add-dir, the record's and the settings files' (or is not a file) | a line in which anything may run git, plan mode, an unresolved or undeterminable segment, a harness-allowed call, a record without the explicit lists, a settings file that cannot be read or parsed, an error |
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
prefix shelter an uncovered tail (`ls && touch /elsewhere`).

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

- **A line in which anything may run git.** The hook never returns an allow for such a line: it makes no decision
  (reason class `undeterminable`, message "the line runs git; the harness's static allow-list decides"), and never
  a deny on this basis. This holds for a git segment of any form, state and venue, covered, harness-allowed or
  matched by a rule pattern, read-only or writing, for pytest and the scripts of `scripts/script_effects.toml`,
  and for any segment whose resolved or judge-named effects include a ref change. *Functional ground:* git's execution surface is open-ended. A hook (`post-index-change`
  runs even on `git status`), a program the config names (`core.fsmonitor`, `filter.*`, `diff.external`,
  `core.sshCommand`, `gpg.program`, an `alias` starting with `!`), repository discovery (`GIT_DIR`, a `.git` file,
  `safe.directory`), attributes, a hook framework reading a file of the worktree and pathspec magic each start
  code nobody named, and every channel the hook recognised was one channel behind the next. No state of the
  repository can be verified as safe, so the hook does not pretend to; the child's static allow-list already
  covers the git the executor needs, and a git call it does not cover falls to the harness's own prompt.
  What counts as "may run git" (`runs_git`), over the whole line and with no pattern matching on meaning: the word
  `git` (not part of a longer name: `digit`, `github`, `.gitignore` do not count) in the line or in any
  shell-unquoted token of it, so `g'i't`, `\git`, `/usr/bin/git`, `env git`, `command git`, `xargs git`,
  `exec git`, `sh -c 'git ...'` and `git-upload-pack` all abstain; a segment holding the word `pytest` or
  naming a script of the registry (`land-branch.py`, `instruction-smoke-gate.py`: both run `git -C`, and pytest
  runs this repository's tests, which start git and `conftest.py`); a segment whose resolved or judge-named
  effects include a ref change (`VcsRefResource`), which is how a script or `make` target whose judged effects
  include a ref change is caught; and opaque code (a `-c` operand, a heredoc body, a script file) in which the
  word `git` occurs. The registry and pytest are matched by their tokens whether or not the resolver went on to
  resolve them, so no per-entry exemption exists. An entry could be exempted only together with pinning of its
  whole import closure, since the registry pins the entry file's sha256 and nothing it imports; that is out of
  scope here. For the same reason an edited import or `conftest.py` can no longer ride a registry or pytest
  allow: there is none. The resolver still resolves both, because the uncovered-write deny needs their
  resources; only the allow is withheld. A
  computed command word (`$X push`) never gets this far: the resolver leaves such a segment undeterminable. The
  rule applies once every segment is otherwise covered, so it never hides an uncovered-write deny: `git push origin
  HEAD:refs/heads/main` and `git push origin feat && touch /outside/f` are still denied.
- **An environment blocker.** While a `GIT_*` variable (or a `PYTHON*` variable other than the harmless ones) is
  exported in the hook's environment, every segment is undeterminable unless its program is a known non-carrier
  (`NON_CARRIER_PROGRAMS`: programs that neither run git nor python nor a shell nor an interpreter, e.g. `ls`,
  `cat`, `touch`). The alternative, blocking only `git` and `python`, lets a wrapper (`env`, `make`, `bash -c`,
  an arbitrary script) carry the variable into a git or python process; the allow-list errs toward a prompt.
- **Opaque code that writes nothing the judge can name**, because what it reads is not confined.

The resolver's git classification (`segment_resolver.GIT_WRITE_FORMS`, the commit and push `VcsRefResource`
effects) is kept for two readers that still need it: the uncovered-write deny, which must name the resource a
`git push` to a branch the plan does not own would change, and the replay answer key, which says what the plan
sanctions. It no longer serves any allow.

Every `cd <relative>` is resolved as if `CDPATH` were set (its value in the Bash shell cannot be read), so the
working directory after it is unknown. The judge calls of one hook call draw from a single deadline
(`JudgeBudget`): when it is spent a call is not started and the segment is undeterminable.

## The decision log

Every call appends one JSON row to `<state root>/resource-uses/<parent session>-<stage>.jsonl`:
`{ts, tool, call_sha, decision, reason_class, resources, venue, matched, reason}` (`call_sha` is the hash of the
whole call text, not of one segment; `reason` is the free-text cause of a no-decision row, e.g. the line-runs-git
message or an error, and empty otherwise). `decision` is `allow`, `deny` or
`none`. `reason_class` is one of:

| reason_class | meaning |
|---|---|
| `covered` | an allow: the call's changes are inside the record |
| `opaque-judged` | an allow whose changes came from `code_effects` (the isolated judge read the code) |
| `uncovered-write` | a deny (Bash), or an Edit/Write outside the record (no decision) |
| `harness-allowed` | the child's allow list or the permission mode already runs the call; no decision |
| `undeterminable` | the resolver or the code reader cannot name the effect, or the line may run git; no decision |
| `plan-mode` | the permission mode is plan; no decision |
| `kill-switch` | the switch is off |
| `inactive` | not a linked child, or the record fails verification |
| `error` | the hook raised; no decision, never an allow |

`matched` names what decided: the allow entry, the rule pattern, `mode:acceptEdits`, `record-resource` or
`parent-scratchpad`. A hook timeout is the harness's: the call falls through to its normal flow (probe Q5).

## Residuals

These are the places where the hook sees less than the Bash tool does. Most fail toward "no decision" or a prompt.
**Four can still yield an ALLOW** and are marked so: the shell snapshot divergence, git started by a program the
line does not show, opaque code reads, and the parent scratchpad.

- **Shell snapshot divergence (can yield an ALLOW).** The hook runs in the hook process's environment, not the Bash
  tool's shell snapshot. A `PYTHON*` variable set only inside the snapshot changes what a python segment does
  without the hook seeing it, so such a segment can still be allowed. The same variable exported in the hook's own
  environment makes the segment `undeterminable`. `CDPATH` is closed instead: every `cd <relative>` is resolved as
  if it were set, so it leaves the working directory unknown. `HOME` and `XDG_CONFIG_HOME` set only in the
  snapshot are a further residual of the same kind; the resolver reads the push configuration from the hook's own
  environment. A `GIT_*` variable no longer matters for an allow, since a line that runs git gets none.
- **Git started by a program the line does not show (can yield an ALLOW).** `runs_git` reads the line, pytest and
  the registry scripts, the effects the resolver or the judge names and the code of an opaque segment. What it does
  not model is a program that is neither pytest nor a registry script and starts git without the line naming it: a
  `make` target, an alias or a binary the line does not show, and a script file read as opaque code whose judged
  effects name no ref change (the judge reports writes, so read-only git inside code it clears is invisible). Such a
  segment is held only to the environment blocker and its own coverage. The static allow-list of the child stays the
  only guard for what such a program then runs.
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
  record and the scratchpad. The reads of an opaque segment are never confined, whether the judge named a write or
  not: only its writes are checked.
- **Ambient settings.** The deny reads the settings files listed under Decisions at decision time, so it follows
  what the harness merges now; it cannot see an `--add-dir` or a permission rule given to the child only on its
  command line beyond what the record states, which is why a record without those lists never denies.
- **The parent scratchpad (can yield an ALLOW of a Read)** is recognised by layout: a realpath under the OS temp root with consecutive
  components `<parent session id>/scratchpad`. A symlink out of it resolves to where it points and is not scratchpad.
- **Unresolved shapes.** `git push <remote> x:x`, `git -C <other repository> commit` and similar forms stay
  unresolved; they make no decision, and no line that runs git gets one in any case.
- **A resumed child** gets its record renewed: the spawn writes a fresh record from the settings the resumed child
  is launched with and adopts it (`refresh_resumed_child_link`), so `resources`, `permissions_allow` and `add_dirs`
  are current. A record that still lacks `permissions_allow` or `add_dirs` is never used to deny.
- **Extra reason class.** `harness-allowed` is a reason class beyond the original brief's list; it records a call
  the hook declined to touch because another part of the permission surface runs it.

## Tests

`scripts/tests/test_hook_resource_grant_check.py` (unit; the section "a line in which anything may run git is never
allowed" holds the abstention rule and each spelling), `scripts/tests/test_resource_grant_replay.py` (every
case of `fixtures/resource_grant_replay/denials.jsonl` through the hook entry point, judge stubbed; the 13
sanctioned cases whose line runs git are listed in its `FALL_THROUGH` with the reason),
`scripts/tests/e2e_resource_grant_hook.py` (`--dry-run` asserts the registration; `--live` is a root-only run) and
the mutants named `hook-*` in `scripts/tests/resource_grant_mutation_control.py`, among them
`hook-allows-a-covered-git-line` (the abstention removed) and `hook-git-ref-effect-ignored`,
`hook-opaque-code-naming-git-ignored`, `hook-git-word-in-a-token-ignored` (a way the line can run git left unread),
`hook-resolved-program-starting-git-ignored` (pytest or a registry script allowed on the resolver's resources) and
`hook-git-abstention-has-no-message` (the abstention loses its `reason`, so any other undeterminable segment would
satisfy the tests). The unit and replay tests assert that `reason` is the git abstention's message, not only that no
decision was made. `plan-removed-runs-worktree-code-accepted` holds the load-time refusal of the removed
`grants.runs_worktree_code` field (`test_stage_grants.py`).

The replay patches the resolver's `git config` probe timeout (`_GIT_CONFIG_TIMEOUT_S`) and the timing constants
it lists in `PATIENT_CONSTANTS` so a loaded machine does not turn a probe into an `undeterminable`. The deadline
tests inject a clock (`JudgeBudget(..., clock=...)`) instead of sleeping.
