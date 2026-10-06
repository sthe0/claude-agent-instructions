---
name: delegatable-work-patterns
description: Two recurring work shapes the opus main thread must hand to a CHEAP-model sub-agent instead of doing inline — (A) post-spawn monitoring loops, (B) initial codebase/data exploration before editing — plus the model-tier heuristic for any spawn, and the return-contract rule for a delegated NEGATIVE finding (a sub-agent's "there is no X" without its probe list is UNKNOWN, never ABSENT). Delegation today fires only for "open research question → return digest"; these two shapes are missed because they don't feel like research.
type: feedback
created: 2026-06-17
last_verified: 2026-09-27
---

**Difficulty:** the main thread runs on the expensive Opus model, yet routinely does high-volume mechanical work **inline** (so the volume stays in opus context and bills opus rates) when it should hand that work to a cheap-model sub-agent that returns only the conclusion. A 48h audit (2026-06-17, 65 sessions) found **~2150 main-thread Read+Bash calls**, the `Agent` tool used in only **~21/65 sessions**, and of 48 sub-agent spawns: **27 opus / 21 haiku / 0 sonnet**, with **44/48 spawned without an explicit `model:`** — but "no explicit model" ≠ "ran opus": the 21 haiku were `Explore`-type spawns that default to haiku on their own, so only **~23/48 actually inherited opus**. In zero cases did the coordinator *deliberately* choose a cheap model — that is the real finding. (`policy-scorecard.py` prints both `no_explicit_model` and the precise `inherit_opus` to keep this distinction; see [[policy-effectiveness-tracking]].)

**Root cause:** delegation fires only when the task is already framed as an *open research question with a "return a digest" shape*. Two common shapes don't feel like research, so they get done inline on opus:

- **Pattern A — post-spawn monitoring.** After launching a developer spawn / job / PR / orchestration-platform work item, the main thread polls `.output` / `TaskOutput` / WI-status with `tail`/`grep` in a Bash loop. Pure waiting. (Audit: `792e9dca` ~35 calls, `04c47a03`, `39f540d0` 26-call data-warehouse/orchestration-platform cluster, `cd236088`.)
- **Pattern B — initial codebase/data exploration before editing.** Multi-file `cat`/`sed -n`/`grep`, YT/log probing, API archaeology to orient before a change. (Audit: `c50c8ae6` 80 Bash / 0 delegation, `899b0fe4` ~40, `53fcd679` 58, `319e203c` vh3 API — which the *later* `636f8e57` correctly delegated, a clean before/after pair.)

**Order & criterion:** before a stretch of ≥~8 mechanical Read/Bash/grep/log/poll calls on the main thread, delegate it to a sub-agent and **set the model explicitly**:

| Work | Model | Why |
|---|---|---|
| Retrieval, polling, log/stderr/traceback fetch, transcript scan | `haiku` | no judgment, just extract & return |
| Codebase/data search needing some judgment, multi-file mapping, "find X and tell me the shape" | `sonnet` | search + light reasoning |
| Genuine hard reasoning (root-cause, feasibility, design) | `opus` | reasoning is the product |

The `Agent` tool **inherits the opus parent model unless `model:` is set** — so omitting it silently runs mechanical work on opus. CLAUDE.md's old "sub-agents default to Sonnet" referred to `spawn-specialist.py`, not the `Agent` tool; do not rely on a cheap default — name the tier.

**Pin the search root when delegating Pattern B.** When the session cwd is under a network-backed VCS FUSE mount (see [[home-dir-arc-fuse-mounts]]), an `Explore`/search sub-agent that defaults its search to `~`/`$HOME`/cwd-parent fans out across every such mount and is pathologically slow. State the **absolute search root** in the spawn prompt (e.g. "search only under `~/claude-agent-instructions/`") and forbid traversal outside it — do not let the sub-agent infer scope. The `hook-multi-mount-search-guard.py` guard denies the worst case (a root spanning ≥2 arc mounts) for `Bash|Grep|Glob`, but it can't author a tight scope for you.

## Negative findings

**A delegated NEGATIVE finding carries its probe list, or it is `UNKNOWN`.** Delegation's whole point
is that the tool volume stays in the sub-agent and only the conclusion returns — which means a return
of the form *"there is no X"* arrives with the one thing needed to weigh it already discarded: what
the sub-agent actually looked at. A positive finding survives this (an observation is an observation);
a negative does not, because its strength is exactly the coverage of the probes behind it, and the
parent cannot see them. So:

- **When the brief asks a sub-agent to establish an absence** ("is X still used?", "does Y exist
  anywhere?", "is there any caller of Z?"), require the return to state **what was probed** — the
  concrete commands / paths / tables, not a summary — and to mark any probe it could not run, and why
  (missing permission, unreadable file, tool absent).
- **A negative arriving without that list is `UNKNOWN`, not `ABSENT`.** Do not amplify it into a
  conclusion, and never into an irreversible recommendation (shut it down, delete it, it's dead). Ask
  the sub-agent for the list, or run the decisive probe yourself.
- **Before accepting any absence, name the mechanism that would explain the observed behaviour and
  check *that*.** A name-filtered process grep cannot find a kernel NAT rule; enumerate the candidate
  mechanisms first, then confirm the probes cover them.
- **A positive counter-signal outranks a bounded negative** — always. Byte counters, timestamps, live
  traffic, a user saying "I use this daily" are observations; a probe that did not find something is a
  statement about the probe. If they conflict, the negative is wrong until its coverage is shown.

*Difficulty removed:* delegation structurally destroys the parent's ability to audit a negative
finding's coverage, so a bounded sub-agent negative gets amplified into a confident parent conclusion
with nothing in between — see [[2026-09-27-negative-finding-amplified-from-bounded-probe]] (a fork's
"no proxy process on that VM" became "forgotten rudiment, can be shut down"; the VM was carrying the
user's daily VPN traffic through a NAT table neither of us had read). The principle:
[[verdict-covers-the-evidence-domain-it-claims]]; the mechanized template for a three-valued answer:
`scripts/lib/hook_wiring.py`.

**Contexts:**
- 2026-06-17 self-improvement audit (this leaf's origin) — user asked "how often were sonnet/haiku used for subtasks, how often *could* they have been, where should a sub-agent have replaced inline work". Answer above. Fix: CLAUDE.md § Cost discipline + § Recognizing when to delegate updated to mandate explicit model tier and to list patterns A/B as delegate-always.
- 2026-06-23 — a Pattern-B `Explore` spawn for roadmap grounding was launched with the target files named but the search root left unpinned; with cwd under a VCS FUSE mount it began a broad search across `/home/the0` (5 such mounts). User caught it. Fix: the "pin the search root" rule above + the `hook-multi-mount-search-guard.py` guard + [[home-dir-arc-fuse-mounts]].
- 2026-09-27 — a reconnaissance fork returned a bare negative ("no proxy process on the peer VM, no policy routing") with no probe list, plus a second wrong negative ("no sudo there" — passwordless sudo was available). The parent amplified it into "forgotten rudiment, can be shut down" about a host carrying the user's daily VPN traffic. User caught it. Fix: the § Negative findings return-contract rule above + [[2026-09-27-negative-finding-amplified-from-bounded-probe]].

**Cost:** an inline exploration/monitoring stretch on opus costs ~5× the same work on sonnet and ~15–20× on haiku, and inflates the parent's retained context (cache read/write on every subsequent turn) — the dominant spend per [[token-economy-plan]]. See also [[log-reading-discipline]], [[large-tool-output-discipline]], [[spawning-specialists]].

**Tracked, not just exhorted:** the metrics this leaf names (spawn model mix, inherit→opus rate, missed-delegation clusters of ≥8 consecutive mechanical main-thread calls) are now measured per session by `scripts/policy-scorecard.py` — see [[policy-effectiveness-tracking]] for the ledger, weekly nudge, and the Flags-fire → self-improvement loop that turns a regression in these numbers into an actual policy adjustment.

## Root responsiveness

*Difficulty removed:* a root that blocks itself in a long foreground call, or goes quiet while work is in flight, forfeits the controllability it exists to provide — the user cannot reach it and a stuck spawn is indistinguishable from slow progress.

- **(i) Long calls go to the background.** A Bash call that needs an explicit timeout above `root-bash-call-max-min` (config.md) runs with `run_in_background`; the call's own need for a longer timeout is the signal. The harness kills a foreground call at its ceiling, so the bound must sit below it. Watch the result with the detached-poller + `ScheduleWakeup` pattern of [[long-job-monitoring]].
- **(ii) No long silence while spawns are in flight.** While spawned specialists or jobs are in flight, the root reports to the user at least every `root-silent-max-min` (config.md): one line naming what is running and what it waits on. Silence longer than that is indistinguishable from a hang.
- **(iii) Specialist work is a separate process.** Work that is specialist-shaped or a plan step runs through `claude -p` (`scripts/spawn-specialist.py`), not an in-process `Agent`: the spawn gets its own runaway budget ceiling, materialized grants and an engine-visible transcript, whereas an in-process `Agent` shares the parent's session and budget and escapes those controls. This does not touch the cheap-Agent row above: short retrieval and polling stay on Agent.

See [[spawning-specialists]].
