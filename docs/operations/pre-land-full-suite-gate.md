# Pre-land full-suite gate

A change can pass review and its own tests and still break a test far from the files it touched; nothing ran the whole suite before the push. This gate runs the repository's test suite on the committed candidate before it lands on `main`, and refuses the landing when the candidate adds a failure the remote tip does not already have.

## Parts

- `scripts/lib/suite_gate.py` — the decision rule and the run, record and baseline handling.
- `scripts/lib/hermetic_run.py` — the namespace runner pytest runs in (§ Hermetic run).
- `scripts/suite-gate.py` — the CLI: `run`, `check`, `show`, `waive`, `pre-push`.
- `scripts/lib/landing_gate.py` — the layer shared with the instruction smoke gate: binding a record to a candidate and a remote tip, the record store, the pre-push loop and the output defusing. The smoke gate (`pre-land-instruction-smoke-test.md` § Landing gate) is built on the same layer.
- `scripts/land-branch.py` and `githooks/pre-push` — the two places that enforce it.

## What the rule is

The suite runs at most twice in full (the candidate, and the remote tip as a baseline), followed by two serial reruns of the new failures only. The candidate runs in an isolated root built by `scripts/instruction-sandbox.sh` (a private clone, a fake HOME, a scrubbed environment: no `AGENTCTL_*`, no `PYTEST_ADDOPTS`, no `PYTHONPATH`). Only when it fails something does the remote tip run in a second root; that baseline is cached per tip sha. Failed test ids are subtracted:

- a failure the tip's baseline also has is **absorbed**: it is not this candidate's doing and does not block;
- a failure the baseline lacks is **new**; so is every test the run collected but never finished (no `call` line in the per-test report, or a `setup` that skipped or failed), such as one that killed pytest. The new ids are rerun in **two fresh serial rounds**, each one pytest process (`-p no:xdist`, no slot, its own `cap-run.sh` timeout outside the 7200 s clock) on the same sandbox, candidate and tip. An id that fails either round is **confirmed** and blocks (FAIL). An id that passes both is **unconfirmed**: it no longer holds the landing (PASS when nothing is confirmed) and is journaled (§ The flaky list). A report with no `collected` lines, or a rerun that does not complete, is UNAVAILABLE, never a pass. The remote tip is re-read after the reruns; a tip that moved makes the run UNAVAILABLE. The record is bound to the pair (candidate, tip).

Outcomes follow the smoke gate: PASS, FAIL, UNAVAILABLE. UNAVAILABLE (a timeout, a bad pytest exit, a failed build, a run without a per-test report, a moved tip) is admitted only under a waiver with a reason (`run --waiver`, `waive --sha … --reason …`, `land-branch.py --suite-waiver`). FAIL is never admitted. Fail closed: a missing, unreadable or inconsistent record and any git error refuse.

## The record

`suite-gate.py run` writes `<git common dir>/suite-gate/<candidate sha>.json` (`suite-run/v1`). A record admits a landing only when it names the candidate, was built on the *current* remote tip and has an admitting status. A remote that moved on makes it stale and the suite runs again. The candidate must contain the remote tip; otherwise merge it and rerun.

The record also says what the run cost: `cpu_s` (CPU of the run's process tree), `peak_disk_mb` (largest size of the sandbox, sampled every minute), `slot_wait_s` and `slots_held`, the instants `queued_at`, `slots_acquired_at` and `timeout_started_at`, the `pytest_argv` it ran, and whether it was `hermetic` (with `hermetic_reason` when not). The verdict part: `new_failures` (the confirmed ids), `unconfirmed`, `unfinished` and `reruns` (per id: `first_run` `failed` or `unfinished`, `failed_rounds`, `result` `confirmed` or `unconfirmed`). Beside it lie `<sha>.log`, `<sha>.junit.xml` and `<sha>.tests.jsonl` (one line per collected test and per test phase, keyed by pytest node id; `scripts/tests/_test_report.py`).

## The flaky list

A test that fails once and passes twice is a flake, and no one learns of it from a gate that simply admits the landing. `scripts/lib/flake_list.py` keeps the trace in two places:

- the **journal**, machine-local: `<git common dir>/suite-gate/flaky-pending.jsonl` (the file `SUITE_GATE_FLAKY_JOURNAL` names, when set). The gate appends one line per unconfirmed id (id, candidate, tip, date; a unique entry id). Appending is best effort: a journal that cannot be written is reported and never changes the verdict. The gate never writes `scripts/tests/flaky.toml`.
- the **list**, `scripts/tests/flaky.toml`: one line `"<id>" = { count, first_seen, last_seen }` per test under `[flaky]`. Listing gives no extra treatment: a listed test that fails both reruns still blocks. The landing prints each listed entry whose `count` reached 3 (`REVIEW_COUNT`), so a person decides to fix or delete it.

`land-branch.py`, once the branch contains the remote tip and before the gate, folds the journal into the list: the entries whose ids no commit in the remote trunk carries yet become one separate commit on top of the branch, `tests: record flaky tests` (an unlisted id is added with the journal's count and first and last seen dates; a listed id's `count` is raised by the journal's count), with a `Flake-Entries:` trailer naming the folded entry ids. The suite gate then judges that commit, and it is what lands. An entry the remote trunk already carries is dropped from the journal on the next landing, so folding is idempotent. A journal that cannot be read is reported and does not hold the landing.

## Worker slots

A full run takes up to `test-slots-per-run` worker slots of the machine-wide `test-slots-total` (`scripts/lib/test_slots.py`) before pytest starts, waiting in a queue while other runs hold them, and runs with as many xdist workers as it holds; its timeout starts once it holds them. While it waits it prints the holders' pids every minute; after `test-slots-max-wait-s` (14400 s) it gives up and the run is UNAVAILABLE, naming them. pytest inherits the slot descriptors, so a gate killed mid-run leaves the slots held until its pytest tree exits. The reruns of new failures are serial and take no slot. Every other parallel `pytest` of `scripts/tests` takes slots the same way, so a gate and an interactive run share the limit. pytest runs under `scripts/cap-run.sh`. That script caps the user's machine-wide thread count at its count on entry plus a headroom, so a run started later spends from the headroom of the runs already going. For this reason a run that holds slots gets the per-run headroom (`fanout-cap-extra-tasks`, read from config) once for every full share the slot holders together can run, `ceil(test-slots-total / test-slots-per-run)`; threads grow with workers, and runs that hold no slot are not counted. `python3 scripts/lib/test_slots.py held` shows who holds which slot.

## Hermetic run

A test that reads the live repository (its refs move with every landing), the agent home or the network passes on the machine that holds them and flakes when other sessions work; the verdict then depends on them, not on the candidate. pytest therefore runs inside `scripts/lib/hermetic_run.py`: a private user, mount and network namespace (entered from inside the Python process; the command-line `unshare` is refused here) in which an empty read-only filesystem is mounted over the source repository's work tree, its git directory, the canonical checkout and the agent homes (the configured config root and both default agent directories under the real home), and only loopback is up. A read of a hidden path through a symlink, an environment variable or a relative path reads an empty directory. The sandbox clone keeps reading the source's objects through alternates, so only that objects directory is bound back into the sandbox, read-only (a bind that cannot be made read-only fails the run, it is never left writable); the source's refs and work tree stay hidden. Not hidden: the rest of the home (the user site holds pytest and xdist), and `/proc/<pid>/root` of a process outside the namespace (there is no PID namespace). The mounts exist only inside pytest's own mount namespace.

The runner exits 125 when it itself fails (namespaces refused, a mount refused, the command not startable), so the gate reports `the hermetic runner failed before pytest ran` and the run is UNAVAILABLE, not taken for pytest's own exit 2.

**Where the namespaces are unavailable the gate does not stop: it runs unhidden, and says so.** It prints `NOT HERMETIC: <reason>` on stderr when the run starts, writes `hermetic: false` and `hermetic_reason` into the record, and `suite-gate.py run` and `check` end the decision line with `[NOT HERMETIC: <reason>]` (`SUITE: ADMITTED (PASS) [NOT HERMETIC: …]`). Read such a PASS as the weaker, pre-isolation verdict: tests that read the live repository ran against it. `scripts/tests/gate_speedup_control.py hermetic-run` never accepts a non-hermetic run.

## What is exempt

A landing that changes only `.md` files under `memory-global/` or `docs/` is exempt (the memory/docs-only case): such files cannot fail a test. The decision reads git's own path list over the whole range, counts both sides of a rename and every deletion, and treats an empty change list as required. It cannot be requested. A repository without `scripts/suite-gate.py` in either the tip or the candidate tree is not gated at all.

## Landing

`land-branch.py` fetches the remote tip once and checks that the candidate contains it, folds the flaky journal (§ The flaky list), then runs the suite gate, then the smoke gate, and pushes only when both admit. The suite line (`SUITE: ADMITTED …`, `SUITE: exempt (memory/docs-only): <files>` or `SUITE: not required …`) is printed before the smoke line and the push report; a refusal or a gate error exits 2 with `NOT-LANDABLE: suite gate: …` and nothing is pushed. `--suite-waiver "<reason>"` and `--suite-timeout <seconds>` pass through to the run (default 7200 s per run, counted from when the run holds its worker slots; config key `suite-gate-timeout-s`). `--check` additionally prints a `SUITE-STATUS:` line from local state and never runs the suite.

## The hook

`githooks/pre-push` reads the pushed refs once and feeds the same lines to both gates, runs both even when the first refuses, and fails when either refused, so a refusal of one gate does not hide the other. A raw `git push` of an unrecorded candidate is refused with `pre-push: to land a change run: python3 scripts/suite-gate.py run`.

## Cost

A landing that needs the suite pays its wall-clock inside the `land-branch.py` call, plus any time queued for worker slots: one run when the candidate is clean; when it has failures, also a baseline run on the tip (skipped when that tip's baseline is cached) and two serial reruns of the failures the baseline lacks, so up to two full runs and two reruns. Callers that wrap the landing with a short timeout must expect that, or run `suite-gate.py run` first and land the recorded commit.

## Not covered

- **Anything the suite does not test.** A green suite shows no regression in what is tested, nothing more.
- **A failure that already exists on the tip.** It is absorbed by id. A candidate that makes an already-failing test fail for a new reason, or a scanner-style test that already fails on the tip and now also fails on a new violation, passes the gate.
- **Flaky tests.** A new failure that passes both reruns is admitted and journaled; a test that fails in either rerun blocks. A flake that fails only when the machine is loaded can still land, once, and only the flaky list tells anyone.
- **Races between landings.** The record is bound to the tip at run time. Two candidates admitted against the same tip can still break each other once both land; there is no merge queue, and the second landing is only re-run when the first has moved the tip.
- **Memory/docs-only landings.** They run nothing; a test that reads a markdown file's content is not exercised by such a landing.
- **A suite that cannot run here.** A missing `pytest-xdist` or a failed sandbox build is UNAVAILABLE, not a verdict on the candidate.
