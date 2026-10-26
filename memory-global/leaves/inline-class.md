---
name: inline-class
description: What makes a change inline (the "small change" class) - absence of choice and risk, one cohesive change, a bound on hand-written production lines - which paths are counted, how to measure, and what the check does not cover.
type: reference
schema: leaf/v1
created: 2026-10-26
last_verified: 2026-10-26
---

# The inline ("small change") class

## Difficulty

The class exists so a change with no choice and no risk is made in-thread, without a plan gate. It used to be defined by proxies (a file count, a raw changed-line count) that generated files and tests dominate: a one-line fix across two files could not be inline, while a design change hidden in one file could. To keep the in-thread path for exactly the changes that need no plan, the class is defined by its functional ground (no choice, no risk), with a size backstop measured over the lines a reviewer weighs.

## Guidance

The definition lives once, in `scripts/agentctl/inline_class.py` (`INLINE_DEFINITION`, `CRITERIA`); `agentctl classify`, the mandate cycle's triage prompt and its post-developer size check all read it from there:

```
A change is inline (a small change) only when there is no choice and no risk in it: one obvious solution, with no design decision; the change is reversible; it has no external or irreversible effect, no public-contract change and no new dependency; it is one cohesive change (a module and its tests); and it is at most small-change-max-lines hand-written production lines (tests, generated files and memory leaves are not counted).
```

**What is counted.** Added plus deleted lines of files that pass `is_production_file` (the extension set of the engine's production-edit gate; the count itself is not that gate's domain). Excluded, each by its own mechanism:

- tests, by path pattern (`scripts/tests/**`, `test_*.py`, ...);
- generated files, by the `linguist-generated` attribute declared in `.gitattributes` (not guessed from a name);
- every leaf under `memory-global/leaves/`, by the user's decision. A rule-leaf rewrite is therefore kept out of inline only by the choice-and-risk criteria, never by its size.

A rename counts if either side counts. A counted binary file always makes the change over the bound.

**How to measure.** `python3 scripts/inline-measure.py BASE TIP` prints the number. It reads the generated-file attributes from the working tree of `--repo` (default: the repo the script lives in), so an uncommitted attribute change there changes the count. The mandate cycle instead reads them from `origin/main` (`git check-attr --source`, git 2.40 or newer), so an item cannot exempt its own lines.

**The bound.** `small-change-max-lines` in `config.md`; it is derived from past accepted changes, see `docs/operations/inline-size-calibration.json` (formula in `docs/operations/config-rationale.md`).

**In the mandate cycle.** A fixed item whose measured change is over the bound, or contains a counted binary file, is declined as `needs-plan`: no PR is opened, the diff is kept as `change.patch`, the issue is commented and labelled so it is not retried, and the digest lists it under "Needs a plan". See `docs/operations/background-mandate.md`.

## Not covered

The check is a backstop on size, not a proof of the other criteria:

- cohesion, design decisions, public contracts and dependencies are not checked mechanically; they are the coordinator's judgement (`agentctl classify --not-cohesive` records a negative one);
- the cycle's rework rounds and failure analysis are out of scope here and a separate issue;
- `agentctl classify` takes the coordinator's estimate of the line count; only the cycle and `inline-measure.py` measure a real diff.

## See also

- `scripts/agentctl/inline_class.py`, `scripts/inline-measure.py`, `scripts/inline-size-calibrate.py`
- [acting-without-asking.md](acting-without-asking.md) § In-context carve-out
- [principle: coordinator-executes-through-specialists](principles/coordinator-executes-through-specialists.md)
