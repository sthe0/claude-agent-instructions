---
name: claude-code-drops-pre-tool-call-text
description: Claude Code CLI (confirmed v2.1.198) does NOT render assistant text emitted before a subsequent tool call in the same turn — including text before an AskUserQuestion; only the turn's final message and the AskUserQuestion question/options themselves reach the user. Text sharing one assistant message with an AskUserQuestion is OFTEN (not always) also absent from the transcript — the drop is real but NOT deterministic (measured 2026-07-17, v2.1.211: 7/1707 such entries DID survive). hook-ask-text-split.py v2 therefore denies the whole mid-turn-ask topology regardless, which stays correct either way. Deliver all substantive content in the final message; at approval gates either ask in final-message text or embed content in the question itself.
type: reference
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-17
---

# Claude Code CLI drops assistant text emitted before a tool call

## Difficulty

Desired: everything the assistant writes in a turn reaches the user. Actual: in the Claude Code terminal client (confirmed v2.1.198), assistant text emitted *before* a subsequent tool call in the same turn is silently not rendered — the user sees only the tool interaction (e.g. the AskUserQuestion buttons) and the turn's final message. A plan summary, an answer to the user's question, or any deliverable placed before a tool call is lost without any error; the user reacts as if it was never written ("ты даже не показал план"), while the agent believes it delivered.

The user reports this worked in earlier client versions (summary text + AskUserQuestion both visible), so this is likely a client regression introduced by a `claude update`; the 2.1.190–2.1.198 changelog has no matching entry.

**The dropped text is OFTEN also absent from the transcript, not merely unrendered — but this is NOT deterministic (corrected 2026-07-17).** An earlier version of this leaf (2026-07-04, v2.1.198) stated as a universal that same-message text is *always* absent from the transcript, so *no* transcript-based hook could ever measure it. Re-measured 2026-07-17 on live client v2.1.211 (1080 local transcripts / 133957 assistant entries): that universal is REFUTED — 7 of 1707 AskUserQuestion entries DO carry a text block in the same entry AND are present in the transcript (provenance: session 313d8c96-64b uuid 0940fbcd client 2.1.169; session 8b22864c-28c uuids db456fc0/8be44fa6/7a78cb24 client 2.1.165; session 4b8c06f3-400 uuid 4c5b4c9f client 2.1.179), against 56 of 57 total mixed entries observed as `[thinking, text, tool_use]`. So the drop is real and common but evidently not deterministic across cases/versions/sizes; the original evidence (session e00ff3b4, one ~2500-char report absent) was generalized past what it supports. **`hook-ask-text-split.py` stays CORRECT and UNCHANGED regardless:** its ban is *topological* — it denies any ask in a turn that already ran a tool, which is sound whether or not the shared-entry text happens to be observable in a given case. Only this leaf's *rationale* sentence was overgeneralized, not the gate it justifies. Do NOT read this correction as grounds to relax `hook-ask-text-split.py`: the topological rule does not depend on the text being unobservable, and relaxing it re-opens the silent-drop for the common case. The two channels that reliably survive remain the turn's final message and the ask's own question/option fields.

> verified by: controlled experiment 2026-07-02, session 62d07353 on Claude Code v2.1.198 (darwin): a reply containing a plan-summary paragraph followed by an AskUserQuestion whose first question asked "do you see the text above?" — user answered "Нет, не вижу". Same session, same content delivered as a turn-final message (no tool calls after) rendered fine.
> verified by: transcript forensics 2026-07-04, session e00ff3b4 (linux): the failing turn's transcript shows only `thinking` + `tool_use` (AskUserQuestion) entries around the ask (entries 1350–1353) — a ~2500-char report emitted in the same message is absent; contrast entry 1358, an 85-char text block before a `Bash` tool_use in a *different* message, which IS transcripted. The drop is specific to text sharing one message with the following tool call.

## Guidance

- Put every deliverable (answers, summaries, findings, plan renderings) in the turn's **final** message, with no tool calls after it.
- **Text-then-buttons workaround (user-confirmed 2026-07-02):** to deliver long content AND still gate with AskUserQuestion, split them across two turns automatically — end turn N with the content as the final message after starting a background timer (`Bash sleep 2, run_in_background=true`); the timer's completion notification returns the turn, and turn N+1 opens **directly** with the AskUserQuestion (zero preceding text, so nothing is lost). The user sees: full rendered message → ~2 s pause → buttons.
- **Machine-enforced (v2, 2026-07-04):** `scripts/hook-ask-text-split.py` (PreToolUse, AskUserQuestion) denies **every mid-turn ask** — any ask in a turn that already completed a tool call (the topological rule, because the same-message text is unobservable, see § Difficulty) — and, for a turn-opening ask, still denies when >200 chars of same-turn assistant text precede it; both paths direct to the timer split. `hook-plan-delivery-gate.py` additionally guards the `PLAN_READY` plan-approval case. Retire/relax both only if the client regression is fixed upstream — re-verify with a live probe first (a mid-turn text+ask reply that asks "do you see the text above?"), because relaxing the topological rule without confirmation re-opens the silent-drop.
- For short confirmations, embedding content in the AskUserQuestion `question`/option `description`/`preview` fields also works (previews render side-by-side) but reads poorly and truncates for long content — prefer the timer split.
- If an AskUserQuestion timed out or a question arrived mid-turn, restate the full answer in the final message (CLAUDE.md § Escalation, "an unanswered user question survives the turn").
- Re-verify against newer client versions before relying on this leaf; if fixed upstream, update `last_verified` and note the fixed version.

## See also

- [[spawning-specialists]] — return-marker delivery has an analogous "only line-initial marker counts" trap on the spawn side.
