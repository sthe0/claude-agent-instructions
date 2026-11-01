"""Tests for the memory seams of the grounding judge.

The judge is wired into the memory scripts at four seams: `record-experience.py promote` (and
`restamp`) judge a principle's link before writing it, `verify-principle-provenance.py` and
`record-experience.py provenance --check` read the stamps that judgement leaves behind, the
verifier's `--judge` asks the judge about every link now, and the staged trigger covers every path
under the leaves tree. Each test builds a small memory under tmp_path, drives a seam in-process
with a fixture runner (no model is called) and pins one case token: its name ends in the token, so
a mutation of that rule turns the test red by assertion on the bytes written, the exit code or the
finding kind.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.dispatch import RunResult  # noqa: E402
from lib import principle_provenance as pp  # noqa: E402

DATE = "2026-10-21"
STALE = "2020-01-01"
TODAY = date.fromisoformat(DATE)
OVERRIDE = "accepted: the ground is a sketch"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RE = _load("record_experience_seams", "record-experience.py")
VPP = _load("verify_principle_provenance_seams", "verify-principle-provenance.py")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-ledger.jsonl"))
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", "0")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    monkeypatch.setenv(RE.worktree_route.GUARD_ENV, "1")
    monkeypatch.setattr(pp, "judge_cache_dir", lambda: tmp_path / "cache")


def reply(*defects, reason="because"):
    return json.dumps({"defects": list(defects), "reason": reason})


class Runner:
    """A fixture judge: answers every prompt with `answer`."""

    def __init__(self, answer=None):
        self.answer = reply() if answer is None else answer
        self.calls = 0

    def __call__(self, argv, *, timeout, stdin):
        self.calls += 1
        return RunResult(0, stdout=self.answer, timed_out=False)


def judge_says(monkeypatch, *defects) -> Runner:
    runner = Runner(reply(*defects))
    monkeypatch.setattr(pp, "live_runner", lambda: runner)
    return runner


def run(module, argv) -> int:
    try:
        return module.main([str(a) for a in argv])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def fm(fields: dict) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


class Mem:
    """`<repo>/memory-global/leaves` and a project memory `<repo>/.claude/agent-memory`."""

    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "repo"
        self.memory = self.repo / "memory-global"
        self.leaves = self.memory / "leaves"
        self.project = self.repo / ".claude" / "agent-memory"
        for base in (self.leaves, self.project):
            for area in ("experience", "regularities", "principles"):
                (base / area).mkdir(parents=True, exist_ok=True)
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "vendor.md").write_text("The vendor limit is 10 rps.\n", encoding="utf-8")

    def _write(self, base: Path, area: str, slug: str, fields: dict, body: str, pointer) -> Path:
        path = base / area / f"{slug}.md"
        path.write_text(fm(fields) + body, encoding="utf-8")
        index = base / area / pp.INDEX
        head = index.read_text(encoding="utf-8") if index.exists() else f"# {area}\n\n"
        index.write_text(head + f"- [A leaf]({slug}.md) — a sample {pp.ARROW}"
                         + ", ".join(f"`{s}`" for s in pointer) + ".\n", encoding="utf-8")
        return path

    def exp(self, base=None, slug="e1", into=("r1",)) -> Path:
        base = base or self.leaves
        path = base / "experience" / f"{slug}.md"
        path.write_text(fm({
            "name": slug, "description": "a sample experience", "type": "reference",
            "schema": "difficulty/v1", pp.FIELD: pp.format_list(into),
            "resolution_confirmed_by_user": '"yes"', "created": DATE, "last_verified": DATE,
        }) + "\n# Sample\n\n## Difficulty\n\nA check runs where its subject is not.\n", encoding="utf-8")
        return path

    def regularity(self, base=None, slug="r1", induced=("e1",)) -> Path:
        return self._write(base or self.leaves, "regularities", slug, {
            "name": slug, "description": "a sample regularity", "type": "reference",
            "schema": "regularity/v1", "generality": 1, pp.INDUCED: pp.format_list(induced),
            "created": DATE, "last_verified": DATE,
        }, "\n# Sample\n\n## Regularity\n\nRun a check where its subject lives.\n\n"
           "## Generality\n\nLevel 1.\n\n## Refutation\n\nA counterexample.\n\n"
           "## Induced from\n\n- [[e1]]\n", induced)

    def given(self, base=None, slug="g1", verified=STALE, order_derived=False) -> Path:
        fields = {"name": slug, "description": "a given regularity", "type": "reference",
                  "schema": "regularity/v1", "generality": 1, "created": DATE, "last_verified": verified}
        if order_derived:
            fields[pp.ORDER_DERIVED] = '"the user ordered it"'
        else:
            fields.update({"given": "doc", pp.GIVEN_SOURCE: "docs/vendor.md"})
        return self._write(base or self.leaves, "regularities", slug, fields,
                           "\n# Given\n\n## Regularity\n\nThe vendor limit is 10 rps.\n\n"
                           "## Given\n\nThe vendor document says so.\n\n## Refutation\n\nA changed limit.\n",
                           ())

    def principle(self, base=None, slug="p1", grounded=("regularity:r1",), **extra) -> Path:
        return self._write(base or self.leaves, "principles", slug, {
            "name": slug, "description": "a sample principle", "type": "reference",
            "schema": "principle/v1", "generality": 1, pp.GROUNDED: pp.format_list(grounded),
            "created": DATE, "last_verified": DATE, **extra,
        }, "\n# Sample\n\n## Principle\n\nRun a check where its subject lives.\n\n"
           "## Goal class\n\nControls.\n\n## Refutation\n\nA counterexample.\n",
           [g.removeprefix("regularity:") for g in grounded])

    def good(self) -> "Mem":
        self.exp()
        self.regularity()
        return self

    def corpus(self, project=False) -> pp.Corpus:
        return pp.load_corpus(self.leaves, self.project if project else None)

    def digest(self, path: Path, project=False) -> str:
        corpus = self.corpus(project)
        leaf = next(l for l in corpus.items if l.path == path)
        return hashlib.sha256(pp._judge().leaf_link_inputs(leaf, corpus).encode("utf-8")).hexdigest()

    def stamp(self, path: Path, project=False) -> Path:
        path.write_text(pp.set_field(path.read_text(encoding="utf-8"), pp.JUDGED_FIELD,
                                     self.digest(path, project)), encoding="utf-8")
        return path

    def kinds(self, findings) -> set[tuple[str, Path]]:
        return {(f.kind, f.path) for f in findings}

    def promote(self, *extra, slug="p1", grounded="r1") -> int:
        return run(RE, ["promote", "--principle", "--slug", slug, "--name", "A principle",
                        "--description", "a promoted principle", "--grounded-in", grounded,
                        "--generality", "1", "--statement", "Run a check where its subject lives.",
                        "--date", DATE, "--root", self.memory, *extra])

    def verify(self, *extra) -> int:
        return run(VPP, ["--root", self.repo, *extra])


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# promote judges the link
# --------------------------------------------------------------------------
def test_promote_refuses_defective_link(tmp_path, monkeypatch):
    m = Mem(tmp_path).good()
    judge_says(monkeypatch, "insufficient")
    assert m.promote() == 2
    assert not (m.leaves / "principles" / "p1.md").exists()


def test_promote_override_written(tmp_path, monkeypatch):
    m = Mem(tmp_path).good()
    judge_says(monkeypatch, "insufficient")
    assert m.promote("--grounding-override", OVERRIDE) == 0
    text = text_of(m.leaves / "principles" / "p1.md")
    assert pp.field_value(text, pp.OVERRIDE_FIELD) == json.dumps(OVERRIDE)
    assert pp.field_value(text, pp.JUDGED_FIELD)


def test_promote_sound_link_stamped(tmp_path, monkeypatch):
    m = Mem(tmp_path).good()
    judge_says(monkeypatch)
    assert m.promote() == 0
    path = m.leaves / "principles" / "p1.md"
    text = text_of(path)
    assert pp.field_value(text, pp.JUDGED_FIELD) == m.digest(path)
    assert pp.field_value(text, pp.PENDING_FIELD) is None
    assert pp.field_value(text, pp.OVERRIDE_FIELD) is None


def test_promote_stale_given_refused(tmp_path, monkeypatch, capsys):
    m = Mem(tmp_path).good()
    m.given()
    runner = judge_says(monkeypatch)
    assert m.promote(grounded="g1") == 2
    assert not (m.leaves / "principles" / "p1.md").exists()
    assert "'g1'" in capsys.readouterr().err
    assert runner.calls == 0


def test_promote_stale_given_refused_judge_down(tmp_path, capsys):
    m = Mem(tmp_path).good()
    m.given()
    assert pp.live_runner() is None
    assert m.promote(grounded="g1") == 2
    assert not (m.leaves / "principles" / "p1.md").exists()
    err = capsys.readouterr().err
    assert "'g1'" in err and "--grounding-override" in err


def test_promote_judge_down_warns(tmp_path, capsys):
    m = Mem(tmp_path).good()
    assert pp.live_runner() is None
    assert m.promote() == 0
    assert "warning: grounding judge unavailable: " in capsys.readouterr().err


def test_promote_judge_down_marks_pending(tmp_path, capsys):
    m = Mem(tmp_path).good()
    assert m.promote() == 0
    text = text_of(m.leaves / "principles" / "p1.md")
    assert pp.field_value(text, pp.PENDING_FIELD) == f"{DATE} judge unavailable"
    assert pp.field_value(text, pp.JUDGED_FIELD) is None
    capsys.readouterr()
    assert m.verify() == 0
    out = capsys.readouterr().out
    assert f"warning: {pp.PENDING_JUDGEMENT}: " in out
    assert pp.UNJUDGED_LINK not in out


def test_restamp_writes_current_stamp(tmp_path, monkeypatch):
    m = Mem(tmp_path).good()
    path = m.principle(**{pp.PENDING_FIELD: f"{DATE} judge unavailable"})
    judge_says(monkeypatch)
    assert run(RE, ["restamp", "p1", "--root", m.memory]) == 0
    text = text_of(path)
    assert pp.field_value(text, pp.JUDGED_FIELD) == m.digest(path)
    assert pp.field_value(text, pp.PENDING_FIELD) is None
    assert pp.stamp_current(next(l for l in m.corpus().items if l.path == path), m.corpus())


# --------------------------------------------------------------------------
# the stamp is read by rule
# --------------------------------------------------------------------------
def test_stamp_mismatch_unjudged(tmp_path):
    m = Mem(tmp_path).good()
    path = m.principle(**{pp.JUDGED_FIELD: "0" * 64})
    assert (pp.UNJUDGED_LINK, path) in m.kinds(pp.provenance_check_findings(m.corpus()))


def test_stamp_match_judged(tmp_path):
    m = Mem(tmp_path).good()
    path = m.stamp(m.principle())
    kinds = {kind for kind, p in m.kinds(pp.provenance_check_findings(m.corpus())) if p == path}
    assert kinds == set()


def test_provenance_check_reports_unjudged(tmp_path, capsys):
    m = Mem(tmp_path).good()
    path = m.principle()
    assert (pp.UNJUDGED_LINK, path) in m.kinds(pp.verify_corpus(m.repo))
    assert m.verify() == 1
    assert f"{pp.UNJUDGED_LINK}: " in capsys.readouterr().out


def test_provenance_command_reports_unjudged(tmp_path, capsys):
    m = Mem(tmp_path).good()
    m.principle()
    assert run(RE, ["provenance", "--check", "--root", m.memory]) == 1
    assert capsys.readouterr().out.startswith(f"{pp.UNJUDGED_LINK}\t")


def test_project_principle_findings_apply(tmp_path):
    m = Mem(tmp_path)
    m.exp(m.project)
    m.regularity(m.project)
    path = m.principle(m.project, grounded=("regularity:r1",))
    assert (pp.UNJUDGED_LINK, path) in m.kinds(pp.verify_corpus(m.repo, m.project))


def test_retired_leaf_schema_flagged(tmp_path):
    m = Mem(tmp_path).good()
    leaf = m.leaves / "memory-usage.md"
    leaf.write_text(fm({"name": "memory-usage", "description": "an old leaf", "type": "feedback",
                        "schema": pp.RETIRED_SCHEMA}) + "\n# Old\n", encoding="utf-8")
    assert (pp.RETIRED_LEAF, leaf) in m.kinds(pp.verify_corpus(m.repo))
    assert m.verify() == 1


def test_retired_leaf_project_flagged(tmp_path):
    m = Mem(tmp_path).good()
    leaf = m.project / "local-rule.md"
    leaf.write_text(fm({"name": "local-rule", "description": "an old leaf", "type": "feedback",
                        "schema": pp.RETIRED_SCHEMA}) + "\n# Old\n", encoding="utf-8")
    assert (pp.RETIRED_LEAF, leaf) in m.kinds(pp.verify_corpus(m.repo, m.project))


# --------------------------------------------------------------------------
# --judge, outages and the age rule
# --------------------------------------------------------------------------
def test_live_judge_reports_defect(tmp_path, monkeypatch, capsys):
    m = Mem(tmp_path).good()
    path = m.stamp(m.principle())
    judge_says(monkeypatch, "insufficient")
    assert m.verify("--judge") == 1
    out = capsys.readouterr().out
    assert f"{pp.LINK_DEFECT}: " in out and "insufficient" in out
    found = pp.live_link_findings(m.corpus(), runner=pp.live_runner(), cache=tmp_path / "cache2")
    assert (pp.LINK_DEFECT, path) in m.kinds(found)


def test_override_clears_defect(tmp_path, monkeypatch):
    m = Mem(tmp_path).good()
    path = m.stamp(m.principle(**{pp.OVERRIDE_FIELD: json.dumps(OVERRIDE)}))
    judge_says(monkeypatch, "insufficient")
    found = pp.live_link_findings(m.corpus(), runner=pp.live_runner(), cache=tmp_path / "cache2")
    assert (pp.LINK_DEFECT, path) not in m.kinds(found)
    assert m.verify("--judge") == 0


def test_live_judge_down_warns(tmp_path, capsys):
    m = Mem(tmp_path).good()
    path = m.stamp(m.principle())
    assert pp.live_runner() is None
    found = pp.live_link_findings(m.corpus(), runner=None)
    assert [f.detail.startswith("grounding judge unavailable: ") for f in found] == [True]
    assert (pp.JUDGE_UNAVAILABLE, path) in m.kinds(found)
    assert m.verify("--judge") == 0
    assert f"warning: {pp.JUDGE_UNAVAILABLE}: " in capsys.readouterr().out


def test_stale_given_warned_apart(tmp_path, capsys):
    m = Mem(tmp_path).good()
    given = m.given()
    m.stamp(m.principle(grounded=("regularity:g1",)))
    found = pp.provenance_check_findings(m.corpus(), today=TODAY)
    assert (pp.STALE_GIVEN, given) in m.kinds(found)
    assert pp.STALE_GIVEN in pp.WARNING_KINDS
    assert "p1" in next(f.detail for f in found if f.kind == pp.STALE_GIVEN)
    assert m.verify() == 0
    assert f"warning: {pp.STALE_GIVEN}: " in capsys.readouterr().out


def test_order_derived_given_aged(tmp_path):
    m = Mem(tmp_path).good()
    ground = m.given(order_derived=True)
    m.stamp(m.principle(grounded=("regularity:g1",)))
    found = pp.stale_given_warnings(m.corpus(), today=TODAY)
    assert (pp.STALE_GIVEN, ground) in m.kinds(found)


# --------------------------------------------------------------------------
# the staged trigger
# --------------------------------------------------------------------------
def test_staged_trigger_any_leaves_path():
    assert pp.staged_triggers(["memory-global/leaves/memory-usage.md"])
    assert pp.triggers_staged_check("memory-global/leaves/system-knowledge/some-fact.md")
    assert not pp.staged_triggers(["scripts/lib/principle_provenance.py", "README.md"])
