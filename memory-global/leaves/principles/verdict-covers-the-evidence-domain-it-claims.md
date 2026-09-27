---
name: verdict-covers-the-evidence-domain-it-claims
description: Anything that issues a verdict — a gate, a checker, or an agent's own investigation — must actually cover the evidence domain its verdict claims, and where it cannot, say so instead of answering. A gate demanding proof whose prover is absent, a checker enumerating from one source while reading another, and and an agent's claim about a mechanism it never ran — its absence, its cause, or its next move — are the same fault; where the mechanism ships a dry run, not spending it before the claim is the whole failure. Escape hatches must stay reachable, diagnosed, typed and counted.
type: reference
schema: principle/v1
generality: 3
domain: development
induced_from: [ask-user-question-split-turn, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-09-gate-must-execute-what-it-attests, 2026-07-04-spawn-budget-death-forensics-before-respawn, 2026-09-27-negative-finding-amplified-from-bounded-probe]
created: 2026-08-04
last_verified: 2026-09-27
---

# A verdict covers the evidence domain it claims

## Principle

To make a verdict mean what it says, **make it cover the evidence domain the verdict claims — and
where it cannot, say so instead of answering.** Anything that judges has three possible honest
answers, not two: *satisfied*, *not satisfied*, and *I could not look*. Collapsing the third into
either of the first two is the fault; which of the two it collapses into decides who it hurts.

This holds whether the judge is a piece of machinery or a reasoner. The three forms met so far:

**The gate form — the absent prover.** A fail-closed gate demands proof produced by something else.
When that producer is not installed in the root the session actually loads from, the gate demands
proof that cannot exist and blames whoever hit it. So an engine gate backed by an external
satisfier must:

1. keep an escape **reachable** — without one, a dead prover bricks the spine, and a gate that
   cannot be satisfied trains its users to route around gates in general;
2. **diagnose** before refusing — distinguish a missing PROOF from an absent PROVER, mechanically
   wherever the engine can observe the difference, and say which one it found;
3. record a **typed** reason for each escape, not free text alone — a note explains one escape, a
   token counts all of them;
4. be **counted**, with a standing report — an escape nobody counts stops being the exception, and
   the change is invisible precisely because it is gradual.

**The checker form — the unread domain.** A checker that enumerates its domain from one source (the
git index) while reading content from another (the working tree) cannot see a new artifact at all,
so it answers OK *for the wrong reason*: not "I looked and it was fine" but "I did not look." A
green that means *not looked at* is worse than a red, because a red gets investigated. The fix
belongs in the producing step — stage the artifact before you verify — not in the checker, which is
behaving exactly as designed.

**The investigator form — the unprobed mechanism.** An agent's own conclusion about the world is a
verdict over an evidence domain, and it inherits the same three answers. **The claim need not be a
negative.** *"X is not used"*, *"the denial fired because of Y"* and *"this command will reclaim
270 MB"* are one act — a statement about a mechanism's behaviour issued without running the
mechanism — and each is only as strong as the probes behind it. A probe selected from a guess about
the mechanism cannot rule out a mechanism it was never shaped to see: a name-filtered process grep
says nothing about a kernel NAT rule, and a reading of what a command *means* says nothing about
what it *does*. The honest answer there is *I could
not look*, and collapsing it into *it is not there* is the same fault the machinery forms commit —
except that here nothing external holds the third answer open, so only the reasoner's own discipline
does. Three consequences follow, and they are the ones a reasoner actually gets wrong:

1. **Name the mechanism before asserting anything about it — its absence, its cause, or its next
   move.** Enumerate what would explain the observed behaviour, then check that the probes run
   actually cover those candidates. An absence established over an unenumerated domain is not
   established, and neither is a cause nor a prediction. **Where the mechanism ships its own dry run,
   the barrier is zero and there is no excuse:** `-s` / `--dry-run` / `--simulate`, reading the script
   that is about to execute, re-attempting a denied call once its target is legible. Each is
   side-effect-free and therefore *already* pre-authorized (`CLAUDE.md` § Acting without asking,
   carve-out 1), so asking the mechanism costs a second and guessing at it costs a wrong artifact.
   Reaching for the dry run only after the claim has failed is the characteristic shape of this
   consequence being got wrong.
2. **A positive observation outranks a bounded negative.** A byte counter, a timestamp, live traffic,
   a user's "I use this daily" are observations; a probe that failed to find something is a statement
   about the probe. When they conflict, the negative is wrong until its coverage is shown — and
   explaining the positive away ("residue of an abandoned experiment") to preserve the negative is the
   characteristic move of this failure, not a resolution of it.
3. **Never let a bounded negative license an irreversible act.** Shut it down, delete it, it's dead —
   these need a positively established `ABSENT`, never the absence of a finding.

Delegation makes this form strictly worse, because the parent cannot audit coverage it never sees:
[[delegatable-work-patterns]] § Negative findings carries the return-contract rule.

**The corollary, which is where this is most often got wrong:** a checker can be
index-ENUMERATED yet manifest-SATISFIED. Making an artifact **visible** to a checker and
**satisfying** that checker are two different acts. Surfacing an obligation never discharges it, and
a plan that treats them as one step produces a stage that is green precisely because nobody has
looked yet.

**Naming corollary — forward-only.** The gate form's "I could not look" and the checker form's "I did
not look" both rest on being able to tell a mechanism-collected observation apart from a hand-typed
one. An artifact's filename is part of that evidence: a probe that writes `finding.md` or
`state.json` produces a file indistinguishable, by name alone, from one a human wrote by hand from
memory — the reader has to open it and reconstruct provenance from prose, if the prose even says.
**An artifact filename should name its producer** (the script, probe or command that wrote it) so a
reader can tell "this was collected" from "this was asserted" without opening the file. Two scope
limits belong to the norm rather than being exceptions to it. It governs **mechanism-collected**
artifacts only: a document a human authored as an analysis is not a capture, and naming it after a
producer would assert exactly the false provenance this corollary removes —
`connection-closed-finding.md`, hand-written in the same task that induced the corollary, is that
case. And a **mixed** artifact must either be split or renamed, never named after its capture half:
`record-experience-search-v3.txt` from the same task names its producer honestly for its second half
(the raw `record-experience.py search` output) while its first half is the author's placement
decisions — so the name promises "collected" over a body that is half "asserted", which is the exact
indistinguishability the corollary removes. A name may claim a pure capture only for a pure capture.
And it norms **forward only** — `old-wiring.json`, `old-wiring-v2.json` and `reload-stamp` keep their
existing names, because a norm that requires touching every prior artifact to stay compliant
confuses the difficulty it removes (an unlabeled NEW artifact reads as trustworthy by default) with
an unrelated one (a naming scheme applied retroactively to a stable, already-consumed corpus).

## Generality

Level 3 — a cross-domain invariant over **anything that issues a verdict over evidence it does not
itself produce**, mechanism or reasoner. It ranges over engine gates whose satisfier is an
out-of-process hook, repo verifiers that enumerate one source and read another, CI checks reporting
on a subset they never state, an agent concluding a host is unused from probes that could not have
found its mechanism, and a sub-agent returning a bare negative whose coverage the parent cannot see.

It stood at level 2 from 2026-08-04 to 2026-09-27, restricted to *judging mechanisms*, and named its
own promotion condition: an instance outside that class. [[2026-09-27-negative-finding-amplified-from-bounded-probe]]
is that instance — the judge was an agent's own investigative reasoning, the collapse of *I could not
look* into *it is not there* was identical, and the repo's three existing statements of the rule
(`hook_wiring`'s three-valued probe, [[verify-ownership-before-shared-state-delete]], this leaf) all
failed to reach it precisely because each was scoped to machinery. The promotion is therefore not a
widening on a hunch but the discharge of a condition this leaf wrote for itself.

What would still be outside it: a mechanism that issues no verdict at all. The claim is not "every
mechanism reports its own coverage" — a transformer, a renderer, a transport makes no judgement and
owes no coverage statement. The invariant binds at the moment something asserts a state of the world
on evidence, and not before.

## Induced from

- [[ask-user-question-split-turn]] — the plan-presentation delivery gate and its residuals. The
  unreachable-prover case is one of those residuals made concrete: `approve` refused for "no
  delivery proof recorded" on a machine where the hook that writes the proof was not registered in
  the root the session loads from, and nothing in the refusal said so.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the verifier form, met
  first as a green stage followed by a red pre-commit: a checker enumerating from the git index over
  a working tree that had moved on.
- [[2026-07-09-gate-must-execute-what-it-attests]] — the experience leaf this task extended. Its
  (b) REFUSE-WITHOUT-A-ROUTE limb is the same fault one level in: a refusal with no reachable
  resolution path. The 2026-08-04 context is the absent-PROVER twin — the route exists, but nothing
  had built the road it points at.
- [[2026-07-04-spawn-budget-death-forensics-before-respawn]] — § 2026-08-10 (the judge-abort verdict)
  is what induced the naming corollary. That stage produced two artifacts side by side in one diff —
  one named after the probe that captured it, one named after its subject — and nothing but the
  author's own memory of who wrote which distinguished "collected" from "asserted". The verdict
  recorded there (the ledger cannot see an abort it was never wired to observe) is the same fault on
  the evidence-domain axis that this principle states.
- [[2026-09-27-negative-finding-amplified-from-bounded-probe]] — the investigator form, and the
  instance outside the judging-mechanism class that promoted this leaf from level 2 to level 3. A peer
  VM was declared a "forgotten rudiment, can be shut down" on the strength of a name-filtered process
  grep and a policy-routing check, neither of which could have seen the `iptables` NAT table the whole
  mechanism lived in; a positive counter-signal (2.7GB of tunnel egress) was explained away to keep the
  conclusion. The host was carrying the user's daily VPN traffic, and only the user's contradiction
  caught it. Its two further contexts, recorded the same day, are what widened consequence 1 beyond
  absence: a permission denial whose CAUSE was named twice and probed neither (the real cause was
  opacity, and one `cat` of the script refuted both guesses), and a package manager's future
  BEHAVIOUR promised to the user while `apt-get -s` sat unused — the dry run was reached for only
  after the promise had failed.

## Refutation

The principle is refuted, or driven to a broader form, by a judging mechanism that provably cannot
distinguish "not satisfied" from "could not look" — where the coverage question is undecidable to
the mechanism itself rather than merely unimplemented. At that point the requirement is not
"diagnose" but "declare the undecidability at the boundary", and the statement must widen to admit
a mechanism whose honest output is a permanent third state rather than a diagnosis.

The naming half is refuted by a corpus where a filename cannot carry provenance without lying: a
producer that is itself a chain (a script invoked by a hook invoked by the engine) has no single name
to give, and forcing one asserts a provenance narrower than the truth. Should that shape prove to be
the common case rather than the exception, the requirement moves off the filename and into a
machine-readable provenance field inside the artifact, and the corollary narrows to "an artifact
declares its producer" — with the filename as merely one place to declare it.

The investigator half — and with it the level-3 claim — is refuted by an investigative domain where
enumerating candidate mechanisms before accepting an absence is not merely expensive but impossible:
an open-world question whose mechanism space cannot be bounded even in principle, so that *I could
not look* is the only answer ever available and the rule degenerates to "never conclude anything
negative". Should that be the common shape rather than the exception, the statement must narrow back
to closed-world domains — where the set of mechanisms that could produce an observation is
enumerable — and level 3 is not earned.

The narrower escape-hatch half carries its own refutation: if escapes turn out to be genuinely rare,
the typed reason is overhead. The measurement is what refutes that — 3 of the first 5 delivery
stamps on this machine were overrides — and `scripts/escape-hatch-report.py` is what will show
whether the ratio falls once the gate names the real cause.

## See also

- [[result-checked-against-its-result-image]] — the same discipline one level up: a result is not
  accepted until compared to its declared image. This principle is about the comparison's *evidence*
  being real.
- [[regex-not-for-semantic-classification]] — a related determinization fault: a mechanism deciding
  something it cannot actually observe (meaning) and issuing a hard verdict anyway.
- `scripts/agentctl/README.md` § Two config roots — the engine-author-facing form of the gate half.
- `scripts/lib/hook_wiring.py` — the WIRED / ABSENT / UNKNOWN probe, where the third answer is
  first-class by construction.
- [[verify-ownership-before-shared-state-delete]] — the same rule stated for shared-state deletion
  ("no live process foothold" ≠ "no owner"; bias the classifier fail-safe to KEEP). One of the three
  scoped statements that existed before the level-3 promotion and did not reach the investigator form.
- [[delegatable-work-patterns]] § Negative findings — the return-contract rule that makes *I could not
  look* expressible across a delegation boundary, where the parent cannot otherwise see coverage.
