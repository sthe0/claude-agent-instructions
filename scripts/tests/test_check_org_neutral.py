"""check-org-neutral.py: ruleset-driven publish gate (C1 mechanism).

Core carries no denylist of its own anymore — see lib/term_ruleset.py. Every
test here drives the script via $CLAUDE_TERM_RULESET_DIR, which REPLACES
discovery (never unions with a real machine's Personal/Team dirs), so these
tests are hermetic and never depend on / interfere with a real installed
ruleset. All terms used are synthetic (zorblex).
"""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check-org-neutral.py"

DENY_ZORBLEX = r"""
[[deny]]
pattern = '\bzorblex\b'
label = "internal-codename"
"""


def _ruleset_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rulesets"
    d.mkdir(exist_ok=True)
    (d / "synthetic.toml").write_text(DENY_ZORBLEX, encoding="utf-8")
    return d


def run(text: str, ruleset_dir: Path | None, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_TERM_RULESET_DIR"] = str(ruleset_dir) if ruleset_dir is not None else str(tmp_path / "empty")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=text, capture_output=True, text=True, env=env,
    )


def test_zero_rulesets_reports_clean_not_silent(tmp_path):
    r = run("mentions zorblex, but no ruleset is installed", None, tmp_path)
    assert r.returncode == 0
    assert "no term ruleset installed" in r.stdout


def test_denied_term_fails(tmp_path):
    r = run("harmless prefix zorblex harmless suffix", _ruleset_dir(tmp_path), tmp_path)
    assert r.returncode == 1
    assert "ORG-INTERNAL MARKERS FOUND" in r.stdout


def test_clean_text_passes_with_ruleset_installed(tmp_path):
    r = run("A transport-neutral pending-gate seam with pluggable notifiers.",
             _ruleset_dir(tmp_path), tmp_path)
    assert r.returncode == 0
    assert "clean" in r.stdout


def test_word_boundary_no_false_positive(tmp_path):
    r = run("a zorblexy word should not match; neither does prezorblex",
             _ruleset_dir(tmp_path), tmp_path)
    assert r.returncode == 0


def test_file_argument(tmp_path):
    f = tmp_path / "body.txt"
    f.write_text("mentions zorblex once", encoding="utf-8")
    env = dict(os.environ)
    env["CLAUDE_TERM_RULESET_DIR"] = str(_ruleset_dir(tmp_path))
    r = subprocess.run([sys.executable, str(SCRIPT), str(f)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
