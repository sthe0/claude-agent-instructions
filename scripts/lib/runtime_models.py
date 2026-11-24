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
# A single-call host only: a session is not bound to it through `HOSTS`; its model per tier is
# the per-machine setting `opencode-model-<tier>` (lib/opencode_env.py), None when unset.
HOST_OPENCODE = "opencode"

# Claude Code model aliases — the same ladder spawn-specialist.py's
# COMPLEXITY_MODEL uses (--complexity low/medium/high -> haiku/sonnet/opus).
CLAUDE_COMPLEXITY_MODEL = {"low": "haiku", "medium": "sonnet", "high": "opus"}

# Cursor: None means omit --model so the CLI picks Auto.
CURSOR_COMPLEXITY_MODEL = {"low": None, "medium": None, "high": None}

COMPLEXITY_MODEL_BY_HOST = {
    HOST_CLAUDE: CLAUDE_COMPLEXITY_MODEL,
    HOST_CURSOR: CURSOR_COMPLEXITY_MODEL,
}


def model_for(host: str, complexity: str) -> str | None:
    """Resolve `host`'s model for a `low`/`medium`/`high` complexity tier."""
    if host == HOST_OPENCODE:
        from .opencode_env import model_setting

        return model_setting(complexity)
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
