---
name: tests-accompany-code
description: Any change to code ships with tests that verify the change — the default for both the developer (who writes them) and the reviewer (who rejects a code diff lacking them); narrow named escape class for the genuinely non-testable
type: feedback
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:what-a-change-does-not-carry-ends-with-its-authors-context]
grounding_judged: db6a2547914422fa1859ffabc09eb2ffc5b794c2ebeabd806c0f6b33d3e51846
created: 2026-06-25
last_verified: 2026-06-25
---

## Difficulty

Nothing mechanically couples a code change to a test of that change — the discipline lives only in prose, so under load a change lands without coverage and the regression it introduces (or the bug it "fixes") is caught later, by a human, or not at all. The narrow framing "bugfixes need a regression test" is too small: the same gap exists for *every* code change, and singling out bugfixes invites arguing about whether a given change "is a bugfix" instead of just writing the test.

## Principle

To keep a code change's behavior verified after its author's context is gone, ship with every change that writes or modifies behavior tests that exercise the new or changed path in the same change (red before and green after for a fix); the reviewer treats a code diff with no corresponding test delta as a should-fix, blocking for behavioral changes, and checks the tests cover the changed paths; only the named non-testable class (pure rename or reformat, prose-only files, config with no logic, an unreachable trigger with a stated one-line waiver) ships without.

## Goal class

code changes verified by tests that travel with them

## Grounded in

- [[what-a-change-does-not-carry-ends-with-its-authors-context]]

## Guidance

**The rule is symmetric and applies to all code changes, not a bugfix subclass.**

- **Author (developer).** Any change that writes or modifies behavior ships, in the same change, with tests that exercise the new/changed path — for a fix, a test that is red before and green after. This is the default, not a judgment call gated on whether the change "feels" test-worthy.
- **Reviewer (code-reviewer).** A code diff with no corresponding test delta is a **should-fix** (escalating to **blocking** for behavioral changes) finding — the reviewer asks for the test rather than approving on the author's say-so, and checks the tests cover the changed/new paths (completeness, not just presence — a test added for an unrelated path does not satisfy this). Symmetry: the author writes, the reviewer checks; neither side waives silently.

**Named non-testable escape class** (the only changes that legitimately ship without a test, and they must be *named as such*, not assumed):

- Pure rename / move / mechanical reformat with no behavior change.
- Documentation, comments, or prose-only files.
- Config / build-manifest edits with no executable logic of their own.
- A behavioral fix whose trigger genuinely cannot be reached in a test harness — state *why* explicitly (a one-line waiver), don't omit silently.

**Mechanical backstop (advisory, not a substitute for the discipline).** A `commit-msg` hook (`verify-tests-accompany-code.py`, under the instructions-repo `scripts/`) *warns* (never blocks) when a commit stages a non-test `scripts/**.py` change with no test delta under the tests directory; a `[skip-test-guard: <reason>]` trailer in the commit message suppresses the warning for the escape class above. The hook is a nudge — the real gate is the reviewer.

## See also

- `~/.claude-agent/skills/specializations/developer/SKILL.md` § Tests and build (author side)
- `~/.claude-agent/skills/specializations/code-reviewer/SKILL.md` § What you review (reviewer side)
- [[plan-activity-ontology]] — review is the control criterion of a developer-actor stage
- regularity:what-a-change-does-not-carry-ends-with-its-authors-context — the regularity this rule rests on.
