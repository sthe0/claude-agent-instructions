---
name: agentctl-ledger-gate-statement-text
description: "The agentctl ledger plugin (auto-activates on SUBSTANTIVE + deliverable_kind reasoning|mixed) blocks resolve until every load-bearing claim is enumerated and dispositioned, but no CLI surface (ledger-check, status) prints a raised candidate's actual statement text — only its bare enum-N id; the text lives only in state.plugins.ledger.candidates[].statement inside the raw session-state JSON."
type: reference
schema: leaf/v1
created: 2026-10-01
last_verified: 2026-10-01
---

## Difficulty

`agentctl resolve` failed with a `[ledger] no load-bearing claims enumerated` /
`enumeration cross-check not run` blocker — a gate not previously documented in this
project's memory. Running `agentctl ledger-enumerate --artifact <file>` raised 36
candidates (`enum-1`..`enum-36`), and `agentctl ledger-check` listed all 36 as
`"undispositioned enumeration candidate '<id>'"` — but gave only the bare ids, no
statement text. Neither `ledger-check` nor `status` nor any `ledger-*` `--help` surface
prints the text needed to judge and disposition each candidate (`ledger-dispose --as
recorded --claim <id>` / `--as dismissed --reason <text>`).

## Guidance

- The `ledger` plugin (`scripts/agentctl/README.md`, grep "ledger") divides labor:
  **which claims/decisions exist is the coordinator's cognition**; the engine only
  enforces structural closure (every load-bearing claim is `axiom`/`derivation`/
  `assumption` with grounding, and every raised candidate is dispositioned) via a pure
  DFS acyclic-graph check (`validate_ledger`, reused from `plan._validate_graph`) — it
  never judges content.
- `ledger-enumerate` is both the trigger (sets `enumerated: true`) and an independent
  recall-booster: it runs `advisor.enumerate_claims` (a `claude -p --model sonnet` pass
  over the artifact) and raises whatever it finds as `candidate`s. Your own read of the
  artifact is primary; this cross-check only widens recall (the README itself says it is
  "narrowed, not closed") — it never substitutes for your own enumeration.
- **The candidate statement text is not exposed by any `ledger-*` command.** It lives in
  the session's raw state JSON at `state.plugins.ledger.candidates[]`, each entry shaped
  `{id, statement, disposition, claim, reason}`. Locate the file via a narrowly-scoped
  `find ~/.claude-agent/agentctl/state -maxdepth 1 -iname "<session-id>.json"` (never an
  unscoped `$HOME`-rooted find — see the project's FUSE-mount find-hook constraint) and
  read it directly, e.g.:
  `python3 -c "import json; d=json.load(open('<path>')); print(json.dumps(d['plugins']['ledger'], indent=2, ensure_ascii=False))"`.
- Disposition strategy that worked cleanly: when `ledger-enumerate` is run over a file
  that predates this session's edit (common for a memory-leaf extension), most raised
  candidates describe **pre-existing content already established in prior sessions** —
  dismiss those in bulk with a reason naming that ("pre-existing content ... out of this
  session's edit scope"); declare `ledger-add` claims (axiom/assumption, with
  `--source`/`--basis`) only for the candidates that trace to **this session's own new
  text**, then `ledger-dispose --as recorded --claim <id>` those.
- `ledger-check --session <sid>` is read-only and safe to re-run after each disposition
  round to confirm `"ledger closed"` / `"blockers": []` before retrying `resolve`.

## See also

- [[question-provenance-gate]] — the sibling plan-approval-axis gate (`premise`
  plugin); this leaf covers the resolution-axis `ledger` gate instead.
- [[resolve-by-user-needs-real-ask]] — the ledger gate is a separate precondition from
  the resolution-confirmation `AskUserQuestion`; both must clear before `resolve`
  succeeds.
