---
name: propose-skill-on-repeated-workaround
description: A second hand-rolled repeat of the same complex reusable action, with no skill/tool covering it, is itself a self-improvement trigger — independent of any user correction and independent of task resolution.
type: feedback
schema: leaf/v1
created: 2026-09-02
last_verified: 2026-09-02
---

# Propose a skill on the second repeated manual workaround

## Difficulty

To achieve turning a complex, reusable, hand-rolled action into a tool *before* the user has to point it out, the agent must notice repetition of the action itself — not only wait for a user correction of its outcome. The two existing triggers both miss this case. `self-improvement` (CLAUDE.md § When the user corrects agent behavior) fires only on an explicit correction/rejection/principle-statement from the user. `systemic-pattern-scan` fires only at task resolution, and only if the closing self-critique for that task was actually written.

A resolved project ticket hit both gaps in the same task. Two separate hand-rolled deploy-CLI invocations landed a wrong metadata field (once, then again two days later); each correction was a point-fix into a narrow reference leaf, never read as "this whole action needs a tool." The ticket resolved `fixed` with no experience leaf, so `systemic-pattern-scan`'s resolution-time hook never ran on it. A deployment-automation skill was authored five days later, on the **third** hit of the same wall — per the landing commit's own words: "several infrastructure skills lived in the source but weren't in the catalog, so the session blindly hand-rolled deploy commands." A second, independent instance in the same ticket: a live-traffic health-check script and a prod-request-sampling script were both hand-rolled into a ticket-scoped scratch area and never promoted to a reusable product-tree tool — even though an existing benchmarking tool was explicitly known to be the wrong shape for the job (wrong row-selection semantics) and was simply routed around, not fixed or named as superseded.

## Guidance

**Rule.** The second time in a session — or, when recoverable from memory, in a project's recent history — the same complex multi-step action is carried out by hand (a sequence of raw CLI/API calls, an inline script, a copy-pasted procedure) and no skill/tool already covers it, treat the repetition itself as a self-improvement trigger. Do not wait for a user correction; do not wait for task resolution. Surface it in the moment via `AskUserQuestion`: "Оформить это действие в навык/инструмент?" (`Оформить (Recommended)` / `Не сейчас` / `В бэклог`). On acceptance, run `self-improvement`'s normal two-beat flow — propose the skill's shape, apply after confirmation — exactly as for a user-triggered edit.

**What qualifies.** A single one-liner or a lone API call does not — formalizing it costs more than repeating it. Qualifies: a multi-step procedure (roughly ≥3 ordered steps, or a step with an easy-to-get-wrong field — the `revision_info`/`author` mistake is the concrete case here), a procedure touching shared or external state (a deploy, a stage write, a prod query), or a procedure that already has an informal write-up (a reference leaf, a plan checklist) — that last case is the strongest signal, since the leaf is already doing a skill's job without the reuse.

**Distinct from, not a replacement for, the two existing triggers.** Keep `self-improvement`'s standard user-correction trigger — this adds a self-noticed-repetition path that needs no user statement. Keep `systemic-pattern-scan`'s resolution-time sweep — this rule does not depend on resolution or on a self-critique having been written, so it catches the case where a ticket closes before that reflection happens.

**Applies symmetrically to hand-rolled measurement/data scripts**, not only to coordination-level CLI sequences: a health-check or dataset-sampling script written into a ticket-scoped scratch area, once reused a second time (same ticket or the next one), is a promotion candidate — either into a proper product-tree tool, or, if an existing tool is the wrong shape for the job, a named fix to that tool rather than a permanent silent bypass of it.

## See also

- [[systemic-pattern-scan]] — the resolution-time sibling; this leaf covers the gap when resolution happens without a self-critique.
- `~/.claude-agent/skills/self-improvement/SKILL.md` § Also self-initiated, not only user-triggered — the other self-initiated path (`self-diagnose.py`, keyed on instruction-file friction); this rule is the second self-initiated path, keyed on repeated *manual work*.
- [[skill-catalog-curation]] — the removal-side discipline; this leaf is its creation-side counterpart.
- A project's resolved-task experience memory (kept outside this repo) holds the concrete case: a deployment-automation skill authored 2026-08-31 on the third repetition; the live-traffic-check and prod-sampling scripts from the same ticket remain scratch-scoped and unpromoted as of 2026-09-02.
