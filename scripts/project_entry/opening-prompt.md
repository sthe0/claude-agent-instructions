# Opening dialogue — first-turn brief

This is your opening prompt for a `claude-task` session. Above this template
the launcher inserted the brief that `opening.py` composed mechanically —
`ticket:` / `artifacts:` / `mode:` — from whatever already exists for this
task. Read that brief before doing anything else; do not re-derive it.

This template is universal across projects. A project may override it via the
registry field `opening_prompt_path` (see `scripts/project_entry/projects.py`).

## Branch on `mode:`

The brief's `mode:` line is a MECHANIZED verdict, computed from three
observable probes — a plan file whose CONTENT mentions the task key, a
tracker comment authored by you, a git branch ahead of its merge-base — never
from a filename or an exact-name match. You may DEMOTE `mode:
resume-candidate` back to the opening branch if, having actually read the
listed artifacts, you judge they do not amount to settled requirements plus
in-flight work. You may never PROMOTE `mode: opening` to resume: if the
mechanism found nothing, there is nothing to resume from, and no artifact you
imagine can substitute for one you can actually read.

### mode: opening

Digest whatever ticket context exists (or note plainly that none does).
Elicit the missing requirements from the user until the task is formulated
and the result image is explicit. Do not ask about anything the ticket or
artifacts already settle.

### mode: resume-candidate

Read every artifact the brief lists — plan file, tracker comments, branch
commits — and reconstruct the prior agent work from them. Continue from that
reconstructed state. Do NOT re-interrogate the user about anything already
settled by those artifacts; ask only about what is genuinely still open.

## The two-turn ask invariant

A hook denies every `AskUserQuestion` that follows a completed tool call in
the same turn, and any assistant text sharing a message with a subsequent
tool call is silently dropped, not merely unrendered. So the opening dialogue
always takes two turns:

- **Turn 1** — read the task, memory, and any plan artifacts the brief
  names. Deliver your understanding (the ticket digest, or the reconstructed
  prior state) as this turn's FINAL text message. Then, with no further tool
  calls this turn, start a background `sleep 2` timer.
- **Turn 2** (opened by the timer's completion notification) — issue exactly
  ONE `AskUserQuestion`, bundling the 3-4 clarifying questions that remain,
  with ZERO text before it.

Never put the digest and the `AskUserQuestion` in the same turn.

## Presenting the formulation

Drive the opening branch to a state where the task is formulated and the
result image is explicit, then present that formulation to the user for
confirmation. Never rewrite the ticket description — the formulation is
always additive, never a replacement.

If the resolved tracker backend defines `tracker_comment` — probe with
`declare -F tracker_comment` before relying on it — post the confirmed
formulation as a NEW comment once the user confirms it. If the backend does
not define the verb, present the formulation and stop; do not attempt to
post anything.

## Language

Conduct this dialogue in the language of the ticket text. When there is no
ticket text, or its language is ambiguous, fall back to the language the
user writes in. Never hardcode a language.

## Before any production edit

The opening dialogue produces, at most, a formulated task and an explicit
result image — never a plan, never a code edit. The normal plan-approval
gate still applies in full once the task is formulated: build the plan,
present it, and wait for explicit user approval before touching any
production file.
