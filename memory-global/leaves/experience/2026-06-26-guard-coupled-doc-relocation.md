---
name: 2026-06-26-guard-coupled-doc-relocation
description: Difficulty — relocating documentation content that a verify-* guard pins to a specific file (inventory sentinels + REGION_FILES, layout-contract require_file, a prose-length ceiling) breaks the guard unless the move and the guard are edited in the SAME commit; plus two relative-path traps (synthesized links must be region-relative, and verify-cross-refs staged-mode only scans tracked files so a full-mode pass can mask a pre-commit failure).
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Да, решено"
refs: [2026-06-24-developer-marker-not-on-line-1-false-block]
created: 2026-06-27
last_verified: 2026-07-01
---

# Relocating guard-pinned docs: move + repoint the guard in lockstep, watch region-relative paths

## Difficulty
A large docs restructure (shrink a 251-line README to a 78-line entry point; grow `docs/` into a general→specific Diátaxis tree) moves content that automated guards are coupled to. Three coupling traps, each of which turns a clean-looking move into a red `verify-all`:
1. **Content a guard pins to a file.** `verify-readme.py` validates inventory sentinels (`<!-- inventory:skills:begin -->` …) against the filesystem via a `REGION_FILES` map. Moving the sentinel block out of README without repointing `REGION_FILES` — and without fixing `_synthesize_row`, which hardcoded repo-root-relative File-column links — fails the guard or emits cross-ref-broken links into the new file.
2. **Synthesized links are region-relative.** Markdown links are resolved by `verify-cross-refs` relative to the file that contains them. Moving the table from `README.md` (repo root) to `docs/components/skills.md` means every File-column path must become `../../skills/…`, and any `--fix` path-synthesizer must derive the prefix from `REGION_FILES[name]`'s directory (not a literal), so a future `--fix` stays correct if the file moves again.
3. **Guards that assert a file EXISTS.** `verify-layout-contract.sh` uses `require_file "$REPO/<path>"`; a new canonical index (`docs/README.md`) should be added there so it can't silently vanish. Use the `"$REPO/"`-prefixed form — the script runs cwd-independently, so a bare relative `require_file docs/README.md` spuriously fails when invoked from a non-repo-root cwd.

## Order & criterion
- **Move + guard in ONE commit.** When relocating content a guard reads, edit the guard (`REGION_FILES`, path-synthesizer, `require_file`, the `GOVERNED` list) in the same stage/commit as the `git mv`. Verify with the guard's own idempotency: `verify-readme.py --fix && git diff --quiet` proves the synthesized rows match what you wrote.
- **Repoint every inbound ref, relative to each referrer.** After `git mv`, `grep -rn <basename>` and fix each hit relative to its own directory (a sibling that linked `../foo.md` may now need `../operations/foo.md`).
- **A new ceiling guard is three coupled edits:** a `config.md` constants row (matching the `| \`key\` | \`value\` | … |` regex), the `GOVERNED` list in `lint-prose-length.py`, and the docstring table — all three or the linter reports a missing key.
- **Acceptance check:** measurable — `verify-all.py` exit 0 AND `verify-layout-contract.sh` exit 0 AND README ≤ ceiling; plus an acceptance-review that the index reads general→specific.

**The staged-mode gotcha (cost me a pre-commit failure):** `verify-cross-refs` in `--staged` mode only scans *tracked* files. A newly-authored, still-untracked doc passes a full-mode run but fails the pre-commit staged-mode hook once staged — so run the guard the same way the hook does before committing, and beware illustrative markdown links or backtick-wrapped paths starting with a top-level directory name (such as `scripts/` or `docs/`) that the cross-ref checker reads as real paths.

## Contexts

### 2026-07-01 — landing a NEW script to `origin/main` trips `verify-readme` (registry coupling), and `land-on-main.sh` can't isolate the row off a WIP branch
- Where it arose: after building `scripts/land-on-main.sh` (a determinized "land the staged diff onto origin/main via an isolated worktree" command) I dogfooded it to land *itself*. The worktree pre-commit hook failed `verify-readme` — `scripts/README.md`'s script inventory pins every `scripts/*` file, so a new script must carry its README registry row in the **same commit**.
- The determinized-script trap: `land-on-main.sh` lands exactly `git diff --cached`. To satisfy the guard I'd have to also stage the README row — but on a shared **WIP feature branch** `scripts/README.md` already carried unrelated concurrent edits (reordered rows, a `sigma-sentinel` row), so there was no clean way to stage *only* my row. The staged-diff mechanism is blind to "just my hunk".
- Fix (mirrors this leaf's lockstep rule): fall back to the **manual worktree recipe** — `git worktree add --detach $WT origin/main`, copy the new files in, insert **only** the one registry row into the worktree's (pristine `origin/main`) `README.md`, then commit (hook runs `verify-all --staged` → 14/14) and `git push origin HEAD:main`. Constructing the isolated 3-file hunk against clean `origin/main` sidesteps the branch's WIP entirely. Landed `02a9b81..c67367d`.
- Generalized lesson: a registry/inventory guard (`verify-readme`) is one more instance of "guard pins content to a file"; **any** flow that lands a new guarded file to main (including a determinized staged-diff lander) must carry the registry edit in the same commit — and when your branch's copy of the registry file is dirty with foreign WIP, the isolated worktree is the only clean carrier. A staged-diff lander is the right tool only when your change does **not** also touch a WIP-contaminated registry file.

### 2026-06-26 — README→docs restructure (6 stages, agentctl-driven)
- Where it arose: shrink README to an entry-point; build `docs/{concepts,architecture,processes,components,operations}` + index, all guards green.
- Working plan: 6 substantive stages on the agentctl spine, each a `spawn:developer` per stage with a focused per-stage brief, `record-result --control` (developer self-review attestation) + coordinator independent re-verify between stages. Stage 4 (the guard-coupled sentinel move) was pre-flagged highest-risk; I read `verify-readme.py` myself first and wrote the `_synthesize_row` region-relative-prefix requirement + a `--fix`-idempotency line into the done-criterion. Principle held throughout: docs **link, never restate** the canonical behavioural rules (CLAUDE.md/skills) — zero rule changes, pure descriptive reorg + relocations. Result: README 251→78, `docs/` 30+ docs across 6 clusters, verify-all 13/13, verify-layout-contract exit 0, verify-cross-refs 0 broken in 125 files.

## Common core & variations
**Common:** any time you move content that an automated guard reads or pins, the move and the guard edit are one atomic change; verify via the guard's own idempotency, not just a green full-run.

**Variations here:** sentinel-block relocation (content the guard validates) vs `require_file` (existence the guard asserts) vs a new prose-length ceiling (a new guarded file). All three rode the same lockstep discipline; the region-relative path-synthesis and the staged-vs-full cross-ref scope were the two non-obvious traps.

## Cost
6 developer spawns (sonnet/large), ~$1.5–3.2 each; coordinator orchestration + per-stage independent re-verify in-thread. 6 commits 34c28a7..b62c2fe, pushed to origin/main. ~1 self-caught issue per stage by the developer's self-review (backticks in link text, a dup sentence, the staged-mode cross-ref false trigger).

## Self-critique of the agent system
The guard coupling was only handled cleanly because I read `verify-readme.py` and `lint-prose-length.py` myself before writing the stage-4/stage-6 briefs and baked the exact failure modes (region-relative `_synthesize_row`, `--fix` idempotency, `$REPO/`-prefixed `require_file`) into the done-criteria. Had I delegated the stage blind, the developer would likely have moved the sentinels and left `REGION_FILES`/`_synthesize_row` stale. Lesson reinforced: for a guard-coupled relocation, the coordinator should inspect the guard internals and encode the coupling into the brief's measurable done-criterion, rather than trusting the spawn to rediscover it.
