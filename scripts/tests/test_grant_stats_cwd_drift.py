"""Coverage for R6 (#267b): `_classify_transcript_denials`'s `cwd_drift` class.

A Bash denial whose raw command text is NOT covered by a stage's effective
grants may still be a planning artifact rather than a genuine miss: the child
ran it from a subdirectory of the stage venue (a `cd`), and the SAME command,
rebased back to the venue root, WOULD be covered under the same literal-text
matcher `grant_covers_call` already uses. That rebased-and-covered case goes to
a new `SessionState.cwd_drift` ledger instead of `planning_misses` — it never
reaches `asked_user`/live promotion, and a rescan never reclassifies it.
Everything else (an ungranted command even after rebasing, a file-tool denial,
no cwd, cwd already at the venue, or already-covered raw text) is unaffected.

Deliberately imports every not-yet-existing-on-base-9c9ba00 symbol
(`ToolUse.cwd`/`BashToolUse.cwd`, `SessionState.cwd_drift`) INSIDE the test
function bodies, never at module level, and only AFTER an assertion that is
designed to fail by `AssertionError` (not `ImportError`/collection error) on
that base — see `test_drift_bash_covered_at_venue`."""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import SessionState


def ns(**kw):
    return Namespace(**kw)


def _to_executing(store, sid, fixtures_dir, plan_path=None):
    plan = plan_path or str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def _write_plan_with_grants_block(fixtures_dir, tmp_path, grants_toml: str, name: str) -> str:
    text = (fixtures_dir / "plan_two_stage.toml").read_text()
    text = text.replace(
        'output_artifacts = ["mod.py"]\n',
        'output_artifacts = ["mod.py"]\n\n' + grants_toml,
        1,
    )
    out = tmp_path / name
    out.write_text(text)
    return str(out)


def _venue_session(store, fixtures_dir, tmp_path, sid, grants_toml=None):
    """Drive `sid` to EXECUTING stage 1, then pin `state.repo_root` to a fresh
    tmp_path venue directory so `resolve_check_venue` returns a deterministic,
    comparable path both for the transcripts this module writes and for
    `cli._effective_stage_grants`'s declared-grant read."""
    plan_path = None
    if grants_toml is not None:
        plan_path = _write_plan_with_grants_block(
            fixtures_dir, tmp_path, grants_toml, f"plan-{sid}.toml",
        )
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    venue = tmp_path / f"venue-{sid}"
    venue.mkdir()
    state = store.load(sid)
    state.repo_root = str(venue)
    store.save(state)
    return venue


def _write_denial_transcript(path: Path, calls: list[dict]) -> None:
    """A synthetic transcript JSONL: one assistant tool_use + one user
    tool_result (permission-denial) line pair per entry in `calls`. Each
    `call` dict: tool_use_id, tool_name (default "Bash"), command or
    file_path, and an optional cwd (top-level "cwd" on the assistant line,
    per `transcript_stops.py`'s docstring — omitted entirely when absent, so
    the parser's `entry.get("cwd") or ""` default is exercised)."""
    lines = []
    for call in calls:
        tool_use_id = call["tool_use_id"]
        tool_name = call.get("tool_name", "Bash")
        if tool_name == "Bash":
            tool_input = {"command": call["command"]}
            default_text = f"Permission to use Bash with command {call['command']} has been denied."
        else:
            tool_input = {"file_path": call["file_path"]}
            default_text = f"Permission to use {tool_name} with file {call['file_path']} has been denied."
        assistant = {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input},
            ]},
        }
        if "cwd" in call and call["cwd"] is not None:
            assistant["cwd"] = call["cwd"]
        lines.append(json.dumps(assistant))
        lines.append(json.dumps({
            "type": "user",
            "toolDenialKind": "permission-rule",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": tool_use_id,
                "is_error": True, "content": call.get("text", default_text),
            }]},
        }))
    path.write_text("\n".join(lines) + "\n")


def test_drift_bash_covered_at_venue(store, fixtures_dir, tmp_path):
    """The core R6 case: `python3 widget.py --flag` denied at a cwd one level
    under the venue (`<venue>/scripts`), with `<venue>/scripts/widget.py`
    actually present, and `Bash(python3 scripts/widget.py --flag:*)` declared
    — the venue-relative spelling of the same call. Rebasing the drifted
    command back to the venue produces exactly that granted text.

    The FIRST assertion (not in planning_misses) is deliberately the one that
    must fail on base commit 9c9ba00, where this whole branch does not exist
    and the row can only ever land in planning_misses — a real behavioural
    negative control, not an import-time one. Only after that assertion would
    already have failed does the test go on to touch `state.cwd_drift`, a
    symbol absent on base."""
    sid = "drift-covered-at-venue"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-covered-at-venue.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_1",
        "command": "python3 widget.py --flag",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert "toolu_drift_1" not in {m.get("tool_use_id") for m in state.planning_misses}

    assert len(state.cwd_drift) == 1
    row = state.cwd_drift[0]
    assert row["tool_use_id"] == "toolu_drift_1"
    assert row["cwd"] == str(venue / "scripts")
    assert row["rebased_command"] == "python3 scripts/widget.py --flag"
    assert not state.planning_misses
    assert not state.materialization_defects


def test_drift_bash_ungranted_stays_miss(store, fixtures_dir, tmp_path):
    """`other.py` exists under the drifted cwd (so rebasing DOES succeed and
    produce `python3 scripts/other.py --flag`) but no grant names it -- the
    rebased-but-still-uncovered command stays a planning_miss, never
    cwd_drift."""
    sid = "drift-ungranted-stays-miss"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "other.py").write_text("# other\n")

    transcript = tmp_path / "t-ungranted.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_2",
        "command": "python3 other.py --flag",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["tool_use_id"] == "toolu_drift_2"


def test_edit_denial_never_drift(store, fixtures_dir, tmp_path):
    """A file-tool (Edit) denial is judged on its absolute `file_path`, which
    a drifted cwd never changes -- it must stay a planning_miss even at a
    drifted cwd, never routed through the Bash-only drift branch."""
    sid = "drift-edit-never"
    venue = _venue_session(store, fixtures_dir, tmp_path, sid)
    (venue / "scripts").mkdir()

    transcript = tmp_path / "t-edit.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_3",
        "tool_name": "Edit",
        "file_path": str(venue / "scripts" / "widget.py"),
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["tool_use_id"] == "toolu_drift_3"


def test_uncovered_at_venue_stays_miss(store, fixtures_dir, tmp_path):
    """cwd equal to the venue itself (no drift at all) -- an uncovered
    command stays a planning_miss; the drift branch never fires because cwd
    doesn't differ from the venue. Characterization test: `other.py` is
    never created, so the command is uncovered (and thus a planning_miss)
    regardless of whether the cwd-equals-venue guard runs at all -- it pins
    the outcome rather than discriminating the guard's own removal."""
    sid = "drift-at-venue-stays-miss"
    venue = _venue_session(store, fixtures_dir, tmp_path, sid)

    transcript = tmp_path / "t-at-venue.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_4",
        "command": "python3 scripts/other.py --flag",
        "cwd": str(venue),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["tool_use_id"] == "toolu_drift_4"


def test_covered_drifted_stays_materialization_defect(store, fixtures_dir, tmp_path):
    """A command already covered at its RAW text (venue-relative spelling)
    stays a materialization_defect whatever its cwd -- the raw-text coverage
    check wins before the drift branch is ever consulted."""
    sid = "drift-covered-raw-stays-defect"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()

    transcript = tmp_path / "t-covered-raw.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_5",
        "command": "python3 scripts/widget.py --flag",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert not state.planning_misses
    assert len(state.materialization_defects) == 1
    assert state.materialization_defects[0]["tool_use_id"] == "toolu_drift_5"


def test_no_cwd_stays_miss(store, fixtures_dir, tmp_path):
    """No `cwd` field on the assistant line at all (parses to "" per
    `transcript_stops.py`) -- the drift branch's "cwd is non-empty"
    precondition guards this; stays a planning_miss. Characterization test:
    the raw command text (`python3 widget.py --flag`, no cwd to rebase from)
    is uncovered either way, so it pins the outcome rather than
    discriminating the empty-cwd guard's own removal."""
    sid = "drift-no-cwd-stays-miss"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-no-cwd.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_6",
        "command": "python3 widget.py --flag",
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["tool_use_id"] == "toolu_drift_6"


def test_rescan_does_not_reclassify_drift(store, fixtures_dir, tmp_path):
    """A second scan of the identical transcript against the identical state
    leaves every ledger unchanged -- the cwd_drift row's tool_use_id joins
    `known_ids` exactly like the other two ledgers do."""
    sid = "drift-rescan-idempotent"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-rescan.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_7",
        "command": "python3 widget.py --flag",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))
    after_first = list(state.cwd_drift)
    assert len(after_first) == 1

    cli._classify_transcript_denials(state, stage, coverage, str(transcript))
    assert state.cwd_drift == after_first
    assert not state.planning_misses
    assert not state.materialization_defects


def test_live_promotion_skips_drift(store, fixtures_dir, tmp_path):
    """The live PERMISSION-REQUEST promotion path in `cmd_dispatch` never
    touches a `cwd_drift` row -- it only ever scans `state.planning_misses`.
    The dispatched child's transcript records a drift-coverable denial; its
    self-reported `Rule:` line names the SAME raw (undrifted) command text,
    which the effective grants do NOT cover (only the rebased spelling is
    granted), so the self-report can't be trusted as covered either. The
    result: the cwd_drift row is untouched, and a FRESH planning_misses row
    (with no tool_use_id, per the self-reported append shape) is appended --
    never a promotion of the cwd_drift row, never a duplicate for the same
    tool_use_id."""
    sid = "drift-live-promotion-skips"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-live-promotion.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_8",
        "command": "python3 widget.py --flag",
        "cwd": str(venue / "scripts"),
    }])

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to run the widget\n"
                "Rule: Bash(python3 widget.py --flag:*)\n"
            ),
            stderr=f"spawn-specialist: transcript={transcript}\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)

    assert len(state.cwd_drift) == 1
    assert state.cwd_drift[0]["tool_use_id"] == "toolu_drift_8"
    assert not state.materialization_defects
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0].get("tool_use_id") is None
    assert state.planning_misses[0]["asked_user"] is True
    assert state.planning_misses[0]["source"] == "permission-request"


def test_grant_stats_reports_cwd_drift_count(store, fixtures_dir, tmp_path):
    sid = "drift-grant-stats-count"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py --flag:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-grant-stats.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_9",
        "command": "python3 widget.py --flag",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))
    store.save(state)

    directive = cli.cmd_grant_stats(ns(session=sid, json=True), store=store)
    assert len(directive.data["cwd_drift"]) == 1
    assert directive.data["cwd_drift"][0]["tool_use_id"] == "toolu_drift_9"

    text_directive = cli.cmd_grant_stats(ns(session=sid, json=False), store=store)
    assert "cwd_drift: 1" in text_directive.detail


def test_legacy_state_dict_without_cwd_drift_loads():
    """A persisted state whose JSON predates schema 39 (no `cwd_drift` key at
    all) must still load, with `cwd_drift` defaulting to []."""
    state = SessionState(session_id="legacy-cwd-drift", task_id="t")
    data = state.to_dict()
    data.pop("cwd_drift", None)
    restored = SessionState.from_dict(data)
    assert restored.cwd_drift == []


def test_drift_compound_command_with_ungranted_segment_stays_miss(store, fixtures_dir, tmp_path):
    """Round-4 review finding 1 (blocking): a compound command whose FIRST
    segment rebases-and-covers must not let a later, genuinely-ungranted
    segment escape as `cwd_drift`. `python3 widget.py && rm -rf other` is
    denied at `<venue>/scripts`; the first segment alone would rebase to the
    granted `python3 scripts/widget.py`, but the second names no grant at
    all. The whole call must stay a `planning_miss` -- `top_level_segment_count`
    refuses to rebase anything but a single top-level statement."""
    sid = "drift-compound-stays-miss"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-compound.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_compound",
        "command": "python3 widget.py && rm -rf other",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["tool_use_id"] == "toolu_drift_compound"


def test_rebase_leaves_absolute_path_word_untouched(store, fixtures_dir, tmp_path):
    """Round-4 review finding 2 (should-fix): an absolute-path word that
    happens to resolve under the drifted cwd must NOT be rewritten to its
    venue-relative spelling -- the cause of the denial is the absolute
    spelling, not the cwd, and a venue-launched child typing the same
    absolute text would still be denied. Only the venue-relative spelling is
    granted here, so the absolute-spelled call must stay a `planning_miss`."""
    sid = "drift-absolute-path-untouched"
    venue = _venue_session(
        store, fixtures_dir, tmp_path, sid,
        grants_toml='[stage.grants]\nallow = ["Bash(python3 scripts/widget.py:*)"]\n',
    )
    (venue / "scripts").mkdir()
    (venue / "scripts" / "widget.py").write_text("# widget\n")

    transcript = tmp_path / "t-absolute.jsonl"
    _write_denial_transcript(transcript, [{
        "tool_use_id": "toolu_drift_abs",
        "command": f"python3 {venue / 'scripts' / 'widget.py'}",
        "cwd": str(venue / "scripts"),
    }])

    state = store.load(sid)
    stage = state.active_stage()
    coverage = cli._effective_stage_grants(state, stage.index)
    cli._classify_transcript_denials(state, stage, coverage, str(transcript))

    assert not state.cwd_drift
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["tool_use_id"] == "toolu_drift_abs"


def test_rebase_preserves_quoting_of_untouched_word(tmp_path):
    """Round-4 review finding 3 (should-fix): `_rebase_bash_command_to_venue`
    edits only the words it actually changes, in place, so an untouched
    word's original quoting survives byte-for-byte -- unlike the prior
    `shlex.split`/`shlex.join` round trip, which re-quotes every word by its
    own minimal-quoting heuristic and drops a literal quote a derived grant
    still expects. `grep -q 'cwd_drift' README.md` from `<venue>/scripts`
    must rebase to `grep -q 'cwd_drift' scripts/README.md` -- the quoted
    search word untouched, only the path word rewritten."""
    from agentctl import cli as _cli

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "README.md").write_text("cwd_drift\n")

    rebased = _cli._rebase_bash_command_to_venue(
        "grep -q 'cwd_drift' README.md", str(scripts_dir), str(tmp_path),
    )
    assert rebased == "grep -q 'cwd_drift' scripts/README.md"


def test_rebase_resolves_symlinked_venue_before_comparing(tmp_path):
    """Round-4 review finding 4 (should-fix): the transcript's recorded `cwd`
    and the stage `venue` are resolved via `os.path.realpath` before the
    containment check, so a symlink hop between the two spellings of the
    same directory doesn't make a genuine drift look like it escapes the
    venue. `real_dir` is the actual venue; `sym_dir` is a symlink to it, the
    spelling `state.repo_root` would carry when the plan names the venue via
    the link. The transcript records `real_dir/scripts` as `cwd` -- the
    resolved spelling a harness can produce (finding 4's own example is
    `/private/tmp/...` vs `/tmp/...` on macOS).

    Without realpath, `relpath(real_dir/scripts/widget.py, sym_dir)` walks
    off through `..` (the two spellings share no path components), so the
    call incorrectly stays a `planning_miss`. With realpath, both resolve to
    `real_dir` and the rebase produces the venue-relative text a grant there
    covers."""
    from agentctl import cli as _cli

    real_dir = tmp_path / "real-venue"
    (real_dir / "scripts").mkdir(parents=True)
    (real_dir / "scripts" / "widget.py").write_text("# widget\n")
    sym_dir = tmp_path / "sym-venue"
    sym_dir.symlink_to(real_dir)

    rebased = _cli._rebase_bash_command_to_venue(
        "python3 widget.py", str(real_dir / "scripts"), str(sym_dir),
    )
    assert rebased == "python3 scripts/widget.py"
