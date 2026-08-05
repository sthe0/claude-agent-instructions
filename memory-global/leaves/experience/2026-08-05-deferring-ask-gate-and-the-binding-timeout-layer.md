---
name: 2026-08-05-deferring-ask-gate-and-the-binding-timeout-layer
description: The agent held the rights, the tools and a finished diagnosis of a defect and still handed the user a menu whose every option deferred the work (file a ticket / leave as is) — capability-before-offload was live in the snapshot and simply did not fire, so the repair had to be structural (a PreToolUse gate on AskUserQuestion, regex prefilter over OPTION text only + fail-open semantic judge) rather than another line of prose; building it surfaced a second, larger defect one layer up — every judge-calling hook in the fleet is registered at a 5 s harness timeout while a live judge call takes 11-15 s with a 47 s tail, so those gates are inert in production and, being fail-open, silent about it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
tier: 1
refs: [memory-global/leaves/capability-before-offload.md, memory-global/leaves/regex-not-for-semantic-classification.md, memory-global/leaves/experience/2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch.md]
plan_file: /home/the0/.claude-agent/plans/gc-criterion-and-defer-gate.toml
created: 2026-08-05
last_verified: 2026-08-05
---

# A menu in which no option does the work, and the timeout layer that made its gate inert

## Difficulty
Asked to restore this machine's workspace mounts, the agent found a real defect in the mount-GC script it ran: the script's "is this branch landed?" criterion read *does the branch have an upstream*, which is systematically inverted — a closed task's branch loses its upstream **because** the review request merged and the remote branch was deleted. Having the rights, the tools and a finished diagnosis, the agent filed a ticket and presented an `AskUserQuestion` whose every branch deferred: *file it as a separate task (Recommended)* / *leave it alone*. The user's correction — "why did you file a ticket instead of offering to fix it right away, you have all the rights" — names the shape exactly: **deferral-offload through the composition of the options**, not through anything the agent said.

<!-- Language exception: the user's correction is quoted in translation; the original is Russian and lives in the session transcript. -->

`capability-before-offload.md` was loaded in that session's snapshot. It did not fire. A norm that is present and does not fire is not repaired by restating it — it needs a mechanism at the point of the act, and the point of the act here is the `AskUserQuestion` tool call itself.

Building that mechanism then exposed a defect one layer above it: the hook's *own* registered harness timeout. Raising the judge's internal ceiling 8 s → 30 s fixed a **non-binding** limit; the binding one was `install-reminder-hooks.sh` registering the hook at `"timeout": 5` **seconds**, against a measured judge latency of 11–15 s. The gate would have been killed on the fifth second of every real invocation, printed nothing, and let the menu through — indistinguishable, from the outside, from a gate that decided to allow.

## Order & criterion
Two independent deliveries, one approved plan (6 stages):
- **A — the defect the ticket was about:** replace the landed-criterion in the mount-GC script with *reachability of the branch's commits from trunk*, with exactly one lazy fetch when the local trunk ref is stale, and KEEP on fetch failure (offline must never forget a store).
- **B — the norm that did not fire:** `hook-deferring-disposition-gate.py` on `PreToolUse`/`AskUserQuestion` + `advisor.judge_deferring_disposition`, patterned on the existing escalation gate: high-recall regex prefilter → fail-open model judge → deny via `permissionDecision`.

**Acceptance check (measurable):** the GC script's `--selftest` exit 0 (8 cases); a live dry-run of it exit 0; `pytest tests/test_deferring_disposition_gate.py tests/test_advisor.py` exit 0 — plus, on the runtime axis, the gate actually denying its own founding ask with the real judge, not a mock.

## Contexts

### 2026-08-05 — Structural repair of a norm that did not fire
- Where it arose: a machine-local workspace-mount GC script (org-side ticket + review request carry that half of the record) and Core `scripts/` (git worktree `~/cai-wt-defer-gate`, branch `defer-gate`, commit `c4fd32c`).
- Working plan: 6 stages — ticket-side mount + branch; criterion replacement + selftest 4→8 cases; commit + review request; isolated Core worktree; judge + hook + tests (three code-review rounds); registration + full suite + commit.

**The split that is load-bearing.** The *same* option vocabulary ("ticket", "later", "leave as is") is defective when the agent could act and legitimate when the work is genuinely someone else's. So the regex may only *widen recall*; the meaning is the model's call ([[regex-not-for-semantic-classification]]). Two scoping decisions came out of live runs, not from reasoning:
- the prefilter must read **option text only, never the question stem** — a resolution gate ("shall we consider the task resolved?") carries the cue word in its own wording while every option is a plain confirm, and stem-scoped prefiltering burned a judge call on every such ask;
- the decision is **per question**, not per payload — the predicate is a property of one menu, and the deny must name *which* menu, or the agent rewrites the wrong one and hits the same deny.

**Each review round found the blocker one layer higher than the last.** Round 1: the registry/audit layer. Round 2: the judge's own timeout. Round 3: the harness registration timeout. The pattern is worth naming — a gate has a *stack* of ceilings (judge → hook budget → harness registration), and fixing any one of them proves nothing about the others. Only a **live end-to-end run with the real judge** distinguishes them; tests-green distinguishes none.

**Latency was characterised, not assumed.** Eight calls on the founding ask with no timeout: 10.5 / 11.5 / 12.2 / 12.6 / 13.7 / 14.2 / 15.4 / **47.0** s. The distribution has a heavy tail, so the chosen budget (20 s hook-wide, 25 s registered) buys ~7/8 recall and drops the tail — documented in-file as a deliberate trade rather than left as an unstated one, because no ceiling a user tolerates ahead of an interactive menu covers 47 s.

**Mechanical traps paid for here** (several are re-payments of known ones): `gen_crutch_registry.py` / `crutch-inventory.py` enumerate via `git ls-files`, so a new file needs `git add -N` **before** any index-dependent run; the spawn-specialist allow-list lets a developer run `python3 -m pytest` but **not** `bash <script>` nor `python3 <script>`, so registry regeneration and installer runs are the coordinator's job (two identical PERMISSION-REQUESTs before that sank in); `agentctl resolve-permission` has no `--note`; piping `agentctl dispatch` through `tail` **discards the specialist's report** — redirect full stdout to a file; driving agentctl against a non-canon tree needs `PYTHONPATH=<canon>/scripts python3 -P -m agentctl` so `-P` drops cwd from `sys.path` and canon's copy wins while cwd stays the worktree.

**A foreign test's defect was named, not routed around.** A comment inside the `DESIRED` block of `install-reminder-hooks.sh` broke `test_no_desired_entry_is_ever_removed`, whose regex `"([^"]*\.py[^"]*)"` parses that block *including comments*, so a quoted aside (`"leave as is"`) shifts the pairing. The first fix reworded my own comment to avoid `.py` substrings — it worked, and left an invisible contract for the next author. The real fix is one line in the foreign test (`desired_block = re.sub(r"#[^\n]*", "", desired_block)`), after which the comment could carry literal filenames again.

## Cost
$22.63 attributed to 7 spawns over 2 dispatched stages (4818 s), plus an unmeasured main-session share across two context windows; 2 deliveries — 1 org-side review request (open) and 1 Core commit of 12 files, 989 insertions, 99 new/changed tests (full suite 3688 passed / 3 skipped).

## Self-critique of the agent system
The largest finding is one this task deliberately did **not** fix: all three judge-calling hooks (`hook-deferring-disposition-gate.py`, `hook-escalation-diagnosis-gate.py`, `hook-turn-end-gate.py`) were registered at 5 s. The two neighbours are therefore **inert in production today** — and because they are fail-open, they have been silent about it for their whole lifetime. That is the same family as [[2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch]]: *committed ≠ running*, and a fail-open mechanism has no way to report its own non-execution. The generalisable rule: **a fail-open gate needs an execution counter, not just a decision path** — otherwise "never denied anything" and "never ran" are the same observation.

Second: three review rounds on one stage is itself an effort signal. Each round was justified by a genuine blocker, but the first two would have been caught in one round by running the hook end-to-end with the real judge **before** the first review, instead of after the third. The runtime axis was checked last when it was the cheapest discriminator available.
