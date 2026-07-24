---
name: solved-by-007-marker-and-usage-stats
description: The resolution gate auto-stamps a `solved_by_007` marker (Startrek tag / GitHub label) on a resolved task's tracker ticket so resolved tasks become a searchable precedent corpus, and the tracker key it now captures on the quality-ledger row backs a single local usage report — why the stamp is engine-executed while the sibling ticket-status transition stays skill-executed.
type: reference
schema: leaf/v1
created: 2026-07-13
last_verified: 2026-07-13
---

## Difficulty

A task the system resolves left no trace on its tracker ticket, so there was no way to later search the tracker for "tasks the system solved" as a precedent corpus. Separately, the per-task quality row written at resolution (`~/.local/log/claude-task-quality.jsonl`) recorded `task_id` but not the ticket/issue it corresponded to, so usage could not be joined back to tickets and no single command answered "how much is this system being used, here vs. everywhere".

## Guidance

**The marker.** `scripts/agentctl/solved_marker.py` defines `SOLVED_MARKER = "solved_by_007"` (the literal codename never appears — see CLAUDE.md's org-neutral convention) and a dispatcher `stamp(tracker_key)` that classifies the key by SHAPE via `key_shape`: an issue-key shape (`^[A-Z][A-Z0-9]+-\d+$`, identical to `agentctl.classify.TRACKER_KEY_RE`) routes to the `add_tag` of whatever channel this machine is configured for, resolved at stamp time via `difficulty_channel.adapters.load_adapter` — Core knows the key shape but names no tracker, and the adapter that owns the tag-add semantics lives in the machine-local plugin dir; a fully-qualified GitHub ref (`owner/repo#N`, `owner/repo/issues/N`, or a full URL) routes to the built-in `adapters.github.add_label` (`POST /repos/<repo>/issues/<n>/labels` with `[label]`). Both verbs reuse their adapter's existing auth/HTTP helpers rather than a new client. `stamp` never raises: no key, no token, an HTTP error, an unclassifiable key, a configured channel with no adapter plugin installed on this machine, or a **bare** GitHub number (rejected rather than silently defaulted to some repo) all resolve to `{"stamped": False, "skipped_reason": ...}`.

**Where it runs.** `cmd_resolve` (in `scripts/agentctl/cli.py`) calls `solved_marker.stamp(tracker_key)` unconditionally, right after the quality row is written, wrapped in its own belt-and-suspenders `try/except` — a marker failure can never undo or block a resolution that has already happened. The key comes from `state.tracker_key` (or, failing that, `state.task_id` when it happens to look like a tracker key). `cmd_classify` was extended so a fully-qualified GitHub ref reaches `state.tracker_key` too: the existing `TRACKER_KEY_RE` branch (which also forces `weight_class=SUBSTANTIVE`) is untouched, and a GitHub ref that doesn't match that regex is stored via a second, non-substantive-forcing branch — so the marker channel and the classify weight-forcing rule stay independent.

**Why the marker is engine-executed while the ticket status transition stays skill-executed.** The `tracker` plugin (`plugins_tracker.py`) already draws this line for the *sibling* obligation on the same `resolve` event: the engine owns **when** (gating `resolution` until the mandatory publications are recorded), the `tracker-management` skill owns **what/where** (comment content, which ticket, transport). The marker stamp is different in kind: whether to apply it is *fully decidable from observed state* — the task resolved, and a tracker key is known — with no comment content or judgement call involved (the user's decision at planning was fixed: automatic, unconditional, no quality threshold). A state-decidable rule belongs in the engine per CLAUDE.md's "separate rule from perception, determinize the rule at its proper structural level" — leaving it to a plugin nudge would reintroduce a manual step the user explicitly rejected. The ticket status transition, by contrast, needs the skill's judgement over comment wording and target, so it stays skill-executed.

**Finding the corpus.** Startrek: TQL `Tags: "solved_by_007"` (cross-queue; tags are case-sensitive and this one is exact). GitHub: `?labels=solved_by_007` on the issues search/list API.

**GitHub production preconditions (unit-green ≠ live-fires-here).** `add_label` requires a fully-qualified ref (`owner/repo#N` or equivalent) — a bare issue number is rejected, never defaulted to a repo. The GitHub labels endpoint does **not** auto-create a label: the `solved_by_007` label must already exist in the target repo, or the `POST` 422s and the dispatcher's fail-open swallows it as a silent skip. Neither precondition is exercised by the unit tests (they fake HTTP), so green tests are not proof the stamp actually lands in a given repo — check the label exists and the stored ref is qualified.

**The usage report.** `scripts/agent-stats.py` is a read-only aggregator over three existing append-only ledgers — `claude-task-quality.jsonl` (resolved tasks, mean quality, and now `tracker_key` — a resolved row with a non-null `tracker_key` is a "marked precedent"), `claude-policy-ledger.jsonl` (invocations), `claude-spawn-costs.jsonl` (spawns, cost) — with `--project`/`--global` scoping mirroring `policy-scorecard.py`/`cost-report.py`. It adds no new central store; the only new capability is the `tracker_key` join field on the quality row.

## See also

- [[docs-accompany-architectural-change]] — why the resolution gate's new side effect must be reflected on the read-first surface, not just in code.
- [[tracker-write-token]] — the Startrek write-auth mechanics (`~/.tracker-token`) that tracker's plugin `add_tag` reuses.
- [[policy-effectiveness-tracking]] — the sibling per-session ledger + scoping convention `agent-stats.py` mirrors.
- `scripts/agentctl/README.md` § Plugins — the `tracker` plugin's when/what-where split this leaf parallels.
