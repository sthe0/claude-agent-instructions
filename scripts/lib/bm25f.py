"""The repo's one BM25F scorer: ranks candidate texts against a query by word overlap.

Difficulty removed: two lexical retrievers (the same-difficulty nominator and the
prompt-time memory retriever) each carrying their own scorer would drift apart, and
a cutoff calibrated on one would silently mean something else on the other.

This module is pure — no judge, no agentctl, no I/O — so a hook can import it
without paying for the judge machinery. A score only ORDERS candidates; it never
decides meaning (memory-global/leaves/regex-not-for-semantic-classification.md).
"""
from __future__ import annotations

import math
import re
from typing import Callable, Iterable

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
    """Lowercased word tokens; the query side of the ranking."""
    return [t.lower() for t in re.findall(r"\w+", text)]


def content_tokens(tokens: Iterable[str]) -> list[str]:
    """The tokens the scorer actually matches on: long enough and not a stopword."""
    return [t for t in tokens if len(t) >= MIN_WORD_LEN and t not in STOPWORDS]


def bm25f_scores(query_tokens, docs) -> list:
    """BM25F (Robertson & Zaragoza 2009) of ``query_tokens`` over ``docs``, a list of
    ``(title_tokens, body_tokens)``; content words only, whole-token matches, idf over ``docs``."""
    terms = sorted(set(content_tokens(query_tokens)))
    fields = [(content_tokens(title), content_tokens(body)) for title, body in docs]
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


def split_docs(texts: Iterable[str]) -> list:
    """``(title_tokens, body_tokens)`` per text; the title is the text's first line."""
    docs = []
    for text in texts:
        title, _, body = text.partition("\n")
        docs.append((tokenize(title), tokenize(body)))
    return docs


def nominate_scored(query: str, candidates, text_fn: Callable, *,
                    scorer: Callable | None = None,
                    passes: Callable | None = None) -> list:
    """``(candidate, score)`` pairs for the candidates scoring positive against ``query``,
    best first (ties keep input order); no top-k, no threshold — the caller cuts.

    ``scorer`` and ``passes`` default to this module's ``bm25f_scores`` and ``score > 0``,
    looked up at call time; a caller with its own mutation seams passes them in.
    """
    candidates = list(candidates)
    docs = split_docs(text_fn(cand) for cand in candidates)
    scores = (scorer or bm25f_scores)(tokenize(query), docs)
    keep = passes or (lambda score: score > 0)
    scored = [(-scores[idx], idx, cand) for idx, cand in enumerate(candidates) if keep(scores[idx])]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [(cand, -neg) for neg, _, cand in scored]
