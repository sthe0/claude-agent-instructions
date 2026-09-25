"""`check-spawn-tool-run.py` answers a narrower question than the engine's own
`agentctl/cli.py::_classify_transcript_denials`: not "was this denial covered by
the stage's grants" but "did THIS command, at THIS point in the transcript, get
blocked (and by what)". These tests exercise both the resolver (ambiguity,
`--after`/`--before`/`--anchor-stopped` narrowing) and the two outcome checks
(`--expect-blocked`, `--expect-guard-block --guard-log`), plus `--list-denied`'s
two sources (a direct transcript, and a kind's recent spawn-costs ledger rows).

Reuses the stage-1 synthetic fixtures under `fixtures/transcript_stops/` (no real
machine path, email or UUID — see `test_transcript_stops.py`'s module docstring)
plus one new fixture, `ordering-and-repeats.jsonl`, built the same way, whose
sole purpose is a command substring that occurs twice so `--after`/`--before`
have something real to disambiguate.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_spawn_tool_run", ROOT / "scripts" / "check-spawn-tool-run.py"
)
check_spawn_tool_run = importlib.util.module_from_spec(_SPEC)
sys.modules["check_spawn_tool_run"] = check_spawn_tool_run
_SPEC.loader.exec_module(check_spawn_tool_run)

main = check_spawn_tool_run.main
build_parser = check_spawn_tool_run.build_parser

_FIXTURES = Path(__file__).parent / "fixtures" / "transcript_stops"
_ORDERING = _FIXTURES / "ordering-and-repeats.jsonl"
_TWO_DENIALS = _FIXTURES / "two-permission-denials.jsonl"
_HOOK_BLOCK = _FIXTURES / "hook-block.jsonl"
_RAN = _FIXTURES / "ran.jsonl"
_PERMISSION_DENIAL = _FIXTURES / "permission-denial.jsonl"


def _args(**overrides):
    """A Namespace with every flag defaulted, as argparse itself would produce."""
    ns = build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


# ---------------------------------------------------------------------------
# resolve_target: ambiguity and --after/--before/--anchor-stopped narrowing
# ---------------------------------------------------------------------------


def test_an_unambiguous_command_resolves_with_no_anchors():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(uses, _args(command_contains="curl"))
    assert err is None
    assert target.tool_use_id == "toolu_fixture_order_3"


def test_a_repeated_command_is_ambiguous_without_an_anchor():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(uses, _args(command_contains="echo start"))
    assert target is None
    assert "ambiguous" in err
    assert "2" in err


def test_after_narrows_a_repeated_command_to_the_later_occurrence():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(
        uses, _args(command_contains="echo start", after="curl")
    )
    assert err is None
    assert target.tool_use_id == "toolu_fixture_order_4"


def test_before_narrows_a_repeated_command_to_the_earlier_occurrence():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(
        uses, _args(command_contains="echo start", before="curl")
    )
    assert err is None
    assert target.tool_use_id == "toolu_fixture_order_2"


def test_anchor_stopped_succeeds_when_the_anchor_was_actually_denied():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(
        uses, _args(command_contains="echo start", after="curl", anchor_stopped=True)
    )
    assert err is None
    assert target.tool_use_id == "toolu_fixture_order_4"


def test_anchor_stopped_fails_when_the_anchor_merely_ran():
    """The distinguishing case `--anchor-stopped` exists for: proving ordering
    relative to an ACTUAL stop, not just relative to an attempt that happened to
    succeed."""
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(
        uses, _args(command_contains="echo end", after="ls", anchor_stopped=True)
    )
    assert target is None
    assert "was not stopped" in err


def test_an_ambiguous_anchor_itself_is_reported_as_such():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(
        uses, _args(command_contains="echo end", after="echo start")
    )
    assert target is None
    assert "ambiguous anchor" in err


def test_no_match_at_all_is_a_clear_error_not_an_index_crash():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_ORDERING)
    target, err = check_spawn_tool_run.resolve_target(uses, _args(command_contains="nonexistent"))
    assert target is None
    assert "no Bash tool_use found" in err


# ---------------------------------------------------------------------------
# check_outcome: --expect-blocked / --expect-guard-block
# ---------------------------------------------------------------------------


def test_expect_blocked_passes_for_a_permission_denial():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_PERMISSION_DENIAL)
    [target] = uses
    err = check_spawn_tool_run.check_outcome(target, _args(expect_blocked=True))
    assert err is None


def test_expect_blocked_fails_for_a_call_that_ran():
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_RAN)
    [target] = uses
    err = check_spawn_tool_run.check_outcome(target, _args(expect_blocked=True))
    assert "to be blocked" in err


def test_expect_guard_block_passes_when_the_guard_log_records_the_command(tmp_path):
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_HOOK_BLOCK)
    [target] = uses
    guard_log = tmp_path / "guard.log"
    guard_log.write_text(f"blocked: {target.command}\n", encoding="utf-8")
    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log)
    )
    assert err is None


def test_expect_guard_block_fails_on_a_missing_guard_log(tmp_path):
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_HOOK_BLOCK)
    [target] = uses
    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=tmp_path / "absent.log")
    )
    assert "does not exist" in err


def test_expect_guard_block_fails_when_the_log_omits_the_command(tmp_path):
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_HOOK_BLOCK)
    [target] = uses
    guard_log = tmp_path / "guard.log"
    guard_log.write_text("some other unrelated line\n", encoding="utf-8")
    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log)
    )
    assert "does not record" in err


def test_expect_guard_block_fails_on_a_permission_denial_not_a_hook_block(tmp_path):
    """The claim is specifically "a PreToolUse guard blocked this", not "this was
    denied by some mechanism or other" -- a permission-rule denial must not
    satisfy it even with a guard-log that happens to mention the command."""
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_PERMISSION_DENIAL)
    [target] = uses
    guard_log = tmp_path / "guard.log"
    guard_log.write_text(f"{target.command}\n", encoding="utf-8")
    err = check_spawn_tool_run.check_outcome(
        target, _args(expect_guard_block=True, guard_log=guard_log)
    )
    assert "hook-guard block" in err


# ---------------------------------------------------------------------------
# --list-denied
# ---------------------------------------------------------------------------


def test_list_denied_over_a_direct_transcript_finds_both_denials(capsys):
    uses = check_spawn_tool_run.transcript_stops.parse_bash_tool_uses(_TWO_DENIALS)
    count = check_spawn_tool_run.list_denied([("t", uses)])
    assert count == 2


def test_list_denied_over_a_ledger_kind_window(tmp_path, monkeypatch, capsys):
    import datetime as dt

    monkeypatch.setattr(check_spawn_tool_run, "COST_LOG", tmp_path / "ledger.jsonl")
    now = dt.datetime.now(dt.timezone.utc)
    rows = [
        {  # inside the window, matching kind, transcript has 2 denials
            "event": "spawn", "kind": "developer",
            "ts": now.isoformat(timespec="seconds"),
            "transcript_path": str(_TWO_DENIALS),
        },
        {  # outside the window -- must be excluded
            "event": "spawn", "kind": "developer",
            "ts": (now - dt.timedelta(days=30)).isoformat(timespec="seconds"),
            "transcript_path": str(_HOOK_BLOCK),
        },
        {  # inside the window, wrong kind -- must be excluded
            "event": "spawn", "kind": "thinker",
            "ts": now.isoformat(timespec="seconds"),
            "transcript_path": str(_HOOK_BLOCK),
        },
    ]
    check_spawn_tool_run.COST_LOG.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    rc = main([
        "--list-denied", "--kind", "developer", "--since", "14d",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 denied Bash call(s) listed" in out


# ---------------------------------------------------------------------------
# main(): CLI plumbing, exit codes, usage errors
# ---------------------------------------------------------------------------


def test_main_ok_exit_for_an_unambiguous_blocked_command(capsys):
    rc = main([
        "--transcript", str(_TWO_DENIALS),
        "--command-contains", "curl", "--expect-blocked",
    ])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_main_fails_a_missing_transcript(capsys):
    rc = main([
        "--transcript", str(_FIXTURES / "does-not-exist.jsonl"),
        "--command-contains", "curl",
    ])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_main_requires_guard_log_with_expect_guard_block(capsys):
    rc = main([
        "--transcript", str(_HOOK_BLOCK), "--command-contains", "find",
        "--expect-guard-block",
    ])
    assert rc == 2
    assert "requires --guard-log" in capsys.readouterr().err


def test_main_requires_after_with_anchor_stopped(capsys):
    rc = main([
        "--transcript", str(_ORDERING), "--command-contains", "echo start",
        "--anchor-stopped",
    ])
    assert rc == 2
    assert "requires --after" in capsys.readouterr().err


def test_main_rejects_since_in_the_wrong_shape(capsys):
    rc = main(["--list-denied", "--kind", "developer", "--since", "2weeks"])
    assert rc == 2
    assert "14d" in capsys.readouterr().err


def test_main_rejects_list_denied_combined_with_command_contains(capsys):
    rc = main([
        "--list-denied", "--transcript", str(_TWO_DENIALS),
        "--command-contains", "curl",
    ])
    assert rc == 2
    assert "does not combine" in capsys.readouterr().err


def test_main_rejects_list_denied_with_both_transcript_and_kind(capsys):
    rc = main([
        "--list-denied", "--transcript", str(_TWO_DENIALS),
        "--kind", "developer", "--since", "14d",
    ])
    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_main_rejects_list_denied_with_neither_transcript_nor_kind(capsys):
    rc = main(["--list-denied"])
    assert rc == 2
    assert "requires --transcript or --kind" in capsys.readouterr().err


def test_main_mutually_exclusive_expect_flags_rejected_by_argparse():
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "--transcript", "x", "--command-contains", "y",
            "--expect-blocked", "--expect-guard-block", "--guard-log", "z",
        ])
