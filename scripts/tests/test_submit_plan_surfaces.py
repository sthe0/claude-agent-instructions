"""Submit-plan and question surfaces (stage 5): the cost-tier floor advisory, two
reachability-lint false positives (glued redirect target, git ref operand), the typed
`[[meta.assumption]]` table registered as `assumed` premises, and `question-list`
printing each unresolved candidate's text. `question-enumerate` stays retired.
"""
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.plan import _reachability_path_tokens, load_plan
from agentctl.store import FileStateStore

from conftest import SUBSTANTIVE_ORDER


def ns(**kw):
    return Namespace(**kw)


def _session(store, sid):
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)


_PLAN_TMPL = """
[meta]
task_id = "demo"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "n/a"
repo_root = "{root}"
{meta_extra}""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "Produce a file"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "{criterion_type}"
done_criterion = "d"
material = "m (traces to R1)"
material_refs = ["m"]
knowledge_refs = ["k"]
knowledge = "kn"
means = "me"
method = "mt"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
invariants = "inv"
capability_required = "c"
conditions = "co"
preconditions = "p"
cost_tier = "{tier}"
verify_command = "{verify}"
{negctl}

[stage.principle]
statement = "s"
source = "src"
derivation = "a distinct derivation clause"
confidence = "high"
refutation = "r"

[[final_check]]
command = "true"
label = "smoke"
"""

_ASSUMPTION = """
[[meta.assumption]]
id = "A1"
text = "the fixture is stable across runs"
basis = "observed three identical runs"
risk = "a flaky fixture would hide the regression"
stage = 1
"""


_NEG = 'negative_control = "false"'


def _write_plan(tmp_path, *, tier, criterion_type="measurable", meta_extra="",
                verify="true", negctl=_NEG):
    plan = tmp_path / "plan.toml"
    plan.write_text(_PLAN_TMPL.format(root=str(tmp_path), tier=tier,
                                      criterion_type=criterion_type,
                                      meta_extra=meta_extra, verify=verify,
                                      negctl=negctl), encoding="utf-8")
    return plan


def _submit(tmp_path, *, tier="small", criterion_type="measurable", meta_extra="",
            sid="surf", store=None, **plan_kw):
    store = store or FileStateStore(tmp_path / "state")
    if store.load(sid) is None:
        _session(store, sid)
    plan = _write_plan(tmp_path, tier=tier, criterion_type=criterion_type,
                       meta_extra=meta_extra, **plan_kw)
    return cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store), store


def _tier(rank):
    """The tier name the module itself ranks at `rank` (0 lowest)."""
    from agentctl.plan import _TIER_RANK
    return sorted(_TIER_RANK, key=_TIER_RANK.__getitem__)[rank]


def _floor_advisories(tmp_path, **plan_kw):
    from agentctl.plan import cost_tier_floor_advisories
    doc = load_plan(str(_write_plan(tmp_path, **plan_kw)))
    return cost_tier_floor_advisories(doc.stages)


_FLOOR_MARK = "floor its typed fields set"


# --- (a) cost-tier floor advisory --------------------------------------------

def test_cost_tier_floor_advisory_at_submit_plan(tmp_path):
    low, mid = _tier(0), _tier(1)
    d, _ = _submit(tmp_path, tier=low)
    advisories = d.data.get("advisories", [])
    floor = [a for a in advisories if _FLOOR_MARK in a]
    assert len(floor) == 1, advisories
    assert "stage 1" in floor[0] and f"'{low}'" in floor[0] and f"'{mid}'" in floor[0]
    assert "negative_control" in floor[0]


def test_floor_advisory_never_blocks_submit_plan(tmp_path):
    d, _ = _submit(tmp_path, tier=_tier(0))
    assert d.ok is True, d.data.get("problems")
    assert d.marker == "PLAN-READY"
    d2, _ = _submit(tmp_path, tier=_tier(1), sid="surf2")
    assert not [a for a in d2.data.get("advisories", []) if _FLOOR_MARK in a]


@pytest.mark.parametrize("verify, negctl, ctype, tier_rank, floor_rank, named", [
    ("pytest -q", "", "measurable", 0, 1, "test runner"),
    ("bash scripts/cap-run.sh -- python3 -m pytest -q -n auto t", "", "measurable", 0, 1,
     "test runner"),
    ("timeout 60 pytest -q", "", "measurable", 0, 1, "test runner"),
    ("true", _NEG, "measurable", 0, 1, "negative_control"),
    ("true", "", "acceptance_review", 0, 2, "acceptance_review"),
    ("true", "", "acceptance_review", 1, 2, "acceptance_review"),
])
def test_cost_tier_floor_is_computed_from_each_typed_input(
        tmp_path, verify, negctl, ctype, tier_rank, floor_rank, named):
    [adv] = _floor_advisories(tmp_path, tier=_tier(tier_rank), criterion_type=ctype,
                              verify=verify, negctl=negctl)
    assert f"'{_tier(floor_rank)}' {_FLOOR_MARK}" in adv and named in adv


@pytest.mark.parametrize("verify, negctl, ctype, tier_rank", [
    ("pytest -q", "", "measurable", 1),          # at the floor
    ("true", "", "acceptance_review", 2),        # at the large floor
    ("true", _NEG, "measurable", 2),             # above the floor
    ("true", "", "measurable", 0),               # no typed input at all
    ("grep pytest notes", "", "measurable", 0),  # a word, not a runner
    ("echo pytest", "", "measurable", 0),
    ("pip show pytest", "", "measurable", 0),
])
def test_no_floor_advisory_at_or_above_the_floor(tmp_path, verify, negctl, ctype, tier_rank):
    assert _floor_advisories(tmp_path, tier=_tier(tier_rank), criterion_type=ctype,
                             verify=verify, negctl=negctl) == []


# --- (b) reachability false positives ----------------------------------------

def test_glued_redirect_target_is_not_a_reachability_path():
    for cmd in ("echo ok >/tmp/out.log", "echo ok 2>/dev/null",
                "echo ok >>/tmp/out.log", "echo ok &>/dev/null",
                "echo ok 1>/tmp/out.log"):
        assert _reachability_path_tokens(cmd) == [], cmd


def test_git_ref_operand_is_not_a_reachability_path(tmp_path):
    (tmp_path / "scripts").mkdir()
    for root in (None, str(tmp_path)):
        for cmd in ("git merge-base HEAD origin/main", "git diff origin/main",
                    "git diff HEAD~1", "git diff origin/main..HEAD",
                    "git log origin/main...HEAD", "git -c core.pager=cat log HEAD"):
            assert _reachability_path_tokens(cmd, root) == [], (cmd, root)
        # true positives stay blocked, with and without a repo to ask
        for cmd, tok in (("git diff scripts/ghost.py", "scripts/ghost.py"),
                         ("git diff scripts/ghost_dir", "scripts/ghost_dir"),
                         ("git diff -- scripts/ghost_dir", "scripts/ghost_dir"),
                         ("git diff HEAD~1 -- scripts/ghost_dir", "scripts/ghost_dir")):
            assert tok in _reachability_path_tokens(cmd, root), (cmd, root)


def test_git_dash_c_value_is_judged_as_a_path():
    assert "scripts/ghostdir" in _reachability_path_tokens("git -C scripts/ghostdir log")


def test_ghost_script_path_still_blocks():
    for cmd in ("python3 scripts/ghost.py", "python3 scripts/ghost.py 2>&1",
                "python3 scripts/ghost.py >/dev/null", "git diff scripts/ghost.py",
                "git diff -- scripts/ghost_dir", "git diff scripts/ghost_dir"):
        assert any(t.startswith("scripts/ghost") for t in _reachability_path_tokens(cmd)), cmd


# --- (c) typed assumption table (#357) ---------------------------------------

def test_typed_plan_assumption_registers_as_assumed(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    d, store = _submit(tmp_path, meta_extra=_ASSUMPTION)
    assert d.ok is True, d.data
    doc = load_plan(str(tmp_path / "plan.toml"))
    assert [(a.id, a.stage) for a in doc.meta.assumptions] == [("A1", 1)]

    def rows():
        bag = store.load("surf").plugins["premise"]
        return [q for q in bag["questions"] if q["id"] == "assume-A1"]

    [row] = rows()
    assert row["disposition"] == "assumed"
    assert row["basis"] == "observed three identical runs"
    assert row["risk"] == "a flaky fixture would hide the regression"
    assert row["question"] == "the fixture is stable across runs"
    assert row["disposed_at_key"] != ""
    # idempotent across a re-submission
    _submit(tmp_path, meta_extra=_ASSUMPTION, store=store)
    assert len(rows()) == 1


# --- (c) question-list prints candidate text ---------------------------------

def test_question_list_prints_candidate_text(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    _, store = _submit(tmp_path)
    state = store.load("surf")
    state.plugins["premise"].setdefault("candidates", []).append({
        "id": "C1", "statement": "does the lint also see heredoc bodies?",
        "disposition": "raised", "reason": "", "question": "", "target": "",
    })
    store.save(state)
    d = cli.cmd_question_list(ns(session="surf", format=None), store=store)
    assert "C1" in d.detail and "does the lint also see heredoc bodies?" in d.detail
    assert [c["id"] for c in d.data["candidates"]] == ["C1"]


# --- (d) #265: enumeration stays retired -------------------------------------

def test_question_enumerate_stays_retired(tmp_path):
    store = FileStateStore(tmp_path / "state")
    with pytest.raises(SystemExit) as e:
        cli.cmd_question_enumerate(ns(session="x", command="question-enumerate"),
                                   store=store)
    assert e.value.code == 2
    d, _ = _submit(tmp_path, sid="surf3")
    assert d.ok is True
    assert not [a for a in d.data.get("advisories", []) if "question-enumerate" in a]
