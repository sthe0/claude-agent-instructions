---
name: instruction-dev-queues
description: "The 3-tier instruction-development tracking model (Core / Org-Yandex / Project): each tier has a proactive internal backlog + a reactive report inbox, with a collapse rule when filers==editors and a stream-separation axis (separate queue vs separate label)."
type: reference
schema: leaf/v1
created: 2026-06-30
last_verified: 2026-06-30
---

# Instruction-development tracking — backlog + report inbox per tier

## Difficulty

To achieve **a planned destination for every instruction-development idea** — both the refinements *we* decide to make and the difficulties *users* report — instead of letting them accumulate ad-hoc on someone's local notes where they are lost between sessions, each instruction tier needs an explicit, durable place to track that work. Two distinct flows are easy to conflate: a **proactive internal backlog** (things the tier's *editors* plan to build) and a **reactive report inbox** (difficulties filed by people *without* edit authority, via self-improvement / the difficulty-channel). Without naming both and saying where each lands per tier, reports get planned-over and backlog ideas get filed as if they were external difficulties — and the digest that clusters difficulties (`core-difficulty-digest.py`, which pulls `labels=difficulty`) silently swallows planning items.

## Guidance

The instruction system has three editable tiers (the `Core < Team < Personal` substrate of ADR-0001; "Org" and "Project" are the Yandex/`robot/deepagent` instances of the editable middle/leaf). **Each tier has two tracking flows:**

- **Internal backlog** — what the tier's *editors* (those with edit authority over that tier's instructions) proactively plan to build. Author-created planning items.
- **Report inbox** — difficulties filed by people *without* edit authority over that tier, surfaced during self-improvement and routed via the difficulty-channel (`file-difficulty.py`). Reactive.

**Collapse rule.** When the set of *filers* equals the set of *editors* for a tier, the two flows have the same population and merge into **one queue** — there is no "external report" because everyone who reports can also plan. This is why the Project tier uses a single queue: project participants edit project instructions directly.

### The three tiers

| Tier | Backlog | Report inbox | Same-tracker? |
|---|---|---|---|
| **Core** (org-neutral) | GitHub Issues | GitHub Issues | yes |
| **Org** (Yandex) | **OOSEVEN** | **OOSEVENREPORT** | no — two queues |
| **Project** (`robot/deepagent`) | **DEEPAGENT** | DEEPAGENT | collapsed — one queue |

- **Core** — org-neutral instructions in `claude-agent-instructions`. Non-author machines file reports via the `github` difficulty-channel adapter (GitHub Issues, repo `sthe0/claude-agent-instructions`); authors also plan there.
- **Org** — Yandex-specific-but-cross-project refinements. Split into two queues because the populations differ: internal Yandex *users* file **OOSEVENREPORT** reports (they hit difficulties but don't own org instructions), org *authors* plan in **OOSEVEN**. The `startrek` difficulty-channel adapter targets the report queue.
- **Project** — project-specific refinements (e.g. `robot/deepagent`). A project participant **designates one queue** via a machine-readable field (`agent-project.json` → `{"instruction_queue":"DEEPAGENT"}`, discoverable from project cwd); because participants have edit rights, backlog and reports collapse onto it. The queue is structural config, not prose — `file-difficulty.py` resolves it from the field; classifying *which tier a target belongs to* is the model's cognition, the queue *lookup* is the structure.

### Stream-separation axis (keeping the two flows formally distinct)

How reports stay distinguishable from backlog depends on whether the tier has one tracker or two:

- **Two trackers** (Org) — the **queues themselves** separate the flows: OOSEVEN = backlog, OOSEVENREPORT = reports. Nothing more needed.
- **One tracker** (Core = GitHub Issues) — separate by **label**. Reports carry `difficulty` (the github adapter's `DIFFICULTY_LABEL`, set on every filed report; `core-difficulty-digest.py` pulls **only** `?labels=difficulty&state=open`, so it sees reports and never backlog). Backlog items carry a distinct label (`backlog`, the adapter's `BACKLOG_LABEL`) and are therefore excluded from the difficulty digest by construction.
- **Collapsed** (Project) — one queue, one population; sub-distinguish by label/component within DEEPAGENT only if a project later wants the split.

The symmetry: "a separate destination for each flow" is realized as a **separate queue** where the tier has a tracker per flow, and as a **separate label** where one tracker holds both.

## See also

- `~/.claude/config.md` § `difficulty-channel` — the per-machine channel (`github`/`startrek`) and its OOSEVENREPORT/GitHub-Issues targets.
- `~/claude-agent-instructions/docs/adr/0001-consensus-architecture.md` § Difficulty-accumulation — how a flagged report cluster graduates into a backlog item.
- `~/claude-agent-instructions/skills/self-improvement/` — where a report is filed (non-author machines) and where tier→queue routing is applied.
- [[handling-escalations]], [[spawning-specialists]] — the coordination surfaces that surface difficulties in the first place.
