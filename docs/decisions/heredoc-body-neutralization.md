# Decision: separate WHERE a here-document construct is from WHETHER it may be trusted

`scripts/lib/shell_tokens.py` answers one question — may a here-document body be REMOVED — under a
deliberately conservative seven-clause rule, and returns the command byte-for-byte unchanged whenever
any clause fails. That refusal is correct. What was wrong is that two consumers then handed the
unstripped text to `shlex` and read the body's bytes as shell syntax, so a body the module correctly
declined to trust got judged as code. This document records the three measured false positives, the
consumer enumeration that bounds the fix's blast radius, and (added by later stages) the API split,
the non-widening argument, and the review disposition.

## Three measured instances

- **I1** (issue #66) — `python3 - <<'PY'` / `print(text.count('word') > 14)` / `PY`. Clause (iv)
  fails (`python3` is deliberately absent from `CONSUMERS`), nothing is stripped, and
  `bash_write_targets.command_write_targets` reads the body's `>` as a redirect, returning
  `['<cwd>/14)']` — a phantom write-target candidate that denies a command that writes nothing.
- **I2** (comment on issue #71) — `cat > <file> <<'TXT' ... TXT` followed by a `python3 ...`
  invocation, whose body contains an ordinary English apostrophe. Clause (v) fails (the residue holds
  a later statement), the body stays, and the apostrophe leaves quotes unbalanced:
  `shlex.split(strip_heredoc_bodies(cmd))` raises `ValueError: No closing quotation`.
- **I3** (found while planning, unfiled) — `python3 - <<'PY'` whose body merely mentions
  `git commit` as a comment. `hook-guard-canon-readonly._is_git_commit` runs `shlex.split` on the
  unstripped text and finds the literal `git`/`commit` token pair inside the body, returning `True`.

## Consumer enumeration

Command run from the worktree root:

```
grep -rn "strip_heredoc_bodies\|command_write_targets\|shell_tokens\|bash_write_targets" --include='*.py' scripts/
```

Output, captured verbatim at revision `de5fae1b84642658a3f8587851b4820556df9d03` (31 hits). The last
three hits are this commit's own new test module, so the same command against `main` returns 28:

```
scripts/hook-guard-canon-readonly.py:38:inspected (`lib/shell_tokens.py`), so a Markdown blockquote line inside a body is
scripts/hook-guard-canon-readonly.py:60:from lib import bash_write_targets, config_root, git_cwd, shell_tokens  # noqa: E402
scripts/hook-guard-canon-readonly.py:227:    `cp`/`mv` targets) lives in `lib/bash_write_targets.py`, which knows nothing
scripts/hook-guard-canon-readonly.py:232:    for candidate in bash_write_targets.command_write_targets(command, eff_cwd):
scripts/hook-guard-canon-readonly.py:348:        command = shell_tokens.strip_heredoc_bodies(command)
scripts/tests/test_no_semantic_unguarded.py:97:        "one-hop transitive import lib.shell_tokens carries only "
scripts/tests/test_guard_canon_bash_writes.py:389:    Both call shapes are matched. `shell_tokens.strip_heredoc_bodies(...)` parses
scripts/tests/test_guard_canon_bash_writes.py:391:    `from lib.shell_tokens import strip_heredoc_bodies` parses to an `ast.Name` —
scripts/tests/test_guard_canon_bash_writes.py:401:            (isinstance(node.func, ast.Attribute) and node.func.attr == "strip_heredoc_bodies")
scripts/tests/test_guard_canon_bash_writes.py:402:            or (isinstance(node.func, ast.Name) and node.func.id == "strip_heredoc_bodies")
scripts/lib/permission_entry_match.py:42:from .bash_write_targets import _BASH_SEPS, split_segments
scripts/tests/test_heredoc_body_neutralization.py:37:from lib import shell_tokens  # noqa: E402
scripts/tests/test_heredoc_body_neutralization.py:109:    `strip_heredoc_bodies` (clause (v): the residue holds two statements) —
scripts/tests/test_heredoc_body_neutralization.py:120:    neutralized = shell_tokens.neutralize_heredoc_constructs(cmd)
scripts/tests/test_shell_tokens_nonwidening.py:1:"""Differential oracle for `lib/shell_tokens.strip_heredoc_bodies`: real bash is
scripts/tests/test_shell_tokens_nonwidening.py:64:from lib import shell_tokens  # noqa: E402
scripts/tests/test_shell_tokens_nonwidening.py:505:        stripped = shell_tokens.strip_heredoc_bodies(raw)
scripts/tests/test_shell_tokens_nonwidening.py:527:    # own. `exercised` is a pure function of `strip_heredoc_bodies` and the table
scripts/tests/test_shell_tokens_nonwidening.py:559:        stripped = shell_tokens.strip_heredoc_bodies(raw)
scripts/tests/test_shell_tokens_nonwidening.py:573:        if not shell_tokens._recognized(command):
scripts/tests/test_shell_tokens_nonwidening.py:575:        saved = shell_tokens._body_inert
scripts/tests/test_shell_tokens_nonwidening.py:576:        shell_tokens._body_inert = lambda delimiter_quoted, text: True
scripts/tests/test_shell_tokens_nonwidening.py:578:            residue = shell_tokens._strip_bodies(command)
scripts/tests/test_shell_tokens_nonwidening.py:580:            shell_tokens._body_inert = saved
scripts/tests/test_shell_tokens_nonwidening.py:581:        if residue != command and shell_tokens._holds_multiple_statements(residue):
scripts/lib/bash_write_targets.py:15:`lib/shell_tokens.py` before tokenizing, so a Markdown blockquote line inside a
scripts/lib/bash_write_targets.py:23:from . import shell_tokens
scripts/lib/bash_write_targets.py:148:def command_write_targets(command: str, eff_cwd: str) -> list[str]:
scripts/lib/bash_write_targets.py:151:    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
scripts/lib/bash_write_targets.py:154:    command = shell_tokens.strip_heredoc_bodies(command)
scripts/lib/shell_tokens.py:363:def strip_heredoc_bodies(command: str) -> str:
```

Classification of every hit:

- `scripts/hook-guard-canon-readonly.py` line 348 — the sole `strip_heredoc_bodies` call site inside
  `decide()`. **CHANGED** in stage 2: swapped for the neutralizer.
- `scripts/hook-guard-canon-readonly.py` lines 38 and 227 — prose in docstrings referencing the module by
  name, not a call site. **CHANGED** only insofar as stage 4 updates the prose to match the new
  mechanism (§ named residual block, § git_cwd relationship); not a code change.
- `scripts/hook-guard-canon-readonly.py` line 232 — the call into `bash_write_targets.command_write_targets`,
  which is where the second, internal `strip_heredoc_bodies` application lives (see R5/R6 below).
  **UNCHANGED** call site; its callee is migrated in stage 2 item C.
- `scripts/lib/bash_write_targets.py` line 154 — the second `strip_heredoc_bodies` call site.
  **CHANGED** in stage 2: swapped for the neutralizer.
- `scripts/lib/bash_write_targets.py` lines 15 and 151 — docstring prose naming the module, not a call site.
  **UNCHANGED** (or reworded to match, at the developer's discretion in stage 2 — no behavioural
  effect either way).
- `scripts/lib/shell_tokens.py` line 363 — the `strip_heredoc_bodies` definition itself.
  **UNCHANGED in its own signature and byte-for-byte output**; stage 2 rewrites its *implementation*
  to sit on top of the new shared region producer, with byte-identical output asserted as an
  invariant (D1c).
- `scripts/lib/permission_entry_match.py` line 42 — imports only `_BASH_SEPS` and `split_segments` from
  `bash_write_targets`, never calls `strip_heredoc_bodies` or `command_write_targets`. Its raw
  separator prescan (`covers()`) returns `True`/covering for any command containing a newline, which
  every here-document construct does. **UNAFFECTED**, not touched by this task.
- `scripts/tests/test_no_semantic_unguarded.py` line 97 — a pinned justification string quoting
  `shell_tokens`'s role ("carries only shell-token-syntax regexes"). **RE-VERIFIED, not touched**
  unless the pin's content hash drifts (see stage 1 step 4 below).
- `scripts/tests/test_shell_tokens_nonwidening.py` (multiple lines) — the differential bash oracle
  and its `test_oracle_goes_red_against_a_superseded_rule` internals, which call `strip_heredoc_bodies`
  and reach into `_recognized`/`_body_inert`/`_strip_bodies`/`_holds_multiple_statements` directly.
  **CHANGED** in stage 2: extended with the token-equivalence assertion (D1), the generated grid
  (D1a), the frozen-reference byte-identity control (D1c), and the new D2/D3 cases.
- `scripts/tests/test_guard_canon_bash_writes.py` lines 389-402 — `test_body_stripper_is_called_once_on_the_bash_path`,
  an AST gate asserting exactly one `strip_heredoc_bodies` call site inside `decide()`.
  **CHANGED** in stage 2: must assert the new function name instead, or it passes vacuously once the
  call site is renamed away.
- `scripts/tests/test_heredoc_body_neutralization.py` lines 37, 109 and 120 — this commit's own new
  test module, which is why the capture holds 31 hits where `main` holds 28. **SELF-REFERENCE, not a
  consumer to migrate:** it is the regression suite the fix must turn GREEN, and its line 120 already
  names `neutralize_heredoc_constructs`, the function stage 2 introduces.

No other hit exists. The claim "no other consumer is affected" rests on this enumeration, not on
recall.

## Test-side gates a rename would silently break

Recorded here for stage 2 to act on:

- `scripts/tests/test_guard_canon_bash_writes.py`, function `test_body_stripper_is_called_once_on_the_bash_path`
  — an AST assertion that exactly one call to `strip_heredoc_bodies` exists in the hook and that it
  sits inside `decide()`. It will need to name the new function instead, or it passes vacuously while
  the gate it encodes is gone.
- `scripts/tests/test_shell_tokens_nonwidening.py` — the differential bash oracle, extended in
  stage 2. Its 186-case corpus contains ZERO multi-here-string cases: `grep -cE '<<<.*<<<'` and
  `grep -cE '<<<.*<<[^<]'` both return `0` against the file, and all six of its two-operator cases are
  `<< <<`, which the stripper leaves verbatim (clause (v) rejects the two-statement residue). Stage 2
  closes that shape gap by CONSTRUCTION rather than by adding corpus rows: D1a's grid enumerates every
  ordered sequence of one, two and three constructs over the three operator forms (`<<`, `<<-`, `<<<`),
  and D1c's frozen-reference byte-identity control was extended to run over those 234 generated cells
  as well as the corpora — without which a producer narrowed to stop after the first construct stays
  byte-identical over every corpus row and both controls pass vacuously. A grid confined to one
  operator form and one construct per command, as the first stage-2 draft shipped, leaves the gap
  exactly where it was.
  Its rename risk is subtler than a symbol name and must be handled explicitly. The oracle measures a
  *differential* — real bash's behaviour against `guard_denies(raw)` versus `guard_denies(stripped)` —
  and once `decide()` neutralizes internally, `raw` and `stripped` both travel the same neutralizing
  path, so the differential stops measuring the transformation the hook actually ships while remaining
  perfectly green. Renaming the symbol it calls is therefore not enough: the oracle must be re-pointed
  at whichever function the hook now applies, so that what it compares is still the shipped transform.
- `scripts/tests/test_no_semantic_unguarded.py` — its pinned entry `c66f1c838758119e` justifies
  `hook-guard-canon-readonly.py` as structural on the ground that "its one-hop transitive import
  `lib.shell_tokens` carries only shell-token-syntax regexes ... never natural-language meaning".
  Re-verify that ground holds after stage 2: a word-identity allowlist (`NON_SHELL_CONSUMERS`) is not
  a natural-language regex, so it should, but the id is content-derived and may need re-stamping if
  the docstring changes.
- `scripts/tests/test_guard_canon_bash_writes.py`, function
  `test_heredoc_body_executed_by_later_statement_still_denies` (line 319) — the one gate that does not
  merely go vacuous but **inverts**. It asserts DENY on
  `cat <<'EOF' > /tmp/s.sh` / `echo x > scripts/existing.py` / `EOF` / `bash /tmp/s.sh`, i.e. on
  precisely the shape stage 2 trades away: an inert consumer PERSISTS a body that a later statement
  then executes. That trade is already named as accepted consequence #1 in the plan, on the ground that
  the guard cannot follow data through the filesystem into a second process and today's DENY is an
  accident of clause (v) rather than a reasoned protection. Stage 2 must invert this test to assert
  ALLOW **and cite the accepted consequence in its docstring**, so the record shows a DENY-to-ALLOW
  change that was decided, not one that was absorbed while a green suite hid it. Silently deleting or
  weakening it is a blocking defect.

## Questions answered

**What is the API split, and why a shared producer?** `_removal_regions(command, consumers) ->
list[tuple[int, int, str]] | None` is the one walk that locates every here-document/here-string
construct's removal region and decides doubt; it returns `None`, as before, on any of clauses
(i)/(ii)/(iii)/(vi)/(vii) failing. `_strip_bodies` (private) and the public `strip_heredoc_bodies`
consume it via `_apply_regions`, which COLLAPSES each region to its `collapse_text` — unchanged
external behaviour, verified byte-identical against a frozen independent reimplementation (D1c,
below) rather than assumed from the refactor being "obviously equivalent". `neutralize_heredoc_
constructs` (new) consumes the SAME region list but BLANKS each region with spaces (preserving every
`\n`, so line numbers a later error message might report stay stable) instead of collapsing it — the
output is the same length as the input, so a downstream `shlex` lexer sees whitespace where a body
was, never the body's bytes, without needing to trust that a later statement never executes it.
`heredoc_construct_spans` is the public span view (`list[tuple[int, int]]`) over the same regions, for
a caller that wants the spans rather than a transformed string.

**What does neutralization relax, and what stays absolute?** Clause (iv) (consumer allowlist) widens
via `NON_SHELL_CONSUMERS = frozenset({"python", "python3", "perl", "ruby", "node"})`, unioned with
`CONSUMERS` — a body fed to one of these is native code for that interpreter, never shell syntax, so
blanking it is safe on the same ground `CONSUMERS` already rests on, just for a different reason (the
body is inert to *this* parser, not because it is quoted or short). Clause (v) (residue holds one
statement) is DROPPED entirely: neutralization does not care whether a later statement executes the
blanked-away body, because blanking removes the body's TEXT from what `shlex` sees — it makes no
claim about what real bash does when it runs. Clauses (i)/(ii)/(iii)/(vi)/(vii) are unchanged and
absolute for both `strip_heredoc_bodies` and `neutralize_heredoc_constructs`: an unrecognized token, a
function definition, an unbalanced quote, or a non-inert body still refuses the whole command
(returns it byte-for-byte unmodified) rather than acting partially.

**What is the non-widening argument, concretely?** Seven independent controls in
`test_shell_tokens_nonwidening.py`, each catching a different way the migration could have gone wrong:
D1 (`test_strip_and_neutralize_agree_on_command_line_tokens`) checks the two appliers' outputs
`shlex`-tokenize identically wherever both act — a coherence check on the two CONSUMERS of
`_removal_regions`, which cannot by itself detect a bug that narrows the shared PRODUCER (documented
in D1's own docstring, since both appliers would shrink together and still agree, now vacuously). D1c
(`test_strip_bodies_matches_a_frozen_independent_reimplementation`) is the control that can: an
independently-coded, from-scratch character walk that shares no code with `_removal_regions`, checked
byte-identical against `strip_heredoc_bodies` over the full corpus, `FALSE_POSITIVES` and D1a's
generated cells. D1a (`test_neutralization_equivalence_grid`) is a generated grid over the four axes
the shape space has — operator form (`<<`, `<<-`, `<<<`), construct count (1, 2, 3, as ordered
sequences over those forms, so `<<<` then `<<` and `<<` then `<<<` are separate cells), the three
classes clause (iv) distinguishes (a `CONSUMERS` member, a `NON_SHELL_CONSUMERS`-only member, an
unrecognized name), and a trailing operand after the last operator — 3 × 39 × 2 = 234 cells, with
named floors (`ACTED_FLOOR_TEE`, `ACTED_FLOOR_NON_SHELL`, `ACTED_FLOOR_UNKNOWN`) so a future
consumer-set change states its expected effect on the grid rather than an unexplained number drifting.
Those three floors pin clause (iv)'s class membership and cannot see a narrowed producer (the
neutralizer acts as soon as ONE construct is found, whatever happens to the rest); `STRIPPED_FLOOR_TEE`
and `STRIPPED_ACTED_BY_COUNT` are the ones that can, because clause (v) rejects the leftover body lines
a walk that stopped early leaves behind — measured 30 stripper-acted cells (6 / 10 / 14 at counts
1 / 2 / 3), falling to 18 under a producer narrowed to return after its first `<<<` region. D2 pins
eighteen real writes that must still be denied today, in two sets: nine selected by name from the
pre-existing corpus (`test_nine_named_constructions_still_deny`, spanning the parse-desync,
shape-inside-recognized, inert-consumer-write, delimiter-quoting, bound-asymmetry and
reviewer-absolute-path families) and nine written out by construction
(`test_nine_enumerated_constructions_still_deny`), which is where the multi-construct and `<<-` shapes
the older corpus never held are pinned. R6 (`test_double_application_is_load_bearing`) pins the
composed hook path's TWO applications, at both the `command_write_targets` level and through `decide()`
itself, since collapsing them to one is a measured DENY-to-ALLOW widening on `tee <<'A' <<<xxx <canon>/f`.
The span view (`test_heredoc_construct_spans_are_the_neutralizers_own_span_view`) pins
`heredoc_construct_spans`, which stage 3 is the first caller of, against the neutralizer it is a view
of. D3 (`test_widened_consumer_body_introduces_no_new_spurious_deny`) checks the widened consumer set
itself does not open a new false positive, using a `ruby` body distinct from the `python3` cases
`test_heredoc_body_neutralization.py` already pins.

**Was any previously-denied real write in the corpus found to flip to allowed?** Two, and both are the
accepted clause-(v) trade named above in "Test-side gates a rename would silently break": "write then
exec" (`cat <<'EOF' > /tmp/s.sh` ... `bash /tmp/s.sh`) and "tee then source" (the `tee`/`.` sourcing
sibling of the same shape) — a body PERSISTED to a file and executed by a LATER, separate statement.
Dropping clause (v) means neutralization no longer refuses on a multi-statement residue, so these two
now allow, exactly as `test_heredoc_body_persisted_and_run_by_later_statement_now_allows` in
`test_guard_canon_bash_writes.py` pins directly. `test_body_removal_never_turns_a_real_write_from_
deny_into_allow` excludes these two names from its regression check by name, with a docstring pointing
at that pinning test, rather than either failing on an already-decided trade or silently swallowing it
into the aggregate pass.

**Measured numbers, so a future drift is legible against a real baseline rather than a guess.** Of the
corpus (`CORPUS = len(CASES)`), 93 constructions are EXERCISED by `neutralize_heredoc_constructs`
(pinned exactly — a pure function of the transform and the table, so any drift is a rule change), of
which 37 are BASH-REACHED (a measured floor of 30 is asserted, since this axis additionally depends on
the local shell and coreutils). "sort -o canon" is NOT among the nine D2 pins despite looking like an
obvious candidate: `sort -o` was never a detected write verb in `bash_write_targets.py` (only
`sed -i`/`tee`/`cp`/`mv`/`patch`/`git apply` are), heredoc or not — it is one of the nine PRE-EXISTING
bypasses named in `_COMMAND_LINE_WRITERS`'s "sort -o bare no heredoc", unaffected by this migration in
either direction.

## Review dispositions

Stage 3's independent adversarial reviewer returned **approve-with-nits** at commit `77cb774`, run via
`agentctl dispatch`. Central claim: the reviewer could not exhibit a DENY-to-ALLOW widening outside
the three named consequences (I1/I2/I3) plus the accepted clause-(v) trade, and verified this against
real `bash` and a synthetic narrowing injected into `_removal_regions` (in a throwaway `/tmp` copy,
deleted after use) — `test_neutralization_equivalence_grid` (D1a) and
`test_removal_regions_matches_a_frozen_independent_reimplementation` (D1e) both went red on the
injected mutation, while `test_strip_and_neutralize_agree_on_command_line_tokens` (D1) and
`test_strip_bodies_matches_a_frozen_independent_reimplementation` (D1c) stayed green — the exact
asymmetry the module's own docs claim, evidence the discrimination claim is not decorative. I1/I2/I3
were each re-verified fixed against real bash and the shipped code; all five named must-still-DENY
cases plus the composed double-application case (`tee <<'A' <<<xxx <canon>/f`) were re-verified
through real `bash -c` and the shipped `decide()` → `command_write_targets` path, all still DENIED.
Full suite: 4187 passed, 3 skipped, 6 failed — all 6 pre-existing in `test_file_difficulty.py`
(`ImportError` on `difficulty_channel.port.StreamUnsupported`), confirmed unrelated: this branch's
diff touches none of `test_file_difficulty.py`, `scripts/difficulty_channel/port.py`, or the
internal-tracker adapter module the failing import chain runs through. `python3
scripts/verify-all.py` → 20/20. No blocking or should-fix finding survived
adversarial testing; two nits:

1. **nit — `scripts/lib/shell_tokens.py`'s `strip_heredoc_bodies` (maintainability)** — `strip_heredoc_bodies` now has no
   production caller (both consumers migrated to `neutralize_heredoc_constructs`); kept for its D1c
   frozen-reference role and as public API. **Disposition: no action.** This is deliberate retained
   API surface, already documented in this record's "Questions answered" § API split, not an
   oversight or dead code left behind by the migration.
2. **nit — `scripts/crutch_registry.toml` (reusability)** — this diff carries auto-generated registry
   churn (re-hashed ids for pre-existing entries, new synthetic-fixture entries for the new test file,
   entries for unrelated experience leaves re-scanned). **Disposition: no action.** This is the
   crutch-scanner's ordinary incidental output on any commit touching these files, not evidence of a
   stray `git add -A` or scope creep.
