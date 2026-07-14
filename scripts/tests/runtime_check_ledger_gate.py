#!/usr/bin/env python3
"""Standalone live-demo of the claim-provenance ledger resolution gate — the
final_check the stage-5 plan calls for. No earlier stage creates it.

Drives a full reasoning-kind session CLASSIFIED..RESOLVED through the real
`cli.main` dispatch (with `--state-root` in a tempdir), the same wiring the live
harness runs, and asserts the resolution gate refuses to close in turn on each of
the THREE ledger blockers, then passes once all three are satisfied:

  1. an ungrounded claim (validate_ledger)                -> blocked
  2. the mandatory enumeration cross-check not yet run     -> blocked
  3. a raised-but-undispositioned candidate the cross-check
     produced (validate_candidates)                        -> blocked
  ... claim grounded + cross-check run + every candidate recorded/dismissed
                                                            -> RESOLVED

The enumeration pass is model perception, so `advisor.enumerate_claims` is
STUBBED to fixed lines — no live model call fires. Exits 0 only if the full
block->pass ladder holds; prints a one-line diagnosis and exits 1 otherwise.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agentctl import advisor, cli  # noqa: E402

_PLAN = _REPO_ROOT / "tests" / "fixtures" / "plan_two_stage.toml"


class _Fail(Exception):
    pass


def _run(root: str, *argv: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--state-root", root, *argv])
    out = buf.getvalue()
    try:
        return rc, json.loads(out)
    except Exception as exc:  # pragma: no cover - diagnostic
        raise _Fail(f"non-JSON output from {argv[0]}: {out!r}") from exc


def _blockers(data: dict) -> list[str]:
    return data.get("data", {}).get("blockers", [])


def _expect_blocked_on(root: str, sid: str, needle: str) -> None:
    rc, d = _run(root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
    if rc == 0:
        raise _Fail(f"expected resolve BLOCKED on {needle!r}, but it passed: {d}")
    if not any(needle in b for b in _blockers(d)):
        raise _Fail(f"expected a blocker containing {needle!r}; got {_blockers(d)}")
    print(f"PASS: resolve blocked on {needle!r}")


def _drive_to_resolution_edge(root: str, sid: str) -> None:
    _run(root, "start", "--session", sid, "--task", "t",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(root, "classify", "--session", sid, "--architectural",
         "--deliverable-kind", "reasoning")
    _run(root, "plan", "--session", sid)
    _run(root, "submit-plan", "--session", sid, "--plan", str(_PLAN))
    _run(root, "approve", "--session", sid, "--by", "user")
    _run(root, "partition", "--session", sid)
    for _ in range(2):
        _run(root, "next-stage", "--session", sid)
        _run(root, "record-result", "--session", sid, "--status", "passed",
             "--actual", "ok", "--control", "reviewed: ok")
    _run(root, "verify-final", "--session", sid)
    # experience auto-activates for every substantive session; satisfy its gate so
    # this demo isolates the ledger plugin's own three blockers
    _run(root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "searched")
    _run(root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "recorded")


def main() -> int:
    os.environ["AGENTCTL_PLAN_REVIEW"] = "0"
    os.environ["AGENTCTL_ADVISOR"] = "0"
    # self-contained temp session: drop any inherited harness id so the
    # session-mismatch warning (authorizes by CLAUDE_CODE_SESSION_ID) stays quiet
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    # STUB the model perception: no live `claude -p` call in the demo.
    advisor.enumerate_claims = lambda text, run: ["chose approach A", "load will grow 2x"]

    with tempfile.TemporaryDirectory() as tmp:
        root = str(Path(tmp) / "state")
        # keep the quality-ledger write inside the tempdir (fail-open anyway)
        cli.TASK_QUALITY_LOG = Path(tmp) / "quality.jsonl"
        sid = "rt-ledger"

        artifact = Path(tmp) / "deliverable.md"
        artifact.write_text("We chose approach A; load will grow 2x.", encoding="utf-8")

        try:
            _drive_to_resolution_edge(root, sid)

            # (1) an ungrounded axiom claim blocks
            _run(root, "ledger-add", "--session", sid, "--id", "c1",
                 "--status", "axiom", "--statement", "measured load")  # no --source
            _expect_blocked_on(root, sid, "c1")

            # ground it -> now blocked only on the un-run cross-check
            _run(root, "ledger-add", "--session", sid, "--id", "c1",
                 "--status", "axiom", "--statement", "measured load",
                 "--source", "prod metrics dashboard")
            _expect_blocked_on(root, sid, "enumeration cross-check not run")

            # (2) run the cross-check (advisor stubbed) -> raises enum-1, enum-2
            rc, d = _run(root, "ledger-enumerate", "--session", sid,
                         "--artifact", str(artifact))
            if rc != 0 or d.get("data", {}).get("raised") != ["enum-1", "enum-2"]:
                raise _Fail(f"ledger-enumerate did not raise the stubbed candidates: {d}")
            print("PASS: cross-check ran; raised enum-1, enum-2")

            # (3) raised candidates now block
            _expect_blocked_on(root, sid, "enum-1")

            # disposition both: one recorded (linked to the grounded claim), one dismissed
            _run(root, "ledger-dispose", "--session", sid, "--id", "enum-1",
                 "--as", "recorded", "--claim", "c1")
            _run(root, "ledger-dispose", "--session", sid, "--id", "enum-2",
                 "--as", "dismissed", "--reason", "restated, not load-bearing")

            # all three satisfied -> resolves
            rc, d = _run(root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
            if rc != 0 or d.get("node") != "RESOLVED":
                raise _Fail(f"expected RESOLVED once all three blockers cleared; got rc={rc} {d}")
            print("PASS: resolve passed once claim grounded, cross-check run, candidates disposed")
        except _Fail as exc:
            print(f"FAIL: {exc}")
            return 1

    print("OK: ledger resolution gate block->pass ladder holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
