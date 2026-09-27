---
name: 2026-09-27-negative-finding-amplified-from-bounded-probe
description: Concluded a peer VM was an unused rudiment after probing only for proxy PROCESSES on it and policy-routing on the other side; the actual mechanism (iptables DNAT on the VM) was never probed by me or the fork I delegated to, and a positive counter-signal (tunnel byte counters) was explained away rather than treated as decisive.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
tier: 1
refs: [verdict-covers-the-evidence-domain-it-claims, delegatable-work-patterns, coordinator-pitfalls, verify-right-axis-report-honestly]
created: 2026-09-27
last_verified: 2026-09-27
---

# A negative finding was amplified into a confident conclusion from a probe that could not have found the mechanism

## Difficulty
An investigative conclusion of the form 'X is not used / does not exist' was issued over an evidence domain that structurally could not contain X: the probes run were (a) a name-filtered grep for proxy processes on the peer host and (b) a policy-routing check on the near host, while the real mechanism was an iptables DNAT relay on the peer. The negative was then amplified into an actionable recommendation ('forgotten rudiment, can be shut down') that, if followed, would have broken the user's live VPN. Compounding: a positive counter-signal already in hand (2.7GB of tunnel egress) was rationalised as 'residue of an abandoned experiment' instead of being treated as decisive against the negative. Delegation amplified this: the fork returned a confident negative with no statement of what it had probed, and CLAUDE.md's cost discipline deliberately keeps the tool volume inside the subagent, so the parent had no way to audit the negative's coverage.

## Order & criterion
Answer which inbound on the0.fun the yandex-cloud tunnel traffic reaches, and whether the wg-tunnel / yandex VM are still in use.

**Acceptance check:** acceptance-review: the user recognises the described topology as the one they actually operate (they hold the working client links).

## Contexts

### 2026-09-27 — initial
- Where it arose: the0.fun VPS / VPN topology investigation, 2026-09-27
- Working plan: In-thread investigation: fork for reconnaissance on the peer VM, live probes over ssh, project-memory update.
- **Evidence domain actually covered:** `ps`/`ss`/`docker` on the peer VM filtered by proxy process
  names; `ip rule`/`ip route` on the near host. **Never covered:** `iptables -t nat -L` on the peer —
  the table the whole mechanism lives in. The conclusion "not used" ranged over a domain that could
  not have contained the answer either way, so the honest verdict was *I could not look*, not
  *it is not there*.
- **Counter-signal held and discarded:** `wg-tunnel` interface counters showed ~350MB rx / ~2.7GB tx.
  A live byte count is a **positive** observation; a name-filtered process grep is a **bounded
  negative**. I inverted their weight and wrote off the bytes as "residue of an abandoned
  experiment" — a story invented to preserve the conclusion.
- **Delegation amplification:** the fork's return was a bare negative ("no proxy process on the VM,
  no policy routing"), with no list of what it had probed. It also reported sudo unavailable on the
  VM when passwordless sudo was in fact available there — a second bounded negative, and the one
  that would have unlocked the `iptables` read. Neither error was auditable from the parent, because
  the subagent's tool volume is deliberately discarded (`CLAUDE.md` § Cost discipline).
- **What the truth turned out to be:** the peer VM is a pure iptables DNAT relay fronting the0.fun's
  Xray inbounds over `wg-tunnel`, with deliberately crossed ports, as an RKN-evasion front. It
  carries the user's daily VPN traffic (13GB via `wg-tunnel→*`, 320GB via the `eth0→eth0` hairpin).
- **Cost of the error had it been acted on:** I recommended the VM could be shut down. That would
  have taken the user's VPN down. Caught only because the user contradicted me from their own
  operational knowledge — *not* by any check of mine.
- **Resolution:** re-probed the peer VM's NAT table directly (`ssh the0.fun 'ssh the0@10.10.0.1
  "sudo iptables -t nat -L -n -v"'`), and rewrote the project's topology leaf against live state.

## Cost
Roughly a dozen extra turns of investigation plus one fork spawn, all spent on a conclusion that was
wrong on the axis that mattered. The material cost is bounded only because the user caught it; the
unbounded branch — a shut-down VM and a broken VPN — was one click away.

## Self-critique of the agent system
The counter-signal was mine, observed before the conclusion, and I argued it away. That is the
cheapest possible catch and I missed it.

Three places in this repo already state the rule I broke, none of which fired:

1. `scripts/lib/hook_wiring.py` — the `WIRED`/`ABSENT`/`UNKNOWN` probe, where "I could not look" is
   first-class **by construction**. Mechanized, and therefore reliable — but only inside its own
   module's domain.
2. [[verify-ownership-before-shared-state-delete]] — "no live process foothold" ≠ "no owner", bias
   the classifier fail-safe. Prose, scoped to shared-state deletion.
3. [[verdict-covers-the-evidence-domain-it-claims]] — the level-2 principle. Prose, and explicitly
   scoped to *judging mechanisms* (gates, checkers, CI), which is why nothing in it addressed itself
   to me while I was reasoning.

So the failure is not a missing rule; it is a rule whose **statement was narrower than its domain**.
This leaf is the instance outside the judging-mechanism class that the level-2 principle named as
its own promotion trigger, and it is cited as provenance for that promotion to level 3.

The one genuinely uncovered gap it also exposes is structural rather than normative: a delegated
negative is **doubly bounded** — bounded by what the subagent thought to probe, and bounded again by
the parent's inability to see what that was. The fix has to travel on the return contract, not on
the parent's vigilance; see [[delegatable-work-patterns]] § Negative findings.
