"""Tests of the OpenCode probe suite's own machinery (``scripts/opencode_probe/``).

Nothing here reaches a real model or the user's OpenCode: a synthetic probe tree stands in for the
phase packages, and the fake binary (``tests/fixtures/opencode-fake/opencode``) stands in for the
binary. The ``test_p6_live_*`` and ``test_paths_*`` nodes read the live record the suite produced and
skip with an ``opencode live:`` reason when there is none.
"""
import json
import os
import shutil
import stat
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import opencode_live
from opencode_probe import cli, core, coverage, harness, isolated, negative, record, runner, stub, subject_only, userconfig
from opencode_probe.mechanisms import c_p6

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


def write_run(state, phase, probe, statuses, version="2.3.2", kind="runs"):
    cases = {cid: {"status": status, "evidence": {}} for cid, status in statuses.items()}
    rec_dir = record.new_record_dir(state, kind)
    record.write_record(rec_dir, version, phase.name, core.phase_hash(phase, probe), cases)
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
    mentions = [p for p in core.PROBE_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                and "2.3.2" in p.read_text(encoding="utf-8", errors="replace")]
    assert [p.name for p in mentions] == [core.PIN_NAME]


def test_live_cases_skip_with_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPENCODE_BIN", str(tmp_path / "absent" / "opencode"))
    monkeypatch.setenv("AGENT_OPENCODE_PROBE_STATE", str(tmp_path / "state"))
    monkeypatch.delenv(opencode_live.CREDENTIAL_ENV, raising=False)
    assert opencode_live.binary_problem().startswith("opencode live: binary")
    assert opencode_live.credential_problem().startswith("opencode live: credential")
    assert opencode_live.record_problem("mechanisms").startswith("opencode live: binary")
    monkeypatch.setenv("AGENT_OPENCODE_BIN", fake_binary(tmp_path))
    assert opencode_live.record_problem("mechanisms").startswith("opencode live: record")


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
        'version = "2.4.0"\n\n[last_pass.alpha]\nversion = "2.3.2"\nsuite_sha256 = "x"\nrecord = "y"\n', encoding="utf-8")
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe)
    write_run(state, alpha, probe, {"A1": "pass", "A2": "pass"}, version="2.4.0")
    assert record.pin_record(state, alpha, probe) == []
    assert record.check_phase(state, alpha, require_pass=True, case_ids=None, probe_dir=probe) == []


def test_pin_is_exact_2x():
    version = core.read_pin()["version"]
    assert version == "2.3.2"


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
    monkeypatch.setattr(core, "binary_problem", lambda: None)
    monkeypatch.setattr(runner, "run_phase", lambda phase, **kw: (tmp_path, {"P1": {"status": "pass"}}))
    pin = core.PROBE_DIR / core.PIN_NAME
    before = pin.read_bytes()
    assert cli.main(["run", "--phase", "mechanisms"]) == 0
    assert pin.read_bytes() == before


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


def test_p6_live_record_names_missing_fields():
    entry = opencode_live.latest_case("mechanisms", "P6")
    if entry["status"] == record.PASS:
        assert entry["evidence"]["missing"] == []
    else:
        assert entry["evidence"]["missing"] and set(entry["evidence"]["missing"]) <= set(entry["evidence"]["missing_observations"])


def test_paths_record_refused_by_per_mechanism():
    entry = opencode_live.latest_case("mechanisms", "P1")
    assert entry["paths"] and all(isinstance(p["refused_by"], list) and p["id"] for p in entry["paths"])


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


def test_inert_deny_disables_both_mechanisms(tmp_path):
    source = (core.PROBE_DIR / "plugin" / "index.mjs").read_text(encoding="utf-8")
    assert source.count("!INERT") == 3
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
