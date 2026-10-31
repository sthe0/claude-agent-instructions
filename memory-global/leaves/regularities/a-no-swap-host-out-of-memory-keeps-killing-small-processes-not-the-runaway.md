---
name: a-no-swap-host-out-of-memory-keeps-killing-small-processes-not-the-runaway
description: On a no-swap host a runaway in D-state exhausts memory while the OOM-killer keeps picking small restartable processes, so only a hard reset recovered the VM
type: reference
schema: regularity/v1
generality: 1
induced_from: [xdist-auto-parallel-fork-bomb]
rule_derived: "lifted from xdist-auto-parallel-fork-bomb; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-no-swap-host-out-of-memory-keeps-killing-small-processes-not-the-runaway

## Regularity

On the 12-core, 50 GB, no-swap VM that ran the 2026-10-07 xdist fork bomb, memory ran out within seconds and the machine hung for about 2 h: the runaway workers sat in D-state, and the kernel OOM-killer kept choosing small oom_score_adj=200 processes (the VCS client, claude) instead of them; the VCS client auto-restarted, so the cycle did not end and only a hard reset recovered the VM. From outside it looked like too many tmux sessions. systemd-oomd needs PSI, which is absent on older kernels and under cgroup v1; a userspace killer such as earlyoom and a MemoryMax on user@<uid>.service keep ssh alive through a runaway but do not stop the runaway itself.

## Generality

Level 1 — ranges over memory exhaustion on a host without swap caused by a runaway process tree whose members sit in D-state. It is deliberately not claimed for hosts with swap, nor for kernels where systemd-oomd has PSI available.

## Induced from

- [[xdist-auto-parallel-fork-bomb]] — contributes the observed case behind the statement: the 2026-10-07 hang of about 2 h and the hard reset that ended it.

## Refutation

On a no-swap host with D-state runaway workers the kernel OOM-killer selects the runaway workers first, or systemd-oomd runs on the same kernel with cgroup v1 and no PSI, or a MemoryMax on user@<uid>.service does not keep ssh responsive during an exhaustion.
