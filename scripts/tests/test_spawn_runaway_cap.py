"""spawn-specialist.py runaway-ceiling decoupling (flat-budget-calibration P1).

Under flat billing the per-tier budget-*-usd values are expected-size TELEMETRY
LABELS, not money kill-caps. The applied `--max-budget-usd` is a single global
runaway ceiling (spawn-runaway-ceiling-usd), so a spawn is never truncated on
legitimate large work — only killed on a true runaway.

Covers:
- runaway_ceiling() returns the config ceiling, falling back to the large tier
  when the key is absent (fail-safe: never unbounded).
- the cap in the constructed `claude -p` argv equals the ceiling regardless of
  the --budget tier (dry-run, both small and large).
- the spawn-costs row records the tier LABEL as budget_tier and the ceiling as
  budget_usd_cap (e2e against a stub claude).
- the soft-warn fires iff realized cost > SOFT_WARN_MULT x the tier label, and
  never changes the exit code / kills the spawn.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_runaway", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ── runaway_ceiling helper ──────────────────────────────────────────────────

def test_runaway_ceiling_returns_config_key_when_present():
    constants = {"spawn-runaway-ceiling-usd": "25.0", "budget-large-usd": "8.00"}
    assert MOD.runaway_ceiling(constants) == "25.0"


def test_runaway_ceiling_falls_back_to_large_tier_when_key_absent():
    # Fail-safe: a partial rollout without the key must never remove the backstop.
    constants = {"budget-large-usd": "8.00"}
    assert MOD.runaway_ceiling(constants) == "8.00"


def test_soft_warn_mult_is_defined():
    assert MOD.SOFT_WARN_MULT == 2.0


def test_real_config_ceiling_is_above_large_tier():
    """The live config.md ceiling must be a real number strictly above the large
    tier, so true runaways still die but legitimate large work is not truncated."""
    constants = MOD.parse_config_md()
    ceiling = float(MOD.runaway_ceiling(constants))
    large = float(MOD.budget_value("large", constants))
    assert ceiling > large


# ── dry-run argv: applied cap is the ceiling regardless of tier ─────────────

pytestmark_posix = pytest.mark.skipif(os.name != "posix", reason="stub scripts are POSIX shell")


def _setup_fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    skill_dir = home / ".claude" / "skills" / "developer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# stub developer skill for tests\n")
    return home


def _base_env(home: Path, bin_dir: Path | None = None) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "AGENT_RECURSION_DEPTH": "0",
    }
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    # config_root prefers CLAUDE_CONFIG_DIR/CLAUDE_AGENT_HOME over HOME — strip
    # them so the child derives its root from the tmp HOME (see the sibling
    # scope-deregister e2e test for the same technique).
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_AGENT_HOME", None)
    return env


def _dry_run(tmp_path: Path, home: Path, tier: str) -> subprocess.CompletedProcess:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    cmd = [
        "python3", str(SCRIPT),
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "stub does nothing",
        "--criterion-type", "measurable",
        "--budget", tier,
        "--dry-run",
    ]
    return subprocess.run(cmd, env=_base_env(home), capture_output=True, text=True, timeout=30)


@pytestmark_posix
@pytest.mark.parametrize("tier", ["small", "large"])
def test_dry_run_applies_runaway_ceiling_regardless_of_tier(tmp_path, tier):
    home = _setup_fake_home(tmp_path)
    result = _dry_run(tmp_path, home, tier)
    assert result.returncode == 0, result.stderr
    constants = MOD.parse_config_md()
    ceiling = MOD.runaway_ceiling(constants)
    assert f"--max-budget-usd {ceiling}" in result.stdout, result.stdout
    # The per-tier value must NOT be the applied cap (unless it happens to equal
    # the ceiling, which it does not for small/large).
    tier_value = MOD.budget_value(tier, constants)
    assert f"--max-budget-usd {tier_value}" not in result.stdout


# ── e2e stub claude: cost row + soft-warn ───────────────────────────────────

def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _stub_claude(bin_dir: Path, home: Path, session_id: str, cost_usd: float) -> None:
    """A stand-in `claude` that touches a decoy transcript (so discovery resolves
    fast) and prints a result JSON carrying the given cost_usd, then exits 0."""
    payload = json.dumps(
        {"result": f"COMPLETED: stub for {session_id}", "cost_usd": cost_usd, "session_id": session_id}
    )
    transcript_dir = home / ".claude" / "projects" / "stub"
    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        mkdir -p {shlex.quote(str(transcript_dir))}
        : > {shlex.quote(str(transcript_dir))}/decoy-$$.jsonl
        printf '%s' {shlex.quote(payload)}
        exit 0
        """
    )
    _write_exec(bin_dir / "claude", script)


def _run_spawn(tmp_path: Path, home: Path, bin_dir: Path, tier: str) -> subprocess.CompletedProcess:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    cmd = [
        "python3", str(SCRIPT),
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "stub does nothing",
        "--criterion-type", "measurable",
        "--budget", tier,
    ]
    return subprocess.run(cmd, env=_base_env(home, bin_dir), capture_output=True, text=True, timeout=30)


def _read_cost_rows(home: Path) -> list[dict]:
    ledger = home / ".local" / "log" / "claude-spawn-costs.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


@pytestmark_posix
def test_cost_row_records_tier_label_and_ceiling_cap(tmp_path):
    home = _setup_fake_home(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    session_id = f"sess-cost-{os.getpid()}-{time.time_ns()}"
    _stub_claude(bin_dir, home, session_id, cost_usd=0.01)

    # --budget small is floored to medium for developer (label-only raise), so
    # assert against the effective label rather than the requested tier.
    result = _run_spawn(tmp_path, home, bin_dir, tier="large")
    assert result.returncode == 0, result.stderr

    rows = [r for r in _read_cost_rows(home) if r.get("event") == "spawn"]
    assert rows, "expected a spawn cost row"
    row = rows[-1]
    ceiling = MOD.runaway_ceiling(MOD.parse_config_md())
    assert row["budget_tier"] == "large"          # tier retained as the label
    assert row["budget_usd_cap"] == ceiling        # applied cap is the ceiling


@pytestmark_posix
def test_soft_warn_does_not_fire_below_threshold(tmp_path):
    home = _setup_fake_home(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    session_id = f"sess-nowarn-{os.getpid()}-{time.time_ns()}"
    # cost 0.01 << 2x the large label ($8) -> no soft-warn.
    _stub_claude(bin_dir, home, session_id, cost_usd=0.01)

    result = _run_spawn(tmp_path, home, bin_dir, tier="large")
    assert result.returncode == 0, result.stderr
    assert "soft-warn" not in result.stderr


@pytestmark_posix
def test_soft_warn_fires_above_threshold_without_killing(tmp_path):
    home = _setup_fake_home(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    session_id = f"sess-warn-{os.getpid()}-{time.time_ns()}"
    # cost 20.0 > 2x the large label ($8=16) -> soft-warn, but exit stays 0
    # (the stub exits 0; the warn must not change that).
    _stub_claude(bin_dir, home, session_id, cost_usd=20.0)

    result = _run_spawn(tmp_path, home, bin_dir, tier="large")
    assert result.returncode == 0, result.stderr
    assert "soft-warn" in result.stderr
