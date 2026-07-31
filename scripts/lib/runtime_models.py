"""Per-host model maps: the pure data a caller needs to translate a task's
difficulty tier into a concrete `--model` value for whichever coordination host
(Claude Code's `claude -p`, Cursor's `agent -p`) it is bound to.

No host-detection or process-spawning logic lives here — only the maps and
their lookup, so `agentctl.runtime_host` (session binding) and `lib.host_llm`
(argv assembly) both depend on this module rather than on each other.
"""
from __future__ import annotations

HOST_CLAUDE = "claude"
HOST_CURSOR = "cursor"
HOSTS = (HOST_CLAUDE, HOST_CURSOR)

# Claude Code model aliases — the same ladder spawn-specialist.py's
# COMPLEXITY_MODEL uses (--complexity low/medium/high -> haiku/sonnet/opus), so
# the advisor/judge/marker-extractor tiers stay consistent with the spawn tiers.
CLAUDE_COMPLEXITY_MODEL = {"low": "haiku", "medium": "sonnet", "high": "opus"}

# Cursor `agent -p --model` slugs. "medium" mirrors the existing
# spawn-cursor-specialist.py/spawn-cursor-escape.py default (composer-2.5).
# "low" drops to the fast/cheap variant of that same family for high-volume
# judge-style calls. "high" steps up to a stronger non-Anthropic model for
# harder reasoning: Cursor's catalog (unlike Claude's single-family ladder) has
# no one family spanning cheap -> frontier, so the top tier is a distinct slug
# rather than a bigger member of the composer family.
CURSOR_COMPLEXITY_MODEL = {
    "low": "composer-2.5-fast",
    "medium": "composer-2.5",
    "high": "gpt-5.3-codex",
}

COMPLEXITY_MODEL_BY_HOST = {
    HOST_CLAUDE: CLAUDE_COMPLEXITY_MODEL,
    HOST_CURSOR: CURSOR_COMPLEXITY_MODEL,
}


def model_for(host: str, complexity: str) -> str:
    """Resolve `host`'s model for a `low`/`medium`/`high` complexity tier."""
    try:
        table = COMPLEXITY_MODEL_BY_HOST[host]
    except KeyError:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}") from None
    try:
        return table[complexity]
    except KeyError:
        raise ValueError(
            f"unknown complexity {complexity!r}; must be one of {tuple(table)}"
        ) from None
