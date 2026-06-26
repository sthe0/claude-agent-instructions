"""Tests for promote-scan and the cmd_new fragmentation guard (Stage 2).

Spec requirements:
  (i)   two leaves with the same ground sum their contexts and flag at threshold
  (ii)  one leaf with >= threshold contexts flags
  (iii) below threshold → no flag
  (iv)  --json shape
  (v)   cmd_new refuses an analogous-ground leaf without --justify-new
  (vi)  cmd_new succeeds with --justify-new
  (vii) cmd_new on a fresh ground (no analog) still writes
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "record_experience",
    Path(__file__).resolve().parents[1] / "record-experience.py",
)
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _exp_dir(tmp_path: Path) -> Path:
    """Return the project-scoped experience dir under tmp_path (created)."""
    d = tmp_path / ".claude" / "agent-memory" / "experience"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _leaf(path: Path, *, difficulty: str, contexts: int = 1, desc: str = "") -> None:
    """Write a minimal difficulty/v1 leaf with `contexts` context blocks."""
    blocks = "".join(
        f"\n### 2026-01-{i + 1:02d} — ctx-{i + 1}\n"
        f"- Where it arose: test\n"
        f"- Working plan: test plan\n"
        for i in range(contexts)
    )
    text = (
        f"---\n"
        f"name: {path.stem}\n"
        f"description: {desc or difficulty[:80]}\n"
        f"type: reference\n"
        f"schema: difficulty/v1\n"
        f'resolution_confirmed_by_user: "tester"\n'
        f"---\n"
        f"\n# Test Leaf\n"
        f"\n## Difficulty\n{difficulty}\n"
        f"\n## Order & criterion\norder\n\n**Acceptance check:** check\n"
        f"\n## Contexts\n{blocks}"
        f"\n## Cost\nfree\n"
    )
    path.write_text(text, encoding="utf-8")


def _scan_args(tmp_path: Path, *, threshold: int | None = None,
               json_out: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        scope="project", project_dir=str(tmp_path),
        threshold=threshold, json_out=json_out, date="2026-01-01",
    )


def _new_args(tmp_path: Path, slug: str, difficulty: str, *,
              justify_new: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        scope="project", project_dir=str(tmp_path),
        date="2026-06-27", slug=slug, title="Test Title",
        description=f"desc: {difficulty[:60]}",
        confirmed_by="tester",
        difficulty=difficulty,
        order="order criterion",
        criterion="acceptance check",
        context_where="test env",
        plan="test plan",
        context_label="initial",
        plan_file=None, cost=None, self_critique=None,
        refs=[],
        justify_new=justify_new,
    )


# ---------------------------------------------------------------------------
# promote-scan tests
# ---------------------------------------------------------------------------

def test_two_leaves_same_ground_sum_and_flag(tmp_path: Path, capsys) -> None:
    """(i) Two leaves with identical ground cluster together; Σ contexts flags at threshold."""
    exp_dir = _exp_dir(tmp_path)
    difficulty = "authentication token expiry not handled gracefully on reconnect"
    _leaf(exp_dir / "leaf-a.md", difficulty=difficulty, contexts=1)
    _leaf(exp_dir / "leaf-b.md", difficulty=difficulty, contexts=1)

    rc = rec.cmd_promote_scan(_scan_args(tmp_path, threshold=2))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 occurrence" in out
    assert "candidate" in out


def test_single_leaf_enough_contexts_flags(tmp_path: Path, capsys) -> None:
    """(ii) A single leaf accumulating >= threshold contexts in one file is flagged."""
    exp_dir = _exp_dir(tmp_path)
    _leaf(exp_dir / "multi.md",
          difficulty="missing retry logic in the sync loop",
          contexts=3)

    rc = rec.cmd_promote_scan(_scan_args(tmp_path, threshold=3))
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 occurrence" in out
    assert "candidate" in out


def test_below_threshold_no_flag(tmp_path: Path, capsys) -> None:
    """(iii) A cluster whose Σ contexts < threshold produces no flag line."""
    exp_dir = _exp_dir(tmp_path)
    _leaf(exp_dir / "few.md",
          difficulty="database connection pool exhausted under load",
          contexts=2)

    rc = rec.cmd_promote_scan(_scan_args(tmp_path, threshold=3))
    assert rc == 0
    out = capsys.readouterr().out
    assert "candidate" not in out


def test_json_shape(tmp_path: Path, capsys) -> None:
    """(iv) --json outputs a parseable list of cluster objects with the right fields."""
    exp_dir = _exp_dir(tmp_path)
    _leaf(exp_dir / "j.md",
          difficulty="the plan approval gate is bypassed when the user is quiet",
          contexts=3)

    rc = rec.cmd_promote_scan(_scan_args(tmp_path, threshold=3, json_out=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) >= 1
    c = data[0]
    assert c["occurrences_total"] == 3
    assert "j.md" in c["members"]
    assert c["flagged"] is True
    assert isinstance(c["fragmented"], bool)


def test_json_fragmented_flag_set_for_two_leaves(tmp_path: Path, capsys) -> None:
    """Clusters with >= 2 member leaves report fragmented=True in JSON."""
    exp_dir = _exp_dir(tmp_path)
    diff = "spawn gate blocks legitimate writes when no plan exists"
    _leaf(exp_dir / "frag-a.md", difficulty=diff, contexts=1)
    _leaf(exp_dir / "frag-b.md", difficulty=diff, contexts=1)

    rc = rec.cmd_promote_scan(_scan_args(tmp_path, threshold=10, json_out=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    cluster = next(c for c in data if len(c["members"]) >= 2)
    assert cluster["fragmented"] is True


# ---------------------------------------------------------------------------
# cmd_new fragmentation guard tests
# ---------------------------------------------------------------------------

def test_cmd_new_refuses_analogous_without_justify(tmp_path: Path) -> None:
    """(v) cmd_new exits when an analogous leaf already exists and --justify-new is absent."""
    exp_dir = _exp_dir(tmp_path)
    difficulty = "the resolution gate is skipped after the user says thanks"
    _leaf(exp_dir / "existing.md", difficulty=difficulty, contexts=1)

    with pytest.raises(SystemExit):
        rec.cmd_new(_new_args(tmp_path, "new-dup", difficulty))


def test_cmd_new_succeeds_with_justify_new(tmp_path: Path, capsys) -> None:
    """(vi) cmd_new writes despite a similar existing leaf when --justify-new is given."""
    exp_dir = _exp_dir(tmp_path)
    difficulty = "the resolution gate is skipped after the user says thanks"
    _leaf(exp_dir / "existing.md", difficulty=difficulty, contexts=1)

    rc = rec.cmd_new(_new_args(tmp_path, "new-justified", difficulty,
                               justify_new="distinct trigger: silent session exit, not gratitude"))
    assert rc == 0
    written = exp_dir / "2026-06-27-new-justified.md"
    assert written.exists()


def test_cmd_new_fresh_ground_still_writes(tmp_path: Path, capsys) -> None:
    """(vii) cmd_new on a genuinely fresh ground (no analog) writes without guard."""
    _exp_dir(tmp_path)  # create dir; no leaves seeded

    rc = rec.cmd_new(_new_args(
        tmp_path, "brand-new",
        "TLS handshake timeout not surfaced in the retry loop error log",
    ))
    assert rc == 0
    written = _exp_dir(tmp_path) / "2026-06-27-brand-new.md"
    assert written.exists()


def test_cmd_new_overwrite_guard_still_fires(tmp_path: Path) -> None:
    """Existing-file guard still fires before the fragmentation guard (regression)."""
    exp_dir = _exp_dir(tmp_path)
    target = exp_dir / "2026-06-27-same-slug.md"
    target.write_text("existing content", encoding="utf-8")

    with pytest.raises(SystemExit):
        rec.cmd_new(_new_args(tmp_path, "same-slug",
                              "some totally fresh difficulty no analog at all",
                              justify_new="would pass fragmentation"))
