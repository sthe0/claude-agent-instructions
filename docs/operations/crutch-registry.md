# The crutch registry

> Standing prevention for two recurring anti-patterns — a **regex-crutch** (a hard block or hard behaviour decided by matching free-text MEANING) and a **prose-crutch** (a rule that is decidable from observable inputs but left as instruction prose instead of mechanized). Enumerate mechanically, classify by judgement, and re-check on every session so a new instance cannot land silently.

## Why this exists

[The regex-crutch principle](../../memory-global/leaves/regex-not-for-semantic-classification.md) was named once, fixed at three sites (a fourth was already judge-backed), and recurred anyway: the fix that landed first for one difficulty was a semantic regex, and only a later pass replaced it with a schema-level check. Naming a crutch does not prevent it from coming back — only a standing, machine-run check does. This page documents that check.

The mechanism has four pieces, each doing exactly one job:

| Piece | Role |
|---|---|
| [scripts/crutch-inventory.py](../../scripts/crutch-inventory.py) | **Enumerates.** Walks the repo and lists every candidate site in both domains. Classifies nothing. |
| [scripts/crutch_registry.toml](../../scripts/crutch_registry.toml) | **Records the classification.** One entry per enumerated site: its class, its disposition, and a one-line ground. Generated data, not hand-edited. |
| [scripts/gen_crutch_registry.py](../../scripts/gen_crutch_registry.py) | **Produces the registry.** Reads the enumerator's live output and assigns each site to a named, auditable partition (plus a short list of individually-grounded overrides), so re-running it over an unchanged tree reproduces a byte-identical file. |
| [scripts/verify-semantic-gates.py](../../scripts/verify-semantic-gates.py) | **Re-checks on every run.** Diffs a fresh enumeration against the registry and fails on drift. Wired into `verify-all.py`; its advisory twin, `scan_crutch_regressions` in `scripts/self-diagnose.py`, flags stale deferrals at session start. |

## The two domains

**Domain A — code sites.** `crutch-inventory.py` AST-walks `scripts/**/*.py` and records every scope that builds or reuses a regex, together with the highest-priority hard outcome it reaches (a PreToolUse deny, a Stop block, `sys.exit(2)`, or a hard *behaviour* — a routed/dispatched/suppressed/recorded decision, the deliberate widening past the three enforcement contracts the original hand audit used) and whether a `judge_*` call guards the same scope.

**Domain B — prose sites.** The same script extracts every normative statement (a sentence carrying an obligation modal) from `CLAUDE.md`, `skills/**/SKILL.md`, `skills/**/policy.md`, and `memory-global/leaves/**`, by heading structure — never by topic, since filtering by topic would itself be a regex deciding meaning.

Every enumerated site, in either domain, gets a stable content-derived id so a registry entry survives unrelated line drift.

## The classes

| Class | Domain | Meaning |
|---|---|---|
| `structural` | code | The regex reads tool-invocation shape, command syntax, or a file path — decidable from structure, not meaning. Legitimate, no judge needed. |
| `semantic-guarded` | code | The regex reads free-text meaning and drives a hard outcome, but a fail-open `judge_*` call guards the same path — the correct shape (mirrors `agentctl/advisor.py::judge_binary_ask`). |
| `semantic-unguarded` | code | Same as above with **no** judge guard — the anti-pattern. `verify-semantic-gates.py` fails if any site is ever classified this way. |
| `not-a-gate` | code | A regex whose match feeds nothing hard (a log line, a test fixture). |
| `perception` | prose | A genuine judgement call, correctly left as prose. |
| `decidable` | prose | The rule is decidable from observable inputs and either has a structural home already, or is `defer`red with a named reason until one exists. |
| `already-mechanized` | prose | The statement documents an existing mechanism rather than substituting for one. |
| `not-normative` | prose | The enumerator's expected false-positive class (a modal keyword in non-obligation prose). |

Every entry also carries a **disposition** — `keep` (the class is correct and final) or `defer` (a `decidable` site not yet mechanized, with a mandatory non-empty reason and a `deferred_since` date).

## Adding a new site

Nothing is added by hand. Land the code or prose change, then:

```bash
python3 scripts/crutch-inventory.py --check   # will report the new site as unregistered
python3 scripts/gen_crutch_registry.py        # regenerate the registry to cover it
python3 scripts/crutch-inventory.py --check   # now clean
```

`gen_crutch_registry.py` assigns the new site a class from its partition table (file-path-keyed, see the registry's own header comment) unless it needs a per-id override — a true regex-feeding-a-hard-sink pairing, or a CLAUDE.md statement with a distinct fate. Add the override in `gen_crutch_registry.py` itself (`CODE_ID_OVERRIDES` / `CLAUDE_MD_OVERRIDES`) with a ground that names *why*, not just *what*; re-run the script so the registry stays reproducible data rather than a hand edit.

## Justifying a `keep` versus reviewing a `defer`

A `keep` ground should name the concrete thing that makes the class correct — for `structural`, what shape the regex actually reads (a stage marker, a command prefix, a mount path); for `semantic-guarded`, which judge call guards it and where. A ground that only restates the class name (e.g. "this is structural") is a rubber stamp, not a justification, and defeats the registry's purpose.

A `defer` is reviewed by reading its `reason` and `deferred_since`: is the blocking condition (usually "its structural home doesn't exist yet") still true? `self-diagnose.py`'s `scan_crutch_regressions` flags any `defer` entry older than 90 days as advisory-only worklist, keyed to the entry's own `deferred_since`, not the registry file's mtime (which any unrelated change would reset).

## The current deferral list

Three `decidable` prose rules are deferred rather than mechanized. Every `defer` entry in the registry is published here verbatim with its own reason and date, so a deferral is a visible, dated commitment rather than a silent omission — the reviewer cross-checks this list against `crutch-inventory.py --check` (which reports the registry's `defer` count and fails on any `defer` missing a reason):

- **`5355bf72d79adb38`** — `CLAUDE.md`, `deferred_since` 2026-07-29:
  > Deferred rather than remediated AT THIS STAGE (stage 3) because that structural home is a STAGE-4 output — a rule cannot point at a mechanism that does not exist yet. Once stage 4 lands verify-semantic-gates.py, this prose becomes a pointer to that check; the deferral is published in stage 5's deferral list, not silently dropped.
- **`e523ebc3136de535`** — `CLAUDE.md`, `deferred_since` 2026-07-29:
  > Deferred for the same ordering reason as the adjacent sentence (id 5355bf72d79adb38): its structural home is also scripts/verify-semantic-gates.py, not yet landed at this point in the plan (stage 3, ahead of stage 4). Mechanized at stage 4, published in stage 5's deferral list.
- **`031125a68fc8a99c`** — `CLAUDE.md`, `deferred_since` 2026-07-29:
  > Not built: no such counter exists today, and the event (an unknown tool needing more than one lookup) is rare enough that the cost of building a counter was judged, this pass, not worth it relative to the size of the win — deferred rather than remediated.

**Converted versus deferred, in numbers.** Of the code sites that could carry the regex-crutch, **7 are now `semantic-guarded`** (the regex demoted to a prefilter with a fail-open judge on the same path — converted to the safe shape) and **0 are `semantic-unguarded`**; **3 prose rules remain `defer`red** (the list above). The full enumeration is **2042 sites** — 752 code + 166 code-file-rollup + 1124 prose; by disposition, **2039 `keep` and 3 `defer`**. No partition was silently dropped: all 1124 prose sites are dispositioned, and `crutch-inventory.py --check` reports 0 missing / 0 stale / 0 undispositioned.

## Honest limits

State plainly what this mechanism does and does not cover — the point of a standing check is to be trusted no further than it earns:

- **Dynamic pattern construction is invisible.** `getattr`, `exec`, an aliased `import re as rx`, or a pattern assembled at runtime are not visible to the AST walk.
- **The prose guard routes, it does not decide.** Domain B's enumerator surfaces a new normative statement and requires it to be dispositioned; it never judges *whether* a statement is genuine perception or a decidable rule — that stays a model pass over the enumerator's output, same as the original classification.
- **A crutch expressed without a regex, or a prose obligation phrased without a modal keyword, is invisible to the enumerator.** A hand-written character-by-character string scan standing in for a regex, or an imperative sentence with no modal ("Ask when: ..."), is not detected. This is a recall limit on the prefilter, not something the classification pass downstream can recover.
- **`verify-semantic-gates.py`'s condition (c) — has a guard been silently reverted — works at file-rollup granularity**, following imports one hop deep. It cannot say which of a file's several regexes feeds which of its several sinks when both are plural, and it cannot see a single guardian in a multi-guardian file lose its own judge while a sibling guardian's judge keeps the file-level flag `True`.
- **The prose partition assignment is coarse for the two large bulk partitions** (ordinary leaves, `SKILL.md`/`policy.md`) — one verdict per several dozen to several hundred statements, refined only by whether a statement's own text names a concrete enforcing artifact. `CLAUDE.md` is the one exception, read and dispositioned statement-by-statement because it is the highest-priority governance file and small enough to review in full.
- **Neither domain traces cross-file semantic equivalence** — two statements or two regexes expressing the same rule in different words are two separate candidates.

## Why this shape and not a heavier one

Rejected as heavier without proportional gain: a pre-commit hook (duplicates `verify-all.py`, adds a second install surface), a CI service (this repo's checks are local by design), and a new plugin/registry framework (a framework built to prevent crutches would itself cost more, loaded, than the crutches it prevents). What shipped is the lightest form that actually prevents: one script on the existing `verify-all.py` runner, one checked-in registry file, and one advisory arm in the existing `self-diagnose.py` scan.

## See also

- [regex-not-for-semantic-classification.md](../../memory-global/leaves/regex-not-for-semantic-classification.md) — the design principle this mechanism enforces, and the history of why naming it once was not enough.
- [Verification guards](guards.md) — where `verify-semantic-gates.py` sits among the rest of the `verify-all.py` suite.
- `scripts/crutch_registry.toml`'s own header comment — the authoritative, current partition table and override counts.
