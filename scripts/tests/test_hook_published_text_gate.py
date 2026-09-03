"""hook-published-text-writer-gate.py: deny a Bash publication call (gh /
tracker-cli.sh comment / PR create / a seam verb) whose TEXT body has no
tech-writer witness bound to it (lib.writer_pass.bind), and gate an
ATTACHMENT upload on a model judge ONLY once a content-sniffing prefilter
fails to parse it as a genuine artifact shape.

Reuses scripts/tests/fixtures/published-text/: commands.json's 11 recorded/
modelled shapes (from stage 3's published_body fixtures) drive the TEXT/
ATTACHMENT/UNRESOLVED/NOT_A_PUBLICATION dispatch; the witnessed/unwitnessed/
two-witness/containment-trap/discrimination/writer-output-equality
transcripts (stage 4's writer_pass fixtures) drive the binding-strength
dispatch. Every tracker-cli.sh / ya-tool-mcp-connect scenario is seam-
dependent (published_body.resolve recognizes those verbs ONLY via
lib.config_root.publication_tools_file's seam list, never built-in), so every
such test seeds CLAUDE_AGENT_HOME with a copy of seam.json BEFORE running the
hook -- proving the seam is genuinely consulted, not trivially inert."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentctl import advisor  # noqa: E402
from lib import published_body  # noqa: E402

HOOK = Path(__file__).resolve().parent.parent / "hook-published-text-writer-gate.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "published-text"
REPO_ROOT = Path(__file__).resolve().parents[2]

COMMANDS = json.loads((FIXTURES / "commands.json").read_text(encoding="utf-8"))
SEAM = (FIXTURES / "seam.json").read_text(encoding="utf-8")


def _by_label(fragment: str) -> dict:
    for entry in COMMANDS:
        if fragment in entry["label"]:
            return entry
    raise KeyError(fragment)


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_published_text_writer_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_seam(agent_home: Path) -> None:
    agent_home.mkdir(parents=True, exist_ok=True)
    (agent_home / "publication-tools.local").write_text(SEAM, encoding="utf-8")


def bash_payload(command: str, transcript_path: "Path | None" = None) -> dict:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO_ROOT)}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    return payload


def run_hook(payload: dict, agent_home: Path, env_extra: "dict | None" = None) -> subprocess.CompletedProcess:
    # CLAUDE_AGENT_HOME, not CLAUDE_CONFIG_DIR: the Done criterion requires the
    # seam-dependent scenarios to run under CLAUDE_AGENT_HOME specifically, so
    # the seam load path is genuinely exercised rather than trivially inert.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(agent_home), "CLAUDE_AGENT_HOME": str(agent_home)}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _is_deny(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    out = json.loads(proc.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _deny_reason(proc: subprocess.CompletedProcess) -> str:
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def _advisory_rows(agent_home: Path) -> list[dict]:
    sink = agent_home / "state" / "published-text-gate" / published_body.ADVISORY_SINK_NAME
    if not sink.exists():
        return []
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line.strip()]


def transcript(name: str) -> Path:
    return FIXTURES / f"transcript-{name}.jsonl"


# --- TEXT shapes, witnessed vs unwitnessed (subprocess end-to-end) ----------

def test_shape1_file_flag_witnessed_allows(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape1-file-valued-flag")
    proc = run_hook(bash_payload(entry["command"], transcript("witnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_shape1_file_flag_unwitnessed_denies_with_generic_reason(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape1-file-valued-flag")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "spawn" in reason and "tech-writer" in reason
    assert "inline literal" not in reason  # the shape-2-specific wording must not leak here


def test_shape2_inline_literal_unwitnessed_denies_naming_both_remedies(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape2-inline-literal")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "spawn" in reason and "--body-file" in reason
    assert "does not by itself clear this gate" in reason.lower()


def test_shape2_inline_literal_witnessed_still_denies_since_body_never_matches(tmp_path):
    # The exact point of the shape-2 message: an inline Skill pass does not
    # bind the literal argument bytes to anything -- the fixture's witnessed
    # transcript composes reader-facing.md, not this inline string, so even a
    # "witnessed" session still denies.
    write_seam(tmp_path)
    entry = _by_label("shape2-inline-literal")
    proc = run_hook(bash_payload(entry["command"], transcript("witnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_shape1_unwitnessed_deny_reason_names_the_override_env(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape1-file-valued-flag")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "CLAUDE_PUBLISHED_TEXT_GATE=0" in reason


def test_shape2_inline_literal_deny_reason_also_names_the_override_env(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape2-inline-literal")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "CLAUDE_PUBLISHED_TEXT_GATE=0" in reason


def test_text_gate_override_force_allows_a_genuine_deny_and_records_advisory(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape1-file-valued-flag")
    proc = run_hook(
        bash_payload(entry["command"], transcript("unwitnessed")), tmp_path,
        env_extra={"CLAUDE_PUBLISHED_TEXT_GATE": "0"},
    )
    assert proc.returncode == 0
    assert not _is_deny(proc)
    rows = _advisory_rows(tmp_path)
    assert any(r.get("kind") == "TEXT_GATE_OVERRIDE_USED" for r in rows)


def test_shape3_heredoc_in_command_substitution_wiring(tmp_path):
    entry = _by_label("shape3-heredoc-in-command-substitution")
    proc_unwitnessed = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc_unwitnessed.returncode == 0
    assert _is_deny(proc_unwitnessed)


def test_shape4_same_command_var_assignment_witnessed_allows(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape4-same-command-var-assignment")
    proc = run_hook(bash_payload(entry["command"], transcript("witnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_shape4_same_command_var_assignment_unwitnessed_denies(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape4-same-command-var-assignment")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_shape6_inline_path_read_cat_form_unwitnessed_denies(tmp_path):
    entry = _by_label("shape6-inline-path-read-cat-form")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_shape6_inline_path_read_lt_form_witnessed_allows(tmp_path):
    entry = _by_label("shape6-inline-path-read-lt-form")
    proc = run_hook(bash_payload(entry["command"], transcript("witnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_core_gh_pr_create_body_file_witnessed_allows(tmp_path):
    entry = _by_label("core-gh-pr-create-body-file")
    proc = run_hook(bash_payload(entry["command"], transcript("witnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_core_gh_pr_create_body_file_unwitnessed_denies(tmp_path):
    entry = _by_label("core-gh-pr-create-body-file")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_witnessed_body_with_artifact_hint_gets_the_same_decision_as_without(tmp_path):
    # Both bodies bind WRITER_OUTPUT/POST_WITNESS, so both must allow with the
    # identical observable shape (returncode + empty stdout, since the advisory
    # is a sink record, never a permissionDecision on an allow path -- see the
    # module docstring's ALLOW-PATH ARTIFACT HINT paragraph). Only the hinted
    # body's advisory row differs.
    plain_home = tmp_path / "plain"
    hinted_home = tmp_path / "hinted"
    write_seam(plain_home)
    write_seam(hinted_home)

    plain_entry = _by_label("core-gh-pr-create-body-file")
    plain_proc = run_hook(bash_payload(plain_entry["command"], transcript("witnessed")), plain_home)

    hinted_command = (
        'gh pr create --title "Fix" --body-file '
        "scripts/tests/fixtures/published-text/artifact-hint-body.md"
    )
    hinted_proc = run_hook(bash_payload(hinted_command, transcript("artifact-hint")), hinted_home)

    assert plain_proc.returncode == hinted_proc.returncode == 0
    assert not _is_deny(plain_proc) and not _is_deny(hinted_proc)
    assert plain_proc.stdout == hinted_proc.stdout == ""

    assert not any(r.get("kind") == "ALLOWED_WITH_ARTIFACT_HINT" for r in _advisory_rows(plain_home))
    assert any(r.get("kind") == "ALLOWED_WITH_ARTIFACT_HINT" for r in _advisory_rows(hinted_home))


# --- UNRESOLVED / NOT_A_PUBLICATION (subprocess end-to-end) -----------------

def test_unresolvable_substitution_allows_and_records_advisory(tmp_path):
    entry = _by_label("genuinely-unresolvable-substitution")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    rows = _advisory_rows(tmp_path)
    assert any(r.get("kind") == "UNRESOLVED" for r in rows)


def test_missing_target_file_allows(tmp_path):
    entry = _by_label("missing-target-file-valued-command")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_non_publication_bash_command_allows_silently(tmp_path):
    entry = _by_label("non-publication-bash-command")
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert _advisory_rows(tmp_path) == []  # NOT_A_PUBLICATION records nothing


# --- ATTACHMENT: content-sniffing prefilter before the judge ----------------

def test_recognized_artifact_attachment_allows_without_a_judge_call(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape5-attachment-operand")
    # Killswitch OFF would still fail-open, so leaving it enabled and checking
    # no ATTACHMENT_JUDGE_* advisory landed proves the judge was never reached
    # -- artifact-dump.md's own [meta.order]/key=value syntax must be parsed
    # and excluded by the prefilter alone.
    proc = run_hook(bash_payload(entry["command"], transcript("unwitnessed")), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    rows = _advisory_rows(tmp_path)
    assert not any(str(r.get("kind", "")).startswith("ATTACHMENT_JUDGE") for r in rows)


def test_unrecognized_attachment_content_falls_open_on_killswitch(tmp_path):
    write_seam(tmp_path)
    entry = _by_label("shape5-attachment-operand")
    # Point the same attachment verb at reader-facing.md (plain prose, no
    # artifact syntax at all) instead of the fixture's own artifact-dump.md.
    command = entry["command"].replace("artifact-dump.md", "reader-facing.md")
    proc = run_hook(
        bash_payload(command, transcript("unwitnessed")), tmp_path,
        env_extra={"CLAUDE_PUBLISHED_ATTACHMENT_SEMANTIC": "0"},
    )
    assert proc.returncode == 0
    assert not _is_deny(proc)
    rows = _advisory_rows(tmp_path)
    assert any(r.get("kind") == "ATTACHMENT_JUDGE_FAIL_OPEN" for r in rows)


# --- writer_pass wiring against the richer fixture matrix (in-process) ------

def test_containment_trap_denies_both_short_and_full_bodies():
    mod = _load_module()
    short_body = (FIXTURES / "short-body.md").read_text(encoding="utf-8")
    reader_body = (FIXTURES / "reader-facing.md").read_text(encoding="utf-8")
    for body in (short_body, reader_body):
        resolution = published_body.Resolution(kind=published_body.TEXT, body=body, shape=1)
        payload = {"transcript_path": str(transcript("containment-trap"))}
        decision, _reason = mod._decide_text(resolution, "cmd", payload)
        assert decision == "deny", "containment must not substitute for equality"


def test_discrimination_denies_pre_witness_write_allows_post_witness_write():
    mod = _load_module()
    unpolished = (FIXTURES / "unpolished.md").read_text(encoding="utf-8")
    polished = (FIXTURES / "polished.md").read_text(encoding="utf-8")
    payload = {"transcript_path": str(transcript("discrimination"))}

    pre = published_body.Resolution(kind=published_body.TEXT, body=unpolished, shape=1)
    decision, _ = mod._decide_text(pre, "cmd", payload)
    assert decision == "deny"

    post = published_body.Resolution(kind=published_body.TEXT, body=polished, shape=1)
    decision, _ = mod._decide_text(post, "cmd", payload)
    assert decision == "allow"


def test_writer_output_equality_allows_a_short_body_without_containment():
    mod = _load_module()
    short_body = (FIXTURES / "short-body.md").read_text(encoding="utf-8")
    resolution = published_body.Resolution(kind=published_body.TEXT, body=short_body, shape=1)
    payload = {"transcript_path": str(transcript("writer-output-equality"))}
    decision, _ = mod._decide_text(resolution, "cmd", payload)
    assert decision == "allow"


def test_two_witness_transcript_binds_to_the_first_witness_existentially():
    mod = _load_module()
    body = (FIXTURES / "reader-facing.md").read_text(encoding="utf-8")
    resolution = published_body.Resolution(kind=published_body.TEXT, body=body, shape=1)
    payload = {"transcript_path": str(transcript("two-witness"))}
    decision, _ = mod._decide_text(resolution, "cmd", payload)
    assert decision == "allow"


# --- fail-open discipline (in-process) --------------------------------------

def test_no_transcript_path_allows_and_records_advisory():
    mod = _load_module()
    resolution = published_body.Resolution(kind=published_body.TEXT, body="anything", shape=1)
    decision, reason = mod._decide_text(resolution, "cmd", {})
    assert (decision, reason) == ("allow", "")


def test_unreadable_transcript_allows_and_records_advisory(tmp_path):
    mod = _load_module()
    resolution = published_body.Resolution(kind=published_body.TEXT, body="anything", shape=1)
    payload = {"transcript_path": str(tmp_path / "absent.jsonl")}
    decision, reason = mod._decide_text(resolution, "cmd", payload)
    assert (decision, reason) == ("allow", "")


def test_unreadable_attachment_allows():
    mod = _load_module()
    resolution = published_body.Resolution(kind=published_body.ATTACHMENT, path="/no/such/file", shape=5)
    decision, reason = mod._decide_attachment(resolution, "cmd")
    assert (decision, reason) == ("allow", "")


def test_attachment_judge_budget_exhaustion_allows(monkeypatch, tmp_path):
    mod = _load_module()
    # First clock read is JudgeBudget's construction (deadline = t0 + 45); the
    # second is remaining_and_timeout's single read, advanced far past the
    # deadline so remaining() is deeply negative -- well under the 41s floor.
    reads = iter([0.0, 1000.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(reads))
    resolution = published_body.Resolution(
        kind=published_body.ATTACHMENT, path=str(FIXTURES / "reader-facing.md"), shape=5,
    )
    decision, reason = mod._decide_attachment(resolution, "cmd")
    assert (decision, reason) == ("allow", "")


def test_attachment_judge_genuine_deny_and_allow(monkeypatch):
    mod = _load_module()
    resolution = published_body.Resolution(
        kind=published_body.ATTACHMENT, path=str(FIXTURES / "reader-facing.md"), shape=5,
    )

    monkeypatch.setattr(advisor, "subprocess_runner", lambda argv, *, timeout, stdin="": types.SimpleNamespace(
        returncode=0, stdout="YES\n", timed_out=False,
    ))
    decision, _ = mod._decide_attachment(resolution, "cmd")
    assert decision == "deny"

    monkeypatch.setattr(advisor, "subprocess_runner", lambda argv, *, timeout, stdin="": types.SimpleNamespace(
        returncode=0, stdout="NO\n", timed_out=False,
    ))
    decision, _ = mod._decide_attachment(resolution, "cmd")
    assert decision == "allow"


# --- content-sniffing prefilter, isolated -----------------------------------

def test_recognized_artifact_kind_json():
    mod = _load_module()
    assert mod._recognized_artifact_kind('{"a": 1, "b": [1, 2, 3]}') == "json"


def test_recognized_artifact_kind_unified_diff():
    mod = _load_module()
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-old\n+new\n"
    assert mod._recognized_artifact_kind(diff) == "unified diff"


def test_recognized_artifact_kind_prose_is_none():
    mod = _load_module()
    prose = (FIXTURES / "reader-facing.md").read_text(encoding="utf-8")
    assert mod._recognized_artifact_kind(prose) is None


def test_recognized_artifact_kind_toml_plan_render():
    mod = _load_module()
    artifact = (FIXTURES / "artifact-dump.md").read_text(encoding="utf-8")
    assert mod._recognized_artifact_kind(artifact) is not None


# --- always exits 0, even when decide() raises ------------------------------

def test_main_never_raises_on_a_broken_decide(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    monkeypatch.setattr(mod, "decide", lambda payload: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(read=lambda: "{}"))
    monkeypatch.setattr(json, "load", lambda fh: {"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert mod.main() == 0


def test_malformed_stdin_allows(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="this is not json",
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_AGENT_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_not_a_publication_returns_well_under_the_stated_bound(tmp_path):
    start = time.monotonic()
    proc = run_hook(bash_payload("git status --short", transcript("unwitnessed")), tmp_path)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert elapsed <= 2.0, f"NOT_A_PUBLICATION took {elapsed:.2f}s"
