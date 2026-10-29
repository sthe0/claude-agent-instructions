# Raw plan readers

A hypertext plan ([ADR-0012](../adr/0012-hypertext-plans.md)) is an index file whose fields and
stages may be hash-pinned references (`{ ref, sha256 }`) to other files. Code that parses the
index with `tomllib` and reads the result sees the reference table where the text should be.
This inventory lists every place in `scripts/` that reads a plan file's bytes or TOML directly
instead of through `plan.load_plan` / `plan.load_plan_with_digest`, and says for each one why it
is correct for a plan with references.

The inventory exists so a new raw reader cannot appear unnoticed: a test
(`scripts/tests/test_plan_refs_consumers.py`) enumerates the readers and requires the rows below
to be exactly that set.

## Dispositions

- `resolves` — the reader resolves references (it is, or calls, the loader, the resolver or the
  object store), so it sees the text the plan stands for.
- `index-only` — the reader hashes or copies the index file's own bytes on purpose. The plan
  digest is the sha256 of the index bytes (the Merkle root: the index carries every part's hash),
  so the index bytes alone identify the whole plan. Such a reader must not resolve references.
- `not a plan` — the file it reads is not a plan (a state file, a ledger, a rendering) although a
  name in the read mentions a plan.

## Raw plan readers

| Reader | Disposition | Why |
|---|---|---|
| `scripts/agentctl/cli.py`:`_mandate_basis_problems` | index-only | Compares the index bytes `cmd_approve` read with the item norm re-rendered from its inputs; a rendered item norm holds no references, so a plan that does is refused rather than resolved. |
| `scripts/agentctl/cli.py`:`_plan_file_sha256` | index-only | The attested plan digest is the sha256 of the index bytes; callers that need the references checked call `_plan_refs_problem` next to it. |
| `scripts/agentctl/cli.py`:`_plan_from_snapshot` | resolves | Parses a stored plan version for the change tier and the row diff: the index bytes come from `_read_plan_version_snapshot`, and its references are resolved against the registered plan path through the object store, so a split plan is read as the plan it stands for. |
| `scripts/agentctl/cli.py`:`_read_plan_version_snapshot` | index-only | Returns the stored index bytes of a plan version by their digest, checking the hash; its one consumer, `_plan_from_snapshot`, resolves the references. |
| `scripts/agentctl/cli.py`:`_snapshot_approved_plan` | index-only | Copies the index bytes to the approved snapshot and puts the index and every referenced file into the object store (`plan_store.store_plan`). |
| `scripts/agentctl/cli.py`:`_snapshot_plan_version` | index-only | Same for every submitted plan version: the index bytes are the version, the referenced files go to the object store. |
| `scripts/agentctl/cli.py`:`_stamp_accepted_plan_digest` | index-only | Stamps the sha256 of the index bytes as the accepted plan digest. |
| `scripts/agentctl/order_approvals.py`:`record_approval` | not a plan | Reads the order-approval ledger (JSON); the plan appears only as a digest argument. |
| `scripts/agentctl/plan.py`:`load_plan` | resolves | The loader: resolves references through `plan_refs.resolve_refs`. |
| `scripts/agentctl/plan.py`:`load_plan_with_digest` | resolves | The loader that also returns the index bytes' digest from the same read. |
| `scripts/agentctl/plan_convert.py`:`cmd_plan_convert_live` | index-only | Reads the registered plan's index bytes: their sha256 must equal the accepted digest before anything is converted, and the bytes go to the object store as the old version. The converter refuses a source that already holds a reference, so the index bytes are the whole plan. |
| `scripts/agentctl/plan_refs.py`:`plan_digest` | resolves | Reports the root digest, every reference and each stage closure digest from the resolved plan. |
| `scripts/agentctl/plan_refs.py`:`plan_externalize` | resolves | Reads the index, resolves it, moves one field into a file and reseals. |
| `scripts/agentctl/plan_store.py`:`store_plan` | resolves | Resolves a plan version's references and stores the index and every visited file by hash. |
| `scripts/agentctl/plan_store.py`:`visited_refs` | resolves | Resolves the plan on disk and returns every reference it visited. |
| `scripts/agentctl/render.py`:`_load_site_source` | resolves | Resolves the plan through `plan_refs.resolve_refs` and keeps the verified bytes of the index and of every referenced file; the index bytes become the whole-plan review's digest object (`objects/<plan_sha256>`). |
| `scripts/agentctl/render.py`:`non_plan_brief_text` | not a plan | Reads a `--plan` file that already failed to load as a typed plan — a plain brief — and returns its text, refusing one past `NON_PLAN_BRIEF_MAX_CHARS`. |
| `scripts/check-exception-set-enumeration.py`:`_field_sentences` | index-only | A report-only line scanner over the index text (it needs line numbers); it never changes an exit code. A field held in a referenced file is not scanned. |
| `scripts/hook-state-gate.py`:`diagnosing_plan_write_ok` | not a plan | Reads the session state (JSON) to decide whether a plan write is allowed. |
| `scripts/plan-review-topological.py`:`Driver.launch` | index-only | Recomputes the root digest the reviewer attests: the sha256 of the index bytes. |
| `scripts/verify-agentctl.py`:`check_obligations` | index-only | Hashes the index bytes of a synthetic plan to attest a review in a self-check. |
| `scripts/verify-agentctl.py`:`check_review_dispatch` | index-only | Same, for the review-dispatch self-check. |
| `scripts/verify-ticket-plan-sync.py`:`plan_file_sha256` | index-only | Compares the sha256 of the index bytes with the digest posted to the ticket. |

### Known indirect readers

Functions that read plan bytes but are not found by the enumerator, because neither the read call
nor the enclosing function names a plan:

- `gates._file_sha256` — the sha256 of a file's bytes, used for the whole-plan review's staleness
  check (`gates._plan_review_content_stale`); on a plan path it hashes the index bytes, so the
  whole-plan review goes stale on any edit, including an edit to a referenced file that was
  resealed into the index.
- `cli._stage_grant_entries` — compares the sha256 of the approved snapshot's bytes with the
  recorded snapshot hash before trusting the snapshot's grants. The hash check is index-only, as
  the snapshot hash is the hash of the index bytes; the snapshot itself is then loaded through
  `plan_store.load_baseline`, which resolves its references.
- `plan_refs.read_referenced_file` — the resolver's default reader of a referenced file; the hash check on
  the returned bytes is the resolver's.
- `plan_refs._parse` — parses one plan-shaped TOML file (the index, a referenced stage file)
  for the resolver, the sealer and the externalizer.
- `plan_store.get` — returns an object from the store by hash and refuses an absent or corrupt one.
- `plan_store._intact` — re-hashes an object already in the store, so `put` rewrites one whose
  bytes do not hash to its name.
- `plan_store.reader` — its disk fallback reads a referenced file by path while a plan version
  is being stored; the resolver checks the hash of the returned bytes.

## How the enumerator works, and what it misses

The enumerator in the test is a syntactic scan (Python `ast`) of every `*.py` under `scripts/`
except `scripts/tests/`. A function is listed when it contains a read call — `tomllib.load`,
`tomllib.loads`, `.read_bytes()`, `.read_text()` or `open(...)` — and at least one of these
mentions "plan" (case-insensitive): an argument of the call, the enclosing function's qualified
name, or a parameter name of the enclosing function. A read outside any function is listed as
`<module>`.

This is broader than "a read whose arguments mention a plan": the narrower rule misses
`plan.load_plan_with_digest` and `plan-review-topological.py`'s root-digest recomputation, which
read the path through a variable named `path`/`target` inside a function that takes a plan.

Limits — what the scan does not see:

- a read through a helper that names no plan (the indirect readers above);
- a read through `Path.open()` or a bare file handle: the scan sees only the builtin `open(...)`,
  so a function whose name and parameters do not say "plan" is missed even when it calls
  `tomllib.load(fh)` on a plan;
- a plan read by a subprocess or a shell command (`cat plan.toml`, `git show`);
- a path or a function that never says "plan" (a renamed parameter);
- code outside `scripts/` and files that are not Python;
- a read of an older plan version from the state directory by anything but the engine's loader.

A row's disposition is a judgement the scan cannot make; the test only requires that it is one of
the three above.

## Root-digest comparisons

A plan's root digest is the sha256 of its index bytes, so converting a plan between its single-file
and its hypertext form changes the root digest and keeps the plan's content digest. A record bound
to the old root (a review, a presentation, the accepted digest) must stay valid through a
`plan-convert-live`, and an edit made after the conversion must still stale it. That is what
`gates.plan_digest_equivalent(state, a, b)` decides: two root digests are equivalent when they are
equal or linked by a chain of `state.plan_conversions`. Every place that compares a recorded root
digest with a current or accepted one therefore goes through it; this inventory lists the places the
enumerator finds and says why each one is routed or exempt. A test
(`scripts/tests/test_plan_convert.py`) requires the rows below to be exactly the enumerated set.

### Dispositions of a comparison

- `routed through plan_digest_equivalent` — the function compares a recorded root digest with a
  current or accepted one by calling `gates.plan_digest_equivalent`; no raw comparison of that kind
  is left in it.
- `audit only` — the digest is compared to find or label a historical record (a ledger row, a
  ticket marker); a mismatch costs a lookup, never a gate.
- `not a binding` — the read or comparison is not a binding of a record to a plan version: a file
  integrity check, a comparison of two values that are both recorded, an exact byte check made
  before a conversion, or a fixture.

### Root-digest comparisons

| Site | Disposition | Why |
|---|---|---|
| `scripts/agentctl/cli.py`:`_count_plan_review_round` | routed through plan_digest_equivalent | Decides whether a review round was already counted for this plan version by comparing the recorded counted digest with the current one. |
| `scripts/agentctl/cli.py`:`cmd_accept` | routed through plan_digest_equivalent | Refuses to bind an acceptance to a plan file that is not the accepted version: the file's digest against the accepted plan digest. |
| `scripts/agentctl/gates.py`:`acceptance_staleness` | routed through plan_digest_equivalent | Compares the digest a review without bindings was recorded against with the digest accepted now. |
| `scripts/agentctl/gates.py`:`_acceptance_review_check` | routed through plan_digest_equivalent | Compares the review's digest and the plan file's digest with the accepted digest. |
| `scripts/agentctl/gates.py`:`_plan_review_content_stale` | routed through plan_digest_equivalent | The review-currency comparison: the plan file's digest against the digest the whole-plan review attested. |
| `scripts/agentctl/gates.py`:`_binds_across_path_change` | routed through plan_digest_equivalent | A review recorded at another path binds a renamed plan only when the digests are equivalent. |
| `scripts/agentctl/gates.py`:`_receipt_binding_blocker` | routed through plan_digest_equivalent | A presentation receipt is stale when the plan file's digest is not equivalent to the receipt's. |
| `scripts/agentctl/gates.py`:`replan_authorization_blockers` | routed through plan_digest_equivalent | An edit that leaves the file's digest equivalent to the accepted one needs no replan-diff presentation. |
| `scripts/agentctl/cli.py`:`_review_trigger_exit` | routed through plan_digest_equivalent | Picks a review-cost trigger's exit: a plan whose digest is equivalent to the one the trigger fired on leaves by the recorded override; any edit owes a fresh pass. |
| `scripts/agentctl/cli.py`:`_review_cycle_arrive` | routed through plan_digest_equivalent | Decides whether an arriving plan version opens a new review cycle: a converted resubmission keeps the open cycle and its act count. |
| `scripts/agentctl/cli.py`:`_plan_file_sha256` | not a binding | Computes the digest a record is bound to; it compares nothing. |
| `scripts/agentctl/cli.py`:`_carry_records` | not a binding | Hashes the plan file to restamp the records it carries onto it (`carry_forward.apply`); it compares no recorded digest. |
| `scripts/agentctl/cli.py`:`_review_baseline` | not a binding | Returns the approved snapshot's recorded hash, or hashes the plan file when there is no snapshot, as the digest the review records describe; it compares nothing. |
| `scripts/agentctl/carry_forward.py`:`_Ctx._was_current` | not a binding | A shortcut: a pair record whose stored digest equals the old version's digest is current. A miss (for example after `plan-convert-live`) falls through to the content-derived binding comparison, which does not depend on the root digest. |
| `scripts/agentctl/gates.py`:`_pair_staleness` | not a binding | The same shortcut for a legacy pair record against the plan file's digest; a miss falls through to the content-derived binding comparison, which does not depend on the root digest. |
| `scripts/agentctl/cli.py`:`_refresh_approved_grant_snapshot` | not a binding | Re-hashes the approved snapshot to refresh the recorded snapshot hash; the snapshot is a copy the engine wrote. |
| `scripts/agentctl/cli.py`:`_stamp_accepted_plan_digest` | not a binding | Writes the accepted digest from the file's bytes; `plan-convert-live` writes the converted root the same way. |
| `scripts/agentctl/cli.py`:`cmd_present_plan` | not a binding | Binds a new receipt to the digest of the plan as it is now; the receipt is created here, not compared. |
| `scripts/agentctl/cli.py`:`_present_order` | not a binding | Compares the order draft's understanding digest with the one `order-submit` recorded, so a changed draft is refused; the receipt it then creates is bound to that digest, not compared against a plan version. |
| `scripts/agentctl/cli.py`:`_attested_digest_refusal` | not a binding | The exact cross-check of the digest a reviewer attests against the reviewed file's bytes at record time, shared by the plan scopes and `--scope order`; it compares nothing recorded earlier. |
| `scripts/agentctl/cli.py`:`cmd_plan_review` | not a binding | An exact cross-check of the digest the reviewer attests against the plan file's bytes at record time; the staleness that follows is read through the callee that counts rounds. |
| `scripts/agentctl/cli.py`:`cmd_dispatch` | not a binding | Compares the hashes of the settings files a spawn materializes, not a plan. |
| `scripts/agentctl/cli.py`:`_stage_grant_entries` | not a binding | Checks the approved snapshot's bytes against the recorded snapshot hash, which `plan-convert-live` never rewrites (no new snapshot is written). |
| `scripts/agentctl/gates.py`:`_file_sha256` | not a binding | The hashing helper itself. |
| `scripts/agentctl/gates.py`:`file_norm_digest` | not a binding | Hashes a norm file, not a plan. |
| `scripts/agentctl/delivery.py`:`has_answered_channel_delivery` | not a binding | Compares the plan digest a channel stamp was written for with the one in the engine's own recorded channel-delivery event; both are recorded, neither is a plan file's current digest. |
| `scripts/agentctl/gates.py`:`_delivery_stamp_blocker` | not a binding | Compares a delivery stamp with its receipt; both are recorded, neither is a plan file's current digest. |
| `scripts/agentctl/plan_convert.py`:`cmd_plan_convert_live` | not a binding | Requires the plan file's digest to equal the accepted digest exactly before converting; after a conversion the two are different forms, and this check runs against the form on disk. |
| `scripts/agentctl/plan_store.py`:`_intact` | not a binding | Re-hashes a stored object against its own name. |
| `scripts/hook-plan-delivery-gate.py`:`_already_stamped` | not a binding | Compares a stamp's receipt digest with the receipt, both recorded. |
| `scripts/plan-review-topological.py`:`Driver.launch` | not a binding | Recomputes the root digest the reviewer attests: the sha256 of the index bytes. A positive control of the enumerator. |
| `scripts/spawn-specialist.py`:`load_review_doc` | not a binding | Computes the digest a `--review-topo` reviewer attests: the loader's index digest for a plan, the sha256 of the file's bytes for an order draft; it compares nothing. |
| `scripts/plan-review-topological.py`:`select_cost_row` | audit only | Picks a cost-ledger row for a pair by the plan digest it was recorded under; a miss means no cost estimate. |
| `scripts/verify-agentctl.py`:`check_obligations` | not a binding | Hashes a synthetic plan to attest a review in a self-check fixture. |
| `scripts/verify-agentctl.py`:`check_review_dispatch` | not a binding | Same, for the review-dispatch self-check fixture. |
| `scripts/verify-judge-isolation.py`:`credential_fingerprint` | not a binding | Hashes a credential file to prove the judge does not touch it. |
| `scripts/verify-ticket-plan-sync.py`:`check` | audit only | Compares the digest posted to the ticket with the file's; a report, never a gate. |
| `scripts/verify-ticket-plan-sync.py`:`format_marker` | audit only | Writes the digest marker posted to the ticket. |
| `scripts/verify-ticket-plan-sync.py`:`plan_file_sha256` | audit only | Hashes the plan file for the ticket marker. |

### What the enumerator misses

The enumerator in the test is a syntactic scan (Python `ast`) of every `*.py` under `scripts/`
except `scripts/tests/`. It selects four things, by the enclosing function's qualified name:

1. a comparison one of whose operands is named `plan_sha256`, `accepted_plan_digest`,
   `plan_snapshot_hash`, `plan_review_counted_digest`, `root_digest` or `plan_digest` (as a name, an
   attribute, a constant subscript or a `.get` of that constant, possibly under `or`);
2. a call `hashlib.sha256(x.read_bytes())`;
3. a call to `_file_sha256` or `_plan_file_sha256`;
4. a call to `plan_digest_equivalent`.

It does not see:

- a comparison through a name that is not in the list (a digest bound to `current`, `before`,
  `digest`) unless the function also hashes a file or calls one of the helpers;
- a digest compared in a shell command, a hook written in another language or a script outside
  `scripts/`;
- a digest compared after a round trip through JSON under a key that is not in the list.

Known comparisons of a root digest that the enumerator does not select, listed by hand:

- the task accumulator's `review_rounds_last_sha256`, compared by equality against the last counted
  plan digest. It is never given a raw root: `cli._count_review_round` passes
  `gates.plan_lineage_root(state, digest)`, one stable digest for every form of a plan.
- history events that carry `plan_sha256` (`plan_review`, `plan_present` and the like). They record
  what was attested and are read for audit; nothing compares them with a current digest.
