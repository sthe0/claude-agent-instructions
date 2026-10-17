"""The prompt ceiling a spawn is refused above (`dispatch_prompt_ceiling_chars`).

Lives under `scripts/lib/` rather than in `spawn-specialist.py` (the launcher) for the
reason `kind_baselines` does: the launcher imports `agentctl.render`, so `agentctl.render`
loading the launcher script to read the ceiling would make engine and launcher depend on
each other. This module has no such dependency and is imported from either side;
`spawn-specialist.py` re-exports every name below so callers and tests that read them off
the launcher module keep working.
"""
from __future__ import annotations

# Absolute context ceiling before auto-compaction (tokens) — our own intent, not a
# copy of anything Anthropic owns. It reaches the child via `claude --settings`
# (highest in the settings precedence ladder) rather than the child's process env,
# because an env entry in ~/.claude/settings.json is applied after process start and
# would win over process env. See memory-global leaves autocompact-threshold-policy.md
# and claude-code-settings-env-precedence.md.
AUTOCOMPACT_CEILING_TOKENS = 150_000

# The client resolves the effective window as min(model max, configured), then fires
# auto-compaction at min(round((window - 20000) * (1 - frac)), (window - 20000) - 13000).
# Pinning the WINDOW rather than a percentage lets that min() do the per-model work,
# so no per-model window table is needed here: a Haiku child clamps itself to its own
# 200k maximum with nothing about Haiku recorded in this file.
# Residual exposure, stated rather than hidden: `frac` is server-driven (flags
# tengu_amber_moleskin / tengu_amber_rokovoko in client 2.1.220), so a fraction change
# moves the trigger. The previous percentage mechanism carried the same exposure —
# the fraction term is an outer min() in the client's trigger — so nothing is lost.
# The next two are CLIENT-side constants, read out of the installed bundle at
# /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe,
# client 2.1.220 — not values this repository owns. They move on a client
# release without our involvement, so re-READ them from that bundle rather than
# re-deriving them, and locate them by the surrounding literal strings rather
# than by symbol name (the minifier renames symbols between builds). This is
# the discipline config.md's claude-md-max-chars row already records for a
# borrowed constant; the version named above is the only thing that later tells
# a reader whether the numbers here still match the installed client.
OUTPUT_RESERVE_TOKENS = 20_000        # min(maxOutputTokens, 20000) in the client
CLIENT_TRIGGER_FLOOR_MARGIN_TOKENS = 13_000   # the floor term of the trigger's min()
PRECOMPUTE_BUFFER_FRACTION = 0.2      # client default; server-tunable (see above)
SPAWN_AUTOCOMPACT_WINDOW_TOKENS = (
    round(AUTOCOMPACT_CEILING_TOKENS / (1 - PRECOMPUTE_BUFFER_FRACTION))
    + OUTPUT_RESERVE_TOKENS
)

# The `model max` term of the client's min(model max, configured window) step.
# Deliberately ONE number rather than a per-model table, on the same rationale
# as SPAWN_AUTOCOMPACT_WINDOW_TOKENS: every COMPLEXITY_MODEL entry (haiku,
# sonnet, opus) shares this 200k maximum, so applying it to every model is
# exact for the roster as it stands and conservative for anything larger. A
# smaller-window model joining the roster would make it too generous: a
# residual, not a guarantee.
MODEL_FLOOR_WINDOW_TOKENS = 200_000

# Conservative chars-per-token divisor for estimating an assembled prompt's
# token count from its char count without invoking a tokenizer. Fixed below
# the 1.744 chars/token actually measured on the failed dispatch below
# (375,759 chars / 215,416 tokens), so this estimate errs toward refusing
# early rather than discovering the failure only after the child is spawned.
# The distance from 1.744 down to 1.5 is picked headroom, not a derivation:
# only the ratio is measured, and nothing in it fixes how far below to sit.
PROMPT_CHARS_PER_TOKEN = 1.5


def dispatch_prompt_ceiling_tokens(model: str | None) -> int:
    """Largest assembled prompt (in tokens) this parent will spawn a child with."""
    window = min(MODEL_FLOOR_WINDOW_TOKENS, SPAWN_AUTOCOMPACT_WINDOW_TOKENS)
    usable = window - OUTPUT_RESERVE_TOKENS
    fraction_term = round(usable * (1 - PRECOMPUTE_BUFFER_FRACTION))
    floor_margin_term = usable - CLIENT_TRIGGER_FLOOR_MARGIN_TOKENS
    return min(fraction_term, floor_margin_term)


def dispatch_prompt_ceiling_chars(model: str | None) -> int:
    """The token ceiling above, converted into the unit the assembled prompt is
    already measured in. The CEILING is multiplied by a divisor set below the
    measured ratio, so the conversion shrinks the char budget; the prompt is
    never converted the other way, which would invert that safety margin."""
    return int(dispatch_prompt_ceiling_tokens(model) * PROMPT_CHARS_PER_TOKEN)
