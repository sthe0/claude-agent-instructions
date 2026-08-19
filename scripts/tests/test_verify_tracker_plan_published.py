"""Tracker-agnostic plan-publication gate (verify-tracker-plan-published.py).

Stage 2 of the ticket-plan-publication-gate plan: reduces two tracker read
verbs (tracker_plan_marker / tracker_plan_artifact_digest) to a single
five-status verdict on whether a durable artifact and its marker comment both
still match the current TOML plan. Every ticket key here is the neutral
ABC-123 fixture (this is a PUBLIC repo).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SCRIPT = SCRIPTS_DIR / "verify-tracker-plan-published.py"

_SPEC = importlib.util.spec_from_file_location("verify_tracker_plan_published", SCRIPT)
vtp = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = vtp  # dataclass() needs the module registered to resolve type hints
_SPEC.loader.exec_module(vtp)

KEY = "ABC-123"


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def plan(tmp_path):
    p = tmp_path / "plan.toml"
    p.write_text('[meta]\ntask_id = "t"\n', encoding="utf-8")
    return p


@pytest.fixture
def plan_sha(plan):
    return vtp.plan_sync.plan_file_sha256(str(plan))


def _write_backend(plugin_dir: Path, name: str, body: str) -> Path:
    d = plugin_dir / "trackers"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.sh"
    f.write_text(body, encoding="utf-8")
    return f


def _env(base: "dict | None" = None) -> dict:
    return vtp.build_env(base if base is not None else os.environ, SCRIPTS_DIR)


# ── evaluate(): the five statuses (mirrors --selftest, at the pytest layer) ──

def test_ok_when_both_verbs_confirm(tmp_path, plan, plan_sha):
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("OK", "ok", "-")
    assert vtp.EXIT_BY_STATUS[status] == 0


def test_drift_when_marker_names_a_stale_hash(tmp_path, plan, plan_sha):
    stale = "0" * 64 if plan_sha != "0" * 64 else "1" * 64
    stale_marker_line = vtp.plan_sync.format_marker(str(plan), stale)
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{stale_marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("DRIFT", "mismatch", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_no_plan_when_marker_verb_finds_nothing(tmp_path, plan, plan_sha):
    backend = _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { :; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "absent", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_no_artifact_when_digest_mismatches(tmp_path, plan, plan_sha):
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    wrong_hex = "a" * 64 if plan_sha != "a" * 64 else "b" * 64
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{wrong_hex}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-ARTIFACT", "mismatch", vtp.DIGEST_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_unverifiable_when_backend_unresolvable(plan, plan_sha):
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, None, _env(), plan.name)
    assert (status, reason, verb) == ("UNVERIFIABLE", "backend-unresolved", "-")
    assert vtp.EXIT_BY_STATUS[status] == 3


def test_unverifiable_when_both_verbs_undeclared(tmp_path, plan, plan_sha):
    backend = _write_backend(tmp_path, "trk", "# no verbs declared\n")
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert status == "UNVERIFIABLE"
    assert reason == "verb-undeclared"
    assert verb == f"{vtp.MARKER_VERB},{vtp.DIGEST_VERB}"
    assert vtp.EXIT_BY_STATUS[status] == 3


# ── declared-but-failing verb -> exit 1 (not 3), for EACH verb ──────────────

def test_declared_failing_marker_verb_is_exit1_not_3(tmp_path, plan, plan_sha):
    backend = _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { return 1; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "verb-failed", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_declared_failing_digest_verb_is_exit1_not_3(tmp_path, plan, plan_sha):
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        "tracker_plan_artifact_digest() { return 1; }\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-ARTIFACT", "verb-failed", vtp.DIGEST_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


# ── exit 3 reached ONLY on {both undeclared, backend unresolvable} ──────────

def test_marker_refutes_digest_undeclared_stays_exit1(tmp_path, plan, plan_sha):
    """A refuting declared verb outranks a sibling's non-declaration."""
    backend = _write_backend(tmp_path, "trk", "tracker_plan_marker() { :; }\n")
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "absent", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_marker_confirms_digest_undeclared_is_no_artifact_verb_undeclared(tmp_path, plan, plan_sha):
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    backend = _write_backend(
        tmp_path, "trk", f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-ARTIFACT", "verb-undeclared", vtp.DIGEST_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


# ── the seam: only a genuine non-declaration may reach exit 3 ──────────────

def test_source_time_credentials_guard_is_verb_failed_not_unverifiable(tmp_path, plan, plan_sha):
    """A backend aborting BEFORE its `function` definitions (no credentials) leaves
    both verbs undeclarable. Reading that as "undeclared" would return exit 3 — a
    free skip through the gate for anyone who unsets one env var."""
    backend = _write_backend(
        tmp_path, "trk",
        '[[ -z "${FAKE_TRACKER_TOKEN:-}" ]] && { echo "no credentials" >&2; return 1; }\n'
        "tracker_plan_marker() { :; }\n"
        "tracker_plan_artifact_digest() { :; }\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "verb-failed", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


@pytest.mark.parametrize("rc", [97, 98])
def test_verb_returning_a_probe_sentinel_rc_is_not_read_as_undeclared(tmp_path, plan, plan_sha, rc):
    """The probe owns 97/98; a verb returning either stays in its one degrade class."""
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ return {rc}; }}\n"
        f"tracker_plan_artifact_digest() {{ return {rc}; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "verb-failed", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_probe_verb_separates_declared_undeclared_and_unsourceable(tmp_path):
    declared = _write_backend(tmp_path / "a", "trk", "tracker_plan_marker() { :; }\n")
    undeclared = _write_backend(tmp_path / "b", "trk", "# nothing declared\n")
    unsourceable = _write_backend(tmp_path / "c", "trk", "return 1\ntracker_plan_marker() { :; }\n")

    state, _stderr = vtp.probe_verb(str(declared), vtp.MARKER_VERB, _env())
    assert state == "declared"
    state, _stderr = vtp.probe_verb(str(undeclared), vtp.MARKER_VERB, _env())
    assert state == "undeclared"
    state, _stderr = vtp.probe_verb(str(unsourceable), vtp.MARKER_VERB, _env())
    assert state == "unavailable"
    state, _stderr = vtp.probe_verb(str(tmp_path / "gone.sh"), vtp.MARKER_VERB, _env())
    assert state == "unavailable"


def test_probe_verb_exit97_source_time_guard_is_unavailable_not_undeclared(tmp_path):
    """should-fix 2: a source-time `exit 97` (not `return 1`) terminates the probe's
    shell before the `declare -F` check ever runs — no positive token is printed, so
    rc alone must not be trusted as the UNDECLARED sentinel."""
    guard = _write_backend(
        tmp_path, "trk", f"exit {vtp.PROBE_UNDECLARED_RC}\ntracker_plan_marker() {{ :; }}\n"
    )
    state, _stderr = vtp.probe_verb(str(guard), vtp.MARKER_VERB, _env())
    assert state == "unavailable"


def test_source_time_exit97_guard_is_verb_failed_not_unverifiable(tmp_path, plan, plan_sha):
    """should-fix 2, at the CLI-facing evaluate() layer: a source-time `exit 97`
    guard (as opposed to `return 1`) must land the gate on exit 1 (unavailable /
    verb-failed), not exit 3 (UNVERIFIABLE) — before the fix, rc 97 alone forged
    the probe's own UNDECLARED sentinel and both verbs read as "undeclared"."""
    backend = _write_backend(
        tmp_path, "trk",
        f"exit {vtp.PROBE_UNDECLARED_RC}\n"
        "tracker_plan_marker() { :; }\n"
        "tracker_plan_artifact_digest() { :; }\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "verb-failed", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_hanging_verb_times_out_into_verb_failed(tmp_path, plan, plan_sha, monkeypatch):
    monkeypatch.setattr(vtp, "VERB_TIMEOUT_S", 1)
    backend = _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { sleep 5; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "verb-failed", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


# ── the marker verb alone undeclared (its sibling still answering) ──────────

def test_marker_undeclared_digest_confirms_is_no_plan_verb_undeclared(tmp_path, plan, plan_sha):
    backend = _write_backend(
        tmp_path, "trk", f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n"
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-PLAN", "verb-undeclared", vtp.MARKER_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


def test_marker_undeclared_digest_refutes_lets_the_declared_verb_decide(tmp_path, plan, plan_sha):
    wrong_hex = "a" * 64 if plan_sha != "a" * 64 else "b" * 64
    backend = _write_backend(
        tmp_path, "trk", f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{wrong_hex}'; }}\n"
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert (status, reason, verb) == ("NO-ARTIFACT", "mismatch", vtp.DIGEST_VERB)
    assert vtp.EXIT_BY_STATUS[status] == 1


# ── both declared verbs refute -> the MARKER verb decides (N6) ─────────────

def test_both_verbs_refute_marker_decides(tmp_path, plan, plan_sha):
    stale = "0" * 64 if plan_sha != "0" * 64 else "1" * 64
    stale_marker_line = vtp.plan_sync.format_marker(str(plan), stale)
    wrong_hex = "a" * 64 if plan_sha != "a" * 64 else "b" * 64
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{stale_marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{wrong_hex}'; }}\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    # Both marker (mismatch) and digest (mismatch) refute; marker's verdict wins.
    assert (status, reason, verb) == ("DRIFT", "mismatch", vtp.MARKER_VERB)


# ── artifact basename: from the marker's plan= field, else --plan's own name ─

def test_artifact_basename_taken_from_marker_plan_field(tmp_path, plan, plan_sha):
    other_name = "other-plan.toml"
    marker_line = f"<!-- agent-plan-sync: plan_sha256={plan_sha} plan={other_name} -->"
    backend = _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        "tracker_plan_artifact_digest() { return 1; }\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert artifact == other_name


def test_artifact_basename_falls_back_to_plan_file_name(tmp_path, plan, plan_sha):
    backend = _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { :; }\n"
        "tracker_plan_artifact_digest() { return 1; }\n",
    )
    status, reason, verb, artifact = vtp.evaluate(plan_sha, KEY, backend, _env(), plan.name)
    assert artifact == plan.name


# ── backend NAME precedence ladder (flag > env > record > unset) ────────────

def test_backend_name_flag_wins_over_all():
    name, source = vtp.resolve_backend_name(
        "flagname", lambda k: "envname", lambda pwd: "recname", "/pwd"
    )
    assert (name, source) == ("flagname", "flag")


def test_backend_name_env_wins_over_record():
    getenv = lambda k: "envname" if k == "CLAUDE_TRACKER_BACKEND" else None
    name, source = vtp.resolve_backend_name(None, getenv, lambda pwd: "recname", "/pwd")
    assert (name, source) == ("envname", "env")


def test_backend_name_record_used_when_no_flag_or_env():
    name, source = vtp.resolve_backend_name(None, lambda k: None, lambda pwd: "recname", "/pwd")
    assert (name, source) == ("recname", "record")


def test_backend_name_unset_when_nothing_resolves():
    name, source = vtp.resolve_backend_name(None, lambda k: None, lambda pwd: None, "/pwd")
    assert (name, source) == (None, "unset")


# ── backend FILE resolution: built-in first, plugin second ─────────────────

def test_resolve_backend_file_builtin_before_plugin(tmp_path):
    scripts_dir = tmp_path / "scripts"
    builtin_file = scripts_dir / "project_entry" / "trackers" / "trk.sh"
    builtin_file.parent.mkdir(parents=True)
    builtin_file.write_text("# builtin\n")
    plugin_dir = tmp_path / "plugins"
    _write_backend(plugin_dir, "trk", "# plugin\n")

    assert vtp.resolve_backend_file("trk", scripts_dir, plugin_dir) == builtin_file


def test_resolve_backend_file_falls_back_to_plugin(tmp_path):
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "project_entry" / "trackers").mkdir(parents=True)
    plugin_dir = tmp_path / "plugins"
    plugin_file = _write_backend(plugin_dir, "trk", "# plugin\n")

    assert vtp.resolve_backend_file("trk", scripts_dir, plugin_dir) == plugin_file


def test_resolve_backend_file_none_when_neither_exists(tmp_path):
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "project_entry" / "trackers").mkdir(parents=True)
    assert vtp.resolve_backend_file("trk", scripts_dir, tmp_path / "plugins") is None


def test_resolve_backend_file_none_when_name_is_none(tmp_path):
    assert vtp.resolve_backend_file(None, tmp_path, tmp_path) is None


# ── forwarded environment (f) ────────────────────────────────────────────────

def test_build_env_defaults_claude_enter_task_dir():
    env = vtp.build_env({}, SCRIPTS_DIR)
    assert env["CLAUDE_ENTER_TASK_DIR"] == str(SCRIPTS_DIR)


def test_build_env_preserves_explicit_claude_enter_task_dir():
    env = vtp.build_env({"CLAUDE_ENTER_TASK_DIR": "/custom"}, SCRIPTS_DIR)
    assert env["CLAUDE_ENTER_TASK_DIR"] == "/custom"


def test_forwarded_env_reaches_the_backend_process(tmp_path):
    backend = _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { printf 'TRACKER_CLI=%s\\n' \"$TRACKER_CLI\"; }\n",
    )
    env = vtp.build_env({**os.environ, "TRACKER_CLI": "/opt/faketracker"}, SCRIPTS_DIR)
    run = vtp.call_verb(str(backend), vtp.MARKER_VERB, [KEY], env)
    assert run.state == "declared"
    assert "TRACKER_CLI=/opt/faketracker" in run.stdout


# ── a backend reached through a SYMLINK: BACKEND= carries the realpath (N2) ──

def test_backend_field_carries_realpath_through_symlinked_plugin_dir(tmp_path, plan, plan_sha):
    real_root = tmp_path / "real-plugin-root"
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    real_backend = _write_backend(
        real_root, "synthtrk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "trackers").symlink_to(real_root / "trackers", target_is_directory=True)

    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--plan", str(plan), "--key", KEY, "--backend", "synthtrk"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    fields = _parse_status_line(proc.stderr)
    assert dict(fields)["BACKEND"] == str(real_backend.resolve())
    assert dict(fields)["BACKEND_SOURCE"] == "flag"


# ── full CLI round-trip ──────────────────────────────────────────────────────

def test_cli_end_to_end_ok(tmp_path, plan, plan_sha):
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--plan", str(plan), "--key", KEY, "--backend", "trk"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "OK\n"  # the bare status word, and nothing else
    assert "STATUS=OK REASON=ok" in proc.stderr
    assert f"KEY={KEY}" in proc.stderr
    assert f"PLAN_SHA={plan_sha}" in proc.stderr
    assert "BACKEND_SOURCE=flag" in proc.stderr


def test_cli_status_word_on_stdout_for_a_refuting_status(tmp_path, plan, plan_sha):
    """Both streams carry the verdict on a refuting path too, not only the happy one."""
    _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { :; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--plan", str(plan), "--key", KEY, "--backend", "trk"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1
    assert proc.stdout == "NO-PLAN\n"
    assert "STATUS=NO-PLAN REASON=absent" in proc.stderr


# ── the exit-3 contract, asserted at PROCESS level ──────────────────────────

def _run_cli(plan: Path, plugin_dir: Path, backend_name: str, key: str = KEY):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--plan", str(plan), "--key", key, "--backend", backend_name],
        capture_output=True, text=True, env=env,
    )


def test_cli_exit3_when_the_backend_file_is_unresolvable(tmp_path, plan):
    proc = _run_cli(plan, tmp_path, "nosuchtracker")
    assert proc.returncode == 3
    assert proc.stdout == "UNVERIFIABLE\n"
    assert "REASON=backend-unresolved" in proc.stderr


def test_cli_exit3_when_both_verbs_are_genuinely_undeclared(tmp_path, plan):
    _write_backend(tmp_path, "trk", "# no verbs declared\n")
    proc = _run_cli(plan, tmp_path, "trk")
    assert proc.returncode == 3
    assert proc.stdout == "UNVERIFIABLE\n"
    assert "REASON=verb-undeclared" in proc.stderr


def test_cli_source_time_failure_exits_1_not_3(tmp_path, plan):
    _write_backend(
        tmp_path, "trk",
        '[[ -z "${FAKE_TRACKER_TOKEN:-}" ]] && { echo "no credentials" >&2; return 1; }\n'
        "tracker_plan_marker() { :; }\n"
        "tracker_plan_artifact_digest() { :; }\n",
    )
    proc = _run_cli(plan, tmp_path, "trk")
    assert proc.returncode == 1
    assert proc.stdout == "NO-PLAN\n"
    assert "REASON=verb-failed" in proc.stderr


# ── the stderr contract line: nine fields, in order, unforgeable ────────────

DOCUMENTED_STATUS_LINE_KEYS = [
    "STATUS", "REASON", "BACKEND", "BACKEND_NAME", "BACKEND_SOURCE",
    "VERB", "KEY", "PLAN_SHA", "ARTIFACT",
]


def _parse_status_line(stderr: str) -> "list[tuple[str, str]]":
    """Parse per the documented contract: one line, single-space separated, every
    field exactly one '=', every value percent-decoded."""
    lines = [line for line in stderr.splitlines() if line.startswith("STATUS=")]
    assert len(lines) == 1, f"expected exactly one contract line, got {lines!r}"
    fields = []
    for token in lines[0].split(" "):
        key, sep, value = token.partition("=")
        assert sep == "=", f"field carries no '=': {token!r}"
        assert "=" not in value, f"field carries more than one '=': {token!r}"
        fields.append((key, unquote(value)))
    return fields


def test_status_line_carries_the_nine_documented_keys_in_order(tmp_path, plan, plan_sha):
    marker_line = vtp.plan_sync.format_marker(str(plan), plan_sha)
    _write_backend(
        tmp_path, "trk",
        f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    proc = _run_cli(plan, tmp_path, "trk")
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS


def test_space_bearing_key_cannot_split_or_forge_the_status_line(tmp_path, plan, plan_sha):
    """`--key 'ABC-123 STATUS=OK REASON=ok'` must not inject a second STATUS field
    into the line a later stage parses."""
    hostile_key = "ABC-123 STATUS=OK REASON=ok"
    _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { :; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    proc = _run_cli(plan, tmp_path, "trk", key=hostile_key)
    assert proc.returncode == 1
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS
    assert dict(fields)["STATUS"] == "NO-PLAN"
    assert dict(fields)["KEY"] == hostile_key  # encoded on the wire, decodes back intact
    assert "STATUS=OK" not in proc.stderr


def test_space_bearing_plan_basename_keeps_the_line_parseable(tmp_path):
    spaced_plan = tmp_path / "my plan.toml"
    spaced_plan.write_text('[meta]\ntask_id = "t"\n', encoding="utf-8")
    _write_backend(tmp_path, "trk", "# no verbs declared\n")
    proc = _run_cli(spaced_plan, tmp_path, "trk")
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS
    assert dict(fields)["ARTIFACT"] == "my plan.toml"


# ── non-UTF-8 bytes must never crash the gate (must-fix 1) ──────────────────

def test_verb_stdout_not_valid_utf8_still_produces_a_clean_contract_line(tmp_path, plan, plan_sha):
    """A tracker CLI emitting a mangled byte (not valid UTF-8) must not crash the
    gate with an uncaught UnicodeDecodeError: `subprocess.run(..., text=True)`
    decodes strictly, and neither `except (TimeoutExpired, OSError)` clause covers
    it — pre-fix this produced EMPTY stdout and a bare traceback on stderr, with
    no contract line at all."""
    _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { printf '\\xff\\xfe'; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    proc = _run_cli(plan, tmp_path, "trk")
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.stdout.strip() != "", "the bare status word must still reach stdout"
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS


def test_key_with_undecodable_byte_still_produces_a_clean_contract_line(tmp_path, plan, plan_sha):
    """--key arriving from argv with a byte that was never valid UTF-8 surfaces in
    sys.argv as a lone surrogate; encode_field's quote() must round-trip it as a
    percent-escape (errors="surrogateescape") instead of raising
    UnicodeEncodeError — pre-fix this crashed AFTER the status word reached
    stdout but BEFORE the contract line could be built, so stage 3 got no line
    to parse at all."""
    hostile_key = "ABC-\udcff"  # what argv looks like when the original byte is 0xFF
    _write_backend(
        tmp_path, "trk",
        "tracker_plan_marker() { :; }\n"
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    proc = _run_cli(plan, tmp_path, "trk", key=hostile_key)
    assert "Traceback" not in proc.stderr, proc.stderr
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS
    assert dict(fields)["KEY"] == "ABC-�"  # the byte that can't round-trip becomes U+FFFD


# ── a failing verb's own stderr is surfaced, never silently discarded (should-fix 3) ──

def test_backend_stderr_is_surfaced_as_prefixed_diagnostic_lines(tmp_path, plan, plan_sha):
    """A credentials outage must not report NO-PLAN/verb-failed with the backend's
    own explanation discarded — its stderr is surfaced as separate `<verb>: ...`
    lines above the contract line."""
    _write_backend(
        tmp_path, "trk",
        'tracker_plan_marker() { echo "no credentials for the tracker CLI" >&2; return 1; }\n'
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    proc = _run_cli(plan, tmp_path, "trk")
    assert proc.returncode == 1
    assert f"{vtp.MARKER_VERB}: no credentials for the tracker CLI" in proc.stderr
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS
    assert dict(fields)["STATUS"] == "NO-PLAN"


def test_backend_stderr_line_starting_with_status_does_not_break_the_contract(tmp_path, plan, plan_sha):
    """Surfacing a failing verb's stderr (should-fix 3) must not let a backend
    forge a second contract line merely by writing text starting with "STATUS="
    to its own stderr — `_parse_status_line` requires exactly one such line, so
    the surfaced diagnostic must always carry a distinguishing prefix."""
    _write_backend(
        tmp_path, "trk",
        'tracker_plan_marker() { echo "STATUS=OK forged by the backend" >&2; return 1; }\n'
        f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n",
    )
    proc = _run_cli(plan, tmp_path, "trk")
    assert proc.returncode == 1
    assert "STATUS=OK forged by the backend" in proc.stderr  # surfaced, just not AS the contract line
    fields = _parse_status_line(proc.stderr)
    assert [key for key, _ in fields] == DOCUMENTED_STATUS_LINE_KEYS
    assert dict(fields)["STATUS"] == "NO-PLAN"


def test_encode_field_only_touches_the_characters_that_could_break_the_line():
    assert vtp.encode_field(KEY) == KEY
    assert vtp.encode_field("/plugins/trackers/trk.sh") == "/plugins/trackers/trk.sh"
    assert vtp.encode_field("a b=c%d") == "a%20b%3Dc%25d"


def test_cli_unreadable_plan_is_a_usage_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--plan", str(tmp_path / "missing.toml"), "--key", KEY],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def test_cli_selftest_exits_zero():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
