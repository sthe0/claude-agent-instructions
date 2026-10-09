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

State lives under `<agent home>/agentctl/mandates/<id>/` (default id `core-debt`; `$AGENTCTL_MANDATE_DIR` overrides the root): `mandate.json`, `events.jsonl` (audit), `labels.jsonl`, `items.jsonl`, `cycles.jsonl`, `cycles/<cycle_id>/commands.jsonl` (every external command the driver ran), `digests/<cycle_id>.md`, `cycle.lock`.

## Granting, watching, stopping

```
agentctl mandate-grant --by user      # creates the mandate: 14 days, $30/day, $120/week, 3 items/cycle
agentctl mandate-status               # the mandate, both spend windows, and whether a cycle may start
agentctl mandate-extend --by user     # push the expiry out
agentctl mandate-stop                 # pause, disable the timer, kill a running cycle
agentctl mandate-resume --by user     # unpause and close the breaker
```

`mandate-stop` needs no `--by`: stopping is always allowed. A cycle that sees its mandate paused before an item takes nothing further. A cycle already inside a spawn is killed and its item is charged. The driver's change check reads the mandate record the stop rewrote as tampering, so that item is recorded as `state-tampered` and the breaker opens; `mandate-resume` clears both the pause and the breaker.

The breaker opens on a failed item and stays open until `mandate-resume`. The reason is in `mandate-status` and in the next digest.

## One cycle

1. **Gate.** Expired, paused, breaker open, daily or weekly budget spent, or cycle wall-clock exhausted: the cycle logs a `limit` event with the reasons and exits `0`, with no digest. A second cycle while the lock is held exits `3` and records nothing.
2. **Reap.** Dead mandate scope records, worktrees under the temp root and local `mandate/*` branches nobody has checked out are removed. Remote `mandate/*` branches with no PR are *reported* in the digest, never deleted.
3. **Baseline.** The test suite and the two gate scripts run once on `origin/main`. A red baseline takes no item (the cycle still triages) and does not open the breaker.
4. **Fix, oldest eligible first** (the planning board's rank, when one exists). Per item: worktree and branch, a `developer` spawn, then in order: constitution check on the diff, org-neutral check of commit messages + added lines + PR text, the suite compared to the baseline (one rerun of failing ids), the gate scripts compared to the baseline, an independent `code-reviewer` spawn that must say `VERDICT: accept`, then push of the `mandate/<issue>-<date>` branch, `gh pr create --base main`, and a comment on the issue linking the PR. The gate is re-evaluated before the next item.
5. **Triage.** Unlabelled owner-authored domain issues are judged in batches of ten, oldest first. A verdict is logged as a label row *before* `gh issue edit` applies it, and a short comment explains it and how to veto. Nothing is posted unless the org-neutral check of the comment is clean.
6. **Digest.** What was taken, with outcomes, cost and PR links; what was triaged and how to veto it (`no-auto`); the breaker reason when open; orphan remote branches and other notes. It goes through the notifier and a copy is always written to `digests/<cycle_id>.md`.

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

Discovery is sorted by file name and the first loadable plugin wins; a file that fails to import or has no `send` is skipped. A plugin that raises, or returns false, makes that digest a failed delivery: the file copy is still written, and its labels stay ineligible.

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

`items.jsonl` has one row per item with `outcome` (`pr-opened`, `failed`, `overrun`, `declined`, `limit`) and, for `failed`, a `failure_kind` (`tests`, `review-reject`, `constitution`, `permission`, `org-neutral`, `state-tampered`, `spawn-error`, `infra`). `declined` is a developer that reported `INCOMPLETE` / `ESCALATE` / `CLARIFY` / `REPLAN`: the issue gets `auto-no` and the breaker stays closed. Resume with `mandate-resume` after reading the reason.
