# Suite gate: tests that fail in the sandbox

The pre-land suite gate runs the whole suite in a sandbox (a private clone, a fake `HOME`, a scrubbed
environment, a root under `/tmp`) and subtracts the failures the remote tip already has. A tip whose
baseline fails hundreds of tests turns that subtraction into noise: a candidate's own regression hides
among the baseline's failures. This document records why the baseline failed in the sandbox, what was
fixed, and which failures remain by design.

`scripts/tests/gate_speedup_control.py sandbox-run` runs the committed tip once the way the gate does and
writes every failing id; `verify-named` fails when a failing id is absent from this document, when this
document names more than 20 ids, or when the run did not finish (fewer than 15000 tests collected, or a
collection error). Both are listed in [scripts/README.md](../../scripts/README.md).

## Causes found, and where each was fixed

On the tip `5d52233a` the sandbox failed 240 tests. Two causes account for all of them; neither was a
defect of the code under test.

1. **No Cursor API key in the sandbox's `HOME`** (about 185 failures: the `classify`-driven tests of the
   child contour, the review triggers, the tier carry-forward and the replan boundary). The engine resolves
   its host from the Cursor credentials in `HOME`; the sandbox's fake `HOME` has none, so `classify` raised
   `HostAmbiguousError` and the tests failed at setup. Fixed in the test environment, not the sandbox: the
   autouse fixture `_cursor_key_present_by_default` in `scripts/tests/conftest.py` gives every test a
   placeholder key. The sandbox stays as the gate needs it, without credentials.
2. **`/tmp/` counts as engine scratch** (the hook-guard, scope-conflict, scope-track and canon-writer
   tests). `exempt_paths._EXEMPT_SUBSTRINGS` holds the raw substring `/tmp/`, and the sandbox, with its
   `TMPDIR`, lives under `/tmp/instruction-sandbox.*`, so every real path in these tests was exempt and the
   hooks never denied anything. Fixed in the tests: `conftest.unexempt_dir` names a path that is never
   exempt wherever the machine's temporary directory is; the hook-guard tests run the hook with the `/tmp/`
   substring removed and an explicit scratch root, and the canon-writer tests pass `AGENTCTL_SCRATCH_ROOTS`.
   Every changed test still checks the behaviour it checked before; none was skipped or deleted.

## Failures that remain by design

None: the last full run in the sandbox failed no test.

A test that cannot pass in the sandbox for a reason outside Core gets one line here, as its full pytest
node id (the repository-relative test file, then `::`, then the test name) with the reason it cannot be fixed.
