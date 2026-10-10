"""Stage 9 ("PR-A2: grant materialization reads the plan the user last approved
and fails loudly") coverage: a refinement/no_change replan must refresh the
approved-plan snapshot + `approved_grants_sha256` in place, a read failure from
`_stage_grant_entries` must be reported rather than silently swallowed into
empty grants, `cmd_dispatch` must refuse a stage that declares grants it cannot
currently read, and the markdown-decorated PERMISSION-REQUEST `Rule:` line must
parse. Also pins the pre-existing (not newly added) invariant that a refinement
replan attempting to GROW derived grants still classifies as `substantive`.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import diff_plans, grants_sha256, load_plan
from agentctl.state import CheckVenue
from lib import kind_baselines
from test_stage_grants import _to_executing, _write_plan_with_grants_block, ns

_SPAWN_SPECIALIST_SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_specialist():
    spec = importlib.util.spec_from_file_location(
        "spawn_specialist_grant_snapshot_refresh", _SPAWN_SPECIALIST_SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPAWN_SPECIALIST = _load_spawn_specialist()


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    """This module exercises bare refinement/no_change replans directly (no prior
    diff presentation) -- same rationale as test_replan.py's identical, module-wide
    fixture of this name; the gate itself is covered in test_replan_authorization.py."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _write_grants_plan(fixtures_dir, tmp_path, grants_toml: str, name: str) -> str:
    return _write_plan_with_grants_block(fixtures_dir, tmp_path, grants_toml, name)


def _write_plan_with_repo_root(fixtures_dir, tmp_path, repo_root: str, name: str) -> str:
    """`plan_two_stage.toml` with `[meta].repo_root` set to a directory OTHER than
    this repo's own `SCRIPTS_DIR` parent -- the base fixture declares neither
    `repo_root` nor `delivery_worktree`, so `_venue_for` falls back to `"."`,
    which under `python3 -m pytest` run from the repo root resolves to the SAME
    tree `SCRIPTS_DIR` already points at and `baseline_for_workdir` correctly
    no-ops on. A distinct `repo_root` is what actually exercises the
    workdir-absolute expansion (finding S9 / 3b)."""
    text = (fixtures_dir / "plan_two_stage.toml").read_text()
    text = text.replace(
        'criterion_type = "measurable"\n',
        f'criterion_type = "measurable"\nrepo_root = {json.dumps(repo_root)}\n',
        1,
    )
    out = tmp_path / name
    out.write_text(text)
    return str(out)


def _write_transcript_stop(tmp_path, name: str, tool_use_id: str, command: str) -> str:
    """A minimal one-stop `transcript_stops`-format jsonl fixture for a single
    Bash permission-denial, for a command whose path depends on a per-test
    `tmp_path` (so it can't be a static fixture file under
    `scripts/tests/fixtures/transcript_stops/`)."""
    out = tmp_path / name
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}},
            ]},
        }),
        json.dumps({
            "type": "user",
            "message": {"content": [
                {
                    "type": "tool_result", "tool_use_id": tool_use_id,
                    "content": f"Permission to use Bash with command {command} has been denied.",
                    "is_error": True,
                },
            ]},
            "toolDenialKind": "permission-rule",
        }),
    ]
    out.write_text("\n".join(lines) + "\n")
    return str(out)


# --- (1) no_change/refinement replans refresh the approved-plan snapshot ---


def test_no_change_replan_refreshes_stale_snapshot_to_new_bytes(store, fixtures_dir, tmp_path):
    """A byte-different but semantically `no_change` replan (a comment-only edit,
    which parses identically so `diff_plans` returns 'no_change') must still
    re-snapshot to the NEW bytes. Before the fix, the no_change branch only
    backfilled a snapshot when NONE existed yet -- an already-snapshotted session
    kept pointing at the OLD approved bytes forever, so a later `_stage_grant_
    entries` read of `state.plan_snapshot_path` would report stale content (or, if
    the underlying file were ever pruned/moved, fail outright)."""
    sid = "no-change-refresh"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan)

    state = store.load(sid)
    old_snapshot_path = state.plan_snapshot_path
    old_snapshot_hash = state.plan_snapshot_hash
    assert old_snapshot_path and old_snapshot_hash

    commented = tmp_path / "plan_two_stage_commented.toml"
    commented.write_text((fixtures_dir / "plan_two_stage.toml").read_text() + "\n# a trailing comment\n")
    new_bytes_hash = hashlib.sha256(commented.read_bytes()).hexdigest()
    assert new_bytes_hash != old_snapshot_hash  # genuinely different bytes

    d = cli.cmd_replan(ns(session=sid, plan=str(commented)), store=store)
    assert d.action == "continue"

    state = store.load(sid)
    assert diff_plans(load_plan(plan), load_plan(str(commented))) == "no_change"
    assert state.plan_snapshot_hash == new_bytes_hash
    assert state.plan_snapshot_path != old_snapshot_path
    assert Path(state.plan_snapshot_path).read_bytes() == commented.read_bytes()


def test_refinement_replan_refreshes_snapshot_and_approved_grants_hash(store, fixtures_dir):
    """The refinement branch had NO refresh logic at all before the fix -- a prose
    correction (title/expected_result_image) left `plan_snapshot_path`/`hash` and
    `approved_grants_sha256` pointed at the plan this session originally
    approved, forever, no matter how many further refinements landed."""
    sid = "refinement-refresh"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan)

    state = store.load(sid)
    old_snapshot_path = state.plan_snapshot_path
    old_grants_hash = state.approved_grants_sha256

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.action == "continue"

    state = store.load(sid)
    expected_hash = hashlib.sha256(Path(refined).read_bytes()).hexdigest()
    assert state.plan_snapshot_hash == expected_hash
    assert state.plan_snapshot_path != old_snapshot_path
    assert Path(state.plan_snapshot_path).read_bytes() == Path(refined).read_bytes()
    assert state.approved_grants_sha256 == grants_sha256(load_plan(refined))
    # The refined plan declares no grants at all, same as the base fixture, so the
    # hash is unchanged in VALUE here -- the point is that it was actively
    # RECOMPUTED against the new doc, not merely left alone (pinned by the
    # snapshot/path assertions above, which do differ).
    assert state.approved_grants_sha256 == old_grants_hash


def test_refresh_approved_grant_snapshot_refuses_when_digest_mismatches_accepted(
    store, fixtures_dir,
):
    """Belt and braces (should-fix item 1): `_refresh_approved_grant_snapshot`
    must refuse to rebind -- raising, not silently keeping the stale snapshot --
    when the digest of the bytes it is asked to snapshot does not match
    `state.accepted_plan_digest`. `cmd_replan` itself always stamps that field
    from the SAME buffer it passes here, so this only fires when something
    upstream broke that invariant; exercised directly since going through
    `cmd_replan` can never itself produce a mismatch by construction."""
    sid = "refresh-digest-mismatch"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan)

    state = store.load(sid)
    old_snapshot_path = state.plan_snapshot_path
    old_snapshot_hash = state.plan_snapshot_hash
    old_grants_hash = state.approved_grants_sha256
    state.accepted_plan_digest = "0" * 64  # deliberately wrong

    doc, data, digest = cli.load_plan_with_digest(plan)
    assert digest != state.accepted_plan_digest

    with pytest.raises(ValueError, match="accepted_plan_digest"):
        cli._refresh_approved_grant_snapshot(state, store, doc, data, digest)

    # no rebind happened: snapshot path/hash and the grant hash are untouched
    assert state.plan_snapshot_path == old_snapshot_path
    assert state.plan_snapshot_hash == old_snapshot_hash
    assert state.approved_grants_sha256 == old_grants_hash


# --- (2) _stage_grant_entries reports read failures loudly, not as empty grants ---


def test_stage_grant_entries_reports_missing_snapshot_file(store, fixtures_dir):
    sid = "snapshot-missing-on-disk"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    Path(state.plan_snapshot_path).unlink()

    declared, derived, dropped, error, _note = cli._stage_grant_entries(state, 1)
    assert declared == [] and derived == [] and dropped == []
    assert error and "missing on disk" in error


def test_stage_grant_entries_reports_unloadable_snapshot(store, fixtures_dir):
    """A snapshot whose bytes still match the stamped hash (so the tamper check
    above does not fire) but no longer parse as a valid plan must be reported as
    an unloadable-snapshot error, not silently emptied."""
    sid = "snapshot-unloadable"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    garbage = b"this is not valid toml [["
    Path(state.plan_snapshot_path).write_bytes(garbage)
    state.plan_snapshot_hash = hashlib.sha256(garbage).hexdigest()
    store.save(state)

    declared, derived, dropped, error, _note = cli._stage_grant_entries(state, 1)
    assert declared == [] and derived == [] and dropped == []
    assert error and "cannot load" in error


def test_stage_grant_entries_read_failure_is_isolated_per_call(store, fixtures_dir):
    """A read failure against the (whole-plan) snapshot is reported at most once per
    call and never leaks state that corrupts a subsequent, independent read once
    the underlying condition is gone -- a stage-2 read after a failing stage-1
    read on the SAME state object still succeeds cleanly."""
    sid = "snapshot-isolation"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    good_path = state.plan_snapshot_path
    good_hash = state.plan_snapshot_hash
    Path(good_path).unlink()

    _decl1, _der1, _drop1, error1, _note1 = cli._stage_grant_entries(state, 1)
    assert error1 and "missing on disk" in error1

    # restore the snapshot exactly as it was; a fresh read must be clean again --
    # the earlier failure must not have mutated `state` itself.
    assert state.plan_snapshot_path == good_path
    assert state.plan_snapshot_hash == good_hash
    Path(good_path).write_bytes(Path(fixtures_dir / "plan_two_stage.toml").read_bytes())

    _decl2, _der2, _drop2, error2, _note2 = cli._stage_grant_entries(state, 1)
    assert error2 is None


def test_cmd_stage_grants_surfaces_derived_hash_mismatch_error(store, fixtures_dir):
    sid = "derived-hash-stale-report"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.approved_grants_sha256 = "0" * 64
    store.save(state)

    directive = cli.cmd_stage_grants(ns(session=sid, stage=1, json=False), store=store)
    assert directive.ok
    assert directive.data["error"] and "approved_grants_sha256" in directive.data["error"]
    assert "ERROR:" in directive.detail


def test_stage_grant_entries_legacy_session_reports_advisory_note_not_error(
    store, fixtures_dir, tmp_path,
):
    """Item 4 (should-fix): a session approved before grant hashing existed
    (`approved_grants_sha256` unset) is NOT an error path -- `_stage_grant_
    entries` must return `error=None` with a `note` naming it advisory
    ("unverified"), and the DECLARED entries (computed unconditionally, before
    the hash-unset branch) must be byte-identical to the same session's
    declared entries read with the hash set. `cmd_stage_grants`'s rendered
    text must surface that note, not swallow it."""
    sid = "legacy-session-advisory-note"
    plan_path = _write_grants_plan(
        fixtures_dir, tmp_path, "[stage.grants]\nallow = [\"Bash(git status:*)\"]\n",
        "plan_two_stage_legacy_note_declared_grants.toml",
    )
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    state = store.load(sid)
    assert state.approved_grants_sha256  # approve-time hashing set it

    declared_hashed, _derived_hashed, _dropped_hashed, error_hashed, note_hashed = \
        cli._stage_grant_entries(state, 1)
    assert error_hashed is None
    assert note_hashed is None

    state.approved_grants_sha256 = None
    state.approved_grant_entries = None  # approved before hashing: nothing stored either
    store.save(state)
    state = store.load(sid)

    declared_legacy, _derived_legacy, _dropped_legacy, error_legacy, note_legacy = \
        cli._stage_grant_entries(state, 1)
    assert error_legacy is None
    assert note_legacy and "unverified" in note_legacy
    assert declared_legacy == declared_hashed

    directive = cli.cmd_stage_grants(ns(session=sid, stage=1, json=False), store=store)
    assert directive.data["note"] == note_legacy
    assert "NOTE:" in directive.detail
    assert "unverified" in directive.detail


def test_effective_stage_grants_venue_decoupled_from_snapshot_meta(
    store, fixtures_dir, tmp_path,
):
    """Item 2 (should-fix): `_effective_stage_grants` must resolve the
    workdir-absolute baseline expansion venue via `state.resolve_check_venue
    (CheckVenue.DELIVERY.value)` -- the SAME live state field `cmd_dispatch`
    passes as the child's actual cwd -- never by reloading the approved-plan
    SNAPSHOT's own `[meta]` and deriving a venue from that. The base fixture
    `plan_two_stage.toml` declares neither `repo_root` nor `delivery_worktree`,
    so a snapshot-meta read would resolve to `"."` (the canon checkout) and
    never expand for a venue the snapshot itself has no idea about; setting
    `state.delivery_worktree` directly (exactly what a LATER-approved plan's
    `_sync_venue_from_plan` would have done) must still show up in the
    baseline expansion, because coverage is read off the live state field, not
    off the frozen snapshot bytes.

    Regression pin: reverting `_effective_stage_grants`'s venue line
    (line ~5153) to `_venue_for(load_plan(state.plan_snapshot_path))` instead
    of `state.resolve_check_venue(CheckVenue.DELIVERY.value)` makes this test
    fail, since the reloaded snapshot's `[meta]` still declares no venue at
    all -- confirmed by hand at authoring time, not re-checked by CI."""
    sid = "effective-grants-venue-from-state-not-snapshot"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    assert state.repo_root is None and state.delivery_worktree is None  # fixture premise

    venue = str(tmp_path / "state-only-delivery-venue")
    state.delivery_worktree = venue
    store.save(state)
    state = store.load(sid)
    assert state.resolve_check_venue(CheckVenue.DELIVERY.value) == venue

    coverage = cli._effective_stage_grants(state, 1)
    workdir_prefix = f"Bash(python3 {venue}/scripts/"
    assert any(g.rule.startswith(workdir_prefix) for g in coverage.allow)


# --- (3) cmd_dispatch refuses to spawn a grant-declaring stage it cannot read ---


def test_dispatch_refuses_when_declared_grants_unreadable(store, fixtures_dir, tmp_path):
    sid = "dispatch-refuses-unreadable-grants"
    plan_path = _write_grants_plan(
        fixtures_dir, tmp_path, "[stage.grants]\nallow = [\"Bash(git status:*)\"]\n",
        "plan_dispatch_refuse_grants.toml",
    )
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    state = store.load(sid)
    Path(state.plan_snapshot_path).unlink()

    def _runner(argv, cwd=None):
        raise AssertionError("dispatch_stage must not be invoked when grants can't be read")

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=_runner,
    )
    assert directive.ok is False
    assert "missing on disk" in directive.detail


def test_dispatch_proceeds_when_stage_declares_no_grants_despite_unreadable_snapshot(
    store, fixtures_dir,
):
    """A stage that declares no grants at all has nothing to silently lose from an
    unreadable snapshot, so dispatch must not refuse on its account -- only a
    grant-DECLARING stage's own read failure blocks it."""
    sid = "dispatch-proceeds-no-grants-declared"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    Path(state.plan_snapshot_path).unlink()

    def _runner(argv, cwd=None):
        from agentctl.dispatch import RunResult
        return RunResult(0, stdout="COMPLETED: done\n")

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=_runner,
    )
    assert directive.marker == "COMPLETED"


def test_dispatch_refuses_when_derived_only_stage_has_hash_mismatch(store, fixtures_dir):
    """Should-fix item 3: a stage that declares NO grants at all (`plan_two_stage
    .toml`'s stage 1: no `[stage.grants]`) but whose plan content still derives a
    non-empty set (a DR-O rule for its `mod.py` output_artifact) must still block
    dispatch when `_stage_grant_entries` reports a derived-grants hash mismatch.
    Before this fix, the refusal check looked only at `_stage_declares_grants`,
    so a derived-only stage's read error let dispatch through silently, spawning
    the child without the coverage it was actually approved with."""
    sid = "dispatch-refuses-derived-only-hash-mismatch"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    stage1 = state.stage(1)
    _declares = bool(getattr(stage1, "grants", None)) and not stage1.grants.is_empty()
    assert not _declares  # the fixture premise: nothing DECLARED to lose
    assert cli._stage_would_derive_grants(state, 1)  # but something DERIVED
    state.approved_grants_sha256 = "0" * 64  # forces the derived-grants hash mismatch
    store.save(state)

    def _runner(argv, cwd=None):
        raise AssertionError(
            "dispatch_stage must not be invoked when derived grants can't be trusted"
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=_runner,
    )
    assert directive.ok is False
    assert "derived-grants hash mismatch" in directive.detail


# --- (4) markdown-decorated `Rule:` line parsing ---


def test_parse_rule_line_tolerates_markdown_bullet_and_bold():
    body = "PERMISSION-REQUEST:\nAction: run pytest\nWhy: need to verify\n- **Rule:** `Bash(python3 -m pytest:*)`"
    assert cli._parse_rule_line(body) == "Bash(python3 -m pytest:*)"


def test_parse_rule_line_still_accepts_bare_line():
    body = "PERMISSION-REQUEST:\nAction: x\nRule: Bash(git status:*)"
    assert cli._parse_rule_line(body) == "Bash(git status:*)"


def test_parse_rule_line_accepts_bold_without_bullet():
    body = "**Rule:** Bash(git status:*)"
    assert cli._parse_rule_line(body) == "Bash(git status:*)"


def test_parse_rule_line_rejects_line_not_anchored_on_rule():
    body = "See the Rule: this isn't it, just prose mentioning the word"
    # "Rule:" must anchor the (stripped, bullet/bold-unwrapped) line itself, not
    # appear mid-sentence -- this line's mention doesn't start the line even after
    # stripping decoration, so it must not be mistaken for a real Rule: line.
    assert cli._parse_rule_line(body) is None


# --- (5) regression: growing derived grants on a refinement still classifies substantive ---


def test_refinement_attempting_to_grow_grants_still_classifies_substantive(fixtures_dir):
    """Pre-existing behavior (`_grants_grew`/`diff_plans` in plan.py), pinned here
    as part of this stage's done criterion rather than left to rely solely on
    test_replan.py's own coverage: a verify_command edit that derives a wider
    DR-V Bash rule intercepts BEFORE the prose comparison and forces
    'substantive', so a stale snapshot is never even reachable for this case --
    `cmd_replan`'s refinement branch (where the snapshot-refresh fix lives) is
    never entered."""
    base = load_plan(str(fixtures_dir / "plan_two_stage_verifyfix_grantgrowth.toml"))
    changed = load_plan(str(fixtures_dir / "plan_two_stage_verifyfix_grantgrowth_changed.toml"))
    assert diff_plans(base, changed) == "substantive"


def test_replan_of_grant_growing_plan_returns_to_plan_ready(store, fixtures_dir):
    sid = "grant-growth-replan-integration"
    base = str(fixtures_dir / "plan_two_stage_verifyfix_grantgrowth.toml")
    changed = str(fixtures_dir / "plan_two_stage_verifyfix_grantgrowth_changed.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=base)

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert not state.approval.passed  # re-arm: must re-approve


# --- (3a) transcript evidence wins over a contradicting self-reported Rule: ---


def test_dispatch_transcript_uncovered_wins_over_contradicting_self_reported_rule(
    monkeypatch, store, fixtures_dir,
):
    """Finding S9 regression. `grant_covers_call` is a pure function of
    (coverage, tool_name, tool_input) -- both `_classify_transcript_denials`
    and the PERMISSION-REQUEST branch's `self_covered` check read off the
    SAME pre-launch `coverage` object, so a self-reported `Rule:` line naming
    the exact command a transcript denial recorded can never naturally
    disagree with the transcript's own verdict on that denial. To pin the
    branching logic in cli.py's PERMISSION-REQUEST handler in isolation
    (defense-in-depth for the real engine regression the stage-9 live run hit,
    where the two verdicts DID disagree), stub `grant_covers_call` to answer
    by CALL ORDER: False for the first call (the transcript classification,
    correctly finding this denial uncovered), True for every call after
    (simulating a wrongly-permissive self-reported verdict for the identical
    command). The fix must let the transcript's (first, uncovered) verdict
    win -- the request stays a planning miss routed to the user, never a
    materialization defect."""
    sid = "perm-request-transcript-uncovered-wins"
    _to_executing(store, sid, fixtures_dir)
    transcript_path = fixtures_dir / "transcript_stops" / "permission-denial-uncovered.jsonl"
    # The self-reported Rule: line names the EXACT full command the transcript
    # fixture denies (redirect included), via bash_command_from_rule_arg's
    # trailing-":*"-strip -- byte-identical text so `expected_digest` matches
    # the transcript row's own digest without relying on grant_covers_call's
    # (stubbed) verdict at all.
    denied_command = "python3 scripts/unlisted-tool.py 1>/tmp/out.txt 2>&1"

    calls = {"n": 0}

    def _stub(coverage, tool_name, tool_input):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(cli._grants, "grant_covers_call", _stub)

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to run an unlisted tool\n"
                f"Rule: Bash({denied_command}:*)\n"
            ),
            stderr=f"spawn-specialist: transcript={transcript_path}\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert calls["n"] >= 2  # the transcript classification ran, then self_covered
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert not state.materialization_defects
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["source"] == "transcript"
    assert state.planning_misses[0]["asked_user"] is True


# --- (3b) baseline script rules also cover the child's own workdir-absolute spelling ---


def test_baseline_for_workdir_noop_on_falsy_workdir():
    assert kind_baselines.baseline_for_workdir("developer", None) == \
        kind_baselines.KIND_BASELINES["developer"]
    assert kind_baselines.baseline_for_workdir("developer", "") == \
        kind_baselines.KIND_BASELINES["developer"]


def test_baseline_for_workdir_noop_when_workdir_scripts_is_canon():
    canon_workdir = str(kind_baselines.SCRIPTS_DIR.parent)
    assert kind_baselines.baseline_for_workdir("developer", canon_workdir) == \
        kind_baselines.KIND_BASELINES["developer"]


def test_baseline_for_workdir_adds_only_workdir_spellings_of_existing_script_rules(tmp_path):
    before = {kind: list(rules) for kind, rules in kind_baselines.KIND_BASELINES.items()}
    before_sha = {
        kind: kind_baselines.kind_baseline_sha256(kind) for kind in kind_baselines.KIND_BASELINES
    }

    workdir = str(tmp_path / "some-worktree")
    expanded = kind_baselines.baseline_for_workdir("developer", workdir)
    canon = kind_baselines.KIND_BASELINES["developer"]
    workdir_scripts = tmp_path / "some-worktree" / "scripts"

    assert expanded[: len(canon)] == canon  # unmodified baseline, still present verbatim
    added = expanded[len(canon):]
    assert added  # the developer baseline has at least one canon-absolute script rule
    canon_prefix = f"Bash(python3 {kind_baselines.SCRIPTS_DIR}/"
    canon_script_rules = [r for r in canon if r.startswith(canon_prefix)]
    assert len(added) == len(canon_script_rules)
    for rule in added:
        assert rule.startswith(f"Bash(python3 {workdir_scripts}/")
    # every added rule is exactly a canon-absolute rule with the SCRIPTS_DIR
    # prefix swapped for the workdir's own scripts/ dir -- no new script names.
    added_suffixes = {r[len(f"Bash(python3 {workdir_scripts}/"):] for r in added}
    canon_suffixes = {r[len(canon_prefix):] for r in canon_script_rules}
    assert added_suffixes == canon_suffixes

    # KIND_BASELINES / kind_baseline_sha256 are untouched by the expansion call.
    assert {kind: list(rules) for kind, rules in kind_baselines.KIND_BASELINES.items()} == before
    assert {
        kind: kind_baselines.kind_baseline_sha256(kind) for kind in kind_baselines.KIND_BASELINES
    } == before_sha


def test_dispatch_denied_workdir_absolute_baseline_call_classifies_as_materialization_defect(
    store, fixtures_dir, tmp_path,
):
    """The exact live scenario (finding S9): a developer stage's child ran under a
    worktree the stage's plan declares as `repo_root`, invoked a baseline script
    (`verify-agentctl.py`) by its own workdir-absolute spelling, and was denied --
    before 3b, no materialized rule named that spelling (only canon-absolute and
    repo-relative), so this denial was a genuine planning miss. After 3b,
    `_effective_stage_grants` expands the developer baseline with the stage's own
    resolved venue, covering exactly this spelling -- so the denial reclassifies
    as a materialization defect (the grant existed; --settings simply failed to
    carry the workdir spelling), never a user ask."""
    sid = "perm-request-workdir-absolute-baseline"
    repo_root = str(tmp_path / "worktree-repo-root")
    plan_path = _write_plan_with_repo_root(
        fixtures_dir, tmp_path, repo_root, "plan_two_stage_workdir_baseline.toml",
    )
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    state = store.load(sid)
    # `_effective_stage_grants` expands the baseline with
    # `state.resolve_check_venue(CheckVenue.DELIVERY.value)` -- the same venue
    # dispatch itself passes as the child's cwd -- so coverage can never expand
    # for a workdir the child does not actually run in.
    assert state.resolve_check_venue(CheckVenue.DELIVERY.value) == repo_root

    denied_command = f"python3 {repo_root}/scripts/verify-agentctl.py"

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to verify the engine's own invariants\n"
                f"Rule: Bash({denied_command}:*)\n"
            ),
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "OVERCOME-DIFFICULTY"
    state = store.load(sid)
    assert len(state.materialization_defects) == 1
    assert state.materialization_defects[0]["evidence"] == "self-reported"
    assert state.permission_request is None
    assert not state.planning_misses


def test_dispatch_passes_runner_a_cwd_equal_to_the_venue_coverage_expands_with(
    store, fixtures_dir, tmp_path,
):
    """`spawn-specialist.py`'s own `--workdir` argument has no explicit CLI flag
    threaded by dispatch -- its `main()` falls back to `os.getcwd()`, which under
    `dispatch_stage`'s `subprocess.run(argv, cwd=cwd)` IS the child process's cwd,
    so `build_child_settings`'s workdir naturally equals whatever `cwd` dispatch
    passed the runner. This pins that the `cwd` `cmd_dispatch` threads through
    (`child_cwd = state.resolve_check_venue(CheckVenue.DELIVERY.value)`) is the
    SAME venue `_effective_stage_grants`'s baseline expansion resolves
    (`state.resolve_check_venue(CheckVenue.DELIVERY.value)`) -- the two must
    never drift on which workdir spelling a baseline actually covers vs. which
    one the child actually runs under."""
    sid = "dispatch-cwd-matches-venue"
    repo_root = str(tmp_path / "cwd-venue-worktree")
    plan_path = _write_plan_with_repo_root(
        fixtures_dir, tmp_path, repo_root, "plan_two_stage_cwd_venue.toml",
    )
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    state = store.load(sid)
    expected_venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    assert expected_venue == repo_root

    seen = {}

    def runner(argv, cwd=None):
        seen["cwd"] = cwd
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert seen["cwd"] == expected_venue
