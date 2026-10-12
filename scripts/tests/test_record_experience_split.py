"""Tests for the split flow of record-experience.py: split-contexts, split-apply,
promote and split-verify.

The curator (a model) writes a declarative manifest; the code executes it and
judges the result. These tests pin the judging side: `split-verify` must accept the
committed good fixture and reject the lossy one, and must fail on each *kind* of
wrong result (a lost, altered, duplicated or misplaced context), not just on any
non-zero input. Every test writes only under pytest's tmp_path; the committed
fixture tree is read, never modified.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "memory_split"
SCRIPT = SCRIPTS_DIR / "record-experience.py"
SOURCE = "leaves/experience/2026-01-05-sample-venue-difficulty.md"
DRIFT = "leaves/experience/2026-01-05-sample-venue-drift.md"
HANG = "leaves/experience/2026-02-15-sample-fetch-hang.md"
PRINCIPLE = "leaves/principles/sample-venue-principle.md"
DATE = "2026-10-12"
MISSING_LINE = re.compile(r"^missing: [0-9]+:[0-9a-f]{12}$")

PROMOTE_ARGS = [
    "--slug", "sample-venue-principle",
    "--name", "A control is run where its subject lives",
    "--description", "A sample principle for the split fixture — run a control in the "
                     "venue that holds the commits it judges",
    "--generality", "1",
    "--induced-from", "2026-01-05-sample-venue-drift",
    "--statement", "Execute a control in the checkout that holds the commits it judges, "
                   "so a green stage is not recorded as failed.",
    "--date", DATE,
]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


re_mod = _load("record_experience", "record-experience.py")
diagnose = _load("self_diagnose", "self-diagnose.py")


@pytest.fixture
def env(tmp_path):
    """Environment of a linked-worktree session: routing is not suppressed by
    env, so a canon refusal comes from the checkout's own shape."""
    base = {k: v for k, v in os.environ.items()
            if k not in (re_mod.worktree_route.DISABLE_ENV, re_mod.worktree_route.GUARD_ENV)}
    base["AGENTCTL_EDIT_LEDGER"] = str(tmp_path / "edit-ledger.jsonl")
    return base


def run(env, *args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=env,
                          cwd=cwd, capture_output=True, text=True, timeout=300)


def fixture_copy(tmp_path) -> Path:
    """The committed fixture tree, copied; manifests keep their relative roots."""
    dest = tmp_path / "memory_split"
    shutil.copytree(FIXTURES, dest)
    return dest


def fresh_work(tmp_path, name="work"):
    """A root holding the pre-split source and the seed indexes, plus a manifest
    (no principles listed yet) pointing at it; applying and promoting on it must
    regenerate the committed good tree."""
    root = tmp_path / name
    experience = root / "leaves/experience"
    experience.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "good/base.md", root / SOURCE)
    shutil.copyfile(FIXTURES / "good/base.md", root / "base.md")
    (experience / "MEMORY.md").write_text(
        "# Experience\n\nDifficulty-centric experience leaves of the split fixture. "
        "Most recent first.\n\n## 2026-01\n\n"
        "- [2026-01-05 — A sample difficulty used as a split fixture]"
        "(2026-01-05-sample-venue-difficulty.md) — a check runs where the commits are "
        "not and a fetch hangs the engine\n", encoding="utf-8")
    shutil.copyfile(FIXTURES / "good/MEMORY.md", root / "MEMORY.md")
    (root / "leaves/principles").mkdir()
    (root / "leaves/principles/MEMORY.md").write_text(
        "# Principles\n\nPrinciples of the split fixture, grouped by generality level.\n",
        encoding="utf-8")
    manifest = json.loads((FIXTURES / "good.json").read_text(encoding="utf-8"))
    manifest["root"] = str(root)
    manifest["principles"] = []
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root, path, manifest


def write_manifest(path: Path, manifest: dict) -> Path:
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def block_text(leaf: Path, ordinal: int) -> str:
    return re_mod.parse_contexts(leaf.read_text(encoding="utf-8")).blocks[ordinal - 1]


def git(repo: Path, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, check=True)


def primary_checkout(tmp_path) -> Path:
    repo = tmp_path / "canon"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


# --- split-contexts --------------------------------------------------------

def test_split_contexts_lists_ids_and_headings(env):
    done = run(env, "split-contexts", "--source", FIXTURES / "good/base.md")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert [l.split(" ", 1)[0] for l in lines] == [
        "1:5dbabc969e13", "2:66a81f81a879", "3:e51c0fc04bb0",
        "4:0372ce75d1b7", "5:403a7a175eaa"]
    assert "venue drift" in lines[1] and "venue drift" in lines[2]


def test_split_contexts_ignores_headings_inside_code_fences(env):
    done = run(env, "split-contexts", "--source", FIXTURES / "good/base.md")
    assert "not a context heading" not in done.stdout
    assert len(done.stdout.splitlines()) == 5


def test_split_contexts_agrees_with_measure_leaf(env):
    for leaf in (FIXTURES / "good/base.md", FIXTURES / "good" / SOURCE,
                 FIXTURES / "good" / HANG):
        text = leaf.read_text(encoding="utf-8")
        _, counted = diagnose.measure_leaf(text)
        assert len(re_mod.parse_contexts(text).blocks) == counted, leaf


def test_split_contexts_identical_headings_get_distinct_ids(env):
    ids = [l.split(" ", 1)[0] for l in
           run(env, "split-contexts", "--source", FIXTURES / "good/base.md").stdout.splitlines()]
    assert len(set(ids)) == len(ids)
    assert len({i.split(":")[0] for i in ids}) == len(ids)


def test_split_contexts_refuses_leaf_without_contexts_section(env, tmp_path):
    leaf = tmp_path / "plain.md"
    leaf.write_text("---\nname: x\n---\n\n# X\n\n## Cost\nnone\n", encoding="utf-8")
    done = run(env, "split-contexts", "--source", leaf)
    assert done.returncode == 2
    assert "Contexts" in done.stderr


# --- split-apply -----------------------------------------------------------

def test_split_apply_moves_each_context_to_its_cluster_leaf(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    before = block_text(root / SOURCE, 4)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    assert done.returncode == 0, done.stderr

    def shas(path):
        return [re_mod.block_sha(b)
                for b in re_mod.parse_contexts((root / path).read_text(encoding="utf-8")).blocks]

    wanted = lambda ids: [i.split(":")[1] for i in ids]
    assert shas(SOURCE) == wanted(manifest["keep"])
    assert shas(DRIFT) == wanted(manifest["clusters"][0]["contexts"])
    assert shas(HANG) == wanted(manifest["clusters"][1]["contexts"])
    assert before in (root / HANG).read_text(encoding="utf-8")


def test_split_apply_moves_blocks_byte_exact_including_fenced_headings(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    original = block_text(root / SOURCE, 4)
    assert "### not a context heading" in original
    run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    moved = (root / HANG).read_text(encoding="utf-8")
    assert original in moved


def test_split_apply_moves_produce_the_committed_fixture_bytes(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    assert run(env, "split-apply", "--manifest", manifest_path, "--date", DATE).returncode == 0
    assert run(env, "promote", "--manifest", manifest_path, *PROMOTE_ARGS).returncode == 0
    produced = tree(root)
    produced.pop("base.md")
    committed = tree(FIXTURES / "good")
    committed.pop("base.md")
    assert produced == committed


def test_split_apply_moves_leave_source_front_matter_carried_to_clusters(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    for leaf in (DRIFT, HANG):
        text = (root / leaf).read_text(encoding="utf-8")
        assert "schema: difficulty/v1" in text
        assert re.search(r'^resolution_confirmed_by_user: ".+"$', text, re.MULTILINE)
        assert f"created: {DATE}" in text


def test_split_apply_moves_indexes_the_new_leaves(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    index = (root / "leaves/experience/MEMORY.md").read_text(encoding="utf-8")
    assert "(2026-01-05-sample-venue-drift.md)" in index
    assert "(2026-02-15-sample-fetch-hang.md)" in index
    assert index.count("(2026-01-05-sample-venue-difficulty.md)") == 1


def test_split_apply_moves_stamp_the_edit_ledger(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    ledger = Path(env["AGENTCTL_EDIT_LEDGER"]).read_text(encoding="utf-8")
    assert str(root / DRIFT) in ledger and "record-experience:split-apply" in ledger


def _refused(done, root, original_tree, needle):
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert needle in done.stderr
    assert tree(root) == original_tree


def test_split_apply_rejects_inconsistent_manifest_unknown_id(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    manifest["clusters"][0]["contexts"][0] = "2:000000000000"
    write_manifest(manifest_path, manifest)
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, "2:000000000000 is not in the source")


def test_split_apply_rejects_inconsistent_manifest_doubly_assigned_id(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    manifest["clusters"][1]["contexts"].append(manifest["clusters"][0]["contexts"][0])
    write_manifest(manifest_path, manifest)
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, "already assigned")


def test_split_apply_rejects_inconsistent_manifest_unassigned_id(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    dropped = manifest["clusters"][1]["contexts"].pop()
    write_manifest(manifest_path, manifest)
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, f"context {dropped} is neither kept nor clustered")


def test_split_apply_rejects_inconsistent_manifest_malformed_cluster(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    manifest["clusters"][0]["slug"] = "Not A Slug"
    write_manifest(manifest_path, manifest)
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, "slug")


def test_split_apply_rejects_inconsistent_manifest_existing_cluster_leaf(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    (root / DRIFT).write_text("occupied\n", encoding="utf-8")
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, "already exists")


def test_split_apply_rejects_inconsistent_manifest_listed_principle_missing(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    manifest["principles"] = ["no-such-principle"]
    write_manifest(manifest_path, manifest)
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, "does not exist")


def test_split_apply_refuses_canon_checkout(env, tmp_path):
    canon = primary_checkout(tmp_path)
    root = canon / "memory"
    (root / "leaves/experience").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "good/base.md", root / SOURCE)
    shutil.copyfile(FIXTURES / "good/base.md", root / "base.md")
    manifest = json.loads((FIXTURES / "good.json").read_text(encoding="utf-8"))
    manifest["root"] = str(root)
    manifest["principles"] = []
    manifest_path = write_manifest(tmp_path / "canon.json", manifest)
    original = tree(root)
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    _refused(done, root, original, "canonical checkout")
    assert not (root / DRIFT).exists()


def test_split_apply_moves_in_linked_worktree_and_commits_nothing(env, tmp_path):
    canon = primary_checkout(tmp_path)
    linked = tmp_path / "linked"
    git(canon, "worktree", "add", "-q", "-b", "proposal", str(linked))
    root = linked / "memory"
    (root / "leaves/experience").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "good/base.md", root / SOURCE)
    shutil.copyfile(FIXTURES / "good/base.md", root / "base.md")
    manifest = json.loads((FIXTURES / "good.json").read_text(encoding="utf-8"))
    manifest["root"] = str(root)
    manifest["principles"] = []
    manifest_path = write_manifest(tmp_path / "linked.json", manifest)
    head = git(linked, "rev-parse", "HEAD").stdout
    done = run(env, "split-apply", "--manifest", manifest_path, "--date", DATE)
    assert done.returncode == 0, done.stderr
    assert (root / DRIFT).is_file()
    assert git(linked, "rev-parse", "HEAD").stdout == head
    assert "memory/" in git(linked, "status", "--porcelain").stdout


# --- promote ---------------------------------------------------------------

def _split_then_promote(env, tmp_path):
    root, manifest_path, manifest = fresh_work(tmp_path)
    assert run(env, "split-apply", "--manifest", manifest_path, "--date", DATE).returncode == 0
    done = run(env, "promote", "--manifest", manifest_path, *PROMOTE_ARGS)
    assert done.returncode == 0, done.stderr
    return root, manifest_path


def test_promote_schema_matches_principle_v1_and_passes_the_structure_verifier(env, tmp_path):
    root, _ = _split_then_promote(env, tmp_path)
    text = (root / PRINCIPLE).read_text(encoding="utf-8")
    front = re_mod.frontmatter_values(text)
    assert front["schema"] == "principle/v1"
    assert front["generality"] == "1"
    assert front["name"] == "sample-venue-principle"
    for section in ("## Principle", "## Generality", "## Induced from",
                    "## Refutation", "## See also"):
        assert section in text
    verified = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "verify-leaf-structure.py"), str(root / PRINCIPLE)],
        capture_output=True, text=True)
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_promote_schema_indexes_under_its_generality_level(env, tmp_path):
    root, _ = _split_then_promote(env, tmp_path)
    index = (root / "leaves/principles/MEMORY.md").read_text(encoding="utf-8")
    level = index.index("## Level 1")
    assert index.index("(sample-venue-principle.md)") > level


def test_promote_schema_refuses_unknown_generality(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    args = list(PROMOTE_ARGS)
    args[args.index("--generality") + 1] = "7"
    done = run(env, "promote", "--root", root, *args)
    assert done.returncode == 2
    assert not (root / PRINCIPLE).exists()


def test_promote_writes_induced_from_every_named_source(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    assert run(env, "split-apply", "--manifest", manifest_path, "--date", DATE).returncode == 0
    args = [a for a in PROMOTE_ARGS]
    args += ["--induced-from", "2026-02-15-sample-fetch-hang"]
    done = run(env, "promote", "--root", root, *args)
    assert done.returncode == 0, done.stderr
    text = (root / PRINCIPLE).read_text(encoding="utf-8")
    front = re_mod.frontmatter_values(text)
    assert front["induced_from"] == "[2026-01-05-sample-venue-drift, 2026-02-15-sample-fetch-hang]"
    assert "[[2026-01-05-sample-venue-drift]]" in text
    assert "[[2026-02-15-sample-fetch-hang]]" in text
    index = (root / "leaves/principles/MEMORY.md").read_text(encoding="utf-8")
    assert "`2026-01-05-sample-venue-drift`" in index and "`2026-02-15-sample-fetch-hang`" in index


def test_promote_writes_induced_from_only_leaves_that_exist(env, tmp_path):
    root, manifest_path, _ = fresh_work(tmp_path)
    args = list(PROMOTE_ARGS)
    args[args.index("2026-01-05-sample-venue-drift")] = "2026-01-05-never-written"
    done = run(env, "promote", "--root", root, *args)
    assert done.returncode == 2
    assert "never-written" in done.stderr
    assert not (root / PRINCIPLE).exists()


def test_promote_writes_induced_from_into_manifest_and_source_pointer_list(env, tmp_path):
    root, manifest_path = _split_then_promote(env, tmp_path)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["principles"] == [
        "sample-venue-principle"]
    source = (root / SOURCE).read_text(encoding="utf-8")
    assert "principle: [sample-venue-principle](../principles/sample-venue-principle.md)" in source


def test_promote_schema_adds_a_second_principle_to_its_own_level_section(env, tmp_path):
    root, _ = _split_then_promote(env, tmp_path)
    same_level = [("second-principle" if a == "sample-venue-principle" else a)
                  for a in PROMOTE_ARGS]
    assert run(env, "promote", "--root", root, *same_level).returncode == 0
    higher = [("third-principle" if a == "sample-venue-principle" else a) for a in PROMOTE_ARGS]
    higher[higher.index("--generality") + 1] = "2"
    assert run(env, "promote", "--root", root, *higher).returncode == 0
    index = (root / "leaves/principles/MEMORY.md").read_text(encoding="utf-8")
    level1, level2 = index.index("## Level 1"), index.index("## Level 2")
    assert index.count("## Level 1") == 1
    assert level1 < index.index("(sample-venue-principle.md)") < level2
    assert level1 < index.index("(second-principle.md)") < level2
    assert index.index("(third-principle.md)") > level2


def test_promote_refuses_existing_principle(env, tmp_path):
    root, manifest_path = _split_then_promote(env, tmp_path)
    original = tree(root)
    done = run(env, "promote", "--manifest", manifest_path, *PROMOTE_ARGS)
    assert done.returncode == 2
    assert "already exists" in done.stderr
    assert tree(root) == original


def test_promote_refuses_canon_checkout(env, tmp_path):
    canon = primary_checkout(tmp_path)
    root = canon / "memory"
    (root / "leaves/experience").mkdir(parents=True)
    (root / "leaves/experience/2026-01-05-sample-venue-drift.md").write_text(
        "leaf\n", encoding="utf-8")
    original = tree(root)
    done = run(env, "promote", "--root", root, *PROMOTE_ARGS)
    assert done.returncode == 2
    assert "canonical checkout" in done.stderr
    assert tree(root) == original


# --- split-verify ----------------------------------------------------------

def test_split_verify_conservation_accepts_good_fixture(env):
    done = run(env, "split-verify", "--manifest", FIXTURES / "good.json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "5 context(s)" in done.stdout and "OK" in done.stdout


def test_split_verify_conservation_does_not_depend_on_leaf_order_in_the_manifest(env, tmp_path):
    work = fixture_copy(tmp_path)
    manifest_path = work / "good.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clusters"].reverse()
    write_manifest(manifest_path, manifest)
    assert run(env, "split-verify", "--manifest", manifest_path).returncode == 0


def test_split_verify_detects_loss_in_lossy_fixture(env):
    done = run(env, "split-verify", "--manifest", FIXTURES / "lossy.json")
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    missing = [l for l in lines if l.startswith("missing:")]
    assert missing == ["missing: 3:e51c0fc04bb0"]
    assert all(MISSING_LINE.fullmatch(l) for l in missing)
    assert not [l for l in lines if l.startswith(("duplicate:", "unexpected:", "verifier:"))]


def test_split_verify_detects_loss_of_an_altered_block(env, tmp_path):
    work = fixture_copy(tmp_path)
    leaf = work / "good" / DRIFT
    text = leaf.read_text(encoding="utf-8")
    leaf.write_text(text.replace("three stages", "four stages"), encoding="utf-8")
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert [l for l in lines if l.startswith("missing:")] == ["missing: 2:66a81f81a879"]
    assert any(l.startswith("unexpected: 1:") and DRIFT in l for l in lines)


def test_split_verify_detects_loss_of_a_whole_cluster_leaf(env, tmp_path):
    work = fixture_copy(tmp_path)
    (work / "good" / HANG).unlink()
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    missing = [l for l in done.stdout.splitlines() if l.startswith("missing:")]
    assert missing == ["missing: 4:0372ce75d1b7", "missing: 5:403a7a175eaa"]


def test_split_verify_detects_duplicate_context(env, tmp_path):
    work = fixture_copy(tmp_path)
    leaf = work / "good" / HANG
    duplicated = block_text(work / "good" / DRIFT, 1)
    text = leaf.read_text(encoding="utf-8")
    leaf.write_text(text.replace("## Cost\n", duplicated + "\n## Cost\n", 1), encoding="utf-8")
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    assert "duplicate: 2:66a81f81a879" in done.stdout.splitlines()
    assert not [l for l in done.stdout.splitlines() if l.startswith("missing:")]


def test_split_verify_detects_duplicate_between_source_and_cluster(env, tmp_path):
    work = fixture_copy(tmp_path)
    leaf = work / "good" / SOURCE
    kept_again = block_text(work / "good" / HANG, 1)
    text = leaf.read_text(encoding="utf-8")
    leaf.write_text(text.replace("## Common core", kept_again + "\n## Common core", 1),
                    encoding="utf-8")
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    assert "duplicate: 4:0372ce75d1b7" in done.stdout.splitlines()


def test_split_verify_detects_a_conserved_but_misplaced_context(env, tmp_path):
    work = fixture_copy(tmp_path)
    drift, hang = work / "good" / DRIFT, work / "good" / HANG
    moved = block_text(hang, 2)
    hang.write_text(hang.read_text(encoding="utf-8").replace(moved + "\n", "", 1),
                    encoding="utf-8")
    drift.write_text(drift.read_text(encoding="utf-8").replace(
        "## Cost\n", moved + "\n## Cost\n", 1), encoding="utf-8")
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert not [l for l in lines if l.startswith(("missing:", "duplicate:"))]
    assert any(l.startswith("misplaced:") and DRIFT in l for l in lines)


def test_split_verify_runs_leaf_verifiers_on_every_cluster_leaf(env, tmp_path):
    work = fixture_copy(tmp_path)
    leaf = work / "good" / DRIFT
    text = leaf.read_text(encoding="utf-8")
    leaf.write_text(re.sub(r"^resolution_confirmed_by_user:.*\n", "", text, flags=re.MULTILINE),
                    encoding="utf-8")
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert any(l.startswith("verifier:") and DRIFT in l for l in lines)
    assert not [l for l in lines if l.startswith(("missing:", "duplicate:", "unexpected:"))]


def test_split_verify_runs_leaf_verifiers_on_listed_principles(env, tmp_path):
    work = fixture_copy(tmp_path)
    (work / "good" / PRINCIPLE).unlink()
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    assert any(l.startswith("verifier:") and "sample-venue-principle" in l
               for l in done.stdout.splitlines())


def test_split_verify_runs_the_memory_index_verifier(env, tmp_path):
    work = fixture_copy(tmp_path)
    (work / "good/leaves/experience/MEMORY.md").write_text(
        "# Experience\n\n## 2026-01\n\n- [only one](2026-01-05-sample-venue-difficulty.md) — x\n",
        encoding="utf-8")
    done = run(env, "split-verify", "--manifest", work / "good.json")
    assert done.returncode == 1
    assert any(l.startswith("verifier:") for l in done.stdout.splitlines())


def test_split_verify_refuses_a_malformed_manifest(env, tmp_path):
    work = fixture_copy(tmp_path)
    manifest = json.loads((work / "good.json").read_text(encoding="utf-8"))
    del manifest["keep"]
    write_manifest(work / "bad.json", manifest)
    done = run(env, "split-verify", "--manifest", work / "bad.json")
    assert done.returncode == 2
    assert "keep" in done.stderr


def test_split_verify_reports_manifest_assignment_problems_as_failures(env, tmp_path):
    work = fixture_copy(tmp_path)
    manifest = json.loads((work / "good.json").read_text(encoding="utf-8"))
    manifest["keep"] = []
    write_manifest(work / "unassigned.json", manifest)
    done = run(env, "split-verify", "--manifest", work / "unassigned.json")
    assert done.returncode == 1
    assert any(l.startswith("manifest:") and "1:5dbabc969e13" in l
               for l in done.stdout.splitlines())


def test_split_verify_reads_base_bytes_from_base_rev(env, tmp_path):
    repo = primary_checkout(tmp_path)
    root = repo / "memory"
    shutil.copytree(FIXTURES / "good", root)
    (root / "base.md").unlink()
    git(repo, "add", "-A")
    base = repo / "memory" / SOURCE
    shutil.copyfile(FIXTURES / "good/base.md", base)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    manifest = json.loads((FIXTURES / "good.json").read_text(encoding="utf-8"))
    manifest.pop("base_path")
    manifest["base_rev"] = "HEAD~1"
    manifest["root"] = str(root)
    manifest_path = write_manifest(tmp_path / "rev.json", manifest)
    shutil.copyfile(FIXTURES / "good" / SOURCE, base)
    done = run(env, "split-verify", "--manifest", manifest_path)
    assert done.returncode == 0, done.stdout + done.stderr


# --- the apply -> verify loop and the command-routing contract --------------

def test_split_apply_then_verify_accepts_its_own_output(env, tmp_path):
    root, manifest_path = _split_then_promote(env, tmp_path)
    done = run(env, "split-verify", "--manifest", manifest_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_split_apply_then_verify_rejects_the_output_after_a_context_is_deleted(env, tmp_path):
    root, manifest_path = _split_then_promote(env, tmp_path)
    leaf = root / DRIFT
    gone = block_text(leaf, 2)
    leaf.write_text(leaf.read_text(encoding="utf-8").replace(gone, "", 1), encoding="utf-8")
    done = run(env, "split-verify", "--manifest", manifest_path)
    assert done.returncode == 1
    assert "missing: 3:e51c0fc04bb0" in done.stdout.splitlines()


def test_split_commands_not_in_write_commands_so_nothing_is_auto_landed():
    for command in ("split-contexts", "split-apply", "split-verify", "promote"):
        assert command not in re_mod.WRITE_COMMANDS
        args = SimpleNamespace(cmd=command, scope="global", leaf="x")
        assert re_mod.needs_worktree(args) is False


def test_split_commands_not_in_write_commands_keeps_the_existing_set():
    assert set(re_mod.WRITE_COMMANDS) == {"new", "extend", "ticket", "set-last-verified"}
