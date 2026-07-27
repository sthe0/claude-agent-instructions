---
name: robot-run-acl-access
description: When data-warehouse/orchestration-platform operations run under a robot identity, set an explicit read ACL for the team's access group on EVERY op — else humans can't read stderr/logs/artifacts with their own token and debugging needs robot-token gymnastics.
type: reference
created: 2026-06-16
last_verified: 2026-06-16
---

# Robot-launched runs: set team-readable ACL on every operation

**Difficulty this removes.** A graph/job launched under a **robot** identity (a robot orchestration-platform/data-warehouse token) owns all its operations. Their stderr, job logs, and output tables are then readable only by the robot. A human developer (you / the team) gets *Access denied* with their own OAuth token and is forced into robot-token gymnastics to debug a failure: fetch job stderr under the robot token → resolve a step-result id → fetch that step's log via the platform's log-proxy API. Slow, and only the robot-token holder can do it.

**Rule — think about ACL up front, at launch-design time.** Whenever a task launches data-warehouse/orchestration-platform operations under a robot identity, plan for human read access from the start:

1. **Attach a read ACL for the team's access group to every operation spec**, via the platform's operation ACL mechanism:
   `{"acl": [{"subjects": ["<team-access-group>"], "action": "allow", "permissions": ["read"]}]}`.
2. **All ops, not a subset.** The classic bug: ACL is set on the "main" ops (train/eval) but a later-added or ported-in stage (e.g. a dataset-compose stage, a remote-copy bridge) reuses bare `update_defaults` and silently drops the ACL → exactly those blocks become unreadable, and they're often the ones that fail. Audit *every* op decorator, especially code ported from a standalone workflow that originally ran under a human.
3. **Name the subject constant.** Put the access-group identifier in a named constant (e.g. `PROJECT_ACL_SUBJECT = "<team-access-group>"`) and share it across all op-spec sites — a raw group id inline is opaque and invites the subset-drift in (2).
4. **Verify on the real axis.** "ACL kwarg present in code" is static. Confirm a *human* token can actually read a finished op's stderr/output before calling it done.

**Concrete instance (a past ticket).** The team access group was set on the train and eval ops but NOT on the ported compose ops or a newly added remote-copy op → their logs needed the robot token. See the project's own checkpoint notes for the follow-up batch. Related anti-pattern: [[mirror-working-caller-before-bypass]] — the compose ops should have mirrored the meta's ACL convention when ported in.
