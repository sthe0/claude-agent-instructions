---
name: published-text-writer-gate
description: Publishing reader-facing text to a ticket/issue is gated on the FACT that a tech-writer pass precedes the composition of those exact bytes in the harness's own transcript, never on content classification — the recurring failure was unpolished/leaked prose reaching a ticket with nothing in the harness able to answer "did tech-writer run before these bytes existed".
type: reference
schema: difficulty/v1
created: 2026-09-02
last_verified: 2026-09-02
---

# The published-text writer gate: bind on the fact of a tech-writer pass, not on content

## Difficulty

Reader-facing text reached a tracker/issue comment without ever passing through the tech-writer specialization, in two distinct shapes: raw artifact bytes pasted verbatim as comment text, and hand-authored or memory-recalled prose written straight into a publish call. Both are invisible to any check that inspects only the outgoing body's *content* — a body can look polished and still have been typed by the coordinator with no tech-writer invocation anywhere in the session, and a genuinely leaked artifact can be reformatted just enough to defeat a syntax sniff. Nothing in the harness could answer the one question that actually discriminates the two cases: did a tech-writer pass happen **before** these exact bytes were composed?

## Order & criterion

A `PreToolUse` hook on `Bash` intercepts a publication-shaped command, resolves the literal body it is about to send, and computes a **binding**: does the body's composition event in the *publishing process's own transcript* occur at or after a recorded tech-writer witness. No length threshold, no keyword/artifact-syntax content classification on the text path — every publication is checked, unconditionally, because tech-writer runs inline through `Skill` at negligible cost even for a one-line update. Acceptance check: the gate denies a body with no witness binding it and allows the same body once a tech-writer invocation precedes its composition, verified on committed fixture transcripts and in a real harness (two `claude -p` children, one deny arm and one allow arm — see stage 6's `samples/published-text-gate/in-harness-observation.json`).

## Route inventory (empirical, from this machine's own transcripts)

All observed publication calls travel as `Bash`, never as an in-process tool call, in these verbatim shapes:

| Shape | Ticket | Transport |
|---|---|---|
| `bash .claude/skills/tracker/scripts/tracker-cli.sh comment TICKET-467 --text @/tmp/update-comment.md` | TICKET-467 | `tracker-cli.sh comment`, file-valued `@path` |
| `bash .claude/skills/tracker/scripts/tracker-cli.sh comment TICKET-495 --text "..."` | TICKET-495 | `tracker-cli.sh comment`, inline literal |
| `gh issue comment 125 --repo sthe0/claude-agent-instructions --body "$(cat <<'EOF' ... EOF)"` | GitHub #125 | heredoc inside a command substitution |
| an internal MCP-bridge CLI, `--tool UpdateComment issue_key:TICKET-467 comment_id:<id> "text:$TEXT6"` (with `TEXT6=$(cat comment6_new.txt)` assigned earlier in the same command string) | TICKET-467 | an `UpdateComment` MCP-shaped tool NAME reached over a `Bash` CLI bridge, not a real `mcp__*` tool call |
| `bash .claude/skills/tracker/scripts/tracker-cli.sh attachment-upload TICKET-467 <path>` | TICKET-467 | `tracker-cli.sh attachment-upload` — the attachment verb, direct evidence for the attachment judge's call site |

Four recorded occurrences of unpolished/leaked text reaching a ticket, none caught by any content classifier: two inline `--text "literal"` postings on **TICKET-495** (raw artifact bytes pasted as comment text — no prior recorded bytes exist for an inline literal, so these deny via inline composition, a different sub-mechanism from the binding proper — see Residual 6); one hand-authored/memory-recalled body on **TICKET-467** before any witness; and **TICKET-467's own remediation trace**, the decisive case — a hand-written body published, then a tech-writer witness, then three separate `Write` calls producing three corrected comment bodies, then the corrected publish. Only the binding proper — not inline-composition denial — demonstrates discrimination between a polished body and an unpolished one on its own terms, which is why stage 4's fixture pairs two sibling `--body-file` bodies in one session (one composed after the witness, one before).

**The decisive trace** (a real remediation session on TICKET-467): `10:08:03Z` publish with a hand-written body → `10:10:52Z` `Skill{skill: tech-writer}` witness → `10:11:59Z`, `10:12:08Z`, `10:12:14Z` three `Write` calls (`comment1_new.txt`, `comment6_new.txt`, `comment7_new.txt`) → `10:34:09Z` corrected publish. Two design facts follow: the compliant flow really is witness-then-compose, so "these bytes were composed at or after a tech-writer invocation" allows the compliant flow and denies the pre-witness hand-written one without inverting; and one witness backed three bodies, so a one-witness-one-body consumption rule would have false-denied a legitimate flow — the binding is per-body containment, never witness consumption.

## Design: gate on the fact of the pass, not on content

Two earlier revisions of this design layered a harness-vocabulary term ruleset, an artifact-syntax prefilter, and a text judge, with a separately-triggered writer-pass attestation behind a length threshold. Both thinker reviews of that design spent nearly all their blocking concerns on the classification machinery itself — denylist literalness, ordinary-word false positives, threshold derivation, venue exemptions — none of which has to be answered if the gate never classifies content. The current design drops all of it: an unconditional, computed **binding** between the outgoing body and a transcript-recorded tech-writer witness (`scripts/lib/writer_pass.py`), fail-open only on the one path where no deterministic predicate exists — attachment uploads, gated instead by a narrow binary judge (machine artifact vs. reader-facing prose smuggled as a file).

Why a *computed* binding rather than a coordinator-supplied attestation field: a field the coordinator writes into its own payload ("writer_pass: true") is a claim the gated actor can mint — an honour system. A tech-writer invocation is instead an entry the harness itself writes into the session transcript as a `tool_use` block, which nothing the coordinator puts in a tool call's arguments can forge.

## Residuals

### Residual 1 -- Ordering, not authorship

The binding proves the body was composed at or after a witness, not that the witness authored it — except in the `WRITER_OUTPUT` strength, where the body arrives directly as the witness's own `tool_result`. In the weaker `POST_WITNESS` strength (an exact-equal `Write`/`Edit` after a witness), a trivial one-token tech-writer pass followed by hand-written prose written to the same file would still satisfy the predicate. This is accepted because closing it would require re-introducing content classification of the witness's own output — exactly the machinery this design exists to avoid — and no occurrence in the route inventory exploits this gap.

### Residual 2 -- Scan window

The transcript scan is bounded to a declared window. A witness older than the window denies the body even though a real tech-writer pass happened earlier in the same long-running session. The window is a resource bound (unbounded transcript scanning on every `Bash` call is not viable), and a false deny here fails safe — the coordinator re-runs tech-writer and the body composes again, inside the window.

### Residual 3 -- Unresolvable body

When the hook cannot resolve the literal body from the tool call's argv (a shape it does not recognize), it allows rather than denies — a fail-open choice, because a false deny on every unrecognized shape would make the gate a general publication blocker rather than a writer-pass check. The unresolved rate is countable only from the hook's own advisory stream (see the sink path below); a rising rate is the signal that a new command shape needs a resolver, not a reason to flip the default to fail-closed.

### Residual 4 -- Uncalibrated attachment judge

The attachment path's binary judge (machine artifact vs. reader-facing prose smuggled as a file) ships wired but uncalibrated: no measured accuracy sample backs its verdict yet. It is fail-open by construction — a judge error or timeout allows the upload — so an uncalibrated judge degrades to "no attachment check" rather than to a false block. Deferred calibration is filed as its own Core backlog item (see below); an unmeasured judge is a precedented state in this repo (`scripts/lib/judge_latency.py`'s `MEASURED` rows already carry `n=0`/`UNMEASURED` entries for `acceptance_judge` and `question_materiality`).

### Residual 5 -- Transport: the in-process blind spot

The gate's matcher is the literal `Bash`. A publication issued **in-process** — a raw `urllib` POST, a genuine `mcp__*` tool call — is invisible to it. Concretely, this repo's own `scripts/difficulty_channel/adapters/github.py` files Core difficulties over `urllib` against `api.github.com` and never traverses a shell at all; its filings are entirely outside this gate. This is the same fact, read from its other side, that retired the verb layer from earlier revisions of this design (that adapter's existence proved the verb layer was unnecessary for filing Core difficulties — the corollary is that its filings are also unreachable by any shell-matched hook). Closing it would need either a `PreToolUse` matcher on every `mcp__*` tool name (none is declared on this machine today, so it would be inert) or a differently-shaped in-library check inside each in-process publisher — a distinct mechanism, out of this plan's scope.

### Residual 6 -- Inline composition denies by a different sub-mechanism

An inline literal (`--text "..."` typed directly into the command, or an inline heredoc) has no prior recorded bytes anywhere in the transcript for the binding to bind to — there is nothing to look up, which is itself grounds for denial. This is correct and is this gate's, but it is not the binding *discriminating* a polished body from an unpolished one; it is the absence of any composition event at all. The two TICKET-495 occurrences deny this way, not via the binding proper — the leaf's route inventory keeps these labeled separately so this sub-mechanism isn't mistaken for evidence of content-level discrimination.

### Residual 7 -- Transcript topology: one process, one transcript

The binding is computed over the ONE transcript the `PreToolUse` payload names as the publishing process's own. Subagent transcripts are separate files: a witness held by a PARENT process is invisible to a spawned CHILD that publishes directly. An empirical scan of 120 recent transcripts on this machine found 127 of 127 real publication-shaped `Bash` calls in ROOT-process transcripts, and all 13 `("Skill", "tech-writer")` witnesses likewise in root transcripts — consistent with `skills/tracker-management/SKILL.md` making publication a root-coordinator responsibility. The scan is deliberately NOT widened to follow a parent-transcript pointer a child would have to supply, because that pointer is exactly the coordinator-authored input this design exists to exclude. A spawned specialist that published directly (unobserved so far — a developer opening a review request is the plausible future case) would need its own inline or self-spawned tech-writer pass.

### Residual 8 -- Harness-vocabulary leak (uncovered by this gate)

GitHub issue #125's 2026-08-19 comment flags a distinct failure mode: TICKET-467 leaked internal engine vocabulary (crutch names, internal tool identifiers, engine state labels) into a published body. A body can pass a genuine tech-writer pass and still carry this vocabulary — polish does not reliably strip harness-specific terms, and recognizing them needs a vocabulary list this gate does not carry. The attachment judge's structural sniff catches TOML/plan-render *shape*, never terminology, and the term ruleset that once addressed this was deleted (Core issue #157). This gate does not close that gap; the underlying ask — a Core harness-vocabulary term ruleset — remains alive as its own, not-yet-scoped backlog item, not folded into this plan's scope.

## Deferred calibration

The attachment judge's calibration is filed as a Core backlog issue rather than left as an unqueued word in this plan: it needs a sample of ≥16 calls over distinct attachment bodies in two arms (machine-artifact vs. reader-facing-smuggled), under the sampler discipline `samples/judge-latency/README.md` records, replacing the `UNMEASURED` row in `scripts/lib/judge_latency.py`'s table for `judge_published_attachment`. Issue number recorded here once filed: **pending — Bash access for `gh`/`scripts/file-difficulty.py` was not granted to this stage's spawn (see the stage's own `REPLAN:`/`PERMISSION-REQUEST:` return); filing is deferred to the coordinator.**

## Cost

Stage 7 only (memory/documentation stage, no new code): a handful of read-only greps and file reads plus this leaf write and the `verify-all.py` CHECKS/CHECK_ARGS edit; no `claude -p` spawns from within this stage.

## See also

- [[experience-leaf-schema]] — the `difficulty/v1` shape this leaf follows (standalone, outside `experience/`, so `verify-experience-leaf.py`'s path-scoped checks do not apply to it — it is indexed via `memory-global/MEMORY.md` directly instead).
- `scripts/lib/writer_pass.py` — the binding computation this leaf documents.
- `scripts/hook-published-text-writer-gate.py` — the `PreToolUse` hook.
- `skills/tracker-management/SKILL.md` § How to publish — cross-referenced from there.
