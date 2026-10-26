"""Tests for the regularity layer of the provenance library and its writers.

The corpus keeps two global layers: regularities (`regularities/`, schema regularity/v1, the
knowledge layer, induced from experience or cases) and principles (`principles/`, schema
principle/v1, the norm layer, `grounded_in: [regularity:<slug>]`). Each test builds a small
corpus under tmp_path and runs a script as a subprocess, so the tests import none of the
stage's new symbols. Nothing outside tmp_path is read or written.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
VERIFY_PROVENANCE = SCRIPTS_DIR / "verify-principle-provenance.py"
VERIFY_LEAF = SCRIPTS_DIR / "verify-leaf-structure.py"
RECORD = SCRIPTS_DIR / "record-experience.py"
DATE = "2026-10-21"
DIGEST = "sha256:" + "0" * 64


@pytest.fixture
def env(tmp_path):
    base = dict(os.environ)
    base.update({
        "AGENTCTL_EDIT_LEDGER": str(tmp_path / "edit-ledger.jsonl"),
        "CLAUDE_MEMORY_RETRIEVAL_LEDGER": str(tmp_path / "retrieval.jsonl"),
        "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S": "0",
        "AGENTCTL_ADVISOR": "0",
        "WORKTREE_ROUTED": "1",
    })
    return base


def _run(script, env, *args):
    return subprocess.run([sys.executable, str(script), *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=300)


def _fm(fields: dict) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Corpus:
    """`<repo>/memory-global/leaves` with experience, regularities and principles."""

    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "repo"
        self.memory = self.repo / "memory-global"
        self.leaves = self.memory / "leaves"
        for d in ("experience", "regularities", "principles", "principles/cases"):
            (self.leaves / d).mkdir(parents=True, exist_ok=True)

    def exp(self, slug="e1", into=("r1",)) -> Path:
        return _write(self.leaves / "experience" / f"{slug}.md", _fm({
            "name": slug, "description": "a sample experience", "type": "reference",
            "schema": "difficulty/v1", "generalized_into": "[" + ", ".join(into) + "]",
            "resolution_confirmed_by_user": '"yes"', "created": DATE, "last_verified": DATE,
        }) + "\n# Sample\n\n## Difficulty\n\nA check runs where its subject is not.\n")

    def case(self, slug="c1", into=("r1",)) -> Path:
        sections = "".join(f"\n## {n}\n\nThe {n.lower()} of the case.\n"
                           for n in ("Aspect", "Expected", "Actual", "Mismatch"))
        return _write(self.leaves / "principles" / "cases" / f"{slug}.md", _fm({
            "name": slug, "description": "a sample case", "type": "reference",
            "schema": "case/v1", "origin_tier": "project", "source_digest": DIGEST,
            "generalized_into": "[" + ", ".join(into) + "]",
            "created": DATE, "last_verified": DATE,
        }) + f"\n# Case: {slug}\n" + sections)

    def regularity(self, slug="r1", induced=("e1",), rule_derived=None, index=None) -> Path:
        path = _write(self.leaves / "regularities" / f"{slug}.md", _fm({
            "name": slug, "description": "a sample regularity", "type": "reference",
            "schema": "regularity/v1", "generality": 1,
            "induced_from": "[" + ", ".join(induced) + "]",
            "rule_derived": json.dumps(rule_derived) if rule_derived else None,
            "created": DATE, "last_verified": DATE,
        }) + "\n# Sample\n\n## Regularity\n\nRun a check where its subject lives.\n\n"
             "## Generality\n\nLevel 1.\n\n## Refutation\n\nA counterexample.\n\n"
             "## Induced from\n\n- [[e1]]\n")
        self._index("regularities", slug, induced if index is None else index)
        return path

    def principle(self, slug="p1", grounded=("regularity:r1",), induced=None, index=None) -> Path:
        path = _write(self.leaves / "principles" / f"{slug}.md", _fm({
            "name": slug, "description": "a sample principle", "type": "reference",
            "schema": "principle/v1", "generality": 1,
            "grounded_in": "[" + ", ".join(grounded) + "]" if grounded is not None else None,
            "induced_from": "[" + ", ".join(induced) + "]" if induced else None,
            "created": DATE, "last_verified": DATE,
        }) + "\n# Sample\n\n## Principle\n\nRun a check where its subject lives.\n\n"
             "## Goal class\n\nControls.\n\n## Refutation\n\nA counterexample.\n")
        listed = [g.removeprefix("regularity:") for g in (grounded or ())] if index is None else index
        self._index("principles", slug, listed)
        return path

    def _index(self, area: str, slug: str, sources) -> None:
        index = self.leaves / area / "MEMORY.md"
        head = index.read_text(encoding="utf-8") if index.exists() else f"# {area}\n\n"
        head = "".join(line for line in head.splitlines(keepends=True)
                       if f"]({slug}.md)" not in line)
        _write(index, head + f"- [A leaf]({slug}.md) — a sample ← "
               + ", ".join(f"`{s}`" for s in sources) + ".\n")

    def good(self) -> "Corpus":
        self.exp()
        self.regularity()
        self.principle()
        return self

    def verify(self, env, *extra):
        return _run(VERIFY_PROVENANCE, env, "--root", self.repo, *extra)


def _finding(done, kind: str, suffix: str) -> bool:
    return any(line.startswith(f"  {kind}: memory-global/leaves/{suffix} ")
               for line in done.stdout.splitlines())


def _load_record(env):
    spec = importlib.util.spec_from_file_location("record_experience_layer", RECORD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regularity_area_classified(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    done = c.verify(env)
    assert done.returncode == 0, done.stdout + done.stderr


def test_grounded_principle_accepted(tmp_path, env):
    done = Corpus(tmp_path).good().verify(env)
    assert done.returncode == 0, done.stdout + done.stderr


def test_ungrounded_principle_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    c.principle(grounded=None, index=[])
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "ungrounded_principle", "principles/p1.md")


def test_unknown_regularity_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    c.principle(grounded=("regularity:r9",))
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "unknown_regularity", "principles/p1.md")


def test_principle_induced_from_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    c.principle(induced=("e1",))
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "principle_induced_from", "principles/p1.md")


def test_regularity_no_source_reported(tmp_path, env):
    c = Corpus(tmp_path).good()
    c.regularity("r2", induced=())
    c.principle(grounded=("regularity:r1", "regularity:r2"))
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "no_experience_source", "regularities/r2.md")


def test_regularity_rule_derived_accepted(tmp_path, env):
    c = Corpus(tmp_path).good()
    c.regularity("r2", induced=(), rule_derived="written from the standing rules")
    c.principle(grounded=("regularity:r1", "regularity:r2"))
    done = c.verify(env)
    assert done.returncode == 0, done.stdout + done.stderr


def test_regularity_asymmetric_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp(into=())
    c.regularity()
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "asymmetric_link", "regularities/r1.md")


def test_regularity_dangling_source_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity(induced=("e1", "e9"))
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "dangling_source", "regularities/r1.md")


def test_regularity_index_mismatch_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity(index=("e2",))
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "index_mismatch", "regularities/r1.md")


def test_principle_index_mismatch_reported(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    c.principle(index=("r9",))
    done = c.verify(env)
    assert done.returncode == 1
    assert _finding(done, "index_mismatch", "principles/p1.md")


def test_staged_regularity_triggers(tmp_path, env):
    c = Corpus(tmp_path)
    leaf = c.regularity(induced=("e9",))
    git_env = dict(env, GIT_CEILING_DIRECTORIES=str(tmp_path))
    subprocess.run(["git", "init", "-q", str(c.repo)], check=True, env=git_env)
    subprocess.run(["git", "-C", str(c.repo), "add", str(leaf)], check=True, env=git_env)
    done = c.verify(env, "--staged")
    assert done.returncode == 1
    assert _finding(done, "dangling_source", "regularities/r1.md")


def test_project_principle_grandfathered(tmp_path, env):
    c = Corpus(tmp_path).good()
    project = tmp_path / "acme-widgets" / ".claude" / "agent-memory"
    _write(project / "experience" / "w1.md", _fm({
        "name": "w1", "description": "a project experience", "type": "reference",
        "schema": "difficulty/v1", "generalized_into": "[widget-principle]",
        "resolution_confirmed_by_user": '"yes"', "created": DATE, "last_verified": DATE,
    }) + "\n# Sample\n\n## Difficulty\n\nA widget drifts.\n")
    _write(project / "principles" / "widget-principle.md", _fm({
        "name": "widget-principle", "description": "a project principle", "type": "reference",
        "schema": "principle/v1", "generality": 1, "induced_from": "[w1]",
        "created": DATE, "last_verified": DATE,
    }) + "\n# Sample\n\n## Principle\n\nKeep widgets aligned.\n")
    _write(project / "principles" / "MEMORY.md",
           "# Principles\n\n- [Widgets](widget-principle.md) — a sample ← `w1`.\n")
    done = _run(RECORD, env, "provenance", "--check", "--root", c.memory,
                "--project-dir", tmp_path / "acme-widgets")
    assert done.returncode == 0, done.stdout + done.stderr


def test_case_cited_by_regularity(tmp_path, env):
    c = Corpus(tmp_path)
    c.case()
    c.regularity(induced=("c1",))
    done = c.verify(env)
    assert done.returncode == 0, done.stdout + done.stderr


def _promote(env, root: Path, *args):
    return _run(RECORD, env, "promote", "--root", root, "--generality", "1",
                "--date", DATE, "--statement", "Run a check where its subject lives.", *args)


def test_promote_writes_regularity(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp(into=())
    done = _promote(env, c.memory, "--slug", "r1", "--name", "A regularity",
                    "--description", "a promoted regularity", "--induced-from", "e1")
    assert done.returncode == 0, done.stdout + done.stderr
    text = (c.leaves / "regularities" / "r1.md").read_text(encoding="utf-8")
    assert "schema: regularity/v1" in text and "induced_from: [e1]" in text
    assert "## Regularity" in text
    assert not (c.leaves / "principles" / "r1.md").exists()
    assert c.verify(env).returncode == 0


def test_promote_principle_requires_grounded_in(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    args = ("--principle", "--slug", "p1", "--name", "A principle",
            "--description", "a promoted principle")
    refused = _promote(env, c.memory, *args)
    assert refused.returncode != 0
    assert not (c.leaves / "principles" / "p1.md").exists()
    done = _promote(env, c.memory, *args, "--grounded-in", "r1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert c.verify(env).returncode == 0


def test_promote_principle_writes_grounded_skeleton(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    c.regularity()
    done = _promote(env, c.memory, "--principle", "--slug", "p1", "--name", "A principle",
                    "--description", "a promoted principle", "--grounded-in", "r1")
    assert done.returncode == 0, done.stdout + done.stderr
    text = (c.leaves / "principles" / "p1.md").read_text(encoding="utf-8")
    assert "schema: principle/v1" in text and "grounded_in: [regularity:r1]" in text
    assert "induced_from" not in text
    for heading in ("## Principle", "## Goal class", "## Grounded in", "## Refutation"):
        assert heading in text


def test_promote_scan_targets_regularity(tmp_path, env):
    memory = tmp_path / "acme-widgets" / ".claude" / "agent-memory"
    contexts = "".join(f"\n### 2026-01-{i:02d} — ctx\n- Where it arose: t\n- Working plan: p\n"
                       for i in (1, 2, 3))
    _write(memory / "experience" / "2026-01-01-sample.md", _fm({
        "name": "2026-01-01-sample", "description": "a sample", "type": "reference",
        "schema": "difficulty/v1", "resolution_confirmed_by_user": '"yes"',
    }) + "\n# Sample\n\n## Difficulty\n\nA check runs where its subject is not.\n\n"
         "## Order & criterion\n\no\n\n## Contexts\n" + contexts)
    done = _run(RECORD, env, "promote-scan", "--scope", "project", "--threshold", "3",
                "--project-dir", tmp_path / "acme-widgets")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "regularity/v1" in done.stdout
    assert "principle/v1" not in done.stdout


def test_search_tier_regularities(tmp_path, env):
    module = _load_record(env)
    root = module.search_root("global", None, "regularities")
    assert root == SCRIPTS_DIR.parent / "memory-global/leaves/regularities"
    assert module.TIER_SECTION["regularities"] == "Regularity"
    assert module.TIER_SECTION["principles"] == "Principle"


def test_link_targets_regularity(tmp_path, env):
    c = Corpus(tmp_path).good()
    leaf = c.exp("e2", into=())
    done = _run(RECORD, env, "link", "e2", "r1", "--root", c.memory)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "generalized_into: [r1]" in leaf.read_text(encoding="utf-8")
    assert "induced_from: [e1, e2]" in (c.leaves / "regularities" / "r1.md").read_text(encoding="utf-8")
    assert c.verify(env).returncode == 0


def test_unlink_targets_regularity(tmp_path, env):
    c = Corpus(tmp_path)
    c.exp()
    leaf = c.exp("e2")
    c.regularity(induced=("e1", "e2"))
    c.principle()
    assert c.verify(env).returncode == 0
    done = _run(RECORD, env, "unlink", "e2", "r1", "--root", c.memory)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "generalized_into: []" in leaf.read_text(encoding="utf-8")
    assert "induced_from: [e1]" in (c.leaves / "regularities" / "r1.md").read_text(encoding="utf-8")
    assert c.verify(env).returncode == 0


def test_leaf_structure_checks_regularity(tmp_path, env):
    c = Corpus(tmp_path)
    complete = c.regularity()
    assert _run(VERIFY_LEAF, env, complete).returncode == 0
    text = complete.read_text(encoding="utf-8")
    broken = _write(c.leaves / "regularities" / "r2.md",
                    text.replace("## Refutation\n\nA counterexample.\n\n", ""))
    done = _run(VERIFY_LEAF, env, broken)
    assert done.returncode != 0
    assert "Refutation" in done.stdout + done.stderr
