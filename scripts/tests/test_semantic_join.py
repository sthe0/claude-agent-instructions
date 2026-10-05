"""lib/semantic_join: nominate orders, a genuine judge verdict decides, unjudged is counted apart."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

from agentctl import advisor
from lib.judge_budget import JudgeBudget

SCRIPTS = Path(__file__).resolve().parent.parent


def _sj():
    return importlib.import_module("lib.semantic_join")


def _cache_env(monkeypatch, tmp_path, name="verdicts.json"):
    path = tmp_path / name
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_VERDICTS", str(path))
    return path


def _key(salt, *texts):
    joined = "\x00".join([salt] + sorted(" ".join(t.split()) for t in texts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _d1(*texts):
    return _key(_sj().SAME_DIFFICULTY_JUDGE_SALT, *texts)


class Judge:
    def __init__(self, answer=False, reason=""):
        self.answer, self.reason = answer, reason
        self.calls = []

    def __call__(self, a, b, timeout=None):
        self.calls.append((a, b, timeout))
        if callable(self.answer):
            return self.answer(a, b), self.reason
        return self.answer, self.reason


def _live():
    return JudgeBudget(100, 1)


def _zero():
    return JudgeBudget(0, 1)


def test_judged_match_yes_returns_match(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    r = sj.judged_match("disk full error", ["disk full on node", "unrelated"], judge=j, budget=_live())
    assert r.outcome == "match" and r.candidate == "disk full on node"
    assert r.stats.judged_calls == 2
    assert r.nominated == ["disk full on node"]


def test_judged_match_no_returns_no_match(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(False)
    r = sj.judged_match("disk full error", ["disk full on node", "error on disk"], judge=j, budget=_live())
    assert r.outcome == "no-match" and r.candidate is None
    assert r.stats.judged_calls == 2 and r.stats.unjudged_items == 0
    assert sorted(r.nominated) == ["disk full on node", "error on disk"]


def test_judged_match_fabricated_verdict_is_unjudged(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True, "judge unavailable (fail-open)")
    r = sj.judged_match("disk full error", ["disk full on node"], judge=j, budget=_live())
    assert r.outcome == "unjudged" and r.candidate is None
    assert r.stats.judged_calls == 0 and r.stats.unjudged_items == 1
    assert r.nominated == ["disk full on node"]
    assert not path.exists()


def test_judged_match_unjudged_without_judge_or_cache(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    r = sj.judged_match("disk full error", ["disk full on node", "error on disk"], judge=j, budget=_zero())
    assert r.outcome == "unjudged"
    assert j.calls == []
    assert r.stats.unjudged_items == 1 and r.stats.unjudged_pairs == 2


def test_judge_no_keeps_near_identical_grounds_apart(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(False)
    res = sj.judged_clusters(["disk full error on node a", "disk full error on node b"],
                             lambda t: t, judge=j, budget=_live())
    assert len(res.groups) == 2


def test_identical_normalized_grounds_join_without_judge_call(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(False)
    res = sj.judged_clusters(["disk full", "disk  full "], lambda t: t, judge=j, budget=_live())
    assert len(res.groups) == 1 and len(res.groups[0]) == 2
    assert j.calls == [] and res.stats.identity_joins == 1
    assert not path.exists()


def test_judged_match_identity_without_judge_call(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    for budget in (_live(), _zero()):
        j = Judge(False)
        r = sj.judged_match("disk full error", ["disk  full   error "], judge=j, budget=budget)
        assert r.outcome == "match" and r.candidate == "disk  full   error "
        assert j.calls == [] and r.stats.identity_joins == 1
    assert not path.exists()


def test_identity_join_survives_outranked_nomination(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    longer = ["disk full disk full x", "disk full disk full y", "disk full disk full z"]
    ident = "disk  full "
    cands = longer + [ident]
    nominated = sj.nominate(query="disk full", candidates=cands, text_fn=lambda t: t, k=3)
    assert nominated == longer
    j = Judge(False)
    r = sj.judged_match("disk full", cands, judge=j, budget=_live(), k=3)
    assert r.outcome == "match" and r.candidate == ident
    assert j.calls == [] and r.stats.identity_joins == 1

    j = Judge(False)
    res = sj.judged_clusters(["disk full"] + longer + [ident], lambda t: t, judge=j, budget=_zero(), k=3)
    assert j.calls == []
    assert res.stats.identity_joins == 1
    assert res.groups[0] == ["disk full", ident]
    assert not path.exists()


def test_identity_join_reaches_non_representative_member(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    a, b = "disk full", "storage exhausted on disk"
    d, e, f = ("storage exhausted disk storage exhausted disk " + s for s in "xyz")
    c = "storage  exhausted on  disk "
    sj.same_difficulty_cache().put(True, a, b)
    assert sj.nominate(query=c, candidates=[a, d, e, f], text_fn=lambda t: t, k=3) == [d, e, f]
    assert sj._identity_join(c, b) and not sj._identity_join(c, a)
    j = Judge(False)
    items = [a, b, d, e, f, c]
    res = sj.judged_clusters(items, lambda t: t, judge=j, budget=_zero(), k=3)
    assert j.calls == []
    group = next(g for g in res.groups if c in g)
    assert set(group) == {a, b, c}
    assert res.stats.identity_joins == 1 and res.stats.cached_hits == 1
    assert res.unjudged[5] is False


def test_judge_yes_joins_nominated_paraphrase(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    res = sj.judged_clusters(["disk full on node", "node storage exhausted disk"], lambda t: t,
                             judge=Judge(True), budget=_live())
    assert len(res.groups) == 1 and len(res.groups[0]) == 2


def test_fabricated_verdict_never_joins_and_counts_unjudged(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    res = sj.judged_clusters(["disk full on node", "node storage exhausted disk"], lambda t: t,
                             judge=Judge(True, "x (fail-open)"), budget=_live())
    assert len(res.groups) == 2
    assert res.stats.unjudged_items == 1 and res.stats.judged_calls == 0
    assert not path.exists()


def test_cached_verdict_skips_the_judge(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    sj.same_difficulty_cache().put(True, "disk full error", "disk full on node")
    j = Judge(False)
    r = sj.judged_match("disk full error", ["disk full on node"], judge=j, budget=_live())
    assert r.outcome == "match" and j.calls == [] and r.stats.cached_hits == 1


def test_budget_zero_is_cache_only(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    sj.same_difficulty_cache().put(True, "disk full error", "disk full on node")
    j = Judge(True)
    r = sj.judged_match("disk full error", ["disk full on node"], judge=j, budget=_zero())
    assert r.outcome == "match" and r.stats.cached_hits == 1
    r = sj.judged_match("error on disk", ["disk full on node"], judge=j, budget=_zero())
    assert r.outcome == "unjudged"
    assert j.calls == []


def test_call_count_bounded_by_k_per_item(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(False)
    items = [f"disk issue {w}" for w in ("alpha", "beta", "gamma", "delta", "eps", "zeta")]
    sj.judged_clusters(items, lambda t: t, judge=j, budget=_live(), k=3)
    assert 0 < len(j.calls) <= 3 * (len(items) - 1)


def test_nominate_orders_only_without_threshold():
    sj = _sj()
    cands = ["alpha", "alpha alpha beta", "zzz", "beta beta"]
    out = sj.nominate(query="alpha beta", candidates=cands, text_fn=lambda t: t, k=5)
    assert out == ["alpha alpha beta", "beta beta", "alpha"]
    assert "zzz" not in out
    assert sj.nominate(query="alpha beta", candidates=cands, text_fn=lambda t: t, k=1) == ["alpha alpha beta"]


def test_budget_is_judge_budget(monkeypatch):
    sj = _sj()
    monkeypatch.delenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", raising=False)
    b = sj.env_budget()
    assert isinstance(b, JudgeBudget) and 299 < b.remaining() <= 300
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", "7")
    assert 6 < sj.env_budget().remaining() <= 7


def test_cache_key_contract(monkeypatch, tmp_path):
    sj = _sj()
    c = sj.same_difficulty_cache()
    assert c.key("a  b", " c") == _key(sj.SAME_DIFFICULTY_JUDGE_SALT, "a b", "c")
    assert c.key("a", "b") == c.key("b", "a")
    monkeypatch.delenv("CLAUDE_SAME_DIFFICULTY_VERDICTS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sj.same_difficulty_cache().path == tmp_path / ".local/state/claude-same-difficulty-verdicts.json"
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_VERDICTS", str(tmp_path / "x.json"))
    assert sj.same_difficulty_cache().path == tmp_path / "x.json"


def test_salt_pinned_to_judge_prompt():
    sj = _sj()
    seen = []

    def runner(argv, *, timeout=None, stdin=""):
        seen.append(stdin)
        return advisor.RunResult(0, stdout="YES", stderr="")

    verdict, reason = advisor.judge_same_difficulty("fixed ground one", "fixed ground two", runner)
    assert (verdict, reason) == (True, "")
    assert hashlib.sha256(seen[0].encode("utf-8")).hexdigest() == sj.SAME_DIFFICULTY_JUDGE_PROMPT_SHA256
    assert sj.SAME_DIFFICULTY_JUDGE_SALT == "same-difficulty@" + sj.SAME_DIFFICULTY_JUDGE_PROMPT_SHA256 + ":confirmed-yes"


def test_judge_enabled_follows_agentctl_advisor(monkeypatch):
    sj = _sj()
    monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
    assert sj.judge_enabled() is True
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    assert sj.judge_enabled() is False
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    assert sj.judge_enabled() is True


def test_default_judge_wraps_advisor_subprocess_runner(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    calls = []

    def stub(argv, *, timeout=None, stdin=""):
        calls.append((timeout, stdin))
        return advisor.RunResult(0, stdout="YES", stderr="")

    monkeypatch.setattr(advisor, "subprocess_runner", stub)

    def budget(total):
        return JudgeBudget(total, 1, clock=lambda: 0.0)

    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    r = sj.judged_match("disk full error", ["disk full on node"], budget=budget(7))
    assert r.outcome == "unjudged" and r.stats.unjudged_items == 1 and r.stats.judged_calls == 0
    assert calls == [] and not path.exists()

    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    r = sj.judged_match("disk full error", ["disk full on node"], budget=budget(7))
    assert r.outcome == "match" and r.stats.judged_calls == 2
    assert json.loads(path.read_text()) == {_d1("disk full error", "disk full on node"): True}
    assert len(calls) == 2
    assert "disk full error" in calls[0][1] and "disk full on node" in calls[0][1]
    assert calls[0][0] == calls[1][0] == min(advisor._BINARY_ASK_TIMEOUT_S, 7.0) == 7

    r = sj.judged_match("lock timeout error", ["lock timeout on db"], budget=budget(500))
    assert r.outcome == "match" and len(calls) == 4
    assert calls[2][0] == calls[3][0] == min(advisor._BINARY_ASK_TIMEOUT_S, 500.0) == 185


def test_corrupt_cache_reads_empty(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    path.write_text("{not json", encoding="utf-8")
    sj = _sj()
    assert sj.same_difficulty_cache().get("a", "b") is None
    r = sj.judged_match("disk full error", ["disk full on node"], judge=Judge(True), budget=_live())
    assert r.outcome == "match"
    assert json.loads(path.read_text()) == {_d1("disk full error", "disk full on node"): True}


def test_genuine_verdicts_are_written_to_the_cache(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    j = Judge(lambda a, b: "yes-pair" in a)
    r1 = sj.judged_match("yes-pair disk", ["disk one"], judge=j, budget=_live())
    r2 = sj.judged_match("no-pair disk", ["disk two"], judge=j, budget=_live())
    assert r1.outcome == "match" and r2.outcome == "no-match"
    assert json.loads(path.read_text()) == {
        _d1("yes-pair disk", "disk one"): True,
        _d1("no-pair disk", "disk two"): False,
    }
    j2 = Judge(False)
    r = sj.judged_match("yes-pair disk", ["disk one"], judge=j2, budget=_live())
    assert r.outcome == "match" and j2.calls == [] and r.stats.cached_hits == 1


def test_budget_exhaustion_counts_remaining_pairs_unjudged(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    now = [0.0]

    def judge(a, b, timeout=None):
        calls.append(timeout)
        now[0] += 10
        return False, ""

    calls = []
    budget = JudgeBudget(10, 1, clock=lambda: now[0])
    items = [f"disk issue {w}" for w in ("alpha", "beta", "gamma", "delta")]
    res = sj.judged_clusters(items, lambda t: t, judge=judge, budget=budget, k=3)
    assert calls == [min(advisor._BINARY_ASK_TIMEOUT_S, 10)]
    assert res.stats.judged_calls == 1
    assert res.stats.unjudged_pairs == 5 and res.stats.unjudged_items == 2
    assert res.unjudged == [False, False, True, True]

    calls.clear()
    now[0] = 0.0
    big = JudgeBudget(500, 1, clock=lambda: now[0])
    sj.judged_match("disk issue alpha", ["disk issue omega"], judge=judge, budget=big)
    assert calls == [min(advisor._BINARY_ASK_TIMEOUT_S, 500)]


def test_raising_judge_is_unjudged_and_not_cached(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()

    def boom(a, b, timeout=None):
        raise RuntimeError("judge down")

    caught = None
    try:
        r = sj.judged_match("disk full error", ["disk full on node"], judge=boom, budget=_live())
    except Exception as exc:  # noqa: BLE001
        caught = exc
    assert caught is None
    assert r.outcome == "unjudged" and r.stats.judged_calls == 0 and r.stats.unjudged_items == 1
    assert not path.exists()


def test_overlap_helpers_exported_and_used_by_nominate(monkeypatch):
    sj = _sj()
    spec = importlib.util.spec_from_file_location("record_experience_ref", SCRIPTS / "record-experience.py")
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    text = "Disk full on node, Disk!"
    assert sj.tokenize(text) == ref.tokenize(text)
    assert sj.term_score(text, ["disk", "node"]) == ref.term_score(text, ["disk", "node"])
    cands = ["disk", "disk disk node", "node node node node"]
    assert sj.nominate(query="disk node", candidates=cands, text_fn=lambda t: t, k=5)[0] == "disk disk node"
    seen = []

    def fixed(query_tokens, docs):
        seen.append((query_tokens, docs))
        return [1.0, 3.0, 2.0]

    monkeypatch.setattr(sj, "bm25f_scores", fixed)
    assert sj.nominate(query="disk node", candidates=cands, text_fn=lambda t: t, k=5) == [cands[1], cands[2], cands[0]]
    assert seen[0] == (["disk", "node"], [(["disk"], []), (["disk", "disk", "node"], []),
                                          (["node"] * 4, [])])
    monkeypatch.setattr(sj, "bm25f_scores", lambda q, d: [3.0, 1.0, 2.0])
    assert sj.nominate(query="disk node", candidates=cands, text_fn=lambda t: t, k=5) == [cands[0], cands[2], cands[1]]


def test_unjudged_items_counts_items_not_pairs(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    items = [f"disk {w}" for w in ("alpha", "beta", "gamma", "delta", "eps")]
    res = sj.judged_clusters(items, lambda t: t, judge=j, budget=_zero(), k=3)
    assert j.calls == []
    assert res.stats.unjudged_pairs == 9 and res.stats.unjudged_items == 4 <= len(items)
    assert res.unjudged == [False, True, True, True, True]
    assert res.undecided == [True] * 5
    assert len(res.groups) == 5 and all(len(g) == 1 for g in res.groups)


def test_item_with_one_no_and_one_unjudged_pair_is_unjudged(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    a, b, c = "disk alpha", "disk beta", "disk gamma"
    cache = sj.same_difficulty_cache()
    cache.put(False, a, b)
    cache.put(False, a, c)
    res = sj.judged_clusters([a, b, c], lambda t: t, judge=j, budget=_zero(), k=3)
    assert j.calls == [] and len(res.groups) == 3
    assert res.stats.cached_hits == 2 and res.stats.unjudged_pairs == 1 and res.stats.unjudged_items == 1
    assert res.unjudged == [False, False, True]
    assert res.undecided == [False, True, True]
    r = sj.judged_match(c, [a, b], judge=j, budget=_zero())
    assert r.outcome == "unjudged" and r.stats.unjudged_items == 1


def test_failed_cache_write_leaves_previous_file_intact(monkeypatch, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    path = _cache_env(monkeypatch, tmp_path, "sub/verdicts.json")
    sj = _sj()
    seeded = {"a" * 64: True}
    path.write_text(json.dumps(seeded), encoding="utf-8")
    before = path.read_bytes()

    def half_dump(obj, fh, *a, **kw):
        text = json.dumps(obj)
        fh.write(text[: len(text) // 2])
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", half_dump)
    caught = None
    try:
        r = sj.judged_match("disk full error", ["disk full on node"], judge=Judge(True), budget=_live())
    except Exception as exc:  # noqa: BLE001
        caught = exc
    assert caught is None
    assert r.outcome == "match"
    assert path.read_bytes() == before
    assert json.loads(path.read_text()) == seeded
    assert [p.name for p in sub.iterdir()] == ["verdicts.json"]


def test_verdict_cache_parametric_single_text_key(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    p1, p2 = tmp_path / "one.json", tmp_path / "two.json"
    c1 = sj.VerdictCache(path=p1, salt="salt-one@x")
    c2 = sj.VerdictCache(path=p2, salt="salt-two@x")
    t = "  inner   outer text "
    k1 = hashlib.sha256(("salt-one@x" + "\x00" + "inner outer text").encode("utf-8")).hexdigest()
    k2 = hashlib.sha256(("salt-two@x" + "\x00" + "inner outer text").encode("utf-8")).hexdigest()
    assert c1.key(t) == k1 and c2.key(t) == k2 and k1 != k2
    same = sj.VerdictCache(path=tmp_path / "sd.json", salt=sj.SAME_DIFFICULTY_JUDGE_SALT)
    assert same.key("a b", "c") == _d1("a b", "c")
    c1.put(True, t)
    assert json.loads(p1.read_text()) == {k1: True}
    assert not p2.exists()
    assert c2.get(t) is None and c1.get(t) is True


def test_verdict_cache_key_fn_overrides_key(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()

    def kf(texts):
        return hashlib.sha256(texts[0].encode("utf-8")).hexdigest()

    p3, p4 = tmp_path / "three.json", tmp_path / "four.json"
    c3 = sj.VerdictCache(path=p3, salt="", key_fn=kf)
    t = "  inner   outer text "
    assert c3.key(t) == hashlib.sha256(t.encode("utf-8")).hexdigest()
    assert c3.key(t) != _key("", t)
    c3.put(False, t)
    assert json.loads(p3.read_text()) == {kf((t,)): False}
    assert c3.get(t) is False
    u = "another  text"
    p4.write_text(json.dumps({kf((u,)): True}), encoding="utf-8")
    assert sj.VerdictCache(path=p4, salt="", key_fn=kf).get(u) is True


def test_budget_below_call_floor_issues_no_judge_call(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    r = sj.judged_match("disk full", ["disk full on node"], judge=j, budget=JudgeBudget(0.5, 1.0))
    assert r.outcome == "unjudged" and j.calls == []
    assert r.stats.unjudged_pairs == 1 and r.stats.unjudged_items == 1


def test_empty_texts_never_join_by_identity(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    r = sj.judged_match("   ", ["", "  "], judge=j, budget=_live())
    assert r.outcome == "unjudged" and r.candidate is None and j.calls == []
    assert r.stats.identity_joins == 0 and r.stats.unjudged_items == 1
    res = sj.judged_clusters(["", "  ", "disk full"], lambda t: t, judge=j, budget=_live())
    assert len(res.groups) == 3 and j.calls == []
    assert res.stats.identity_joins == 0
    assert res.unjudged == [True, True, False] and res.stats.unjudged_items == 2


def test_judged_match_yes_after_undecided_pair_is_a_match(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    seen = []

    def judge(a, b, timeout=None):
        seen.append(b)
        return (True, "") if len(seen) > 1 else (False, "unavailable (fail-open)")

    r = sj.judged_match("disk full error", ["disk full error on node", "disk full"],
                        judge=judge, budget=_live())
    assert r.outcome == "match" and r.candidate == "disk full"
    assert r.stats.unjudged_pairs == 1 and r.stats.unjudged_items == 0


def test_cached_no_is_served_as_no_match_without_judge(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(True)
    path.write_text(json.dumps({_d1("disk full", "disk full on node"): False}), encoding="utf-8")
    r = sj.judged_match("disk full", ["disk full on node"], judge=j, budget=_zero())
    assert r.outcome == "no-match" and j.calls == []
    assert r.stats.cached_hits == 1 and r.stats.unjudged_items == 0


def test_k_bounds_judge_calls_in_judged_match(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Judge(False)
    cands = [f"disk full variant {w}" for w in "abcdef"]
    r = sj.judged_match("disk full", cands, judge=j, budget=_live(), k=2)
    assert r.outcome == "no-match" and len(j.calls) == 2


def test_cache_get_sees_a_write_by_another_instance(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    p = tmp_path / "shared.json"
    reader = sj.VerdictCache(path=p, salt="s@x")
    assert reader.get("t") is None
    sj.VerdictCache(path=p, salt="s@x").put(True, "t")
    assert reader.get("t") is True
    sj.VerdictCache(path=p, salt="s@x").put(False, "u")
    assert reader.get("t") is True and reader.get("u") is False
