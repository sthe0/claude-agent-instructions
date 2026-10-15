# Raw plan readers

A hypertext plan ([ADR-0011](../adr/0011-hypertext-plans.md)) is an index file whose fields and
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
| `scripts/agentctl/cli.py`:`_plan_file_sha256` | index-only | The attested plan digest is the sha256 of the index bytes; callers that need the references checked call `_plan_refs_problem` next to it. |
| `scripts/agentctl/cli.py`:`_snapshot_approved_plan` | index-only | Copies the index bytes to the approved snapshot and puts the index and every referenced file into the object store (`plan_store.store_plan`). |
| `scripts/agentctl/cli.py`:`_snapshot_plan_version` | index-only | Same for every submitted plan version: the index bytes are the version, the referenced files go to the object store. |
| `scripts/agentctl/cli.py`:`_stamp_accepted_plan_digest` | index-only | Stamps the sha256 of the index bytes as the accepted plan digest. |
| `scripts/agentctl/cli.py`:`cmd_present_plan` | not a plan | Reads the rendering file whose digest is bound to the delivery receipt, not the plan. |
| `scripts/agentctl/order_approvals.py`:`record_approval` | not a plan | Reads the order-approval ledger (JSON); the plan appears only as a digest argument. |
| `scripts/agentctl/plan.py`:`load_plan` | resolves | The loader: resolves references through `plan_refs.resolve_refs`. |
| `scripts/agentctl/plan.py`:`load_plan_with_digest` | resolves | The loader that also returns the index bytes' digest from the same read. |
| `scripts/agentctl/plan_refs.py`:`plan_digest` | resolves | Reports the root digest, every reference and each stage closure digest from the resolved plan. |
| `scripts/agentctl/plan_refs.py`:`plan_externalize` | resolves | Reads the index, resolves it, moves one field into a file and reseals. |
| `scripts/agentctl/plan_store.py`:`store_plan` | resolves | Resolves a plan version's references and stores the index and every visited file by hash. |
| `scripts/agentctl/plan_store.py`:`visited_refs` | resolves | Resolves the plan on disk and returns every reference it visited. |
| `scripts/agentctl/render.py`:`_load_site_source` | resolves | Resolves the plan through `plan_refs.resolve_refs` and keeps the verified bytes of the index and of every referenced file; the index bytes become the whole-plan review's digest object (`objects/<plan_sha256>`). |
| `scripts/agentctl/render.py`:`non_plan_brief_text` | not a plan | Reads a `--plan` file that already failed to load as a typed plan — a plain brief — and returns its text, refusing one past `NON_PLAN_BRIEF_MAX_CHARS`. |
| `scripts/check-exception-set-enumeration.py`:`_field_sentences` | index-only | A report-only line scanner over the index text (it needs line numbers); it never changes an exit code. A field held in a referenced file is not scanned. |
| `scripts/hook-state-gate.py`:`diagnosing_plan_write_ok` | not a plan | Reads the session state (JSON) to decide whether a plan write is allowed. |
| `scripts/hook-ticket-plan-sync.py`:`session_plan_path` | not a plan | Reads the session state (JSON) for the registered plan path. |
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
