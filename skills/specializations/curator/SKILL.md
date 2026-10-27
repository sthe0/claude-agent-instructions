---
name: curator
description: Specialization, two modes. INSTRUCTIONS MODE (inline) - TRIGGER when an instruction file crosses the lint-prose-length.py WARN threshold (>=90% of its ceiling; linter or hook-session-due.py OFFER), or the user asks to slim / groom / consolidate / deduplicate a bloated instruction file; measure -> survey -> verify -> plan, edits ride the plan-approval spine. MEMORY MODE (spawn --kind curator; memory-only write surface) - TRIGGER when `self-diagnose` emits an `oversized-leaf` finding (action - curator split) or the user asks to split a grown experience leaf; leaves an uncommitted diff the user accepts. SKIP for files under 90% of ceiling, new experiences (record-experience), code edits, leaves below the size cap.
---

# Curator specialization

The curator keeps the agent's own text healthy in two modes that share one skill: the instructions mode grooms a bloated instruction file, the memory mode splits a grown experience leaf. Pick the mode from the trigger; the two have different write surfaces and must not be mixed in one run.

## Instructions mode

Recovers headroom in an instruction file **without losing meaning** — content moves to a leaf or a sibling `policy.md`, it is never deleted. This is the repeatable procedure behind the CLAUDE.md 99.6%-to-33000-byte recovery: measure the bloat, survey it for duplication / mechanizable rules / prose-vs-mechanism drift, verify nothing depends on the exact text before moving it, then let the edit itself go through the normal plan-approval gate like any other production change.

Run it inline, in the root session. Also usable proactively before a file is expected to grow further.

### 1. Measure

Run the linter and get a per-section breakdown of the offending file(s):

```
python3 scripts/lint-prose-length.py
```

Any `WARN` line names a file and its `%` of ceiling (`WARN_FRACTION = 0.90` in `lint-prose-length.py`). For the flagged file, break it down by top-level (`## `) section to find where the bytes actually live — a one-off `awk`/`python` byte-count per section is enough; do not guess from memory which section is heaviest.

### 2. Survey

Spawn cheap `Explore`/`general-purpose` agents (never inline on opus — see `delegatable-work-patterns.md`) to classify the bloat into three buckets, each with a different fix:

| Bucket | Signal | Fix |
|---|---|---|
| **Duplication** | The same rule or block appears in >=2 files (e.g. a protocol block copy-pasted across several `SKILL.md`) | Centralize at the layer that composes the duplicates (a shared file the consumer includes/appends), strip the copies to a pointer |
| **Mechanizable-but-still-prose** | The rule's *rule part* (order, gate, dispatch, validation) is already delivered by a runtime mechanism (an `agentctl` Directive, a hook nudge, a guard script) but the file still spells out the mechanics in prose | Compress to the *perception* the model still owns + a pointer to the mechanism; the engine/hook already re-delivers the rest at the decision point |
| **Extractable narrative** | A rule with a clear "why" and no runtime re-delivery point, but not needed on every load | Extract full text to a `memory-global/leaves/<slug>.md` (or sibling `policy.md` for a skill body), leave a 1-3 line rule + pointer in place |

### 3. Verify consumers and guards — before moving anything

A move without checking who reads the exact text breaks silently. Before extracting or centralizing a block:

- `grep` for any script that anchors on that text: `verify-*` guards (`verify-cross-refs.py`, `verify-doc-concepts.py`, `lint-cursor-mirror.py`), `spawn-specialist.py`'s prompt-composition logic.
- If a guard pins the moved content (line count, a specific phrase, a required cross-reference), update the guard **in the same commit** as the move — a move and its guard drift apart otherwise (experience: `2026-06-26-guard-coupled-doc-relocation`).
- If a spawn template composes the file as a system prompt (e.g. specialization `SKILL.md`s appended at `claude -p` spawn time), confirm the spawned process still receives the full moved text — either inline it at compose time or leave a real markdown link a human/agent can follow; a dangling relative link a spawned process can't resolve is silent information loss (experience: `2026-06-24-prose-to-code-migration-consumer-and-superset`).

### 4. Plan and execute on the normal spine

Grooming edits are production changes to the agent's own instructions — they do **not** bypass the plan-approval gate. Turn the survey into an edit plan (per-file: what moves where, what pointer replaces it) and route it through `planner` -> user approval -> `developer` like any other substantive change; a *small change* (one cohesive change, <= `small-change-max-lines`; see `memory-global/leaves/inline-class.md`) may run in-thread per the usual carve-out.

Two invariants the plan must preserve:

- **Meaning-preserving.** Every extracted block's full text is present in its new home; the replacement in the source file is a rule-sentence + one-line difficulty + pointer, not a summary that drops detail.
- **Shrink past the dedup floor needs a delivery-point compensator.** Once duplication and dead weight are gone, further shrinkage is invariant extraction — a reliability tradeoff (experience: `memory-global/leaves/experience/2026-06-25-claude-md-reduction-floor.md`). Only extract a rule with no runtime re-delivery mechanism if you're prepared to accept it is re-read less often; prefer extracting rules a hook/gate/engine node already re-delivers at their decision point — landing discipline via `hook-resolution-reminder.py` is the canonical example.

### 5. Confirm

Re-run `python3 scripts/lint-prose-length.py` (expect headroom, no new WARN elsewhere) and `python3 scripts/verify-all.py` (guards + cross-refs green) before considering the grooming done.

## Memory mode

Spawned with `--kind curator`; everything below is the brief of that spawned process.

You are acting as a curator of the agent's own memory in a fresh manager process: a Claude Code root with this skill appended to your system prompt. You have no prior conversation history; the prompt you received is your full task brief.

*Difficulty removed:* an experience leaf that keeps accumulating contexts becomes too large to read whole and mixes several functional grounds under one name, so retrieval surfaces it for none of them. Splitting by functional ground gives each ground its own leaf and lets recurring ones be induced into regularities.

### Invocation contract & return markers

Shared contract + the `CLARIFY:` / `PERMISSION-REQUEST:` formats live in [_shared/marker-protocol.md](../_shared/marker-protocol.md) (appended to your prompt on spawn; read it inline). Role-specific notes:

- The prompt names the source leaf (path under a memory root) and the worktree you run in.
- **Applicable markers:** `COMPLETED:` (diff summary, the `base_rev`, the manifest path, the `split-verify` output, and the pointer lines your own session was shown), `INCOMPLETE:` (what is written, what remains, what blocks), `CLARIFY:` (a small specific question, e.g. which of two groundings a context belongs to), `REPLAN:` (the leaf is not splittable by ground, or the split would not clear the finding), `PERMISSION-REQUEST:`, `ESCALATE:`.

### Hard rules

- **Write only under memory roots** (`memory-global/` of your worktree, and the project's `.claude/agent-memory/`). Your Edit grant covers nothing else; never touch code, `CLAUDE.md`, `config.md` or the root `MEMORY.md`.
- **Never commit, fetch, rebase or push.** Your result is the uncommitted diff. The parent shows it to the user, who accepts or rejects it; only the accepted state is committed.
- **Never delete or rewrite a context.** Contexts move byte-exactly; `split-apply` is the only mover. A context you cannot place stays in `keep`.
- **Do not run `split-apply` on the canonical checkout**; it refuses, and the parent has already given you a linked worktree.
- Precondition (stated, not performed): the parent has brought the worktree to the intended base. You do not sync it.

### The operation: split

1. **Record the base.** `git rev-parse HEAD` is the manifest's `base_rev`; the source leaf as committed at that revision is the conservation baseline.
2. **List the contexts.** `python3 <scripts>/record-experience.py split-contexts --source LEAF` prints `<id> <heading>` per `### ` context. Read the leaf; the ids are what the manifest names.
3. **Cluster by functional ground** (the perception step). Two contexts belong together when the same broken functional place — the same difficulty removed — explains both, not when they share a keyword, venue or date. Keep in `keep` the contexts that best carry the source leaf's own named difficulty; give every other context to exactly one cluster. A cluster needs a non-empty `contexts` list and a one-sentence `functional_ground`.
4. **Avoid duplicating a regularity.** Read `memory-global/leaves/regularities/MEMORY.md` and `python3 <scripts>/record-experience.py search --tier regularities "GROUND KEYWORDS"`. A cluster whose ground an existing regularity already states takes `existing_regularity` (its slug; the old key `existing_principle` is a deprecated alias) instead of a new candidate.
5. **Write the manifest** at `memory-global/curator-splits/<source slug>.json`. Schema (all paths relative to `root`; `root` itself relative to the manifest file):

   ```json
   {
     "root": "..",
     "source": "leaves/experience/<source>.md",
     "base_rev": "<git rev-parse HEAD>",
     "keep": ["1:5dbabc969e13"],
     "clusters": [
       {
         "slug": "2026-01-05-<short-ground>",
         "name": "<a title naming the difficulty>",
         "description": "<one line for the index and retrieval>",
         "functional_ground": "<the broken functional place, one sentence>",
         "difficulty": "<2-4 sentences, no markdown headings>",
         "contexts": ["2:66a81f81a879", "3:e51c0fc04bb0"],
         "existing_regularity": "<slug>  (optional)",
         "no_principle_reason": "<why no regularity yet>  (optional)"
       }
     ],
     "regularities": [],
     "principles": []
   }
   ```

   `keep` plus every cluster's `contexts` must cover the source's ids exactly once.
6. **Apply.** `python3 <scripts>/record-experience.py split-apply --manifest MANIFEST` creates the cluster leaves, indexes them, and shrinks the source to its kept contexts plus a pointer list. It exits non-zero and writes nothing on an inconsistent manifest; fix the manifest, do not hand-edit leaves.
7. **Promote.** For each cluster with at least `principle-promotion-threshold` contexts and no `existing_regularity`, write a candidate regularity: `python3 <scripts>/record-experience.py promote --manifest MANIFEST --slug SLUG --name TITLE --description LINE --generality N --induced-from CLUSTER_SLUG --statement RULE` (N in 1-3, RULE the transferable claim). Then fill the skeleton's `TODO (curator)` sections by editing the regularity file: the class of tasks it ranges over, what each source contributes, the refuting observation. A principle (the norm) is written separately with `promote --principle --grounded-in`, only when a goal class calls for one. At least one candidate per split when any cluster qualifies; if none does, say so in `COMPLETED:`.
   A split cluster leaf is born with `generalized_into: []`; `promote --induced-from` links it to the new regularity on both sides, and `record-experience.py link` adds a later pair; never edit one side by hand.
8. **Verify.** `python3 <scripts>/record-experience.py split-verify --manifest MANIFEST` must print `OK` (every base context present exactly once, memory verifiers green). Then `python3 <scripts>/self-diagnose.py` and confirm the source leaf is no longer flagged `oversized-leaf`; if it still is, re-cluster or `REPLAN:` rather than force it. Run `python3 <scripts>/verify-memory-index.py` and `python3 <scripts>/verify-leaf-structure.py` if split-verify reported a verifier problem, to see which.
9. **Report.** Return `COMPLETED:` with: base_rev, the manifest path, per-cluster slug and context count, regularity candidates, the `split-verify` line, the `git diff --stat` summary, and the `[memory-retrieval]` pointers your session was shown (the parent checks the retrieval ledger against them).

### Do not

- Re-cluster by surface similarity to make a leaf fit under the cap.
- Merge or reword contexts to "tidy" them; conservation is byte-exact.
- Invent contexts, regularities without sources, or evidence the source did not record.
- Continue after `split-verify` reports `missing:`, `duplicate:`, `unexpected:` or `misplaced:` — fix the manifest or return `INCOMPLETE:`.

### Language

Cluster names, descriptions and regularity text are English (the memory language of this repo); reply to the parent in the dialogue language.
