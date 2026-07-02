---
name: claude-code-drops-pre-tool-call-text
description: Claude Code CLI (confirmed v2.1.198) does NOT render assistant text emitted before a subsequent tool call in the same turn — including text before an AskUserQuestion; only the turn's final message and the AskUserQuestion question/options themselves reach the user. Deliver all substantive content in the final message; at approval gates either ask in final-message text or embed content in the question itself.
type: reference
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
---

# Claude Code CLI drops assistant text emitted before a tool call

## Difficulty

Desired: everything the assistant writes in a turn reaches the user. Actual: in the Claude Code terminal client (confirmed v2.1.198), assistant text emitted *before* a subsequent tool call in the same turn is silently not rendered — the user sees only the tool interaction (e.g. the AskUserQuestion buttons) and the turn's final message. A plan summary, an answer to the user's question, or any deliverable placed before a tool call is lost without any error; the user reacts as if it was never written ("ты даже не показал план"), while the agent believes it delivered.

The user reports this worked in earlier client versions (summary text + AskUserQuestion both visible), so this is likely a client regression introduced by a `claude update`; the 2.1.190–2.1.198 changelog has no matching entry.

> verified by: controlled experiment 2026-07-02, session 62d07353 on Claude Code v2.1.198 (darwin): a reply containing a plan-summary paragraph followed by an AskUserQuestion whose first question asked "do you see the text above?" — user answered "Нет, не вижу". Same session, same content delivered as a turn-final message (no tool calls after) rendered fine.

## Guidance

- Put every deliverable (answers, summaries, findings, plan renderings) in the turn's **final** message, with no tool calls after it.
- At an approval gate, either (a) ask in final-message **text** and accept a typed reply, or (b) put the content the user must read into the AskUserQuestion `question`/option `description`/`preview` fields — those render; the text before the call does not.
- If an AskUserQuestion timed out or a question arrived mid-turn, restate the full answer in the final message (CLAUDE.md § Escalation, "an unanswered user question survives the turn").
- Re-verify against newer client versions before relying on this leaf; if fixed upstream, update `last_verified` and note the fixed version.

## See also

- [[spawning-specialists]] — return-marker delivery has an analogous "only line-initial marker counts" trap on the spawn side.
