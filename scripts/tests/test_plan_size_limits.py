"""Plan size limits: the calibrated config keys, the submit-plan lint, the topological
refusal before a spawn, and the plan-fit report.

A plan node over `plan-stage-max-chars` is refused when the plan is submitted and again
before a topological review is spawned; a prompt over the spawn ceiling whose nodes each
fit is refused on its own size. The limits come from `plan-size-calibrate.py` and are
pinned against it here.

New symbols are imported inside the tests so this file collects, and fails, on a tree that
predates them.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import plan_site_fixture as fx

SCRIPTS = Path(__file__).resolve().parent.parent
CALIBRATION = SCRIPTS.parent / "docs" / "operations" / "plan-size-calibration.json"

HEAD = """[meta]
task_id = "size-limits"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"

[meta.order]
customer_id = "c"
customer = "c"
functional_place = "f"

[[meta.order.requirements]]
id = "R1"
text = "r"
derivation = "d"
"""

STAGE = """
[[stage]]
index = {n}
title = "t{n}"
executor = "in_thread"
expected_result_image = "{image}"
criterion_type = "measurable"
done_criterion = "{done}"
verify_command = "{verify}"
{extra}"""

COVERED_STAGE = 1


def plan_toml(*, requirements: "list[int]" = (), covered: int = 0,
              pad: "dict[int, int] | None" = None, image: "dict[int, int] | None" = None,
              done: "dict[int, int] | None" = None, verify: "dict[int, int] | None" = None,
              extra: "dict[int, str] | None" = None) -> str:
    """A three-stage chain (3 relies on 2, 2 on 1) whose requirement R1 is covered by stage
    2. `requirements` adds one requirement per entry, of that many characters of text;
    `covered` names stage 1's verify command in that many further coverage entries.
    `pad[n]` characters go into stage n's `material_refs`, `image[n]`, `done[n]` and
    `verify[n]` set the length of those fields."""
    pad, image, done, verify, extra = pad or {}, image or {}, done or {}, verify or {}, extra or {}
    text = HEAD
    for position, length in enumerate(requirements, start=2):
        text += (f'\n[[meta.order.requirements]]\nid = "R{position}"\n'
                 f'text = "{"r" * length}"\nderivation = "d"\n')
    coverage = ['R1 = ["stage 2 verify_command"]']
    coverage += [f'R{position} = ["stage {COVERED_STAGE} verify_command"]'
                 for position in range(2, 2 + covered)]
    text += "\n[meta.order.coverage]\n" + "\n".join(coverage) + "\n"
    for n in (1, 2, 3):
        body = extra.get(n, "")
        if pad.get(n):
            body += f'material_refs = ["{"x" * pad[n]}"]\n'
        if n > 1:
            body += f"depends_on = [{n - 1}]\n"
        text += STAGE.format(n=n, image="i" * image.get(n, 1), done="d" * done.get(n, 1),
                             verify="v" * verify.get(n, 1), extra=body)
    return text


def write(directory: Path, text: str, name: str = "plan.toml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def calibration() -> dict:
    return json.loads(CALIBRATION.read_text(encoding="utf-8"))


def sizes(plan_path: Path) -> "tuple[int, dict[int, int]]":
    from agentctl import plan

    doc = plan.load_plan(plan_path)
    venue = plan._venue_for(doc)
    return plan.meta_rendered_size(doc), {s.index: plan.stage_rendered_size(s, venue) for s in doc.stages}


def fit_to(directory: Path, target: int, stages=(2, 3), **fields) -> Path:
    """Write the plan with the plan-level node and each of `stages` rendering to exactly
    `target` characters, every plan-level field within the inline limit; `fields` are the
    other `plan_toml` arguments."""
    field_max = calibration()["F"]
    requirements = [field_max - 100] * (target // (field_max - 100))
    pad = {n: 0 for n in stages}
    for _ in range(3):
        path = write(directory, plan_toml(requirements=requirements, pad=pad, **fields))
        meta, stage = sizes(path)
        if meta == target and all(stage[n] == target for n in stages):
            return path
        requirements = [*requirements[:-1], requirements[-1] + target - meta]
        pad = {n: pad[n] + target - stage[n] for n in stages}
    raise AssertionError(f"could not fit the nodes to {target}: {meta} {stage}")


def topo_driver():
    return fx.load_script("plan-review-topological.py", "plan_review_topological_size_limits")


def normalized_prompt(plan_path: Path, pair: str, view_root: Path, **kw) -> str:
    from agentctl import render

    text = topo_driver().reviewer_prompt(str(plan_path), pair, view_root=view_root, **kw)
    return render.normalize_prompt_paths(text, view_root)


def refusal(plan_path: Path, pair: str, view_root: Path, **kw):
    """`topo_unit_refusal` of `pair`'s nodes and its built prompt, as the driver asks it."""
    from agentctl import plan, render

    doc = plan.load_plan(plan_path)
    nodes = (plan.parse_unit(doc, pair),) if plan.is_unit_id(pair) else plan.parse_pair(doc, pair)
    text = topo_driver().reviewer_prompt(str(plan_path), pair, view_root=view_root, **kw)
    return render.topo_unit_refusal(doc, ("meta", *nodes), render.normalize_prompt_paths(text, view_root))


@pytest.fixture
def views(tmp_path, monkeypatch):
    root = tmp_path / "views"
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(root))
    return root


# --- the calibration ---------------------------------------------------------------

def fixture_prompt(directory: Path) -> str:
    """The calibration fixture's pair prompt, normalized, as the calibrator measures it."""
    calibrator = fx.load_script("plan-size-calibrate.py", "plan_size_calibrate_in_test")
    directory.mkdir(parents=True, exist_ok=True)
    plan_path = write(directory, calibrator.FIXTURE_PLAN)
    return normalized_prompt(plan_path, calibrator.PAIR_ID, directory)


def test_calibration_follows_from_its_measured_overhead(tmp_path):
    from agentctl import plan_refs, render

    cal = calibration()
    text = fixture_prompt(tmp_path / "fixture")
    assert cal["C"] == render.dispatch_ceiling_chars()
    assert cal["normalized_prompt_chars"] == len(text)
    assert cal["n_p"] == sum(text.count(p) for p in render.PATH_PLACEHOLDERS)
    assert cal["O"] == len(text) + cal["n_p"] * cal["P"]
    assert cal["k"] == max(len(fields) for fields in plan_refs.EXTERNALIZABLE_FIELDS.values())
    assert cal["S"] == (cal["C"] - cal["O"]) // 3 // 1000 * 1000
    assert cal["F"] == cal["S"] // cal["k"] // 500 * 500
    assert 3 * cal["S"] + cal["O"] <= cal["C"] < 3 * (cal["S"] + 1000) + cal["O"]
    assert 2 * len(render.PROJECTED_FIELDS["stage"]) * cal["F"] <= cal["S"]


def test_config_rows_are_the_calibrated_values():
    from agentctl.config import Thresholds

    cal = calibration()
    limits = Thresholds()
    assert (limits.plan_stage_max_chars, limits.plan_field_inline_max_chars) == (cal["S"], cal["F"])


def test_normalized_prompt_does_not_move_with_the_machine(tmp_path, monkeypatch):
    here = fixture_prompt(tmp_path / "fixture")
    other = tmp_path / "second" / "checkout-with-a-longer-name"
    (other / "cwd").mkdir(parents=True)
    (other / "home").mkdir()
    monkeypatch.chdir(other / "cwd")
    monkeypatch.setenv("HOME", str(other / "home"))
    there = fixture_prompt(other / "fixture-with-a-different-view-root-length")
    assert there == here
    assert there.count("<view-root>") == here.count("<view-root>") == calibration()["n_p"] - sum(
        here.count(p) for p in ("<repo>", "<cwd>", "<plans-dir>", "<agent-home>", "<home>"))


# --- a calibrated pair fits the ceiling ----------------------------------------------

def test_calibrated_pair_prompt_fits_the_ceiling(tmp_path, views):
    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"], stages=(2, 3),
                  image={2: cal["F"]}, done={2: cal["F"]})
    meta, stage = sizes(path)
    assert (meta, stage[2], stage[3]) == (cal["S"],) * 3
    assert normalized_prompt(path, "3-2", views).__len__() <= cal["C"]
    assert refusal(path, "3-2", views) is None


def test_three_source_nodes_at_the_limit_with_minimal_summaries_fit_the_ceiling(tmp_path, views):
    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"], stages=(1, 2))
    assert len(normalized_prompt(path, "2-1", views)) <= cal["C"]
    assert refusal(path, "2-1", views) is None


def test_unnormalized_prompt_fits_with_a_view_root_as_long_as_the_live_one(tmp_path, monkeypatch):
    from lib import config_root

    cal = calibration()
    monkeypatch.delenv("AGENTCTL_TOPO_UNITS_DIR", raising=False)
    calibrator = fx.load_script("plan-size-calibrate.py", "plan_size_calibrate_in_test")
    if calibrator.path_allowance() > cal["P"]:
        pytest.skip("this machine's paths are longer than the ones the calibration was made on")
    live_length = len(str(config_root.topo_units_root()))
    root = tmp_path / "views"
    root = root.parent / (root.name + "v" * max(0, live_length - len(str(root))))
    path = fit_to(tmp_path / "plan", cal["S"], stages=(1, 2))
    text = topo_driver().reviewer_prompt(str(path), "2-1", view_root=root)
    assert len(str(root)) >= live_length
    assert len(text) <= cal["C"]


# --- nodes under the limit, a prompt over the ceiling ---------------------------------

def test_source_stage_service_printed_in_full_beside_large_summaries_exceeds_the_ceiling(
        tmp_path, views):
    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"], stages=(1, 2),
                  image={1: cal["F"], 2: cal["F"]}, done={1: cal["F"], 2: cal["F"]})
    meta, stage = sizes(path)
    assert max(meta, stage[1], stage[2], stage[3]) <= cal["S"]
    assert len(normalized_prompt(path, "2-1", views)) > cal["C"]
    message = refusal(path, "2-1", views)
    assert message and "spawn ceiling" in message
    from agentctl import plan, render

    doc = plan.load_plan(path)
    assert render.topo_unit_refusal(doc, ("meta", 2, 1)) is None


def test_many_coverage_entries_exceed_the_ceiling_with_every_node_under_the_limit(tmp_path, views):
    cal = calibration()
    entries = cal["C"] // cal["F"] + 8
    path = write(tmp_path / "plan", plan_toml(
        requirements=[1] * entries, covered=entries, verify={1: cal["F"]}))
    meta, stage = sizes(path)
    assert max(meta, *stage.values()) <= cal["S"]
    assert len(normalized_prompt(path, "base-1", views)) > cal["C"]
    message = refusal(path, "base-1", views)
    assert message and "spawn ceiling" in message


# --- the driver refuses before it spawns -----------------------------------------------

class _HistoryEngine:
    def __init__(self, records):
        self.records = records

    def run(self, argv, env, script=None):
        assert argv[0] == "plan-review-pair-history", argv[0]
        return {"ok": True, "data": {"pair": "3-2", "records": self.records,
                                     "changed_parts_since_last": []}}


def launching_driver(monkeypatch, plan_path: Path, records=()):
    drv = topo_driver()
    spawned: "list[list[str]]" = []
    monkeypatch.setattr(drv, "run_agentctl", _HistoryEngine(list(records)).run)
    monkeypatch.setattr(drv, "cost_log_size", lambda: 0)
    monkeypatch.setattr(drv, "spawn_pair", lambda argv: spawned.append(list(argv)) or (0, "", ""))
    args = Namespace(session="size-limits", pairs=None, model=None, complexity="high",
                     early_stop=False, parallel=None, dry_run=False)
    return drv.Driver(args, str(plan_path), {}), spawned


def test_driver_spawns_a_pair_whose_nodes_are_within_the_limit(tmp_path, views, monkeypatch, capsys):
    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"], stages=(2, 3))
    driver, spawned = launching_driver(monkeypatch, path)
    try:
        assert driver.launch({"pair": "3-2", "status": "missing"}) is not None
    finally:
        driver.cleanup_history()
    assert len(spawned) == 1 and "TOPO-REFUSED" not in capsys.readouterr().out


def test_one_character_over_the_limit_blocks_the_lint_and_the_spawn(tmp_path, views, monkeypatch, capsys):
    from agentctl import submission, plan

    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"] + 1, stages=(2, 3))
    meta, stage = sizes(path)
    assert (meta, stage[2], stage[3]) == (cal["S"] + 1,) * 3
    blocks = [f for f in submission.plan_size_findings(plan.load_plan(path), frozenset())
              if f["severity"] == "block"]
    assert {f["node"] for f in blocks} == {"meta", 2, 3}
    driver, spawned = launching_driver(monkeypatch, path)
    try:
        assert driver.launch({"pair": "3-2", "status": "missing"}) is None
    finally:
        driver.cleanup_history()
    assert spawned == []
    assert "TOPO-REFUSED: pair=3-2" in capsys.readouterr().out
    assert driver.refused


def test_a_long_review_history_over_the_ceiling_is_refused_before_the_spawn(
        tmp_path, views, monkeypatch, capsys):
    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"] // 2, stages=(2, 3))
    concern = {"id": "", "severity": "blocker", "effective_severity": "blocker",
               "unresolved": True, "parts": ["stage:2"], "text": "c" * cal["C"]}
    record = {"record_seq": 1, "plan_sha256": "a" * 64, "reviewer_verdict": "revise",
              "effective_verdict": "revise", "concerns": [concern]}
    driver, spawned = launching_driver(monkeypatch, path, [record])
    try:
        assert driver.launch({"pair": "3-2", "status": "revise"}) is None
        refused_text = capsys.readouterr().out
    finally:
        driver.cleanup_history()
    assert spawned == []
    assert "TOPO-REFUSED: pair=3-2" in refused_text and "spawn ceiling" in refused_text
    fresh, spawned = launching_driver(monkeypatch, path)
    try:
        assert fresh.launch({"pair": "3-2", "status": "missing"}) is not None
    finally:
        fresh.cleanup_history()
    assert len(spawned) == 1


# --- the lint ------------------------------------------------------------------------

def findings(path: Path, refs=frozenset()) -> "list[dict]":
    from agentctl import plan, submission

    return submission.plan_size_findings(plan.load_plan(path), refs)


def test_a_field_warns_above_the_inline_limit_and_not_at_it(tmp_path):
    field_max = calibration()["F"]
    at = findings(write(tmp_path / "at", plan_toml(image={1: field_max}, done={1: field_max})))
    assert at == []
    over = findings(write(tmp_path / "over", plan_toml(image={1: field_max + 1})))
    assert [(f["node"], f["field"], f["severity"]) for f in over] == [(1, "expected_result_image", "warn")]
    assert "`agentctl plan-externalize`" in over[0]["message"]


def test_an_externalized_field_gets_no_warning_but_still_counts_toward_the_block(tmp_path):
    cal = calibration()
    over = write(tmp_path / "over", plan_toml(image={1: cal["F"] + 1}))
    reference = {(1, "expected_result_image")}
    assert findings(over, reference) == []
    huge = write(tmp_path / "huge", plan_toml(image={1: cal["S"] + 1}))
    assert [(f["node"], f["severity"]) for f in findings(huge, reference)] == [(1, "block")]


def test_a_grants_list_longer_than_the_inline_limit_gets_no_warning(tmp_path):
    cal = calibration()
    long_rule = "Bash(echo " + "a" * cal["F"] + ":*)"
    path = write(tmp_path / "plan", plan_toml(extra={1: f'grants = {{ allow = ["{long_rule}"] }}\n'}))
    assert findings(path) == []


def test_a_plan_under_every_limit_has_no_size_finding_and_unchanged_other_lints(tmp_path):
    from agentctl import plan, submission

    clean = plan.load_plan(write(tmp_path / "clean", plan_toml()))
    assert submission.plan_size_findings(clean) == []
    assert submission.submission_violations(clean) == []
    flawed = plan.load_plan(write(tmp_path / "flawed", plan_toml().replace(
        "[meta]\n", '[meta]\nweight_class = "bogus"\n', 1)))
    assert submission.plan_size_findings(flawed) == []
    assert submission.submission_violations(flawed) == [
        "[meta] weight_class = 'bogus' is not one of 'chat'/'small_change'/'substantive'. "
        "The substantive plan grade is keyed on this declaration, so an unreadable value "
        "escapes the grade exactly as silence would — declare the class this plan really is"
    ]


# --- the submit-plan seam --------------------------------------------------------------

def submit(store, tmp_path, venued_fixtures_dir, method_chars: int, sid: str):
    from agentctl import cli

    text = (venued_fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    text = text.replace("depends_on = []\n", f'depends_on = []\nmethod = "{"m" * method_chars}"\n', 1)
    path = write(tmp_path / sid, text)
    cli.cmd_start(Namespace(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(Namespace(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session=sid), store=store)
    return cli.cmd_submit_plan(Namespace(session=sid, plan=str(path)), store=store)


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def test_submit_plan_blocks_a_node_over_the_limit(store, tmp_path, venued_fixtures_dir):
    d = submit(store, tmp_path, venued_fixtures_dir, calibration()["S"] + 1, "over-limit")
    assert d.ok is False
    problems = " ".join(d.data["problems"])
    assert "stage 1 renders to" in problems and "plan-stage-max-chars" in problems


def test_submit_plan_warns_on_an_over_limit_field_naming_externalize(store, tmp_path, venued_fixtures_dir):
    d = submit(store, tmp_path, venued_fixtures_dir, calibration()["F"] + 1, "over-field")
    assert d.ok is True
    advice = " ".join(d.data["advisories"])
    assert "stage 1 field 'method'" in advice and "`agentctl plan-externalize`" in advice


def test_submit_plan_is_silent_about_size_for_a_plan_within_every_limit(store, tmp_path, venued_fixtures_dir):
    d = submit(store, tmp_path, venued_fixtures_dir, calibration()["F"], "within")
    assert d.ok is True
    assert not any("plan-externalize" in a for a in d.data.get("advisories", []))


# --- measuring a raw plan --------------------------------------------------------------

def test_raw_and_parsed_nodes_measure_the_same(tmp_path):
    import tomllib
    from agentctl import plan

    path = write(tmp_path / "plan", plan_toml(requirements=[40], pad={2: 300}, image={1: 90}))
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    doc = plan.load_plan(path)
    venue = plan._venue_for(doc)
    assert plan.meta_rendered_size(raw) == plan.meta_rendered_size(doc)
    assert plan.meta_rendered_size(raw["meta"]) == plan.meta_rendered_size(doc)
    for table, stage in zip(raw["stage"], doc.stages):
        assert plan.stage_rendered_size(table, venue) == plan.stage_rendered_size(stage, venue)


def test_a_raw_plan_the_loader_refuses_is_still_measured_and_counts_an_undefined_key(tmp_path):
    import tomllib
    from agentctl import plan, submission

    cal = calibration()
    text = plan_toml().replace('expected_result_image = "i"\n', "", 1)
    text = text.replace('title = "t1"\n', f'title = "t1"\nbogus_key = "{"b" * cal["S"]}"\n', 1)
    path = write(tmp_path / "plan", text)
    with pytest.raises(plan.PlanError):
        plan.load_plan(path)
    raw = tomllib.loads(text)
    assert plan.stage_rendered_size(raw["stage"][0]) > cal["S"]
    assert plan.stage_rendered_size(raw["stage"][1]) < cal["S"]
    assert plan.meta_rendered_size(raw) < cal["S"]
    assert [(f["node"], f["severity"]) for f in submission.plan_size_findings(raw)] == [(1, "block")]


# --- plan-fit-report -------------------------------------------------------------------

def fit_report():
    return fx.load_script("plan-fit-report.py", "plan_fit_report_in_test")


def argv_value(argv: "list[str]", flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_fit_report_topo_verdicts_are_the_drivers_refusals(tmp_path, views):
    from agentctl import plan

    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"], stages=(1, 2),
                  image={1: cal["F"], 2: cal["F"]}, done={1: cal["F"], 2: cal["F"]})
    report = fit_report().build_report(path, None)
    doc = plan.load_plan(path)
    ids = [*plan.review_pairs(doc), *plan.review_units(doc)]
    assert sorted(report["topo"]) == sorted(ids)
    assert report["oversize_nodes"] == []
    for review_id in ids:
        expected = "refused-before-spawn" if refusal(path, review_id, views) else "ok"
        assert report["topo"][review_id] == expected, review_id
    assert report["topo"]["2-1"] == "refused-before-spawn"
    assert "ok" in report["topo"].values()


def test_fit_report_flags_an_oversize_node_and_the_lint_verdict(tmp_path):
    cal = calibration()
    path = fit_to(tmp_path / "plan", cal["S"] + 1, stages=(3,), image={1: cal["F"] + 1})
    report = fit_report().build_report(path, None)
    assert report["oversize_nodes"] == ["meta", "3"]
    assert report["lint"] == {"meta": "block", "1": "warn", "2": "ok", "3": "block"}
    assert report["node_chars"]["3"] == cal["S"] + 1
    assert (report["plan_stage_max_chars"], report["plan_field_inline_max_chars"]) == (cal["S"], cal["F"])


def test_fit_report_site_sizes_are_what_the_spawner_assembles(tmp_path):
    from agentctl import render

    path = write(tmp_path / "plan", plan_toml(requirements=[200], pad={2: 500}))
    report = fit_report().build_report(path, None)
    spawner = fx.load_script("spawn-specialist.py", "spawn_specialist_in_size_test")
    sites = {site["id"]: site for site in report["sites"]}
    assert {"dispatch-stage-brief 1", "dispatch-stage-brief 2", "dispatch-stage-brief 3",
            "dispatch-no-stage", "whole-plan-review"} <= set(sites)
    for site in sites.values():
        if site["id"].startswith("topo-"):
            continue
        view_root = tmp_path / ("fresh-" + site["id"].replace(" ", "-"))
        argv = [str(view_root) if part == "<view-root>" else part for part in site["argv"]]
        text = spawner.assembled_prompt(argv)
        assert len(render.normalize_prompt_paths(text, view_root)) == site["chars"], site["id"]


def test_fit_report_argv_shapes_and_shown_nodes(tmp_path):
    path = write(tmp_path / "plan", plan_toml())
    sites = {s["id"]: s for s in fit_report().build_report(path, None)["sites"]}
    brief = sites["dispatch-stage-brief 2"]
    assert argv_value(brief["argv"], "--stage-index") == "2"
    assert argv_value(brief["argv"], "--plan") == str(path)
    assert argv_value(brief["argv"], "--view-root") == "<view-root>"
    assert "--review-topo" not in brief["argv"]
    whole = sites["whole-plan-review"]
    assert argv_value(whole["argv"], "--kind") == "thinker"
    assert "--stage-index" not in whole["argv"] and "--review-topo" not in whole["argv"]
    unstaged = sites["dispatch-no-stage"]
    assert "--stage-index" not in unstaged["argv"] and "--review-topo" not in unstaged["argv"]
    assert unstaged["shown_nodes"] == ["meta"]
    assert whole["shown_nodes"] == ["meta"]
    assert brief["shown_nodes"] == ["2"]


def test_fit_report_content_digest_tells_an_externalized_copy_from_an_edited_one(tmp_path, capsys):
    import shutil
    from agentctl import cli

    inline = write(tmp_path / "inline", plan_toml(extra={1: 'method = "do the work"\n'}))
    external = tmp_path / "external" / "plan.toml"
    external.parent.mkdir()
    shutil.copy(inline, external)
    assert cli.main(["--state-root", str(tmp_path / "state"), "plan-externalize",
                     "--plan", str(external), "--stage", "1", "--field", "method"]) == 0
    capsys.readouterr()
    assert 'ref = "' in external.read_text(encoding="utf-8")
    assert fit_report().build_report(external, inline)["content_digest_equal"] is True
    edited = write(tmp_path / "edited", plan_toml(extra={1: 'method = "do other work"\n'}))
    assert fit_report().build_report(external, edited)["content_digest_equal"] is False


def test_fit_report_loads_a_hypertext_plan_outside_the_plans_directory(tmp_path):
    from agentctl import plan, plan_refs

    index = fx.write_hypertext_plan(tmp_path / "ht", long_method=True)
    report = fit_report().build_report(index, None)
    doc = plan.load_plan(index, root=plan_refs.default_root(index))
    venue = plan._venue_for(doc)
    assert report["node_chars"] == {"meta": plan.meta_rendered_size(doc),
                                    **{str(s.index): plan.stage_rendered_size(s, venue) for s in doc.stages}}
    assert any(site["id"] == "dispatch-no-stage" for site in report["sites"])


def test_fit_report_cli_writes_the_report_file(tmp_path, capsys):
    path = write(tmp_path / "plan", plan_toml())
    out = tmp_path / "fit.json"
    assert fit_report().main(["--plan", str(path), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["node_chars"].keys() == {"meta", "1", "2", "3"}
    assert "sites" in capsys.readouterr().out

