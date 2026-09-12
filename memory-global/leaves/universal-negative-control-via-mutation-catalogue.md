---
name: universal-negative-control-via-mutation-catalogue
description: A universally-quantified control claim ("no narrowing passes", "nothing widens the gate") is discharged by a committed mutation catalogue over the subject, not by enumerating more input axes
type: feedback
created: 2026-09-04
last_verified: 2026-09-04
---

# Discharge a universal-negative control claim with a mutation catalogue, not more inputs

When a test suite has to establish a **universal negative** about a piece of code — "no narrowing of this producer passes the suite", "nothing widens this fail-closed gate" — enumerating more *input* axes from recall (more command shapes, more operator forms, more corpora) cannot discharge it in principle. The claim is about every possible *mutation of the subject*, and a defect that survives round N simply picks the input axis that round N did not enumerate. Recall is unbounded; the property under test is not.

**Why:** on GitHub [#108](https://github.com/sthe0/claude-agent-instructions/issues/108) (`heredoc-body-spans`, the `shell_tokens.py` producer/consumer split), two consecutive `thinker` review rounds each found a distinct narrowing of `_removal_regions` that the then-current 234-cell input grid did not see — round 1 needed a new grid axis (operator form + construct count), round 2 (conditioning the walk's early exit on the widened consumer path) proved the *widened* consumer path had no extent-sensitive, reference-backed control at all, only boolean acted-floors insensitive to region count. Both rounds were answered the same way: enlarge the input corpus. Both times the corpus enlargement missed the actual gap, because the gap was never an input the grid lacked — it was a *mutation of the producer* that no control was watching for. The instrument that would have caught it (mutate `_removal_regions`, assert a named control goes RED) existed only as three throwaway `/tmp` scripts (`inject_narrowing.py`, `inject_narrowing_c.py`, `localize_c.py`) invoked once per review round by a human, then discarded.

**How to apply:** when a plan's done criterion or a control-suite's claim is phrased as a universal negative over some code (no narrowing/widening/regression/bypass survives), do not accept "the grid/corpus covers these cases" as the done criterion. Require instead: **a committed, in-suite mutation catalogue** — a small module enumerating the known-dangerous mutations of the subject (each one a change that would violate the claim if undetected) and asserting, for each, that at least one *named* control goes RED. New mutations are added to the catalogue as they're discovered (by review, by incident); the catalogue only grows. This is the existential twin of the planner's rule for universally-quantified *done criteria* ("all X", "no Y remains" needs mechanical enumeration of the domain plus a negative end-state check, [[plan-control-criterion-hygiene]]) applied to *control claims* instead: universal claims need mechanical enumeration of the mutation space, not the input space.

A corollary, from the same task: **an ad-hoc mutation/injection script written during a review round is a committed artifact by default, not scratch** ([[committed-files-earn-their-place]]) — if a script was worth running once to measure a control gap, it is worth the next reviewer's time, and leaving it in `/tmp` guarantees the next round reinvents it (or worse, doesn't, and the same class of gap reopens unmeasured).

## See also

- [[committed-files-earn-their-place]] — the general committed-vs-scratch keep-criterion this corollary specializes.
- [[plan-control-criterion-hygiene]] — the universally-quantified-done-criterion twin on the planning side.
