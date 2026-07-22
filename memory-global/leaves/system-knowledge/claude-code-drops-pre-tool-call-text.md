---
name: claude-code-drops-pre-tool-call-text
description: HISTORICAL (resolved 2026-07-21). On Claude Code terminal clients up to ~v2.1.211, assistant text emitted before a subsequent tool call in the same turn could be silently dropped from render — and often from the transcript too (non-deterministic; measured 2026-07-17 on v2.1.211: 7/1707 such entries survived). Re-measured FALSE on client 2.1.216 (2026-07-21): pre-tool-call text now renders and persists. The workaround hooks and the universal AskUserQuestion turn-split were retired; the current norm assumes a render-capable client. Kept as forensic record and as the provenance for that retirement.
type: reference
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-21
---

# Claude Code CLI drops assistant text emitted before a tool call (historical, fixed 2026-07-21)

## Difficulty

Desired: everything the assistant writes in a turn reaches the user. Actual (on clients up to ~v2.1.211): assistant text emitted *before* a subsequent tool call in the same turn was silently not rendered — the user saw only the tool interaction (e.g. the AskUserQuestion buttons) and the turn's final message. A plan summary, an answer to the user's question, or any deliverable placed before a tool call was lost with no error; the user reacted as if it was never written ("ты даже не показал план"), while the agent believed it delivered. The dropped text was OFTEN also absent from the transcript, but NOT deterministically so — measured 2026-07-17 on live client v2.1.211, 7 of 1707 AskUserQuestion entries DID carry a same-entry text block that survived in the transcript.

> **RESOLVED (2026-07-21).** Re-measured on live client **2.1.216**: text preceding a same-turn tool call now **renders and persists** in the transcript. The behavior was a genuine client regression, since fixed upstream. Provenance for the reversal (item c — grounded, not merely asserted): the 2026-07-21 live re-measurement on 2.1.216, plus three confirmed upstream issues — [anthropics/claude-code#21751](https://github.com/anthropics/claude-code/issues/21751) (transcript-drop, closed *not planned*), [#74558](https://github.com/anthropics/claude-code/issues/74558) (Fable-5 variant, references #21751), and [#24733](https://github.com/anthropics/claude-code/issues/24733) (VS Code render-disappear). On the strength of this, the render-workaround machinery — the universal AskUserQuestion turn-split and its dedicated enforcement hooks — was **retired**; the current norm assumes a render-capable client. This leaf is kept as the forensic record of why that machinery existed and why it was removed.

The original forensic evidence, preserved because it is why the fact existed:

> verified by: controlled experiment 2026-07-02, session 62d07353 on Claude Code v2.1.198 (darwin): a reply containing a plan-summary paragraph followed by an AskUserQuestion whose first question asked "do you see the text above?" — user answered "Нет, не вижу". Same session, same content delivered as a turn-final message (no tool calls after) rendered fine.
> verified by: transcript forensics 2026-07-04, session e00ff3b4 (linux): the failing turn's transcript showed only `thinking` + `tool_use` (AskUserQuestion) entries around the ask (entries 1350–1353) — a ~2500-char report emitted in the same message was absent; contrast entry 1358, an 85-char text block before a `Bash` tool_use in a *different* message, which WAS transcripted.
> verified by: re-measurement 2026-07-17, v2.1.211 (1080 local transcripts / 133957 assistant entries): the "always absent from transcript" universal was REFUTED — 7 of 1707 AskUserQuestion entries carried a same-entry text block present in the transcript. The drop was real and common but not deterministic.
> verified by: re-measurement 2026-07-21, client 2.1.216: pre-tool-call text renders and persists — the regression is fixed.

## Guidance

- **This is history.** Do not build new render-workarounds on it, and do not treat pre-tool-call text as unsafe on current clients. An ask may share its turn with preceding text and tool calls.
- The **plan-presentation two-turn flow still exists**, but it no longer rests on this fact — it is structurally forced by the delivery gate's receipt-binding to `plan_sha256` (the essence must land as a *completed* turn's final message so the hook can observe it). See [[ask-user-question-split-turn]] § Machine enforcement.
- If a future client regresses this again, re-open with a live probe (a mid-turn text+ask reply that asks "do you see the text above?") and record the regressing version here before restoring any workaround.

## See also

- [[ask-user-question-split-turn]] — the plan-presentation delivery gate that survives the render fix (receipt-binding, not render behavior); formerly also carried the retired universal turn-split.
- [[spawning-specialists]] — return-marker delivery has an analogous "only line-initial marker counts" trap on the spawn side.
