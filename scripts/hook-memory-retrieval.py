#!/usr/bin/env python3
"""UserPromptSubmit hook: point the model at the memory leaves whose text overlaps the prompt.

Difficulty removed: a norm recorded in a memory leaf is not mobilized when its trigger
arrives, because a leaf is found only by someone who already suspects it exists. A BM25F
ranking of the prompt against every leaf nominates at most TOP_K pointers; the model
judges whether one bears on the task. A lexical score may order candidates and never
decides meaning (memory-global/leaves/regex-not-for-semantic-classification.md).

Advisory only: stdout is appended to the model's context for the turn, exit 0, never
blocks, no judge subprocess, no network. MIN_SCORE and BODY_PREFIX_CHARS are measured by
scripts/memory-retrieval-calibrate.py (docs/operations/memory-retrieval-calibration.json),
not chosen by hand.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from lib import bm25f  # noqa: E402
    from lib import config_root  # noqa: E402
except Exception:  # a broken or stale lib must never block a prompt: main() exits 0 below
    bm25f = None
    config_root = None
try:
    from lib import memory_retrieval_ledger  # noqa: E402
except Exception:  # the ledger is telemetry: its absence must not cost the pointers
    memory_retrieval_ledger = None

# Same literal as host_llm.JUDGE_CHILD_ENV_VAR (pinned by a test); host_llm is not imported
# because it pulls in judge machinery this hook never uses and runs on every prompt.
JUDGE_CHILD_ENV_VAR = "AGENTCTL_JUDGE_CHILD"
GLOBAL_ROOT_ENV = "CLAUDE_MEMORY_RETRIEVAL_GLOBAL_ROOT"
STATE_DIR_ENV = "CLAUDE_MEMORY_RETRIEVAL_STATE_DIR"

# Below this the prompt is the Chat class of CLAUDE.md ("ok", "thanks", "yes do it"): a score
# over one or two words is noise, so the scorer is not run.
MIN_QUERY_TOKENS = 3
# A pasted log would otherwise out-score every real match; the calibration queries are cut at
# the same length so the cutoff measured there holds here.
QUERY_TERM_CAP = 25
# Three pointers is what fits beside the prompt without becoming a reading list; recall is
# reported at the same k.
TOP_K = 3
# Measured: largest cutoff keeping recall@3 within 0.05 of the no-cutoff recall while
# suppressing every synthetic off-topic prompt (docs/operations/memory-retrieval-calibration.json).
MIN_SCORE = 5.32
# Measured: the smallest swept prefix whose recall is within 0.01 of the best among prefixes that
# keep the cutoff above every synthetic off-topic prompt; a longer body lets one-word matches
# outscore the cutoff.
BODY_PREFIX_CHARS = 500
# A pointer line is read on every matching prompt; the full description is one open away.
DESCRIPTION_MAX_CHARS = 160
# Leaves are read from disk on every prompt, and one corpus root may sit on a network mount; past
# this budget the leaves read so far are ranked and the rest are skipped, so the hook stays
# well inside its timeout (the calibration records the warm-disk p95 separately).
LOAD_BUDGET_S = 0.8

HEADER = (
    "[memory-retrieval] Candidate memory leaves matched lexically to this prompt: "
    "judge relevance yourself and open one only if it bears on the task."
)


class Leaf(NamedTuple):
    name: str
    description: str
    path: str
    doc: tuple


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fields = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip("\"'")
    return fields, text[end + 4:]


def build_leaf(text: str, path: str, body_prefix_chars: int = BODY_PREFIX_CHARS) -> Leaf:
    fields, body = split_frontmatter(text)
    name = fields.get("name") or Path(path).stem
    description = " ".join(fields.get("description", "").split())
    if not description:
        first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
        description = first
    title = bm25f.tokenize(f"{name} {description}")
    return Leaf(name, description, path, (title, bm25f.tokenize(body[:body_prefix_chars])))


def leaf_paths(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if p.name != "MEMORY.md")


def load_corpus(roots, body_prefix_chars: int = BODY_PREFIX_CHARS) -> list[Leaf]:
    seen = set()
    corpus = []
    deadline = time.monotonic() + LOAD_BUDGET_S
    for root in roots:
        if not root.is_dir():
            continue
        for path in leaf_paths(Path(os.path.realpath(root))):
            if time.monotonic() > deadline:
                return corpus
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                text = Path(real).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            corpus.append(build_leaf(text, real, body_prefix_chars))
    return corpus


def query_terms(prompt: str) -> list[str]:
    return list(dict.fromkeys(bm25f.content_tokens(bm25f.tokenize(prompt))))[:QUERY_TERM_CAP]


def rank(terms, corpus, min_score: float = MIN_SCORE, top_k: int = TOP_K) -> list:
    scores = bm25f.bm25f_scores(terms, [leaf.doc for leaf in corpus])
    ranked = sorted(
        ((score, idx) for idx, score in enumerate(scores) if score >= min_score),
        key=lambda t: (-t[0], t[1]),
    )
    return [(corpus[idx], score) for score, idx in ranked[:top_k]]


def pointer_line(leaf: Leaf) -> str:
    description = leaf.description
    if len(description) > DESCRIPTION_MAX_CHARS:
        description = description[: DESCRIPTION_MAX_CHARS - 3] + "..."
    return f"[memory-retrieval] {leaf.name} — {description} — {leaf.path}"


def corpus_roots(payload: dict) -> list[Path]:
    global_root = os.environ.get(GLOBAL_ROOT_ENV)
    roots = [Path(global_root) if global_root else config_root.agent_home() / "memory-global" / "leaves"]
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        roots.append(Path(cwd) / ".claude" / "agent-memory")
    return roots


def state_file(session_id) -> Path:
    base = os.environ.get(STATE_DIR_ENV)
    state_dir = Path(base) if base else Path.home() / ".local" / "state" / "claude-memory-retrieval"
    return state_dir / f"{config_root.sanitize_session_id(session_id)}.json"


def read_surfaced(path: Path) -> set:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def write_surfaced(path: Path, surfaced: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(sorted(surfaced), handle)
    os.replace(tmp, path)


def log_surface(payload: dict, prompt: str, fresh: list) -> None:
    if memory_retrieval_ledger is None:
        return
    session = payload.get("session_id")
    memory_retrieval_ledger.append(
        "surface",
        session=session if isinstance(session, str) else None,
        query=prompt,
        surfaced=[memory_retrieval_ledger.leaf_key(leaf.path) for leaf in fresh],
        nonce=memory_retrieval_ledger.extract_nonce(prompt),
    )


def run(payload: dict) -> str:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return ""
    terms = query_terms(prompt)
    if len(terms) < MIN_QUERY_TOKENS:
        return ""
    corpus = load_corpus(corpus_roots(payload))
    if not corpus:
        log_surface(payload, prompt, [])
        return ""
    hits = rank(terms, corpus)
    state = state_file(payload.get("session_id"))
    surfaced = read_surfaced(state)
    fresh = [leaf for leaf, _ in hits if leaf.path not in surfaced]
    log_surface(payload, prompt, fresh)
    if not fresh:
        return ""
    try:
        write_surfaced(state, surfaced | {leaf.path for leaf in fresh})
    except OSError:
        pass
    return "\n".join([HEADER, *(pointer_line(leaf) for leaf in fresh)])


def main() -> int:
    if os.environ.get(JUDGE_CHILD_ENV_VAR) or bm25f is None:
        return 0
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            out = run(payload)
            if out:
                print(out)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
