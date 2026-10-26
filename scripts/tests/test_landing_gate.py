"""Shared layer of the pre-land gates: git edges, keyed record store, record binding, pre-push loop.

Hermetic: every repository is a throwaway one under tmp_path; no network, no sandbox.
"""
import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from lib import landing_gate as shared

LIB_FILE = Path(shared.__file__)
ZEROS = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SCHEMA = "some-gate/v7"
HINT = "merge it and rerun the other gate"


def git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return path


def commit(repo, files, message="change"):
    for rel, content in files.items():
        if content is None:
            git(repo, "rm", "-q", "--", rel)
            continue
        target = Path(repo) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git(repo, "add", "--", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def good_record(**overrides):
    record = {"schema": SCHEMA, "candidate_sha": SHA_A, "sandbox_core_sha": SHA_A, "base_sha": SHA_B}
    record.update(overrides)
    return record


def bind(record, **overrides):
    args = {"schema": SCHEMA, "candidate_sha": SHA_A, "live_remote_sha": SHA_B, "rerun_hint": HINT}
    args.update(overrides)
    return shared.check_binding(record, **args)


class Wording:
    def refused(self, reason):
        return [f"refused: {reason}"]

    def decision(self, local_sha, decision):
        return [f"{decision.kind} {local_sha} ({decision.reason})"]

    def no_remote(self, trunk):
        return f"{trunk} is missing on the remote"

    def unknown_remote(self, trunk, remote_sha):
        return f"{trunk} tip {shared.short(remote_sha)} is unknown"


class Evaluator:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def __call__(self, local_sha, remote_sha):
        self.calls.append((local_sha, remote_sha))
        return self.decision


def push_line(local_sha=SHA_A, remote_sha=SHA_B, remote_ref="refs/heads/main"):
    return f"refs/heads/work {local_sha} {remote_ref} {remote_sha}\n"


def loop(lines, repo, evaluator, trunk="main"):
    return shared.prepush_loop(lines, repo, trunk, evaluate=evaluator, wording=Wording())


@pytest.fixture
def repo(tmp_path):
    path = make_repo(tmp_path / "repo")
    commit(path, {"a.txt": "one\ntwo\nthree\nfour\nfive\n"}, "first")
    return path


# ── seam tests stage 2 relies on ──────────────────────────────────────────

def test_prepush_loop_admits_any_allowed_kind(repo):
    remote_sha = git(repo, "rev-parse", "HEAD")
    evaluator = Evaluator(shared.Decision(True, "exempt", "suite gate is waived"))
    result = loop([push_line(SHA_A, remote_sha)], repo, evaluator)
    assert result.allowed is True
    assert result.messages == (f"exempt {SHA_A} (suite gate is waived)",)
    assert evaluator.calls == [(SHA_A, remote_sha)]


def test_store_writes_by_explicit_key_under_nested_dir(tmp_path):
    record = {"result": "PASS", "tip": SHA_C}
    path = shared.write_record(tmp_path, "x/baseline", SHA_C, record)
    assert path == tmp_path / "x" / "baseline" / f"{SHA_C}.json"
    assert shared.load_record(tmp_path, "x/baseline", SHA_C) == record
    assert shared.load_record(tmp_path, "x/baseline", SHA_A) is None


def test_binding_check_uses_caller_schema():
    record = good_record()
    assert bind(record) == (True, "")
    bound, reason = bind(record, schema="another/v1")
    assert not bound and reason == f"unsupported record schema {SCHEMA!r}"
    assert bind(good_record(schema="another/v1"), schema="another/v1") == (True, "")
    bound, reason = bind(good_record(base_sha=SHA_C))
    assert not bound and reason.endswith(f"; {HINT}")


# ── record store ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("dirname", ["instruction-smoke", "suite-gate/baseline", "a/b/c"])
def test_record_path_is_per_directory_name(tmp_path, dirname):
    assert shared.record_path(tmp_path, dirname, SHA_A) == tmp_path / dirname / f"{SHA_A}.json"


@pytest.mark.parametrize("key", ["", "abc", "../" + SHA_A, SHA_A.upper(), SHA_A + "0"])
def test_record_path_refuses_a_key_that_is_not_a_full_sha(tmp_path, key):
    with pytest.raises(shared.RecordError):
        shared.record_path(tmp_path, "d", key)


def test_write_record_replaces_an_existing_record(tmp_path):
    shared.write_record(tmp_path, "d", SHA_A, {"n": 1})
    shared.write_record(tmp_path, "d", SHA_A, {"n": 2})
    assert shared.load_record(tmp_path, "d", SHA_A) == {"n": 2}
    assert [p.name for p in (tmp_path / "d").iterdir()] == [f"{SHA_A}.json"]


def test_write_record_leaves_no_partial_file_when_serialisation_fails(tmp_path):
    with pytest.raises(TypeError):
        shared.write_record(tmp_path, "d", SHA_A, {"bad": object()})
    assert list((tmp_path / "d").iterdir()) == []
    shared.write_record(tmp_path, "d", SHA_B, {"n": 1})
    with pytest.raises(TypeError):
        shared.write_record(tmp_path, "d", SHA_B, {"bad": object()})
    assert shared.load_record(tmp_path, "d", SHA_B) == {"n": 1}


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "null", '"text"'])
def test_a_malformed_record_raises_record_error(tmp_path, text):
    path = shared.record_path(tmp_path, "d", SHA_A)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(shared.RecordError):
        shared.load_record(tmp_path, "d", SHA_A)


def test_an_unreadable_record_raises_record_error(tmp_path):
    with pytest.raises(shared.RecordError):
        shared.read_record(tmp_path / "missing.json")


def test_record_error_is_a_gate_error_and_not_a_refusal_by_default():
    error = shared.RecordError("x")
    assert isinstance(error, shared.GateError) and error.refused is False
    assert shared.GateError("x", refused=True).refused is True


# ── binding ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field, other, expected", [
    ("candidate_sha", SHA_C, f"record is for {SHA_C[:12]}, not {SHA_A[:12]}"),
    ("sandbox_core_sha", SHA_C, f"record's sandbox was built from {SHA_C[:12]}, not the candidate {SHA_A[:12]}"),
    ("base_sha", SHA_C, f"record was built on {SHA_C[:12]} but the remote tip is {SHA_B[:12]}; {HINT}"),
])
def test_each_binding_field_refuses_with_its_own_message(field, other, expected):
    assert bind(good_record(**{field: other})) == (False, expected)


def test_a_missing_binding_field_refuses():
    record = good_record()
    del record["base_sha"]
    bound, reason = bind(record)
    assert not bound and "record was built on" in reason


def test_binding_checks_schema_before_candidate():
    bound, reason = bind(good_record(schema="other/v1", candidate_sha=SHA_C))
    assert not bound and reason.startswith("unsupported record schema")


def test_an_unprintable_schema_is_shown_on_one_line():
    bound, reason = bind(good_record(schema="a\nb\x00c"))
    assert not bound and "\n" not in reason and "\x00" not in reason


# ── git edges ─────────────────────────────────────────────────────────────

def test_changed_paths_lists_both_sides_of_a_rename(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "a.txt", "b.txt")
    candidate = commit(repo, {}, "rename")
    assert sorted(shared.changed_paths(repo, base, candidate)) == ["a.txt", "b.txt"]


def test_changed_paths_lists_a_deleted_path(repo):
    base = git(repo, "rev-parse", "HEAD")
    candidate = commit(repo, {"a.txt": None}, "delete")
    assert shared.changed_paths(repo, base, candidate) == ["a.txt"]


def test_changed_paths_of_identical_commits_is_empty(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert shared.changed_paths(repo, head, head) == []


def test_path_in_tree_in_one_tree_and_not_the_other(repo):
    before = git(repo, "rev-parse", "HEAD")
    after = commit(repo, {"tools/run.sh": "#!/bin/sh\n"}, "add")
    assert shared.path_in_tree(repo, after, "tools/run.sh") is True
    assert shared.path_in_tree(repo, before, "tools/run.sh") is False


def test_is_ancestor_follows_history(repo):
    first = git(repo, "rev-parse", "HEAD")
    second = commit(repo, {"b.txt": "b\n"}, "second")
    assert shared.is_ancestor(repo, first, second) is True
    assert shared.is_ancestor(repo, second, first) is False


def test_is_ancestor_on_unrelated_histories_is_false(repo):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--orphan", "island")
    git(repo, "rm", "-rfq", ".")
    island = commit(repo, {"z.txt": "z\n"}, "island")
    assert shared.is_ancestor(repo, first, island) is False
    assert shared.is_ancestor(repo, island, first) is False


def test_is_ancestor_with_an_unknown_object_is_a_gate_error(repo):
    with pytest.raises(shared.GateError):
        shared.is_ancestor(repo, SHA_C, git(repo, "rev-parse", "HEAD"))


def test_resolve_commit_of_an_unknown_ref_raises(repo):
    with pytest.raises(shared.GateError):
        shared.resolve_commit(repo, "no-such-ref")
    assert shared.resolve_commit(repo, "HEAD") == git(repo, "rev-parse", "HEAD")


def test_object_exists_and_common_dir(repo):
    assert shared.object_exists(repo, git(repo, "rev-parse", "HEAD")) is True
    assert shared.object_exists(repo, SHA_C) is False
    assert shared.git_common_dir(repo) == (repo / ".git").resolve()


def test_git_ok_raises_a_gate_error_naming_the_subcommand(repo):
    with pytest.raises(shared.GateError, match="git rev-parse failed"):
        shared._git_ok(repo, "rev-parse", "--verify", "no-such-ref")


def test_the_clean_env_pops_the_git_dir_family(monkeypatch):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.setenv(name, "/somewhere")
    monkeypatch.setenv("KEEP_ME", "1")
    env = shared.clean_env()
    assert not any(name in env for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"))
    assert env["KEEP_ME"] == "1"


# ── pre-push loop ─────────────────────────────────────────────────────────

def test_prepush_loop_skips_blank_lines(repo):
    evaluator = Evaluator(shared.Decision(True, shared.NOT_REQUIRED, "n/a"))
    result = loop(["\n", "   \n"], repo, evaluator)
    assert result.allowed is True and result.messages == () and evaluator.calls == []


@pytest.mark.parametrize("raw", ["only three fields\n", "a b c d e\n", "x\n"])
def test_prepush_loop_refuses_a_malformed_line(repo, raw):
    evaluator = Evaluator(shared.Decision(True, shared.ADMITTED, "PASS"))
    result = loop([raw], repo, evaluator)
    assert result.allowed is False
    assert result.messages[0].startswith("refused: malformed pre-push input")
    assert evaluator.calls == []


def test_prepush_loop_ignores_a_ref_that_is_not_the_trunk(repo):
    evaluator = Evaluator(shared.Decision(False, shared.REFUSED, "no"))
    result = loop([push_line(remote_ref="refs/heads/topic"), push_line(remote_ref="refs/tags/main")], repo, evaluator)
    assert result.allowed is True and result.messages == () and evaluator.calls == []


def test_prepush_loop_follows_the_named_trunk(repo):
    evaluator = Evaluator(shared.Decision(True, shared.ADMITTED, "PASS"))
    head = git(repo, "rev-parse", "HEAD")
    assert loop([push_line(SHA_A, head)], repo, evaluator, trunk="release").messages == ()
    assert loop([push_line(SHA_A, head, "refs/heads/release")], repo, evaluator, trunk="release").allowed is True
    assert len(evaluator.calls) == 1


def test_prepush_loop_skips_a_deleted_ref(repo):
    evaluator = Evaluator(shared.Decision(False, shared.REFUSED, "no"))
    result = loop([push_line(local_sha=ZEROS)], repo, evaluator)
    assert result.allowed is True and result.messages == () and evaluator.calls == []


def test_prepush_loop_refuses_a_missing_remote_before_evaluating(repo):
    evaluator = Evaluator(shared.Decision(True, shared.ADMITTED, "PASS"))
    result = loop([push_line(remote_sha=ZEROS)], repo, evaluator)
    assert result.allowed is False
    assert result.messages == ("refused: main is missing on the remote",)
    assert evaluator.calls == []


def test_prepush_loop_refuses_an_unknown_remote_tip_before_evaluating(repo):
    evaluator = Evaluator(shared.Decision(True, shared.ADMITTED, "PASS"))
    result = loop([push_line(remote_sha=SHA_C)], repo, evaluator)
    assert result.allowed is False
    assert result.messages == (f"refused: main tip {SHA_C[:12]} is unknown",)
    assert evaluator.calls == []


def test_prepush_loop_propagates_a_callback_refusal(repo):
    head = git(repo, "rev-parse", "HEAD")
    evaluator = Evaluator(shared.Decision(False, shared.REFUSED, "no record"))
    result = loop([push_line(SHA_A, head)], repo, evaluator)
    assert result.allowed is False and result.messages == ("refused: no record",)


def test_prepush_loop_one_refusal_taints_the_whole_push(repo):
    head = git(repo, "rev-parse", "HEAD")
    decisions = iter([shared.Decision(True, shared.ADMITTED, "PASS"), shared.Decision(False, shared.REFUSED, "no")])
    result = loop([push_line(SHA_A, head), push_line(SHA_B, head)], repo, lambda local, remote: next(decisions))
    assert result.allowed is False
    assert result.messages == (f"admitted {SHA_A} (PASS)", "refused: no")


def test_prepush_loop_defuses_every_message(repo):
    head = git(repo, "rev-parse", "HEAD")
    pattern = re.compile(shared.PUSH_RIGHTS_PATTERN, re.IGNORECASE)
    noisy = shared.Decision(False, shared.REFUSED, "permission denied: read-only, 403 forbidden")
    refused = loop([push_line(SHA_A, head)], repo, Evaluator(noisy))
    allowed = loop([push_line(SHA_A, head)], repo, Evaluator(shared.Decision(True, "x", "access rights")))
    assert refused.messages and allowed.messages
    assert all(pattern.search(m) is None for m in refused.messages + allowed.messages)
    assert "denied" not in refused.messages[0] and "d·enied" in refused.messages[0]


# ── text helpers, waiver reason ───────────────────────────────────────────

def test_defuse_breaks_each_push_rights_phrase_and_keeps_other_text():
    pattern = re.compile(shared.PUSH_RIGHTS_PATTERN, re.IGNORECASE)
    text = "Permission DENIED, forbidden, 403, read-only, readonly, not authorized, access rights; fine"
    out = shared.defuse(text)
    assert pattern.search(out) is None and out.endswith("; fine")
    assert shared.defuse("nothing to see") == "nothing to see"


def test_one_line_collapses_whitespace_strips_unprintables_and_limits():
    assert shared.one_line("a \n\t b\x00c") == "a bc"
    assert shared.one_line("x" * 500) == "x" * 300
    assert shared.one_line("x" * 50, 10) == "x" * 10
    assert shared.one_line(None) == "None"


def test_is_valid_sha_accepts_sha1_and_sha256_only():
    assert shared.is_valid_sha(SHA_A) and shared.is_valid_sha("f" * 64)
    for bad in ("", "a" * 39, "a" * 41, "g" * 40, "A" * 40, None, 12345, b"a" * 40):
        assert shared.is_valid_sha(bad) is False


def test_short_is_twelve_characters():
    assert shared.short(SHA_A) == "a" * 12
    assert shared.short(None) == "None"


def test_normalize_reason_cleans_to_one_line_and_refuses_an_empty_one():
    assert shared.normalize_reason("  the live \n launch\twas down ") == "the live launch was down"
    for empty in ("", "   \n\t", "\x00"):
        with pytest.raises(shared.GateError) as raised:
            shared.normalize_reason(empty)
        assert raised.value.refused is True


def test_utc_now_is_an_iso_timestamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", shared.utc_now())


# ── the layer stays gate-agnostic ─────────────────────────────────────────

def test_landing_gate_imports_nothing_from_a_concrete_gate():
    tree = ast.parse(LIB_FILE.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [node.module or ""] + [alias.name for alias in node.names]
    assert imported and not [name for name in imported if "instruction_smoke_gate" in name]


def test_landing_gate_names_no_smoke_wording():
    source = LIB_FILE.read_text(encoding="utf-8")
    for fragment in ("instruction-smoke", "SANDBOX_SCRIPT", "REQUIRED_CHECKS", "instruction change", "rerun the smoke"):
        assert fragment not in source


def test_the_decision_dataclass_is_frozen_and_carries_any_kind():
    decision = shared.Decision(True, "exempt", "why")
    assert (decision.allowed, decision.kind, decision.reason) == (True, "exempt", "why")
    with pytest.raises(AttributeError):
        decision.allowed = False


def test_a_record_survives_a_json_round_trip_through_the_store(tmp_path):
    record = good_record(checks=[{"name": "n", "status": "PASS"}], note="é")
    shared.write_record(tmp_path, "suite-gate/baseline", SHA_C, record)
    text = (tmp_path / "suite-gate" / "baseline" / f"{SHA_C}.json").read_text(encoding="utf-8")
    assert text.endswith("\n") and json.loads(text) == record
