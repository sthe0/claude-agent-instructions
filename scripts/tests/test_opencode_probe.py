"""Tests of the OpenCode probe suite's own machinery (``scripts/opencode_probe/``).

Nothing here reaches a real model or the user's OpenCode: a synthetic probe tree stands in for the
phase packages, and the fake binary (``tests/fixtures/opencode-fake/opencode``) stands in for the
binary. Every test here passes or fails on its own; the checks of the live record are in
``live_opencode_probe_record.py``, outside the default run.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import opencode_live
from opencode_probe import cli, core, coverage, harness, isolated, negative, paths, record, runner, stub, subject_only, userconfig
from opencode_probe.mechanisms import c_p1, c_p6, c_p7, c_p13

FAKE = Path(__file__).resolve().parent / "fixtures" / "opencode-fake" / "opencode"
STUB_REQUEST = {"stream": False, "tools": [{"function": {"name": "t", "parameters": {}}}]}


def case_source(case_id, k7):
    return f'"""{case_id} predicate."""\nCASE_ID = "{case_id}"\nK7 = {k7}\n'


def make_probe(tmp_path):
    """A probe tree under tmp_path/scripts/opencode_probe with phases alpha (A1 K7, A2) and beta (B1)."""
    probe = tmp_path / "scripts" / "opencode_probe"
    files = {
        "pin.toml": 'version = "2.3.2"\n',
        "shared.py": "SHARED = 1\n",
        "plugin/index.mjs": "export default {}\n",
        "alpha/__init__.py": 'NEGATIVE_ANCHOR = "A1"\nSUBJECT_FILES = ["subject_a.txt"]\n',
        "alpha/c_a1.py": case_source("A1", True),
        "alpha/c_a2.py": case_source("A2", False),
        "beta/__init__.py": 'NEGATIVE_ANCHOR = "B1"\n',
        "beta/c_b1.py": case_source("B1", False),
    }
    for rel, text in files.items():
        (probe / rel).parent.mkdir(parents=True, exist_ok=True)
        (probe / rel).write_text(text, encoding="utf-8")
    (tmp_path / "subject_a.txt").write_text("subject\n", encoding="utf-8")
    return probe


def phase_of(probe, name):
    return core.get_phase(name, probe)


def write_run(state, phase, probe, statuses, version="2.3.2", kind="runs", event_logs=True):
    cases = {cid: {"status": status, "evidence": {}} for cid, status in statuses.items()}
    rec_dir = record.new_record_dir(state, kind)
    record.write_record(rec_dir, version, phase.name, core.phase_hash(phase, probe), cases)
    if event_logs:
        (rec_dir / "events").mkdir()
        for case_id in cases:
            (rec_dir / "events" / f"{case_id}.jsonl").touch()
    return rec_dir / "record.json"


def fake_binary(tmp_path):
    target_dir = tmp_path / "fake-bin"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / "opencode"
    shutil.copyfile(FAKE, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return str(target)


def fake_harness(tmp_path, case_id="T1"):
    return harness.Harness(case_id, tmp_path / "events", state=tmp_path / "state", binary=fake_binary(tmp_path))


FAKE_SCENARIO = {"models": {"m1": [{"text": "x"}]}}


def post_stub(port, model, out):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", method="POST",
                                     data=json.dumps({"model": model, **STUB_REQUEST}).encode(),
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            out[model] = (response.status, json.loads(response.read()))
    except urllib.error.HTTPError as error:
        out[model] = (error.code, json.loads(error.read()))


def test_pin_mismatch_fails(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"}, version="2.3.1")
    problems = record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)
    assert problems and "no record for version 2.3.2" in problems[0]


def test_version_mismatch_refuses_launch(tmp_path, monkeypatch):
    marker = tmp_path / "launched"
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", "9.9.9")
    monkeypatch.setenv("FAKE_OPENCODE_MARKER", str(marker))
    with pytest.raises(harness.VersionMismatch):
        fake_harness(tmp_path).run(FAKE_SCENARIO, label="wrong")
    assert not marker.exists()


def test_user_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(tmp_path / "probe-state"))
    home = tmp_path / "user-home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text("{}\n", encoding="utf-8")
    userconfig.write_snapshot(home)
    run = fake_harness(tmp_path).run(FAKE_SCENARIO, label="writes-home", env={"FAKE_OPENCODE_WRITE_HOME": "1"})
    assert run.exists("home/.claude/CLAUDE.md")
    assert not (home / ".claude").exists()
    assert userconfig.compare(home) == 0
    (home / ".config" / "opencode" / "opencode.json").write_text('{"changed": true}\n', encoding="utf-8")
    assert userconfig.compare(home) == 1


def test_single_exact_version_string():
    version = core.read_pin()["version"]
    mentions = [p for p in core.PROBE_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                and version in p.read_text(encoding="utf-8", errors="replace")]
    assert [p.name for p in mentions] == [core.PIN_NAME]


def test_a_record_dependent_check_fails_without_a_record(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(tmp_path / "state"))
    with pytest.raises(pytest.fail.Exception, match="no record of phase mechanisms"):
        opencode_live.latest_record("mechanisms")


def test_skips_without_binary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_OPENCODE_BIN", str(tmp_path / "absent" / "opencode"))
    assert cli.main(["run", "--phase", "mechanisms"]) == 3
    assert cli.main(["run", "--phase", "mechanisms", "--require-live"]) == 2
    assert cli.main(["negative", "--phase", "mechanisms", "--inert-deny"]) == 0
    captured = capsys.readouterr()
    assert "SKIP: opencode live: binary" in captured.err
    assert negative.HOLDS_LINE not in captured.out


def test_pin_bump_requires_matching_pass(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == []
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == []
    (probe / "pin.toml").write_text(
        'version = "2.4.0"\n\n[last_pass.alpha]\nversion = "2.3.2"\nsuite_sha256 = "x"\nrun = "y"\nrecord_sha256 = "z"\n',
        encoding="utf-8")
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"}, version="2.4.0")
    assert record.pin_record(state, alpha, probe) == []
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == []


def test_pin_is_exact_2x():
    assert re.fullmatch(r"2\.\d+\.\d+", core.read_pin()["version"])


def test_require_pass_accepts_absent_only_outside_k7(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    write_run(state, alpha, probe, {"A1": "pass", "A2": "absent"})
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == ["alpha: pin.toml has no last_pass entry (run `pin --record`)"]
    write_run(state, alpha, probe, {"A1": "absent", "A2": "pass"})
    problems = record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)
    assert "alpha/A1: absent is not accepted" in problems
    assert record.accepted(record.ABSENT, False) and not record.accepted(record.ABSENT, True)


def test_unobserved_never_accepted():
    assert not record.accepted(record.UNOBSERVED, False)
    assert not record.accepted(record.UNOBSERVED, True)
    assert not record.accepted(record.SKIP, False)
    assert not record.accepted(record.FAIL, False)


def test_run_exit_codes(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "binary_problem", lambda: None)
    outcome = {}
    monkeypatch.setattr(runner, "run_phase", lambda phase, **kw: (tmp_path, outcome["cases"]))
    outcome["cases"] = {"P1": {"status": "pass"}, "P8": {"status": "absent"}}
    assert cli.main(["run", "--phase", "mechanisms"]) == 0
    outcome["cases"] = {"P1": {"status": "fail"}}
    assert cli.main(["run", "--phase", "mechanisms"]) == 1
    outcome["cases"] = {"P1": {"status": "absent"}}
    assert cli.main(["run", "--phase", "mechanisms"]) == 1
    outcome["cases"] = {"P1": {"status": "pass"}, "P6": {"status": "unobserved"}}
    assert cli.main(["run", "--phase", "mechanisms"]) == 3
    monkeypatch.setattr(runner, "run_phase", lambda phase, **kw: (_ for _ in ()).throw(harness.VersionMismatch("x")))
    assert cli.main(["run", "--phase", "mechanisms"]) == 2


def test_negative_dir_never_read(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"}, kind="negative")
    assert record.latest_record(state, "alpha", "2.3.2", core.phase_hash(alpha, probe)) is None
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)


def test_phase_hash_excludes_pin(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    before = core.phase_hash(alpha, probe)
    (probe / "pin.toml").write_text('version = "2.3.2"\n\n[last_pass.alpha]\nversion = "v"\n', encoding="utf-8")
    assert core.phase_hash(alpha, probe) == before


def test_shared_file_change_stales_all_phases(tmp_path):
    probe = make_probe(tmp_path)
    alpha, beta = phase_of(probe, "alpha"), phase_of(probe, "beta")
    before = (core.phase_hash(alpha, probe), core.phase_hash(beta, probe))
    (probe / "shared.py").write_text("SHARED = 2\n", encoding="utf-8")
    assert core.phase_hash(alpha, probe) != before[0]
    assert core.phase_hash(beta, probe) != before[1]


def test_compare_fails_without_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(tmp_path / "state"))
    assert userconfig.compare(tmp_path / "home") == 1
    assert "no snapshot" in capsys.readouterr().out


def test_negative_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(core, "binary_problem", lambda: None)
    monkeypatch.setattr(negative, "last_pass_cases", lambda phase: {"P0": {"status": "pass"}})
    marked = {"status": "fail", "evidence": {"marked_executed": True}}
    outcome = {"cases": {"P1": marked, "P13": marked, "P0": {"status": "pass", "evidence": {}}}}
    monkeypatch.setattr(runner, "run_phase", lambda phase, **kw: (tmp_path, outcome["cases"]))
    assert cli.main(["negative", "--phase", "mechanisms", "--inert-deny"]) == 1
    assert negative.HOLDS_LINE in capsys.readouterr().out.splitlines()
    outcome["cases"] = {"P1": {"status": "pass", "evidence": {}}, "P0": {"status": "pass", "evidence": {}}}
    assert cli.main(["negative", "--phase", "mechanisms", "--inert-deny"]) == 0
    printed = capsys.readouterr().out
    assert negative.BROKEN_LINE in printed and negative.HOLDS_LINE not in printed.splitlines()


def test_negative_requires_deny_resting_cases():
    phase = core.get_phase("mechanisms")
    marked = {"status": "fail", "evidence": {"marked_executed": True}}
    unmarked = {"status": "fail", "evidence": {"marked_executed": False}}
    last = {"P0": {"status": "pass"}}
    assert "anchor" in negative.judge(phase, {"P0": {"status": "pass", "evidence": {}}}, last)[0]
    assert "no recorded last pass" in negative.judge(phase, {"P1": marked}, None)[0]
    assert negative.judge(phase, {"P1": marked}, last) == []
    assert "P13" in negative.judge(phase, {"P1": marked, "P13": unmarked}, last)[0]


def test_event_log_per_case(tmp_path):
    events = tmp_path / "events"
    harness.Harness("T1", events, state=tmp_path / "state", binary=fake_binary(tmp_path)).run(FAKE_SCENARIO, label="a")
    harness.Harness("T2", events, state=tmp_path / "state", binary=fake_binary(tmp_path)).run(FAKE_SCENARIO, label="b")
    first = [json.loads(line) for line in (events / "T1.jsonl").read_text().splitlines()]
    second = [json.loads(line) for line in (events / "T2.jsonl").read_text().splitlines()]
    assert {e["label"] for e in first} == {"a"} and {e["label"] for e in second} == {"b"}
    assert {"meta", "stdout"} <= {e["src"] for e in first}


def test_stub_serves_concurrent_models(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"models": {"m1": [{"text": "one"}], "m2": [{"text": "two"}]}}), encoding="utf-8")
    server, port = stub.start_stub(scenario)
    out = {}
    threads = [threading.Thread(target=post_stub, args=(port, "m1", out)),
               threading.Thread(target=post_stub, args=(port, "m2", out))]
    try:
        threads[0].start()
        threads[1].start()
        threads[0].join(30)
        threads[1].join(30)
    finally:
        server.shutdown()
    assert out["m1"][1]["choices"][0]["message"]["content"] == "one"
    assert out["m2"][1]["choices"][0]["message"]["content"] == "two"
    logged = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert sorted(e["model"] for e in logged) == ["m1", "m2"]


def test_stub_error_turn_answers_its_status(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"models": {"m1": [{"error": 400}]}}), encoding="utf-8")
    server, port = stub.start_stub(scenario)
    out = {}
    try:
        post_stub(port, "m1", out)
    finally:
        server.shutdown()
    assert out["m1"][0] == 400 and out["m1"][1]["error"]["type"] == "stub_error"


def test_subject_file_change_stales_only_its_phase(tmp_path):
    probe = make_probe(tmp_path)
    alpha, beta = phase_of(probe, "alpha"), phase_of(probe, "beta")
    before = (core.phase_hash(alpha, probe), core.phase_hash(beta, probe))
    (tmp_path / "subject_a.txt").write_text("changed\n", encoding="utf-8")
    assert core.phase_hash(alpha, probe) != before[0]
    assert core.phase_hash(beta, probe) == before[1]


def test_run_never_writes_pin(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", core.read_pin()["version"])
    pin = core.PROBE_DIR / core.PIN_NAME
    before = (pin.read_bytes(), pin.stat().st_mtime_ns)
    rec_dir, cases = runner.run_phase(core.get_phase("mechanisms"), case_ids={"P11"}, state=tmp_path / "state",
                                      binary=fake_binary(tmp_path))
    assert cases["P11"]["status"] == record.PASS and (rec_dir / "record.json").is_file()
    assert (pin.read_bytes(), pin.stat().st_mtime_ns) == before


def make_usage_db(root, tokens_input, tokens_output):
    import sqlite3

    path = root / c_p6.DB_REL
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute("create table session_v2 (id text, parent_id text, model text, cost real, "
                "tokens_input integer, tokens_output integer)")
    con.execute("insert into session_v2 values ('ses_1', null, 'm1', 0.0, ?, ?)", (tokens_input, tokens_output))
    con.commit()
    con.close()


def test_p6_absent_names_missing_fields(tmp_path):
    requests = [{"model": "m1", "tools": ["t"]}, {"model": "m1", "tools": []}]
    success = SimpleNamespace(root=tmp_path, requests=requests)
    assert c_p6._store(success)[1] == {"store": f"no readable {c_p6.DB_REL} under the isolated root"}
    make_usage_db(tmp_path, 5, 3)
    evidence, missing, row = c_p6._store(success)
    assert list(missing) == ["tokens"] and "stored {'input': 5, 'output': 3}" in missing["tokens"]
    assert evidence["table"] == "session_v2" and row["id"] == "ses_1"


def test_p6_counts_every_request_of_the_model(tmp_path):
    make_usage_db(tmp_path, 22, 14)
    requests = [{"model": "m1", "tools": ["t"]}, {"model": "m1", "tools": []}]
    assert c_p6._store(SimpleNamespace(root=tmp_path, requests=requests))[1] == {}


def test_isolated_env_asserts_scratch_in_effect(tmp_path):
    root = isolated.make_root(tmp_path)
    env = isolated.isolated_env(root, caller={"PATH": os.environ["PATH"]})
    isolated.assert_scratch_in_effect(root, env)
    env["HOME"] = str(tmp_path / "elsewhere")
    with pytest.raises(isolated.ScratchNotInEffect):
        isolated.assert_scratch_in_effect(root, env)


def test_isolated_env_fills_agent_home(tmp_path):
    root = isolated.make_root(tmp_path)
    env = isolated.isolated_env(root, caller={})
    agent_home = Path(env["CLAUDE_AGENT_HOME"])
    assert (agent_home / "CLAUDE.md").read_bytes() == (core.REPO_ROOT / "CLAUDE.md").read_bytes()
    assert (agent_home / "skills").is_dir()


def test_isolated_root_under_runs_with_marker(tmp_path):
    root = isolated.make_root(tmp_path)
    assert root.parent == (tmp_path / "runs").resolve() and root.name.startswith("root.")
    assert (root / isolated.MARKER).is_file()


PLUGIN_DRIVER = """
import plugin from %(plugin)s;
const hooks = {};
const domain = () => ({ hook: (name, fn) => { hooks[name] = fn; } });
plugin.setup({ tool: domain(), permission: domain(), session: domain() });
const out = {};
try { hooks["execute.before"]({ id: "c1", tool: "shell", input: { command: "touch m" } }); out.threw = false; }
catch (error) { out.threw = String(error.message); }
const evaluated = { action: "shell", resources: ["touch m"], source: { id: "c1" }, effect: "allow" };
hooks["evaluate"](evaluated);
out.effect = evaluated.effect;
try { hooks["prompt"]({ sessionID: "s1", prompt: { text: "go" } }); out.prompt_threw = false; }
catch (error) { out.prompt_threw = String(error.message); }
console.log(JSON.stringify(out));
"""


def drive_plugin(tmp_path, inert):
    rules = {"throw": [{"name": "shell", "contains": "m"}], "deny": [{"name": "shell", "contains": "m"}],
             "refuse_prompt": True}
    env = {"PATH": os.environ["PATH"], "PROBE_LOG": str(tmp_path / f"log-{inert}.jsonl"),
           "PROBE_RULES": json.dumps(rules), **({"PROBE_INERT": "1"} if inert else {})}
    driver = tmp_path / f"driver-{inert}.mjs"
    driver.write_text(PLUGIN_DRIVER % {"plugin": json.dumps((core.PROBE_DIR / "plugin" / "index.mjs").as_uri())},
                      encoding="utf-8")
    done = subprocess.run(["node", str(driver)], env=env, capture_output=True, text=True, timeout=30, check=True)
    log = [json.loads(line) for line in (tmp_path / f"log-{inert}.jsonl").read_text(encoding="utf-8").splitlines()]
    return json.loads(done.stdout), log


def test_inert_deny_disables_all_three_refusals(tmp_path):
    live, live_log = drive_plugin(tmp_path, inert=False)
    assert live == {"threw": "probe throw: shell", "effect": "deny", "prompt_threw": "probe throw: prompt"}
    assert {e["mechanism"] for e in live_log if e["at"] == "refusal"} == {"PreToolUse", "permission.evaluate",
                                                                          "UserPromptSubmit"}
    inert, inert_log = drive_plugin(tmp_path, inert=True)
    assert inert == {"threw": False, "effect": "allow", "prompt_threw": False}
    assert not [e for e in inert_log if e["at"] == "refusal"]
    assert {e["at"] for e in inert_log} >= {"tool.execute.before", "permission.evaluate", "session.prompt"}


def test_inert_harness_sets_the_plugin_switch(tmp_path):
    inert = harness.Harness("T1", tmp_path / "e", state=tmp_path / "s", binary=fake_binary(tmp_path), inert=True)
    plain = harness.Harness("T1", tmp_path / "e", state=tmp_path / "s", binary=fake_binary(tmp_path))
    with inert.world(FAKE_SCENARIO) as inert_world, plain.world(FAKE_SCENARIO) as plain_world:
        assert inert_world.env["PROBE_INERT"] == "1"
        assert "PROBE_INERT" not in plain_world.env


def test_subject_coverage_names_uncovered_file(monkeypatch, capsys):
    monkeypatch.setattr(coverage, "changed_files", lambda since, until: [
        "scripts/lib/new_thing.py", "scripts/opencode_probe/x.py", "scripts/tests/t.py", "README.md"])
    assert coverage.uncovered("a", "b") == ["scripts/lib/new_thing.py"]
    assert cli.main(["subject-coverage", "--since", "a", "--until", "b"]) == 1
    assert "scripts/lib/new_thing.py" in capsys.readouterr().out


def test_subject_coverage_requires_until():
    with pytest.raises(SystemExit) as exc:
        cli.main(["subject-coverage", "--since", "a"])
    assert exc.value.code == 2


def test_subject_only_set_counts_for_coverage_not_phases(monkeypatch):
    phase = core.get_phase("mechanisms")
    before = core.phase_hash(phase)
    monkeypatch.setattr(subject_only, "SUBJECT_ONLY", {"scripts/lib/new_thing.py": "a subject no phase hashes"})
    monkeypatch.setattr(coverage, "changed_files", lambda since, until: ["scripts/lib/new_thing.py"])
    assert coverage.uncovered("a", "b") == []
    assert core.phase_hash(phase) == before
    assert "scripts/lib/new_thing.py" not in phase.subject_files


def test_isolated_env_unsets_integration_dirs(tmp_path):
    root = isolated.make_root(tmp_path)
    caller = {"PATH": "/usr/bin", "AGENTCTL_SESSION_ID": "s", "AGENTCTL_PLAN": "p", "CLAUDE_CODE_SESSION_ID": "c",
              "OPENCODE_CONFIG_DIR": "/elsewhere", "XDG_CONFIG_HOME": "/elsewhere", "CLAUDE_CONFIG_DIR": "/elsewhere"}
    env = isolated.isolated_env(root, caller)
    assert not [k for k in env if k.startswith("AGENTCTL_") or k == "CLAUDE_CODE_SESSION_ID"]
    assert env["OPENCODE_CONFIG_DIR"].startswith(str(root)) and env["XDG_CONFIG_HOME"].startswith(str(root))
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(root))


def test_p7_leaves_home_stand_ins_unchanged(tmp_path):
    untouched = fake_harness(tmp_path).run(FAKE_SCENARIO, label="plain", stand_ins=True)
    assert untouched.stand_ins_before and untouched.stand_ins_changed() == []
    written = fake_harness(tmp_path).run(FAKE_SCENARIO, label="writes", stand_ins=True,
                                         env={"FAKE_OPENCODE_WRITE_HOME": "1"})
    assert written.stand_ins_changed() == [".claude/CLAUDE.md"]


def test_phase_package_discovered_without_core_edit(tmp_path):
    probe = make_probe(tmp_path)
    before = {p.name: core.phase_hash(p, probe) for p in core.discover_phases(probe)}
    (probe / "gamma").mkdir()
    (probe / "gamma" / "__init__.py").write_text('NEGATIVE_ANCHOR = "G1"\n', encoding="utf-8")
    (probe / "gamma" / "c_g1.py").write_text(case_source("G1", False), encoding="utf-8")
    after = {p.name: core.phase_hash(p, probe) for p in core.discover_phases(probe)}
    assert sorted(after) == ["alpha", "beta", "gamma"]
    assert {name: after[name] for name in before} == before


def test_record_reader_rejects_foreign_files(tmp_path):
    path = tmp_path / "record.json"
    assert record.read_record(path) is None
    path.write_text("not json", encoding="utf-8")
    assert record.read_record(path) is None
    path.write_text(json.dumps({"schema": "other/1"}), encoding="utf-8")
    assert record.read_record(path) is None
    path.write_text(json.dumps({"schema": record.SCHEMA, "cases": {}}), encoding="utf-8")
    assert record.read_record(path) == {"schema": record.SCHEMA, "cases": {}}


def test_latest_record_takes_the_newest_matching_one(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    first = write_run(state, alpha, probe, {"A1": "fail", "A2": "pass"})
    second = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    (state / "runs" / "29990101T000000Z").mkdir()
    found = record.latest_record(state, "alpha", "2.3.2", core.phase_hash(alpha, probe))
    assert found[0] == second and found[0] != first


def test_check_record_requires_an_event_log_per_case(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    path = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == []
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == []
    (path.parent / "events" / "A2.jsonl").unlink()
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == [
        "alpha/A2: no raw event log A2.jsonl beside the record"]


def test_a_newer_passing_run_does_not_unpin_the_pinned_record(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == []
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == []
    write_run(state, alpha, probe, {"A1": "fail", "A2": "pass"})
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == [
        "alpha/A1: fail is not accepted"]


def test_pin_refuses_a_record_without_event_logs(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"}, event_logs=False)
    assert record.pin_record(state, alpha, probe) == [
        "alpha/A1: no raw event log A1.jsonl beside the record",
        "alpha/A2: no raw event log A2.jsonl beside the record"]


def test_case_check_record_hook_problems_are_reported(tmp_path):
    probe = make_probe(tmp_path)
    hook = "\n\ndef check_record(entry, cases):\n    return [] if entry['evidence'] else ['shape wrong']\n"
    (probe / "alpha" / "c_a1.py").write_text(case_source("A1", True) + hook, encoding="utf-8")
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    path = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == ["alpha/A1: shape wrong"]
    rec = record.read_record(path)
    rec["cases"]["A1"]["evidence"] = {"k": 1}
    path.write_text(json.dumps(rec), encoding="utf-8")
    assert record.pin_record(state, alpha, probe) == []


CODE_MODE = {"before_fires_for_inner_call_under_own_name": False, "evaluate_fires_for_inner_call_under_own_name": False,
             "before_rule_stops_inner_effect": None, "evaluate_deny_stops_inner_effect": None,
             "execute_switchable_by_config": True, "outcome": "wrapper-only"}


def p1_path(spec, **extra):
    base = {"id": spec.path_id, "tool": spec.tool, "verdict": "refused", "refused_by": ["PreToolUse"]}
    if spec.path_id == paths.INNER_PATH_ID:
        base.update(code_mode=CODE_MODE, refused_scope="wrapper-only")
    return {**base, **extra}


ALL_PATHS = [p1_path(s) for s in paths.build_plan().specs]
SHELL_PATH = p1_path(paths.build_plan().spec("root:shell"))
EXCLUSION = {"reason": "needs the network", "schema": {"query": "string"}, "description": "searches",
             "absent_from_tool_list_when_denied": True}
P1_OK = {"paths": ALL_PATHS, "evidence": {"tool_list": ["shell", "websearch"],
                                          "exclusions": {"websearch": EXCLUSION}}}


def p1_with(listed=None, **evidence):
    return {"paths": ALL_PATHS if listed is None else listed,
            "evidence": {"tool_list": ["shell"], "exclusions": {}, **evidence}}


def without(path_id):
    return [p for p in ALL_PATHS if p["id"] != path_id]


def test_p1_record_needs_paths_with_refused_by():
    assert c_p1.check_record(P1_OK, {}) == []
    assert c_p1.check_record({"paths": []}, {}) == ["no `paths` list"]
    broken = c_p1.check_record({"paths": [{"id": "x"}]}, {})
    assert broken[0] == "path 'x' has no id, tool, verdict or refused_by list"
    assert broken[-1] == "no recorded tool list (the tools the stub received)"
    contradictory = {"paths": [{**SHELL_PATH, "refused_by": []}, *without("root:shell")], "evidence": P1_OK["evidence"]}
    assert c_p1.check_record(contradictory, {}) == [
        "path 'root:shell' is refused_by [] but its verdict is 'refused'"]


@pytest.mark.parametrize("vanished", ["mcp:probesrv:touch", "plugin-tool:probe_tool", "root:execute",
                                      "root:execute-inner", "root:shell"])
def test_p1_record_cannot_pass_with_a_vanished_path(vanished):
    problems = c_p1.check_record(p1_with(listed=without(vanished)), {})
    assert f"path {vanished!r} is missing from `paths`" in problems


def test_p1_leaves_out_only_an_optional_tool_recorded_as_unavailable():
    image = "root:experimental_image_generate"
    assert c_p1.check_record(p1_with(listed=without(image)), {}) == [f"path {image!r} is missing from `paths`"]
    unavailable = {paths.OPTIONAL_TOOLS[0]: f"{paths.UNAVAILABLE_TEXT} {paths.OPTIONAL_TOOLS[0]}"}
    assert c_p1.check_record(p1_with(listed=without(image), unavailable_tools=unavailable), {}) == []
    shell_gone = c_p1.check_record(p1_with(listed=without("root:shell"), unavailable_tools={"shell": "x"}), {})
    assert "tool 'shell' is recorded unavailable but is not an optional tool" in shell_gone
    assert "path 'root:shell' is missing from `paths`" in shell_gone


def test_only_an_optional_tool_may_be_unavailable():
    spec = paths.build_plan().spec("root:shell")
    image = paths.build_plan().spec("root:experimental_image_generate")
    unavailable = {"status": "error", "error": f"{paths.UNAVAILABLE_TEXT} \"x\" is currently available"}
    assert paths.may_be_unavailable(image, unavailable) and not paths.may_be_unavailable(spec, unavailable)
    assert not paths.may_be_unavailable(image, {"status": "error", "error": "boom"})
    assert not paths.may_be_unavailable(image, {"status": "completed", "error": None})


def test_refusal_text_shown_needs_the_error_state_and_a_runtime_refusal_text():
    assert paths.refusal_text_shown({"status": "error", "error": f"{paths.UNAVAILABLE_TEXT} \"shell\" is not available"})
    assert paths.refusal_text_shown({"status": "error", "error": "Permission denied: skill"})
    assert not paths.refusal_text_shown({"status": "error", "error": "ENOENT: no such file"})
    assert not paths.refusal_text_shown({"status": "completed", "error": "Permission denied"})
    assert not paths.refusal_text_shown({"status": None, "error": None})


def test_the_dispatched_effect_counts_an_answered_call_but_no_refusal():
    spec = paths.build_plan().spec("root:question")
    plan = paths.build_plan(include=("root:question",))
    call = plan.call_ids["root:question"]

    def part(status, error=None):
        return SimpleNamespace(tool_part=lambda call_id: {"state": {"status": status, "error": error}})

    assert paths.executed(part("completed"), plan, spec)
    assert paths.executed(part("error", "This non-interactive run cannot ask the user questions"), plan, spec)
    assert not paths.executed(part("error", f"{paths.UNAVAILABLE_TEXT} \"question\""), plan, spec)
    assert not paths.executed(part("error", harness.REFUSAL_TEXT["PreToolUse"] + ": question"), plan, spec)
    assert not paths.executed(part("error", harness.REFUSAL_TEXT["permission.evaluate"] + ": question"), plan, spec)
    assert not paths.executed(part("running"), plan, spec)
    assert not paths.executed(SimpleNamespace(tool_part=lambda call_id: None), plan, spec)
    assert call in plan.call_ids.values()


def test_a_refusal_event_prefers_the_tool_part_and_reads_the_log_only_without_one():
    errored = {"type": "tool_use", "part": {"id": "c1", "state": {"status": "error", "error": "probe throw: shell"}}}
    refusal = {"at": "refusal", "mechanism": "PreToolUse", "id": "c1", "tool": "shell"}
    run = harness.Run("r", Path("."), [], {}, 0, False, "", "", events=[errored], probe=[refusal])
    assert run.refusal_event("PreToolUse", "c1") == "tool-part-error"
    clean = {"type": "tool_use", "part": {"id": "c1", "state": {"status": "completed", "output": "ok"}}}
    logged_only = harness.Run("r", Path("."), [], {}, 0, False, "", "", events=[clean], probe=[refusal])
    assert logged_only.refusal_event("PreToolUse", "c1") is None
    no_part = harness.Run("r", Path("."), [], {}, 0, False, "", "", probe=[refusal])
    assert no_part.refusal_event("PreToolUse", "c1") == "hook-log"
    assert no_part.refusal_event("PreToolUse", "c2") is None


def run_with_requests(exit_code, requests):
    return SimpleNamespace(exit_code=exit_code, requests=requests,
                           tool_list=lambda: sorted({t for r in requests for t in r.get("tools") or []}))


def test_switch_off_run_counts_only_when_exited_clean_with_tools_and_the_path_tools_listed():
    ok = run_with_requests(0, [{"tools": ["shell", "write"]}])
    assert c_p1.switch_off_problems(ok, ["shell", "write"]) == []
    assert c_p1.switch_off_problems(run_with_requests(1, ok.requests), ["shell"]) == ["exit code 1"]
    assert c_p1.switch_off_problems(run_with_requests(0, [{"tools": []}]), []) == ["no chat request carried tools"]
    assert c_p1.switch_off_problems(ok, ["shell", "grep"]) == ["path tool 'grep' not in its tool list"]


def inner_path(**code_mode):
    return {**p1_path(paths.build_plan().spec(paths.INNER_PATH_ID)), "code_mode": {**CODE_MODE, **code_mode}}


def test_p1_inner_code_mode_call_is_recorded_with_its_facts():
    assert c_p1.check_record(p1_with(), {}) == []
    no_facts = [{k: v for k, v in inner_path().items() if k != "code_mode"}, *without(paths.INNER_PATH_ID)]
    assert c_p1.check_record(p1_with(listed=no_facts), {}) == [
        f"path {paths.INNER_PATH_ID!r} has no `code_mode` facts"]
    missing = {k: v for k, v in CODE_MODE.items() if k != "execute_switchable_by_config"}
    lacking = [{**inner_path(), "code_mode": missing}, *without(paths.INNER_PATH_ID)]
    assert c_p1.check_record(p1_with(listed=lacking), {}) == ["code_mode lacks 'execute_switchable_by_config'"]


def test_p1_inner_code_mode_outcome_follows_from_its_facts():
    def problems(**code_mode):
        listed = [inner_path(**code_mode), *without(paths.INNER_PATH_ID)]
        return c_p1.check_record(p1_with(listed=listed), {})

    assert problems(outcome="unstoppable")[0].startswith("code_mode outcome 'unstoppable' is neither")
    assert problems(outcome="inner-call") == ["code_mode outcome is inner-call but no rule stopped the inner effect"]
    seen = {"before_fires_for_inner_call_under_own_name": True, "before_rule_stops_inner_effect": True}
    assert problems(outcome="inner-call", **seen) == []
    assert problems(outcome="wrapper-only", **seen) == ["code_mode outcome is wrapper-only but a rule stopped the inner effect"]
    unmarked = [{k: v for k, v in inner_path().items() if k != "refused_scope"}, *without(paths.INNER_PATH_ID)]
    assert c_p1.check_record(p1_with(listed=unmarked), {}) == [
        "code_mode outcome is wrapper-only but the path is not marked refused_scope wrapper-only"]
    assert problems(before_rule_stops_inner_effect=False) == [
        "code_mode says a before rule stops the inner call that no before hook saw"]
    assert problems(evaluate_deny_stops_inner_effect=False) == [
        "code_mode says an evaluate deny stops the inner call that no evaluate hook saw"]


def test_p1_record_fails_for_a_listed_tool_that_is_neither_path_nor_exclusion():
    assert c_p1.check_record(p1_with(tool_list=["shell", "todo"]), {}) == [
        "tool 'todo' is in the model's tool list and in neither `paths` nor `exclusions`"]
    bare = {**EXCLUSION, "schema": {}}
    assert c_p1.check_record(p1_with(tool_list=["shell", "todo"], exclusions={"todo": bare}), {}) == [
        "exclusion 'todo' lacks a reason, schema, description or switch-off result"]
    assert c_p1.check_record(p1_with(tool_list=["shell", "todo"], exclusions={"todo": EXCLUSION}), {}) == []


def test_p1_credits_a_refusal_only_on_a_positive_event():
    spec = SimpleNamespace(path_id="shell:bash", tool="shell")
    absent = {"executed": False, "refusal_event": None}
    ran = {"executed": True, "refusal_event": None}
    marker_missing = c_p1._tool_entry(spec, {"executed": True}, absent, absent, True)
    assert marker_missing["refused_by"] == [] and marker_missing["verdict"] == "unconfirmed"
    for event in ("hook-log", "tool-part-error"):
        seen = {"executed": False, "refusal_event": event}
        credited = c_p1._tool_entry(spec, {"executed": True}, seen, absent, True)
        assert credited["refused_by"] == [c_p1.PRE] and credited["verdict"] == "refused"
        evaluated = c_p1._tool_entry(spec, {"executed": True}, absent, seen, True)
        assert evaluated["refused_by"] == [c_p1.EVAL]
        assert c_p1._tool_entry(spec, {"executed": True}, absent, seen, False)["refused_by"] == []
    assert c_p1._tool_entry(spec, {"executed": True}, ran, ran, True)["verdict"] == "unrefused"


def test_p13_refusal_without_an_event_is_not_a_pass():
    def mechanisms(thrown_event, denied_event):
        return [c_p13.mechanism_entry("PreToolUse", True, True, thrown_event),
                c_p13.mechanism_entry("permission.evaluate", True, True, denied_event),
                c_p13.mechanism_entry("Stop", True, False)]

    assert c_p13.verdict(mechanisms("probe throw: shell", "probe deny: shell"), False) == record.PASS
    silent = mechanisms(None, "probe deny: shell")
    assert silent[0]["can_refuse"] is False
    assert c_p13.verdict(silent, False) == record.UNOBSERVED
    assert c_p13.verdict(mechanisms("probe throw: shell", "probe deny: shell"), True) == record.FAIL


def test_p13_record_needs_mechanisms():
    refusal = {"mechanism": "PreToolUse", "fired": True, "can_refuse": True, "refusal_event": "probe throw: shell"}
    assert c_p13.check_record({"mechanisms": [{"mechanism": "Stop", "fired": True, "can_refuse": False}]}, {}) == []
    assert c_p13.check_record({"mechanisms": [refusal]}, {}) == []
    assert c_p13.check_record({}, {}) == ["no `mechanisms` list"]
    assert c_p13.check_record({"mechanisms": [{"mechanism": "Stop", "fired": True}]}, {}) == [
        "mechanism 'Stop' lacks fired or can_refuse"]
    assert c_p13.check_record({"mechanisms": [{**refusal, "refusal_event": None}]}, {}) == [
        "mechanism 'PreToolUse' can_refuse without a refusal_event"]


P6_STORE = {"columns": {"id": "text"}, "tokens": {"stored": {}}, "sample_row": {"id": "ses_1"}}
P6_SWITCH = {"assistant_messages": [{"model": "m1"}, {"model": "m2"}]}
P6_BASE = {"event_schema": ["type"], "exit_codes": {"success": 0}}


def p6_entry(status, **evidence):
    return {"status": status, "evidence": {**P6_BASE, **evidence}}


def test_p6_pass_needs_the_store_a_sample_row_and_two_models():
    ok = p6_entry(record.PASS, store=P6_STORE, missing=[], model_switch=P6_SWITCH)
    assert c_p6.check_record(ok, {}) == []
    assert c_p6.check_record(p6_entry(record.PASS, store=P6_STORE, missing=["store"], model_switch=P6_SWITCH), {}) == [
        "status pass but missing is ['store']"]
    assert c_p6.check_record(p6_entry(record.PASS, store={**P6_STORE, "sample_row": None}, missing=[],
                                      model_switch=P6_SWITCH), {}) == ["status pass but no sample row is recorded"]
    assert c_p6.check_record(p6_entry(record.PASS, store={}, missing=[], model_switch=P6_SWITCH), {}) == [
        "status pass but the usage-store columns or tokens are not recorded", "status pass but no sample row is recorded"]
    one_model = {"assistant_messages": [{"model": "m1"}, {"model": "m1"}]}
    assert c_p6.check_record(p6_entry(record.PASS, store=P6_STORE, missing=[], model_switch=one_model), {}) == [
        "status pass but the switch shows model ids ['m1']"]
    assert c_p6.check_record(p6_entry(record.PASS, store=P6_STORE, model_switch=P6_SWITCH), {}) == [
        "evidence has no `missing` list"]


def test_p6_absent_needs_a_named_missing_item_with_its_observation():
    item = c_p6.MISSING_ITEMS[0]
    ok = p6_entry(record.ABSENT, missing=[item], missing_observations={item: "no table"})
    assert c_p6.check_record(ok, {}) == []
    assert c_p6.check_record(p6_entry(record.ABSENT, missing=[], missing_observations={}), {}) == [
        "status absent but missing is empty"]
    assert c_p6.check_record(p6_entry(record.ABSENT, missing=["bogus"], missing_observations={"bogus": "x"}), {}) == [
        f"missing item 'bogus' is not one of {list(c_p6.MISSING_ITEMS)}"]
    assert c_p6.check_record(p6_entry(record.ABSENT, missing=[item], missing_observations={}), {}) == [
        f"missing item {item!r} has no observation"]


def test_p6_record_needs_schema_and_exit_codes():
    assert c_p6.check_record({"status": record.FAIL, "evidence": {}}, {}) == [
        "evidence lacks event_schema", "evidence lacks exit_codes"]


def p7_path(path_id, **extra):
    return {"id": path_id, "config_channel": c_p7.CHANNEL, "env_keys": [c_p7.CHANNEL], **extra}


P7_PATHS = [p7_path(p["id"]) for p in ALL_PATHS if p["id"] != "child-run"]


def test_p7_record_needs_evidence_per_p1_path_id():
    cases = {"P1": P1_OK}
    assert c_p7.check_record({"evidence": {"paths": P7_PATHS}}, cases) == []
    assert c_p7.check_record({"evidence": {"paths": P7_PATHS[1:]}}, cases) == [
        f"no evidence for P1 path {P7_PATHS[0]['id']!r}"]
    assert c_p7.check_record({"evidence": {"paths": P7_PATHS}}, {}) == ["P1 has no paths to compare against"]


def test_p7_record_needs_the_config_channel_and_env_key_names_per_path():
    cases = {"P1": P1_OK}
    first = P7_PATHS[0]["id"]
    for broken in ({"config_channel": None}, {"env_keys": []}, {"env_keys": ["HOME"]}):
        listed = [p7_path(first, **broken), *P7_PATHS[1:]]
        assert c_p7.check_record({"evidence": {"paths": listed}}, cases) == [
            f"P7 path {first!r} names no config channel or env key"]


def test_p7_leaves_out_only_an_optional_tool():
    cases = {"P1": P1_OK}
    ok = {"evidence": {"paths": P7_PATHS, "unavailable_tools": {paths.OPTIONAL_TOOLS[0]: "x"}}}
    assert c_p7.check_record(ok, cases) == []
    bad = {"evidence": {"paths": P7_PATHS, "unavailable_tools": {"write": "x"}}}
    assert c_p7.check_record(bad, cases) == ["tool 'write' is recorded unavailable but is not an optional tool"]


def test_run_phase_removes_the_roots_only_of_a_passing_case(monkeypatch, tmp_path):
    state = tmp_path / "state"
    statuses = {"A": record.PASS, "B": record.FAIL}
    roots = {}

    def load(info):
        def run(ctx):
            roots[info.case_id] = isolated.make_root(ctx.state)
            return runner.result(statuses[info.case_id], {})
        return SimpleNamespace(run=run)

    infos = [core.CaseInfo(cid, tmp_path / f"c_{cid}.py", False, False, "p") for cid in statuses]
    monkeypatch.setattr(core, "phase_cases", lambda phase: infos)
    monkeypatch.setattr(core, "load_case", load)
    runner.run_phase(core.get_phase("mechanisms"), state=state)
    assert not roots["A"].exists() and roots["B"].is_dir()


class FakeRun:
    """The part of a ``harness.Run`` that ``paths.outcome`` reads."""

    def __init__(self, files=(), refusals=()):
        self.files, self.refusals = set(files), set(refusals)
        self.requests = []

    def tool_part(self, call_id):
        return {"state": {"status": "error", "error": "probe throw: shell"}} if call_id in self.refusals else None

    def exists(self, rel):
        return rel in self.files

    def refusal_event(self, mechanism, call_id=None):
        return f"{mechanism} refused {call_id}" if call_id in self.refusals else None


def test_nested_child_run_refusal_is_read_at_the_nested_call_id():
    plan = SimpleNamespace(call_ids={"child-run": "call_m1_3_0", "shell:bash": "call_m1_0_0"})
    nested = SimpleNamespace(path_id="child-run", effect="file:m-child-run", refusal_call_id=paths.NESTED_CALL_ID)
    own = SimpleNamespace(path_id="shell:bash", effect="file:m-shell", refusal_call_id=None)
    run = FakeRun(refusals={paths.NESTED_CALL_ID, "call_m1_0_0"})
    assert paths.outcome(run, plan, nested, c_p1.PRE)["refusal_event"] == f"{c_p1.PRE} refused {paths.NESTED_CALL_ID}"
    assert paths.outcome(run, plan, own, c_p1.PRE)["refusal_event"] == f"{c_p1.PRE} refused call_m1_0_0"
    assert paths.outcome(FakeRun(), plan, own, c_p1.PRE)["refusal_event"] is None


def test_compare_refuses_a_probe_path_in_the_user_data_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(tmp_path / "probe-state"))
    home = tmp_path / "user-home"
    (home / ".config" / "opencode").mkdir(parents=True)
    for rel in userconfig.DATA_DIRS:
        (home / rel).mkdir(parents=True)
    (home / userconfig.DATA_DIRS[0] / "session.db").write_bytes(b"rows without any probe path")
    userconfig.write_snapshot(home)
    assert userconfig.compare(home) == 0
    leak = str(core.state_dir() / "runs" / "root.abc" / "work").encode()
    (home / userconfig.DATA_DIRS[2] / "index.json").write_bytes(b"x" * 3 + leak + b"y")
    assert userconfig.leaked_probe_paths(home) == [f"{userconfig.DATA_DIRS[2]}/index.json"]
    assert userconfig.compare(home) == 1


def test_leak_scan_finds_a_path_split_across_read_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(userconfig, "CHUNK", 8)
    (tmp_path / userconfig.DATA_DIRS[1]).mkdir(parents=True)
    (tmp_path / userconfig.DATA_DIRS[1] / "f").write_bytes(b"ab" + b"NEEDLE-ROOT" + b"cd")
    assert userconfig.leaked_probe_paths(tmp_path, b"NEEDLE-ROOT") == [f"{userconfig.DATA_DIRS[1]}/f"]


def test_leak_scan_searches_the_resolved_and_the_unresolved_state_dir(monkeypatch, tmp_path):
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "link-state"
    link.symlink_to(real)
    monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(link))
    needles = userconfig.probe_root_needles()
    assert needles == tuple(sorted({str(link / "runs" / "root.").encode(), str(real / "runs" / "root.").encode()}))
    for index, spelled in enumerate((link, real)):
        home = tmp_path / f"home{index}"
        (home / userconfig.DATA_DIRS[1]).mkdir(parents=True)
        (home / userconfig.DATA_DIRS[1] / "f").write_bytes(str(spelled / "runs" / "root.abc").encode())
        assert userconfig.leaked_probe_paths(home) == [f"{userconfig.DATA_DIRS[1]}/f"]


def test_a_pinned_record_is_checked_against_the_pin_entry_and_its_own_cases(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    pinned = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == []
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == []

    def repin(rec):
        pinned.write_text(json.dumps(rec), encoding="utf-8")
        entry = core.read_pin(probe)["last_pass"]["alpha"]
        record.write_pin(probe, "2.3.2", {"alpha": {**entry, "record_sha256": record.file_sha256(pinned)}})

    rec = record.read_record(pinned)
    repin({**rec, "version": "9.9.9"})
    problems = record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)
    assert problems == [f"alpha: the pinned record {pinned.parent.name} is of another version, phase or phase hash "
                        f"than the pin entry"]
    repin({**rec, "cases": {**rec["cases"], "A1": {"status": "fail", "evidence": {}}}})
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == [
        "pinned record: alpha/A1: fail is not accepted"]


def env_meta(root, **env_paths):
    return json.dumps({"src": "meta", "run": "r1", "root": str(root), "env_paths": env_paths})


def test_env_paths_must_lie_under_a_probe_root_of_the_runs_dir(tmp_path):
    runs = tmp_path / "runs"
    root = runs / "root.abc"
    log = tmp_path / "P1.jsonl"
    log.write_text(env_meta(root, HOME=str(root / "home"), XDG_DATA_HOME=str(root / "xdg" / "data")) + "\n",
                   encoding="utf-8")
    assert record.env_path_problems(log, runs) == []
    log.write_text(env_meta(root, HOME=str(tmp_path / "real-home")) + "\n", encoding="utf-8")
    assert record.env_path_problems(log, runs) == [
        f"P1.jsonl run r1: HOME={tmp_path / 'real-home'} lies outside its root {root}"]
    log.write_text(env_meta(tmp_path / "elsewhere", HOME=str(tmp_path / "elsewhere" / "home")) + "\n", encoding="utf-8")
    assert "is not a probe root" in record.env_path_problems(log, runs)[0]
    log.write_text(json.dumps({"src": "meta", "run": "r1", "root": str(root)}) + "\n", encoding="utf-8")
    assert record.env_path_problems(log, runs) == ["P1.jsonl run r1: no env_paths recorded"]


def test_check_record_reports_an_env_path_outside_the_run_root(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    path = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    (path.parent / "events" / "A1.jsonl").write_text(
        env_meta(state / "runs" / "root.x", HOME="/home/someone") + "\n", encoding="utf-8")
    assert record.pin_record(state, alpha, probe) == [
        f"alpha/A1: A1.jsonl run r1: HOME=/home/someone lies outside its root {state / 'runs' / 'root.x'}"]


def test_the_config_pins_the_stub_provider_and_small_model(tmp_path):
    config = fake_harness(tmp_path).base_config(plugin=True)
    assert config["enabled_providers"] == ["stub"]
    assert config["small_model"] == f"stub/{harness.STUB_MODELS[0]}"
    written = json.loads(fake_harness(tmp_path).run(FAKE_SCENARIO, label="config").read("opencode.json"))
    assert written["enabled_providers"] == ["stub"] and written["small_model"].startswith("stub/")


def test_the_binary_under_test_is_copied_not_linked(tmp_path):
    root = isolated.make_root(tmp_path)
    source = tmp_path / "installed-opencode"
    source.write_text("#!/bin/sh\necho 2.3.2\n", encoding="utf-8")
    source.chmod(0o755)
    placed = Path(isolated.place_binary(root, str(source)))
    assert placed.read_bytes() == source.read_bytes()
    assert not placed.is_symlink() and placed.stat().st_ino != source.stat().st_ino
    assert source.stat().st_nlink == 1
    placed.write_text("changed\n", encoding="utf-8")
    assert source.read_text(encoding="utf-8").endswith("2.3.2\n")


def test_the_pin_names_a_run_and_the_record_hash_not_a_path(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    path = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == []
    pin_text = (probe / core.PIN_NAME).read_text(encoding="utf-8")
    entry = core.read_pin(probe)["last_pass"]["alpha"]
    assert entry["run"] == path.parent.name and entry["record_sha256"] == record.file_sha256(path)
    assert str(tmp_path) not in pin_text and "/" not in pin_text
    assert record.pinned_record(state, entry)[0] == path


def test_a_record_whose_bytes_changed_is_no_longer_the_pinned_one(tmp_path):
    probe = make_probe(tmp_path)
    alpha = phase_of(probe, "alpha")
    state = tmp_path / "state"
    path = write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"})
    assert record.pin_record(state, alpha, probe) == []
    rec = record.read_record(path)
    rec["cases"]["A1"]["evidence"] = {"edited": True}
    path.write_text(json.dumps(rec), encoding="utf-8")
    entry = core.read_pin(probe)["last_pass"]["alpha"]
    assert record.pinned_record(state, entry) is None
    assert record.pinned_record(state, {**entry, "run": "../elsewhere"}) is None
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)[0].endswith(
        "whose sha256 is the pinned one")


def test_the_committed_pin_holds_no_machine_path():
    text = (core.PROBE_DIR / core.PIN_NAME).read_text(encoding="utf-8")
    assert not re.search(r'"/|/home/|/tmp/|/var/', text)


