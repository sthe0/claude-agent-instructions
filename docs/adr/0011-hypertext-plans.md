# ADR-0011: Hypertext plans — hash-pinned references, a Merkle root digest and a deterministic converter

- Status: Proposed 2026-10-14
- Plan: `plan-hypertext.toml` (the implementing plan, kept in the agent's plans directory)

## Context

The largest live plans are 700-830 KB of TOML and a single stage reaches 133 KB, while the spawn ceiling for a prompt is 144 000 tokens (about 216 000 characters at the spawner's 1.5 chars/token, `PROMPT_CHARS_PER_TOKEN` in `scripts/spawn-specialist.py`). Review is already pairwise, but the plan is one file: every edit rewrites it, a reviewer is pointed at the whole file or at engine-rendered copies, nothing limits a stage's size, and in some spawn paths the whole plan is still inlined into the prompt (Core backlog issue #73, a GitHub issue labelled `backlog`: `brief_plan_path` falls back to inlining the plan when the spawn kind is not in `PLANS_READ_KINDS`, no stage index is given, or the plan lies outside the plans directory). Core backlog issue #392 (same venue) asks for a stage size limit before pair review. A plan that has outgrown the context also cannot be split by hand without changing the plan file's digest, to which every approval, review and presentation record is bound, so a live session needs a conversion that costs no new review round, and a past task's plan needs one to be analysable.

## Reference format

A reference is an inline TOML table with **exactly** the keys `ref` (a path relative to the file that holds the reference) and `sha256` (64 hex characters, the digest of the referenced file's bytes). It stands where a string value or a whole `[[stage]]` table would stand. Positions are decided by structure, not by schema field names: the value of any top-level key of a `[[stage]]`, `[meta]` or `[meta.order]` table, or a whole `[[stage]]` entry — except keys whose schema value is not a string (`index`, `depends_on`, `grants`, …), listed per node kind in `plan_refs.NON_STRING_FIELDS`, where a reference is refused as a wrong position. A table with a non-empty proper subset of the two keys is a **malformed** reference and is refused; a table with any other key is ordinary plan data (every `[[stage.effects]]` entry carries `path`, `sha256` and `resolver`) and is never a reference. A string field resolves to the file's text verbatim (UTF-8, no trimming); a stage resolves to the file parsed as a TOML table.

## Resolution and the DAG rule

Resolution is a pre-pass over the parsed document (TOML has no include mechanism): `plan_refs.resolve_refs` replaces every reference by the referenced content, recursively, before `parse_plan` runs, so the parser and every validator see an ordinary document. Referenced files may themselves hold references, so the structure is a DAG of documents (premise Q1); a file may be referenced from several places (Q3).

Every file must lie below the **confinement root** after symlink resolution: the plans directory when the index lies under it, otherwise the index file's own directory; a caller may pass the root explicitly (Q2). Refusals raise `PlanError` with a `ref_kind` and the reference path: `missing`, `mismatch` (expected and actual hash), `cycle`, `depth`, `escape` (absolute, or outside the root), `malformed`, `position`, `index` (a referenced stage file whose own `index` disagrees with its position). The cycle check runs on the resolution stack before the file is read, hash-verified or counted for depth, so a cycle that closes within the depth bound is reported as a cycle; a chain of more than 8 nested references is refused as `depth`.

The depth bound is **8**. It is a structural constant stated here, not a size threshold, so it is not a `config.md` key: the deepest intended nesting is index → stage → field file, or index → child plan → stage → field file (an [ADR-0010](0010-child-contour.md) child plan), and 8 leaves headroom while stopping a runaway chain.

## Plan digest

The plan digest stays `sha256` of the index file's bytes. Because every reference embeds the child's `sha256` and the loader refuses a mismatch, any change anywhere below changes a hash in the index: the root digest is a **Merkle root** covering the whole hierarchy, and no second digest is needed. `accepted_plan_digest`, the `plan-approved-` / `plan-version-` snapshot names, `PlanReview.plan_sha256`, the reviewer's `shasum -a 256` attestation and the present-plan bindings keep their meaning. For a single-file plan the digest is byte-identical to today's.

The part digests (`stage_element_keys`, `plan_stage_digests`, `plan_meta_digest`, `order_digest`, `plan_content_digest` — whose payload stays frozen, pinned by `test_enumeration_keying` — and the pair and unit currency hashes) are computed over resolved content. They are unchanged for inline plans and invariant under moving a field into a file, so externalizing text never forces a re-review.

## Stage closure digest

`stage_closure_digest(doc, n)` hashes stage n's whole-stage element key together with the sorted `(index, closure digest)` pairs of every stage in n's **reliance set** (`depends_on` plus every stage named by a `[[stage.supplies]]` `on`). It first runs the existing reliance-cycle check and refuses a cycle with a `PlanError` naming it. It is a new derived digest, exposed by the read-only verb `agentctl plan-digest --plan <path> [--stage n]` (root digest, each referenced file's verified hash, each closure digest), carried in the stage brief and in the ADR-0010 norm handoff record. It does not replace the persisted review-currency hashes, which keep their definition because replacing them would invalidate every recorded review (premise Q4). The authoring verbs `plan-externalize --plan <path> (--stage n --field <name> | --meta-field <name> | --order-field <name>)` (move one string field into its file and write the reference, resealing ancestors) and `plan-seal --plan <path>` (recompute reference hashes bottom-up after an edit) maintain the pins; both are built on the shared `plan_refs.seal` and `plan_refs.write_field_file`.

## Object store and snapshots

The object store is `<plans_dir>/objects/<sha256>`, files named by their own digest, filled with every referenced file at submit-plan, replan and approve, so an older plan version stays resolvable after its files change. A snapshot or older version is loaded with `base_dir` and root taken from the session's registered plan path (references are relative to the original index, not to the snapshot's directory) and a store-backed reader (the `read` hook of `plan_refs`, whose default reads the file system), so it resolves by `sha256` and never silently reads the current file. `plan_snapshot_path` and `plan_snapshot_hash` keep their meaning.

## Plan views and read grants

`materialize_topo_units` generalizes into one materializer used by every model-facing site: dispatch stage brief, dispatch without a stage index, topological pair views, whole-plan thinker review. A view directory holds `objects/<sha256>` copies of every file a node references plus engine-rendered node files; the prompt names only paths inside it, and the spawn grants it with `--add-dir`, so reading referenced files is granted mechanically. A rendered node inlines a field up to the inline limit and above it prints `see objects/<sha256>`. There is one **site builder** per site kind (`render.build_site_prompt`) that returns the exact plan-bearing prompt text and the granted directories without spawning; the spawner, the topological driver, the size lint and the acceptance check all call it, so what is measured is what is sent. A topological unit review and a ledger-enumerate node call build no view and inline every field.

## Size limits

Two limits, both values living only in `config.md` (this ADR names the keys, not the numbers), filled by a committed calibration. `plan-stage-max-chars` is S = floor((C − O) / 3) rounded down to a multiple of 1000, where C is the dispatch prompt ceiling in characters and O the rendered size of a first-review pair prompt whose plan-level node and two stages carry no fields; the divisor is 3 because a pair prompt shows the plan-level node (as `## Order context`) and up to two stage nodes. `plan-field-inline-max-chars` is F = floor(S / k) rounded down to a multiple of 500, k being the number of string fields `plan-externalize` accepts on the node with the most of them. Both apply to **node size** (the block rendered with every field inline, structured fields included).

The #392 remedy is two steps: first move field text into files (above F), then reformulate the stage. `submit-plan` warns on a field above F and blocks a node above S (no waiver: such a node cannot be reviewed in one pair prompt; premise Q5). The plan-level node can also exceed S; the remedy there is to externalize or shorten its prose.  `plan-review-topological.py` refuses before spawning any reviewer a unit with an over-limit node or whose built prompt (including a re-review's history section, which the formula does not bound) exceeds C. `plan-fit-report.py` (under `scripts/`) measures any plan against the limits and every prompt site.

## Conversion of single-file plans

The `plan_convert.py` module of the `agentctl` package converts an old-format plan (single-file TOML, no reference) into an index plus referenced files that resolve to the same document, so that live sessions whose plan has outgrown the context continue without a new review and past tasks can be analysed. Conversion is optional: the loader keeps reading single-file plans.

**Determinism.** The tree is a pure function of the old plan's bytes, its file stem and the two limits (passed in from `config.md`, recorded in the report): every stage into `stage_file_relpath`, every top-level string field of a stage or `[meta]`/`[meta.order]` longer than F into its own `.md` file named by `field_file_relpath`, shorter fields inline, hashes sealed bottom-up, TOML written by one small deterministic emitter (the standard library reads TOML but cannot write it) whose round trip is asserted on every call.

**Equivalence is proven before anything is written.** Raw equivalence (the resolved tree as a parsed document equals the old document) always; parsed equivalence (`plan_content_digest`, meta, order, per-stage and closure digests, dependencies, supplies, effective grants, final checks) whenever the old plan loads under the current loader. Any difference refuses the conversion. A past plan that the current loader refuses is converted on raw equivalence alone and the report says parsed equivalence is not applicable (Q9).

**Mechanical splitting only.** A node above S is converted unchanged and reported, never reworded: reformulating a stage changes the norm and is the planner's act. For a plan the loader refuses, an oversize node is measured on the raw document.

**Historical mode** `agentctl plan-convert --plan <old> --out <dir>` is read-only: it writes only into `<dir>` (and a temporary directory it removes) and never touches the source, session state, snapshots or the object store. The result loads with `load_plan(root=<dir>)`, so `plan-digest`, `plan-render`, `plan-grants` and `plan-fit-report` analyse it.

**Live mode** `agentctl plan-convert-live --session <id> --by agent|user` converts the session's registered plan in place — the index is written at the registered plan path and the old bytes stay in the object store (Q10), so every `plan_path`-bound record stays bound. It appends a record to the new optional `SessionState` field `plan_conversions` (old root digest → new root digest, resolved content equal) and sets `accepted_plan_digest` to the new root. The verb is in `AGENTCTL_USER_AUTHORITY_VERBS`, so no spawned specialist can run it; the root may run it with `--by agent`, disclosed on the Directive, at verify-final and at resolve (Q8), because the engine refuses any conversion whose content is not proven equal and `--revert` restores the recorded old bytes.

**Bindings that are NOT rewritten.** `PlanReview.plan_sha256` and its reviewed keys, `PlanPairReview.plan_sha256` (audit only), `PlanPresentation.plan_sha256` and its delivery stamp, `plan_snapshot_path` / `plan_snapshot_hash` and `plan_review_counted_digest` keep their values. One helper, `gates.plan_digest_equivalent(state, a, b)` — `a` equals `b`, or the equivalence chain links them — replaces every comparison of a recorded root digest with a current or accepted one; a committed inventory of those comparisons, held equal to a pinned syntactic enumerator by a test, makes "every" checkable. The per-part digests are invariant by construction. Topological view roots are an on-disk cache keyed by root digest with no persisted binding, re-materialized under the new root; recorded pair reviews stay current by content currency. An edit after conversion leaves the chain and gates exactly as an edit does today.

## Compatibility

Additive, no state migration. Single-file plans: no reference, identical bytes, identical digests and part digests. No persisted `SessionState` field changes meaning; the additions are new verbs, the derived closure digest (never persisted as a gate input), the object store, and the optional `plan_conversions` (default empty, so older states load and gate unchanged; with an empty chain `plan_digest_equivalent` reduces to equality). Hypertext plans approved after the change resolve their snapshots through the object store. The frozen `plan_content_digest` payload is untouched, and a test loads every persisted snapshot shape the suite has fixtures for. Not covered: markdown-era plans (not TOML) are not convertible; a node above S is reported, not reformulated.

## Prompt-site inventory

Closing #73: the whole-plan inline fallback in `brief_plan_path` is removed. A stage-scoped spawn of any kind gets a stage brief plus a view grant; a spawn without a stage index gets a view holding a rendered index plus its objects, never a plans-directory path and never the inlined plan; ledger-enumerate over an artifact that loads as a plan enumerates node by node. Every site that hands plan content to a model (spawns, runner-based judge calls, advisor judge calls, claim enumeration) is listed in `llm-prompt-sites.md` in the operations docs directory (created by the implementing stage) with a mechanism from a closed vocabulary — view, brief, inline field, caller artifact, no plan content — and a test checks that the enumerated call sites equal the inventory and that no site's prompt carries text from a node it does not show. Frozen fixtures (the benchmark-profile planner copies) are listed with a disposition and not edited (Q7).

## Open premises

Each recommendation is what the stages implement; the user's disposition is recorded here when known.

- **Q1** — DAG now (depth bound 8) versus two levels only. Recommended: DAG now; the resolver is the same code and ADR-0010 child plans become natural sub-documents.
- **Q2** — references confined to the confinement root versus any readable path. Recommended: confined; the spawner already grants Read on the plans directory, which makes the grant mechanical.
- **Q3** — may one file be referenced by several stages or plans. Recommended: yes; a reference pins content, not ownership, and an edit to a shared file shows as a hash mismatch in every referrer until resealed.
- **Q4** — the closure digest is a new exposed digest and the persisted currency hashes keep their definition. Recommended: yes.
- **Q5** — the stage size limit blocks submit-plan (recommended) versus warns; the field limit only warns.
- **Q6** — one final landing (recommended) versus a landing after each stage. Reason: one gate, the suite judged on what lands.
- **Q7** (stage-local, assumed) — the planner SKILL copies under `benchmark-profile*/` are frozen fixtures and not edited; the inventory records the disposition.
- **Q8** — the root may run `plan-convert-live` on its own judgment (`--by agent`, disclosed) versus user confirmation per conversion. Recommended: the root may, since equality is proven and `--revert` undoes it.
- **Q9** (stage-local, assumed) — a past plan the current loader refuses is converted on raw equivalence alone, not refused.
- **Q10** (stage-local, assumed) — live conversion writes the index at the registered plan path, keeping the old bytes in the object store, rather than aliasing paths.
- **Q11** (stage-local, assumed) — the acceptance copy of the real plan is made by one `cp` the user is asked to allow at the acceptance step of the implementing plan; historical mode never writes its source, and the copy keeps a converter defect from touching a past plan.

## Decision notes

- Fidelity of this ADR to its implementing plan is checked by an independent review (assumed: every code stage already passes one).
- The depth bound 8 is an ADR constant, not a config key (assumed: it is a structural invariant of the reference model, not a calibrated size threshold; see "Resolution and the DAG rule").
- The size formula divides by 3 because the pair prompt also inlines the plan-level node (decided by the user; see "Size limits").

The status stays Proposed until the implementation lands.
