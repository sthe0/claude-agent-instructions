---
name: published-text-writer-gate
description: Publishing reader-facing text to a ticket/issue is gated first on the FACT that a tech-writer pass precedes the composition of those exact bytes in the harness's own transcript, then on a fail-open judge check of the judge-checkable tech-writer rules listed in publish-rules.toml — the recurring failure was unpolished/leaked prose reaching a ticket with nothing in the harness able to answer "did tech-writer run before these bytes existed".
type: reference
schema: difficulty/v1
created: 2026-09-02
last_verified: 2026-10-06
---

# The published-text writer gate: bind on the fact of a tech-writer pass, then check judge-checkable rules

## Difficulty

Reader-facing text reached a tracker/issue comment without ever passing through the tech-writer specialization, in two distinct shapes: raw artifact bytes pasted verbatim as comment text, and hand-authored or memory-recalled prose written straight into a publish call. Both are invisible to any check that inspects only the outgoing body's *content* — a body can look polished and still have been typed by the coordinator with no tech-writer invocation anywhere in the session, and a genuinely leaked artifact can be reformatted just enough to defeat a syntax sniff. Nothing in the harness could answer the one question that actually discriminates the two cases: did a tech-writer pass happen **before** these exact bytes were composed?

## Order & criterion

A `PreToolUse` hook on `Bash` intercepts a publication-shaped command, resolves the literal body it is about to send, and computes a **binding**: does the body's composition event in the *publishing process's own transcript* occur at or after a recorded tech-writer witness. No length threshold on the binding — every publication is checked, unconditionally, because tech-writer runs inline through `Skill` at negligible cost even for a one-line update. Acceptance check: the gate denies a body with no witness binding it and allows the same body once a tech-writer invocation precedes its composition, verified on committed fixture transcripts and in a real harness (two `claude -p` children, one deny arm and one allow arm — see stage 6's `samples/published-text-gate/in-harness-observation.json`).

## Route inventory (empirical, from this machine's own transcripts)

All observed publication calls travel as `Bash`, never as an in-process tool call, in these verbatim shapes:

| Shape | Ticket | Transport |
|---|---|---|
| `bash .claude/skills/tracker/scripts/tracker-cli.sh comment TICKET-467 --text @/tmp/update-comment.md` | TICKET-467 | `tracker-cli.sh comment`, file-valued `@path` |
| `bash .claude/skills/tracker/scripts/tracker-cli.sh comment TICKET-495 --text "..."` | TICKET-495 | `tracker-cli.sh comment`, inline literal |
| `gh issue comment 125 --repo sthe0/claude-agent-instructions --body "$(cat <<'EOF' ... EOF)"` | GitHub #125 | heredoc inside a command substitution |
| an internal MCP-bridge CLI, `--tool UpdateComment issue_key:TICKET-467 comment_id:<id> "text:$TEXT6"` (with `TEXT6=$(cat comment6_new.txt)` assigned earlier in the same command string) | TICKET-467 | an `UpdateComment` MCP-shaped tool NAME reached over a `Bash` CLI bridge, not a real `mcp__*` tool call |
| `bash .claude/skills/tracker/scripts/tracker-cli.sh attachment-upload TICKET-467 <path>` | TICKET-467 | `tracker-cli.sh attachment-upload` — the attachment verb, direct evidence for the attachment judge's call site |
| `agentctl present-plan --kind essence\|full\|replan_diff --rendering-file <path>` | the engine's own plan presentation | an in-process engine check on the rendering bytes (`writer_pass.bind` against the session transcript derived from the session id), not a Bash hook — the one non-`Bash` row, added by construction rather than observed. Same two limits as the hook: the witness is existential (any earlier tech-writer pass in the scanned window, never consumed), and the override (`CLAUDE_PUBLISHED_TEXT_GATE=0`) is one env-prefixed command away — which is why the root names an `override` outcome to the user in the presenting turn |

Four recorded occurrences of unpolished/leaked text reaching a ticket, none caught by any content classifier: two inline `--text "literal"` postings on **TICKET-495** (raw artifact bytes pasted as comment text — no prior recorded bytes exist for an inline literal, so these deny via inline composition, a different sub-mechanism from the binding proper — see Residual 6); one hand-authored/memory-recalled body on **TICKET-467** before any witness; and **TICKET-467's own remediation trace**, the decisive case — a hand-written body published, then a tech-writer witness, then three separate `Write` calls producing three corrected comment bodies, then the corrected publish. Only the binding proper — not inline-composition denial — demonstrates discrimination between a polished body and an unpolished one on its own terms, which is why stage 4's fixture pairs two sibling `--body-file` bodies in one session (one composed after the witness, one before).

**The decisive trace** (a real remediation session on TICKET-467): `10:08:03Z` publish with a hand-written body → `10:10:52Z` `Skill{skill: tech-writer}` witness → `10:11:59Z`, `10:12:08Z`, `10:12:14Z` three `Write` calls (`comment1_new.txt`, `comment6_new.txt`, `comment7_new.txt`) → `10:34:09Z` corrected publish. Two design facts follow: the compliant flow really is witness-then-compose, so "these bytes were composed at or after a tech-writer invocation" allows the compliant flow and denies the pre-witness hand-written one without inverting; and one witness backed three bodies, so a one-witness-one-body consumption rule would have false-denied a legitimate flow — the binding is per-body containment, never witness consumption.

## Design: gate on the fact of the pass, then on content for judge-checkable rules

Two earlier revisions layered a harness-vocabulary term ruleset, an artifact-syntax prefilter and a text judge behind a length threshold; both thinker reviews spent their blocking concerns on that classification machinery. The current design keeps the **binding** unconditional and free of content classification: a computed link between the outgoing body and a transcript-recorded tech-writer witness (`scripts/lib/writer_pass.py`). Why computed rather than a coordinator-written attestation field: a field the gated actor writes into its own payload is a claim it can mint, while a tech-writer invocation is a `tool_use` entry the harness itself writes into the transcript.

Core #288 showed the binding is not enough: a bound body can still break a tech-writer rule (the 2026-10-05 second-person address, rule 13). So a bound TEXT body now also gets a **content check against the tech-writer rule registry**:

- **One rule list.** The rule wording lives only in `skills/specializations/tech-writer/SKILL.md`. `skills/specializations/tech-writer/publish-rules.toml` classifies every numbered rule as judge-checkable (with candidate literals) or perception-only (with a reason); tests keep the two in sync.
- **Prefilter, never a verdict.** `writer_rules.find_candidates` nominates judge-checkable rules whose literals occur in the body. A silent prefilter makes no model call.
- **One fail-open judge.** A non-empty candidate set goes to `agentctl.advisor.judge_published_text_rules` (sonnet, low effort, hard timeout, shared judge budget). Only a genuine YES with a span found verbatim in the body denies, naming each violated rule and span. NO, the killswitch `CLAUDE_PUBLISHED_TEXT_RULES_SEMANTIC=0`, budget exhaustion, timeout, runner error or an unparseable answer allow and leave an advisory in the sink.
- **Unchanged.** The attachment judge, the `CLAUDE_PUBLISHED_TEXT_GATE=0` override and the 190 s hook budget.

### Content-check residuals

- Perception-only rules (the registry gives each its reason) are not checked.
- A prompt-injected body can at best push the judge to NO, i.e. to allow; a deny needs a verbatim span.
- Any judge failure allows, so the check then degrades to the binding alone.
- Rule say-3 is deferred.
- The judge prompt truncates the body at 12000 characters; a violation beyond is unseen.
- A body that reassembles the prompt's delimiters is not specially defended against.
- When the judge's span is invalid, the deny lists all candidate rules, not only the violated one.
- The span match is whitespace-sensitive, so a re-wrapped span fails validation and the verdict falls back to allow.
- `advisor.py` carries an orphan string, `_TEXT_RULES_SAY13_NUANCE`, left as a nit.
- The calibration set is thin and was tuned after its first run (below).

## Residuals

### Residual 1 -- Ordering, not authorship

The binding proves the body was composed at or after a witness, not that the witness authored it (the content check narrows this for judge-checkable rules but does not close it) — except in the `WRITER_OUTPUT` strength, where the body arrives directly as the witness's own `tool_result`. In the weaker `POST_WITNESS` strength (an exact-equal `Write`/`Edit` after a witness), a trivial one-token tech-writer pass followed by hand-written prose written to the same file would still satisfy the predicate. This was accepted for the binding itself, because closing it would require classifying the witness's own output; the registry-driven content check below is the partial answer.

**2026-10-05, an internal-project ticket: a concrete occurrence of exactly this gap.** A ticket-comment draft was polished inline (the coordinator editing its own text by memory of the rules) without ever actually invoking `Skill(tech-writer)` — so whatever satisfied the gate's witness check was shallow, not the real pass this design assumes. The published comment still addressed the chat partner directly ("по вашему выбору…") in violation of tech-writer rule 13, and still carried unnecessary ceremony the user flagged as bureaucratic. The gate cannot distinguish a genuine tech-writer pass from a token one by design (see above); the fix applied this round was on the authoring side, not the gate — tech-writer rule 13 gained an explicit mechanical self-check (grep for second-person forms before publish), and tracker-management gained a proportionality rule against over-ceremony. This is the occurrence that motivated the content check (Core #288): the gate now denies such a body when the judge returns a genuine YES on rule 13. That narrows the gap; the binding itself still proves ordering, not authorship.

### Residual 2 -- Scan window

The transcript scan is bounded to a declared window. A witness older than the window denies the body even though a real tech-writer pass happened earlier in the same long-running session. The window is a resource bound (unbounded transcript scanning on every `Bash` call is not viable), and a false deny here fails safe — the coordinator re-runs tech-writer and the body composes again, inside the window.

### Residual 3 -- Unresolvable body

When the hook cannot resolve the literal body from the tool call's argv (a shape it does not recognize), it allows rather than denies — a fail-open choice, because a false deny on every unrecognized shape would make the gate a general publication blocker rather than a writer-pass check. The unresolved rate is countable only from the hook's own advisory stream (see the sink path below); a rising rate is the signal that a new command shape needs a resolver, not a reason to flip the default to fail-closed.

### Residual 4 -- Uncalibrated attachment judge

The attachment path's binary judge (machine artifact vs. reader-facing prose smuggled as a file) ships wired but uncalibrated: no measured accuracy sample backs its verdict yet. It is fail-open by construction — a judge error or timeout allows the upload — so an uncalibrated judge degrades to "no attachment check" rather than to a false block. Deferred calibration is filed as its own Core backlog item (see below); an unmeasured judge is a precedented state in this repo (`scripts/lib/judge_latency.py`'s `MEASURED` rows already carry `n=0`/`UNMEASURED` entries for `acceptance_judge` and `question_materiality`).

### Residual 5 -- Transport: the in-process blind spot

The gate's matcher is the literal `Bash`. A publication issued **in-process** — a raw `urllib` POST, a genuine `mcp__*` tool call — is invisible to it. Concretely, this repo's own `scripts/difficulty_channel/adapters/github.py` files Core difficulties over `urllib` against `api.github.com` and never traverses a shell at all; its filings are entirely outside this gate. This is the same fact, read from its other side, that retired the verb layer from earlier revisions of this design (that adapter's existence proved the verb layer was unnecessary for filing Core difficulties — the corollary is that its filings are also unreachable by any shell-matched hook). Closing it would need either a `PreToolUse` matcher on every `mcp__*` tool name (none is declared on this machine today, so it would be inert) or a differently-shaped in-library check inside each in-process publisher — a distinct mechanism, out of this plan's scope. The engine's own presentation path, `agentctl present-plan`, was another instance of this blind spot and is now covered in-process by the same `writer_pass.bind` (route inventory above), so it no longer is one.

### Residual 6 -- Inline composition denies by a different sub-mechanism

An inline literal (`--text "..."` typed directly into the command, or an inline heredoc) has no prior recorded bytes anywhere in the transcript for the binding to bind to — there is nothing to look up, which is itself grounds for denial. This is correct and is this gate's, but it is not the binding *discriminating* a polished body from an unpolished one; it is the absence of any composition event at all. The two TICKET-495 occurrences deny this way, not via the binding proper — the leaf's route inventory keeps these labeled separately so this sub-mechanism isn't mistaken for evidence of content-level discrimination.

### Residual 7 -- Transcript topology: one process, one transcript

The binding is computed over the ONE transcript the `PreToolUse` payload names as the publishing process's own. Subagent transcripts are separate files: a witness held by a PARENT process is invisible to a spawned CHILD that publishes directly. An empirical scan of 120 recent transcripts on this machine found 127 of 127 real publication-shaped `Bash` calls in ROOT-process transcripts, and all 13 `("Skill", "tech-writer")` witnesses likewise in root transcripts — consistent with `skills/tracker-management/SKILL.md` making publication a root-coordinator responsibility. The scan is deliberately NOT widened to follow a parent-transcript pointer a child would have to supply, because that pointer is exactly the coordinator-authored input this design exists to exclude. A spawned specialist that published directly (unobserved so far — a developer opening a review request is the plausible future case) would need its own inline or self-spawned tech-writer pass.

### Residual 8 -- Harness-vocabulary leak (uncovered by this gate)

GitHub issue #125's 2026-08-19 comment flags a distinct failure mode: TICKET-467 leaked internal engine vocabulary (crutch names, internal tool identifiers, engine state labels) into a published body. A body can pass a genuine tech-writer pass and still carry this vocabulary — polish does not reliably strip harness-specific terms, and recognizing them needs a vocabulary list this gate does not carry. The attachment judge's structural sniff catches TOML/plan-render *shape*, never terminology, and the term ruleset that once addressed this was deleted (Core issue #157). This gate does not close that gap; the underlying ask — a Core harness-vocabulary term ruleset — remains alive as its own, not-yet-scoped backlog item, not folded into this plan's scope.

### Residual 9 -- Seam coverage is per-exact-verb, not per-tool

`publication-tools.local`'s `bash_verb` matching (`lib/published_body._segment_matches_verb`) is an exact-token match on the verb's full token sequence, not a prefix or substring match: an entry named `tracker-cli.sh comment` matches only a command segment whose corresponding tokens are `tracker-cli.sh comment` verbatim — a sibling subcommand like `tracker-cli.sh comment-update` or `tracker-cli.sh project-comment` does **not** match it and resolves to `NOT_A_PUBLICATION`, silently unGATED, with no advisory record distinguishing this from an ordinary non-publishing command. Concretely: this machine's seam registered `tracker-cli.sh comment` but not `tracker-cli.sh comment-update`, so every `comment-update` call — including an in-place edit of an already-published body — bypassed the writer-pass binding entirely; a 2026-09-18 TICKET-497 ticket comment, edited via `comment-update`, reached the ticket with no witness check at all, and the user flagged it as bureaucratic filler for an external reader. This is a coverage gap in the seam, not a content-classification failure — the fix was two added seam entries (`tracker-cli.sh comment-update`, `tracker-cli.sh project-comment`), re-verified by calling `published_body.resolve()` directly against a synthetic `comment-update` command (resolves to `TEXT` after the fix; the verb did not match at all before it). **Generalizes:** any tool skill that grows a new publish-shaped subcommand (an edit verb, a second comment type, a new CLI) needs an explicit seam entry — the gate does not infer siblings from a registered verb's name, and a missing entry fails open with no distinguishing signal from an ordinary non-publishing command.

### Residual 10 (CLOSED 2026-09-27) -- a raw HTTP write matched no verb, and the fail-open sink was read by nothing

Residual 9's generalization understated the gap on two independent axes, both found while publishing a quota ticket's description through `st-api` directly.

**No verb to match.** A publication that reaches a ticket API by raw HTTP — `curl -X PATCH`, or a `python3` heredoc driving `urllib` — invokes an *interpreter*, not a publishing tool. Every verb matcher saw `curl`/`python3`, returned `NOT_A_PUBLICATION`, and allowed it silently: the seam could have listed every tracker subcommand in existence and still missed this route entirely. The endpoint is the only stable handle, and it is deployment-specific, so it now lives in the seam as data (`{"name": "st-api.<host>/v2/issues", "kind": "http_write_endpoint"}`) while the matching stays Core mechanism (`_match_seam_http_endpoint`) — the same data/mechanism split the verb seam already used. Its body resolver is **shape 7**: the ONE text-suffixed path the command references, `@`-prefix stripped (curl's own `-d @file` form — a live probe caught that falling through after the fixtures passed), UNRESOLVED when two candidates survive, and route-scoped via `_Match.allow_referenced_file` so a stray path mention on a flag-bearing route is never read as the body. Endpoint matching is syntactic, so this is not the semantic-classification-by-regex anti-pattern: an over-match can only add a witness requirement or an advisory, never turn a real publication into an allow.

**Nothing read the advisories.** `record_advisory` had been writing every `UNRESOLVED` fall-through to `published-text-gate-advisories.jsonl` since the gate shipped, and no production code anywhere read that file — so each fail-open was invisible *by construction*, and Residual 9's "fails open with no distinguishing signal" held for the whole class, not just unregistered verbs. `self-diagnose.py`'s `scan_writer_gate_advisories` now reports the last 7 days grouped by `shape` (the grouping is the actionable part: it says whether to register a route or widen a resolver), placed ADVISORY in `self_diagnose_store` on purpose — the publications already went out, so blocking a later turn would charge an unrelated turn for a past fall-through. Reading the live sink for the first time showed at once why an unread trail rots: its recent rows were dominated by near-exact daily `shape 1`/`shape None` pairs whose counts track fixture cardinality — the **test suite** had been appending to the production sink all along. Fixed structurally by `advisory_sink()` plus the `CLAUDE_PUBLISHED_TEXT_GATE_ADVISORIES` override, isolated suite-wide in `scripts/tests/conftest.py`, so writer and reader move together.

**Generalizes past this gate:** a fail-open that records a diagnostic nothing consumes is indistinguishable from no diagnostic at all. When a gate is given a non-blocking path, the reader for its trail belongs in the same change, not a follow-up — and until a reader exists, nothing keeps test fixtures out of the production trail either.

## Deferred calibration

The attachment judge's calibration is filed as a Core backlog issue rather than left as an unqueued word in this plan: it needs a sample of ≥16 calls over distinct attachment bodies in two arms (machine-artifact vs. reader-facing-smuggled), under the sampler discipline `samples/judge-latency/README.md` records, replacing the `UNMEASURED` row in `scripts/lib/judge_latency.py`'s table for `judge_published_attachment`. Issue number recorded here once filed: **pending — Bash access for `gh`/`scripts/file-difficulty.py` was not granted to this stage's spawn (see the stage's own `REPLAN:`/`PERMISSION-REQUEST:` return); filing is deferred to the coordinator.**

## Text-rule judge calibration (2026-10-06)

Harness: `samples/text-rule-judge/` (`calibrate.py`, `check_calibration.py`, `labelled.jsonl`). 17 labelled org-neutral items, each judged twice, model sonnet:

| Measure | Result |
|---|---|
| Both historical incidents (2026-09-17, 2026-10-05) | genuine YES naming say-13 in every run |
| False denies on clean items | 0 |
| Verdict flips between the two runs | 0 |
| Latency median / p90 / max | 1.94 s / 2.37 s / 3.57 s (hook budget 190 s) |
| Prefilter trigger rate, historical TEXT publications | 42.05% (82 of 195; 2074 transcripts scanned) |

**Tuned to the set.** The first live run falsely denied a clean item that names a participant by name; the prompt's say-13 nuance was clarified and the same set rerun gave 0 false denies. The rerun is tuned-to-the-set evidence, not an out-of-sample estimate; the false-deny rate on unseen text is unmeasured. The latency row stays `UNMEASURED` (the `acceptance_judge` precedent); the figures live in `calibration.json`.

## Cost

Text path: 0 model calls when the prefilter is silent (about 58% of historical publications), at most 1 sonnet call otherwise (median 1.94 s, p90 2.37 s), inside the shared judge budget. The attachment path is unchanged.

## See also

- [[experience-leaf-schema]] — the `difficulty/v1` shape this leaf follows (standalone, outside `experience/`, so `verify-experience-leaf.py`'s path-scoped checks do not apply to it — it is indexed via `memory-global/MEMORY.md` directly instead).
- `scripts/lib/writer_pass.py` — the binding computation this leaf documents.
- `skills/specializations/tech-writer/publish-rules.toml` — the rule registry the content check reads.
- `scripts/hook-published-text-writer-gate.py` — the `PreToolUse` hook.
- `skills/tracker-management/SKILL.md` § How to publish — cross-referenced from there.
