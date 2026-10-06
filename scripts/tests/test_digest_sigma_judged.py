"""core-difficulty-digest and sigma-sentinel decide "same difficulty" only by a judged YES.

Lexical overlap only nominates candidates; a model judge decides; undecided pairs are counted
(`unjudged`), never joined or refuted. Both scripts stay report-only.
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import difficulty_channel as dc

SCRIPTS = Path(__file__).resolve().parents[1]

BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
VERDICTS_ENV = "CLAUDE_SAME_DIFFICULTY_VERDICTS"

G1 = "gate denies a legitimate memory write during session"
G1_NEAR = "gate denies a legitimate memory write during sessions"
G_REWORDED = "memory writes get blocked wrongly by the permission gate"
G_UNRELATED = "disk quota exhaustion on the shared volume"


def _sj():
    return importlib.import_module("lib.semantic_join")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _digest():
    return _load("core_difficulty_digest_judged", "core-difficulty-digest.py")


def _sigma():
    return _load("sigma_sentinel_judged", "sigma-sentinel.py")


class Judge:
    def __init__(self, answer=False):
        self.answer = answer
        self.calls = []

    def __call__(self, a, b, timeout=None):
        self.calls.append((a, b))
        return self.answer, ""


def _setup(monkeypatch, tmp_path, *, budget="0", answer=False):
    monkeypatch.setenv(BUDGET_ENV, budget)
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "verdicts.json"))
    judge = Judge(answer)
    monkeypatch.setattr(_sj(), "default_judge", judge)
    return judge


def _seed(tmp_path, verdict, a, b):
    sj = _sj()
    sj.VerdictCache(tmp_path / "verdicts.json", sj.SAME_DIFFICULTY_JUDGE_SALT).put(verdict, a, b)


def _record(ground, reporter="r"):
    return dc.DifficultyRecord(
        ts="2026-06-26T00:00:00", layer="core", target="CLAUDE.md", functional_ground=ground,
        severity=dc.Severity.MEDIUM, reporter=reporter, evidence="e", cost_estimate="",
    )


def _write_leaf(d: Path, name: str, desc: str, difficulty: str, tier: int | None = None) -> None:
    tier_line = f"tier: {tier}\n" if tier is not None else ""
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: {desc}\ntype: reference\n"
        f"schema: difficulty/v1\n{tier_line}"
        f'resolution_confirmed_by_user: "tester"\n---\n'
        f"\n# T\n\n## Difficulty\n{difficulty}\n"
        f"\n## Order & criterion\no\n\n**Acceptance check:** c\n"
        f"\n## Contexts\n\n### 2026-01-01 — ctx\n- Where it arose: x\n- Working plan: y\n"
        f"\n## Cost\nfree\n",
        encoding="utf-8",
    )


def _corpus(tmp_path, sigma):
    sigma.PRINCIPLES_DIR = tmp_path / "no-principles"
    exp = tmp_path / ".claude" / "agent-memory" / "experience"
    _write_leaf(exp, "e1.md", "coordinator edits directly", "gate denies a legitimate memory write during session")
    _write_leaf(exp, "e2.md", "coordinator edits directly", "gate denies a legitimate memory write during sessions")
    return exp


def test_cluster_records_follows_judge(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    digest = _digest()
    near = [_record(G1), _record(G1_NEAR)]
    _seed(tmp_path, False, G1, G1_NEAR)
    assert len(digest.cluster_records(near)) == 2

    reworded = [_record(G1), _record(G_REWORDED)]
    _seed(tmp_path, True, G1, G_REWORDED)
    assert len(digest.cluster_records(reworded)) == 1


def test_condition_a_counts_only_judged_refutations(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sigma = _sigma()
    _seed(tmp_path, False, G1, G1_NEAR)
    hits = sigma.measure_condition_a(
        [sigma.Leaf("e.md", G1_NEAR, tier=1)], [sigma.Leaf("p.md", G1)], threshold=3)
    assert len(hits) == 0


def test_condition_a_counts_judged_yes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sigma = _sigma()
    _seed(tmp_path, True, G1, G_REWORDED)
    hits = sigma.measure_condition_a(
        [sigma.Leaf("e.md", G_REWORDED, tier=1)], [sigma.Leaf("p.md", G1)], threshold=3)
    assert [h.count for h in hits] == [1]


def test_cheap_c_follows_judge(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sigma = _sigma()
    items = [sigma.Leaf("a.md", G1), sigma.Leaf("b.md", G1_NEAR), sigma.Leaf("c.md", G_UNRELATED)]
    _seed(tmp_path, False, G1, G1_NEAR)
    assert sigma.measure_cheap_c(items).near_duplicate_pairs == 0

    _seed(tmp_path, True, G1, G1_NEAR)
    c = sigma.measure_cheap_c(items)
    assert c.near_duplicate_pairs == 1
    assert c.largest_cluster == 2


def test_budget_zero_reports_unjudged_counts(monkeypatch, tmp_path):
    judge = _setup(monkeypatch, tmp_path, budget="0")
    digest, sigma = _digest(), _sigma()

    clusters = digest.cluster_records([_record(G1), _record(G1_NEAR)])
    assert getattr(clusters, "unjudged_pairs", None) == 1
    assert len(clusters) == 2

    hits = sigma.measure_condition_a(
        [sigma.Leaf("e.md", G1_NEAR, tier=1)], [sigma.Leaf("p.md", G1)], threshold=3)
    assert getattr(hits, "unjudged_pairs", None) == 1
    assert len(hits) == 0

    c = sigma.measure_cheap_c([sigma.Leaf("a.md", G1), sigma.Leaf("b.md", G1_NEAR)])
    assert getattr(c, "unjudged_pairs", None) == 1
    assert c.near_duplicate_pairs == 0
    assert judge.calls == []


def _unjudged_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("unjudged=")]


def test_cli_and_report_print_unjudged_line(monkeypatch, tmp_path, capsys):
    judge = _setup(monkeypatch, tmp_path, budget="0")
    digest, sigma = _digest(), _sigma()

    monkeypatch.setattr(digest, "pull_all", lambda channels, since=None: [_record(G1), _record(G1_NEAR)])
    assert digest.main(["--channel", "x"]) == 0
    lines = _unjudged_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert lines[0] == "unjudged=1"

    _corpus(tmp_path, sigma)
    assert sigma.main(["--scope", "project", "--project-dir", str(tmp_path)]) == 0
    lines = _unjudged_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert lines[0] == "unjudged=1"
    assert judge.calls == []


def test_sigma_and_promote_scan_share_cache(monkeypatch, tmp_path):
    judge = _setup(monkeypatch, tmp_path, budget="60", answer=True)
    sigma = _sigma()
    rec = sigma._rec
    items = [SimpleNamespace(ground=G1), SimpleNamespace(ground=G_REWORDED)]
    assert len(rec.cluster_by_ground(items, lambda it: it.ground)) == 1
    assert judge.calls, "the first run must reach the judge"

    judge.calls.clear()
    c = sigma.measure_cheap_c([sigma.Leaf("a.md", G1), sigma.Leaf("b.md", G_REWORDED)])
    stats = getattr(c, "stats", None) or {}
    assert stats.get("cached_hits", 0) >= 1
    assert judge.calls == []


def test_digest_and_sigma_stay_report_only(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, budget="0")
    digest, sigma = _digest(), _sigma()
    spawned = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        spawned.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    _corpus(tmp_path, sigma)
    d = sigma.build_digest("project", str(tmp_path), threshold=3)
    assert d["decides"] is False and d["builds"] is False

    monkeypatch.setattr(digest, "pull_all", lambda channels, since=None: [_record(G1), _record(G1_NEAR)])
    assert digest.main(["--channel", "x"]) == 0
    capsys.readouterr()
    assert not [c for c in spawned if "file-difficulty" in " ".join(map(str, c))]


def _five(prefix_tokens, tag):
    return [f"retry loop timeout gate failure {prefix_tokens} {tag}{w}" for w in
            ("alpha", "bravo", "charlie", "delta", "echo")]


def _asked_per_query(judge):
    asked = {}
    for a, _b in judge.calls:
        asked[a] = asked.get(a, 0) + 1
    return asked


def test_condition_a_asks_at_most_k3_nominees(monkeypatch, tmp_path):
    judge = _setup(monkeypatch, tmp_path, budget="60", answer=False)
    sigma = _sigma()
    principles = [sigma.Leaf(f"p{i}.md", g) for i, g in enumerate(_five("principle", "p"))]
    leaves = [sigma.Leaf("e1.md", "retry loop timeout gate failure leafone", tier=1),
              sigma.Leaf("e2.md", "retry loop timeout gate failure leaftwo", tier=1)]
    sigma.measure_condition_a(leaves, principles, threshold=3)
    asked = _asked_per_query(judge)
    assert sorted(asked.values()) == [3, 3]
    assert len(judge.calls) <= 3 * len(leaves)


def test_cheap_c_asks_at_most_k3_nominees(monkeypatch, tmp_path):
    judge = _setup(monkeypatch, tmp_path, budget="60", answer=False)
    sigma = _sigma()
    items = [sigma.Leaf(f"i{i}.md", g) for i, g in enumerate(_five("item", "v"))]
    c = sigma.measure_cheap_c(items)
    assert c.near_duplicate_pairs == 0
    asked = _asked_per_query(judge)
    assert 0 < len(judge.calls) <= 3 * len(items)
    assert asked and max(asked.values()) <= 3
