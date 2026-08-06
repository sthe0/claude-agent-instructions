"""check-dispatch-witness.py — proving a judge hook really dispatched.

The claim under test is narrow and easy to fake: "the hooks are re-registered
with a bigger timeout" is a statement about a config file. These tests pin
that the witness only ever accepts a ledger line that could NOT have been
written under the old registration, and that every uncertainty it meets —
a snapshot it cannot read, a hook whose old limit is unknown — makes it
refuse rather than relax.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "check_dispatch_witness", SCRIPTS / "check-dispatch-witness.py"
)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["check_dispatch_witness"] = mod  # dataclass string-annotation resolution needs this
_SPEC.loader.exec_module(mod)

from lib import hook_wiring  # noqa: E402
from lib import judge_latency  # noqa: E402
from lib import judge_ledger  # noqa: E402

SESSION = "sess-live"
CUTOFF = 1_000_000.0

TURN_END = "hook-turn-end-gate.py"
ESCALATION = "hook-escalation-diagnosis-gate.py"
DEFERRING = "hook-deferring-disposition-gate.py"


def _record(kind, invocation_id, *, hook, ts=CUTOFF + 10.0, source=SESSION, **fields):
    record = {
        "ts": ts,
        "kind": kind,
        "invocation_id": invocation_id,
        "hook": hook,
        "source": source,
    }
    record.update(fields)
    return record


def _call(invocation_id, *, hook, duration, **overrides):
    return _record(
        "call", invocation_id, hook=hook, judge="outage_escalation",
        timed_out=False, duration=duration, returncode=0, raised=None, **overrides
    )


def _ledger(tmp_path, records) -> Path:
    path = tmp_path / "judge-usage-ledger.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _stamp(tmp_path) -> Path:
    path = tmp_path / "since.stamp"
    path.write_text("", encoding="utf-8")
    os.utime(path, (CUTOFF, CUTOFF))
    return path


def _snapshot(tmp_path, **overrides) -> Path:
    """All three hooks default to "was not registered before". Each test
    overrides only the hook it is about."""
    hooks = {
        basename: {"status": hook_wiring.ABSENT, "timeout": None}
        for basename in mod.WITNESSED_BASENAMES
    }
    hooks.update(overrides)
    path = tmp_path / "old-wiring.json"
    path.write_text(
        json.dumps({"schema": mod.SNAPSHOT_SCHEMA, "hooks": hooks}), encoding="utf-8"
    )
    return path


def _run(tmp_path, records, *, snapshot=None, extra=(), stamp=None, session=("--session-id", SESSION)):
    ledger = _ledger(tmp_path, records)
    argv = [
        "--since-file", str(stamp if stamp is not None else _stamp(tmp_path)),
        *session,
        "--old-wiring-file", str(snapshot if snapshot is not None else _snapshot(tmp_path)),
        "--ledger", str(ledger),
        *extra,
    ]
    return mod.main(argv)


def test_the_ledger_hook_names_cover_exactly_the_judge_calling_hooks():
    """lib/judge_ledger.HOOK_NAME_BY_BASENAME and
    lib/judge_latency.HOOK_CALL_SEQUENCE describe the same three hooks under
    two different keyings. A fourth judge hook added to one and forgotten in
    the other would leave the witness unable to look up its ledger name — and
    the witness fails closed, so the whole check would start refusing."""
    assert set(judge_ledger.HOOK_NAME_BY_BASENAME) == set(judge_latency.HOOK_CALL_SEQUENCE)
    assert set(mod.WITNESSED_BASENAMES) == set(judge_ledger.HOOK_NAME_BY_BASENAME)
    assert len(set(judge_ledger.HOOK_NAME_BY_BASENAME.values())) == len(
        judge_ledger.HOOK_NAME_BY_BASENAME
    ), "two hooks mapped to the same ledger name"


def test_a_call_outliving_the_old_limit_is_a_witness(tmp_path):
    snapshot = _snapshot(tmp_path, **{ESCALATION: {"status": hook_wiring.WIRED, "timeout": 5}})
    records = [_call("a", hook="escalation_diagnosis", duration=6.0)]
    assert _run(tmp_path, records, snapshot=snapshot) == 0


def test_a_call_the_old_limit_could_have_survived_is_not_a_witness(tmp_path):
    """The mirror of the test above, and the one that makes it mean anything:
    under a 5s registration a 4s call proves nothing, because the old wiring
    would have produced exactly the same line."""
    snapshot = _snapshot(tmp_path, **{ESCALATION: {"status": hook_wiring.WIRED, "timeout": 5}})
    records = [_call("a", hook="escalation_diagnosis", duration=4.0)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1


def test_a_hook_that_was_never_registered_is_witnessed_by_merely_running(tmp_path):
    """There is no old limit to beat when the hook was not wired at all, so
    any in-window line of its own is already the new evidence."""
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records) == 0


def test_a_line_written_before_the_stamp_is_not_evidence(tmp_path):
    """A ledger line predating the re-wiring cannot vouch for the re-wiring,
    however long its call ran."""
    records = [_call("a", hook="deferring_disposition", duration=40.0, ts=CUTOFF - 1.0)]
    assert _run(tmp_path, records) == 1


def test_a_manual_run_is_not_evidence(tmp_path):
    """A hand-driven or test invocation says nothing about what the HARNESS
    dispatches — which is the only thing the old registration could kill."""
    records = [_call("a", hook="deferring_disposition", duration=40.0, source="manual")]
    assert _run(tmp_path, records) == 1


def test_a_line_from_another_session_is_not_evidence(tmp_path):
    records = [_call("a", hook="deferring_disposition", duration=40.0, source="sess-other")]
    assert _run(tmp_path, records) == 1


def test_a_line_with_no_source_at_all_is_not_evidence(tmp_path):
    record = _call("a", hook="deferring_disposition", duration=40.0)
    del record["source"]
    assert _run(tmp_path, [record]) == 1


def test_one_witnessed_hook_does_not_cover_another_hooks_silence(tmp_path):
    """Without --require-all a single witness is enough to say the wiring
    works at all; with it, every hook must answer for itself."""
    records = [_record("entered", "a", hook="turn_end", judge="feedback_signal",
                       prefilter_fired=True)]
    assert _run(tmp_path, records) == 0
    assert _run(tmp_path, records, extra=["--require-all"]) == 1


def test_a_missing_snapshot_fails_rather_than_relaxing(tmp_path):
    """The permissive branch (no old limit to beat) is reached only via an
    explicit "absent" status. A snapshot that is simply not there must not
    fall into it."""
    records = [_call("a", hook="deferring_disposition", duration=40.0)]
    assert _run(tmp_path, records, snapshot=tmp_path / "nope.json") == 1


def test_a_missing_stamp_fails_rather_than_admitting_every_line(tmp_path):
    records = [_call("a", hook="deferring_disposition", duration=40.0)]
    assert _run(tmp_path, records, stamp=tmp_path / "nope.stamp") == 1


def test_a_snapshot_of_the_wrong_schema_fails(tmp_path):
    path = tmp_path / "old-wiring.json"
    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    records = [_call("a", hook="deferring_disposition", duration=40.0)]
    assert _run(tmp_path, records, snapshot=path) == 1


def test_an_undetermined_old_registration_blocks_even_when_another_hook_passes(tmp_path):
    """"unknown" is not "absent". A hook whose old registration could not be
    read makes the whole run refuse, in either mode — the silence of a wired
    hook is data, but an unknown is not."""
    snapshot = _snapshot(tmp_path, **{TURN_END: {"status": hook_wiring.UNKNOWN, "timeout": None}})
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1


def test_a_wired_hook_with_no_readable_timeout_blocks(tmp_path):
    """lib/hook_wiring treats a registration with no timeout key as UNKNOWN,
    never as fine. A snapshot carrying that shape must not become "absent" by
    virtue of its null."""
    snapshot = _snapshot(tmp_path, **{TURN_END: {"status": hook_wiring.WIRED, "timeout": None}})
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1


def test_a_hook_missing_from_the_snapshot_blocks(tmp_path):
    hooks = {
        basename: {"status": hook_wiring.ABSENT, "timeout": None}
        for basename in mod.WITNESSED_BASENAMES
        if basename != DEFERRING
    }
    path = tmp_path / "old-wiring.json"
    path.write_text(
        json.dumps({"schema": mod.SNAPSHOT_SCHEMA, "hooks": hooks}), encoding="utf-8"
    )
    records = [_record("entered", "a", hook="turn_end", judge="feedback_signal",
                       prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=path) == 1


def test_an_empty_ledger_is_not_a_witness(tmp_path):
    assert _run(tmp_path, []) == 1


def test_the_env_variable_this_script_reads_is_the_one_the_harness_writes():
    """A near-miss spelling of this name was a real defect once
    (tests/test_spawn_specialist_lineage.py records that the pre-fix code read
    a non-existent variable and silently logged null). Pin it against a caller
    that is known to work rather than against a literal typed twice."""
    source = (SCRIPTS / "spawn-specialist.py").read_text(encoding="utf-8")
    assert f'"{mod.SESSION_ID_ENV}"' in source


def test_session_from_env_takes_the_id_from_the_environment(tmp_path, monkeypatch):
    """The caller that matters — a verify command running inside the session
    being witnessed — cannot type an id it does not know."""
    monkeypatch.setenv(mod.SESSION_ID_ENV, SESSION)
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, session=("--session-from-env",)) == 0


def test_session_from_env_still_filters_on_that_id(tmp_path, monkeypatch):
    """The env route must be the same filter, not a way around it: a line from
    a different session stays inadmissible however the id was supplied."""
    monkeypatch.setenv(mod.SESSION_ID_ENV, "sess-other")
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, session=("--session-from-env",)) == 1


def test_a_blank_env_variable_fails_rather_than_matching_everything(
    tmp_path, monkeypatch, capsys
):
    """An unset id is an UNDETERMINED filter, and the exit code alone cannot
    tell that apart from an ordinary silent run: an id of "" matches no record
    either, so a witness that quietly dropped the guard would also exit 1
    here — and would then accept every session's lines the moment one arrived.
    What pins the guard is that the run says WHY it refused."""
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    for value in ("", "   "):
        monkeypatch.setenv(mod.SESSION_ID_ENV, value)
        assert _run(tmp_path, records, session=("--session-from-env",)) == 1
        out = capsys.readouterr().out
        assert mod.SESSION_ID_ENV in out and "cannot be evaluated" in out
    monkeypatch.delenv(mod.SESSION_ID_ENV, raising=False)
    assert _run(tmp_path, records, session=("--session-from-env",)) == 1
    assert mod.SESSION_ID_ENV in capsys.readouterr().out


def test_the_session_id_must_come_from_exactly_one_of_the_two_routes(tmp_path):
    """argparse enforces the choice, so neither a silent default nor a
    conflict between the two can reach the filter."""
    for session in ((), ("--session-id", SESSION, "--session-from-env")):
        with pytest.raises(SystemExit) as excinfo:
            _run(tmp_path, [], session=session)
        assert excinfo.value.code == 2


def test_the_verify_command_the_plan_will_run_parses(tmp_path, monkeypatch):
    """The flag set is not this script's to choose: stage 8 runs a fixed
    command line, and a witness that rejects it witnesses nothing."""
    monkeypatch.setenv(mod.SESSION_ID_ENV, SESSION)
    args = mod.build_parser().parse_args([
        "--session-from-env",
        "--since-file", str(_stamp(tmp_path)),
        "--old-wiring-file", str(_snapshot(tmp_path)),
        "--require-all",
    ])
    assert mod.resolve_session_id(args) == (SESSION, "")
    assert args.require_all is True
