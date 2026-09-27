"""Fixture-driven tests for lib/published_body.py.

Drives every case in fixtures/published-text/commands.json plus the specific
assertions the plan's Stage 3 Procedure names: shared-reader byte-identity,
missing-target UNRESOLVED (never empty TEXT), seam-parity, seam-required
regression, artifact_syntax_hint non-gating, and record_advisory fail-open.
"""
from __future__ import annotations

import json
import shlex

import pytest

from lib import config_root, published_body


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))




@pytest.fixture
def published_text_dir(fixtures_dir):
    return fixtures_dir / "published-text"


@pytest.fixture
def repo_root(published_text_dir):
    """Fixture commands embed paths relative to the repo root (as an actual
    Bash tool call's cwd would be), not relative to the fixtures directory
    itself -- so resolution must run with cwd=repo_root."""
    return published_text_dir.parent.parent.parent.parent


@pytest.fixture
def seam(published_text_dir):
    return _load_json(published_text_dir / "seam.json")


@pytest.fixture
def commands(published_text_dir):
    return _load_json(published_text_dir / "commands.json")


def _resolve(case, cwd, seam):
    tool_input = {"command": case["command"]}
    used_seam = seam if case.get("seam") == "fixture" else None
    return published_body.resolve("Bash", tool_input, cwd=str(cwd), seam=used_seam)


def test_every_fixture_case_resolves_as_declared(commands, seam, repo_root):
    for case in commands:
        res = _resolve(case, repo_root, seam)
        assert res.kind == case["expect_kind"], case["label"]
        if "expect_shape" in case:
            assert res.shape == case["expect_shape"], case["label"]


def test_all_four_kinds_are_covered(commands):
    kinds = {c["expect_kind"] for c in commands}
    assert kinds == {"TEXT", "ATTACHMENT", "UNRESOLVED", "NOT_A_PUBLICATION"}


def test_all_seven_distinct_shapes_are_covered(commands):
    shapes = {c["expect_shape"] for c in commands if "expect_shape" in c}
    assert shapes == {1, 2, 3, 4, 5, 6, 7}


def test_seam_parity_same_shape_core_case(commands, seam, repo_root):
    """A seam-declared verb resolves identically (kind + shape) to a
    same-shape Core `gh` case -- the seam only supplies which VERBS count,
    never a different resolution mechanism."""
    seam_case = next(
        c for c in commands
        if c.get("seam") == "fixture" and c["expect_kind"] == "TEXT" and c.get("expect_shape") == 1
    )
    core_case = next(c for c in commands if c["label"] == "core-gh-pr-create-body-file (new Core shape)")
    res_seam = _resolve(seam_case, repo_root, seam)
    res_core = _resolve(core_case, repo_root, seam)
    assert res_seam.kind == res_core.kind == "TEXT"
    assert res_seam.shape == res_core.shape == 1


def test_seam_required_verbs_are_not_publications_without_seam(commands, repo_root):
    seam_cases = [c for c in commands if c.get("seam") == "fixture" and c["expect_kind"] != "NOT_A_PUBLICATION"]
    assert seam_cases
    for case in seam_cases:
        res = _resolve(case, repo_root, seam=None)
        assert res.kind == "NOT_A_PUBLICATION", case["label"]


def test_attachment_via_seam_is_present(commands):
    assert any(c["expect_kind"] == "ATTACHMENT" and c.get("seam") == "fixture" for c in commands)


def test_gh_pr_create_body_flag_case_is_present(commands):
    assert any(
        "gh pr create" in c["command"] and "--body" in c["command"] for c in commands
    )


def test_inline_path_read_both_forms_are_present(commands):
    cat_form = [c for c in commands if c["label"].startswith("shape6-inline-path-read-cat-form")]
    lt_form = [c for c in commands if c["label"].startswith("shape6-inline-path-read-lt-form")]
    assert cat_form and lt_form
    assert all(c["expect_shape"] == 6 for c in cat_form + lt_form)


def test_missing_target_case_is_present_with_valid_shape(commands):
    missing = [c for c in commands if c["expect_kind"] == "UNRESOLVED" and "expect_shape" in c]
    assert missing
    assert all(c["expect_shape"] in (1, 3, 4, 6, 7) for c in missing)


def test_non_publication_bash_command_resolves_not_a_publication():
    res = published_body.resolve("Bash", {"command": "git status --short"}, cwd=".")
    assert res.kind == published_body.NOT_A_PUBLICATION


def test_seam_verb_name_as_substring_in_unrelated_command_does_not_match(seam):
    """The code review's verified false positive: a seam verb NAME occurring
    as a substring of an unrelated command (here, inside a commit message
    operand) must not match -- `_match_seam_bash` matches at a command
    POSITION (a verb's own token sequence), not anywhere in the raw text."""
    command = 'git commit -m "fix tracker-cli.sh comment handling"'
    res = published_body.resolve("Bash", {"command": command}, cwd=".", seam=seam)
    assert res.kind == published_body.NOT_A_PUBLICATION


def test_attachment_verb_resolves_with_path(seam, repo_root):
    command = (
        "bash .claude/skills/tracker/scripts/tracker-cli.sh attachment-upload "
        "PROJ-467 scripts/tests/fixtures/published-text/artifact-dump.md"
    )
    res = published_body.resolve(
        "Bash", {"command": command}, cwd=str(repo_root), seam=seam
    )
    assert res.kind == published_body.ATTACHMENT
    assert res.path is not None
    assert res.path.endswith("artifact-dump.md")


def test_shape6_both_forms_are_byte_identical_to_shape1(repo_root, seam):
    cat_form = published_body.resolve(
        "Bash",
        {
            "command": (
                "gh issue comment 125 --repo sthe0/claude-agent-instructions --body "
                '"$(cat scripts/tests/fixtures/published-text/reader-facing.md)"'
            )
        },
        cwd=str(repo_root),
    )
    lt_form = published_body.resolve(
        "Bash",
        {
            "command": (
                'gh pr comment 42 --repo sthe0/claude-agent-instructions --body '
                '"$(< scripts/tests/fixtures/published-text/reader-facing.md)"'
            )
        },
        cwd=str(repo_root),
    )
    shape1 = published_body.resolve(
        "Bash",
        {
            "command": (
                "bash .claude/skills/tracker/scripts/tracker-cli.sh comment PROJ-467 "
                "--text @scripts/tests/fixtures/published-text/reader-facing.md"
            )
        },
        cwd=str(repo_root),
        seam=seam,
    )
    assert cat_form.kind == lt_form.kind == shape1.kind == published_body.TEXT
    assert cat_form.body == lt_form.body == shape1.body
    assert cat_form.body


def test_gh_verb_defeating_tokenization_resolves_unresolved_not_not_a_publication():
    """The blocking bug the code review found: an apostrophe inside an
    unquoted heredoc body makes `shlex.split` raise, so `_match_gh` never
    runs on a real `gh issue comment` publication. Without the raw-command
    verb fallback this fell through to NOT_A_PUBLICATION with no advisory --
    a real publication allowed silently. Confirms the tokenizer really does
    fail on this input (otherwise the test would not be exercising the
    fallback path at all) before asserting the fixed behaviour."""
    command = (
        "gh issue comment 125 --repo sthe0/claude-agent-instructions "
        "--body-file - <<'EOF'\n"
        "Hello reviewer, don't worry about the apostrophe.\n"
        "EOF\n"
    )
    with pytest.raises(ValueError):
        shlex.split(command)
    res = published_body.resolve("Bash", {"command": command}, cwd=".")
    assert res.kind == published_body.UNRESOLVED
    assert res.body is None
    assert published_body.is_publication("Bash", {"command": command}) is True


def test_missing_target_resolves_unresolved_never_empty_text(repo_root):
    res = published_body.resolve(
        "Bash",
        {
            "command": (
                "gh issue comment 999 --repo sthe0/claude-agent-instructions "
                "--body-file scripts/tests/fixtures/published-text/does-not-exist.md"
            )
        },
        cwd=str(repo_root),
    )
    assert res.kind == published_body.UNRESOLVED
    assert res.body is None


def test_artifact_syntax_hint_flags_dump_but_not_reader_facing(published_text_dir):
    dump = (published_text_dir / "artifact-dump.md").read_text(encoding="utf-8")
    prose = (published_text_dir / "reader-facing.md").read_text(encoding="utf-8")
    assert published_body.artifact_syntax_hint(dump) != ""
    assert published_body.artifact_syntax_hint(prose) == ""


def test_artifact_syntax_hint_never_gates_a_decision(published_text_dir, seam, repo_root):
    dump = (published_text_dir / "artifact-dump.md").read_text(encoding="utf-8")
    assert published_body.artifact_syntax_hint(dump) != ""
    # shape 1 (file-valued flag), reading the dump fixture itself -- proves a
    # body that trips the hint still resolves normally: the hint never feeds
    # into resolve()'s own kind/shape decision.
    command = (
        "gh issue comment 125 --repo sthe0/claude-agent-instructions "
        "--body-file scripts/tests/fixtures/published-text/artifact-dump.md"
    )
    res = published_body.resolve("Bash", {"command": command}, cwd=str(repo_root), seam=seam)
    assert res.kind == published_body.TEXT
    assert res.body == dump


def test_is_publication_writes_no_advisory(tmp_path, monkeypatch):
    """`is_publication` is a pure existence check -- calling it on a command
    that would resolve UNRESOLVED (a genuinely unmodellable substitution)
    must not itself write an advisory line; only `resolve()`'s own body
    resolution does that."""
    monkeypatch.delenv(published_body.ADVISORY_SINK_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))
    command = (
        "gh issue comment 125 --repo sthe0/claude-agent-instructions "
        '--body "$(some-generator arg)"'
    )
    assert published_body.is_publication("Bash", {"command": command}) is True
    sink = tmp_path / "state" / "published-text-gate" / published_body.ADVISORY_SINK_NAME
    assert not sink.exists()


def test_record_advisory_writes_a_parseable_line(tmp_path, monkeypatch):
    # Drop the suite-wide sink override (conftest's `_isolate_writer_gate_advisories`)
    # deliberately: the subject here is the PRODUCTION path resolution, which is
    # config-root-derived. Under the override this would assert against a path
    # production never uses, and `advisory_sink()`'s default branch would lose
    # its only coverage -- the same reason the two tests above drop it.
    monkeypatch.delenv(published_body.ADVISORY_SINK_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))
    published_body.record_advisory(published_body.UNRESOLVED, 4, "some $VAR reference")
    sink = tmp_path / "state" / "published-text-gate" / published_body.ADVISORY_SINK_NAME
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == published_body.UNRESOLVED
    assert record["shape"] == 4
    assert "command" not in record
    assert len(record["command_sha256"]) == 64
    assert "timestamp" in record


def test_record_advisory_swallows_write_failure(tmp_path, monkeypatch):
    # Must drop the override too, or this test proves nothing: with the sink
    # redirected to a writable tmp path the write SUCCEEDS and the swallow
    # branch is never entered, so the control silently stops controlling.
    monkeypatch.delenv(published_body.ADVISORY_SINK_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    unwritable = tmp_path / "not-a-real-dir-parent-is-a-file"
    unwritable.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(unwritable / "nested"))
    published_body.record_advisory(published_body.TEXT, 2, "irrelevant")


def test_is_publication_matches_resolve_kind(published_text_dir, seam):
    assert published_body.is_publication("Bash", {"command": "git status --short"}) is False
    command = (
        "bash .claude/skills/tracker/scripts/tracker-cli.sh comment PROJ-467 "
        '--text "hello"'
    )
    assert published_body.is_publication("Bash", {"command": command}, seam=seam) is True


# --- raw-HTTP route (seam kind `http_write_endpoint`, body shape 7) -----------
#
# The route exists because a publication reaching a ticket API through `curl` or
# a `python3` heredoc invokes an interpreter, not a publishing verb, so every
# verb matcher says NOT_A_PUBLICATION and the gate never runs. These cases pin
# both halves of the trigger (write method AND declared endpoint) and the two
# ways shape 7 must refuse to guess, so a later widening of either half shows up
# here as a failure rather than as a silently broader gate.

_RAW_HTTP_BASE = (
    "python3 - <<'PY'\n"
    "import urllib.request, pathlib\n"
    "body = pathlib.Path('{body}').read_text()\n"
    "req = urllib.request.Request("
    "'https://{host}/v2/issues/ABC-1', data=b'{{}}', method='{method}')\n"
    "PY"
)
_FIXTURE_BODY = "scripts/tests/fixtures/published-text/reader-facing.md"
_SEAM_HOST = "st-api.example-tracker.test"


def _raw_http(seam_obj, repo_root, *, body=_FIXTURE_BODY, host=_SEAM_HOST, method="PATCH"):
    command = _RAW_HTTP_BASE.format(body=body, host=host, method=method)
    return published_body.resolve("Bash", {"command": command}, cwd=str(repo_root), seam=seam_obj)


def test_raw_http_write_resolves_the_referenced_file_byte_for_byte(seam, repo_root):
    res = _raw_http(seam, repo_root)
    assert res.kind == "TEXT"
    assert res.shape == 7
    assert res.body == (repo_root / _FIXTURE_BODY).read_text(encoding="utf-8")


def test_raw_http_needs_a_write_method(seam, repo_root):
    """Control: the same endpoint read with GET is not a publication. Mutating
    `_HTTP_WRITE_METHOD_RE` to match any method turns this RED."""
    res = _raw_http(seam, repo_root, method="GET")
    assert res.kind == "NOT_A_PUBLICATION"


def test_raw_http_needs_a_seam_declared_endpoint(seam, repo_root):
    """Control: a write to an endpoint the seam does not declare is not a
    publication -- the endpoint list is DATA, so Core must not infer siblings.
    Mutating `_match_seam_http_endpoint` to match any host turns this RED."""
    res = _raw_http(seam, repo_root, host="metrics.example.test")
    assert res.kind == "NOT_A_PUBLICATION"


def test_raw_http_without_a_seam_is_not_a_publication(repo_root):
    """Control: a machine declaring no seam has no HTTP endpoints at all."""
    assert _raw_http(None, repo_root).kind == "NOT_A_PUBLICATION"


def test_raw_http_is_publication_agrees_with_resolve(seam, repo_root):
    command = _RAW_HTTP_BASE.format(body=_FIXTURE_BODY, host=_SEAM_HOST, method="PATCH")
    assert published_body.is_publication("Bash", {"command": command}, seam=seam) is True


def test_raw_http_two_text_files_stay_unresolved(seam, repo_root):
    """Control: shape 7 resolves ONE referenced text file or none -- two
    candidates mean the module cannot tell which is the body, which is
    UNRESOLVED (fail open + advisory), never a guess at the first one."""
    command = _RAW_HTTP_BASE.format(body=_FIXTURE_BODY, host=_SEAM_HOST, method="POST")
    command += f"\n# also mentions scripts/tests/fixtures/published-text/polished.md"
    res = published_body.resolve("Bash", {"command": command}, cwd=str(repo_root), seam=seam)
    assert res.kind == "UNRESOLVED"
    assert res.shape == 7


def test_shape_seven_does_not_leak_onto_verb_routes(seam, repo_root):
    """Control: shape 7 is route-scoped (`_Match.allow_referenced_file`). A
    seam VERB call with no body flag that merely mentions a text file must stay
    UNRESOLVED -- on a route that has body flags, a stray path is not the body.
    Defaulting `allow_referenced_file` to True turns this RED."""
    command = (
        "bash .claude/skills/tracker/scripts/tracker-cli.sh comment PROJ-467 "
        f"--unmodelled-flag {_FIXTURE_BODY}"
    )
    res = published_body.resolve("Bash", {"command": command}, cwd=str(repo_root), seam=seam)
    assert res.kind == "UNRESOLVED"
    assert res.shape is None


def test_url_ending_in_md_is_not_read_as_a_body_path(seam, repo_root):
    """A `.md` URL is not a path the shell could read, so it must not be
    counted as a shape-7 candidate -- otherwise it would both fail to read AND
    mask the real body file by making the candidate count 2."""
    command = _RAW_HTTP_BASE.format(body=_FIXTURE_BODY, host=_SEAM_HOST, method="PATCH")
    command += "\n# see https://example.test/docs/guide.md\n"
    res = published_body.resolve("Bash", {"command": command}, cwd=str(repo_root), seam=seam)
    assert res.kind == "TEXT"
    assert res.shape == 7


def test_advisory_sink_env_override_redirects_both_writer_and_reader(tmp_path, monkeypatch):
    """The override exists so the SUITE cannot pollute the live sink, and the
    property that makes it work is that one setenv moves the writer and the
    reader together -- `record_advisory` and self-diagnose's scanner both go
    through `advisory_sink()`. A reader left on the real path while the writer
    was redirected would be worse than no isolation: the scan would report a
    file nothing is writing and silently never see the suite's own records."""
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "home"))
    redirected = tmp_path / "elsewhere" / "advisories.jsonl"
    monkeypatch.setenv(published_body.ADVISORY_SINK_ENV, str(redirected))
    assert published_body.advisory_sink() == redirected
    published_body.record_advisory(published_body.UNRESOLVED, 7, "some command")
    assert redirected.exists()
    # …and nothing landed on the config-root path the default branch would pick.
    assert not (tmp_path / "home" / "state" / "published-text-gate").exists()


def test_shape_seven_reads_a_curl_at_prefixed_body_path(repo_root, seam):
    """`-d @file` / `--data-binary @file` is curl's own read-from-file form, so
    on the raw-HTTP route it is the MOST likely real spelling -- and it fell
    through until a live probe caught it: the `@` stayed on the captured path, so
    an existing file still failed to read and a genuine publication was allowed
    with only an advisory. Mutation that turns this RED: drop the `@`-strip in
    `_resolve_referenced_text_file`."""
    command = (
        "curl -sS -X POST https://st-api.example-tracker.test/v2/issues/ABC-1/comments "
        "-K /tmp/creds.conf "
        "--data-binary @scripts/tests/fixtures/published-text/reader-facing.md"
    )
    res = published_body.resolve(
        "Bash", {"command": command}, cwd=str(repo_root), seam=seam
    )
    assert res.kind == published_body.TEXT
    assert res.shape == 7
    expected = (
        repo_root / "scripts/tests/fixtures/published-text/reader-facing.md"
    ).read_text(encoding="utf-8")
    assert res.body == expected
