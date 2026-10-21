"""Tests for the experience <-> principle provenance link (lib/principle_provenance.py).

One test per rule the library decides: each builds a small two-tier memory under
tmp_path (a global `leaves/` tree and, where the rule is cross-tier, a project memory
dir holding `experience/` and `principles/`) and asserts on the finding kind or the
bytes a writer leaves behind. The writers run in-process through `main(argv)`, so
replacing one library function changes what the command does. Nothing outside
tmp_path is read or written.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import principle_provenance as pp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "memory_split"
DATE = "2026-10-21"
BODY = "\n# A sample difficulty\n\n## Difficulty\n\nA check runs where its subject is not.\n"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RE = _load("record_experience_provenance", "record-experience.py")
VPP = _load("verify_principle_provenance", "verify-principle-provenance.py")
VEL = _load("verify_experience_leaf_provenance", "verify-experience-leaf.py")
VMI = _load("verify_memory_index_provenance", "verify-memory-index.py")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-ledger.jsonl"))
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", "0")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    monkeypatch.setenv(RE.worktree_route.GUARD_ENV, "1")


def _run(module, argv) -> int:
    try:
        return module.main([str(a) for a in argv])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _fm(fields: dict) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


class World:
    """A memory root `<tmp>/mem` (leaves under `mem/leaves`) and a project memory dir."""

    def __init__(self, tmp: Path) -> None:
        self.mem = tmp / "mem"
        self.leaves = self.mem / "leaves"
        self.project = tmp / "acme-widgets" / ".claude" / "agent-memory"
        for d in (self.leaves / "experience", self.leaves / "principles" / "cases",
                  self.project / "experience", self.project / "principles"):
            d.mkdir(parents=True, exist_ok=True)

    def _base(self, tier: str) -> Path:
        return self.project if tier == "project" else self.leaves

    def exp(self, slug, status=(), tier="global", body=BODY) -> Path:
        path = self._base(tier) / "experience" / f"{slug}.md"
        path.write_text(_fm({
            "name": slug, "description": "a sample leaf", "type": "reference",
            "schema": "difficulty/v1",
            pp.FIELD: None if status is None else pp.format_list(status),
            "resolution_confirmed_by_user": '"yes"', "created": DATE, "last_verified": DATE,
        }) + body, encoding="utf-8")
        return path

    def rule(self, slug) -> Path:
        path = self.leaves / f"{slug}.md"
        path.write_text(_fm({"name": slug, "description": "a rule leaf", "type": "feedback"})
                        + "\n# A rule\n", encoding="utf-8")
        return path

    def principle(self, slug, induced=(), tier="global", rule_derived=None,
                  pointer=None) -> Path:
        base = self._base(tier) / "principles"
        path = base / f"{slug}.md"
        path.write_text(_fm({
            "name": slug, "description": "a sample principle", "type": "reference",
            "schema": "principle/v1", "generality": 1, pp.INDUCED: pp.format_list(induced),
            pp.RULE_DERIVED: json.dumps(rule_derived) if rule_derived else None,
            "created": DATE, "last_verified": DATE,
        }) + "\n# A principle\n\n## Principle\n\nRun a check where its subject lives.\n",
            encoding="utf-8")
        listed = induced if pointer is None else pointer
        index = base / pp.INDEX
        text = index.read_text(encoding="utf-8") if index.exists() else (
            "# Principles\n\n## Level 1 — sibling-context rules\n\n")
        index.write_text(text + f"- [A principle]({slug}.md) — a sample principle "
                         + pp.ARROW + ", ".join(f"`{s}`" for s in listed) + ".\n",
                         encoding="utf-8")
        return path

    def case(self, slug, status=(), digest=None, origin=pp.ORIGIN_TIER,
             sections=pp.CASE_SECTIONS, extra="") -> Path:
        path = self.leaves / pp.CASES_DIR / f"{slug}.md"
        body = f"\n# Case: {slug}\n" + "".join(
            f"\n## {name}\n\nThe {name.lower()} of the case.{extra}\n" for name in sections)
        path.write_text(_fm({
            "name": slug, "description": "a sample case", "type": "reference",
            "schema": pp.CASE_SCHEMA, pp.ORIGIN_TIER_FIELD: origin,
            pp.SOURCE_DIGEST: digest or (pp.DIGEST_PREFIX + "0" * 64),
            pp.FIELD: pp.format_list(status), "created": DATE, "last_verified": DATE,
        }) + body, encoding="utf-8")
        return path

    def corpus(self, project=False) -> pp.Corpus:
        return pp.load_corpus(self.leaves, self.project if project else None)

    def findings(self, project=False) -> set[tuple[str, Path]]:
        return {(f.kind, f.path) for f in pp.unaccounted(self.corpus(project))}

    def kinds(self, project=False) -> set[str]:
        return {kind for kind, _ in self.findings(project)}

    def cli(self, *argv) -> int:
        return _run(RE, [*argv, "--root", self.mem])


def status_of(path: Path):
    return pp.read_status(path.read_text(encoding="utf-8"))


def induced_of(path: Path):
    return pp.read_induced(path.read_text(encoding="utf-8"))


def body_sha(text: str) -> str:
    return pp.DIGEST_PREFIX + hashlib.sha256(text.split("\n---\n", 1)[1].encode()).hexdigest()


def _cross_tier_world(tmp_path, digest_matches=True):
    """A project leaf citing a case that a global principle lists (both directions)."""
    w = World(tmp_path)
    leaf = w.exp("2026-03-01-widget-drift", status=["sample-case"], tier="project")
    digest = body_sha(leaf.read_text(encoding="utf-8")) if digest_matches else None
    w.case("sample-case", status=["sample-principle"], digest=digest)
    w.principle("sample-principle", induced=["sample-case"])
    return w, leaf


# --------------------------------------------------------------------------
# status field
# --------------------------------------------------------------------------
def test_missing_status_reported(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=None)
    assert (pp.MISSING_STATUS, leaf) in w.findings()


def test_project_leaf_without_status_reported(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=None, tier="project")
    assert (pp.MISSING_STATUS, leaf) in w.findings(project=True)


def test_empty_status_accepted(tmp_path):
    w = World(tmp_path)
    w.exp("2026-01-01-sample", status=[])
    assert pp.unaccounted(w.corpus()) == []


def test_unknown_principle_reported(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=["no-such-principle"])
    assert (pp.UNKNOWN_PRINCIPLE, leaf) in w.findings()


def test_experience_validator_requires_status(tmp_path):
    w = World(tmp_path)
    without = w.exp("2026-01-01-without", status=None).read_text(encoding="utf-8")
    with_field = w.exp("2026-01-01-with", status=[]).read_text(encoding="utf-8")
    error = VEL.check_content(without)
    assert error is not None and pp.FIELD in error
    assert pp.FIELD not in (VEL.check_content(with_field) or "")


# --------------------------------------------------------------------------
# symmetry
# --------------------------------------------------------------------------
def test_asymmetry_reported_both_ways(tmp_path):
    w = World(tmp_path)
    w.exp("2026-01-01-listed", status=["sample-principle"])
    claims = w.exp("2026-01-02-claims", status=["sample-principle"])
    w.exp("2026-01-03-silent", status=[])
    principle = w.principle("sample-principle", induced=["2026-01-01-listed", "2026-01-03-silent"])
    found = w.findings()
    assert (pp.ASYMMETRIC, claims) in found
    assert (pp.ASYMMETRIC, principle) in found


def test_case_link_symmetric(tmp_path):
    w = World(tmp_path)
    w.case("sample-case", status=[])
    w.exp("2026-01-01-sample", status=["sample-principle"])
    principle = w.principle("sample-principle", induced=["2026-01-01-sample", "sample-case"])
    assert (pp.ASYMMETRIC, principle) in w.findings()


def test_project_principle_symmetric(tmp_path):
    w = World(tmp_path)
    w.exp("2026-03-01-widget-listed", status=["widget-principle"], tier="project")
    claims = w.exp("2026-03-02-widget-claims", status=["widget-principle"], tier="project")
    w.principle("widget-principle", induced=["2026-03-01-widget-listed"], tier="project")
    assert (pp.ASYMMETRIC, claims) in w.findings(project=True)


# --------------------------------------------------------------------------
# sources and the rule_derived mark
# --------------------------------------------------------------------------
def test_rule_only_principle_needs_mark(tmp_path):
    w = World(tmp_path)
    w.rule("sample-rule")
    principle = w.principle("sample-principle", induced=["sample-rule"])
    assert (pp.NO_SOURCE, principle) in w.findings()


def test_case_counts_as_experience_source(tmp_path):
    w = World(tmp_path)
    w.case("sample-case", status=["sample-principle"])
    w.principle("sample-principle", induced=["sample-case"])
    assert pp.NO_SOURCE not in w.kinds()
    assert pp.unaccounted(w.corpus()) == []


def test_stale_rule_mark_reported(tmp_path):
    w = World(tmp_path)
    w.exp("2026-01-01-sample", status=["sample-principle"])
    principle = w.principle("sample-principle", induced=["2026-01-01-sample"],
                            rule_derived="written from rules")
    assert (pp.STALE_MARK, principle) in w.findings()


def test_project_principle_needs_source(tmp_path):
    w = World(tmp_path)
    principle = w.principle("widget-principle", induced=[], tier="project")
    assert (pp.NO_SOURCE, principle) in w.findings(project=True)


def test_dangling_source_reported(tmp_path):
    w = World(tmp_path)
    principle = w.principle("sample-principle", induced=["2026-01-01-gone"],
                            rule_derived="written from rules")
    assert (pp.DANGLING, principle) in w.findings()


def test_index_mirror_checked(tmp_path):
    w = World(tmp_path)
    w.exp("2026-01-01-sample", status=["sample-principle"])
    principle = w.principle("sample-principle", induced=["2026-01-01-sample"],
                            pointer=["2026-01-01-other"])
    assert (pp.INDEX_MISMATCH, principle) in w.findings()


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------
def test_case_sections_required(tmp_path):
    w = World(tmp_path)
    case = w.case("sample-case", status=["sample-principle"], sections=pp.CASE_SECTIONS[:-1])
    w.principle("sample-principle", induced=["sample-case"])
    assert (pp.CASE_SECTIONS_KIND, case) in w.findings()


def test_case_digest_format_checked(tmp_path):
    w = World(tmp_path)
    case = w.case("sample-case", status=["sample-principle"], digest=pp.DIGEST_PREFIX + "xyz")
    w.principle("sample-principle", induced=["sample-case"])
    assert (pp.CASE_DIGEST_FORMAT, case) in w.findings()


def test_case_origin_tier_checked(tmp_path):
    w = World(tmp_path)
    case = w.case("sample-case", status=["sample-principle"], origin="global")
    w.principle("sample-principle", induced=["sample-case"])
    assert (pp.CASE_ORIGIN_TIER, case) in w.findings()


def test_uncited_case_reported(tmp_path):
    w = World(tmp_path)
    case = w.case("sample-case", status=[])
    assert (pp.UNCITED_CASE, case) in w.findings()


def test_core_check_without_project_memory(tmp_path):
    w = World(tmp_path)
    w.case("sample-case", status=["sample-principle"])
    w.principle("sample-principle", induced=["sample-case"])
    assert pp.unaccounted(w.corpus(project=False)) == []


def test_project_direct_global_reported(tmp_path):
    w = World(tmp_path)
    w.exp("2026-01-01-sample", status=["sample-principle"])
    w.principle("sample-principle", induced=["2026-01-01-sample"])
    leaf = w.exp("2026-03-01-widget-drift", status=["sample-principle"], tier="project")
    assert (pp.PROJECT_DIRECT_GLOBAL, leaf) in w.findings(project=True)


def test_project_case_link_one_way(tmp_path):
    w, _ = _cross_tier_world(tmp_path)
    assert pp.unaccounted(w.corpus(project=True)) == []


def test_stale_case_reported(tmp_path):
    w, _ = _cross_tier_world(tmp_path, digest_matches=False)
    assert (pp.STALE_CASE, w.leaves / pp.CASES_DIR / "sample-case.md") in w.findings(project=True)


def test_orphan_case_reported(tmp_path):
    w = World(tmp_path)
    case = w.case("sample-case", status=["sample-principle"])
    w.principle("sample-principle", induced=["sample-case"])
    assert (pp.ORPHAN_CASE, case) in w.findings(project=True)


def test_case_leak_reported(tmp_path):
    w, _ = _cross_tier_world(tmp_path)
    case = w.leaves / pp.CASES_DIR / "sample-case.md"
    case.write_text(case.read_text(encoding="utf-8") + "\nSeen in 2026-03-01-widget-drift.\n",
                    encoding="utf-8")
    assert (pp.CASE_LEAK, case) in w.findings(project=True)


def test_digest_ignores_link_fields():
    text = _fm({"name": "x", pp.FIELD: "[]"}) + BODY
    relinked = pp.set_field(text, pp.FIELD, pp.format_list(["sample-case"]))
    assert pp.body_digest(text) == pp.body_digest(relinked)
    assert pp.body_digest(text) == pp.DIGEST_PREFIX + hashlib.sha256(BODY.encode()).hexdigest()


def _case_spec(w: World, leaf: Path, **overrides) -> Path:
    spec = {"slug": "sample-case", "description": "a sanitized case", "source": str(leaf),
            "into": ["sample-principle"], "aspect": "Where a check runs.",
            "expected": "The check runs beside its subject.",
            "actual": "The check ran in another checkout.",
            "mismatch": "The verdict judged the wrong bytes."}
    spec.update(overrides)
    path = w.mem / "spec.json"
    path.write_text(json.dumps([spec]), encoding="utf-8")
    return path


def _case_add_world(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-03-01-widget-drift", status=[], tier="project")
    w.exp("2026-01-01-sample", status=["sample-principle"])
    w.principle("sample-principle", induced=["2026-01-01-sample"])
    return w, leaf


def test_case_add_refuses_missing_mismatch(tmp_path):
    w, leaf = _case_add_world(tmp_path)
    assert w.cli("case-add", "--spec", _case_spec(w, leaf, mismatch=" ")) == 2
    assert not (w.leaves / pp.CASES_DIR / "sample-case.md").exists()


def test_case_add_refuses_leak(tmp_path):
    w, leaf = _case_add_world(tmp_path)
    spec = _case_spec(w, leaf, aspect="As in 2026-03-01-widget-drift.")
    assert w.cli("case-add", "--spec", spec) == 2
    assert not (w.leaves / pp.CASES_DIR / "sample-case.md").exists()


def test_case_add_writes_body_digest(tmp_path):
    w, leaf = _case_add_world(tmp_path)
    leaf_bytes = leaf.read_bytes()
    assert w.cli("case-add", "--spec", _case_spec(w, leaf)) == 0
    case = (w.leaves / pp.CASES_DIR / "sample-case.md").read_text(encoding="utf-8")
    assert pp.field_value(case, pp.SOURCE_DIGEST) == body_sha(leaf_bytes.decode())
    assert leaf.read_bytes() == leaf_bytes
    assert "sample-case" in induced_of(w.leaves / "principles" / "sample-principle.md")


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
def test_experiences_of_both_tiers(tmp_path):
    w, leaf = _cross_tier_world(tmp_path)
    corpus = w.corpus(project=True)
    found = pp.experiences_of(corpus.find("sample-principle"), corpus)
    assert {(l.tier, l.path) for l in found} == {
        ("global", w.leaves / pp.CASES_DIR / "sample-case.md"), ("project", leaf)}


def test_provenance_cli_reports(tmp_path, capsys):
    w, leaf = _cross_tier_world(tmp_path)
    assert w.cli("provenance", "--project-dir", w.project, "--of", "sample-principle") == 0
    assert f"project\t{leaf.stem}\t{leaf}" in capsys.readouterr().out.splitlines()
    missing = w.exp("2026-01-01-sample", status=None)
    assert w.cli("provenance", "--project-dir", w.project, "--check") == 1
    assert any(line.startswith(f"{pp.MISSING_STATUS}\t{missing}")
               for line in capsys.readouterr().out.splitlines())


# --------------------------------------------------------------------------
# link / unlink
# --------------------------------------------------------------------------
def test_link_writes_both_sides(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-02-new", status=[])
    w.exp("2026-01-01-sample", status=["sample-principle"])
    principle = w.principle("sample-principle", induced=["2026-01-01-sample"])
    assert w.cli("link", "2026-01-02-new", "sample-principle") == 0
    assert status_of(leaf) == ["sample-principle"]
    assert induced_of(principle) == ["2026-01-01-sample", "2026-01-02-new"]
    assert pp.unaccounted(w.corpus()) == []


def test_link_project_principle_both_sides(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-03-01-widget-drift", status=[], tier="project")
    principle = w.principle("widget-principle", induced=[], tier="project",
                            rule_derived="seeded from rules")
    assert w.cli("link", leaf.stem, "widget-principle", "--project-dir", w.project) == 0
    assert status_of(leaf) == ["widget-principle"]
    assert induced_of(principle) == [leaf.stem]


def test_link_project_case_leaf_only(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-03-01-widget-drift", status=[], tier="project")
    case = w.case("sample-case", status=["sample-principle"])
    w.principle("sample-principle", induced=["sample-case"])
    case_bytes = case.read_bytes()
    assert w.cli("link", leaf.stem, "sample-case", "--project-dir", w.project) == 0
    assert status_of(leaf) == ["sample-case"]
    assert case.read_bytes() == case_bytes


def test_link_clears_rule_mark(tmp_path):
    w = World(tmp_path)
    w.rule("sample-rule")
    w.exp("2026-01-01-sample", status=[])
    principle = w.principle("sample-principle", induced=["sample-rule"],
                            rule_derived="written from rules")
    assert w.cli("link", "2026-01-01-sample", "sample-principle") == 0
    assert pp.field_value(principle.read_text(encoding="utf-8"), pp.RULE_DERIVED) is None
    assert pp.unaccounted(w.corpus()) == []


def test_link_atomic(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=[])
    principle = w.principle("sample-principle", induced=["sample-rule"],
                            rule_derived="written from rules")
    w.rule("sample-rule")
    leaf_bytes = leaf.read_bytes()
    principles = principle.parent
    modes = {p: p.stat().st_mode for p in (principles, principle, principles / pp.INDEX)}
    for p in modes:
        p.chmod(0o555 if p.is_dir() else 0o444)
    try:
        with pytest.raises((SystemExit, OSError)):
            RE.main(["link", "2026-01-01-sample", "sample-principle", "--root", str(w.mem)])
    finally:
        for p, mode in modes.items():
            p.chmod(stat.S_IMODE(mode))
    assert leaf.read_bytes() == leaf_bytes


def test_unlink_symmetric(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=["sample-principle"])
    w.exp("2026-01-02-kept", status=["sample-principle"])
    principle = w.principle("sample-principle", induced=["2026-01-01-sample", "2026-01-02-kept"])
    assert w.cli("unlink", "2026-01-01-sample", "sample-principle") == 0
    assert status_of(leaf) == []
    assert induced_of(principle) == ["2026-01-02-kept"]
    assert pp.unaccounted(w.corpus()) == []


# --------------------------------------------------------------------------
# birth, promote, split
# --------------------------------------------------------------------------
def test_new_leaf_born_unlinked(tmp_path):
    project = tmp_path / "proj"
    assert _run(RE, ["new", "--scope", "project", "--project-dir", project, "--date", DATE,
                     "--slug", "born", "--title", "Born", "--description", "a new leaf",
                     "--confirmed-by", "yes", "--difficulty", "d", "--order", "o",
                     "--criterion", "c", "--context-where", "w", "--plan", "p"]) == 0
    leaf = project / ".claude/agent-memory/experience" / f"{DATE}-born.md"
    assert status_of(leaf) == []


def _promote(w: World, *extra) -> int:
    return w.cli("promote", "--slug", "new-principle", "--name", "A new principle",
                 "--description", "a promoted principle", "--generality", "1",
                 "--statement", "Run a check where its subject lives.", "--date", DATE, *extra)


def test_promote_writes_back_pointers(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=[])
    assert _promote(w, "--induced-from", "2026-01-01-sample") == 0
    assert status_of(leaf) == ["new-principle"]
    assert pp.unaccounted(w.corpus()) == []


def test_promote_project_principle(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-03-01-widget-drift", status=[], tier="project")
    assert _promote(w, "--induced-from", leaf.stem, "--project-dir", w.project) == 0
    assert induced_of(w.project / "principles" / "new-principle.md") == [leaf.stem]
    assert status_of(leaf) == ["new-principle"]
    assert not (w.leaves / "principles" / "new-principle.md").exists()


def test_promote_requires_experience_or_mark(tmp_path):
    w = World(tmp_path)
    w.rule("sample-rule")
    assert _promote(w, "--induced-from", "sample-rule") == 2
    assert not (w.leaves / "principles" / "new-principle.md").exists()


def _split_world(tmp_path):
    """The split fixture's source with a principle the drift cluster names as existing."""
    root = tmp_path / "work"
    (root / "leaves/experience").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "good/base.md", root / "leaves/experience/2026-01-05-sample-venue-difficulty.md")
    shutil.copyfile(FIXTURES / "good/base.md", root / "base.md")
    (root / "leaves/experience/MEMORY.md").write_text("# Experience\n\n## 2026-01\n\n",
                                                      encoding="utf-8")
    w = World(tmp_path)
    w.leaves = root / "leaves"
    (w.leaves / "principles").mkdir()
    w.rule("sample-rule")
    principle = w.principle("venue-principle", induced=["sample-rule"],
                            rule_derived="written from rules")
    manifest = json.loads((FIXTURES / "good.json").read_text(encoding="utf-8"))
    manifest.update(root=str(root), principles=[])
    manifest["clusters"][0]["existing_principle"] = "venue-principle"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _run(RE, ["split-apply", "--manifest", path, "--date", DATE]) == 0
    return root, principle, manifest


def test_split_apply_links_cluster_principle(tmp_path):
    root, principle, manifest = _split_world(tmp_path)
    drift = manifest["clusters"][0]["slug"]
    assert status_of(root / "leaves/experience" / f"{drift}.md") == ["venue-principle"]
    assert drift in induced_of(principle)


def test_split_leaf_born_unlinked(tmp_path):
    root, _, manifest = _split_world(tmp_path)
    hang = manifest["clusters"][1]["slug"]
    assert status_of(root / "leaves/experience" / f"{hang}.md") == []


# --------------------------------------------------------------------------
# index exemption, backfill, verifier
# --------------------------------------------------------------------------
def test_case_files_exempt_from_index(tmp_path):
    root = tmp_path / "repo"
    w = World(tmp_path)
    w.leaves = root / "memory-global/leaves"
    (w.leaves / pp.CASES_DIR).mkdir(parents=True)
    w.case("sample-case", status=["sample-principle"])
    w.principle("sample-principle", induced=["sample-case"])
    (root / "memory-global/MEMORY.md").write_text(
        "# Global\n\n- [Principles](leaves/principles/MEMORY.md) — index\n", encoding="utf-8")
    assert _run(VMI, ["--root", root]) == 0


def test_backfill_mirrors_links(tmp_path):
    w = World(tmp_path)
    linked = w.exp("2026-01-01-sample", status=None)
    unlinked = w.exp("2026-01-02-other", status=None)
    w.principle("sample-principle", induced=["2026-01-01-sample"])
    assert w.cli("backfill") == 0
    assert status_of(linked) == ["sample-principle"]
    assert status_of(unlinked) == []
    assert pp.unaccounted(w.corpus()) == []


def test_backfill_idempotent(tmp_path):
    w = World(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=None)
    w.principle("sample-principle", induced=["2026-01-01-sample"])
    assert w.cli("backfill") == 0
    once = leaf.read_bytes()
    assert w.cli("backfill") == 0
    assert leaf.read_bytes() == once
    assert status_of(leaf) == ["sample-principle"]


def _verifier_world(tmp_path):
    root = tmp_path / "repo"
    w = World(tmp_path)
    w.leaves = root / pp.LEAVES_REL
    (w.leaves / "experience").mkdir(parents=True)
    (w.leaves / "principles").mkdir(parents=True)
    return root, w


def test_verifier_reports_unaccounted(tmp_path, capsys):
    root, w = _verifier_world(tmp_path)
    w.exp("2026-01-01-sample", status=[])
    assert _run(VPP, ["--root", root]) == 0
    w.exp("2026-01-02-missing", status=None)
    assert _run(VPP, ["--root", root]) == 1
    assert pp.MISSING_STATUS in capsys.readouterr().out


def _stage(tmp_path, root, *paths):
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", *map(str, paths)], check=True, env=env)


def test_verifier_staged_scans_corpus(tmp_path):
    root, w = _verifier_world(tmp_path)
    broken = w.exp("2026-01-02-missing", status=None)
    staged = w.exp("2026-01-01-sample", status=[])
    _stage(tmp_path, root, staged, broken)
    assert _run(VPP, ["--root", root, "--staged"]) == 1


def test_verifier_staged_ignores_untracked_leaf(tmp_path):
    root, w = _verifier_world(tmp_path)
    w.exp("2026-01-02-untracked", status=None)
    staged = w.exp("2026-01-01-sample", status=[])
    _stage(tmp_path, root, staged)
    assert _run(VPP, ["--root", root, "--staged"]) == 0


def test_verifier_staged_reads_the_index_not_the_working_tree(tmp_path):
    root, w = _verifier_world(tmp_path)
    leaf = w.exp("2026-01-01-sample", status=[])
    principle = w.principle("sample-principle", induced=["2026-01-01-sample"])
    _stage(tmp_path, root, leaf, principle)
    w.exp("2026-01-01-sample", status=["sample-principle"])
    assert _run(VPP, ["--root", root]) == 0
    assert _run(VPP, ["--root", root, "--staged"]) == 1
