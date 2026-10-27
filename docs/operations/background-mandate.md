# The background debt mandate

A standing, bounded authority for the agent to work through labelled backlog issues overnight and to open pull requests for the user to review. The design and its reasons are in [ADR-0008](../adr/0008-standing-mandate.md); this page is how to run it.

Nothing here merges, pushes to trunk or creates an issue. The user's merge of the PR is the only act that lands a change.

## Parts

| Part | Where | Role |
|---|---|---|
| Rules | `scripts/agentctl/mandate.py` | Pure bounds: the start gate, spend windows, eligibility, veto window, constitution, baseline-relative tests |
| Store | `scripts/agentctl/mandate_store.py` | Mandate record, audit logs, cycle lock, the stop mechanics |
| Verbs | `agentctl mandate-grant`, `-extend`, `-resume`, `-stop`, `-status` | The user's levers; `grant`, `extend` and `resume` are `--by user` only |
| Driver | `scripts/mandate-cycle.py`, package `scripts/mandate_cycle/` | One cycle: reap, baseline, fix eligible items, triage the rest, deliver a digest |
| Timer | `scripts/install-mandate-timer.sh` | `agent-debt-cycle.timer`, nightly at 01:00, runs `mandate-cycle.py run` |

State lives under `<agent home>/agentctl/mandates/<id>/` (default id `core-debt`; `$AGENTCTL_MANDATE_DIR` overrides the root): `mandate.json`, `events.jsonl` (audit), `labels.jsonl`, `items.jsonl`, `cycles.jsonl`, `cycles/<cycle_id>/commands.jsonl` (every external command the driver ran), `digests/<cycle_id>.md`, `notices/<cycle_id>.md` (refused-start notices), `redelivered/<cycle_id>.md` (re-sent digests), `cycle.lock`.

## Granting, watching, stopping

```
agentctl mandate-grant --by user      # creates the mandate: 14 days, $30/day, $120/week, 3 items/cycle
agentctl mandate-status               # the mandate, both spend windows, and whether a cycle may start
agentctl mandate-extend --by user     # push the expiry out
agentctl mandate-stop                 # pause, disable the timer, kill a running cycle
agentctl mandate-resume --by user     # unpause and close the breaker
agentctl mandate-retry --issue N --by user  # let the cycle take issue N again after it failed
```

`mandate-stop` needs no `--by`: stopping is always allowed. A cycle that sees its mandate paused before an item takes nothing further. A cycle already inside a spawn is killed and its item is charged. The driver's change check reads the mandate record the stop rewrote as tampering, so that item is recorded as `state-tampered` and the breaker opens; `mandate-resume` clears both the pause and the breaker.

The breaker opens on a failure that says something about the cycle rather than about one issue, and stays open until `mandate-resume`. Cycle-level failure kinds are `spawn-error`, `infra` and `state-tampered`; an unknown outcome or kind opens it too. The item-local kinds (`tests`, `review-reject`, `constitution`, `permission`, `org-neutral`) are recorded and the cycle takes the next item. Three item-local failures in a row open the breaker as well, with the reason `3 consecutive item failures: #a, #b, #c`; a `pr-opened` row, or a cycle-level failure, ends the run, a `declined`, `overrun` or `limit` row neither extends nor ends it, and failures from before the last `mandate-resume` (or, if there was none, the grant) do not count. The reason is in `mandate-status` and in the next digest.

An issue whose latest attempt failed item-locally is not taken again: its candidate reason is `failed-before`, and the digest lists it under "Not retried (failed before)" with the command to allow it. Only the user lifts that, with `mandate-retry --issue N --by user`, which records a `retry-allowed` event; a later `declined`, `overrun` or `limit` row does not lift it, and a failure after the retry excludes the issue again. The cycle never calls `mandate-retry`.

## One cycle

1. **Gate.** Expired, paused, breaker open, daily or weekly budget spent, or cycle wall-clock exhausted: the cycle logs a `limit` event with the reasons and exits `0`, with no digest. A second cycle while the lock is held exits `3` and records nothing.
2. **Reap.** Dead mandate scope records, worktrees under the temp root and local `mandate/*` branches nobody has checked out are removed. Remote `mandate/*` branches with no PR are *reported* in the digest, never deleted.
3. **Baseline.** The test suite and the two gate scripts run once on `origin/main`. A red baseline takes no item (the cycle still triages) and does not open the breaker.
4. **Fix, oldest eligible first** (the planning board's rank, when one exists). Each item runs as an *ordinary task* of the engine (see "An item is an ordinary task" below): worktree and branch, a parent engine session, a `linked` developer child, then one judgement, `mandate-cycle.py check-item --item-dir <dir>`. When it is green, the driver pushes the exact commit that passed (by sha, to the `mandate/<issue>-<date>` branch; a worktree that moved since fails the item as `state-tampered`), runs `gh pr create --base main` and comments on the issue linking the PR. The gate is re-evaluated before the next item.
5. **Triage.** Unlabelled owner-authored domain issues are judged in batches of ten, oldest first. A verdict is logged as a label row *before* `gh issue edit` applies it, and a short comment explains it and how to veto. Nothing is posted unless the org-neutral check of the comment is clean.
6. **Digest.** The items with outcomes, cost and PR links; the labels the cycle applied; what was triaged and how to veto it (`no-auto`); triage comments the org-neutral check held back; limits hit (daily, weekly, cycle minutes); overruns; open mandate pull requests; remote `mandate/*` branches with no PR; issues declined, each with its reason; issues not retried because they failed before; the mandate's expiry date, the breaker state and reason, and the kill switch `agentctl mandate-stop`. A cycle the gate refuses at the start writes no digest. It goes through the notifier and a copy is always written to `digests/<cycle_id>.md`. When the mandate expires within 3 days, the digest opens with an expiry warning and the `mandate-extend` command.

### An item is an ordinary task

The driver opens a parent engine session for the item: `start`, `classify`, `plan`, then `submit-plan` of the generated *item norm* (`norm.toml` in the item directory) and `approve --by mandate:<id>`. The mandate approves exactly that norm and skips only the presentation, grants-approval and plan-review blockers (`mandate_approval_problems` in `scripts/agentctl/mandate.py`). The developer is spawned as a `linked` child of the parent's stage 1 (ADR-0010), so it runs its own engine contour and its spawn spend (a nested thinker, say) is charged to the item and to the 24h/7d windows through `nested-spawn-cost` events.

Each engine ask of the child is answered by the pure rule `classify_ask` in `scripts/agentctl/mandate.py`:

- A *settled* ask gets its answer recorded as the mandate's, `mandate:<id>`, never the user's.
- An ask that is `exceeds-mandate` ends the item through `stop_disposition`. Effort spend or wall-clock ends it as `overrun`; `account-limit` ends it as `limit` and stops the cycle with no breaker; anything else is `declined` with the `exceeds-mandate` marker and a reason. None of these opens the breaker.

An account or rate limit seen in the new spawn's transcript ends the item the same way, as `limit` with reason `account-limit`.

A failed round is rejected with `agentctl reject --by mandate:<id>` and the child resumes, for at most `effort-replan-absolute` rounds. There is no size pre-check and no `needs-plan` decline: an over-bound diff is judged by `check-item`, not refused up front.

`check-item` runs, in order: the constitution check on the diff; the bound rule (production lines changed against `small-change-max-lines`, counted as in `scripts/agentctl/inline_class.py`; an over-bound diff passes only when the child's last approval stands on the parent norm); the org-neutral check of commit messages, added lines and PR text; the suite compared to the cycle's stored baseline (one rerun of failing ids; a baseline-passed test id the item's own run never saw is *excused* when it maps to a module the diff touches, a rename counting both its old and new path as one module, and that whole module has no new failure -- one not-passing id that was not already not-passing on the baseline voids the excuse, and an id in an untouched module still fails); the gate scripts compared to the baseline. The item norm's negative control re-runs it with `--head <base_sha>` and expects red. An independent `code-reviewer` spawn must still say `VERDICT: accept`.

Every stopped item that changed files keeps its work as `salvage_patch` (`salvage.patch` in the item directory). It is fail-open: an error saving it is an event, not a failed item.

### A refused start sends a notice

A start the gate refuses also goes through the notifier, as a notice rather than a digest: it lists the reasons, since when the cycle has been blocked, and per reason the command that unblocks it (`mandate-resume` for `paused` and `breaker-open`, `mandate-extend` for `expired`, "clears by itself at <time>" for the budgets, `mandate-grant` for `no-mandate`). At most one delivered notice goes out per UTC day for the same set of reasons; a changed set, or a day on which the earlier notice only reached the `file` notifier or failed, sends again. Each delivery attempt is a `refused-notice` event, the cycle row gets `notice` = `sent`, `failed` or `suppressed`, and a copy lands in `notices/`. A notice is never recorded as a digest, so it cannot start a veto window.

### Re-sending an undelivered digest

```
python3 scripts/mandate-cycle.py redeliver-digest --cycle <cycle_id> [--json]
```

Use it when a cycle's digest only reached the `file` notifier or its delivery failed (`digests/<cycle_id>.md` exists but the delivery does not count). It sends the stored text, prefixed with a line saying the veto window counts from this message, through the notifier, writes `redelivered/<cycle_id>.md`, records the delivery (so the veto window of that cycle's labels starts from it) and logs a `digest-redelivered` event. A digest that already counts as delivered is not sent again (exit `0`, `sent` false); an unknown cycle or a missing stored digest exits `1`. It is a driver command, not an `agentctl` verb with `--by user`, because it changes no authority: it only re-sends text the cycle already wrote.

## Eligibility, in one place

An issue is taken when all of these hold: authored by the repo owner; carries `backlog` or `difficulty`; carries `auto-ok`; carries no `no-auto`; has no open PR referencing it; is not `severity:high` when the label was set by a cycle; and, when a cycle set `auto-ok`, a digest that announced it was **delivered through a non-file notifier** at least `veto_window_hours` (24) ago.

The last condition is why a machine with no notifier plugin never acts on its own labels: it fixes what the user labelled and keeps triaging, and the digests accumulate as files under `digests/`. To let the cycle's labels count, install a notifier.

## Notifier plugins

A notifier is a Python file in `${CLAUDE_MANDATE_PLUGIN_DIR:-<config root>/mandate-plugins}/notifiers/`:

```python
NAME = "chat"              # optional; the file stem when absent

def send(text: str) -> bool:
    ...                    # True only when the message was delivered
```

Discovery is sorted by file name and the first loadable plugin wins; a file that fails to import or has no `send` is skipped, and so is one named `file` (by `NAME` or by file stem: that name belongs to the built-in, which never counts as delivered). A plugin that raises, or returns false, makes that digest a failed delivery: the file copy is still written, and its labels stay ineligible.

Check what a cycle would use, and that it works, without a mandate or a cycle:

```
python3 scripts/mandate-cycle.py notify-test --json   # {"ok": ..., "notifier": ..., "counts_as_delivered": ...}
```

`counts_as_delivered` is false for the `file` notifier. Exit `1` means the chosen notifier did not deliver.

## Dry run

```
python3 scripts/mandate-cycle.py run --dry-run --json
```

Reads the forge and the board and prints the gate, every domain issue with its eligibility and reason, and what a cycle would take and triage. It spawns nothing, runs no test, creates no label and writes nothing (with no mandate it evaluates a synthetic one, and `would_take` is empty because the gate says `no-mandate`).

## Installing the timer

```
bash scripts/install-mandate-timer.sh              # env file + units, enable --now
bash scripts/install-mandate-timer.sh --write-env  # only ~/.config/agent-debt-cycle/env
bash scripts/install-mandate-timer.sh --uninstall  # disable and remove the units
```

Run it from the canonical checkout: the unit's `ExecStart` is `%h/claude-agent-instructions/scripts/mandate-cycle.py run`, and its output is appended to `~/.local/log/agent-debt-cycle.log`. The env file carries `PATH` and, when set in the installing shell, `CLAUDE_AGENT_HOME`, `CLAUDE_CONFIG_DIR` and `GH_CONFIG_DIR`. It never carries a token; the cycle reads credentials from the `gh` config dir. `Persistent=false`: a night the machine was off is skipped, not caught up.

## What a failed item looks like

`items.jsonl` has one row per item. Every row carries `cycle_id`, `issue`, `branch`, `pr_url`, `cost_usd`, `duration_s`, `tests`, `review`, `excused_tests` (the first 10 excused test ids), `excused_count` (both are written before the gate decision, so a `failed` row carries them too; the rerun verdict does not hold an excused id against the item either) and `gates` (`constitution`, `org_neutral`, `lint`, each `pass`, `fail` or `not-run`); a gate that was never reached reads `not-run`. The raw exit codes of the baseline-compared gate scripts are in `gate_exit_codes`. `outcome` is one of `pr-opened`, `failed`, `overrun`, `declined`, `limit` and, for `failed`, a `failure_kind` (`tests`, `review-reject`, `constitution`, `permission`, `org-neutral`, `state-tampered`, `spawn-error`, `infra`). `declined` is an item stopped by an ask that is `exceeds-mandate` (the row carries the `exceeds-mandate` marker and the reason) or a developer that reported `INCOMPLETE` / `ESCALATE` / `CLARIFY` / `REPLAN`: the issue gets `auto-no` and the breaker stays closed. `overrun` is an effort spend or wall-clock ask; `limit` with reason `account-limit` is an account or rate limit, which also stops the cycle. Neither opens the breaker. A stopped item that changed files keeps its work as `salvage_patch`: `salvage.patch` in the item directory. Saving it is fail-open, so a failure to write it appears as an event and never changes the outcome. Resume with `mandate-resume` after reading the reason; allow a failed issue again with `mandate-retry`.
