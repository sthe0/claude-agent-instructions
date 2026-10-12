#!/usr/bin/env python3
"""Derive the memory-retrieval hook's cutoff (MIN_SCORE) and body prefix from the memory corpus.

Difficulty removed: BM25F scores are unbounded, so a cutoff means something only against a
measured score distribution, and that distribution shifts as the corpus grows. This script
re-derives the cutoff from the repo's own memory-global/leaves/ and records it in
docs/operations/memory-retrieval-calibration.json, which the hook's test pins to the hook.

Sample: leave-one-out over the corpus. Each `### ` context block under `## Contexts` of an
experience leaf is a query whose target is the leaf it belongs to, scored against the corpus
with that block removed from the target. A pair is a hit when the target is in the top 3.
No session transcript is read (private prompt text) and no query text is written to the
output (the repo is public): only counts and scores are recorded.

Usage:
  python3 scripts/memory-retrieval-calibrate.py            # measure, write the JSON
  python3 scripts/memory-retrieval-calibrate.py --no-write # measure and print only

Exit: 0 recorded/printed; 2 no feasible cutoff or too few pairs (nothing written);
3 the hook's MIN_SCORE / BODY_PREFIX_CHARS differ from the measured ones (nothing written;
set them to the printed values and re-run).
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from lib import bm25f  # noqa: E402

LEAVES_ROOT = REPO_ROOT / "memory-global" / "leaves"
OUT_PATH = REPO_ROOT / "docs" / "operations" / "memory-retrieval-calibration.json"
HOOK_PATH = SCRIPTS_DIR / "hook-memory-retrieval.py"

SAMPLE_SOURCE = "memory-corpus-leave-one-out"
MIN_PAIRS = 20  # below this a recall figure is anecdote, not a measurement
TOP_K = 3
RECALL_TOLERANCE = 0.05  # the largest recall loss the cutoff may buy in exchange for suppressing noise
CALIBRATION_QUERY_TERMS = 10  # assumption: a one-sentence prompt carries about this many content words
QUERY_TERM_SENSITIVITY = (5, 10, 25)
PREFIX_CANDIDATES = (500, 1000, 2000, 4000, 8000)
PREFIX_RECALL_SLACK = 0.01  # a longer prefix must buy more than this much recall to be chosen
LATENCY_RUNS = 15
LATENCY_PROMPT = "how should the retrieval hook rank candidate leaves for this prompt"

# Synthetic off-topic prompts written for this script; none is a real user prompt.
NEGATIVE_PROMPTS = (
    "please format this table as csv with a header row",
    "rename the variable in that loop to something clearer",
    "what is the capital city of australia and its population",
    "write a haiku about autumn rain on a quiet street",
    "convert this temperature from fahrenheit to celsius for me",
    "translate the following paragraph into french please",
    "give me a recipe for banana bread without eggs",
    "how many days are there between march and september",
    "sort these names alphabetically and remove the duplicates",
    "summarize this article about volcanoes in three sentences",
    "explain how photosynthesis works to a ten year old",
    "suggest a good name for a small bakery on the corner",
    "i would like you to write a short story about a lighthouse keeper who finds a message "
    "in a bottle during a storm and decides to sail across the ocean to deliver it",
    "draw up a weekly meal plan for a family of four with vegetarian dinners and cheap "
    "lunches that can be packed for school and work",
)

BIAS_NOTE = (
    "Optimistic against live prompts: a context block is written in its leaf's own vocabulary "
    "and the other blocks of that leaf stay in the target, so this recall is an upper bound on "
    "what a free-form prompt achieves. The retrieval ledger measures live recall."
)


def load_hook():
    spec = importlib.util.spec_from_file_location("hook_memory_retrieval", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context_blocks(text: str) -> list[str]:
    """The full text of every `### ` block under the `## Contexts` heading."""
    blocks = []
    in_contexts = False
    current: list[str] | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            in_contexts = line.strip().lower() == "## contexts"
            if current is not None:
                blocks.append("".join(current))
                current = None
        elif line.startswith("### ") and in_contexts:
            if current is not None:
                blocks.append("".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append("".join(current))
    return blocks


def positive_pairs(corpus_paths: list[str], texts: dict, hook, n_terms: int) -> list:
    """(target index, query terms, target text with the block removed)."""
    index = {path: idx for idx, path in enumerate(corpus_paths)}
    experience = os.path.realpath(LEAVES_ROOT / "experience") + os.sep
    pairs = []
    for path in corpus_paths:
        if not path.startswith(experience):
            continue
        text = texts[path]
        for block in context_blocks(text):
            body = block.partition("\n")[2]
            terms = list(dict.fromkeys(bm25f.content_tokens(bm25f.tokenize(body))))[:n_terms]
            if len(terms) < hook.MIN_QUERY_TOKENS:
                continue
            pairs.append((index[path], terms, text.replace(block, "", 1)))
    return pairs


def hit_scores(pairs, corpus_paths, texts, hook, prefix: int) -> list:
    """Per pair: the target's score when it is in the top K, else None."""
    docs = [hook.build_leaf(texts[path], path, prefix).doc for path in corpus_paths]
    out = []
    for target, terms, stripped in pairs:
        trial = list(docs)
        trial[target] = hook.build_leaf(stripped, corpus_paths[target], prefix).doc
        scores = bm25f.bm25f_scores(terms, trial)
        better = sum(1 for idx, score in enumerate(scores)
                     if score > scores[target] or (score == scores[target] and idx < target))
        out.append(scores[target] if scores[target] > 0 and better < TOP_K else None)
    return out


def negative_top_scores(corpus_paths, texts, hook, prefix: int) -> list:
    docs = [hook.build_leaf(texts[path], path, prefix).doc for path in corpus_paths]
    out = []
    for prompt in NEGATIVE_PROMPTS:
        terms = hook.query_terms(prompt)
        assert len(terms) >= hook.MIN_QUERY_TOKENS, "negative prompt would never reach the scorer"
        out.append(max(bm25f.bm25f_scores(terms, docs)))
    return out


def recall(scores: list, cutoff: float = 0.0) -> float:
    return sum(1 for s in scores if s is not None and s >= cutoff) / len(scores)


def select_cutoff(scores: list, negative_max: float) -> dict:
    """Largest cutoff (floored to 0.01) keeping recall within RECALL_TOLERANCE of the
    no-cutoff recall; feasible only when it also lies above every negative's top score."""
    hits = sorted((s for s in scores if s is not None), reverse=True)
    need = math.ceil((recall(scores) - RECALL_TOLERANCE) * len(scores) - 1e-9)
    ceiling = hits[need - 1] if need >= 1 and hits else 0.0
    cutoff = math.floor(ceiling * 100) / 100
    return {
        "recall_no_cutoff": recall(scores),
        "recall_ceiling_score": ceiling,
        "cutoff": cutoff,
        "recall_with_cutoff": recall(scores, cutoff),
        "negative_max_score": negative_max,
        "feasible": cutoff > negative_max,
    }


def latency_ms(hook, runs: int) -> dict:
    payload = json.dumps({"prompt": LATENCY_PROMPT, "session_id": "calibration"})
    samples = []
    with tempfile.TemporaryDirectory() as state:
        env = {k: v for k, v in os.environ.items() if k != hook.JUDGE_CHILD_ENV_VAR}
        env[hook.GLOBAL_ROOT_ENV] = str(LEAVES_ROOT)
        env[hook.STATE_DIR_ENV] = state
        for run in range(runs):
            Path(state, "calibration.json").unlink(missing_ok=True)
            start = time.perf_counter()
            subprocess.run([sys.executable, str(HOOK_PATH)], input=payload, text=True,
                           capture_output=True, env=env, check=True)
            samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    pick = lambda q: round(samples[min(len(samples) - 1, round(q * (len(samples) - 1)))], 1)
    return {"p50": pick(0.5), "p95": pick(0.95), "runs": runs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-write", action="store_true", help="measure and print, do not write the JSON")
    parser.add_argument("--verbose", action="store_true", help="print per-negative top scores and hit-score quartiles")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    parser.add_argument("--runs", type=int, default=LATENCY_RUNS, help="fresh-process latency runs")
    args = parser.parse_args()

    hook = load_hook()
    paths = [os.path.realpath(p) for p in hook.leaf_paths(Path(os.path.realpath(LEAVES_ROOT)))]
    texts = {p: Path(p).read_text(encoding="utf-8", errors="replace") for p in paths}

    sweep = []
    pairs = positive_pairs(paths, texts, hook, CALIBRATION_QUERY_TERMS)
    for prefix in PREFIX_CANDIDATES:
        scores = hit_scores(pairs, paths, texts, hook, prefix)
        negatives = negative_top_scores(paths, texts, hook, prefix)
        if args.verbose:
            hits = sorted(s for s in scores if s is not None)
            quartiles = [round(hits[int(q * (len(hits) - 1))], 2) for q in (0, 0.25, 0.5, 0.75, 1)]
            print(f"  hit score quartiles {quartiles}")
            for prompt, top in zip(NEGATIVE_PROMPTS, negatives):
                print(f"  neg {top:6.3f}  terms={len(hook.query_terms(prompt)):>2}  {prompt[:50]}")
        sweep.append({"body_prefix_chars": prefix, "n_pairs": len(pairs), **select_cutoff(scores, max(negatives))})
        print(f"prefix={prefix:>5} pairs={len(pairs)} recall_all={sweep[-1]['recall_no_cutoff']:.3f} "
              f"ceiling={sweep[-1]['recall_ceiling_score']:.3f} cutoff={sweep[-1]['cutoff']:.2f} "
              f"neg_max={sweep[-1]['negative_max_score']:.3f} feasible={sweep[-1]['feasible']}")

    n_pairs = sweep[0]["n_pairs"]
    if n_pairs < MIN_PAIRS:
        print(f"INCOMPLETE: only {n_pairs} positive pairs (floor {MIN_PAIRS})", file=sys.stderr)
        return 2
    feasible = [row for row in sweep if row["feasible"]]
    if not feasible:
        print("INCOMPLETE: no prefix has a cutoff above every negative that keeps recall within "
              f"{RECALL_TOLERANCE}", file=sys.stderr)
        return 2
    best = max(row["recall_no_cutoff"] for row in feasible)
    chosen = next(row for row in feasible if row["recall_no_cutoff"] >= best - PREFIX_RECALL_SLACK)
    prefix, min_score = chosen["body_prefix_chars"], chosen["cutoff"]

    if hook.MIN_SCORE != min_score or hook.BODY_PREFIX_CHARS != prefix:
        print(f"hook constants stale: set MIN_SCORE = {min_score} and BODY_PREFIX_CHARS = {prefix} "
              f"(hook has {hook.MIN_SCORE} / {hook.BODY_PREFIX_CHARS}), then re-run", file=sys.stderr)
        return 3

    by_terms = {}
    for n_terms in QUERY_TERM_SENSITIVITY:
        pairs = positive_pairs(paths, texts, hook, n_terms)
        scores = hit_scores(pairs, paths, texts, hook, prefix)
        by_terms[str(n_terms)] = {
            "n_pairs": len(pairs),
            "no_cutoff": round(recall(scores), 3),
            "with_cutoff": round(recall(scores, min_score), 3),
        }
    result = {
        "sample_source": SAMPLE_SOURCE,
        "date": datetime.date.today().isoformat(),
        "corpus": {"root": "memory-global/leaves", "leaves": len(paths)},
        "n_positive_pairs": n_pairs,
        "n_negative_prompts": len(NEGATIVE_PROMPTS),
        "top_k": TOP_K,
        "query_terms": CALIBRATION_QUERY_TERMS,
        "recall_at_3": {
            "no_cutoff": round(chosen["recall_no_cutoff"], 3),
            "with_cutoff": round(chosen["recall_with_cutoff"], 3),
        },
        "min_score": min_score,
        "max_negative_score": round(chosen["negative_max_score"], 3),
        "body_prefix_chars": prefix,
        "prefix_sweep": [
            {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()} for row in sweep
        ],
        "recall_by_query_terms": by_terms,
        "latency_ms": latency_ms(hook, args.runs),
        "bias": BIAS_NOTE,
    }
    text = json.dumps(result, indent=2, sort_keys=False) + "\n"
    if args.no_write:
        print(text, end="")
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
