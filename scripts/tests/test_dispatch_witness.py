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

from lib import dispatch_witness_snapshot  # noqa: E402
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


def _entry(status, timeout=None, *, scope_qualified=False, members_read=()):
    """One snapshot entry in the schema the writer emits.

    ``scope_qualified`` defaults to False — an absence established over the
    FULL settings scope — so that a test about timeouts, sessions or stamps
    exercises the permissive absent branch it is actually about. The tests
    that are ABOUT the qualification pass it explicitly."""
    return {
        "status": status,
        "timeout": timeout,
        "scope_qualified": scope_qualified,
        "members_read": [str(member) for member in members_read],
    }


def _snapshot(tmp_path, **overrides) -> Path:
    """All three hooks default to "was not registered before, over the full
    scope". Each test overrides only the hook it is about."""
    hooks = {basename: _entry(hook_wiring.ABSENT) for basename in mod.WITNESSED_BASENAMES}
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
    snapshot = _snapshot(tmp_path, **{ESCALATION: _entry(hook_wiring.WIRED, 5)})
    records = [_call("a", hook="escalation_diagnosis", duration=6.0)]
    assert _run(tmp_path, records, snapshot=snapshot) == 0


def test_a_call_the_old_limit_could_have_survived_is_not_a_witness(tmp_path):
    """The mirror of the test above, and the one that makes it mean anything:
    under a 5s registration a 4s call proves nothing, because the old wiring
    would have produced exactly the same line."""
    snapshot = _snapshot(tmp_path, **{ESCALATION: _entry(hook_wiring.WIRED, 5)})
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
    snapshot = _snapshot(tmp_path, **{TURN_END: _entry(hook_wiring.UNKNOWN)})
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1


def test_a_wired_hook_with_no_readable_timeout_blocks(tmp_path):
    """lib/hook_wiring treats a registration with no timeout key as UNKNOWN,
    never as fine. A snapshot carrying that shape must not become "absent" by
    virtue of its null."""
    snapshot = _snapshot(tmp_path, **{TURN_END: _entry(hook_wiring.WIRED)})
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1


def test_a_hook_missing_from_the_snapshot_blocks(tmp_path):
    hooks = {
        basename: _entry(hook_wiring.ABSENT)
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


def test_a_qualified_absence_blocks_rather_than_licensing_any_line(tmp_path):
    """The permissive absent branch spends "it was never wired" as a fact, and
    that is exactly what lib/hook_wiring cannot establish: it deliberately does
    not read project-level settings, so its ABSENT means "not in the members I
    could read". A snapshot that says so must not buy the permissive branch —
    a hook wired at project level with a small timeout would otherwise be
    witnessed by any line at all, including one the old limit could produce."""
    snapshot = _snapshot(tmp_path, **{
        DEFERRING: _entry(hook_wiring.ABSENT, scope_qualified=True,
                          members_read=["/root/settings.json"]),
    })
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1


def test_the_stage_8_configuration_is_witnessed(tmp_path, capsys):
    """The configuration stage 8 actually runs against, which no test covered:
    two hooks wired at 5s before the change, one not registered, and
    --require-all demanding that each answer for itself.

    The deferring hook's absence is recorded UNQUALIFIED here — established
    over the full settings scope. That is a real requirement on the capture
    step, not a convenience: with the qualified absence hook_wiring alone can
    supply, this same run blocks (see the test above)."""
    snapshot = _snapshot(tmp_path, **{
        ESCALATION: _entry(hook_wiring.WIRED, 5),
        TURN_END: _entry(hook_wiring.WIRED, 5),
        DEFERRING: _entry(hook_wiring.ABSENT),
    })
    records = [
        _call("a", hook="escalation_diagnosis", duration=6.0),
        _call("b", hook="turn_end", duration=6.0),
        _record("entered", "c", hook="deferring_disposition",
                judge="deferring_disposition", prefilter_fired=True),
    ]
    assert _run(tmp_path, records, snapshot=snapshot, extra=["--require-all"]) == 0
    assert "3 of 3 hooks witnessed" in capsys.readouterr().out


def test_the_stage_8_configuration_fails_when_one_wired_hook_stays_silent(tmp_path):
    """The mirror that gives the test above its meaning: --require-all must
    still fail on the real configuration when one of the two wired hooks
    produced no call outliving its old limit."""
    snapshot = _snapshot(tmp_path, **{
        ESCALATION: _entry(hook_wiring.WIRED, 5),
        TURN_END: _entry(hook_wiring.WIRED, 5),
        DEFERRING: _entry(hook_wiring.ABSENT),
    })
    records = [
        _call("a", hook="escalation_diagnosis", duration=6.0),
        _record("entered", "c", hook="deferring_disposition",
                judge="deferring_disposition", prefilter_fired=True),
    ]
    assert _run(tmp_path, records, snapshot=snapshot, extra=["--require-all"]) == 1


def test_the_snapshot_the_writer_produces_is_the_one_the_witness_reads(tmp_path):
    """The contract between the capture step and this script is a file format,
    and nothing but prose held its two ends together. Round-trip a real probe
    result through the writer into the witness: a shape either end invented on
    its own would fail here rather than in stage 8."""
    root = tmp_path / "config-root"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": f"python3 $CLAUDE_PROJECT_DIR/hooks/{ESCALATION}",
                    "timeout": 5,
                }],
            }],
        }
    }), encoding="utf-8")

    wirings = [hook_wiring.probe(basename, root) for basename in mod.WITNESSED_BASENAMES]
    assert {w.basename: w.status for w in wirings}[ESCALATION] == hook_wiring.WIRED

    snapshot = tmp_path / "written-old-wiring.json"
    document = dispatch_witness_snapshot.write_snapshot(snapshot, wirings)
    assert document["schema"] == mod.SNAPSHOT_SCHEMA
    assert document["hooks"][ESCALATION]["timeout"] == 5

    hooks, error = mod.load_snapshot(snapshot)
    assert error == "" and set(hooks) == set(mod.WITNESSED_BASENAMES)

    # The witness reads the file end to end: the escalation hook's own 6s call
    # outlives the 5s the writer recorded from the settings member...
    records = [_call("a", hook="escalation_diagnosis", duration=6.0)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1
    hooks_line = mod.judge_hook(
        ESCALATION, hooks[ESCALATION], records, CUTOFF, SESSION
    )
    assert hooks_line.witnessed and not hooks_line.blocking
    # ...and the run still refuses overall, because the two hooks the probe
    # found absent are QUALIFIED absences — what probe() can honestly report —
    # and those block in either mode.

    # The seam for a caller that HAS established full scope produces a file the
    # same witness accepts, which is the only difference between the two.
    unqualified = tmp_path / "unqualified-old-wiring.json"
    dispatch_witness_snapshot.write_snapshot(unqualified, wirings, unqualified=True)
    assert _run(tmp_path, records, snapshot=unqualified) == 0


def test_a_snapshot_entry_with_no_qualification_field_blocks(tmp_path):
    """Including every v1 snapshot ever written, which carried no scope
    information at all: there is nothing to upgrade it from, so it is refused
    rather than read as the permissive case."""
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({
        "schema": mod.SNAPSHOT_SCHEMA,
        "hooks": {b: {"status": hook_wiring.ABSENT, "timeout": None}
                  for b in mod.WITNESSED_BASENAMES},
    }), encoding="utf-8")
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=path) == 1


def test_a_snapshot_describing_a_hook_this_script_does_not_witness_blocks(tmp_path, capsys):
    """The writer emits exactly the hooks it is given, so a name outside
    WITNESSED_BASENAMES means the snapshot and this script disagree about which
    hooks are under witness. Skipping it silently is how "3 of 3 witnessed"
    gets printed for a run that never looked at a fourth hook."""
    snapshot = _snapshot(tmp_path, **{"hook-some-fourth-gate.py": _entry(hook_wiring.ABSENT)})
    records = [_record("entered", "a", hook="deferring_disposition",
                       judge="deferring_disposition", prefilter_fired=True)]
    assert _run(tmp_path, records, snapshot=snapshot) == 1
    out = capsys.readouterr().out
    assert "hook-some-fourth-gate.py" in out
    assert "3 of 3 hooks witnessed" not in out


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
