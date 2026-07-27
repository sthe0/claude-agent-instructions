"""The child auto-compaction pin is a WINDOW, derived once from our own ceiling.

Drift this replaces: a per-model window table in spawn-specialist.py mirrored
Anthropic's per-model context maxima, so every model release rotted the file
silently — a model absent from the table got a wrong default window and its
percentage-derived trigger landed wherever that default put it.

Pinning the window instead lets the client's own `min(model max, configured)`
do the per-model work, so a Haiku child clamps itself to its own 200k maximum
with nothing about Haiku written down here. These tests re-derive the resulting
trigger through the client's arithmetic (2.1.220: `vSe` computes
z = window - min(maxOutputTokens, 20000), `Mds` fires at
min(z - round(z * frac), z - 13000)) and check it lands where we intend on both
the unclamped and the clamped path.

The client expression is mirrored literally rather than algebraically
rearranged: an identity that holds only for today's constants would make the
test agree with a future regression instead of catching it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"

# Client-side constants that are NOT ours to set — mirrored here only so the
# expected trigger can be recomputed. `OUTPUT_RESERVE`/`FRACTION` have module
# counterparts (asserted equal below); the 13000 buffer has none, because we
# never needed to name it.
CLIENT_FIXED_BUFFER = 13_000
HAIKU_MAX_WINDOW = 200_000
# `o7` clamps a configured window into this range before using it, so a pin
# outside it is silently rewritten and the trigger arithmetic below — which
# assumes the pin is honoured verbatim — would describe a window nobody got.
CLIENT_WINDOW_RANGE = (100_000, 1_000_000)
# Post-compaction context floor observed at ~90–97k (memory-global leaf
# autocompact-threshold-policy.md). A trigger near the floor thrashes: the
# session compacts, lands just under the trigger, and compacts again.
POST_COMPACTION_FLOOR = 97_000
MIN_MARGIN_ABOVE_FLOOR = 45_000


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_autocompact", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _client_trigger(configured_window: int, model_max: int | None = None) -> int:
    """Where client 2.1.220 fires auto-compaction for a given pin."""
    window = configured_window if model_max is None else min(model_max, configured_window)
    z = window - min(MOD.OUTPUT_RESERVE_TOKENS, 20_000)
    return min(z - round(z * MOD.PRECOMPUTE_BUFFER_FRACTION), z - CLIENT_FIXED_BUFFER)


# (a) the pin is derived from the ceiling, not written down twice
def test_window_is_derived_from_the_ceiling():
    assert MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS == (
        round(MOD.AUTOCOMPACT_CEILING_TOKENS / (1 - MOD.PRECOMPUTE_BUFFER_FRACTION))
        + MOD.OUTPUT_RESERVE_TOKENS
    )


# (b) the pin survives the client's window parser unaltered — the one constraint
#     on it that the trigger arithmetic does not already imply
def test_pin_is_inside_the_client_window_parser_range():
    low, high = CLIENT_WINDOW_RANGE
    assert low <= MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS <= high


# (c) on a model whose maximum exceeds the pin, the trigger IS our ceiling
def test_unclamped_trigger_equals_our_ceiling():
    assert _client_trigger(MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS) == MOD.AUTOCOMPACT_CEILING_TOKENS


# (d) on a model whose maximum is below the pin, the client clamps and the
#     trigger stays clear of the post-compaction floor rather than thrashing
def test_clamped_trigger_keeps_margin_above_the_post_compaction_floor():
    trigger = _client_trigger(MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS, model_max=HAIKU_MAX_WINDOW)
    assert trigger < MOD.AUTOCOMPACT_CEILING_TOKENS, "a clamp can only lower the trigger"
    assert trigger - POST_COMPACTION_FLOOR >= MIN_MARGIN_ABOVE_FLOOR


# (e) every kind gets the pin, in both forms the client reads
def test_every_kind_gets_the_window_pin_in_both_forms():
    for kind in ("developer", "planner", "thinker", "code-reviewer", "tech-writer"):
        settings = MOD.build_child_settings(kind)
        assert settings["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(
            MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS)
        assert settings["autoCompactWindow"] == MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS


# (f) the per-model table and the percentage mechanism are gone for good —
#     re-adding either is the regression, in the source as well as the API
def test_no_per_model_window_table_and_no_percentage_key():
    for gone in ("MODEL_WINDOW_TOKENS", "DEFAULT_WINDOW_TOKENS", "autocompact_pct_for_model"):
        assert not hasattr(MOD, gone), f"{gone} is the drift this pin replaced"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in SCRIPT.read_text(encoding="utf-8")
