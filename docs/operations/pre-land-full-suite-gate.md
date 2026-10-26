# Pre-land full-suite gate

A change can pass review and its own tests and still break a test far from the files it touched; nothing ran the whole suite before the push. This gate runs the repository's test suite on the committed candidate before it lands on `main`, and refuses the landing when the candidate adds a failure the remote tip does not already have.

## Parts

- `scripts/lib/suite_gate.py` — the decision rule and the run, record and baseline handling.
- `scripts/suite-gate.py` — the CLI: `run`, `check`, `show`, `waive`, `pre-push`.
- `scripts/lib/landing_gate.py` — the layer shared with the instruction smoke gate: binding a record to a candidate and a remote tip, the record store, the pre-push loop and the output defusing. The smoke gate (`pre-land-instruction-smoke-test.md` § Landing gate) is built on the same layer.
- `scripts/land-branch.py` and `githooks/pre-push` — the two places that enforce it.

## What the rule is

The suite runs twice at most. The candidate runs in an isolated root built by `scripts/instruction-sandbox.sh` (a private clone, a fake HOME, a scrubbed environment: no `AGENTCTL_*`, no `PYTEST_ADDOPTS`, no `PYTHONPATH`). Only when it fails something does the remote tip run in a second root; that baseline is cached per tip sha. Failed test ids are subtracted:

- a failure the tip's baseline also has is **absorbed**: it is not this candidate's doing and does not block;
- a failure the baseline lacks is rerun once on its own and blocks when it fails again; one that passes on the rerun makes the run UNAVAILABLE (flaky), not admitted.

Outcomes follow the smoke gate: PASS, FAIL, UNAVAILABLE. UNAVAILABLE (a timeout, a bad pytest exit, a failed build, a flaky new failure) is admitted only under a waiver with a reason (`run --waiver`, `waive --sha … --reason …`, `land-branch.py --suite-waiver`). FAIL is never admitted. Fail closed: a missing, unreadable or inconsistent record and any git error refuse.

## The record

`suite-gate.py run` writes `<git common dir>/suite-gate/<candidate sha>.json` (`suite-run/v1`). A record admits a landing only when it names the candidate, was built on the *current* remote tip and has an admitting status. A remote that moved on makes it stale and the suite runs again. The candidate must contain the remote tip; otherwise merge it and rerun.

## What is exempt

A landing that changes only `.md` files under `memory-global/` or `docs/` is exempt (the memory/docs-only case): such files cannot fail a test. The decision reads git's own path list over the whole range, counts both sides of a rename and every deletion, and treats an empty change list as required. It cannot be requested. A repository without `scripts/suite-gate.py` in either the tip or the candidate tree is not gated at all.

## Landing

`land-branch.py` fetches the remote tip once and checks that the candidate contains it, then runs the suite gate, then the smoke gate, and pushes only when both admit. The suite line (`SUITE: ADMITTED …`, `SUITE: exempt (memory/docs-only): <files>` or `SUITE: not required …`) is printed before the smoke line and the push report; a refusal or a gate error exits 2 with `NOT-LANDABLE: suite gate: …` and nothing is pushed. `--suite-waiver "<reason>"` and `--suite-timeout <seconds>` pass through to the run (default 1800 s per run; config key `suite-gate-timeout-s`). `--check` additionally prints a `SUITE-STATUS:` line from local state and never runs the suite.

## The hook

`githooks/pre-push` reads the pushed refs once and feeds the same lines to both gates, runs both even when the first refuses, and fails when either refused, so a refusal of one gate does not hide the other. A raw `git push` of an unrecorded candidate is refused with `pre-push: to land a change run: python3 scripts/suite-gate.py run`.

## Cost

A landing that needs the suite pays its wall-clock inside the `land-branch.py` call, with a second run when the candidate has failures to compare with the tip. Callers that wrap the landing with a short timeout must expect that, or run `suite-gate.py run` first and land the recorded commit.

## Not covered

- **Anything the suite does not test.** A green suite shows no regression in what is tested, nothing more.
- **A failure that already exists on the tip.** It is absorbed by id. A candidate that makes an already-failing test fail for a new reason, or a scanner-style test that already fails on the tip and now also fails on a new violation, passes the gate.
- **Flaky tests.** A new failure that passes on its rerun gives UNAVAILABLE, which needs a waiver; a flake that fails twice blocks.
- **Races between landings.** The record is bound to the tip at run time. Two candidates admitted against the same tip can still break each other once both land; there is no merge queue, and the second landing is only re-run when the first has moved the tip.
- **Memory/docs-only landings.** They run nothing; a test that reads a markdown file's content is not exercised by such a landing.
- **A suite that cannot run here.** A missing `pytest-xdist` or a failed sandbox build is UNAVAILABLE, not a verdict on the candidate.
