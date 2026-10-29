"""The old-to-new plan converter, `plan-convert`, `plan-convert-live` and the inventory of
root-digest comparisons (ADR-0012).

Plans here are synthetic and written by this module. Imports sit inside test bodies so a
failing import is one failing test, not a module that collects nothing.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent
CLI = SCRIPTS / "agentctl-cli.py"
INVENTORY = REPO / "docs" / "operations" / "plan-readers.md"

ACCEPT_PLAN_ENV = "PLAN_CONVERT_ACCEPT_PLAN"
SMALL_FIELD_MAX = 20
BIG_STAGE_MAX = 100_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan_text(*, goal="exercise the converter", method='line one\nline "two"\n',
               executor_two="spawn:developer") -> str:
    return (
        "[meta]\n"
        'task_id = "demo"\n'
        f'goal = "{goal}"\n'
        'done_criterion = "all stages PASSED"\n'
        'criterion_type = "measurable"\n'
        "\n"
        "[[stage]]\n"
        "index = 1\n"
        'title = "Stage 1"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "image 1"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "stage 1 done"\n'
        "depends_on = []\n"
        'output_artifacts = ["s1.py"]\n'
        f'method = """\n{method}"""\n'
        "\n"
        "[[stage]]\n"
        "index = 2\n"
        'title = "Stage 2"\n'
        f'executor = "{executor_two}"\n'
        'expected_result_image = "image 2"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "stage 2 done"\n'
        "depends_on = [1]\n"
        'output_artifacts = ["s2.py"]\n'
    )


SOURCE = _plan_text().encode()
REFUSED_SOURCE = _plan_text(executor_two="nobody").encode()
LONG_METHOD = "a line of method text that is repeated\n" * 120


def _convert(source=SOURCE, stem="demo", field=SMALL_FIELD_MAX, stage=BIG_STAGE_MAX):
    from agentctl import plan_convert

    return plan_convert.convert_with_report(
        source, stem, field_max_chars=field, stage_max_chars=stage)


def _tree(root: Path) -> dict[str, str]:
    """Every file under `root` as relative path -> sha256, directories as `<dir>/`."""
    listing: dict[str, str] = {}
    if not root.exists():
        return listing
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        listing[rel] = "<dir>" if path.is_dir() else _sha(path.read_bytes())
    return listing


def _write_tree(files: dict[str, bytes], root: Path) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root / next(iter(files))


# --- the converter -------------------------------------------------------------------------


def test_layout_names_every_file_the_spec_names_and_the_index_comes_first():
    text = _plan_text(method="a method that is longer than twenty chars\n").replace(
        "[[stage]]\nindex = 1", '[meta.order]\ncustomer = "a customer long enough to move"\n\n'
        "[[stage]]\nindex = 1", 1)
    files, report = _convert(text.encode(), field=20)

    assert next(iter(files)) == "demo.toml"
    assert set(files) == {
        "demo.toml", "demo.parts/stage-1.toml", "demo.parts/stage-2.toml",
        "demo.parts/meta-goal.md", "demo.parts/meta-order-customer.md",
        "demo.parts/stage-1-method.md",
    }
    assert {(str(e["node"]), e["field"]) for e in report["externalized"]} == {
        ("meta", "goal"), ("meta.order", "customer"), ("1", "method")}


def test_a_field_is_externalized_only_when_longer_than_the_limit():
    goal = "exercise the converter"
    at_limit, _ = _convert(_plan_text(goal=goal).encode(), field=len(goal), stage=BIG_STAGE_MAX)
    over_limit, _ = _convert(_plan_text(goal=goal).encode(), field=len(goal) - 1)

    assert "demo.parts/meta-goal.md" not in at_limit
    assert over_limit["demo.parts/meta-goal.md"] == goal.encode()


def test_a_field_file_holds_the_exact_utf8_bytes_of_the_text():
    method = "lines\nwith \"quotes\", a tab\t and ünïcode — ✓\n"
    files, _ = _convert(_plan_text(method=method).encode(), field=10)

    assert files["demo.parts/stage-1-method.md"] == method.encode("utf-8")


def test_a_non_string_field_and_a_non_externalizable_key_stay_in_the_stage_file():
    files, _ = _convert(field=5)

    import tomllib

    stage_one = tomllib.loads(files["demo.parts/stage-1.toml"].decode())
    assert stage_one["depends_on"] == []
    assert stage_one["output_artifacts"] == ["s1.py"]
    assert stage_one["index"] == 1


def test_a_stage_without_an_index_key_gets_its_position_as_index_in_its_stage_file():
    text = _plan_text().replace("index = 1\n", "").replace("index = 2\n", "")
    files, report = _convert(text.encode())

    import tomllib

    for n in (1, 2):
        assert tomllib.loads(files[f"demo.parts/stage-{n}.toml"].decode())["index"] == n
    assert report["added_indices"] == [1, 2]


def test_the_hash_of_each_reference_is_the_sha256_of_the_bytes_it_names():
    files, _ = _convert(field=10)

    import tomllib

    index = tomllib.loads(files["demo.toml"].decode())
    assert [r["ref"] for r in index["stage"]] == ["demo.parts/stage-1.toml", "demo.parts/stage-2.toml"]
    for ref in index["stage"]:
        assert ref["sha256"] == _sha(files[ref["ref"]])
    stage_one = tomllib.loads(files["demo.parts/stage-1.toml"].decode())
    assert stage_one["method"]["sha256"] == _sha(files["demo.parts/stage-1-method.md"])
    assert index["meta"]["goal"]["sha256"] == _sha(files["demo.parts/meta-goal.md"])


def test_an_already_hypertext_source_is_refused_toward_plan_seal():
    from agentctl.plan import PlanError

    files, _ = _convert(field=10)

    with pytest.raises(PlanError, match="already hypertext; use plan-seal"):
        _convert(files["demo.toml"])


def test_a_stage_index_that_differs_from_its_position_converts_under_its_declared_index():
    text = _plan_text().replace("index = 2\n", "index = 5\n")
    files, _ = _convert(text.encode())

    assert "demo.parts/stage-5.toml" in files
    assert "demo.parts/stage-2.toml" not in files


def test_a_stage_index_that_is_not_an_integer_is_refused():
    from agentctl.plan import PlanError

    text = _plan_text().replace("index = 2\n", 'index = "2"\n')

    with pytest.raises(PlanError, match="stage index must be an integer"):
        _convert(text.encode())


def test_a_source_that_is_not_toml_is_refused_without_a_traceback():
    from agentctl.plan import PlanError

    with pytest.raises(PlanError, match="malformed TOML"):
        _convert(b"[meta\nbroken")


def test_the_report_carries_both_limits_the_root_digest_and_the_proofs():
    files, report = _convert(field=10, stage=BIG_STAGE_MAX)

    assert report["field_max_chars"] == 10
    assert report["stage_max_chars"] == BIG_STAGE_MAX
    assert report["source_sha256"] == _sha(SOURCE)
    assert report["root_digest"] == _sha(files["demo.toml"])
    assert (report["round_trip"], report["raw_equivalence"], report["parsed_equivalence"]) == (
        "ok", "ok", "ok")
    assert report["oversize_stages"] == []


def test_a_node_over_the_stage_limit_is_converted_as_it_is_and_reported():
    files, report = _convert(_plan_text(method=LONG_METHOD).encode(), field=10, stage=500)

    assert [o["node"] for o in report["oversize_stages"]] == [1]
    assert report["oversize_stages"][0]["chars"] > 500
    assert report["oversize_stages"][0]["remedy"]
    assert files["demo.parts/stage-1-method.md"] == LONG_METHOD.encode()


def test_the_meta_node_is_reported_by_name_when_it_is_the_oversize_one():
    _, report = _convert(_plan_text(goal="g" * 900).encode(), field=10, stage=500)

    assert "meta" in [o["node"] for o in report["oversize_stages"]]


# --- determinism -----------------------------------------------------------------------------


def test_the_conversion_is_a_pure_function_of_its_arguments():
    first, report_one = _convert(_plan_text(method=LONG_METHOD).encode(), field=100)
    second, report_two = _convert(_plan_text(method=LONG_METHOD).encode(), field=100)

    assert list(first.items()) == list(second.items())
    assert report_one == report_two


def test_a_second_process_under_another_hash_seed_writes_the_same_tree(tmp_path):
    source = tmp_path / "demo.toml"
    source.write_text(_plan_text(method=LONG_METHOD), encoding="utf-8")
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from agentctl import plan_convert\n"
        f"files = plan_convert.convert(open({str(source)!r}, 'rb').read(), 'demo', "
        "field_max_chars=100, stage_max_chars=100000)\n"
        "print(json.dumps({k: v.hex() for k, v in files.items()}))\n"
    )
    runs = []
    for seed in ("0", "12345"):
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
            env={"PATH": os.environ["PATH"], "PYTHONHASHSEED": seed,
                 "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path)})
        assert done.returncode == 0, done.stderr[-400:]
        runs.append(done.stdout)

    assert runs[0] == runs[1]
    assert list(json.loads(runs[0])) [0] == "demo.toml"


# --- the emitter -----------------------------------------------------------------------------


def test_the_emitter_round_trips_awkward_values():
    import tomllib

    from agentctl import toml_emit

    awkward = {
        "plain": "x", "quote": 'say "hi"', "backslash": "a\\b", "multi": "one\ntwo\n\"three\"\n",
        "unicode": "ünï — ✓", "tab": "a\tb", "empty": "", "number": 7, "flag": True,
        "list": [1, 2], "words": ["a", "b"], "empty_list": [],
        "ref": {"ref": "x.parts/f.md", "sha256": "0" * 64},
        "table": {"inner": "v", "deeper": {"k": 1}},
        "rows": [{"a": 1, "b": "two"}, {"a": 3, "b": "four"}],
    }

    assert tomllib.loads(toml_emit.dumps(awkward)) == awkward


def test_the_emitter_is_deterministic_for_equal_input():
    from agentctl import toml_emit

    data = {"b": "2", "a": {"y": 1, "x": [1, 2]}, "c": [{"k": "v"}]}

    assert toml_emit.dumps(data) == toml_emit.dumps(json.loads(json.dumps(data)))


# --- equivalence -----------------------------------------------------------------------------


def test_a_loadable_source_is_proved_on_both_raw_and_parsed_equivalence():
    _, report = _convert(field=10)

    assert report["raw_equivalence"] == "ok"
    assert report["parsed_equivalence"] == "ok"
    assert "parsed_equivalence_detail" not in report


def test_a_loader_refused_source_converts_on_raw_equivalence_alone():
    files, report = _convert(REFUSED_SOURCE, field=10)

    assert report["raw_equivalence"] == "ok"
    assert report["parsed_equivalence"] == "not-applicable"
    assert "outside the vocabulary" in report["parsed_equivalence_detail"]
    assert "demo.toml" in files


def test_a_loader_refused_source_still_reports_its_oversize_stages():
    _, report = _convert(REFUSED_SOURCE, field=10, stage=50)

    assert report["parsed_equivalence"] == "not-applicable"
    assert report["oversize_stages"]


def test_the_converted_tree_loads_and_digests_the_same_plan_content(tmp_path):
    from agentctl import plan

    files, _ = _convert(_plan_text(method=LONG_METHOD).encode(), field=100)
    index = _write_tree(files, tmp_path / "tree")
    flat = tmp_path / "demo.toml"
    flat.write_bytes(_plan_text(method=LONG_METHOD).encode())

    assert plan.plan_content_digest(plan.load_plan(index)) == plan.plan_content_digest(
        plan.load_plan(flat))


# --- mutation controls on the proof ----------------------------------------------------------


def test_a_parsed_only_difference_in_the_grants_computation_is_refused_as_parsed_equivalence(
        monkeypatch):
    from agentctl import plan
    from agentctl.plan import PlanError

    calls = []

    def drifting(doc):
        calls.append(1)
        return {"granted": len(calls)}

    monkeypatch.setattr(plan, "_grants_effective_map", drifting)

    with pytest.raises(PlanError, match=r"parsed equivalence: effective_grants differs"):
        _convert(field=10)


def test_an_emitter_fault_that_drops_a_key_is_refused_as_the_round_trip(monkeypatch):
    from agentctl import toml_emit
    from agentctl.plan import PlanError

    real = toml_emit.dumps

    def dropping(data):
        if "title" in data:
            return real({k: v for k, v in data.items() if k != "title"})
        return real(data)

    monkeypatch.setattr(toml_emit, "dumps", dropping)

    with pytest.raises(PlanError, match=r"round trip: .*title"):
        _convert(field=10)


def _faulty_field_file(monkeypatch):
    """`write_field_file` writes one byte more than the text and names that file's hash:
    the emitted TOML stays valid and every hash stays consistent, so only the resolved
    text can show it."""
    from agentctl import plan_refs

    def faulty(path, text):
        return real(path, text + "\n")

    real = plan_refs.write_field_file
    monkeypatch.setattr(plan_refs, "write_field_file", faulty)


@pytest.mark.parametrize("source", [SOURCE, REFUSED_SOURCE], ids=["loadable", "loader-refused"])
def test_an_encoding_fault_in_a_field_file_is_refused_as_raw_equivalence(monkeypatch, source):
    from agentctl.plan import PlanError

    _faulty_field_file(monkeypatch)

    with pytest.raises(PlanError, match=r"raw equivalence: .*differs from the source"):
        _convert(source, field=10)


def test_a_refused_conversion_writes_nothing_into_the_directory_it_was_run_from(
        monkeypatch, tmp_path):
    from agentctl.plan import PlanError

    _faulty_field_file(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree(tmp_path)

    with pytest.raises(PlanError):
        _convert(field=10)

    assert _tree(tmp_path) == before


# --- the read-only verb ----------------------------------------------------------------------


def _isolated_env(base: Path, seed: str = "0") -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"], "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(base / "home"), "CLAUDE_CONFIG_DIR": str(base / "config"),
        "CLAUDE_AGENT_HOME": str(base / "agent-home"), "TMPDIR": str(base / "tmp"),
    }
    for name in ("home", "config", "agent-home", "tmp"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return env


def _seed_world(base: Path) -> None:
    """Files a stray write would disturb: a state file, a snapshot, an object, a plan."""
    for rel, content in {
        "agent-home/agentctl/state/old.json": '{"session_id": "old"}',
        "agent-home/plans/objects/ab/ab" + "0" * 62: "an object",
        "agent-home/plans/snapshot.toml": "[meta]\n",
        "config/settings.json": "{}",
        "home/.keep": "",
        "tmp/scratch": "x",
    }.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _run_cli(env, *argv, timeout=180):
    return subprocess.run(
        [sys.executable, str(CLI), *argv], capture_output=True, text=True, timeout=timeout,
        env=env, cwd=env["HOME"])


def _world_listing(base: Path) -> dict[str, dict[str, str]]:
    return {name: _tree(base / name) for name in ("home", "config", "agent-home", "tmp")}


@pytest.fixture
def world(tmp_path):
    base = tmp_path / "world"
    base.mkdir()
    env = _isolated_env(base)
    _seed_world(base)
    plan_file = tmp_path / "source" / "demo.toml"
    plan_file.parent.mkdir()
    plan_file.write_bytes(_plan_text(goal="exercise the converter and then some",
                                     method=LONG_METHOD * 3).encode())
    return base, env, plan_file, tmp_path / "out"


def test_historical_mode_writes_only_out_and_reports_the_proofs(world):
    base, env, plan_file, out = world
    before, source_before = _world_listing(base), plan_file.read_bytes()

    done = _run_cli(env, "plan-convert", "--plan", str(plan_file), "--out", str(out),
                    "--format", "json")

    assert done.returncode == 0, done.stdout[-600:] + done.stderr[-300:]
    assert _world_listing(base) == before
    assert plan_file.read_bytes() == source_before
    assert (out / "demo.toml").is_file() and (out / "demo.parts").is_dir()
    data = json.loads(done.stdout)["data"]
    assert data["source_sha256"] == _sha(source_before)
    assert data["root_digest"] == _sha((out / "demo.toml").read_bytes())
    assert data["parsed_equivalence"] == "ok" and data["raw_equivalence"] == "ok"
    assert isinstance(data["oversize_stages"], list)


def test_two_runs_give_byte_identical_trees_and_the_same_root_digest(world, tmp_path):
    base, env, plan_file, out = world
    other_env = _isolated_env(tmp_path / "second-world", seed="777")
    second = tmp_path / "out2"

    one = _run_cli(env, "plan-convert", "--plan", str(plan_file), "--out", str(out),
                   "--format", "json")
    two = _run_cli(other_env, "plan-convert", "--plan", str(plan_file), "--out", str(second),
                   "--format", "json")

    assert one.returncode == 0 and two.returncode == 0
    assert _tree(out) == _tree(second)
    assert (json.loads(one.stdout)["data"]["root_digest"]
            == json.loads(two.stdout)["data"]["root_digest"])


def test_the_text_report_names_both_limits_and_each_externalized_field(world):
    _, env, plan_file, out = world

    done = _run_cli(env, "plan-convert", "--plan", str(plan_file), "--out", str(out))

    assert done.returncode == 0
    detail = json.loads(done.stdout)["detail"]
    assert "plan-stage-max-chars 70000" in detail and "plan-field-inline-max-chars 3000" in detail
    assert "externalized 1.method" in detail


@pytest.mark.parametrize("case", ["out-not-empty", "out-is-a-file", "missing-plan", "hypertext"])
def test_historical_mode_refusals_leave_every_file_as_it_was(world, tmp_path, case):
    base, env, plan_file, out = world
    plan_arg = str(plan_file)
    if case == "out-not-empty":
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
    elif case == "out-is-a-file":
        out.write_text("a file", encoding="utf-8")
    elif case == "missing-plan":
        plan_arg = str(tmp_path / "nowhere.toml")
    else:
        assert _run_cli(env, "plan-convert", "--plan", plan_arg,
                        "--out", str(tmp_path / "first")).returncode == 0
        plan_arg = str(tmp_path / "first" / "demo.toml")
    before = _world_listing(base)
    out_before, source_before = _tree(out), _tree(plan_file.parent)

    done = _run_cli(env, "plan-convert", "--plan", plan_arg, "--out", str(out))

    assert done.returncode == 1
    assert json.loads(done.stdout)["ok"] is False
    assert _world_listing(base) == before
    assert _tree(out) == out_before
    assert _tree(plan_file.parent) == source_before
    if case == "hypertext":
        assert "already hypertext; use plan-seal" in json.loads(done.stdout)["detail"]


def test_a_write_outside_out_is_visible_to_the_before_after_listing(world):
    base, env, plan_file, out = world
    before = _world_listing(base)

    (base / "agent-home" / "plans" / "objects" / "stray").write_text("seeded", encoding="utf-8")

    assert _world_listing(base) != before


def test_the_converted_tree_feeds_the_plan_readers(world, tmp_path):
    base, env, plan_file, out = world
    assert _run_cli(env, "plan-convert", "--plan", str(plan_file), "--out", str(out)).returncode == 0
    index = out / "demo.toml"

    digest = json.loads(_run_cli(env, "plan-digest", "--plan", str(index)).stdout)
    render = json.loads(_run_cli(env, "plan-render", "--plan", str(index)).stdout)
    grants = json.loads(_run_cli(env, "plan-grants", "--plan", str(index)).stdout)
    import plan_site_fixture as fx

    report = fx.load_script("plan-fit-report.py", "plan_fit_report_for_convert").build_report(
        index, None)

    assert digest["ok"] and render["ok"] and grants["ok"]
    assert report["node_chars"]


def test_the_flat_and_the_converted_plan_render_the_same_text(world, tmp_path):
    base, env, plan_file, out = world
    assert _run_cli(env, "plan-convert", "--plan", str(plan_file), "--out", str(out)).returncode == 0

    flat = json.loads(_run_cli(env, "plan-render", "--plan", str(plan_file)).stdout)["detail"]
    tree = json.loads(_run_cli(env, "plan-render", "--plan", str(out / "demo.toml")).stdout)["detail"]

    shown_tree = tree.split("\n\n## Externalized fields")[0] + "\n"
    assert flat.replace(str(plan_file), "PLAN") == shown_tree.replace(str(out / "demo.toml"), "PLAN")


def test_an_over_limit_stage_is_reported_in_the_json_data_and_not_admitted_silently(world):
    base, env, plan_file, out = world
    plan_file.write_bytes(_plan_text(method="x" * 71_000).encode())

    done = _run_cli(env, "plan-convert", "--plan", str(plan_file), "--out", str(out),
                    "--format", "json")

    assert done.returncode == 0
    data = json.loads(done.stdout)["data"]
    assert [o["node"] for o in data["oversize_stages"]] == [1]
    assert data["oversize_stages"][0]["chars"] > 70_000
    assert "reformulate or split" in data["oversize_stages"][0]["remedy"]


def test_a_converted_tree_goes_through_the_same_size_seam_submit_plan_uses(tmp_path):
    from agentctl import cli, plan, plan_convert

    stage_max, field_max = plan_convert._limits()
    files, report = _convert(_plan_text(method="x" * 71_000).encode(),
                             field=field_max, stage=stage_max)
    index = _write_tree(files, tmp_path / "tree")

    problems = cli._submission_problems(plan.load_plan(index), None, "substantive")

    assert [o["node"] for o in report["oversize_stages"]] == [1]
    assert any("plan-stage-max-chars" in p for p in problems)


def test_the_read_only_verb_is_not_a_user_authority_verb_and_the_live_one_is():
    from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS

    assert "plan-convert-live" in AGENTCTL_USER_AUTHORITY_VERBS
    assert "plan-convert" not in AGENTCTL_USER_AUTHORITY_VERBS


def test_the_user_authority_verb_set_differs_from_the_stage_five_set_by_the_live_verb_only():
    from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS

    stage_five = frozenset({
        "start", "reset", "approve", "resolve-permission", "resolve", "reject",
        "accept", "risk-accept", "plan-review", "code-review", "stage-review",
        "confirm-delivery", "present-plan", "submit-plan", "dispatch",
        "record-result", "verify-final", "replan", "close", "fire-acknowledge",
        "block", "unblock", "drive", "push-subplan", "pop-subplan", "task-reset",
        "declare", "investigate", "critique", "normalize", "difficulty-open",
        "difficulty-close", "partition",
        "partition-units", "next-stage", "plugin-activate", "plugin-deactivate",
        "plugin-record", "mandate-grant", "mandate-extend", "mandate-resume",
    })

    added_on_main_since = {
        "accept-protected-grant", "adopt-escalation", "mandate-retry", "record-landing",
    }

    assert AGENTCTL_USER_AUTHORITY_VERBS - stage_five - added_on_main_since == {"plan-convert-live"}
    assert stage_five - AGENTCTL_USER_AUTHORITY_VERBS == set()


# --- the live verb ---------------------------------------------------------------------------


def _fixture_source() -> tuple[str, bytes]:
    """`(file name, bytes)` of the plan the live tests convert: the synthetic one, or the
    plan `PLAN_CONVERT_ACCEPT_PLAN` names, copied by the caller and never written to."""
    given = os.environ.get(ACCEPT_PLAN_ENV)
    if not given:
        return "demo.toml", _plan_text(method=LONG_METHOD * 3).encode()
    path = Path(given)
    try:
        return path.name, path.read_bytes()
    except OSError as exc:
        pytest.fail(f"{ACCEPT_PLAN_ENV}={given} cannot be read: {exc}")


def _live_session(store, tmp_path, *, shape: str, session_id="live-a"):
    """A session built through `state.py`'s own constructors and `store.save` (never
    `submit-plan`), bound to a plan file in a fixture directory."""
    from agentctl.state import GateRecord, Node, PlanPresentation, PlanReview, SessionState

    name, data = _fixture_source()
    plan_dir = tmp_path / f"plans-{session_id}"
    plan_dir.mkdir()
    plan_file = plan_dir / name
    plan_file.write_bytes(data)
    root = _sha(data)
    state = SessionState(session_id=session_id, task_id="live")
    state.node = Node.EXECUTING.value
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="user")
    state.plan_path = str(plan_file)
    state.accepted_plan_digest = root
    state.plan_snapshot_path = str(plan_dir / "snapshot.toml")
    state.plan_snapshot_hash = root
    state.plan_review = PlanReview(
        plan_path=str(plan_file), verdict="pass", reviewer="thinker", plan_sha256=root)
    state.plan_presentations = [PlanPresentation(
        plan_path=str(plan_file), kind="essence", plan_sha256=root,
        rendering_sha256="0" * 64, rendering_text="the essence", presented_ts=1.0)]
    if shape == "counted":
        state.plan_review_counted_digest = root
        state.plan_review_rounds = 2
    store.save(state)
    return session_id, plan_file, root


def _live(store, sid, *flags, by="agent"):
    from argparse import Namespace

    from agentctl import plan_convert

    return plan_convert.cmd_plan_convert_live(
        Namespace(session=sid, by=by, revert="--revert" in flags), store=store)


def _bindings(state) -> dict:
    return {
        "review": state.plan_review.plan_sha256,
        "presentation": state.plan_presentations[0].plan_sha256,
        "snapshot_hash": state.plan_snapshot_hash,
        "snapshot_path": state.plan_snapshot_path,
        "counted": state.plan_review_counted_digest,
        "rounds": state.plan_review_rounds,
    }


def _content_digest(plan_file):
    # Loaded with the defaults the engine uses for a registered plan (plan_store.load_baseline).
    from agentctl import plan as P

    return P.plan_content_digest(P.load_plan(str(plan_file)))


def _print_rebinding(bindings_before, after, plan_file):
    print(f"live_rebinding before={bindings_before['review']} "
          f"after={_content_digest(plan_file)} root={after.accepted_plan_digest}")


@pytest.mark.parametrize("shape", ["bound", "counted"])
def test_live_rebinding_keeps_every_recorded_binding_and_moves_only_the_accepted_digest(
        store, tmp_path, shape):
    from agentctl import gates

    sid, plan_file, root = _live_session(store, tmp_path, shape=shape)
    before = store.load(sid)
    bindings_before = _bindings(before)

    result = _live(store, sid)

    assert result.ok, result.detail
    after = store.load(sid)
    new_root = _sha(plan_file.read_bytes())
    assert new_root != root
    assert _bindings(after) == bindings_before
    assert after.accepted_plan_digest == new_root
    assert [(c.old_digest, c.new_digest, c.direction, c.by) for c in after.plan_conversions] == [
        (root, new_root, "convert", "agent")]
    assert gates._plan_review_content_stale(after.plan_review, str(plan_file), after) is None
    assert gates._receipt_binding_blocker(
        after.plan_presentations[0], str(plan_file), "plan presentation", after) is None
    _print_rebinding(bindings_before, after, plan_file)


def test_live_rebinding_writes_no_new_snapshot_and_stores_the_old_and_new_bytes(
        store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")
    old_bytes = plan_file.read_bytes()
    before = store.load(sid)
    snapshot = Path(before.plan_snapshot_path)

    assert _live(store, sid).ok

    _print_rebinding(_bindings(before), store.load(sid), plan_file)
    assert not snapshot.exists()
    assert plan_store.get(root) == old_bytes
    new_root = _sha(plan_file.read_bytes())
    assert plan_store.get(new_root) == plan_file.read_bytes()


def test_the_live_directive_discloses_who_both_digests_and_the_oversize_stages(
        store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")

    result = _live(store, sid, by="user")

    assert result.data["by"] == "user"
    assert result.data["old_digest"] == root
    assert result.data["new_digest"] == _sha(plan_file.read_bytes())
    assert result.data["oversize_stages"] == []
    assert root in result.detail and result.data["new_digest"] in result.detail


@pytest.fixture
def live_objects(tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))


def test_an_edit_after_the_conversion_blocks_the_bound_records_again(
        store, tmp_path, live_objects):
    from agentctl import gates, plan_refs

    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    assert _live(store, sid).ok
    part = plan_file.parent / "demo.parts" / "stage-1-method.md"
    if os.environ.get(ACCEPT_PLAN_ENV) and not part.exists():
        pytest.skip("the plan given through the environment externalizes no stage-1 method")
    assert part.exists(), "the synthetic plan must externalize its stage-1 method"
    part.write_text(part.read_text(encoding="utf-8") + "an edit\n", encoding="utf-8")
    plan_refs.seal(plan_file)

    state = store.load(sid)
    stale = gates._plan_review_content_stale(state.plan_review, str(plan_file), state)

    assert stale is not None and "stale" in stale
    assert gates._receipt_binding_blocker(
        state.plan_presentations[0], str(plan_file), "plan presentation", state) is not None


def test_revert_restores_the_old_bytes_and_leaves_the_parts_on_disk(store, tmp_path, live_objects):
    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")
    old_bytes = plan_file.read_bytes()
    assert _live(store, sid).ok
    parts_before = _tree(plan_file.parent / f"{plan_file.stem}.parts")

    result = _live(store, sid, "--revert", by="user")

    assert result.ok, result.detail
    assert plan_file.read_bytes() == old_bytes
    state = store.load(sid)
    assert state.accepted_plan_digest == root
    assert [c.direction for c in state.plan_conversions] == ["convert", "revert"]
    assert _tree(plan_file.parent / f"{plan_file.stem}.parts") == parts_before
    assert result.data["parts_left_on_disk"] is True
    assert "stay on disk" in result.detail
    assert _bindings(state)["review"] == root


def test_revert_is_refused_when_the_plan_changed_after_the_conversion(
        store, tmp_path, live_objects):
    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    assert _live(store, sid).ok
    plan_file.write_bytes(plan_file.read_bytes() + b"\n# edited\n")
    state = store.load(sid)
    state.accepted_plan_digest = _sha(plan_file.read_bytes())
    store.save(state)

    result = _live(store, sid, "--revert")

    assert not result.ok and "not the new digest of the last conversion" in result.detail


def test_revert_without_a_conversion_is_refused(store, tmp_path, live_objects):
    sid, _, _ = _live_session(store, tmp_path, shape="bound")

    result = _live(store, sid, "--revert")

    assert not result.ok and "nothing to revert" in result.detail


def _refused(store, sid, *flags):
    result = _live(store, sid, *flags)
    assert result.ok is False
    return result.detail


def test_a_resolved_session_is_refused_naming_plan_convert(store, tmp_path, live_objects):
    from agentctl.state import GateRecord, Node

    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    state = store.load(sid)
    state.node = Node.RESOLVED.value
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="user")
    store.save(state)
    before = plan_file.read_bytes()

    detail = _refused(store, sid)

    assert "resolved or closed" in detail and "plan-convert" in detail
    assert plan_file.read_bytes() == before


def test_a_session_without_a_registered_plan_is_refused(store, tmp_path, live_objects):
    sid, _, _ = _live_session(store, tmp_path, shape="bound")
    state = store.load(sid)
    state.plan_path = None
    store.save(state)

    assert "no registered plan file" in _refused(store, sid)


def test_a_dispatched_stage_not_yet_recorded_is_refused(store, tmp_path, live_objects):
    from agentctl.state import (
        Actor, Criterion, Means, Outcome, Stage, StageStatus, Subject)

    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    state = store.load(sid)
    state.stages = [Stage(
        index=1, title="a", subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"), actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
        outcome=Outcome(status=StageStatus.ACTIVE.value))]
    store.save(state)
    before = plan_file.read_bytes()

    detail = _refused(store, sid)

    assert "dispatched and not yet recorded" in detail
    assert plan_file.read_bytes() == before


def test_a_plan_file_that_is_not_the_accepted_version_is_refused(store, tmp_path, live_objects):
    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    plan_file.write_bytes(plan_file.read_bytes() + b"\n")

    assert "differs from the session's accepted digest" in _refused(store, sid)


def test_a_conversion_whose_proof_fails_is_refused_and_changes_nothing(
        store, tmp_path, live_objects, monkeypatch):
    from agentctl import plan_refs

    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    before_bytes, before_state = plan_file.read_bytes(), store.load(sid).to_dict()
    real = plan_refs.write_field_file
    monkeypatch.setattr(plan_refs, "write_field_file", lambda p, t: real(p, t + "\n"))
    has_method = bool(_fixture_source()[1].count(b"method"))
    if os.environ.get(ACCEPT_PLAN_ENV) and not has_method:
        pytest.skip("the plan given through the environment has nothing to externalize")
    assert has_method, "the synthetic plan must have a field to externalize"

    detail = _refused(store, sid)

    assert "raw equivalence" in detail
    assert plan_file.read_bytes() == before_bytes
    assert store.load(sid).to_dict() == before_state
    assert not (plan_file.parent / f"{plan_file.stem}.parts").exists()


def test_a_plan_the_current_loader_refuses_is_not_converted_in_place(
        store, tmp_path, live_objects):
    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")
    refused = plan_file.parent / "demo-refused.toml"
    refused.write_bytes(REFUSED_SOURCE)
    state = store.load(sid)
    state.plan_path = str(refused)
    state.accepted_plan_digest = _sha(REFUSED_SOURCE)
    store.save(state)

    detail = _refused(store, sid)

    assert "current loader refuses" in detail and "plan-convert" in detail
    assert refused.read_bytes() == REFUSED_SOURCE


def _expected_new_root(plan_file: Path, old_bytes: bytes) -> str:
    from agentctl import plan_convert

    stage_max, field_max = plan_convert._limits()
    index = plan_convert.convert(
        old_bytes, plan_file.stem, field_max_chars=field_max, stage_max_chars=stage_max)
    return _sha(next(iter(index.values())))


def test_a_plan_file_changed_after_it_was_read_is_refused_and_not_linked(
        store, tmp_path, live_objects, monkeypatch):
    from agentctl import plan_convert

    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")
    real, edited = plan_convert._write_atomic, []

    def write_then_edit(path, data):
        real(path, data)
        if not edited:
            edited.append(path)
            plan_file.write_bytes(plan_file.read_bytes() + b"# edited meanwhile\n")

    monkeypatch.setattr(plan_convert, "_write_atomic", write_then_edit)

    detail = _refused(store, sid)

    assert edited, "the plan under test writes no part, so there is no window to edit in"
    assert "changed while it was being converted" in detail
    state = store.load(sid)
    assert state.plan_conversions == []
    assert state.accepted_plan_digest == root
    assert plan_file.read_bytes().endswith(b"# edited meanwhile\n")


def test_a_failure_at_the_index_swap_leaves_the_session_consistent_and_the_verb_retryable(
        store, tmp_path, live_objects, monkeypatch):
    from agentctl import gates, plan_convert

    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")
    old_bytes = plan_file.read_bytes()
    new_root = _expected_new_root(plan_file, old_bytes)
    real = plan_convert._write_atomic

    def failing_swap(path, data):
        if Path(path) == plan_file:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(plan_convert, "_write_atomic", failing_swap)

    detail = _refused(store, sid)

    assert "disk full" in detail and "run the verb again" in detail
    assert plan_file.read_bytes() == old_bytes
    state = store.load(sid)
    assert [(c.old_digest, c.new_digest) for c in state.plan_conversions] == [(root, new_root)]
    assert state.accepted_plan_digest == new_root
    assert gates._plan_review_content_stale(state.plan_review, str(plan_file), state) is None
    assert gates._receipt_binding_blocker(
        state.plan_presentations[0], str(plan_file), "plan presentation", state) is None

    monkeypatch.setattr(plan_convert, "_write_atomic", real)
    retried = _live(store, sid)

    assert retried.ok, retried.detail
    state = store.load(sid)
    assert _sha(plan_file.read_bytes()) == new_root
    assert [(c.old_digest, c.new_digest) for c in state.plan_conversions] == [(root, new_root)]
    assert state.accepted_plan_digest == new_root


def test_a_converted_plan_gets_the_version_snapshot_every_other_plan_version_gets(
        store, tmp_path, live_objects):
    sid, plan_file, root = _live_session(store, tmp_path, shape="bound")

    assert _live(store, sid).ok

    session_dir = store.path(sid).parent
    new_root = _sha(plan_file.read_bytes())
    assert (session_dir / f"plan-version-{new_root[:16]}.toml").read_bytes() == plan_file.read_bytes()
    assert (session_dir / f"plan-version-{root[:16]}.toml").is_file()


def test_the_oversize_report_is_what_the_submit_gate_blocks_by_construction():
    from agentctl import plan_convert, submission
    import tomllib

    big = _plan_text(method=LONG_METHOD).encode()
    raw = tomllib.loads(big.decode())
    stage_max = 600

    report = _convert(big, stage=stage_max)[1]["oversize_stages"]
    blocks = submission.plan_size_blocks(raw, stage_max)

    assert report and [(o["node"], o["chars"], o["message"]) for o in report] == [
        (b["node"], b["chars"], b["message"]) for b in blocks]
    assert plan_convert._oversize(raw, stage_max) == report


def test_an_oversize_stage_with_a_shifted_index_is_reported_under_its_declared_index():
    from agentctl import submission
    import tomllib

    shifted = (_plan_text(method=LONG_METHOD).replace("index = 1\n", "index = 7\n", 1)
               .replace("depends_on = [1]", "depends_on = [7]")).encode()
    stage_max = 600

    report = _convert(shifted, stage=stage_max)[1]["oversize_stages"]
    labelled = submission.plan_size_blocks(tomllib.loads(shifted.decode()), stage_max)
    assert [o["node"] for o in report] == [b["node"] for b in labelled] == [7]


def _run_to_the_last_result(store, sid, plan):
    from argparse import Namespace

    from agentctl import cli
    from conftest import STAGE_OBSERVATIONS

    def ns(**kw):
        return Namespace(**kw)

    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=None, architectural=False, external_effect=False,
        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation), store=store)


def test_an_agent_run_conversion_is_disclosed_at_verify_final_and_resolve(
        store, venued_fixtures_dir):
    from argparse import Namespace

    from agentctl import cli
    from agentctl.state import PlanConversion

    sid = "disclose-agent"
    _run_to_the_last_result(store, sid, str(venued_fixtures_dir / "plan_two_stage.toml"))
    state = store.load(sid)
    state.plan_conversions += [
        PlanConversion("a" * 64, "b" * 64, "c" * 64, "/p.toml", "agent", "convert"),
        PlanConversion("d" * 64, "e" * 64, "f" * 64, "/q.toml", "user", "convert")]
    store.save(state)

    final = cli.cmd_verify_final(Namespace(session=sid), store=store)

    assert final.ok, final.detail
    assert [c["old_digest"] for c in final.data["agent_plan_conversions"]] == ["a" * 64]
    assert "plan conversion(s) run by the agent" in final.detail
    for phase, note in (("searched", ""), ("skipped", "test fixture, nothing to record")):
        cli.cmd_plugin_record(Namespace(session=sid, plugin="experience", phase=phase, note=note),
                              store=store)
    resolved = cli.cmd_resolve(Namespace(session=sid, by="user", quality=5,
                                         quality_by="user-confirmed", quality_note=None),
                               store=store)
    assert resolved.ok, resolved.detail
    assert [c["old_digest"] for c in resolved.data["agent_plan_conversions"]] == ["a" * 64]
    assert "plan conversion(s) run by the agent" in resolved.detail


def test_a_conversion_run_by_the_user_alone_adds_no_disclosure(store, venued_fixtures_dir):
    from argparse import Namespace

    from agentctl import cli
    from agentctl.state import PlanConversion

    sid = "disclose-user"
    _run_to_the_last_result(store, sid, str(venued_fixtures_dir / "plan_two_stage.toml"))
    state = store.load(sid)
    state.plan_conversions.append(
        PlanConversion("d" * 64, "e" * 64, "f" * 64, "/q.toml", "user", "convert"))
    store.save(state)

    final = cli.cmd_verify_final(Namespace(session=sid), store=store)

    assert final.ok, final.detail
    assert "agent_plan_conversions" not in final.data
    assert "run by the agent" not in final.detail


def test_the_live_verb_is_registered_with_a_by_choice_and_no_default_session(store):
    from agentctl import cli

    args = cli.build_parser().parse_args(
        ["plan-convert-live", "--session", "s", "--by", "agent", "--revert"])

    assert (args.command, args.by, args.revert) == ("plan-convert-live", "agent", True)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["plan-convert-live", "--session", "s", "--by", "nobody"])
    assert "plan-convert-live" in cli.COMMANDS and "plan-convert" in cli.COMMANDS


# --- the digest-equivalence helper and its mutation control ---------------------------------------


def _linked_state(old="a" * 64, new="b" * 64):
    from agentctl.state import PlanConversion, SessionState

    state = SessionState(session_id="s", task_id="t")
    state.plan_conversions.append(PlanConversion(
        old_digest=old, new_digest=new, content_digest="c" * 64, plan_path="p", by="agent",
        direction="convert"))
    return state


def test_equivalence_is_symmetric_across_a_link_and_an_unlinked_digest_matches_nothing():
    from agentctl import gates

    state = _linked_state()

    assert gates.plan_digest_equivalent(state, "a" * 64, "b" * 64)
    assert gates.plan_digest_equivalent(state, "b" * 64, "a" * 64)
    assert gates.plan_digest_equivalent(state, "a" * 64, "a" * 64)
    assert not gates.plan_digest_equivalent(state, "a" * 64, "d" * 64)
    assert not gates.plan_digest_equivalent(None, "a" * 64, "b" * 64)
    assert gates.plan_digest_equivalent(None, "a" * 64, "a" * 64)
    assert not gates.plan_digest_equivalent(state, "", "a" * 64)


def test_a_revert_keeps_the_chain_connected():
    from agentctl import gates
    from agentctl.state import PlanConversion

    state = _linked_state()
    state.plan_conversions.append(PlanConversion(
        old_digest="b" * 64, new_digest="a" * 64, content_digest="c" * 64, plan_path="p",
        by="user", direction="revert"))

    assert gates.plan_digest_equivalent(state, "a" * 64, "b" * 64)
    assert gates.plan_lineage_root(state, "b" * 64) == "a" * 64
    assert gates.plan_lineage_root(state, "a" * 64) == "a" * 64


def _patch_equivalence_everywhere(monkeypatch):
    """Replace `plan_digest_equivalent` with plain equality under EVERY name that is
    bound to it in a loaded `agentctl` module."""
    from agentctl import gates

    real = gates.plan_digest_equivalent
    bound = []
    for name, module in list(sys.modules.items()):
        if not (name == "agentctl" or name.startswith("agentctl.")):
            continue
        for attr, value in list(vars(module).items()):
            if value is real:
                monkeypatch.setattr(module, attr, lambda state, a, b: a == b)
                bound.append((name, attr))
    assert ("agentctl.gates", "plan_digest_equivalent") in bound
    return bound


def _counting_state(tmp_path):
    from agentctl.state import GateRecord

    plan_file = tmp_path / "counted.toml"
    plan_file.write_bytes(b"[meta]\n")
    state = _linked_state(old="a" * 64, new=_sha(plan_file.read_bytes()))
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="user")
    state.plan_review_counted_digest = "a" * 64
    return state, str(plan_file)


def test_the_counted_digest_comparison_does_not_count_a_converted_plan_twice(tmp_path):
    from agentctl import cli

    state, target = _counting_state(tmp_path)

    cli._count_plan_review_round(state, target)

    assert state.plan_review_rounds == 0
    assert state.plan_review_counted_digest == "a" * 64


def test_with_equivalence_reduced_to_equality_the_gates_block_and_the_round_is_counted(
        tmp_path, monkeypatch):
    from agentctl import cli, gates
    from agentctl.state import PlanReview

    _patch_equivalence_everywhere(monkeypatch)
    state, target = _counting_state(tmp_path)
    new = _sha(Path(target).read_bytes())
    review = PlanReview(plan_path=target, verdict="pass", reviewer="thinker",
                        plan_sha256="a" * 64)

    cli._count_plan_review_round(state, target)

    assert state.plan_review_rounds == 1
    assert gates._plan_review_content_stale(review, target, state) is not None
    assert new != "a" * 64
    receipt = type("R", (), {"plan_path": target, "plan_sha256": "a" * 64})()
    assert gates._receipt_binding_blocker(receipt, target, "plan presentation", state) is not None


def test_the_accumulator_is_given_one_digest_for_both_forms_of_a_plan():
    from agentctl import gates

    state = _linked_state()

    assert gates.plan_lineage_root(state, "a" * 64) == gates.plan_lineage_root(state, "b" * 64)
    assert gates.plan_lineage_root(None, "b" * 64) == "b" * 64
    assert gates.plan_lineage_root(state, "e" * 64) == "e" * 64


# --- the live rebinding, rerun against a plan the caller names -----------------------------------


def test_the_rebinding_tests_run_green_against_a_plan_given_through_the_environment(tmp_path):
    from agentctl import plan_convert

    stem_dir = tmp_path / "given"
    stem_dir.mkdir()
    given = stem_dir / "given.toml"
    given.write_bytes(_plan_text(goal="a goal long enough to move to its own file",
                                 method=LONG_METHOD * 3).encode())
    expected_content = _content_digest(given)
    stage_max, field_max = plan_convert._limits()
    expected_index = plan_convert.convert(
        given.read_bytes(), "given", field_max_chars=field_max, stage_max_chars=stage_max)[
        "given.toml"]
    env = {**os.environ, ACCEPT_PLAN_ENV: str(given), "PYTHONDONTWRITEBYTECODE": "1"}

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-k", "live_rebinding", "--no-header", str(Path(__file__))],
        capture_output=True, text=True, timeout=600, env=env, cwd=str(REPO))

    assert done.returncode == 0, done.stdout[-800:]
    assert "skipped" not in done.stdout
    import re

    lines = re.findall(
        r"live_rebinding before=([0-9a-f]{64}) after=([0-9a-f]{64}) root=([0-9a-f]{64})",
        done.stdout)
    summary = re.search(r"(\d+) passed", done.stdout)
    assert summary is not None, done.stdout[-800:]
    passed = int(summary.group(1))
    assert passed >= 3
    assert len(lines) == passed
    for before, after, root in lines:
        assert before == _sha(given.read_bytes())
        assert after == expected_content
        assert root == _sha(expected_index)


def test_an_unreadable_given_plan_fails_the_rebinding_tests_instead_of_skipping_them(tmp_path):
    env = {**os.environ, ACCEPT_PLAN_ENV: str(tmp_path / "missing.toml"),
           "PYTHONDONTWRITEBYTECODE": "1"}

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-k", "live_rebinding", "--no-header", str(Path(__file__))],
        capture_output=True, text=True, timeout=600, env=env, cwd=str(REPO))

    assert done.returncode != 0
    assert "cannot be read" in done.stdout
    assert "skipped" not in done.stdout.split("short test summary")[-1].splitlines()[-1]


def test_the_given_plan_is_copied_and_the_given_path_is_never_written(
        store, tmp_path, live_objects, monkeypatch):
    given = tmp_path / "given" / "mine.toml"
    given.parent.mkdir()
    given.write_bytes(_plan_text(method=LONG_METHOD * 3).encode())
    monkeypatch.setenv(ACCEPT_PLAN_ENV, str(given))
    before = _tree(given.parent)

    sid, plan_file, _ = _live_session(store, tmp_path, shape="bound")
    assert _live(store, sid).ok

    assert plan_file.name == "mine.toml" and plan_file != given
    assert _tree(given.parent) == before


# --- the inventory of root-digest comparisons ---------------------------------------------------


LISTED_NAMES = frozenset({
    "plan_sha256", "accepted_plan_digest", "plan_snapshot_hash", "plan_review_counted_digest",
    "root_digest", "plan_digest"})
HELPER_CALLS = frozenset({"_file_sha256", "_plan_file_sha256", "plan_digest_equivalent"})
DISPOSITIONS = frozenset({
    "routed through plan_digest_equivalent", "audit only", "not a binding"})
POSITIVE_CONTROLS = frozenset({
    "scripts/agentctl/gates.py:_plan_review_content_stale",
    "scripts/agentctl/cli_common.py:_count_plan_review_round",
    "scripts/plan-review-topological.py:Driver.launch",
})


def _names_listed(node) -> bool:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_names_listed(v) for v in node.values)
    if isinstance(node, ast.Name):
        return node.id in LISTED_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in LISTED_NAMES
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in LISTED_NAMES
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value in LISTED_NAMES):
        return True
    return False


def _compares_listed_digest(node) -> bool:
    """Selector (1): a comparison with an operand naming a recorded root digest."""
    return isinstance(node, ast.Compare) and any(
        _names_listed(operand) for operand in [node.left, *node.comparators])


def _call_name(node) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(
        func, ast.Attribute) else None


def _selected(node) -> bool:
    """Selectors (1)-(4): a compare of a listed digest, `hashlib.sha256(x.read_bytes())`,
    a call of one of the hashing helpers, a call of `plan_digest_equivalent`."""
    if _compares_listed_digest(node):
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if (isinstance(func, ast.Attribute) and func.attr == "sha256"
            and isinstance(func.value, ast.Name) and func.value.id == "hashlib" and node.args):
        argument = node.args[0]
        if (isinstance(argument, ast.Call) and isinstance(argument.func, ast.Attribute)
                and argument.func.attr == "read_bytes"):
            return True
    return _call_name(node) in HELPER_CALLS


class _Selector(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.scope: list[str] = []
        self.found: dict[str, list[ast.AST]] = {}

    def _enter(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def generic_visit(self, node):
        if _selected(node):
            key = f"{self.rel}:{'.'.join(self.scope) or '<module>'}"
            self.found.setdefault(key, []).append(node)
        super().generic_visit(node)


def enumerate_root_digest_comparisons(root: Path) -> dict[str, list[ast.AST]]:
    """`<repo-relative file>:<qualname>` -> the selected nodes, for every `*.py` under
    `root/scripts` except `scripts/tests/`."""
    found: dict[str, list[ast.AST]] = {}
    for path in sorted((root / "scripts").glob("**/*.py")):
        rel = path.relative_to(root)
        if rel.parts[:2] == ("scripts", "tests"):
            continue
        selector = _Selector(rel.as_posix())
        selector.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.update(selector.found)
    return found


def _comparison_rows() -> dict[str, str]:
    lines = INVENTORY.read_text(encoding="utf-8").splitlines()
    start = lines.index("### Root-digest comparisons") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("#")), len(lines))
    rows: dict[str, str] = {}
    for line in lines[start:end]:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set(cells[0]) <= set("-: ") or cells[0].lower() == "site":
            continue
        rows[cells[0].replace("`", "")] = cells[1]
    return rows


def test_the_enumerator_selects_what_it_selects_in_a_planted_tree(tmp_path):
    scripts = tmp_path / "scripts"
    (scripts / "tests").mkdir(parents=True)
    (scripts / "tool.py").write_text(
        "import hashlib\n\n"
        "class Gate:\n"
        "    def stale(self, review, current):\n"
        "        return review.plan_sha256 != current\n\n"
        "    def other(self, a, b):\n"
        "        return a == b\n\n"
        "def digest(plan_file):\n"
        "    return hashlib.sha256(plan_file.read_bytes()).hexdigest()\n\n"
        "def routed(state, a, b):\n"
        "    return plan_digest_equivalent(state, a, b)\n",
        encoding="utf-8")
    (scripts / "tests" / "test_x.py").write_text(
        "def t(x):\n    return x.plan_sha256 == 1\n", encoding="utf-8")

    assert set(enumerate_root_digest_comparisons(tmp_path)) == {
        "scripts/tool.py:Gate.stale", "scripts/tool.py:digest", "scripts/tool.py:routed"}


def test_the_inventory_rows_are_exactly_the_enumerated_comparisons():
    rows = set(_comparison_rows())
    enumerated = set(enumerate_root_digest_comparisons(REPO))

    assert enumerated - rows == set(), "comparisons missing from docs/operations/plan-readers.md"
    assert rows - enumerated == set(), "inventory rows that no longer name a comparison"


def test_every_comparison_row_uses_a_disposition_from_the_closed_list():
    rows = _comparison_rows()

    assert rows
    assert {key: d for key, d in rows.items() if d not in DISPOSITIONS} == {}


def test_every_routed_row_calls_the_helper_and_keeps_no_raw_comparison():
    found = enumerate_root_digest_comparisons(REPO)
    routed = [key for key, d in _comparison_rows().items()
              if d == "routed through plan_digest_equivalent"]

    assert routed
    for key in routed:
        nodes = found[key]
        calls = [n for n in nodes if isinstance(n, ast.Call)
                 and _call_name(n) == "plan_digest_equivalent"]
        raw = [n for n in nodes if _compares_listed_digest(n)]
        assert calls, f"{key} does not call plan_digest_equivalent"
        assert raw == [], f"{key} still compares a recorded root digest directly"


def test_the_enumerator_still_finds_the_pinned_positive_controls():
    enumerated = set(enumerate_root_digest_comparisons(REPO))

    assert POSITIVE_CONTROLS <= enumerated
    assert POSITIVE_CONTROLS <= set(_comparison_rows())


def test_a_routed_function_that_is_given_back_a_raw_comparison_is_caught():
    source = (
        "def f(state, review, current):\n"
        "    ok = plan_digest_equivalent(state, review.plan_sha256, current)\n"
        "    return ok and review.plan_sha256 == current\n")
    selector = _Selector("scripts/x.py")
    selector.visit(ast.parse(source))
    nodes = selector.found["scripts/x.py:f"]

    assert any(_compares_listed_digest(n) for n in nodes)


def test_the_inventory_states_the_enumerator_limits_and_the_hand_listed_comparisons():
    text = INVENTORY.read_text(encoding="utf-8")
    section = text[text.index("### What the enumerator misses"):]

    assert "review_rounds_last_sha256" in section
    assert "plan_lineage_root" in section
    assert "does not see" in section
