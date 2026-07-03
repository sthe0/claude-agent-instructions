---
name: systemic-pattern-scan
description: At task resolution — scan experience for systemic friction patterns and run overcome-difficulty against the agent-system-as-plan to produce architectural proposals, not rule tweaks
type: reference
created: 2026-05-27
last_verified: 2026-05-27
---

# Systemic pattern scan

**Premise.** Your instructions (`CLAUDE.md`, memory, skills, hooks, scripts) **are the plan you work by.** It is a persistent, multi-session plan, but a plan all the same. Therefore `overcome-difficulty` applies to it naturally: when the actual behavior across sessions diverges from what the plan expected, OD's Expected/Actual/Mismatch frame works without any meta-translation.

This leaf describes how to apply that lens at task resolution, so architectural improvements get proposed proactively — not only when the user points one out.

## When to scan

After drafting § **Self-critique** of the experience leaf, before invoking `self-improvement`:

1. Open the relevant `<scope>/experience/MEMORY.md` sub-index (project + global).
2. For each friction item in your draft self-critique — scan for prior leaves mentioning the same or adjacent friction.
3. **Systemic pattern criterion** (any one sufficient):
   - The same friction appears in **≥2 prior experience leaves**.
   - The current task hit the same friction **≥2 times** in different stages.
   - The friction is a category your CLAUDE.md / skill / memory **claims to address**, but didn't fire (rule exists, behavior doesn't).
   - The friction is structural — e.g. a section that consistently outgrows its container, an index that loses scanability, a script that mis-classifies new file types.

If criterion holds → systemic pattern. If not → it's a one-off, the existing `self-improvement` flow (write a rule tweak) is enough.

## Running OD against the agent-system

Frame the divergence at the agent-system level. Concrete template:

```
Skill(skill="overcome-difficulty", args="
Plan (persistent): agent system = CLAUDE.md + memory + skills + hooks.

Expected: when situation <X> arises, the system should <Y>
  (per <CLAUDE.md § Z> / <leaf A> / <skill B's trigger>).
Actual: across <N> recent experience leaves and the current task,
  situation <X> arose <M> times; <Y> happened <0 / rarely / late>.
Mismatch: rule exists at <location>, but the trigger does not fire /
  the structure does not contain the growth / the dispatch is missing.
")
```

OD's declaration → investigation → critique cycle then localizes the gap. The replanning task it returns will be an **architectural improvement**, distinct from a rule patch:

| Architectural improvement | Rule patch |
|---|---|
| New memory node (sub-index, dedicated leaf) | Rewording an existing paragraph |
| New trigger-leaf with concrete signals and examples | Adding "remember to do X" |
| New hook / script / verifier | Bolding existing text |
| Structural refactor (2→3 memory levels, new sub-skill, new agent kind) | Adding another copy of the rule in another file |
| New file-naming convention picked up by tooling | Adding "and also Y" to an existing rule |

If OD proposes a rule patch — push back. Usually the right move is the architectural one *in addition to* or *instead of* the patch.

## Routing the proposal

1. OD returns the architectural proposal (replanning task) — typically a small set of concrete file changes.
2. **Bundle into the final resolution `AskUserQuestion`**: alongside "considered resolved?" and "push?", add "Apply architectural fix <name>?" with options `Apply (Recommended) / Show diff / Reject`.
3. On `Apply` → `Skill(self-improvement)` writes the changes. On `Reject` → record in the experience leaf as a **rejected architectural proposal** with the user's reason (so a future scan doesn't re-discover the same proposal blindly).
4. On `Show diff` → write the files in chat, then re-ask.

## Worked example (this session)

Across the 2026-05-27 session — DEEPAGENT-367/414/415 audit + follow-ups — multiple instances of the same shape surfaced:

- yandex-guru subagent existed in `.claude/agents/` but had **0 invocations** in 9 transcripts.
- `overcome-difficulty` was rule-mentioned 3 times in global CLAUDE.md but had **0 invocations** in those same 9 transcripts.
- `fewer-permission-prompts` skill existed but I audited prompts by hand instead.
- A range of skills (`arc`, `arcanum`, `ya-vault`, `codesearch`, `paste`, …) were available but I tackled their domains via direct `Bash`.

**Systemic pattern:** *capability exists in the registry → trigger does not fire → user has to point it out*. Repeated across yandex-guru, OD, skill-pool. Not a one-off.

**Architectural improvements** that came out of OD-style analysis:
- **Project-memory trigger leaves** with concrete signals + examples (yandex-guru-trigger.md, overcome-difficulty-trigger.md). These are not "another copy of the rule" — they're a *project-local activation surface* the global rule lacked.
- **Skill-first dispatch** (global discipline leaf + project mapping table). New navigation surface, not a louder rule.
- **Allow-list parity with policy** (`acting-without-asking.md` § Policy ↔ settings.json). New documented alignment surface.
- **Memory hierarchy with sub-indexes** (this leaf's neighbor). Structural refactor from physical-3-level / navigational-2-level to coherent 3-level.

All four are architectural, not patches. None would have been produced by a simple "the rule needs to be louder" reaction. The pattern-scan discipline encoded in this leaf is itself the meta-result: making sure future sessions reach for the architectural option proactively.

## Anti-patterns

- **Add another copy of the existing rule.** If the rule already appears in 3 places and isn't firing, the 4th won't help. Look for what's missing structurally.
- **Patch the symptom, ignore the pattern.** Fixing only the immediate friction (this leaf wasn't auto-loaded → add a manual `Read` instruction) is a rule patch; the structural fix (introduce auto-loading sub-indexes) is what generalizes.
- **Scan only the current session.** The point of `experience/MEMORY.md` is cross-session detection. Scan the sub-index, not just the current chat.
- **Defer architectural proposals to the user.** The discipline exists so they get proposed by me; the user reviews/approves, not invents.
- **Silently apply.** Architectural improvements still go through the `AskUserQuestion` confirmation gate — they are scope-changing by definition.

## See also

- `~/.claude-agent/CLAUDE.md` § On task resolution § What to record § Self-critique — the trigger.
- `~/.claude-agent/CLAUDE.md` § On task resolution § Auto-trigger self-improvement — the routing.
- `~/.claude-agent/skills/overcome-difficulty/SKILL.md` — the analysis tool (applies naturally to agent-system-as-plan).
- `~/.claude-agent/skills/self-improvement/SKILL.md` — the writer of the architectural change.
- [memory-hierarchy.md](memory-hierarchy.md) — sister leaf produced by the same kind of analysis; demonstrates the "architectural, not patch" distinction.
