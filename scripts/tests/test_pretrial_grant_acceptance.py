"""Pre-trial defect #414: a wildcard grant that covers a protected action (a push to trunk) is
shown to the user at presentation and must be accepted before approve, and a linked child's
wildcard does not stand in for the parent's protected check.

The protected action is a push to the trunk branch: the standing norm forbids it without the
user's explicit confirmation, and `Bash(git push:*)` matches it. The interface these tests
drive is the one the repair adds: the catalogue entry id `push-trunk` in the rendered grants
block, and the user-authority verb `accept-protected-grant --session --id --note`.

A test asserts the repaired behaviour with a message that starts `BEHAVIOUR:`; every setup
assertion lacks that marker. Symbols the repair adds are reached only through the CLI.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

from agentctl import cli, gates
from agentctl.plan import load_plan
from agentctl.render import render_plan_grants
from agentctl.state import Node
from test_plan_presentation import (
    _declared_venue_plan,
    _to_plan_ready,
    _write_rendering,
    gate_on,  # noqa: F401 - fixture
    home_store,  # noqa: F401 - fixture
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLI = SCRIPTS_DIR / "agentctl-cli.py"
PROTECTED_ENTRY = "push-trunk"
WILDCARD_PUSH = "Bash(git push:*)"
NARROW_PUSH = "Bash(git push origin feature:*)"
TRUNK_PUSH_CHECK = "git push origin main"
PARENT_VERIFY = "python3 -m pytest -q tests/test_mod.py"


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _plan_granting(fixtures_dir, tmp_path, rule: str) -> str:
    """The venued two-stage fixture with `rule` as a declared allow of stage 1."""
    src = Path(_declared_venue_plan(fixtures_dir, tmp_path)).read_text(encoding="utf-8")
    marker = 'output_artifacts = ["mod.py"]\n'
    assert marker in src, "setup: the fixture changed and has no stage-1 output_artifacts line"
    out = tmp_path / "granting-plan.toml"
    out.write_text(src.replace(marker, marker + f'\n[stage.grants]\nallow = ["{rule}"]\n', 1),
                   encoding="utf-8")
    return str(out)


def _grants_text(plan: str, fmt: str) -> str:
    return render_plan_grants(load_plan(plan), fmt=fmt)


def _sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_i414_wildcard_push_grant_shown_as_protected_action(fixtures_dir, tmp_path):
    plan = _plan_granting(fixtures_dir, tmp_path, WILDCARD_PUSH)
    for fmt in ("compact", "full"):
        assert WILDCARD_PUSH in _grants_text(plan, fmt), f"setup: the {fmt} block omits the declared rule"

    compact, full = _grants_text(plan, "compact"), _grants_text(plan, "full")

    assert PROTECTED_ENTRY in compact and PROTECTED_ENTRY in full, (
        f"BEHAVIOUR: the grants block of a plan granting {WILDCARD_PUSH} names no protected "
        f"action {PROTECTED_ENTRY!r} (compact: {PROTECTED_ENTRY in compact}, full: {PROTECTED_ENTRY in full})"
    )


def _reach_plan_ready_delivered(home_store, fixtures_dir, tmp_path, sid: str, rule: str) -> None:
    """Every approve blocker except the protected-grant one is satisfied."""
    plan = _plan_granting(fixtures_dir, tmp_path, rule)
    _to_plan_ready(home_store, sid, plan)
    review = cli.cmd_plan_review(
        ns(session=sid, verdict="pass", reviewer="thinker", concerns=None, note="",
           target=None, plan_digest=_sha256_file(plan)),
        store=home_store,
    )
    assert review.ok, f"setup: plan-review failed: {review.detail}"
    rendering = _write_rendering(tmp_path, "essence text", plan_path=plan)
    presented = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=home_store,
    )
    assert presented.ok, f"setup: present-plan failed: {presented.detail}"
    from agentctl import delivery
    confirmed = cli.cmd_confirm_delivery(
        ns(session=sid, by="fedor", note="hook not installed in test",
           escape_reason=delivery.ESCAPE_HOOK_NOT_INSTALLED),
        store=home_store,
    )
    assert confirmed.ok, f"setup: confirm-delivery failed: {confirmed.detail}"


def _accept_protected_grant(home_store, sid: str, tmp_path: Path) -> subprocess.CompletedProcess:
    home = home_store.root.parent.parent
    return subprocess.run(
        [sys.executable, str(CLI), "accept-protected-grant", "--session", sid,
         "--id", PROTECTED_ENTRY, "--note", "the trunk push is intended"],
        capture_output=True, text=True, timeout=120,
        env={"HOME": str(tmp_path / "fakehome"), "PATH": "/usr/bin:/bin:/usr/local/bin",
             "CLAUDE_CONFIG_DIR": str(home), "CLAUDE_AGENT_HOME": str(home)},
    )


def test_i414_approve_blocked_until_protected_grant_accepted(
    home_store, fixtures_dir, tmp_path, gate_on
):
    sid = "protected-approve"
    _reach_plan_ready_delivered(home_store, fixtures_dir, tmp_path, sid, WILDCARD_PUSH)

    refused = cli.cmd_approve(ns(session=sid, by="user"), store=home_store)

    assert refused.ok is False and refused.node == Node.PLAN_READY.value, (
        f"BEHAVIOUR: approve of a plan granting {WILDCARD_PUSH} succeeded with no acceptance "
        f"of the protected action (node={refused.node})"
    )
    accepted = _accept_protected_grant(home_store, sid, tmp_path)
    assert accepted.returncode == 0, (
        f"BEHAVIOUR: accept-protected-grant is not available to record the user's acceptance: "
        f"{(accepted.stdout + accepted.stderr)[-300:]}"
    )
    approved = cli.cmd_approve(ns(session=sid, by="user"), store=home_store)
    assert approved.node == Node.APPROVED.value, (
        f"BEHAVIOUR: approve is still refused after the acceptance: {approved.detail}"
    )


def test_i414_child_wildcard_does_not_cover_protected_check():
    parent_link = {
        "norm": {"criterion_type": "measurable", "verify_command": PARENT_VERIFY, "expected_exit": 0},
        "norm_digest": "d" * 64,
        "depth": 1,
    }
    trunk_push = {
        "identity": ("final_check", TRUNK_PUSH_CHECK), "origin": "final_check", "source": TRUNK_PUSH_CHECK,
    }

    boundary = gates.parent_norm_boundary(
        parent_link, "d" * 64, [(PARENT_VERIFY, 0), (TRUNK_PUSH_CHECK, 0)], [], [],
        unresolved=[trunk_push], held_unresolved=[("wildcard-tail", WILDCARD_PUSH)], max_depth=3,
    )

    assert gates.PARENT_NORM_DIGEST_MISMATCH not in boundary["reasons"], "setup: the handoff digest does not match"
    assert gates.PARENT_NORM_CHECK_DROPPED not in boundary["reasons"], "setup: the parent's own check is dropped"
    uncovered = [item["source"] for item in boundary["unresolved_changed_commands"]]
    assert TRUNK_PUSH_CHECK in uncovered, (
        f"BEHAVIOUR: the child's {WILDCARD_PUSH} is counted as covering the parent's protected "
        f"check {TRUNK_PUSH_CHECK!r}; uncovered commands {uncovered}, "
        f"eligible={boundary['eligible']}"
    )


def test_narrow_grant_has_no_protected_hit(fixtures_dir, tmp_path):
    plan = _plan_granting(fixtures_dir, tmp_path, NARROW_PUSH)

    for fmt in ("compact", "full"):
        text = _grants_text(plan, fmt)
        assert NARROW_PUSH in text, f"the {fmt} block omits the declared rule"
        assert PROTECTED_ENTRY not in text, f"a narrow branch-push grant is flagged in the {fmt} block"


def test_covered_ordinary_check_stays_covered():
    parent_link = {
        "norm": {"criterion_type": "measurable", "verify_command": PARENT_VERIFY, "expected_exit": 0},
        "norm_digest": "d" * 64,
        "depth": 1,
    }
    status_check = {
        "identity": ("final_check", "git status"), "origin": "final_check", "source": "git status",
    }

    boundary = gates.parent_norm_boundary(
        parent_link, "d" * 64, [(PARENT_VERIFY, 0), ("git status", 0)], [], [],
        unresolved=[status_check], held_unresolved=[("wildcard-tail", "Bash(git status:*)")], max_depth=3,
    )

    assert boundary["unresolved_changed_commands"] == []


from agentctl import own_contour  # noqa: E402
import pytest  # noqa: E402
from agentctl.grants import GrantValidationError, validate_rule  # noqa: E402
from lib import widening_targets  # noqa: E402

ACCEPT_VERB = "accept-protected-grant"
PROTECTED_BLOCKER_FRAGMENT = "protected action"


def _protected_blockers(directive) -> list[str]:
    return [b for b in (directive.data or {}).get("blockers", []) if PROTECTED_BLOCKER_FRAGMENT in b]


def test_protected_action_quotes_match_norm_anchors(tmp_path):
    from agentctl import protected_actions

    entry = protected_actions.CATALOGUE[0]
    assert protected_actions.norm_anchor_problems() == [], "the repo's norms and the catalogue disagree"

    stale = tmp_path / "stale"
    (stale / Path(entry.norm_ref).parent).mkdir(parents=True)
    (stale / entry.norm_ref).write_text(
        protected_actions.anchor_marker(entry.id) + " A reworded norm that no longer says it.\n",
        encoding="utf-8",
    )
    reworded = protected_actions.norm_anchor_problems(stale)
    assert reworded, f"a norm reworded away from the catalogue quote goes unnoticed: {reworded}"

    orphan = tmp_path / "orphan"
    (orphan / Path(entry.norm_ref).parent).mkdir(parents=True)
    (orphan / entry.norm_ref).write_text(
        protected_actions.anchor_marker(entry.id) + " " + entry.quote + "\n", encoding="utf-8")
    assert protected_actions.norm_anchor_problems(orphan) == [], "setup: the matching root reports a problem"
    (orphan / "notes.md").write_text(protected_actions.anchor_marker("no-such-entry") + "\n", encoding="utf-8")
    orphaned = protected_actions.norm_anchor_problems(orphan)
    assert any("no-such-entry" in p for p in orphaned), (
        f"an anchor with no catalogue entry goes unnoticed: {orphaned}"
    )


def test_accept_protected_grant_is_user_authority_verb():
    call = ["python3", "scripts/agentctl-cli.py", ACCEPT_VERB, "--session", "s", "--id", PROTECTED_ENTRY]

    assert ACCEPT_VERB in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS
    assert widening_targets.agentctl_user_authority_call(call) == ACCEPT_VERB
    assert ACCEPT_VERB not in own_contour.OWN_CONTOUR_VERBS
    with pytest.raises(GrantValidationError):
        validate_rule(f"Bash(python3 scripts/agentctl-cli.py {ACCEPT_VERB}:*)")


def test_protected_grant_acceptance_invalidated_by_grant_change(
    home_store, fixtures_dir, tmp_path, gate_on
):
    sid = "protected-invalidated"
    _reach_plan_ready_delivered(home_store, fixtures_dir, tmp_path, sid, WILDCARD_PUSH)
    accepted = _accept_protected_grant(home_store, sid, tmp_path)
    assert accepted.returncode == 0, f"setup: accept-protected-grant failed: {(accepted.stdout + accepted.stderr)[-300:]}"
    plan = home_store.load(sid).plan_path
    assert gates.protected_grant_blockers(home_store.load(sid), plan) == [], "setup: the acceptance did not take"

    original = Path(plan).read_text(encoding="utf-8")
    widened = original.replace(f'allow = ["{WILDCARD_PUSH}"]', f'allow = ["{WILDCARD_PUSH}", "Bash(git status:*)"]', 1)
    assert widened != original, "setup: the plan's grants were not changed"
    Path(plan).write_text(widened, encoding="utf-8")

    assert gates.protected_grant_blockers(home_store.load(sid), plan), (
        "an acceptance recorded for one grant set still counts after the grants changed"
    )
    refused = cli.cmd_approve(ns(session=sid, by="user"), store=home_store)
    assert refused.ok is False and _protected_blockers(refused), (
        f"approve of the widened plan is not held by the protected-grant blocker: {refused.detail} {refused.data}"
    )
    again = _accept_protected_grant(home_store, sid, tmp_path)
    assert again.returncode == 0, f"a new acceptance for the new grants failed: {(again.stdout + again.stderr)[-300:]}"
    assert gates.protected_grant_blockers(home_store.load(sid), plan) == []


def test_agent_approve_blocked_until_protected_grant_accepted(
    home_store, fixtures_dir, tmp_path, gate_on
):
    sid = "protected-agent"
    _reach_plan_ready_delivered(home_store, fixtures_dir, tmp_path, sid, WILDCARD_PUSH)

    refused = cli.cmd_approve(ns(session=sid, by="agent"), store=home_store)

    assert refused.ok is False and _protected_blockers(refused), (
        f"approve --by agent of a plan granting {WILDCARD_PUSH} is not held by the "
        f"protected-grant blocker: {refused.detail} {refused.data}"
    )
    accepted = _accept_protected_grant(home_store, sid, tmp_path)
    assert accepted.returncode == 0, f"setup: accept-protected-grant failed: {(accepted.stdout + accepted.stderr)[-300:]}"
    after = cli.cmd_approve(ns(session=sid, by="agent"), store=home_store)
    assert _protected_blockers(after) == [], (
        f"the protected-grant blocker survives the user's acceptance: {_protected_blockers(after)}"
    )
    agent_call = ["python3", "scripts/agentctl-cli.py", ACCEPT_VERB, "--session", sid, "--id", PROTECTED_ENTRY]
    assert widening_targets.agentctl_user_authority_call(agent_call) == ACCEPT_VERB, (
        "an agent can run accept-protected-grant: it is not a user-authority verb"
    )
