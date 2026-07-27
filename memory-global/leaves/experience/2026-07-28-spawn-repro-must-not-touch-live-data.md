---
name: 2026-07-28-spawn-repro-must-not-touch-live-data
description: A code-reviewer spawn asked to reproduce a reported KeyError in scripts/policy-scorecard.py called reprice()/write_ledger() against the real ~/.local/log/claude-policy-ledger.jsonl instead of the suite's monkeypatched tmp_path fixture, replacing 184 live rows with a 1-row stub. Its own two restore attempts were denied by the permission classifier, so it stopped and returned PERMISSION-REQUEST:. Remedy: a review/repro brief for code that reads or writes a live data path must name the fixture entry point the repro must use, hard-deny the production path by absolute name, and require a pre-flight backup — the same explicit hard-deny discipline the bypassPermissions rule already applies to MOUNTS, extended to DATA paths.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev (Да, оценка 4 — resolution gate, 2026-07-28)"
refs: [memory-global/leaves/spawning-specialists.md, memory-global/leaves/experience/2026-07-02-spawn-sandbox-excludes-declared-stage-material.md]
created: 2026-07-28
last_verified: 2026-07-28
---

# A repro-the-defect spawn will run the script against its LIVE data path unless the brief names the fixture and hard-denies production

## Difficulty
A spawn told to 'reproduce this defect' in a script that reads and writes a real data file will reach for the script's own default path, because that is the shortest route to the observed symptom and nothing in the brief says otherwise. Here the third code-reviewer of a stage confirmed a genuine KeyError in policy-scorecard.py's reprice() by running reprice()/write_ledger() against the production ledger ~/.local/log/claude-policy-ledger.jsonl rather than the suite's monkeypatch/tmp_path fixture. All 184 rows were replaced with a 1-row stub. The reviewer then tried twice to restore and was denied by the permission classifier both times, so it stopped and returned PERMISSION-REQUEST: — which is the correct behaviour on its side, and is also why the damage was caught immediately rather than silently. The root cause is not the reviewer: the brief authorized reproduction without ever naming a safe entry point or an unsafe path. The bypassPermissions discipline for developer spawns already requires an explicit hard-deny list, but it is written entirely about MOUNTS and directories — it says nothing about the live DATA a script under review operates on, so a reviewer obeying it to the letter still walks into the production file.

## Order & criterion
Reproduce, in a fresh reviewer context, a KeyError alleged to occur in scripts/policy-scorecard.py's reprice() when the ledger contains a row that the new skip guard leaves without a cost_usd field — then judge whether the defect is real.

**Acceptance check:** measurable — the defect either reproduces as a KeyError at the named line or it does not; the reviewer's verdict is bound to a specific revision. The criterion was met (the defect was real and was fixed in 3706f6b, later b5f59f3 on main), which is precisely why the collateral was easy to miss: a green done-criterion says nothing about what the run touched on its way there.

## Contexts

### 2026-07-28 — Third code-reviewer spawn clobbers the production policy ledger while confirming a real KeyError
- Where it arose: claude-agent-instructions, stage 2 of the model-generation-drift task: scripts/policy-scorecard.py reprice(), fixture scripts/tests/test_policy_scorecard_model_buckets.py, live file ~/.local/log/claude-policy-ledger.jsonl
- Working plan: 1. Detect: the reviewer's own PERMISSION-REQUEST: naming a restore attempt is the signal — read WHAT it tried to restore before answering the permission question. 2. Verify the backup before trusting it: the .bak from 18:53Z held 184 rows, 184 unique session ids, a single rate-table stamp and the same 4390.79 USD total as before the incident. 3. Restore, then re-run reprice --dry-run and confirm it reads back the same row count and total. 4. Rename, do not delete, the corrupt-state backup — and rename it OUT of the .bak-* glob, because the plan's own backup-reversibility final check takes sorted(glob)[-1] and would otherwise have gone red against the 1-row stub for a reason unrelated to the work. 5. State the residual honestly: rows appended between the backup and the incident are unrecoverable; the ledger's hook re-upserts by session_id, so live sessions self-heal but finished ones do not. 6. Re-norm the brief, not the reviewer: name the fixture entry point, hard-deny the production path by absolute name, require a pre-flight backup whenever a repro runs against real data at all.

## Cost
Host task (model-generation-drift, resolved 2026-07-28 at quality 4): **$16.83** list-price telemetry across **5 spawns** (tiers medium and small), ~45 min of engine wall-clock. Under the flat Max subscription that figure is telemetry, not money — see [[flat-max-billing-cost-framing]]. The incident's own cost was not budget but remediation and unrecoverable data: three code-review rounds instead of the planned one, plus ~23 minutes of ledger rows appended between the 18:53Z backup and the clobber that no restore could bring back.

## Self-critique of the agent system
The brief I wrote authorized 'reproduce the KeyError' and listed no data-path constraint at all, although I knew the script's whole subject matter is a live on-disk ledger. I had also already noticed, in the same task, that the plan's backup-reversibility check globs .bak-* and takes the last one — so I had every ingredient needed to foresee that a repro against real data would both corrupt the data and poison that check, and did not connect them until after the damage. The correction belongs to the spawn-brief template, not to this one task.
