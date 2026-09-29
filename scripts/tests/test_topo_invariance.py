"""Byte-invariance pin for the pre-existing plan surface: `stage_element_keys`/
`plan_meta_digest`/`plan_content_digest` and `render_plan_md`/`render_stages_md`/
`render_stage_brief` must produce identical output for a fixed fixture plan on
`origin/main` and on this tree -- including `render_plan_md` after it started composing
the factored `render_meta_md`/`render_order_md`/`render_final_checks_md` helpers instead
of building its markdown inline. The fixture carries an `[meta.order]` with a `coverage`
map, a `[[final_check]]`, an explicit `[[stage.supplies]]` edge, and an interface_empty
SUPPLIER (stage 1, a whitespace-only `expected_result_image`) that stage 2 relies on, so
the consumer-side "Depends on" rendering of an empty interface is pinned too.

This file travels ALONE into a fresh `git archive origin/main` checkout and runs there,
so it:
  - imports ONLY names that already exist on origin/main;
  - never shells out to git or any other tree at run time -- every EXPECTED_* value below
    is a LITERAL captured against origin/main (commit 1e84be28): each file of
    scripts/agentctl/, scripts/lib/, scripts/difficulty_channel/, scripts/proc_tree.py
    and scripts/tests/conftest.py was extracted with
    `git show 1e84be28:<path> > <capture-tree>/<path>`, and the six functions below were
    evaluated on the fixture inside that tree under pytest;
  - embeds the fixture TOML inline rather than reading a sibling fixtures file, since no
    other file travels with this one into the archived tree.
"""
from __future__ import annotations

from pathlib import Path

from agentctl.plan import (
    load_plan,
    plan_content_digest,
    plan_meta_digest,
    stage_element_keys,
)
from agentctl.render import render_plan_md, render_stage_brief, render_stages_md

FIXTURE_TOML = """
[meta]
weight_class = "small_change"
task_id = "invariance-fixture"
goal = "Pin renderer and digest output across origin/main and the delivered tree"
done_criterion = "both stages PASSED"
criterion_type = "measurable"

[meta.order]
customer_id = "user"
customer = "the position that posed this fixture's task"
functional_place = "pin renderer byte-identity across a topo-review change"

[[meta.order.requirements]]
id = "R1"
text = "the fixture plan renders identically on origin/main and the delivered tree"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]

[[final_check]]
command = "true"
label = "fixture final check"

[[stage]]
index = 1
title = "Scaffold module"
executor = "spawn:developer"
expected_result_image = "   "
criterion_type = "measurable"
done_criterion = "python -c 'import mod' exits 0"
depends_on = []
output_artifacts = ["mod.py"]

[[stage]]
index = 2
title = "Add tests"
executor = "spawn:developer"
expected_result_image = "tests exist and pass"
criterion_type = "measurable"
done_criterion = "pytest tests/test_mod.py green"
[[stage.supplies]]
on = 1
"""

EXPECTED_STAGE1_KEYS = {
    "": "41c468d063eacb2316e1b592d80690a00986913a0c0823eadc9db4059d0c0d5d",
    "capability": "0e2e2f0e2c8152711da9f72cba106cc7ca515e7c5817efbd9b57c7dfe937ccf2",
    "conditions": "90d99c825e9d0d88c3c58b9e2c4f5b3c314b24dfaacc5fe8719d18f6ecd2c3b1",
    "control": "41c468d063eacb2316e1b592d80690a00986913a0c0823eadc9db4059d0c0d5d",
    "criterion": "c0a8633894ba61ec1aed2f0e8bd1cf788b47bb71eb42f45a339be51c6e59e31f",
    "done_criterion": "3b407521be74892303f5fab3720df694f22d08b78ffd2ffe263fae1d5db08818",
    "executor": "fd0e2225fa989c71b242befbd56873198a3f1ec9b139638649f2aecba9c4bd04",
    "invariants": "5b8f07296130f3e35996f24d4526f5e3e15d88f5c04756540645ba093d1af754",
    "knowledge": "aac0957086150b04c6fa4f967d8395f6ad524ca3ff6f2ee1cddc0ce2432733bc",
    "material": "34bcc81e20ec56338eb457038a039d07c8e2fbb832e5b677403b2636af3e007e",
    "means": "d24c2a580b10d5cf05fa54fc4fedba63bae251f8f8bba755dc62de85deee8e9f",
    "method": "db283606671d29b294faadca4e7df0ac587ba0eadcbb3ae9aa035bb64140e33f",
    "order": "41c468d063eacb2316e1b592d80690a00986913a0c0823eadc9db4059d0c0d5d",
    "preconditions": "0df4d6a1f321d289d957288ee418f09448981d251ea59ed01099947946f0b30f",
    "principle": "f44d9a6c8e645981fe22a84c27f9849be17f6630e09576eeaee560cc0e897a08",
    "procedure": "907b7e0c97698d6453dcec4c3658a5eb3c7442f4c6ea9968c1b263561d4ec41b",
    "requirements": "41c468d063eacb2316e1b592d80690a00986913a0c0823eadc9db4059d0c0d5d",
    "result": "5647b5160d3ecb70c383d4c4a141d0de4d19dd5780e2eeb88dba9511a463a8dd",
}

EXPECTED_STAGE2_KEYS = {
    "": "e67d637c67e1b805d5a61ea3a726f35922e63bf66a86cda6a778d504c3666bf7",
    "capability": "0e2e2f0e2c8152711da9f72cba106cc7ca515e7c5817efbd9b57c7dfe937ccf2",
    "conditions": "90d99c825e9d0d88c3c58b9e2c4f5b3c314b24dfaacc5fe8719d18f6ecd2c3b1",
    "control": "e67d637c67e1b805d5a61ea3a726f35922e63bf66a86cda6a778d504c3666bf7",
    "criterion": "43d2e8ab1f8296e28c9e16ed7d399b444939d224c3dce324aff3e3e9ef210c75",
    "done_criterion": "bbe791d2bee2f68436611f173c70827b486ba82e698c09f2cfc1d7ac91245dc8",
    "executor": "fd0e2225fa989c71b242befbd56873198a3f1ec9b139638649f2aecba9c4bd04",
    "invariants": "5b8f07296130f3e35996f24d4526f5e3e15d88f5c04756540645ba093d1af754",
    "knowledge": "aac0957086150b04c6fa4f967d8395f6ad524ca3ff6f2ee1cddc0ce2432733bc",
    "material": "bd5dc96dd518d417ac99a9128dffdcde31b22ee786d5110ad566748493a1dbc1",
    "means": "d24c2a580b10d5cf05fa54fc4fedba63bae251f8f8bba755dc62de85deee8e9f",
    "method": "db283606671d29b294faadca4e7df0ac587ba0eadcbb3ae9aa035bb64140e33f",
    "order": "e67d637c67e1b805d5a61ea3a726f35922e63bf66a86cda6a778d504c3666bf7",
    "preconditions": "0df4d6a1f321d289d957288ee418f09448981d251ea59ed01099947946f0b30f",
    "principle": "f44d9a6c8e645981fe22a84c27f9849be17f6630e09576eeaee560cc0e897a08",
    "procedure": "907b7e0c97698d6453dcec4c3658a5eb3c7442f4c6ea9968c1b263561d4ec41b",
    "requirements": "e67d637c67e1b805d5a61ea3a726f35922e63bf66a86cda6a778d504c3666bf7",
    "result": "ef43a015b4af7a4218c892bf0bde2c1774d176eb2e3b026e0ab326893341664a",
}

EXPECTED_PLAN_META_DIGEST = (
    "dea3a52ffa9d80187169a110c398381a86dce496254b2eb41dc5bc3f5d702dde"
)
EXPECTED_PLAN_CONTENT_DIGEST = (
    "372c5a596932fba58e586c75f7cb15706c99f6b2812371f3fcab4660ad1a8ccc"
)

EXPECTED_RENDER_STAGE_BRIEF_2 = (
    "# Plan: Pin renderer and digest output across origin/main and the delivered tree\n"
    "\n"
    "- **Task id:** invariance-fixture\n"
    "- **Weight class:** small_change\n"
    "- **Overall done criterion:** both stages PASSED\n"
    "- **Overall criterion type:** measurable\n"
    "\n"
    "This is a PROJECTED BRIEF of stage 2 only, out of 2 stage(s) in the plan — the "
    "other stages are not shown and are not this step's concern.\n"
    "\n"
    "## Stage 2: Add tests\n"
    "\n"
    "- **Executor:** spawn:developer\n"
    "- **Expected result image:** tests exist and pass\n"
    "- **Criterion type:** measurable\n"
    "- **Done criterion:** pytest tests/test_mod.py green\n"
    "- **Depends on** (direct dependencies only; see their own stage for detail):\n"
    "  - Stage 1: Scaffold module\n"
    "    - **Its expected result image:**    \n"
    "    - **Its output artifacts:** mod.py\n"
    "- **Supplies** (raw provision edges this stage declares):\n"
    "  - on stage 1\n"
    "\n"
    "## Final verification (labels only — this stage does not need the commands; "
    "see the full plan file for those)\n"
    "\n"
    "- fixture final check\n"
)

EXPECTED_RENDER_PLAN_MD = (
    "# Plan: Pin renderer and digest output across origin/main and the delivered tree\n"
    "\n"
    "- **Task id:** invariance-fixture\n"
    "- **Weight class:** small_change\n"
    "- **Done criterion:** both stages PASSED\n"
    "- **Criterion type:** measurable\n"
    "\n"
    "## Order\n"
    "\n"
    "- **Customer:** the position that posed this fixture's task (`user`)\n"
    "- **Functional place:** pin renderer byte-identity across a topo-review change\n"
    "- **Requirements:**\n"
    "  - **R1**: the fixture plan renders identically on origin/main and the delivered "
    "tree\n"
    "    - **Derivation:** *(none)*\n"
    "\n"
    "## Stage 1: Scaffold module\n"
    "\n"
    "- **Executor:** spawn:developer\n"
    "- **Expected result image:**    \n"
    "- **Criterion type:** measurable\n"
    "- **Done criterion:** python -c 'import mod' exits 0\n"
    "- **Grants (file-access scope):**\n"
    "  - derived allow: Bash(python3 mod.py:*), Edit(//./mod.py)\n"
    "\n"
    "## Stage 2: Add tests\n"
    "\n"
    "- **Executor:** spawn:developer\n"
    "- **Expected result image:** tests exist and pass\n"
    "- **Criterion type:** measurable\n"
    "- **Done criterion:** pytest tests/test_mod.py green\n"
    "- **Depends on:** 1\n"
    "\n"
    "## Final verification\n"
    "\n"
    "- fixture final check: `true` (expected exit 0)\n"
)

EXPECTED_RENDER_STAGES_MD = (
    "# Plan: Pin renderer and digest output across origin/main and the delivered tree\n"
    "\n"
    "- **Task id:** invariance-fixture\n"
    "- **Weight class:** small_change\n"
    "- **Overall done criterion:** both stages PASSED\n"
    "- **Overall criterion type:** measurable\n"
    "\n"
    "This is a PROJECTED BRIEF of stage 1 only, out of 2 stage(s) in the plan — the "
    "other stages are not shown and are not this step's concern.\n"
    "\n"
    "## Stage 1: Scaffold module\n"
    "\n"
    "- **Executor:** spawn:developer\n"
    "- **Expected result image:**    \n"
    "- **Criterion type:** measurable\n"
    "- **Done criterion:** python -c 'import mod' exits 0\n"
    "- **Output artifacts:** mod.py\n"
    "- **Grants (file-access scope):**\n"
    "  - derived allow: Bash(python3 mod.py:*), Edit(//./mod.py)\n"
    "\n"
    "## Final verification (labels only — this stage does not need the commands; "
    "see the full plan file for those)\n"
    "\n"
    "- fixture final check\n"
    "\n"
    "# Plan: Pin renderer and digest output across origin/main and the delivered tree\n"
    "\n"
    "- **Task id:** invariance-fixture\n"
    "- **Weight class:** small_change\n"
    "- **Overall done criterion:** both stages PASSED\n"
    "- **Overall criterion type:** measurable\n"
    "\n"
    "This is a PROJECTED BRIEF of stage 2 only, out of 2 stage(s) in the plan — the "
    "other stages are not shown and are not this step's concern.\n"
    "\n"
    "## Stage 2: Add tests\n"
    "\n"
    "- **Executor:** spawn:developer\n"
    "- **Expected result image:** tests exist and pass\n"
    "- **Criterion type:** measurable\n"
    "- **Done criterion:** pytest tests/test_mod.py green\n"
    "- **Depends on** (direct dependencies only; see their own stage for detail):\n"
    "  - Stage 1: Scaffold module\n"
    "    - **Its expected result image:**    \n"
    "    - **Its output artifacts:** mod.py\n"
    "- **Supplies** (raw provision edges this stage declares):\n"
    "  - on stage 1\n"
    "\n"
    "## Final verification (labels only — this stage does not need the commands; "
    "see the full plan file for those)\n"
    "\n"
    "- fixture final check\n"
)


def _load_fixture(tmp_path: Path):
    plan_path = tmp_path / "fixture.toml"
    plan_path.write_text(FIXTURE_TOML)
    return load_plan(plan_path)


def test_tb9_digests_equal_origin_main_constants(tmp_path):
    doc = _load_fixture(tmp_path)
    s1, s2 = doc.stages[0], doc.stages[1]
    assert stage_element_keys(s1) == EXPECTED_STAGE1_KEYS
    assert stage_element_keys(s2) == EXPECTED_STAGE2_KEYS
    assert plan_meta_digest(doc) == EXPECTED_PLAN_META_DIGEST
    assert plan_content_digest(doc) == EXPECTED_PLAN_CONTENT_DIGEST


def test_ti1_renderer_output_equal_origin_main_constants(tmp_path):
    doc = _load_fixture(tmp_path)
    assert render_stage_brief(doc, 2) == EXPECTED_RENDER_STAGE_BRIEF_2
    assert render_plan_md(doc) == EXPECTED_RENDER_PLAN_MD
    assert render_stages_md(doc, [1, 2]) == EXPECTED_RENDER_STAGES_MD
