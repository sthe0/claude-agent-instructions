"""promote-scan joins experience leaves only on a judged YES; lexical overlap only nominates."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location("record_experience_judged", SCRIPTS / "record-experience.py")
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)

BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
VERDICTS_ENV = "CLAUDE_SAME_DIFFICULTY_VERDICTS"

DESC_A = "authentication token expiry not handled on reconnect"
DESC_B = "token expiry for authentication unhandled when reconnecting"


def _sj():
    return importlib.import_module("lib.semantic_join")


class Judge:
    def __init__(self, answer=False):
        self.answer = answer
        self.calls = []

    def __call__(self, a, b, timeout=None):
        self.calls.append((a, b, timeout))
        return self.answer, ""


def _install_judge(monkeypatch, judge):
    monkeypatch.setattr(_sj(), "default_judge", judge)


def _leaf(path: Path, *, desc: str, difficulty: str) -> None:
    path.write_text(
        f"---\nname: {path.stem}\ndescription: {desc}\ntype: reference\nschema: difficulty/v1\n"
        f'resolution_confirmed_by_user: "tester"\n---\n\n# Test Leaf\n\n'
        f"## Difficulty\n{difficulty}\n\n## Order & criterion\norder\n\n"
        f"## Contexts\n\n### 2026-01-01 — ctx\n- Where it arose: test\n\n## Cost\nfree\n",
        encoding="utf-8",
    )


def _exp_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".claude" / "agent-memory" / "experience"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ground(leaf: Path) -> str:
    text = leaf.read_text(encoding="utf-8")
    desc = rec.FRONTMATTER.match(text).group(1).split("description:", 1)[1].splitlines()[0].strip()
    start, end = rec.section_span(text, "Difficulty")
    return desc + " " + text[start:end]


def _seed_yes(path: Path, *grounds: str) -> None:
    joined = "\x00".join([_sj().SAME_DIFFICULTY_JUDGE_SALT] + sorted(" ".join(g.split()) for g in grounds))
    key = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    verdicts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    verdicts[key] = True
    path.write_text(json.dumps(verdicts), encoding="utf-8")


def _scan(tmp_path: Path, capsys) -> dict:
    args = SimpleNamespace(scope="project", project_dir=str(tmp_path), threshold=10,
                           json_out=True, date="2026-01-01")
    assert rec.cmd_promote_scan(args) == 0
    return json.loads(capsys.readouterr().out)


def _clusters(out) -> list:
    return out["clusters"] if isinstance(out, dict) else out


def _two_paraphrases(tmp_path: Path):
    exp = _exp_dir(tmp_path)
    a, b = exp / "leaf-a.md", exp / "leaf-b.md"
    _leaf(a, desc=DESC_A, difficulty="the reconnect path ignores an expired token")
    _leaf(b, desc=DESC_B, difficulty="a stale token is reused after the link drops")
    return a, b


def test_promote_scan_joins_only_on_cached_yes(monkeypatch, tmp_path, capsys):
    judge = Judge(False)
    _install_judge(monkeypatch, judge)
    monkeypatch.setenv(BUDGET_ENV, "0")
    cache = tmp_path / "verdicts.json"
    monkeypatch.setenv(VERDICTS_ENV, str(cache))
    a, b = _two_paraphrases(tmp_path)

    before = _clusters(_scan(tmp_path, capsys))
    assert sorted(len(c["members"]) for c in before) == [1, 1]

    _seed_yes(cache, _ground(a), _ground(b))
    after = _clusters(_scan(tmp_path, capsys))
    assert [sorted(c["members"]) for c in after] == [["leaf-a.md", "leaf-b.md"]]
    assert judge.calls == []


def test_promote_scan_json_reports_stats(monkeypatch, tmp_path, capsys):
    _install_judge(monkeypatch, Judge(False))
    monkeypatch.setenv(BUDGET_ENV, "0")
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "verdicts.json"))
    exp = _exp_dir(tmp_path)
    _leaf(exp / "same-a.md", desc=DESC_A, difficulty="identical body")
    _leaf(exp / "same-b.md", desc=DESC_A, difficulty="identical body")

    out = _scan(tmp_path, capsys)
    assert isinstance(out, dict)
    assert isinstance(out.get("clusters"), list)
    stats = out.get("stats")
    assert isinstance(stats, dict)
    assert {"judged_calls", "cached_hits", "identity_joins", "unjudged_items"} <= set(stats)
    assert stats["identity_joins"] == 1
    assert stats["judged_calls"] == 0
    assert [sorted(c["members"]) for c in out["clusters"]] == [["same-a.md", "same-b.md"]]


def test_cluster_by_ground_asks_at_most_k3_nominees(monkeypatch, tmp_path):
    judge = Judge(False)
    _install_judge(monkeypatch, judge)
    monkeypatch.delenv(BUDGET_ENV, raising=False)
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "verdicts.json"))
    grounds = [f"retry loop timeout gate failure variant{w}" for w in
               ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")]
    items = [SimpleNamespace(ground=g) for g in grounds]

    groups = rec.cluster_by_ground(items, lambda it: it.ground)

    assert len(groups) == len(grounds)
    assert judge.calls, "a lexically overlapping pair must be put to the judge"
    asked = {}
    for a, _b, _t in judge.calls:
        asked[a] = asked.get(a, 0) + 1
    assert max(asked.values()) <= 3


def test_promote_scan_honours_zero_budget_env(monkeypatch, tmp_path, capsys):
    judge = Judge(True)
    _install_judge(monkeypatch, judge)
    monkeypatch.setenv(BUDGET_ENV, "0")
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "verdicts.json"))
    _two_paraphrases(tmp_path)

    out = _scan(tmp_path, capsys)

    assert judge.calls == []
    assert len(_clusters(out)) == 2
    stats = out.get("stats", {}) if isinstance(out, dict) else {}
    assert stats.get("unjudged_items") == 1
    assert stats.get("judged_calls") == 0


def test_promote_scan_uses_semantic_join_env_defaults(monkeypatch, tmp_path, capsys):
    judge = Judge(False)
    _install_judge(monkeypatch, judge)
    monkeypatch.delenv(BUDGET_ENV, raising=False)
    sj = _sj()
    budgets, call_kwargs = [], []

    class SpyBudget(sj.JudgeBudget):
        def __init__(self, total_s, *args, **kwargs):
            budgets.append(total_s)
            super().__init__(total_s, *args, **kwargs)

    real_clusters = sj.judged_clusters

    def spy_clusters(*args, **kwargs):
        call_kwargs.append(kwargs)
        return real_clusters(*args, **kwargs)

    monkeypatch.setattr(sj, "JudgeBudget", SpyBudget)
    monkeypatch.setattr(sj, "judged_clusters", spy_clusters)
    cache = tmp_path / "verdicts.json"
    monkeypatch.setenv(VERDICTS_ENV, str(cache))
    exp = _exp_dir(tmp_path)
    a, b, c = exp / "leaf-a.md", exp / "leaf-b.md", exp / "leaf-c.md"
    _leaf(a, desc=DESC_A, difficulty="the reconnect path ignores an expired token")
    _leaf(b, desc=DESC_B, difficulty="a stale token is reused after the link drops")
    _leaf(c, desc="authentication token expiry silently swallowed by the proxy",
          difficulty="the proxy hides an expired token from the client")
    _seed_yes(cache, _ground(a), _ground(b))

    out = _scan(tmp_path, capsys)

    assert sorted(sorted(g["members"]) for g in _clusters(out)) == [["leaf-a.md", "leaf-b.md"], ["leaf-c.md"]]
    assert judge.calls, "the uncached pair must reach the judge"
    assert len(call_kwargs) == 1
    assert not {"budget", "cache", "path", "cache_path"} & set(call_kwargs[0])
    assert budgets and set(budgets) == {300.0}
