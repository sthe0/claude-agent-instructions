---
name: xdist-auto-parallel-fork-bomb
description: A conftest that auto-enables pytest-xdist when "dist is off" re-fires inside every worker (xdist resets dist/numprocesses there) → exponential fork bomb that hung a no-swap VM for ~2 h on 2026-10-07
type: reference
schema: leaf/v1
created: 2026-10-07
last_verified: 2026-10-07
---

## Difficulty

On 2026-10-07 a session prototyping "auto-parallel tests" ran a `conftest.py` whose `pytest_configure` set `numprocesses=auto, dist=load, tx=popen*n` whenever xdist was importable, `numprocesses is None` and `dist == "no"`. Inside every xdist worker, `xdist/remote.py::setup_config` resets exactly those options (`dist="no"`, `numprocesses=None`), and `config.args` is unchanged. Each worker therefore met the guard again and spawned 12 more (12 → 144 → 1728). The VM (12 cores, 50 GB RAM, no swap) ran out of memory in seconds and hung for about 2 h. Workers sat in D-state, and the kernel OOM-killer kept picking small `oom_score_adj=200` processes (the VCS client, `claude`) instead of them. The VCS client auto-restarted, so the cycle never ended, and only a hard reset recovered the VM. From outside it looked like "too many tmux sessions", but the cause was this one bomb. Tracked in [#304](https://github.com/sthe0/claude-agent-instructions/issues/304).

## Guidance

- Any hook that turns xdist on must first return early inside a worker: `hasattr(config, "workerinput")` or `os.environ.get("PYTEST_XDIST_WORKER")`. It must also skip when `config.option.tx` is already set or `-n` / `-p no:xdist` was passed explicitly.
- Never trial such a conftest without a cap. Run it as `systemd-run --user --scope -p MemoryMax=4G -p TasksMax=200 python3 -m pytest ...`.
- On a host without swap, a userspace killer (earlyoom; systemd-oomd needs PSI, which is absent on older kernels and cgroup v1) plus a `MemoryMax` on `user@<uid>.service` keeps ssh alive through a runaway. These guards make the machine survive, but they do not stop the runaway itself.

## See also

- [Core #304](https://github.com/sthe0/claude-agent-instructions/issues/304) — the guard fix required before auto-parallel lands.
