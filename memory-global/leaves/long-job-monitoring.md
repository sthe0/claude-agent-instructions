---
name: long-job-monitoring
description: Drive a long external job (hours/days) to terminal state and report transitions proactively WITHOUT burning opus on polling and WITHOUT making the user prompt you. Recipe — detached OS poller (zero model tokens) + self-scheduled ScheduleWakeup/CronCreate wakeups + cheap Agent only at the judgment milestone. Anti-pattern: "ping me when it's done".
type: feedback
created: 2026-06-22
last_verified: 2026-06-30
last_accessed: 2026-06-30
---

**Difficulty:** a long-running external job (Nirvana meta-graph, CI, training run — hours to days) must reach terminal state **and** have its key transitions reported proactively, while satisfying two constraints that pull against each other: do not burn the expensive opus main thread on waiting, and do not make the user prompt you to keep watching. Under cost pressure the wrong resolution is seductive — "I'll stop polling to save money; ping me when it reaches stage X" — which offloads the monitoring cadence onto the user and violates the coordinator objective (autonomy / drive-to-resolution) and `CLAUDE.md` § Long-running jobs.

**Why the obvious options each fail alone:**
- Inline opus polling loop — burns the dominant spend (cache read/write on retained context) and can't span a multi-hour wait.
- A single bounded sub-agent poller — comes to rest after its turn; nobody re-arms it, so the job runs on unwatched.
- Offloading to the user ("tell me when X") — the actual failure being corrected here. Never do this.

**Order & criterion — the autonomous low-cost recipe:**

1. **Detached OS poller = the durable watcher.** Launch a plain `nohup python3 watcher.py > /tmp/.../watch.log 2>&1 &` (or bash) that polls the job's status API every N minutes and **logs every transition**, running until ALL key blocks are terminal. This costs **zero model tokens**, survives across conversation turns and session boundaries, and drives to the end on its own. Design it to LOG transitions (not exit on the first one) so a detached run tracks to terminal; emit a clear `TRIGGER=ALL_TERMINAL` / `TRIGGER=FAILED block=…` marker line.
2. **Self-scheduled wakeups = the proactive reporter.** Use `ScheduleWakeup` (dynamic /loop pacing) or `CronCreate` to wake yourself, `tail` the poller log, and report key transitions + the DoD milestone to the user — without being asked. Pick cadence from how fast the job's state actually changes and the prompt-cache window (sub-5-min only for fast external state; otherwise 20–30 min idle ticks). The wakeup peek is one cheap main-thread turn, not a held loop.
3. **Cheap `Agent` only at the judgment moment.** When the job reaches the stage that carries the Definition-of-Done (e.g. the val stage proving routing + remarks), spawn a `sonnet`/`haiku` `Agent` to gather the verification evidence and return a compact verdict — see [[delegatable-work-patterns]] Pattern A. Do not do the verbose API probing inline on opus.
4. **The terminal-WI milestone IS the firing point for description currency.** At that same milestone, before declaring the stage done, run the paired check from [[feedback-vcs-and-review]] (project): reconcile the PR/ticket **description** with the now-true state **and** post the run comment. This binds a repeatedly-missed cognitive step (description left stale until the user pointed it out — DEEPAGENT-414/426/430) to a milestone that now has machinery (the watcher + wakeup), so it stops being a floating step you skip under load.

**Anti-patterns (forbidden):**
- "Ping me when it's done" / "tell me when it reaches X" — offloading the monitoring cadence to the user.
- Holding an expensive opus inline poll loop running for hours.
- Declaring the task done / closing the gate before the job is actually terminal (a config edit or a single green block is not the observable — terminal state is).

**Contexts:**
- 2026-06-22 DEEPAGENT-430 live-E2E (Nirvana meta-WI `d71403aa`, ~long run). After launching it I told the user I'd stop polling to save cost and asked them to ping me at the val stage. The user had to ask "так ты следишь за своим запуском?" and then objected to having to push me to monitor. Fix: the detached watcher + self-scheduled wakeup recipe above made the default; `CLAUDE.md` § Long-running jobs strengthened to forbid offloading cadence.

**Cost:** detached OS poller = 0 model tokens; one `ScheduleWakeup` peek = a single cheap turn; vs an opus inline loop = the dominant per-session spend ([[token-economy-plan]]). The recipe is strictly cheaper than inline polling AND more autonomous than offloading — it dominates both failed options on the objective function. See also [[delegatable-work-patterns]], [[coordinator-objective]].

## Generalization: gate an outward landing by ownership, in code

The same machinery answers a sibling difficulty — **landing a change** (publishing a merge request, shipping a release) carries a verbal rule ("inside your own area, self-landing without review is fine; outside it, never bypass review/CI") that is forgettable and unenforced when it lives only in prose. The fix is the same shape as the watcher above: encode the parts that need no judgment as **deterministic code**, leaving only the genuinely cognitive part to the model.

**Order & criterion — three code-enforced invariants + one model judgment:**

1. **Classify blast radius by an ownership boundary, as a pure function.** A tool-layer pre-check classifies the change's file-set against the actor's *owned namespace* (a path-prefix test: every changed path under the owned prefix → `owned`, else → `outside`; an empty or anomalous file-set is **never** `owned` — fail closed). This is pure code with the boundary constants isolated in one place; no model judgment enters the classification.
2. **Refuse the bypass outside the owned namespace, at the tool layer.** When a landing command carries a review-or-CI-**bypass** token and the change classifies `outside`, the pre-check **denies** it (fail-closed on `unknown` too) and points at the plain, review-respecting landing path. Inside the owned namespace, self-landing proceeds untouched. The refusal is code, not a remembered "should"; it cannot be skipped under load.
3. **Drive an outside landing to terminal with the detached poller above, and surface review feedback as markers.** For an `outside` merge request, launch the same zero-token detached poller; design it to emit `TRIGGER=MERGED` at terminal **and** `TRIGGER=NEW_COMMENTS` / `TRIGGER=CHECK_FAILED` when reviewers respond or checks fail. Closure is gated on the merged marker — "submitted" is not "landed".
4. **Only comment-reply and code-fix stay in the model.** When a `NEW_COMMENTS` / `CHECK_FAILED` marker fires, the model reads the feedback, answers it, and fixes the code — the one irreducibly cognitive step. Classification, the bypass refusal, poll cadence, marker emission, and closure-gating are all code.

**Why split it this way:** a rule a model must *remember* to apply is skipped exactly when load is highest; a check the tool layer *runs* is not. Pushing classification + refusal + cadence into code removes them from the model's attention budget and makes the policy auditable and testable in isolation (a hermetic test of the classifier + gate proves the boundary without a live landing). The residual model judgment (what a review comment means, how to fix it) is where a model genuinely adds value — keep that, automate the rest.

**Anti-pattern:** re-deriving "may I bypass review here?" as a per-landing judgment call. That is the forgettable-rule failure mode this generalization removes — decide it once, in code, by ownership.
