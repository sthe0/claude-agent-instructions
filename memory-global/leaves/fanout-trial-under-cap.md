---
name: fanout-trial-under-cap
description: Before trialing any mechanism that spawns processes per invocation (test parallelizers, pools, recursive hooks, spawners), check how the hook looks from inside a child and run the trial under a memory + task cap; an uncapped trial of such a mechanism cost a 2 h machine reset on 2026-10-07
type: feedback
schema: leaf/v1
created: 2026-10-07
last_verified: 2026-10-10
---

## Difficulty

A prototype that turns on a process fan-out (a `conftest.py` auto-enabling pytest-xdist, a worker pool, a hook that re-spawns the tool it runs in) fails differently from ordinary code: a wrong guard does not produce a red test, it produces an exponential process tree. On 2026-10-07 such a prototype was run with no resource limit and without asking how the same hook evaluates inside a spawned worker; the workers re-fired it (12 → 144 → 1728 processes), the no-swap VM ran out of memory within seconds and stayed hung for about two hours until a hard reset. Nothing in the developer or coordinator norms asked for a cap or a re-entrancy check before the trial, so the cost of one mistake was the machine, not the trial.

## Guidance

To achieve a trial whose worst case is a killed trial rather than a reset machine, do both of the following before the first run of anything that spawns processes per invocation:

1. **Child re-entrancy check.** Read how the spawned child sees the same configuration or hook: which options the framework resets in the child (pytest-xdist resets `dist` and `numprocesses` in every worker and leaves `config.args` intact), which environment markers identify a child (`PYTEST_XDIST_WORKER`, `hasattr(config, "workerinput")`, a depth env var such as `AGENT_RECURSION_DEPTH`), and make the hook return early on any of them. A fan-out hook without an explicit "I am the child" exit is a fork bomb waiting for its first run.
2. **Cap the trial.** Run the first invocations through `bash scripts/cap-run.sh --extra-tasks <N> --timeout <S> -- <command>`: it sets RLIMIT_NPROC to the user's current task count plus N (never above a limit already in force) and a wall-clock timeout, so a runaway fork fails with EAGAIN instead of growing; enforced everywhere for a non-root user.
   A cgroup scope (`systemd-run --user --scope -p TasksMax=<N> -p MemoryMax=<G>`) only after proving on the host that it is enforced (cgroup2fs with the pids and memory controllers delegated) — on cgroup v1 the user manager accepts both limits and ignores them. Keep the cap for the regression test that pins the guard, so the test itself cannot bomb the CI host.

Apply the same two steps to a self-devised wrapper around a spawner (an `Agent` fan-out, a `claude -p` loop, a `make -j` with a computed `j`). A session-wide cap in the agent launcher (`agent-dispatch.sh`) is the structural form of step 2; the per-trial cap remains the author's responsibility where that launcher is absent.

## See also

- [Core #304](https://github.com/sthe0/claude-agent-instructions/issues/304) — closed: the guard (`scripts/tests/_xdist_auto.py`, pinned by `test_conftest_auto_parallel.py`) is in place and the suite now runs in parallel by default. This leaf is about the FIRST trial of a NEW fan-out mechanism, not a ban on parallel test runs: running the existing suite in parallel needs neither the cap nor a serial fallback. Opt out only with a stated reason (`-n 0`, `-p no:xdist`).
- [[universal-negative-control-via-mutation-catalogue]] — the regression test that pins the guard must go RED when the worker exit is removed.
