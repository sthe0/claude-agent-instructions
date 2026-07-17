---
name: 2026-07-17-reviewer-claim-accepted-on-authority
description: A thinker review asserted that a stray double-prefixed remote ref was an 'alternative cause' of a hanging arc pr create. I accepted it on authority and added a plan pre-step to DELETE that ref on the shared Arcanum server. The reviewer later self-retracted, and two checks I could have run in seconds refuted it outright: the 9.70s SUCCESSFUL run produced the same double-prefix, and the trace showed the ref was CREATED by the first hang (SetRemoteRef TypeCreate at 22:50:30) — a consequence, not a cause. A review raises hypotheses at the same epistemic rank as my own; the asymmetry is that acting on a wrong one against shared state is not free.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor (AskUserQuestion, after asking 'Так а содержательно что ты поправил?' and being told the product behaviour did not change: 'Да, решена (Recommended)')"
refs: [https://a.yandex-team.ru/review/14483695, https://a.yandex-team.ru/review/14483719]
created: 2026-07-17
last_verified: 2026-07-17
---

# A reviewer's claim is evidence to check, not a fact to act on — especially when acting means touching shared state

## Difficulty
A review verdict carries social authority (independent, fresh-context, adversarial by design) that does NOT translate into evidential authority. Accepting a reviewer's causal claim without running the check that would refute it is the same unverified-premise failure as accepting my own hunch — but it FEELS like diligence, because a second party said it. It becomes damaging exactly when the claim implies a destructive or outward action: here, deleting a ref on a shared server that other sessions and PRs may reference.

## Order & criterion
Fix a pre-existing red test (T5) and land the seam on trunk; a thinker review gated the plan.

**Acceptance check:** measurable: suite 35 PASS / 0 FAIL and 'arc show trunk:<file>' contains the seam on trunk

## Contexts

### 2026-07-17 — Thinker plan-review claimed a stray remote ref caused an arc hang
- Where it arose: Any spawned-review or judge-panel step whose finding implies a state-changing action
- Working plan: 1) Add the CHECK_SYMLINKS_SKIP_MOUNT_CHECK seam mirroring setup-local.sh's SETUP_LOCAL_SKIP_MOUNT_CHECK; prove it with a negative control and a discriminating mutation. 2) Land on trunk via an -F auto-merge PR, poll to 'merged as rNNNNNNN', confirm with 'arc show trunk:'.

## Cost
`agentctl resolve` surfaced `total_cost_usd: null`, `spawn_count: 0` for this session — the two
thinker plan-reviews were spawned before a context compaction, so the per-session ledger cannot
attribute them and no honest dollar figure is available. The load-bearing cost here is wall-clock,
not tokens: **~18 min** lost to a self-inflicted `arc pr create` hang holding the repo write lock,
plus one full plan-review cycle spent carrying the retracted ref claim — any plan edit stales the
bound review, so a wrong pre-step is not cheap to carry.

## Self-critique of the agent system
Two misses of the same shape. (a) I accepted the reviewer's ref claim and turned it into a destructive pre-step against shared state before checking it — the check took seconds once I ran it. Rule: a review finding that implies a state-changing action must be verified against evidence BEFORE it enters the plan; a finding that implies only a wording change can be taken cheaply. Grade the cost of being wrong, not the authority of the source. (b) Separately, I chained 'arc push 2>&1 | tail -3 && arc pr create', so a FAILED push returned tail's exit 0 and fired the PR create anyway — which hung 18 min holding the repo write lock and made the whole mount read as broken. Both are the same root: acting on an unchecked premise. Credit where due: the same review caught a real defect I had missed — a ';' in final_verification terminated the '&&' chain, letting a RED suite pass on the trunk grep alone.
