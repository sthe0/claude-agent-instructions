"""lib/semantic_join quality: BM25F nomination and a confirmed-YES contract for the judge."""
from __future__ import annotations

import hashlib
import importlib

from lib.judge_budget import JudgeBudget


def _sj():
    return importlib.import_module("lib.semantic_join")


def _cache_env(monkeypatch, tmp_path):
    path = tmp_path / "verdicts.json"
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_VERDICTS", str(path))
    return path


class Scripted:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, a, b, timeout=None):
        self.calls.append((a, b))
        return self.answers[len(self.calls) - 1], ""


def _ident(t):
    return t


def test_nominate_prefers_short_true_duplicate_over_long_unrelated():
    sj = _sj()
    query = "a disk quota is exceeded when the writer of the log flushes"
    short = "disk quota exceeded writer flushes"
    filler = "a the is of a the is of and that with for this from into over under about " * 12
    longs = [f"{filler} unrelated topic number {w}" for w in "abcdefghij"]
    out = sj.nominate(query=query, candidates=longs + [short], text_fn=_ident, k=5)
    assert short in out


def test_nominate_ignores_stopwords_and_substrings():
    sj = _sj()
    out = sj.nominate(query="a the of pattern",
                      candidates=["apathetic theory", "the pattern matcher"],
                      text_fn=_ident, k=5)
    assert "apathetic theory" not in out
    assert "the pattern matcher" in out


def test_nominate_weights_rare_terms_over_common():
    sj = _sj()
    zep = "widget zeppelin"
    cands = ["widget widget widget", "widget alpha one", "widget beta two",
             "widget gamma three", "widget delta four", zep]
    out = sj.nominate(query="widget zeppelin", candidates=cands, text_fn=_ident, k=1)
    assert out == [zep]


def test_nominate_title_field_outweighs_body():
    sj = _sj()
    body_hit = "beta alpha\ngadget gamma delta"
    title_hit = "gadget alpha\nbeta gamma delta"
    out = sj.nominate(query="gadget", candidates=[body_hit, title_hit], text_fn=_ident, k=1)
    assert out == [title_hit]


def test_unconfirmed_yes_is_not_a_match(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Scripted([True, False])
    cache = sj.same_difficulty_cache()
    r = sj.judged_match("disk full error", ["disk full on node"], judge=j,
                        cache=cache, budget=JudgeBudget(100, 1))
    assert r.outcome == "no-match"
    assert cache.get("disk full error", "disk full on node") is not True
    assert r.stats.judged_calls == 2


def test_confirmed_yes_is_cached_and_matched(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Scripted([True, True])
    cache = sj.same_difficulty_cache()
    r = sj.judged_match("disk full error", ["disk full on node"], judge=j,
                        cache=cache, budget=JudgeBudget(100, 1))
    assert r.outcome == "match" and r.stats.judged_calls == 2
    assert cache.get("disk full error", "disk full on node") is True
    again = Scripted([False])
    r2 = sj.judged_match("disk full error", ["disk full on node"], judge=again,
                         cache=cache, budget=JudgeBudget(100, 1))
    assert r2.outcome == "match" and again.calls == [] and r2.stats.cached_hits == 1


def test_confirmation_without_budget_is_unjudged(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    now = [0.0]
    calls = []

    def judge(a, b, timeout=None):
        calls.append(1)
        now[0] += 10
        return True, ""

    cache = sj.same_difficulty_cache()
    r = sj.judged_match("disk full error", ["disk full on node"], judge=judge, cache=cache,
                        budget=JudgeBudget(10, 1, clock=lambda: now[0]))
    assert r.outcome == "unjudged" and len(calls) == 1
    assert cache.get("disk full error", "disk full on node") is None


def test_single_call_cached_yes_is_not_served(monkeypatch, tmp_path):
    path = _cache_env(monkeypatch, tmp_path)
    sj = _sj()
    old_salt = "same-difficulty@" + sj.SAME_DIFFICULTY_JUDGE_PROMPT_SHA256
    a, b = "disk full error", "disk full on node"
    joined = "\x00".join([old_salt] + sorted([a, b]))
    path.write_text('{"%s": true}' % hashlib.sha256(joined.encode("utf-8")).hexdigest(),
                    encoding="utf-8")
    j = Scripted([False])
    r = sj.judged_match(a, [b], judge=j, budget=JudgeBudget(100, 1))
    assert j.calls != []
    assert r.outcome == "no-match"


def test_clusters_join_only_on_confirmed_yes(monkeypatch, tmp_path):
    _cache_env(monkeypatch, tmp_path)
    sj, j = _sj(), Scripted([True, False])
    res = sj.judged_clusters(["disk full on node", "node storage exhausted disk"], _ident,
                             judge=j, budget=JudgeBudget(100, 1))
    assert len(j.calls) == 2
    assert len(res.groups) == 2
