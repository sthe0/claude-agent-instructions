# The crutch registry

> Standing prevention for two recurring anti-patterns — a **regex-crutch** (a hard block or hard behaviour decided by matching free-text MEANING) and a **prose-crutch** (a rule that is decidable from observable inputs but left as instruction prose instead of mechanized). Enumerate mechanically, classify by judgement, and re-check on every session so a new instance cannot land silently.

## Why this exists

[The regex-crutch principle](../../memory-global/leaves/regex-not-for-semantic-classification.md) was named once, fixed at three sites (a fourth was already judge-backed), and recurred anyway: the fix that landed first for one difficulty was a semantic regex, and only a later pass replaced it with a schema-level check. Naming a crutch does not prevent it from coming back — only a standing, machine-run check does. This page documents that check.

Nothing is stored: the standing check computes from the live tree on every run. The mechanism has three pieces, each doing exactly one job:

| Piece | Role |
|---|---|
| [scripts/crutch-inventory.py](../../scripts/crutch-inventory.py) | **Enumerates.** Walks the repo and lists every candidate site in both domains. Classifies nothing; `--check` fails only when two sites share a stable id. |
| [scripts/verify-semantic-gates.py](../../scripts/verify-semantic-gates.py) | **Checks on every run.** Enumerates the code sites live and fails, naming each, on a hard-sink site (deny / block / exit 2) with no `judge_*` call in its scope or file whose id is not in the shared allowlist [scripts/lib/unguarded_sink_allowlist.py](../../scripts/lib/unguarded_sink_allowlist.py) (also pinned by `scripts/tests/test_no_semantic_unguarded.py`). Wired into `verify-all.py`; its advisory twin, `scan_crutch_regressions` in `scripts/self-diagnose.py`, flags `defer` overrides past the 90-day horizon and a rising unguarded count. |
| [scripts/gen_crutch_registry.py](../../scripts/gen_crutch_registry.py) | **Local classification report (optional).** Assigns each enumerated site a class, disposition and ground via named partitions and individually-grounded overrides, and writes the registry file crutch_registry.toml next to itself. That file is generated, git-ignored and read by no check; delete or regenerate it freely. Its `CLAUDE_MD_OVERRIDES` table is the live source of the `defer` dispositions and their `deferred_since` dates. |

## The two domains

**Domain A — code sites.** `crutch-inventory.py` AST-walks `scripts/**/*.py` except the repo's own `scripts/tests/` (test code guards no production behaviour) and records every scope that builds or reuses a regex, together with the highest-priority hard outcome it reaches (a PreToolUse deny, a Stop block, `sys.exit(2)`, or a hard *behaviour* — a routed/dispatched/suppressed/recorded decision, the deliberate widening past the three enforcement contracts the original hand audit used) and whether a `judge_*` call guards the same scope.

**Domain B — prose sites.** The same script extracts every normative statement (a sentence carrying an obligation modal) from `CLAUDE.md`, `skills/**/SKILL.md`, `skills/**/policy.md`, and `memory-global/leaves/**`, by heading structure — never by topic, since filtering by topic would itself be a regex deciding meaning.

Every enumerated site, in either domain, gets a stable content-derived id so an allowlist or override entry survives unrelated line drift.

## The classes

| Class | Domain | Meaning |
|---|---|---|
| `structural` | code | The regex reads tool-invocation shape, command syntax, or a file path — decidable from structure, not meaning. Legitimate, no judge needed. |
| `semantic-guarded` | code | The regex reads free-text meaning and drives a hard outcome, but a fail-open `judge_*` call guards the same path — the correct shape (mirrors `agentctl/advisor.py::judge_binary_ask`). |
| `semantic-unguarded` | code | Same as above with **no** judge guard — the anti-pattern. `verify-semantic-gates.py` fails on any such hard-sink site that is not individually allowlisted with a why-safe ground. |
| `not-a-gate` | code | A regex whose match feeds nothing hard (a log line, a test fixture). |
| `perception` | prose | A genuine judgement call, correctly left as prose. |
| `decidable` | prose | The rule is decidable from observable inputs and either has a structural home already, or is `defer`red with a named reason until one exists. |
| `already-mechanized` | prose | The statement documents an existing mechanism rather than substituting for one. |
| `not-normative` | prose | The enumerator's expected false-positive class (a modal keyword in non-obligation prose). |

Every entry also carries a **disposition** — `keep` (the class is correct and final) or `defer` (a `decidable` site not yet mechanized, with a mandatory non-empty reason and a `deferred_since` date).

## Adding a new site

A new hard-sink code site needs no registration: `python3 scripts/verify-semantic-gates.py` either passes (a `judge_*` guard is present) or names it. Fix it with a fail-open judge guard, or — for a genuine false pairing, where the regex reads unrelated structural syntax — add its id to `KNOWN_UNGUARDED_HARD_SINKS` in `scripts/lib/unguarded_sink_allowlist.py` with a ground that names *why* it is safe.

A prose statement with a distinct fate (a `defer`, say) gets an entry in `gen_crutch_registry.py`'s `CLAUDE_MD_OVERRIDES` (or `CODE_ID_OVERRIDES` for a code site) with a ground that names *why*, not just *what*. To see the full classification locally, run `python3 scripts/gen_crutch_registry.py`; it writes the git-ignored registry file crutch_registry.toml under scripts/ (through `scripts/lib/worktree_route.py` when run from the canonical checkout) and lists only git-tracked files, so `git add` a new script before regenerating.

## Justifying a `keep` versus reviewing a `defer`

A `keep` ground should name the concrete thing that makes the class correct — for `structural`, what shape the regex actually reads (a stage marker, a command prefix, a mount path); for `semantic-guarded`, which judge call guards it and where. A ground that only restates the class name (e.g. "this is structural") is a rubber stamp, not a justification, and defeats the registry's purpose.

A `defer` is reviewed by reading its `reason` and `deferred_since`: is the blocking condition (usually "its structural home doesn't exist yet") still true? `self-diagnose.py`'s `scan_crutch_regressions` flags any `defer` override in `CLAUDE_MD_OVERRIDES` older than 90 days as advisory-only worklist, keyed to the entry's own `deferred_since`, not any file's mtime (which any unrelated change would reset).

## The current deferral list

Three `decidable` prose rules are deferred rather than mechanized. Every `defer` override in `CLAUDE_MD_OVERRIDES` is published here verbatim with its own reason and date, so a deferral is a visible, dated commitment rather than a silent omission — the reviewer cross-checks this list against that table:

- **`5355bf72d79adb38`** — `CLAUDE.md`, `deferred_since` 2026-07-29:
  > Deferred rather than remediated AT THIS STAGE (stage 3) because that structural home is a STAGE-4 output — a rule cannot point at a mechanism that does not exist yet. Once stage 4 lands verify-semantic-gates.py, this prose becomes a pointer to that check; the deferral is published in stage 5's deferral list, not silently dropped.
- **`e523ebc3136de535`** — `CLAUDE.md`, `deferred_since` 2026-07-29:
  > Deferred for the same ordering reason as the adjacent sentence (id 5355bf72d79adb38): its structural home is also scripts/verify-semantic-gates.py, not yet landed at this point in the plan (stage 3, ahead of stage 4). Mechanized at stage 4, published in stage 5's deferral list.
- **`031125a68fc8a99c`** — `CLAUDE.md`, `deferred_since` 2026-07-29:
  > Not built: no such counter exists today, and the event (an unknown tool needing more than one lookup) is rare enough that the cost of building a counter was judged, this pass, not worth it relative to the size of the win — deferred rather than remediated.

**Converted versus deferred.** The regex-crutch sites that carry a same-scope or same-file fail-open judge are in the safe shape; `verify-semantic-gates.py` reports the live count of semantic-unguarded hard sinks (0 on a clean tree) and the number of enumerated sites on every run, so no figure is frozen here. **3 prose rules remain `defer`red** (the list above).

## Honest limits

State plainly what this mechanism does and does not cover — the point of a standing check is to be trusted no further than it earns:

- **Dynamic pattern construction is invisible.** `getattr`, `exec`, an aliased `import re as rx`, or a pattern assembled at runtime are not visible to the AST walk.
- **The prose guard routes, it does not decide.** Domain B's enumerator surfaces a new normative statement and requires it to be dispositioned; it never judges *whether* a statement is genuine perception or a decidable rule — that stays a model pass over the enumerator's output, same as the original classification.
- **A crutch expressed without a regex, or a prose obligation phrased without a modal keyword, is invisible to the enumerator.** A hand-written character-by-character string scan standing in for a regex, or an imperative sentence with no modal ("Ask when: ..."), is not detected. This is a recall limit on the prefilter, not something the classification pass downstream can recover.
- **`verify-semantic-gates.py` corroborates a guard at file-rollup granularity**, following imports one hop deep. It cannot say which of a file's several regexes feeds which of its several sinks when both are plural, and it cannot see a single guardian in a multi-guardian file lose its own judge while a sibling guardian's judge keeps the file-level flag `True`. The allowlist id is file + scope name, so a meaning-regex added inside an already-allowlisted scope is not seen.
- **The prose partition assignment is coarse for the two large bulk partitions** (ordinary leaves, `SKILL.md`/`policy.md`) — one verdict per several dozen to several hundred statements, refined only by whether a statement's own text names a concrete enforcing artifact. `CLAUDE.md` is the one exception, read and dispositioned statement-by-statement because it is the highest-priority governance file and small enough to review in full.
- **Neither domain traces cross-file semantic equivalence** — two statements or two regexes expressing the same rule in different words are two separate candidates.
- **The registry sees a lexical match only where it feeds a hard outcome.** A regex, `difflib` ratio or word-overlap helper that decides a question of meaning behind a nudge, a lint, a counter or a join is outside its sinks. `scripts/lexical-meaning-inventory.py --check` enumerates those call sites over every git-tracked non-test Python file and holds one classified verdict per site in `docs/operations/lexical-meaning-inventory.toml`; its own blind spot (keyword lists, substring tests, set overlap, shell scripts, dynamic patterns) is stated in its `--help` and in [the leaf](../../memory-global/leaves/regex-not-for-semantic-classification.md).

## Why this shape and not a heavier one

Rejected as heavier without proportional gain: a pre-commit hook (duplicates `verify-all.py`, adds a second install surface), a CI service (this repo's checks are local by design), and a new plugin/registry framework (a framework built to prevent crutches would itself cost more, loaded, than the crutches it prevents). What shipped is the lightest form that actually prevents: one script on the existing `verify-all.py` runner computing from the live tree, one shared allowlist module, and one advisory arm in the existing `self-diagnose.py` scan.

## See also

- [regex-not-for-semantic-classification.md](../../memory-global/leaves/regex-not-for-semantic-classification.md) — the design principle this mechanism enforces, and the history of why naming it once was not enough.
- [Verification guards](guards.md) — where `verify-semantic-gates.py` sits among the rest of the `verify-all.py` suite.
- `scripts/gen_crutch_registry.py`'s header — the partition table and override tables behind the optional local classification report.
