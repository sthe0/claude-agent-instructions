---
name: 2026-07-14-claim-provenance-ledger-reasoning-deliverable
description: A reasoning/research (non-code) deliverable had no engine-level done-axis, so the agent under-investigated and presented fabricated claims as facts. Fix: a claim-provenance ledger in agentctl — typed claims (axiom/derivation/assumption) with a fail-closed CLOSURE check (the mechanized RULE), enumeration of load-bearing claims kept with the model (PERCEPTION), regex rejected as a formal proxy that types strings not claims; deliverable_kind at classify arms the resolution gate.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [formalization-ladder-l1-l3.md, 2026-07-14-determinize-memory-org-principle-in-code.md, 2026-07-09-gate-must-execute-what-it-attests.md]
created: 2026-07-14
last_verified: 2026-07-15
---

# Provenance is the done-axis for a reasoning deliverable: mechanize CLOSURE, keep enumeration as perception

## Difficulty
A reasoning/research deliverable had no equivalent of code's tests-green done-criterion, so 'reads confidently' passed for 'grounded' and fabricated decisions/judgments/proximity-guessed numbers shipped as facts. The done-axis (provenance) was left implicit, so under-investigation was invisible to the engine.

## Order & criterion
planner -> thinker plan-review -> user approval of the 8-stage plan -> Layer A (agentctl ledger subsystem) / Layer B (CLAUDE.md + planner discipline) / Layer C (formalization-ladder leaf) -> verify-final -> land to trunk. Mid-execution stage-7 difficulty (verify_command relative path) routed through the full engine declare->investigate>=2H->critique->normalize->plan-review->replan cycle.

**Acceptance check:** measurable: pytest scripts/tests/ 1920 passed/0 failed (15 hook_turn_end_gate failures confirmed a spawn-sandbox artifact, clean in root shell); verify-agentctl OK; runtime_check_ledger_gate OK; verify-final green; commit 45595de on origin/main (0/0 divergence).

## Contexts

### 2026-07-14 — initial
- Where it arose: claude-agent-instructions Core, main serving checkout; agentctl session 07db333d-f63c-44f2-95f4-6c9d54a674b8; commit 45595de.
- Working plan: claim-provenance-ledger.toml (8 stages): ledger.py/plugins_ledger.py typed claims + fail-closed CLOSURE; deliverable_kind at classify arms the resolution gate; advisor.enumerate_claims an independent semantic cross-check (recall-widener, NOT regex); Layer B references in CLAUDE.md + planner SKILL/policy; Layer C leaf formalization-ladder-l1-l3 (L1-L3 ladder + L3 refusal for empirics + honest residual recall<100%/DECOY/junk-dismiss).

### 2026-07-15 — first real application (DEEPAGENT-448 GLM-5.2 migration recommendation)
- Where it arose: deepagent project, DEEPAGENT-448; agentctl session 07db333d; a 3-stage ledger-close plan (ground z-ai price + type 14 claims → spawn:thinker enumeration+soundness cross-check → tech-writer polish + publish to the ticket).
- **Load-bearing lesson: structural CLOSURE ≠ soundness.** `ledger-check` returned "closed" both BEFORE and AFTER a load-bearing error was fixed — the "communal SLA = free / no external spend" premise was well-formed (an axiom with a source) but factually WRONG (the quota'd SLA slice is billed 70/30, ABC-debited; only the Best-Effort bench endpoint is free). The formal closure check cannot catch a well-typed false axiom. What caught it was the **independent enumeration/review layer** (the PERCEPTION half): a spawned thinker cross-check AND a parallel yandex-guru domain read BOTH surfaced it, plus the thinker's highest-value catch `zai_egress_viable` (the z.ai fallback ships ~33.9B prod prompt tokens/mo to an *external* provider — compliance unverified). Takeaway: for a reasoning deliverable, always run the independent cross-check even when the ledger already closes; treat "ledger closed" as necessary-not-sufficient and budget a skeptical enumeration pass whose job is to attack the *premises*, not just the structure.
- **Second lesson (user correction at the resolution gate): don't conflate "can't measure now" with "not viable".** The draft rejected self-host categorically because the measurement GPU pool was revoked. User pushed back: a temporary capacity gap is not a principled impossibility, and a preliminary estimate can still be given. Fix: reframed self-host as deferred-but-viable with an honest assumption-typed preliminary sizing (tens of H200, wide bounds) + the cheap measurement path (short saturation run on a small temporary grant). A "not recommended" verdict must distinguish *infeasible* from *unmeasured-so-far*.
- Dispatch nuance: a spawn:thinker cross-check runs in its OWN session — its `ledger-add`/`ledger-dispose` calls do NOT mutate the parent ledger; its output is a review returned as text. The root must re-apply the recommended claims/dispositions to the parent ledger itself.
- Publication: one Russian comment posted then PATCH-edited in place (single clean comment > addendum) for the self-host correction; both shown to the user before publishing per the acceptance-review criterion. Ticket left OPEN (evaluation done; migration pending the quota grant).

### 2026-07-15 (cont.) — two user corrections at the resolution gate on the SAME deliverable
- Where it arose: DEEPAGENT-448, in-thread refinement of the published recommendation comment (id 1222494430, longId 6a56af9b7f7fc61a4c4e7349); PATCH-edited in place, HTTP 200, read-back verified.
- **Lesson A — compute the estimate; "can't measure directly" ≠ "can't estimate".** The draft gave self-host sizing as a qualitative "десятки H200" and hid behind "the measurement pool was revoked". But the quantity WAS computable: two already-measured stand shapes (input 4096/output 512 → 640 tok/s agg; 1024/256 → 925) are enough to separate the prefill ceiling P from the decode ceiling D (two-resource model: `req/s×input/P + agg_output/D = 1` per shape → P≈8 340, D≈1 647 tok/s per 8×H200 replica), then solve gen/s at the REAL profile 42 419/608: `1/(42419/8340+608/1647)=0.18 gen/s` → ~120 H200 peak / ~48 busy / ~16 avg. The earlier "16–24 H200" was silently at the STAND 4096 profile covering only average load — an order of magnitude low for the real 42k input (prefill ×10). **Rule:** when a load-bearing quantity is derivable from measurements already in hand (even indirect ones), do the arithmetic and ship a number with explicitly-typed assumptions + caveats (prefix-cache↓, tail p95 90 601↑, extrapolation-not-direct-42k-measure) — do NOT deliver a qualitative range. This sharpens the 2026-07-15 "can't-measure-now ≠ not-viable" lesson: viability AND a computed estimate are both owed, not just the viability verdict.
- **Lesson B — provenance isn't closed until each source is independently verifiable with an UNAMBIGUOUS method.** The first claims table had a source NAME per row ("Solomon", "DEEPAGENT-440", "billing.md") but no way to open the exact value — notably no Solomon graph URLs. User: "способ проверки должен быть ясен и однозначен из того как приведён источник". Fix: every row carries a verification METHOD — an axiom links to the exact measurement/price (live Solomon deep-links with the full selector `project=deepagent&cluster=deepagent_prod&service=stats&l.name=model_input_tokens&l.model_id=qwen3_14B_unified_agent&l.host=all`, obtained via yandex-guru not fabricated — no named dashboard exists); a derivation shows the recompute formula referencing the numbered axiom rows (`стр. N`); an assumption states its basis + an explicit "наш выбор / не подтверждено" flag. This strengthens the L1 ledger criterion: a source-NAME is necessary-not-sufficient; the citation must let a reader reach the exact number in one unambiguous step. **Solomon selector gotcha (corrected by guru):** `deepagent_prod` is the CLUSTER, not a model_id; real model_id=`qwen3_14B_unified_agent`, service=`stats` (Django backend emits the token counters, not sglang/GPU-unit), sensor label key is `name` (not `sensor`).

## Cost
engine ~$20.5, 10 spawns, 7 attributed stages; one mid-execution difficulty cycle. (Application run DEEPAGENT-448: ~$1.6, 1 spawn, in-thread stages 1&3.)

## Self-critique of the agent system
Two execution lessons. (1) A verify_command/final_check path to a file OUTSIDE [meta].repo_root must be ABSOLUTE — the engine runs every check from scripts/, so a relative skills/... path fails at record-time (stage-7 defect; normalized note-level; deeper fix = lint it in verify-plan-file.py, a carried follow-up). (2) When root legitimately completes a spawn:developer stage as self-improvement instruction-prose (a root-owned content class, here gated by the user's AskUserQuestion choice), record via direct record-result --status passed --control <self-review> rather than re-dispatching — re-dispatch would only re-hit the same byte-ceiling CLARIFY on already-done work; action=dispatch is the engine default, not a hard requirement, once the measurable verify_command passes.
