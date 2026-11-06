---
name: an-xdist-worker-resets-the-options-an-auto-enable-hook-tests-so-the-hook-fires-in-every-worker
description: xdist resets dist and numprocesses inside each worker, so a conftest hook that auto-enables xdist on those options meets its own guard again and spawns workers recursively
type: reference
schema: regularity/v1
generality: 1
induced_from: [xdist-auto-parallel-fork-bomb]
rule_derived: "lifted from xdist-auto-parallel-fork-bomb; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# an-xdist-worker-resets-the-options-an-auto-enable-hook-tests-so-the-hook-fires-in-every-worker

## Regularity

Inside every pytest-xdist worker, xdist/remote.py::setup_config resets dist to "no" and numprocesses to None while config.args is unchanged. A conftest pytest_configure that sets numprocesses=auto, dist=load and tx=popen*n whenever xdist is importable, numprocesses is None and dist == "no" therefore meets its own guard condition again in each worker and spawns a further generation of workers (12, then 144, then 1728 on the 12-core VM of 2026-10-07).

## Generality

Level 1 — ranges over conftest hooks that turn pytest-xdist on by testing the dist and numprocesses options. It is deliberately not claimed for auto-enable hooks that guard on worker identity, nor for parallel runners other than xdist.

## Induced from

- [[xdist-auto-parallel-fork-bomb]] — contributes the observed case behind the statement: the 2026-10-07 auto-parallel conftest whose workers each spawned 12 more.

## Refutation

A conftest with that guard, run under xdist -n, spawns workers only once (workers do not re-run the enabling branch), or setup_config in the installed xdist does not reset dist and numprocesses in workers.
