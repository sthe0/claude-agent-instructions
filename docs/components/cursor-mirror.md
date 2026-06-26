# The Cursor mirror

> The thin Cursor rule that mirrors the Claude instructions for the things Cursor cannot do natively, and the discipline that keeps it from drifting.

The same repo drives both Claude Code and Cursor, but the canonical instruction file is the single [CLAUDE.md](../../CLAUDE.md). Cursor reads a separate, deliberately **thin** rule — [cursor/rules/claude-code-sync.mdc](../../cursor/rules/claude-code-sync.mdc) — that mirrors only what Cursor cannot do natively: it has no Skill tool and no auto-memory writes, so the mirror carries the irreducible behavioural core rather than the full constitution.

Two disciplines keep the mirror honest:

- **Thin by design.** The mirror has a hard line-count ceiling (`cursor-mirror-max-lines` in [config.md](../../config.md)) — it is not allowed to grow into a second copy of CLAUDE.md. Detail belongs in the canonical file; the mirror points at it.
- **Updated in lockstep.** When the cooperation model changes, the mirror is updated in the **same** change as CLAUDE.md, so the two never describe different agents. The other Cursor-only assets it sits alongside are isolated under [cursor/](../../cursor/README.md), kept out of the `~/.claude/` tree.

At runtime the rule is wired to `~/.cursor/rules/claude-code-sync.mdc` by `setup-symlinks.sh`, the same single wiring command that installs the Claude-side symlinks.
