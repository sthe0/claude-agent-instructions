---
name: robot-run-acl-access
description: When YT/Nirvana operations run under a robot identity, set an explicit read ACL for the team's idm-group on EVERY op — else humans can't read stderr/logs/artifacts with their own token and debugging needs robot-token gymnastics.
type: reference
created: 2026-06-16
last_verified: 2026-06-16
---

# Robot-launched runs: set team-readable ACL on every operation

**Difficulty this removes.** A graph/job launched under a **robot** identity (robot Nirvana/YT token) owns all its operations. Their stderr, job logs, and output tables are then readable only by the robot. A human developer (you / the team) gets *Access denied* with their own OAuth token and is forced into robot-token gymnastics to debug a failure: `yt get-job-stderr` under the robot token → step-result-guid → `ui-api-proxy/nv-api/api/logs/<wi>/<step-result-guid>/stderr.log`. Slow, and only the robot-token holder can do it.

**Rule — think about ACL up front, at launch-design time.** Whenever a task launches YT/Nirvana operations under a robot identity, plan for human read access from the start:

1. **Attach a read ACL for the team's idm-group to every operation spec**, via the YT operation ACL (`job_scheduler_yt_custom_spec` / operation `acl`):
   `{"acl": [{"subjects": ["idm-group:<ID>"], "action": "allow", "permissions": ["read"]}]}`.
2. **All ops, not a subset.** The classic bug: ACL is set on the "main" ops (train/eval) but a later-added or ported-in stage (e.g. a dataset-compose stage, a remote-copy bridge) reuses bare `update_defaults` and silently drops the ACL → exactly those blocks become unreadable, and they're often the ones that fail. Audit *every* op decorator, especially code ported from a standalone workflow that originally ran under a human.
3. **Name the subject constant.** Put `idm-group:<ID>` in a named constant (e.g. `DEEPAGENT_ACL_SUBJECT = "idm-group:410590"`) and share it across all op-spec sites — a raw numeric idm-group id inline is opaque and invites the subset-drift in (2).
4. **Verify on the real axis.** "ACL kwarg present in code" is static. Confirm a *human* token can actually read a finished op's stderr/output before calling it done.

**Concrete instance (DEEPAGENT-430).** `idm-group:410590` (deepagent read) was set on train (`train_model.py`) and eval (`test_quality.py` `_DEEPAGENT_OP_ACL`) ops but NOT on the ported compose ops or the new `remote_copy_table` → their logs needed the robot token. See project checkpoint `deepagent-430-checkpoint.md` follow-up batch. Related anti-pattern: [[mirror-working-caller-before-bypass]] — the compose ops should have mirrored the meta's ACL convention when ported in.
