"""A linked child plan that serves the parent stage's norm is approved on that stage's authority.

A child session spawned for a parent stage holds the norm handoff (`parent_link`). A plan
of its own that declares `[meta.order] parent_norm = <norm_digest>` self-approves with
`approve --by agent` on the basis `parent_norm` exactly when five conditions hold: the
digest is the adopted handoff's, the plan's final checks carry the parent stage's
`verify_command` byte-for-byte (after stripping surrounding whitespace) with the same
`expected_exit`, the parent stage is measurable, every resource of the plan is covered
by a handoff grant (a specialist resource is exempt), and the depth is within the cap.
Each failing condition is named by a reason code; a plan without `parent_norm` keeps every
digest and every autonomy result it had.

Tests drive the engine through `agentctl` CLI argv (`cli.main`) against a real
`FileStateStore`, with the handoff written by `norm_handoff` exactly as a spawn writes it.
The five tests the stage's negative control names drive only entry points that exist on
the base tree and assert the behaviour under repair on a `BEHAVIOUR:`-prefixed assertion,
so on base they fail there and not at collection or setup.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path

import pytest

from agentctl import cli, norm_handoff, plan_resources
from agentctl import resources as agentctl_resources
from agentctl.config import Thresholds
from agentctl.plan import _order_place_digest, _venue_for, load_plan, order_digest, plan_meta_digest
from agentctl.stage_norm import StageNorm
from agentctl.store import FileStateStore
from conftest import SUBSTANTIVE_ORDER, declare_venue

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"
PARENT_CHECK = "python3 -m pytest -q tests/test_mod.py"
NOT_A_DIGEST = "f" * 64
WILDCARD_RULE = "Bash(python3 scripts/tool.py:*)"

# The digests of `FIXTURE` with `SUBSTANTIVE_ORDER` spliced before its first stage, read on
# the base tree a80e19d6, before `Order` had a `parent_norm`.
BASE_ORDER_DIGEST = "925175c938a6fd8d8e4f2b6e0e2c4ea2af601d39cc70d4cd7428d05fd5d8b4d1"
BASE_PLAN_META_DIGEST = "2e57639aec5254f4bc21092d3ef42f9f87b07e9d6a443fb7cf3fd79847fcf2f7"
BASE_ORDER_PLACE_DIGEST = "b7e929674c58e90dce3c0e52a316984e4beb3cf8989f76387c48fed52bea5215"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    monkeypatch.delenv("AGENTCTL_PARENT_DIFFICULTY", raising=False)
    monkeypatch.delenv("AGENTCTL_NORM_HANDOFF", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _spliced(*, parent_norm: str | None = None, order_text: str = SUBSTANTIVE_ORDER,
             final_checks=()) -> str:
    """The two-stage fixture with a `[meta.order]` (and, when given, a `parent_norm` and
    `[[final_check]]` tables) -- varied on exactly one axis per test."""
    order = order_text
    if parent_norm is not None:
        order = order.replace("functional_place =", f'parent_norm = "{parent_norm}"\nfunctional_place =')
    head, sep, tail = FIXTURE.read_text(encoding="utf-8").partition("\n[[stage]]")
    text = head + "\n" + order + sep + tail
    for command, expected_exit in final_checks:
        text += f'\n[[final_check]]\ncommand = "{command}"\nexpected_exit = {expected_exit}\n'
    return text


def _parent_plan(tmp_path: Path, *, verify: str | None = PARENT_CHECK,
                 criterion_type: str = "measurable") -> str:
    """The parent's own plan: the fixture with stage 1's check and criterion type set."""
    head, sep, tail = FIXTURE.read_text(encoding="utf-8").partition("\n[[stage]]")
    tail = tail.replace('criterion_type = "measurable"\n',
                        f'criterion_type = "{criterion_type}"\n', 1)
    if verify is not None:
        tail = tail.replace("depends_on = []\n", f'depends_on = []\nverify_command = "{verify}"\n', 1)
    path = tmp_path / "parent.toml"
    path.write_text(declare_venue(head + "\n" + SUBSTANTIVE_ORDER + sep + tail, tmp_path),
                    encoding="utf-8")
    return str(path)


def _parent_norm(tmp_path: Path, **kw) -> StageNorm:
    doc = load_plan(_parent_plan(tmp_path, **kw), strict=False)
    stage = next(s for s in doc.stages if s.index == 1)
    return StageNorm.from_stage(stage, {}, doc=doc)


def _covering_grants(plan_path: str, *, without: str | None = None) -> list[dict]:
    """Handoff grant entries that cover every file resource of the plan at `plan_path`,
    less the one whose path ends with `without` and every directory entry that would still
    cover it (the plan's venue is one), and the unresolved rules the plan's
    output artifacts derive (the parent stage derived the same ones for the same
    artifacts). A declared unresolved rule is not covered: the tests that need it add it."""
    doc = load_plan(plan_path, strict=False)
    view = plan_resources.compute_boundary_view(doc, order_digest(doc), venue=_venue_for(doc))
    entries = [{"path": r.path, "mode": r.mode} for r in view.resources
               if isinstance(r, agentctl_resources.FileResource)]
    dropped = [e["path"] for e in entries if without and e["path"].endswith(without)]
    entries = [e for e in entries
               if not any(e["path"] == d or d.startswith(e["path"].rstrip("/") + "/")
                          for d in dropped)]
    return entries + [{"rule": item["source"]} for item in view.unresolved
                      if item["origin"] == "output_artifact"]


class Child:
    """One child session, driven through the CLI, with a handoff the parent 'wrote'."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.root = tmp_path / "state"
        self.store = FileStateStore(self.root)
        self.norm = _parent_norm(tmp_path)
        self.digest = self.norm.norm_digest()

    def link(self, *, norm: StageNorm | None = None, grants=(), depth: int = 1) -> None:
        record = norm_handoff.build_record(
            parent_session="parent-s", stage_index=1, kind="developer",
            norm=norm or self.norm, grants=list(grants), depth=depth, parent_lineage=None,
        )
        path, sha = norm_handoff.write_record(self.tmp / "handoffs", record)
        self.monkeypatch.setenv(norm_handoff.NORM_HANDOFF_ENV, norm_handoff.format_env_value(path, sha))

    def run(self, cmd: str, **flags) -> dict:
        argv = ["--state-root", str(self.root), cmd.replace("_", "-")]
        for name, value in flags.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif value is not None and value is not False:
                argv.extend([flag, str(value)])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exit_:
                return {"ok": False, "rc": exit_.code, "marker": None, "data": {},
                        "detail": err.getvalue()}
        result = json.loads(out.getvalue())
        result["rc"] = rc
        return result

    def write(self, text: str, name: str = "child.toml") -> str:
        path = self.tmp / name
        path.write_text(declare_venue(text, self.tmp), encoding="utf-8")
        return str(path)

    def submit(self, plan: str, sid: str = "child-1") -> dict:
        """Start, classify and plan a substantive session and submit `plan`."""
        self.run("start", session=sid, task=f"task-{sid}", goal="g", done_criterion="dc")
        self.run("classify", session=sid, architectural=True, files=5, changed_lines=200,
                 wall_clock_min=60)
        self.run("plan", session=sid)
        return self.run("submit_plan", session=sid, plan=plan)

    def state(self, sid: str = "child-1"):
        return self.store.load(sid)


@pytest.fixture
def child(tmp_path, monkeypatch):
    return Child(tmp_path, monkeypatch)


def _autonomy(d: dict) -> dict:
    return (d.get("data") or {}).get("autonomy") or {}


def _linked_submit(child: Child, *, final_checks=((PARENT_CHECK, 0),), parent_norm=None,
                   grants=None, depth: int = 1, norm: StageNorm | None = None,
                   link: bool = True, plan_text: str | None = None) -> tuple[dict, str]:
    """Link the child to the parent stage (unless `link` is False), write a plan that
    declares `parent_norm` (default: the parent's digest) and submit it."""
    text = plan_text or _spliced(parent_norm=parent_norm or child.digest, final_checks=final_checks)
    plan = child.write(text)
    if link:
        child.link(norm=norm, grants=_covering_grants(plan) if grants is None else grants, depth=depth)
    return child.submit(plan), plan


def _refusal(child: Child, code: str, *, sid: str = "child-1") -> None:
    refused = child.run("approve", session=sid, by="agent")
    assert refused["ok"] is False, f"approve --by agent was admitted: {refused}"
    blockers = (refused.get("data") or {}).get("blockers") or []
    assert any(code in b for b in blockers), f"no blocker names {code!r}: {blockers}"


def _assert_named(d: dict, code: str) -> None:
    autonomy = _autonomy(d)
    assert code in (autonomy.get("reasons") or []), (
        f"BEHAVIOUR: the verdict does not name {code!r}: eligible={autonomy.get('eligible')} "
        f"reasons={autonomy.get('reasons')}"
    )
    assert autonomy.get("eligible") is False
    assert d["marker"] == "PLAN-READY"


# --- the approval --------------------------------------------------------------------


def test_in_norm_child_plan_self_approves(child):
    d, _ = _linked_submit(child)
    assert child.state().parent_link is not None, "the handoff was not adopted: setup defect"
    autonomy = _autonomy(d)
    assert autonomy.get("action") == "self_approve", (
        f"BEHAVIOUR: a plan inside the parent's norm and grants is not routed to self-approval: "
        f"action={autonomy.get('action')} reasons={autonomy.get('reasons')} "
        f"unresolved={autonomy.get('unresolved_changed_commands')}"
    )
    assert autonomy["basis"] == "parent_norm"
    approved = child.run("approve", session="child-1", by="agent")
    assert approved["ok"] is True, approved
    state = child.state()
    assert state.approval.passed is True
    rows = [h for h in state.history if h.get("event") == "approve"]
    assert rows and rows[-1].get("basis") == "parent_norm"
    assert rows[-1].get("by") == "agent"


def test_digest_mismatch_is_named(child):
    d, _ = _linked_submit(child, parent_norm=NOT_A_DIGEST)
    _assert_named(d, "parent-norm-digest-mismatch")
    _refusal(child, "parent-norm-digest-mismatch")


def test_acceptance_review_parent_is_named(child, tmp_path):
    norm = _parent_norm(tmp_path, verify=None, criterion_type="acceptance_review")
    child.norm, child.digest = norm, norm.norm_digest()
    d, _ = _linked_submit(child)
    _assert_named(d, "parent-acceptance-review")
    _refusal(child, "parent-acceptance-review")


def test_dropped_parent_check_is_named(child):
    d, _ = _linked_submit(child, final_checks=(("true", 0),))
    _assert_named(d, "parent-check-dropped")
    _refusal(child, "parent-check-dropped")


def test_parent_check_with_other_expected_exit_is_named(child):
    d, _ = _linked_submit(child, final_checks=((PARENT_CHECK, 1),))
    _assert_named(d, "parent-check-dropped")


def test_uncovered_resource_is_named(child, tmp_path):
    plan = child.write(_spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),)))
    narrowed = _covering_grants(plan, without="/tests/test_mod.py")
    assert narrowed and len(narrowed) < len(_covering_grants(plan)), "grants were not narrowed: setup defect"
    child.link(grants=narrowed)
    d = child.submit(plan)
    _assert_named(d, "resource-not-in-handoff")
    names = [e["name"] for e in _autonomy(d).get("extra_resources") or []]
    assert any("/tests/test_mod.py" in n for n in names), names
    _refusal(child, "resource-not-in-handoff")


def _with_stage_grant(text: str, *rules: str) -> str:
    quoted = ", ".join(f'"{r}"' for r in rules)
    marker = 'output_artifacts = ["mod.py"]\n'
    assert marker in text, "fixture changed: setup defect"
    return text.replace(marker, marker + f"\n[stage.grants]\nallow = [{quoted}]\n", 1)


def test_wildcard_tail_stage_grant_is_refused(child):
    text = _with_stage_grant(
        _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),)), WILDCARD_RULE)
    d, _ = _linked_submit(child, plan_text=text)
    _assert_named(d, "unresolved-not-in-handoff")
    sources = [c["source"] for c in _autonomy(d).get("unresolved_changed_commands") or []]
    assert WILDCARD_RULE in sources, sources
    _refusal(child, "unresolved-not-in-handoff")


def test_wildcard_tail_stage_grant_held_by_the_handoff_is_covered(child):
    text = _with_stage_grant(
        _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),)), WILDCARD_RULE)
    plan = child.write(text)
    child.link(grants=[*_covering_grants(plan), {"rule": WILDCARD_RULE}])
    d = child.submit(plan)
    assert _autonomy(d).get("eligible") is True, _autonomy(d)
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


def _with_stage_verify(text: str, command: str) -> str:
    marker = "depends_on = []\n"
    assert marker in text, "fixture changed: setup defect"
    return text.replace(marker, marker + f'verify_command = "{command}"\n', 1)


def test_unresolved_stage_verify_command_is_covered_only_by_the_identical_handoff_rule(child):
    """The identity `resolve_rule_grant` gives the handoff's entry is the one
    `compute_boundary_view` gives the plan's item, so a handoff carrying exactly
    `Bash(<that command>)` covers it and no other."""
    command = "python3 mod.py --check"
    text = _with_stage_verify(
        _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),)), command)
    d, plan = _linked_submit(child, plan_text=text)
    _assert_named(d, "unresolved-not-in-handoff")
    sources = [c["source"] for c in _autonomy(d).get("unresolved_changed_commands") or []]
    assert command in sources, sources
    _refusal(child, "unresolved-not-in-handoff")

    child.link(grants=[*_covering_grants(plan), {"rule": f"Bash({command})"}])
    d = child.submit(plan, sid="child-2")
    assert _autonomy(d).get("eligible") is True, _autonomy(d)
    assert child.run("approve", session="child-2", by="agent")["ok"] is True


def test_depth_over_cap_is_named(child):
    d, _ = _linked_submit(child, depth=Thresholds().max_recursion_depth + 1)
    _assert_named(d, "depth-over-cap")


def test_unlinked_parent_norm_is_named(child):
    d, _ = _linked_submit(child, link=False)
    assert child.state().parent_link is None, "an ambient handoff leaked into the session: setup defect"
    _assert_named(d, "not-linked")
    _refusal(child, "not-linked")


def test_parent_without_verify_command_is_refused(child, tmp_path):
    norm = _parent_norm(tmp_path, verify=None)
    child.norm, child.digest = norm, norm.norm_digest()
    d, _ = _linked_submit(child, final_checks=(("true", 0),))
    _assert_named(d, "parent-check-dropped")
    _refusal(child, "parent-check-dropped")


def test_review_gate_still_blocks_child(child, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    d, plan = _linked_submit(child)
    assert _autonomy(d).get("eligible") is True, _autonomy(d)
    refused = child.run("approve", session="child-1", by="agent")
    assert refused["ok"] is False, f"approve --by agent skipped the plan review: {refused}"
    blockers = (refused.get("data") or {}).get("blockers") or []
    assert any("review" in b.lower() for b in blockers), blockers
    assert not any("parent-" in b or "not-linked" in b for b in blockers), blockers
    reviewed = child.run("plan_review", session="child-1", verdict="pass", reviewer="thinker",
                         target=plan, plan_digest=hashlib.sha256(Path(plan).read_bytes()).hexdigest())
    assert reviewed["ok"] is True, reviewed
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


# --- a plan that does not declare parent_norm is as it was ---------------------------


def test_plan_without_parent_norm_keeps_base_digests(tmp_path):
    path = tmp_path / "plain.toml"
    path.write_text(_spliced(), encoding="utf-8")
    doc = load_plan(str(path), strict=False)
    assert doc.meta.order is not None
    assert getattr(doc.meta.order, "parent_norm", None) in (None, "")
    assert order_digest(doc) == BASE_ORDER_DIGEST
    assert plan_meta_digest(doc) == BASE_PLAN_META_DIGEST
    assert _order_place_digest(doc) == BASE_ORDER_PLACE_DIGEST


def test_plan_without_parent_norm_keeps_autonomy(child, tmp_path):
    """An adopted handoff does not make an ordinary plan self-approvable: the plan is a
    fresh order, so the user decides, exactly as without a handoff."""
    plan = child.write(_spliced(final_checks=((PARENT_CHECK, 0),)))
    child.link(grants=_covering_grants(plan))
    d = child.submit(plan)
    assert child.state().parent_link is not None, "the handoff was not adopted: setup defect"
    autonomy = _autonomy(d)
    assert autonomy["action"] == "await_user_approval"
    assert autonomy["eligible"] is False
    assert "basis" not in autonomy
    assert d["marker"] == "PLAN-READY"
    refused = child.run("approve", session="child-1", by="agent")
    assert refused["ok"] is False


# --- a replan of a parent_norm plan is judged on the same basis ---------------------


def _replan_kinds(child: Child, old_text: str, new_text: str) -> tuple[str, str]:
    """`diff_plans`' kind and the kind the engine applies, for a replan of the submitted
    `old_text` to `new_text` in a session linked to the parent stage."""
    from agentctl.plan import diff_plans

    old_path, new_path = child.write(old_text, "old.toml"), child.write(new_text, "new.toml")
    child.link(grants=_covering_grants(old_path))
    child.submit(old_path)
    old, new = load_plan(old_path, strict=False), load_plan(new_path, strict=False)
    return diff_plans(old, new, relax_verify_identity=False), cli._replan_kind(child.state(), old, new)


def _tightened(text: str) -> str:
    image = 'expected_result_image = "module file exists and imports cleanly"'
    assert image in text, "fixture changed: setup defect"
    return text.replace(image, image[:-1] + ' with no warnings"', 1)


def test_replan_dropping_the_parent_check_is_substantive(child):
    old = _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),))
    new = _spliced(parent_norm=child.digest, final_checks=(("true", 0),))
    _, applied = _replan_kinds(child, old, new)
    assert applied == "substantive", f"BEHAVIOUR: dropping the parent check applies as {applied!r}"


def test_replan_removing_parent_norm_is_substantive(child):
    old = _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),))
    new = _spliced(final_checks=(("true", 0),))
    diff_kind, applied = _replan_kinds(child, old, new)
    assert diff_kind == "refinement", f"setup: the edit is no refinement ({diff_kind!r})"
    assert applied == "substantive", f"BEHAVIOUR: dropping parent_norm applies as {applied!r}"


def test_replan_adding_an_unresolved_grant_is_substantive(child):
    old = _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),))
    new = _with_stage_grant(old, WILDCARD_RULE)
    _, applied = _replan_kinds(child, old, new)
    assert applied == "substantive", f"BEHAVIOUR: an uncovered grant applies as {applied!r}"


def test_refinement_of_an_ineligible_parent_norm_plan_re_enters_the_gate(child):
    old = _spliced(parent_norm=child.digest, final_checks=(("true", 0),))
    diff_kind, applied = _replan_kinds(child, old, _tightened(old))
    assert diff_kind == "refinement", f"setup: the edit is no refinement ({diff_kind!r})"
    assert applied == "substantive", f"BEHAVIOUR: an ineligible plan's refinement stays {applied!r}"


def test_refinement_of_an_eligible_parent_norm_plan_stays_a_refinement(child):
    old = _spliced(parent_norm=child.digest, final_checks=((PARENT_CHECK, 0),))
    diff_kind, applied = _replan_kinds(child, old, _tightened(old))
    assert (diff_kind, applied) == ("refinement", "refinement")


# --- the gate itself -----------------------------------------------------------------


def _link_dict(norm: StageNorm, *, depth: int = 1) -> dict:
    return {"norm": norm.to_dict(), "norm_digest": norm.norm_digest(), "grants": [], "depth": depth}


def _gate(link, digest, final_checks, *, resources=(), held=(), unresolved=(), held_unresolved=(),
          protected=None) -> dict:
    from agentctl import gates

    return gates.parent_norm_boundary(
        link, digest, final_checks, list(resources), list(held), unresolved=list(unresolved),
        held_unresolved=list(held_unresolved), max_depth=5, protected=protected)


def test_gate_strips_whitespace_around_the_parent_check(tmp_path):
    from agentctl import gates

    norm = _parent_norm(tmp_path)
    verdict = _gate(_link_dict(norm), norm.norm_digest(), [(f"  {PARENT_CHECK}\n", 0)])
    assert verdict["eligible"] is True, verdict
    assert verdict["basis"] == "parent_norm"
    changed = _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK + " -x", 0)])
    assert changed["reasons"] == ["parent-check-dropped"]


def test_gate_reports_every_failing_condition(tmp_path):
    norm = _parent_norm(tmp_path)
    verdict = _gate(_link_dict(norm, depth=9), NOT_A_DIGEST, [])
    assert verdict["reasons"] == [
        "parent-norm-digest-mismatch", "parent-check-dropped", "depth-over-cap"]
    assert verdict["reason_texts"][0].startswith("parent-norm-digest-mismatch: ")
    assert _gate(None, NOT_A_DIGEST, [])["reasons"] == ["not-linked"]


def test_gate_exempts_a_specialist_resource(tmp_path):
    norm = _parent_norm(tmp_path)
    verdict = _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)],
                    resources=[agentctl_resources.SpecialistResource("developer")])
    assert verdict["eligible"] is True, verdict
    assert verdict["extra_resources"] == []


def test_gate_never_covers_a_protected_surface_by_a_handoff_grant(tmp_path):
    norm = _parent_norm(tmp_path)
    surface = str(tmp_path / "ledger")
    resource = agentctl_resources.FileResource(surface, "write")
    verdict = _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)],
                    resources=[resource], held=[resource], protected=[surface])
    assert verdict["reasons"] == ["resource-not-in-handoff"]
    unprotected = _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)],
                        resources=[resource], held=[resource])
    assert unprotected["eligible"] is True, unprotected


def test_gate_covers_an_unresolved_item_only_by_an_identical_identity(tmp_path):
    norm = _parent_norm(tmp_path)
    item = {"identity": ["wildcard-tail", WILDCARD_RULE], "origin": "declared", "source": WILDCARD_RULE}
    uncovered = _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)], unresolved=[item])
    assert uncovered["reasons"] == ["unresolved-not-in-handoff"]
    assert [c["source"] for c in uncovered["unresolved_changed_commands"]] == [WILDCARD_RULE]
    other = [["wildcard-tail", "Bash(python3 scripts/other.py:*)"]]
    assert _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)], unresolved=[item],
                 held_unresolved=other)["reasons"] == ["unresolved-not-in-handoff"]
    held = [["wildcard-tail", WILDCARD_RULE]]
    assert _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)], unresolved=[item],
                 held_unresolved=held)["eligible"] is True


def test_gate_exempts_only_the_final_check_that_is_the_parent_norm(tmp_path):
    norm = _parent_norm(tmp_path)
    parent_check = {"identity": ["final-check", PARENT_CHECK], "origin": "final_check",
                    "source": f" {PARENT_CHECK}\n"}
    assert _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)],
                 unresolved=[parent_check])["eligible"] is True
    as_stage_check = {**parent_check, "origin": "verify_command"}
    assert _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)],
                 unresolved=[as_stage_check])["reasons"] == ["unresolved-not-in-handoff"]
    other_check = {"identity": ["final-check", "true"], "origin": "final_check", "source": "true"}
    assert _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 0)],
                 unresolved=[other_check])["reasons"] == ["unresolved-not-in-handoff"]
    wrong_exit = _gate(_link_dict(norm), norm.norm_digest(), [(PARENT_CHECK, 1)],
                       unresolved=[parent_check])
    assert wrong_exit["reasons"] == ["parent-check-dropped", "unresolved-not-in-handoff"]


def test_declaring_parent_norm_moves_the_order_keys(tmp_path):
    plain = tmp_path / "plain.toml"
    plain.write_text(_spliced(), encoding="utf-8")
    linked = tmp_path / "linked.toml"
    linked.write_text(_spliced(parent_norm=NOT_A_DIGEST), encoding="utf-8")
    a, b = load_plan(str(plain), strict=False), load_plan(str(linked), strict=False)
    assert b.meta.order.parent_norm == NOT_A_DIGEST
    assert order_digest(a) != order_digest(b)
    assert _order_place_digest(a) != _order_place_digest(b)
