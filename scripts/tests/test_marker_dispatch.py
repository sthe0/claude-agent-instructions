"""parse_marker + cmd_dispatch marker routing: every specialist return marker on a
spawn's stdout routes to a Directive whose action/node/marker the manager acts on,
with the deterministic continuation text already assembled."""
import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, dispatch
from agentctl.dispatch import RunResult, build_argv, parse_marker
from agentctl.state import Actor, Criterion, Means, Node, Stage, Subject

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def ns(**kw):
    return Namespace(**kw)


# --- parse_marker -----------------------------------------------------------

@pytest.mark.parametrize("marker", list(dispatch.RETURN_MARKERS))
def test_parse_marker_each_known_marker(marker):
    m, body = parse_marker(f"{marker}: some detail here\n")
    assert m == marker
    assert body == "some detail here"


def test_parse_marker_review():
    m, body = parse_marker("checked the plan\nREVIEW: revise\n")
    assert m == "REVIEW"
    assert body == "revise"


def test_parse_marker_malformed():
    m, body = parse_marker("MALFORMED: specialist output did not start with a marker\nrest\n")
    assert m == "MALFORMED"
    assert body == "specialist output did not start with a marker"


def test_parse_marker_skips_leading_blank_lines():
    m, body = parse_marker("\n\n   \nCLARIFY: which key?\n")
    assert m == "CLARIFY"
    assert body == "which key?"


def test_parse_marker_no_marker():
    assert parse_marker("just some free text\nwith no marker\n") == (None, "")
    assert parse_marker("") == (None, "")


def test_parse_marker_finds_marker_after_preamble():
    # a specialist may print a summary before the marker line — the scan must
    # tolerate the preamble and still find the marker (no false (None, "") BLOCK)
    assert parse_marker("preamble text\nmore notes\nCOMPLETED: done\n") == ("COMPLETED", "done")
    # but genuinely markerless output still maps to (None, "")
    assert parse_marker("preamble text\nmore notes\nstill nothing\n") == (None, "")
    # and a MALFORMED wrapper anywhere is detected
    assert parse_marker("summary line\nMALFORMED: no marker\n") == ("MALFORMED", "no marker")


def test_parse_marker_child_infra_failure():
    m, body = parse_marker(
        "CHILD_INFRA_FAILURE: the child spawn never reached the API "
        "(matched 'ENOTFOUND').\n\nAPI Error: Unable to connect to API (ENOTFOUND)\n"
    )
    assert m == dispatch.CHILD_INFRA_FAILURE
    assert body.startswith("the child spawn never reached the API")


def test_parse_marker_child_exhausted():
    m, body = parse_marker(
        "CHILD_EXHAUSTED: the child spawn was refused for size "
        "(matched 'Prompt is too long').\n"
    )
    assert m == dispatch.CHILD_EXHAUSTED
    assert body.startswith("the child spawn was refused for size")


def _always_failing_runner(calls):
    def run(argv, **kwargs):
        calls.append(argv)
        from lib.marker_extract import RunResult
        return RunResult(1, "", "boom")

    return run


def test_extractor_failed_falls_back_and_the_legacy_scan_finds_a_marker(monkeypatch):
    # Stage-3 scenario: the model extractor itself never delivers a verdict
    # (retry exhausted, outcome=EXTRACTOR_FAILED) but the legacy any-line scan
    # the caller falls back to DOES find a marker in the specialist's own text.
    # That marker is used, and no AGENTCTL_MARKER_EXTRACTOR instruction is
    # surfaced — that env var only helps a FUTURE spawn, not this one.
    from lib import host_llm, marker_extract
    from lib.planner_plan_check import check_planner_return

    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/claude")
    calls: list = []
    extraction = marker_extract.build_extraction(
        "COMPLETED: shipped it, tests green.\n",
        kind="developer",
        runner=_always_failing_runner(calls),
    )
    assert extraction.outcome == marker_extract.OUTCOME_EXTRACTOR_FAILED
    assert extraction.degraded is True
    assert len(calls) == marker_extract._MAX_EXTRACT_ATTEMPTS  # retried, not one-shot

    forwarded, ok, marker = check_planner_return(
        "COMPLETED: shipped it, tests green.\n", "developer", extraction=extraction
    )
    assert ok is True
    assert marker == "COMPLETED"
    assert "AGENTCTL_MARKER_EXTRACTOR" not in forwarded


def test_extractor_failed_falls_back_and_the_legacy_scan_finds_none(monkeypatch):
    # Same observation failure, but the specialist's own text has no marker-
    # shaped line either: MALFORMED as today, attributed to the reader/
    # extractor having failed to observe rather than to the child specialist's
    # output being unparseable — and still no env-var instruction for a spawn
    # that has already finished.
    from lib import host_llm, marker_extract
    from lib.planner_plan_check import check_planner_return

    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/claude")
    calls: list = []
    extraction = marker_extract.build_extraction(
        "just a summary, no marker at all",
        kind="developer",
        runner=_always_failing_runner(calls),
    )
    assert extraction.outcome == marker_extract.OUTCOME_EXTRACTOR_FAILED
    assert extraction.degraded is True

    forwarded, ok, marker = check_planner_return(
        "just a summary, no marker at all", "developer", extraction=extraction
    )
    assert ok is False
    assert marker is None
    assert parse_marker(forwarded)[0] == "MALFORMED"
    assert "AGENTCTL_MARKER_EXTRACTOR" not in forwarded


def test_malformed_envelope_outranks_a_marker_line_in_the_preserved_original():
    # The scan is ONE ordered pass, so the winner is the first line in DOCUMENT
    # order. A MALFORMED envelope preserves the specialist's original bytes
    # below it, and those bytes may well contain a marker-shaped line the
    # second-pass extraction deliberately refused. Two sequential passes (all
    # lines for a marker, then all lines for MALFORMED) would let that refused
    # marker win and route the stage as a success — fail-open.
    from lib.marker_extract import Extraction
    from lib.planner_plan_check import check_planner_return

    forwarded, ok, _ = check_planner_return(
        "COMPLETED: I think I am done\nESCALATE: or perhaps not", "developer",
        extraction=Extraction(None, reason="two markers, no terminal one"),
    )
    assert ok is False
    assert parse_marker(forwarded)[0] == "MALFORMED"


# --- build_argv -------------------------------------------------------------

def _make_spawn_stage(index: int = 3) -> Stage:
    return Stage(
        index=index,
        title="test stage",
        subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="tests green"),
    )


def test_build_argv_includes_stage_index():
    stage = _make_spawn_stage(index=7)
    argv = build_argv(stage, "/tmp/plan.toml")
    assert "--stage-index" in argv
    assert argv[argv.index("--stage-index") + 1] == "7"


def test_build_argv_stage_index_matches_stage():
    for idx in (1, 2, 5):
        stage = _make_spawn_stage(index=idx)
        argv = build_argv(stage, "/tmp/plan.toml")
        assert argv[argv.index("--stage-index") + 1] == str(idx)


# --- drift guard ------------------------------------------------------------

def test_return_markers_mirror_spawn_specialist():
    spec = importlib.util.spec_from_file_location(
        "spawn_specialist", REPO_ROOT / "scripts" / "spawn-specialist.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert dispatch.RETURN_MARKERS == mod.RETURN_MARKERS


@pytest.mark.parametrize("marker", list(dispatch.RETURN_MARKERS))
def test_canonicalized_output_round_trips_through_parse_marker(marker):
    # Behavioural twin of the tuple-identity guard above: a message a second-
    # pass extraction confirmed (e.g. recovered from under markdown emphasis,
    # where the legacy any-line regex would have found nothing at all) is
    # canonicalised by lib.planner_plan_check.canonicalize before it ever
    # reaches this module's parse_marker — confirm that envelope is actually
    # readable by parse_marker for every known marker, not just asserted equal
    # by name.
    from lib.planner_plan_check import canonicalize

    original = f"**{marker}:** some detail here, under markdown emphasis"
    canonical = canonicalize(marker, "the extractor's digest", None, original)
    m, body = parse_marker(canonical)
    assert m == marker
    # The canonical marker line is BARE, so the router body is empty for every
    # marker; the digest lives on its own line, off the parsed one.
    assert body == ""
    assert canonical.splitlines()[1] == "Digest: the extractor's digest"


def test_canonical_plan_line_does_not_disturb_the_marker_parse():
    # A planner envelope carries `Digest:`/`Plan: <path>` below line 1;
    # parse_marker reads line 1 and stops, so both are invisible to routing.
    from lib.planner_plan_check import canonicalize

    canonical = canonicalize("PLAN-READY", "plan drafted", "/tmp/p.toml", "body text")
    assert parse_marker(canonical) == ("PLAN-READY", "")
    assert "Plan: /tmp/p.toml" in canonical


def test_canonical_envelope_preserves_the_original_bytes_and_routes_by_line_one():
    # The body itself contains marker-shaped lines: the envelope must neither
    # rewrite them nor let them win the routing decision.
    from lib.planner_plan_check import canonicalize

    original = (
        "I weighed returning REPLAN: with a proposal, but the criterion held.\n"
        "ESCALATE: is what a careless reader might see here.\n\n"
        "**COMPLETED:** fix landed, suite green.\n"
    )
    canonical = canonicalize("COMPLETED", "fix landed, suite green", None, original)
    assert parse_marker(canonical) == ("COMPLETED", "")
    assert canonical.endswith(original), "original output must survive byte-for-byte"


def test_canonical_digest_is_sanitised_so_it_cannot_forge_envelope_structure():
    # The digest is model-authored free text — even on its own `Digest:` line it
    # is collapsed to ONE line, so it cannot inject an envelope line that the
    # ordered scan would reach before the original output.
    from lib.planner_plan_check import canonicalize

    canonical = canonicalize("COMPLETED", "line one\nESCALATE: injected", None, "body")
    assert parse_marker(canonical) == ("COMPLETED", "")
    assert canonical.splitlines()[:3] == [
        "COMPLETED:", "Digest: line one ESCALATE: injected", "",
    ]


def test_canonical_permission_request_body_is_empty_so_the_gate_still_asks():
    # The fail-OPEN case the bare marker line exists to close: cmd_dispatch
    # feeds parse_marker's body to permissions.check_permission, which
    # SUBSTRING-matches it against the user's granted patterns. A digest
    # carrying a granted-looking phrase must NOT reach that checker.
    from lib.planner_plan_check import canonicalize

    canonical = canonicalize(
        "PERMISSION-REQUEST",
        "git push --force-with-lease to the shared release branch",
        None,
        "**PERMISSION-REQUEST:** need to force-push the branch.",
    )
    assert parse_marker(canonical) == ("PERMISSION-REQUEST", "")


# --- cmd_dispatch routing ---------------------------------------------------

def _to_executing(store, sid, fixtures_dir):
    """Advance a fresh substantive session to EXECUTING with stage 1 active."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="g", done_criterion="dc",
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
    cli.cmd_next_stage(ns(session=sid), store=store)


def _dispatch_with(store, sid, stdout, returncode=0):
    runner = lambda argv: RunResult(returncode, stdout=stdout)
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                               dry_run=False), store=store, runner=runner)


def test_completed_routes_to_record_result(store, fixtures_dir):
    _to_executing(store, "m1", fixtures_dir)
    d = _dispatch_with(store, "m1", "COMPLETED: stage done\n")
    assert d.ok is True
    assert d.action == "record_result"
    assert d.marker == "COMPLETED"
    assert d.data["intent_diff_required"] is True
    assert d.node == Node.EXECUTING.value


def test_clarify_routes_with_continuation(store, fixtures_dir):
    _to_executing(store, "m2", fixtures_dir)
    d = _dispatch_with(store, "m2", "CLARIFY: which config key?\n")
    assert d.ok is True
    assert d.action == "answer_clarify"
    assert d.marker == "CLARIFY"
    assert d.data["question"] == "which config key?"
    assert "which config key?" in d.data["continuation"]
    assert d.node == Node.EXECUTING.value


def test_replan_routes_to_replan(store, fixtures_dir):
    _to_executing(store, "m3", fixtures_dir)
    d = _dispatch_with(store, "m3", "REPLAN: step criterion is wrong\n")
    assert d.ok is False
    assert d.action == "replan"
    assert d.marker == "REPLAN"
    assert d.data["reason"] == "step criterion is wrong"


def test_incomplete_routes_to_decide(store, fixtures_dir):
    _to_executing(store, "m4", fixtures_dir)
    d = _dispatch_with(store, "m4", "INCOMPLETE: half done, blocked on X\n")
    assert d.ok is False
    assert d.action == "decide_incomplete"
    assert d.marker == "INCOMPLETE"
    assert d.data["reason"] == "half done, blocked on X"


def test_plan_ready_routes_to_approval_gate(store, fixtures_dir):
    _to_executing(store, "m5", fixtures_dir)
    d = _dispatch_with(store, "m5", "PLAN-READY: plan at /tmp/p.toml\n")
    assert d.ok is True
    assert d.action == "await_plan_approval"
    assert d.marker == "PLAN-READY"
    assert d.node == Node.EXECUTING.value  # node unchanged — re-enters approval gate


def test_escalate_parks_blocked(store, fixtures_dir):
    _to_executing(store, "m6", fixtures_dir)
    d = _dispatch_with(store, "m6", "ESCALATE: spec ambiguity\n")
    assert d.ok is False
    assert d.action == "escalate"
    assert d.marker == "ESCALATE"
    assert d.node == Node.BLOCKED.value
    assert store.load("m6").blocked_from == Node.EXECUTING.value


def test_review_marker_parks_blocked(store, fixtures_dir):
    # REVIEW is a recognised marker with no dedicated route in cmd_dispatch's
    # if-chain: it must fall through to the same _park_blocked path as an
    # unrecognised marker, exactly as before REVIEW was added to RETURN_MARKERS.
    _to_executing(store, "m11", fixtures_dir)
    d = _dispatch_with(store, "m11", "REVIEW: revise\n")
    assert d.ok is False
    assert d.action == "escalate"
    assert d.marker == "ESCALATE"
    assert d.node == Node.BLOCKED.value
    assert store.load("m11").blocked_from == Node.EXECUTING.value


def test_malformed_parks_blocked(store, fixtures_dir):
    _to_executing(store, "m7", fixtures_dir)
    d = _dispatch_with(store, "m7", "MALFORMED: no marker\n")
    assert d.ok is False
    assert d.node == Node.BLOCKED.value
    assert d.marker == "ESCALATE"


def test_markerless_success_parks_blocked(store, fixtures_dir):
    _to_executing(store, "m8", fixtures_dir)
    d = _dispatch_with(store, "m8", "free text, no marker\n", returncode=0)
    assert d.ok is False
    assert d.node == Node.BLOCKED.value


def test_markerless_failure_handles_spawn_failure(store, fixtures_dir):
    _to_executing(store, "m9", fixtures_dir)
    d = _dispatch_with(store, "m9", "", returncode=1)
    assert d.ok is False
    assert d.action == "handle_spawn_failure"
    assert d.node == Node.EXECUTING.value  # not blocked — a plain spawn failure


def test_permission_dispatch_never_hands_the_digest_to_the_permission_checker(
    store, fixtures_dir
):
    # End-to-end twin of the parse-level guard above: a checker that WOULD grant
    # on the digest's text is asked about the EMPTY body instead, so the
    # directive is ask_user_permission (always-ask, the legacy polarity) rather
    # than continue_spawn with the user's ask silently skipped.
    from lib.planner_plan_check import canonicalize

    _to_executing(store, "m12", fixtures_dir)
    asked = []

    def checker(action: str) -> bool:
        asked.append(action)
        return "git push" in action

    canonical = canonicalize(
        "PERMISSION-REQUEST", "git push to the shared branch", None, "body"
    )
    runner = lambda argv: RunResult(0, stdout=canonical)
    d = cli.cmd_dispatch(ns(session="m12", budget="medium", complexity="medium",
                            dry_run=False), store=store, runner=runner,
                         perm_checker=checker)
    assert asked == [""]
    assert d.action == "ask_user_permission"
    assert d.data["action"] == ""


def test_marker_wins_over_nonzero_returncode(store, fixtures_dir):
    # a specialist may exit non-zero yet carry a valid CLARIFY marker — marker wins
    _to_executing(store, "m10", fixtures_dir)
    d = _dispatch_with(store, "m10", "CLARIFY: which path?\n", returncode=1)
    assert d.action == "answer_clarify"
    assert d.marker == "CLARIFY"


# --- child outcome routing (stage 5, issues #78/#80) -------------------------
#
# spawn-specialist.py classifies the child's own terminal condition (never
# reached/lost the API, or refused for size) BEFORE the marker question, and
# writes CHILD_INFRA_FAILURE:/CHILD_EXHAUSTED: as the envelope's first line
# for those cases. cmd_dispatch routes each to its own named directive —
# never an automatic re-spawn; the coordinator still decides whether to spend
# the money on a retry.

def test_child_infra_failure_routes_to_retry_dispatch(store, fixtures_dir):
    _to_executing(store, "m13", fixtures_dir)
    d = _dispatch_with(
        store, "m13",
        "CHILD_INFRA_FAILURE: the child spawn never reached, or lost, the API "
        "before exiting (matched 'ENOTFOUND').\n\n"
        "API Error: Unable to connect to API (ENOTFOUND)\n",
        returncode=1,
    )
    assert d.ok is False
    assert d.action == "retry_dispatch"
    assert d.marker == dispatch.CHILD_INFRA_FAILURE
    assert d.node == Node.EXECUTING.value  # not blocked — a transient run condition
    assert "retrying the same dispatch" in d.detail


def test_child_exhausted_routes_to_reduce_brief_or_reattest(store, fixtures_dir):
    _to_executing(store, "m14", fixtures_dir)
    d = _dispatch_with(
        store, "m14",
        "CHILD_EXHAUSTED: the child spawn was refused for size "
        "(matched 'Prompt is too long').\n\n"
        "Error: Prompt is too long: 512000 tokens > 400000 maximum\n",
        returncode=1,
    )
    assert d.ok is False
    assert d.action == "reduce_brief_or_reattest"
    assert d.marker == dispatch.CHILD_EXHAUSTED
    assert d.node == Node.EXECUTING.value
    assert "reduced brief" in d.detail


def test_child_outcome_directives_never_recommend_an_automatic_respawn(store, fixtures_dir):
    # Neither directive's action names a re-spawn — the decision to spend
    # money on a retry stays with the coordinator, not the engine.
    _to_executing(store, "m15", fixtures_dir)
    d_infra = _dispatch_with(
        store, "m15", "CHILD_INFRA_FAILURE: transient (matched 'ENOTFOUND').\n", returncode=1
    )
    assert d_infra.action not in ("respawn", "auto_respawn", "continue_spawn")

    _to_executing(store, "m16", fixtures_dir)
    d_exhausted = _dispatch_with(
        store, "m16", "CHILD_EXHAUSTED: refused for size (matched 'Prompt is too long').\n",
        returncode=1,
    )
    assert d_exhausted.action not in ("respawn", "auto_respawn", "continue_spawn")


def test_child_outcome_precedence_a_valid_marker_always_outranks_a_signature(store, fixtures_dir):
    # The critical precedence case: the child's stdout contains BOTH a
    # matching CHILD_INFRA_FAILURE signature AND a valid terminal marker. The
    # marker must win — proving the prefilter cannot suppress a real result.
    # This is spawn-specialist.py's own envelope-writing decision under test
    # at the dispatch boundary: because spawn-specialist.py's
    # _resolve_child_outcome already applied the precedence rule before
    # writing its stdout, a genuinely-answered child's stdout NEVER carries a
    # CHILD_INFRA_FAILURE/CHILD_EXHAUSTED prefix — it carries the real marker
    # on line one, with the signature text merely present later in the body.
    stdout = (
        "COMPLETED: shipped it, tests pass.\n\n"
        "(While debugging I also hit an unrelated API Error: "
        "Unable to connect to API (ENOTFOUND) on a different host, "
        "but that was resolved before this run.)"
    )
    # Confirm the naive failure mode: parse_marker alone, scanning for a
    # CHILD_* signature FIRST rather than deferring to spawn-specialist's own
    # precedence-aware envelope, would find the signature text sitting in a
    # body line — but it is not a CHILD_* PREFIX line, so parse_marker (which
    # only matches line-leading prefixes) still resolves correctly here too.
    m, body = parse_marker(stdout)
    assert m == "COMPLETED"

    _to_executing(store, "m17", fixtures_dir)
    d = _dispatch_with(store, "m17", stdout, returncode=0)
    assert d.ok is True
    assert d.action == "record_result"
    assert d.marker == "COMPLETED"
