---
name: a-probe-observes-only-the-domain-it-reads
description: A check, probe or reasoner observes only the inputs it actually reads; its verdict carries no information about the part of the claimed domain it did not read, however confidently that verdict is worded.
type: reference
schema: regularity/v1
generality: 3
domain: development
induced_from: [ask-user-question-split-turn, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-09-gate-must-execute-what-it-attests, 2026-09-27-negative-finding-amplified-from-bounded-probe, evocative-sample-label-without-provenance, tests-cover-resolved-path-not-raw-readers, pilot-sample-too-small-to-claim-the-sign, 2026-06-04-verify-load-bearing-axis, 2026-06-26-guard-coupled-doc-relocation, 2026-07-02-seam-stub-tests-never-exercise-default, 2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch, 2026-07-11-detect-compiled-claude-exe-via-ps-comm-not-pgrep-argv, 2026-07-14-claim-provenance-ledger-reasoning-deliverable, 2026-07-16-benchmark-measurement-contamination-env-resource-artifact, 2026-07-17-reviewer-claim-accepted-on-authority, 2026-07-20-git-index-file-env-leak-empty-commit-nested-worktree, 2026-07-21-cwd-keyed-hook-cross-repo-blind, 2026-07-29-crutch-registry-standing-prevention, 2026-08-05-deferring-ask-gate-and-the-binding-timeout-layer, 2026-08-05-scope-substitution-at-plan-authoring, 2026-08-11-sync-stash-identity-not-position, 2026-08-12-symlinked-write-path-defeats-path-based-guard, 2026-08-18-fail-open-gate-exit-zero-and-underfetched-listing, 2026-08-20-staged-vs-full-verification-and-generated-registry, 2026-08-22-acceptance-carveout-for-multiweek-measurement, 2026-08-24-required-flag-strands-stubbed-callers, 2026-09-03-hygiene-sweep-ownership-check-argument-order-bug, 2026-09-29-tracker-publish-gate-missed-per-stage-artifacts]
created: 2026-10-27
last_verified: 2026-10-27
---
# A check, probe or reasoner observes only the inputs it actually reads

## Regularity

A check, a probe or a reasoner observes only the inputs it actually reads: a test that exercises a resolved path says nothing about raw readers, a full-tree run says nothing about the files it did not see, a probe for processes says nothing about kernel rules, a sample says nothing about its sign beyond its size. The verdict it produces is worded about the domain it was asked about, while its evidence covers the domain it read, and the two coincide only when someone made them coincide; nothing in the verdict's wording reveals the gap.

## Generality

Level 3 — ranges over any judgement issued about a domain — a test suite, a gate, a monitor, a research finding, a reviewer's claim — whose evidence is gathered by reading part of that domain. It is not claimed for a judgement whose input provably is the whole domain (a check over a closed, enumerated set it reads in full).

## Refutation

A class of judges whose verdicts stayed correct on the unread part of the claimed domain across independent occurrences — the unread part never contradicting the verdict — would show that for that class the read part is a sufficient proxy and the gap carries no risk.

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
