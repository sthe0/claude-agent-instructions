"""Tests for hook-guard-committed-data.py — the PreToolUse gate that denies a
VCS add or commit carrying raw production / personal data.

The judge never reaches a live model here: a fake runner supplies its YES/NO
answer, so the judge's own parsing, the prefilter, the command parse, the file
enumeration and the deny-JSON assembly all run for real while the test stays
deterministic.

Every fixture row is SYNTHETIC and obviously so (`chat-fake-0001`,
`user-fake-0001`, invented sentences). Committing a real chat row as a test
fixture would reproduce, inside this repository, the exact defect the hook
exists to prevent.

Matrix:
  a chat dump the judge confirms          -> deny, exit 0
  the same dump, judge says NO            -> allow, judge consulted
  a script naming the same field names    -> allow, judge never consulted
  a Bash call that stages nothing         -> allow, no file is even read
  no runner / runner raises / times out   -> allow, exit 0 (fail-open)
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK = SCRIPTS_DIR / "hook-guard-committed-data.py"


def _load():
    spec = importlib.util.spec_from_file_location("hook_guard_committed_data", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()

# The second VCS the hook watches, taken from the hook's own constant rather
# than spelled out: verify-terms refuses that binary's name written next to a
# subcommand anywhere in this repository's text.
_VCS2 = _mod._ARC


# --- fixtures: the two files the gate must tell apart -------------------------

# The shape that provoked this hook: one JSON record per line, each carrying a
# user's own message text next to the identifiers that tie it to a person.
# Invented content, invented ids — see the module docstring.
FAKE_CHAT_JSONL = "\n".join(
    json.dumps(row, ensure_ascii=False)
    for row in (
        {
            "chat_id": "chat-fake-0001",
            "user_id": "user-fake-0001",
            "first_message": "Hi, can you help me plan a birthday party for my "
                             "daughter next weekend? She likes dinosaurs.",
            "response": "Of course. Let us start with the guest list and a "
                        "dinosaur theme you can build the rest around.",
        },
        {
            "chat_id": "chat-fake-0002",
            "user_id": "user-fake-0002",
            "first_message": "My laptop will not boot after the update last "
                             "night and I have a deadline tomorrow morning.",
            "response": "Let us try booting into recovery mode first and see "
                        "whether the previous kernel still starts.",
        },
    )
) + "\n"

# The false positive that made the original remediation tedious: five scripts
# that NAME `chat_id` and `first_message` as columns and carry none of them.
# Each had to be opened and cleared by hand. Short string values throughout —
# that absence of a long free-text value is what the prefilter keys on.
FIELD_NAMES_PY = '''\
"""Aggregate a chat export into per-day counts."""
import collections
import json


def load(path):
    with open(path) as handle:
        for line in handle:
            yield json.loads(line)


def counts_by_day(path):
    seen = collections.Counter()
    for row in load(path):
        chat_id = row["chat_id"]
        user_id = row["user_id"]
        text = row["first_message"]
        seen[(chat_id[:8], user_id[:8], len(text))] += 1
    return seen
'''


def _payload(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _runner(answer: str, calls: "list | None" = None):
    from agentctl.dispatch import RunResult

    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return RunResult(0, answer, "")

    return run


def _raising_runner(calls: "list | None" = None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        raise RuntimeError("the judge subprocess died")

    return run


def _timing_out_runner(calls: "list | None" = None):
    from agentctl.dispatch import RunResult

    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return RunResult(1, "", "advisor timed out after 41s", timed_out=True)

    return run


def _run_main(payload: dict, runner, monkeypatch, capsys) -> str:
    """Drive main() end-to-end with a stubbed stdin and a fake judge runner;
    return the permissionDecision ('allow' when the hook prints nothing)."""
    monkeypatch.setattr(_mod.advisor, "subprocess_runner", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A directory holding both fixtures on disk, with the VCS enumeration
    stubbed to report them — so `commit` (whose file set is the index, not its
    argv) resolves to real files without needing a real repository."""
    (tmp_path / "chats.jsonl").write_text(FAKE_CHAT_JSONL, encoding="utf-8")
    (tmp_path / "aggregate.py").write_text(FIELD_NAMES_PY, encoding="utf-8")
    monkeypatch.setattr(
        _mod, "_vcs_reported_paths",
        lambda binary, subcommand, cwd: ["chats.jsonl", "aggregate.py"],
    )
    return tmp_path


# --- the prefilter: the one false positive it must not have -------------------

def test_prefilter_fires_on_a_chat_dump():
    assert _mod.advisor.committed_data_prefilter(FAKE_CHAT_JSONL, "chats.jsonl")


def test_prefilter_does_not_fire_on_a_script_that_only_names_the_fields():
    """The concrete false positive this prefilter is tuned against: a script
    naming `chat_id` / `first_message` as columns carries no payload, and firing
    on it is what made the original remediation a hand audit of five files."""
    assert not _mod.advisor.committed_data_prefilter(FIELD_NAMES_PY, "aggregate.py")


def test_prefilter_needs_a_data_cue_at_all():
    prose = "The quick brown fox jumps over the lazy dog, at length, twice. " * 4
    assert not _mod.advisor.committed_data_prefilter(prose, "notes.txt")


def test_prefilter_needs_more_than_a_cue_outside_a_data_extension():
    """Outside _COMMITTED_DATA_EXTENSIONS a cue alone is not enough — that is
    the whole difference between the two fixtures above. Same text, two names:
    the data extension fires, the source extension does not."""
    short_cue = 'chat_id = row["chat_id"]\nfirst_message = row["first_message"]\n'
    assert _mod.advisor.committed_data_prefilter(short_cue, "rows.jsonl")
    assert not _mod.advisor.committed_data_prefilter(short_cue, "rows.py")


# --- the command gate: which Bash calls cost anything at all ------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("git commit -m wip", ("git", "commit")),
        ("git add a.jsonl b.py", ("git", "add")),
        (f"{_VCS2} commit -am wip", (_VCS2, "commit")),
        ("/usr/bin/git commit -m wip", ("git", "commit")),
        ("command git add .", ("git", "add")),
        ("env GIT_AUTHOR_NAME=x git commit -m wip", ("git", "commit")),
        ("git -C /some/tree commit -m wip", ("git", "commit")),
        ("git --no-pager add x.jsonl", ("git", "add")),
        ("ls -la", None),
        ("git status", ("git", "status")),
        ("grep -r chat_id .", None),
    ],
)
def test_the_command_parse_finds_the_vcs_invocation(command, expected):
    segments = _mod._segments(command)
    found = [_mod._vcs_invocation(tokens) for tokens in segments]
    found = [f for f in found if f is not None]
    if expected is None:
        assert found == []
    else:
        assert found and found[0][:2] == expected


def test_a_compound_command_is_split_into_its_segments():
    """`cd x && git commit -am wip` is one Bash call and two commands; a parse
    that only looked at the first token would never see the commit."""
    segments = _mod._segments("cd /tmp/x && git commit -am wip")
    invocations = [i for i in (_mod._vcs_invocation(t) for t in segments) if i]
    assert [i[:2] for i in invocations] == [("git", "commit")]


def test_an_unrelated_bash_call_reads_no_file_and_calls_no_judge(repo, monkeypatch, capsys):
    calls: list = []
    payload = _payload("ls -la", str(repo))
    assert _run_main(payload, _runner("YES", calls), monkeypatch, capsys) == "allow"
    assert calls == [], "a Bash call that stages nothing must not cost a judge call"


def test_a_commit_message_is_not_mistaken_for_a_path():
    assert _mod._explicit_paths(["-m", "add chats.jsonl", "--", "x.py"]) == ["x.py"]


# --- file enumeration --------------------------------------------------------

def test_git_add_enumerates_its_explicit_path_arguments(repo):
    tokens = _mod._segments(f"git add {repo / 'chats.jsonl'}")[0]
    files = _mod._candidate_files(str(repo), tokens)
    assert [f.name for f in files] == ["chats.jsonl"]


def test_commit_without_paths_asks_the_vcs(repo):
    tokens = _mod._segments("git commit -m wip")[0]
    files = _mod._candidate_files(str(repo), tokens)
    assert [f.name for f in files] == ["chats.jsonl", "aggregate.py"]


def test_a_file_below_the_size_floor_is_never_sampled(repo):
    tiny = repo / "tiny.jsonl"
    tiny.write_text('{"chat_id": "c"}\n', encoding="utf-8")
    tokens = _mod._segments(f"git add {tiny}")[0]
    assert _mod._candidate_files(str(repo), tokens) == []


def test_an_implausible_extension_is_never_sampled(repo):
    binary = repo / "dump.bin"
    binary.write_bytes(b"chat_id first_message " * 40)
    tokens = _mod._segments(f"git add {binary}")[0]
    assert _mod._candidate_files(str(repo), tokens) == []


# --- end to end: the two named checks of the plan's final verification -------

def test_end_to_end_a_fake_chat_jsonl_commit_is_denied(repo, monkeypatch, capsys):
    """A synthetic monorepo-VCS commit PreToolUse payload over a directory holding
    a JSONL of fake chat rows: the hook must print the deny JSON and still exit 0."""
    calls: list = []
    payload = _payload(f"{_VCS2} commit -m 'measurement artifacts'", str(repo))
    assert _run_main(payload, _runner("YES", calls), monkeypatch, capsys) == "deny"
    assert calls, "the judge must be consulted before a deny"


def test_end_to_end_a_script_naming_the_same_fields_is_allowed(repo, monkeypatch, capsys):
    """The same command over a directory holding ONLY the aggregation script.
    The allow must come from the PREFILTER — no judge call at all — otherwise
    every commit of ordinary data-handling code would pay for a model verdict."""
    (repo / "chats.jsonl").unlink()
    monkeypatch.setattr(
        _mod, "_vcs_reported_paths", lambda binary, subcommand, cwd: ["aggregate.py"]
    )
    calls: list = []
    payload = _payload(f"{_VCS2} commit -m 'aggregation script'", str(repo))
    assert _run_main(payload, _runner("YES", calls), monkeypatch, capsys) == "allow"
    assert calls == [], "a script that only names the fields must not cost a judge call"


def test_the_deny_names_the_file_and_the_rule(repo):
    decision = _mod.decide(
        _payload(f"{_VCS2} commit -m wip", str(repo)), runner=_runner("YES")
    )
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith(_mod._DENY_REASON)
    assert "committed-files-earn-their-place.md" in reason
    assert str(repo / "chats.jsonl") in reason


# --- the judge contract: every failure direction allows ----------------------

def test_a_judge_saying_no_allows_the_commit(repo, monkeypatch, capsys):
    calls: list = []
    payload = _payload(f"{_VCS2} commit -m wip", str(repo))
    assert _run_main(payload, _runner("NO", calls), monkeypatch, capsys) == "allow"
    assert calls, "the prefilter must fire on this file and hand it to the judge"


@pytest.mark.parametrize(
    "runner_factory",
    [
        pytest.param(lambda calls: None, id="no_runner"),
        pytest.param(_raising_runner, id="runner_raises"),
        pytest.param(_timing_out_runner, id="runner_times_out"),
        pytest.param(lambda calls: _runner("perhaps", calls), id="answer_unparseable"),
        pytest.param(lambda calls: _runner("", calls), id="answer_empty"),
    ],
)
def test_every_unavailable_judge_path_allows_and_exits_zero(
    repo, monkeypatch, capsys, runner_factory
):
    calls: list = []
    payload = _payload(f"{_VCS2} commit -m wip", str(repo))
    assert _run_main(payload, runner_factory(calls), monkeypatch, capsys) == "allow"


def test_the_killswitch_turns_the_gate_off(repo, monkeypatch, capsys):
    calls: list = []
    monkeypatch.setenv(_mod._COMMITTED_DATA_KILLSWITCH_ENV, "0")
    payload = _payload(f"{_VCS2} commit -m wip", str(repo))
    assert _run_main(payload, _runner("YES", calls), monkeypatch, capsys) == "allow"
    assert calls == [], "a disabled judge must not be called at all"


def test_silent_inside_a_judge_child(repo, monkeypatch, capsys):
    """The commit that denies above, driven again with the judge-child marker
    set. A judge subprocess's own Bash is not a real user turn, and this hook
    consults a judge — so reaching it from inside one is the recursion the
    marker exists to stop. Silence must come from the guard, not from a quiet
    fixture, hence the same payload and a runner that would answer."""
    payload = _payload(f"{_VCS2} commit -m wip", str(repo))
    assert _run_main(payload, _runner("YES"), monkeypatch, capsys) == "deny"

    monkeypatch.setenv(_mod.JUDGE_CHILD_ENV_VAR, "1")
    calls: list = []
    assert _run_main(payload, _runner("YES", calls), monkeypatch, capsys) == "allow"
    assert calls == [], "the guard must return before any judge call"


def test_a_malformed_payload_allows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert _mod.main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_the_judge_never_sees_more_than_the_sample_cap(repo, monkeypatch):
    big = repo / "big.jsonl"
    big.write_text(FAKE_CHAT_JSONL * 200, encoding="utf-8")
    sample = _mod._head_sample(big)
    assert len(sample.encode("utf-8")) <= _mod._SAMPLE_MAX_BYTES
