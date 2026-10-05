"""Judged "same difficulty?" decisions: lexical overlap nominates, a model judge decides.

Difficulty removed: a lexical ratio or keyword test that DECIDES whether two texts
mean the same thing is a lexical matcher doing a meaning job. Every site that needs
that decision (cluster grounds, dedup a filing, guard a new leaf) shares this one
contract, so it cannot drift per site:

* ``nominate`` only ORDERS candidates (positive overlap score, top-k, no threshold);
* byte-identical normalized texts join by identity, with no judge call;
* otherwise only a genuine judge verdict joins or matches, and a YES only when a second
  call confirms it (a YES the second answer contradicts is not cached); a fabricated verdict
  (non-empty ``reason``) never does and is never cached (fail-open);
* genuine verdicts are cached under a salted key, so a cached verdict is tied to the
  judge prompt that produced it;
* the judge runs under a whole-invocation ``JudgeBudget``; budget 0 is cache-only;
* items left undecided are counted separately, never as confirmed non-matches.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agentctl import advisor
from lib.judge_budget import JudgeBudget

SAME_DIFFICULTY_JUDGE_PROMPT_SHA256 = "5fefb6c30bf1c9cf53bc7e58e0b1109d6164466c7fd600ee181f0e536be54d71"
SAME_DIFFICULTY_JUDGE_SALT = "same-difficulty@" + SAME_DIFFICULTY_JUDGE_PROMPT_SHA256 + ":confirmed-yes"

VERDICTS_ENV = "CLAUDE_SAME_DIFFICULTY_VERDICTS"
DEFAULT_VERDICTS_PATH = "~/.local/state/claude-same-difficulty-verdicts.json"
BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
DEFAULT_BUDGET_S = 300.0
MIN_CALL_S = 1.0

K_CLUSTER = 3
K_FILING = 5
K_NEW_LEAF = 3

_FABRICATED_RAISED = "judge raised (fail-open)"

BM25_K1 = 1.2
BM25_B = 0.75
W_TITLE = 2.0
W_BODY = 1.0
MIN_WORD_LEN = 3
STOPWORDS = frozenset(
    "the and for are but not you all any can had her was one our out has his how its may new now "
    "old see two who did get got let put say she too use that this with from have been were they "
    "them then than there their what when which will would into over under about also such some "
    "very just only more most other these those while where being does done each both".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens; the query side of ``nominate``'s ranking."""
    return [t.lower() for t in re.findall(r"\w+", text)]


def term_score(haystack: str, terms: list[str]) -> int:
    """Count of ``terms`` occurrences in ``haystack``; the one ranking engine behind ``nominate``."""
    hay = haystack.lower()
    return sum(hay.count(t) for t in terms)


def norm(text: str) -> str:
    return " ".join(text.split())


def judge_enabled() -> bool:
    return os.environ.get("AGENTCTL_ADVISOR") != "0"


# --------------------------------------------------------------------------
# Test-only mutation seams. The stage-1 negative control redefines each of these
# one-liners on a scratch copy of the module to prove its tests go red, so every
# decision site must keep calling them by bare global name at call time. Do not
# inline them, alias them or bind them as default arguments.
# --------------------------------------------------------------------------
def _is_genuine(reason) -> bool:
    return reason == ""


def _passes_nomination(score) -> bool:
    return score > 0


def _budget_allows(budget) -> bool:
    return budget.remaining_and_timeout(advisor._BINARY_ASK_TIMEOUT_S)[1] is not None


def _identity_join(a, b) -> bool:
    return bool(norm(a)) and norm(a) == norm(b)


def _identity_scope(query, candidates, text_fn, k) -> list:
    return list(candidates)


def _cluster_identity_pool(placed, representatives) -> list:
    return list(placed)


def _cache_key(salt, texts) -> str:
    joined = "\x00".join([salt] + sorted(norm(t) for t in texts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _resolve_key(key_fn, salt, texts) -> str:
    if key_fn is not None:
        return key_fn(texts)
    return _cache_key(salt=salt, texts=texts)


def _ask_judge(judge, *args, **kwargs):
    try:
        return judge(*args, **kwargs)
    except Exception:
        return False, _FABRICATED_RAISED


# --------------------------------------------------------------------------
# verdict cache
# --------------------------------------------------------------------------
class VerdictCache:
    """A flat JSON ``{key: bool}`` file, one per judge. Only genuine verdicts are stored.

    Limit: ``put`` is an unlocked read-modify-write, so two processes writing at once can
    lose one verdict. The replace is atomic (no corrupt file) and a lost verdict only costs
    a repeated judge call.
    """

    def __init__(self, path, salt: str, key_fn: Callable[[tuple], str] | None = None) -> None:
        self.path = Path(path)
        self.salt = salt
        self.key_fn = key_fn
        self._memo: tuple[tuple[int, int], dict] | None = None

    def key(self, *texts: str) -> str:
        return _resolve_key(key_fn=self.key_fn, salt=self.salt, texts=texts)

    def _load(self) -> dict:
        # re-parsed only when the file's (mtime, size) changes, so a clustering pass over a
        # large corpus does not re-read a growing cache once per lookup
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if self._memo is not None and self._memo[0] == sig:
                return dict(self._memo[1])
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        data = {k: v for k, v in data.items() if isinstance(v, bool)}
        self._memo = (sig, data)
        return dict(data)

    def get(self, *texts: str) -> bool | None:
        key = self.key(*texts)
        try:
            st = self.path.stat()
        except OSError:
            return None
        if self._memo is not None and self._memo[0] == (st.st_mtime_ns, st.st_size):
            return self._memo[1].get(key)
        return self._load().get(key)

    def put(self, verdict: bool, *texts: str) -> None:
        data = self._load()
        data[self.key(*texts)] = bool(verdict)
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def same_difficulty_cache() -> VerdictCache:
    path = os.environ.get(VERDICTS_ENV) or os.path.expanduser(DEFAULT_VERDICTS_PATH)
    return VerdictCache(path=path, salt=SAME_DIFFICULTY_JUDGE_SALT)


def env_budget() -> JudgeBudget:
    try:
        total = float(os.environ[BUDGET_ENV])
    except (KeyError, ValueError):
        total = DEFAULT_BUDGET_S
    return JudgeBudget(total, MIN_CALL_S)


def default_judge(a: str, b: str, timeout: float):
    return advisor.judge_same_difficulty(
        a, b, advisor.subprocess_runner,
        enabled=judge_enabled(), timeout=max(1, int(timeout)),
    )


# --------------------------------------------------------------------------
# nomination, matching, clustering
# --------------------------------------------------------------------------
def bm25f_scores(query_tokens, docs) -> list:
    """BM25F (Robertson & Zaragoza 2009) of ``query_tokens`` over ``docs``, a list of
    ``(title_tokens, body_tokens)``; content words only, whole-token matches, idf over ``docs``."""
    def content(tokens):
        return [t for t in tokens if len(t) >= MIN_WORD_LEN and t not in STOPWORDS]

    terms = sorted(set(content(query_tokens)))
    fields = [(content(title), content(body)) for title, body in docs]
    n = len(fields)
    if not terms or n == 0:
        return [0.0] * n
    avg_title = sum(len(t) for t, _ in fields) / n
    avg_body = sum(len(b) for _, b in fields) / n
    df = {w: sum(1 for t, b in fields if w in t or w in b) for w in terms}
    out = []
    for title, body in fields:
        score = 0.0
        for w in terms:
            if not df[w]:
                continue
            tf = 0.0
            for weight, toks, avg in ((W_TITLE, title, avg_title), (W_BODY, body, avg_body)):
                if avg > 0 and toks:
                    tf += weight * toks.count(w) / (1 - BM25_B + BM25_B * len(toks) / avg)
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * tf / (BM25_K1 + tf)
        out.append(score)
    return out


def nominate(query, candidates, text_fn, k) -> list:
    """Order candidates by BM25F relevance to ``query``: positive score only, top ``k``, no threshold."""
    candidates = list(candidates)
    docs = []
    for cand in candidates:
        title, _, body = text_fn(cand).partition("\n")
        docs.append((tokenize(title), tokenize(body)))
    scores = bm25f_scores(tokenize(query), docs)
    scored = []
    for idx, cand in enumerate(candidates):
        score = scores[idx]
        if _passes_nomination(score):
            scored.append((-score, idx, cand))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [cand for _, _, cand in scored[:k]]


@dataclass
class JoinStats:
    judged_calls: int = 0
    cached_hits: int = 0
    identity_joins: int = 0
    unjudged_items: int = 0
    unjudged_pairs: int = 0

    def as_dict(self) -> dict:
        return {
            "judged_calls": self.judged_calls,
            "cached_hits": self.cached_hits,
            "identity_joins": self.identity_joins,
            "unjudged_items": self.unjudged_items,
            "unjudged_pairs": self.unjudged_pairs,
        }


def _decide_pair(a, b, *, judge, cache, budget, stats):
    """Genuine verdict for (a, b) from the cache or the judge, else None (pair undecided)."""
    cached = cache.get(a, b)
    if cached is not None:
        stats.cached_hits += 1
        return cached
    if not _budget_allows(budget):
        return None
    timeout = min(advisor._BINARY_ASK_TIMEOUT_S, budget.remaining())
    verdict, reason = _ask_judge(judge, a, b, timeout=timeout)
    if not _is_genuine(reason):
        return None
    stats.judged_calls += 1
    if verdict:
        if not _budget_allows(budget):
            return None
        timeout = min(advisor._BINARY_ASK_TIMEOUT_S, budget.remaining())
        again, reason = _ask_judge(judge, a, b, timeout=timeout)
        if not _is_genuine(reason):
            return None
        stats.judged_calls += 1
        if not again:
            return False
    cache.put(verdict, a, b)
    return verdict


@dataclass
class MatchResult:
    outcome: str  # "match" | "no-match" | "unjudged"
    candidate: object
    stats: JoinStats = field(default_factory=JoinStats)
    nominated: list = field(default_factory=list)


def judged_match(query, candidates, text_fn=None, *, judge=None, cache=None, budget=None,
                 k: int = K_FILING) -> MatchResult:
    """Does ``query`` (a text) match one of ``candidates`` (items; ``text_fn`` yields their text)?"""
    text_fn = text_fn or (lambda c: c)
    judge = judge or default_judge
    cache = cache if cache is not None else same_difficulty_cache()
    budget = budget if budget is not None else env_budget()
    stats = JoinStats()
    if not norm(query):
        stats.unjudged_items = 1
        return MatchResult("unjudged", None, stats)
    for cand in _identity_scope(query=query, candidates=candidates, text_fn=text_fn, k=k):
        if _identity_join(query, text_fn(cand)):
            stats.identity_joins += 1
            return MatchResult("match", cand, stats)
    undecided = False
    nominated = nominate(query=query, candidates=candidates, text_fn=text_fn, k=k)
    for cand in nominated:
        verdict = _decide_pair(query, text_fn(cand), judge=judge, cache=cache, budget=budget, stats=stats)
        if verdict is None:
            undecided = True
            stats.unjudged_pairs += 1
        elif verdict:
            return MatchResult("match", cand, stats, nominated)
    if undecided:
        stats.unjudged_items = 1
        return MatchResult("unjudged", None, stats, nominated)
    return MatchResult("no-match", None, stats, nominated)


@dataclass
class ClusterResult:
    groups: list
    stats: JoinStats
    unjudged: list  # per input item: not joined, and a nominated pair has no genuine verdict
    undecided: list  # per input item: an endpoint of a nominated pair with no genuine verdict


def judged_clusters(items, ground_fn, *, judge=None, cache=None, budget=None,
                    k: int = K_CLUSTER) -> ClusterResult:
    """Greedy grouping in input order: an item joins the first group whose representative
    it is identical to or judged the same as; otherwise it opens a group."""
    judge = judge or default_judge
    cache = cache if cache is not None else same_difficulty_cache()
    budget = budget if budget is not None else env_budget()
    stats = JoinStats()
    items = list(items)
    texts = [ground_fn(it) for it in items]
    text_of = texts.__getitem__
    n = len(items)
    group_of = [-1] * n
    reps: list[int] = []  # item index of each group's representative; group id == position
    placed: list[int] = []
    unjudged = [False] * n
    undecided = [False] * n

    for i in range(n):
        joined = -1
        pool = _cluster_identity_pool(placed=placed, representatives=reps)
        for j in _identity_scope(query=texts[i], candidates=pool, text_fn=text_of, k=k):
            if _identity_join(texts[i], texts[j]):
                joined = group_of[j]
                stats.identity_joins += 1
                break
        pending = False
        if joined < 0 and not norm(texts[i]):
            pending = True
        elif joined < 0:
            for j in nominate(query=texts[i], candidates=reps, text_fn=text_of, k=k):
                verdict = _decide_pair(texts[i], texts[j], judge=judge, cache=cache,
                                       budget=budget, stats=stats)
                if verdict is None:
                    pending = True
                    undecided[i] = undecided[j] = True
                    stats.unjudged_pairs += 1
                elif verdict:
                    joined = group_of[j]
                    break
        if joined < 0:
            if pending:
                unjudged[i] = True
                stats.unjudged_items += 1
            joined = len(reps)
            reps.append(i)
        group_of[i] = joined
        placed.append(i)

    groups: list[list] = [[] for _ in reps]
    for i in range(n):
        groups[group_of[i]].append(items[i])
    return ClusterResult(groups=groups, stats=stats, unjudged=unjudged, undecided=undecided)
