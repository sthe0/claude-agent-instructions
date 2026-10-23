---
name: memory-index-max-bytes derivation
description: Why memory-global/MEMORY.md is capped at 24000 UTF-8 bytes — harness truncation mechanics, 1000-byte margin sizing, byte-unit choice, and why no companion line-count key is needed.
type: reference
schema: leaf/v1
created: 2026-10-23
last_verified: 2026-10-23
---

# `memory-index-max-bytes` — derivation

## Difficulty

The `memory-index-max-bytes` key in `config.md` (ceiling: 24000, unit: UTF-8 bytes) is authoritative only because it is derived, not asserted. Without the derivation a future editor cannot tell whether the ceiling, the unit, or the margin are tunable or load-bearing — and may change them in ways that cause silent truncation of `memory-global/MEMORY.md` by the harness. Recording the derivation in an on-demand leaf keeps the always-loaded surface lean while preserving the reasoning.

## Guidance

### Harness constants

The installed client bundle enforces a real cap on `memory-global/MEMORY.md` in function `Yqr(e)` on the AutoMem context-assembly path:

- `rxe = 25000` — byte (or JS string-length) cap; file is **silently truncated** if exceeded — no error, no log
- `Doe = 200` — line cap (the two apply independently; whichever binds first)

Verified 2026-07-23 by reading the bundle and by a sentinel probe (a marker at line 150 returned verbatim; a marker at line 240 was absent). The constants and their symbols are also recorded in the preamble of `memory-global/MEMORY.md` itself.

### Why the ceiling is 24000 not 25000 (the 1000-byte margin)

The gate is a pre-commit hook (`core.hooksPath=githooks` runs `verify-all --staged`), so a ceiling sitting exactly at 25000 would still reject the truncating commit. The 1000-byte margin buys two things a ceiling on the constant would not:

**FIRST** — the gate cannot see an uncommitted write at all, and `CLAUDE.md` exempts memory writes from the production-edit gate, so an agent may append a pointer and keep working with no commit in between. Truncation is live from that WRITE, and only headroom below the harness cap keeps the file loadable across that uncommitted window.

The margin is sized to that window: 1000 bytes exceeds the 756-byte pointer line added in commit `776dbf8` on 2026-08-31 — the largest single pointer this index has ever carried, and the one that pushed the file to 25533 bytes and silently dropped its last two pointers — so one ordinary append cannot cross the harness cap before the commit gate sees it.

**SECOND** — `check_level()`'s WARN at 90% (21600 bytes) lands ~3400 bytes below the harness cap: enough advance notice to act structurally (split a section into a sub-index) rather than to discover the pressure at the moment of breakage. A ceiling of 25000 would put that warning at 22500, buying only 2500.

Neither reason generalizes to bounding arbitrary growth — a single commit adding 1500 bytes across several lines is caught by the CEILING, not by the margin, and nothing bounds a commit to one pointer line.

### Why UTF-8 bytes (not chars or lines)

The harness constant `rxe` is compared against the file in the JavaScript runtime. The byte-unit choice is safe under either reading of `rxe` (bytes or JS string-length / UTF-16 code units): UTF-8 byte length ≥ UTF-16 code-unit length for every Unicode scalar value (checked exhaustively over all 1 112 064 values — min difference 0, no counterexamples), so a byte-measured gate is never looser than the harness whether `rxe` compares bytes or string length. Which of the two it actually compares is deliberately left unresolved rather than re-derived from the minified bundle (the minifier renames symbols between builds); the dominance argument makes the answer not matter.

This does NOT extend to a third axis: were `rxe` a token count, this gate would be measuring yet another correlated axis and would need re-deriving.

### Why no companion line-count key

At this index's observed density (~240 bytes per line) the byte cap binds at ~100 lines — roughly half the harness's 200-line cap. Bytes are the binding constraint; a second key measuring lines would be prophylactic.

### Mirrors `claude-md-max-chars`

Both ceiling keys sit under their respective harness constants rather than on them, for the same reason: an uncommitted write can cross the harness cap before the pre-commit gate fires.

### Scope

The ceiling applies only to `memory-global/MEMORY.md` itself. Sub-indexes (`experience/MEMORY.md`, `system-knowledge/MEMORY.md`, …) are read on demand and are never truncated by the harness; they are governed only by their own WARN thresholds, not by this key.

## See also

- `config.md` key `memory-index-max-bytes` — the ceiling value (24000), unit, harness constant, and pointer back here
- `config.md` key `claude-md-max-chars` — the analogous ceiling for `CLAUDE.md`, same margin pattern
- [[memory-hierarchy]] — when to spin off sub-indexes (the structural action that advance warning targets)
