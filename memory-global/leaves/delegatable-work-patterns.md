---
name: delegatable-work-patterns
description: Two recurring work shapes the opus main thread must hand to a CHEAP-model sub-agent instead of doing inline — (A) post-spawn monitoring loops, (B) initial codebase/data exploration before editing — plus the model-tier heuristic for any spawn. Delegation today fires only for "open research question → return digest"; these two shapes are missed because they don't feel like research.
metadata:
  type: feedback
---

**Difficulty:** the main thread runs on the expensive Opus model, yet routinely does high-volume mechanical work **inline** (so the volume stays in opus context and bills opus rates) when it should hand that work to a cheap-model sub-agent that returns only the conclusion. A 48h audit (2026-06-17, 65 sessions) found **~2150 main-thread Read+Bash calls**, the `Agent` tool used in only **~21/65 sessions**, and of 48 sub-agent spawns: **27 opus / 21 haiku / 0 sonnet**, with **44/48 spawned without an explicit `model:`** — but "no explicit model" ≠ "ran opus": the 21 haiku were `Explore`-type spawns that default to haiku on their own, so only **~23/48 actually inherited opus**. In zero cases did the coordinator *deliberately* choose a cheap model — that is the real finding. (`policy-scorecard.py` prints both `no_explicit_model` and the precise `inherit_opus` to keep this distinction; see [[policy-effectiveness-tracking]].)

**Root cause:** delegation fires only when the task is already framed as an *open research question with a "return a digest" shape*. Two common shapes don't feel like research, so they get done inline on opus:

- **Pattern A — post-spawn monitoring.** After launching a developer spawn / job / PR / Nirvana WI, the main thread polls `.output` / `TaskOutput` / WI-status with `tail`/`grep` in a Bash loop. Pure waiting. (Audit: `792e9dca` ~35 calls, `04c47a03`, `39f540d0` 26-call `yt`/Nirvana cluster, `cd236088`.)
- **Pattern B — initial codebase/data exploration before editing.** Multi-file `cat`/`sed -n`/`grep`, YT/log probing, API archaeology to orient before a change. (Audit: `c50c8ae6` 80 Bash / 0 delegation, `899b0fe4` ~40, `53fcd679` 58, `319e203c` vh3 API — which the *later* `636f8e57` correctly delegated, a clean before/after pair.)

**Order & criterion:** before a stretch of ≥~8 mechanical Read/Bash/grep/log/poll calls on the main thread, delegate it to a sub-agent and **set the model explicitly**:

| Work | Model | Why |
|---|---|---|
| Retrieval, polling, log/stderr/traceback fetch, transcript scan | `haiku` | no judgment, just extract & return |
| Codebase/data search needing some judgment, multi-file mapping, "find X and tell me the shape" | `sonnet` | search + light reasoning |
| Genuine hard reasoning (root-cause, feasibility, design) | `opus` | reasoning is the product |

The `Agent` tool **inherits the opus parent model unless `model:` is set** — so omitting it silently runs mechanical work on opus. CLAUDE.md's old "sub-agents default to Sonnet" referred to `spawn-specialist.py`, not the `Agent` tool; do not rely on a cheap default — name the tier.

**Pin the search root when delegating Pattern B.** When the session cwd is under an arc FUSE mount (see [[home-dir-arc-fuse-mounts]]), an `Explore`/search sub-agent that defaults its search to `~`/`$HOME`/cwd-parent fans out across every `fuse.arc` mount and is pathologically slow. State the **absolute search root** in the spawn prompt (e.g. "search only under `~/claude-agent-instructions/`") and forbid traversal outside it — do not let the sub-agent infer scope. The `hook-arc-mount-search-guard.py` guard denies the worst case (a root spanning ≥2 arc mounts) for `Bash|Grep|Glob`, but it can't author a tight scope for you.

**Contexts:**
- 2026-06-17 self-improvement audit (this leaf's origin) — user asked "how often were sonnet/haiku used for subtasks, how often *could* they have been, where should a sub-agent have replaced inline work". Answer above. Fix: CLAUDE.md § Cost discipline + § Recognizing when to delegate updated to mandate explicit model tier and to list patterns A/B as delegate-always.
- 2026-06-23 — a Pattern-B `Explore` spawn for roadmap grounding was launched with the target files named but the search root left unpinned; with cwd under an arc mount it began a broad search across `/home/the0` (5 `fuse.arc` mounts). User caught it. Fix: the "pin the search root" rule above + the `hook-arc-mount-search-guard.py` guard + [[home-dir-arc-fuse-mounts]].

**Cost:** an inline exploration/monitoring stretch on opus costs ~5× the same work on sonnet and ~15–20× on haiku, and inflates the parent's retained context (cache read/write on every subsequent turn) — the dominant spend per [[token-economy-plan]]. See also [[log-reading-discipline]], [[large-tool-output-discipline]], [[spawning-specialists]].

**Tracked, not just exhorted:** the metrics this leaf names (spawn model mix, inherit→opus rate, missed-delegation clusters of ≥8 consecutive mechanical main-thread calls) are now measured per session by `scripts/policy-scorecard.py` — see [[policy-effectiveness-tracking]] for the ledger, weekly nudge, and the Flags-fire → self-improvement loop that turns a regression in these numbers into an actual policy adjustment.
