# The Cursor mirror

> The thin Cursor rule that mirrors the Claude instructions for the things Cursor cannot do natively, and the discipline that keeps it from drifting.

The same repo drives both Claude Code and Cursor, but the canonical instruction file is the single [CLAUDE.md](../../CLAUDE.md). Cursor reads a separate, deliberately **thin** rule — [cursor/rules/claude-code-sync.mdc](../../cursor/rules/claude-code-sync.mdc) — that mirrors only what Cursor cannot do natively: it has no Skill tool and no auto-memory writes, so the mirror carries the irreducible behavioural core rather than the full constitution.

Three disciplines keep the mirror honest:

- **Thin by design.** The mirror has a hard line-count ceiling (`cursor-mirror-max-lines` in [config.md](../../config.md)) — it is not allowed to grow into a second copy of CLAUDE.md. Detail belongs in the canonical file; the mirror points at it. Enforced by [lint-prose-length.py](../../scripts/lint-prose-length.py).
- **Updated in lockstep.** When the cooperation model changes, the mirror is updated in the **same** change as CLAUDE.md, so the two never describe different agents. The other Cursor-only assets it sits alongside are isolated under [cursor/](../../cursor/README.md), kept out of the `~/.claude-agent/` tree.
- **Mechanically linted.** [lint-cursor-mirror.py](../../cursor/scripts/lint-cursor-mirror.py) (in `verify-all` / pre-commit) checks skill/specialization parity, `**TRIGGER:**` markers, the `resolution_confirmed_by_user` rule, forbids the invented path `~/.claude-agent/scripts` (scripts live under `~/claude-agent-instructions/scripts/`), requires the canon self-diagnose path, and requires an explicit Claude-Code-only caveat whenever the mirror mentions hook gates / "blocks the turn" (Cursor has no SessionStart/Stop hooks).

At runtime the rule is wired to `~/.cursor/rules/claude-code-sync.mdc` by `setup-symlinks.sh`, the same single wiring command that installs the Claude-side symlinks.


## Hook compensation (Claude Code → Cursor)

| Claude hook / gate cluster | Cursor compensation |
|---|---|
| SessionStart (`self-diagnose`, scope registration) | Coordinator runs `~/claude-agent-instructions/scripts/self-diagnose.py` periodically; no auto-run |
| PreToolUse / `hook-state-gate.py` (plan approval, execution node) | Prose + `agentctl` spine; mirror § Hookless Cursor obligations |
| Stop / `hook-turn-end-gate.py` (self-improvement, resolution) | Same-turn self-improvement + resolution ask in the final reply |
| Skill tool invocation | Read `SKILL.md` inline in chat |
| `AskUserQuestion` | **AskQuestion** when attached; else fixed-choice prose |

Cursor sessions still run the `agentctl` spine when the engine is available (CLI + hooks); the mirror's thin prose must not be read as a hand-walk carve-out. Claude Code's `AskUserQuestion` maps to Cursor's **AskQuestion** when that tool is attached (otherwise ask the same fixed-choice question in prose).
