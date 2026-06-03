---
name: workflow-debug-investigation
description: Investigation checklist when a long-running orchestrated workflow fails — baseline run, block topology, code delta, then infra logs
type: reference
---

# Workflow debug investigation

Use during **overcome-difficulty § Investigation** when the failure is in an orchestrated pipeline: Nirvana workflow instances, Arcadia CI launches, Reactor reactions, Hitman processes, multi-stage Sandbox graphs.

**Order matters.** Do not jump to infra logs (YT stderr, pod logs, Monium) until the first three passes are done — anchoring on the loudest symptom (timeout, OOM, cancel) without topology is a recurring failure mode.

## Checklist (in order)

### 1. Reference baseline

| Step | Action |
|------|--------|
| Find known-good run | Same workflow / flow id, comparable parameters (checkpoint, baskets, env). Project memory may name one; else search recent successful WIs or CI launches. |
| Compare at block level | Which blocks succeeded/failed in baseline vs failing run? Same block names and progress points? |
| Terminal status in context | `cancel` on a child WI may be **normal cleanup** (e.g. stop launcher after eval) — compare whether baseline had the same pattern at a similar progress. |

**Falsifier:** If baseline shows the same terminal status at the same graph position and baseline succeeded, the status alone is not the root cause.

### 2. Topology / causality

| Step | Action |
|------|--------|
| Block completion order | List blocks that reached `success` / `failure` / `cancel` **before** the reported failure block. |
| Dependencies | Which block outputs feed the failing block? Shared workflow instance ids between blocks? |
| Failure block ≠ root cause | Meta graph may fail because a **child** WI failed; child may fail because an **earlier sibling** (Stop vs Start) changed shared state. |

**Falsifier:** If block A succeeded and block B failed, but B depends on state that A's successor C modified — root cause is orchestration order, not B's infra.

### 3. Code delta

| Step | Action |
|------|--------|
| Ticket scope | If debugging follows a branch/ticket — `arc diff` (or PR diff) on code paths for the failing block names **before** deep infra dives. |
| Behavior change | Did a refactor change which terminal statuses raise (e.g. treat `cancel` as failure when trunk only raised on `failure`)? |

**Falsifier:** If diff explains the mismatch between baseline and failing behavior, fix code/graph — not GPU quota.

### 4. Infra logs (last)

Only after 1–3: YT job stderr, Nirvana block logs, watt/hahn ops, launcher health, Monium — scoped to the **localized** failing step from investigation, not the whole chain.

#### Reading a failed Nirvana block's stderr

**Preferred:** load the `nirvana` skill → `get_failed_blocks(recursive=true)` → for a composite block (e.g. `eval_baskets`) this returns the **nested** `workflow_instance_id`; use that, not the top-level → `get_block_logs` → `get_log_content`, `stderr.log` first.

**If the `mcp__nirvana__*` tools are not connected this session** (check the tool list) — do **not** grind the `vh3.async_api` / `nirvana_api` Python clients: their `get_block_logs` returns `[]` for failed-block logs and there is **no** `get_log_content` equivalent. Use the raw HTTP endpoint instead:

1. `yt --proxy <cluster> get-job-stderr <job_id> <op_id>` (robot YT token) returns the **launcher wrapper** stderr, which contains the pointer `…/process/{iid}/graph/result/{block_guid}` — that gives you `{iid}` and `{block_guid}`.
2. Fetch the user-code stderr directly:

   ```bash
   curl -H "Authorization: OAuth $NIRVANA_TOKEN" \
     "https://nirvana.yandex-team.ru/ui-api-proxy/nv-api/api/logs/{iid}/{block_guid}/stderr.log?file_name=stderr.log&group=DEFAULT_GROUP"
   ```

   (`stdout.log`, `job_launcher.err.log`, `job_launcher.diag.log` available the same way.)

> verified by: DEEPAGENT-415 workflow_url smoke (2026-06-03) — 15+ `vh3`/`nirvana_api` Python-client calls returned `[]` / non-serializable `Obj` and produced two wrong "infra" conclusions; the raw endpoint returned the full traceback in one `curl`, and the root cause (`YtResolveError`: eval-folder not pre-created) was on stderr line 1.

## Hypothesis portfolio (required)

Maintain **at least two** competing hypotheses until one is falsified. For each:

- **Hypothesis** — one sentence.
- **Would confirm** — observation that supports it.
- **Would falsify** — observation that kills it (must be checkable in ≤3 tool calls when possible).

Example (DEEPAGENT-403):

| Hypothesis | Falsifier |
|------------|-----------|
| Model failed to start (health timeout) | Eval blocks never reached; balancer never registered |
| Stop block cancelled launcher while Start still polls same WI | Stop LLM success + Start LLM failure; success baseline also had launcher cancel after eval |

## When to read this leaf

- overcome-difficulty Investigation table row **Reference baseline** / **Topology** / **Code delta** points here.
- Project-specific pipeline signals: see project `overcome-difficulty-signals-pipelines.md` when present.

> verified by: DEEPAGENT-403 val smoke post-mortem (2026-06-03) — cancel on launcher was normal in success run; root cause was Stop→Start race on shared launcher WI.
