---
name: ssh-controlmaster-group-cache
description: After usermod -aG on a remote host, a "new" ssh still shows old groups because ControlMaster/ControlPersist reuses the master session; drop the master to refresh.
type: reference
created: 2026-06-16
last_verified: 2026-06-16
---

# SSH ControlMaster caches group membership

*Difficulty removed: after granting a remote user a new group (`sudo usermod -aG <group> <user>`), a freshly issued `ssh host` still reports the **old** group set, so access checks fail and you wrongly conclude the grant didn't work.*

## Cause

A `~/.ssh/config` with `ControlMaster auto` + `ControlPath` + `ControlPersist <N>m` (common in `Host *` blocks) multiplexes new `ssh` invocations over a **persistent master connection**. Group membership is resolved by PAM **at master-login time**; every multiplexed child inherits that frozen group set. So a "fresh" `ssh` opened within the `ControlPersist` window is **not** a new login and does not pick up the new group.

Symptom: `ssh host id` shows the old `groups=...` and `cd`/write into the group-protected dir is denied, even though `usermod` succeeded.

## Fix

Tear down the master, then reconnect:

```bash
ssh -O exit host            # drop the persistent master socket
ssh host id                 # now a real new login → new group visible
```

(Equivalently delete the `ControlPath` socket, or `ssh -o ControlMaster=no host`.)

## Related

- [[remote-sudo-access-paths]] — getting sudo / cross-user access on a remote host.
- Pairs with setgid shared-dir setup: `mkdir` owned by the target user, `chmod 2775`, `git init --shared=group`, add the collaborator to the owner's group, and `git config --global --add safe.directory <repo>` on the collaborator's side to clear git "dubious ownership".

> verified by: the0.fun setup 2026-06-16 — `usermod -aG nick the0`; first `ssh` still lacked `1001(nick)`; `ssh -O exit the0.fun` then `ssh` showed it.
