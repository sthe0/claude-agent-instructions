---
name: 2026-07-31-generated-config-fix-the-generator-not-the-artifact
description: A tool rejected .cursor/cli.json for a missing permissions.deny; the file turned out to be GENERATED per-mount by a sync script, so the hand-patch would have been silently reverted by the next setup-local.sh run — and the generator was also emitting Claude-notation rules (Bash/Edit/mcp__x__y) that Cursor CLI cannot match at all. Before hand-editing any config/artifact, grep the repo for its path to find the writer; a generated file's real defect is in the generator.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "да"
created: 2026-07-31
last_verified: 2026-07-31
---

# A schema error in a config file: fix the generator, not the artifact

## Difficulty
A tool reports a defect in a config file, and the file is edited in place — but the file is a GENERATED artifact whose writer will overwrite the fix on its next run. The visible symptom is at the artifact; the defect lives in the generator. Worse, the same generator usually reproduces the defect for every other consumer of the artifact, so the in-place fix is both temporary and local.

## Order & criterion
Before editing any config or data file that a tool complains about, establish whether it is hand-maintained or generated: grep the surrounding repo/scripts for the file's path (and for its directory) to find a writer. If a writer exists, the edit target is the writer; regenerate the artifact from it and re-run the failing check.

**Acceptance check:** The failing tool check passes AND re-running the generator reproduces the fixed artifact byte-for-byte (i.e. the fix survives regeneration), not merely the artifact being valid once.

## Contexts

### 2026-07-31 — initial
- Where it arose: 2026-07-31, a project workspace on an org-internal monorepo mount. `cursor-agent` refused to start: "Invalid project config at .cursor/cli.json: permissions.deny Required". I patched cli.json directly and reported done; the user then asked why `.cursor/` was not a symlink farm like the sibling `.claude/`, which surfaced `common/scripts/sync-cursor-cli-permissions.sh` in the agent-config storage repo as the generator (invoked by `setup-local.sh`). cli.json is deliberately a real file rather than a symlink, because it embeds the per-mount auto-memory path.
- Working plan: 1) rg the config path across the tooling repo -> found the sync script writing json.dump({permissions:{allow:...}}) with no deny key. 2) Fetch the authoritative notation from cursor.com/docs/cli/reference/permissions: Cursor CLI knows only Shell/Read/Write/WebFetch/Mcp, requires both allow and deny, WebFetch takes a bare domain (no 'domain:' prefix), and there is no Edit token. 3) Add a to_cursor_rule() translator (Bash->Shell, Edit|Write->Write, Read->Read, WebFetch(domain:x)->WebFetch(x), mcp__server__tool->Mcp(server:tool)); drop untranslatable rules with a printed warning rather than silently. 4) Keep multi-token command arguments verbatim (Shell(git remote:*)) instead of widening to Shell(git:*) — fail-closed beats over-permitting. 5) Emit deny: []. 6) Regenerate, verify with 'cursor-agent --version' in the project dir (it DOES validate the project config — proven by reproducing the error in a throwaway dir with a deliberately broken cli.json). 7) Run the repo suite (35 PASS) since setup-local.sh calls the generator. 8) Land through the storage repo's scripted PR + self-merge path (a personal sandbox area needs no review).

## Cost
~35 min wall-clock, single Cursor session, no claude -p spawns ($0 spawn cost); 4 user interventions (1 corrective: 'shouldn't .cursor be a symlink like .claude?'), 2 approval gates answered via AskQuestion.

## Self-critique of the agent system
I fixed the generated artifact and closed the task without ever asking who writes that file — the user's follow-up, not my own check, surfaced the generator. The 'establish the result image as a verification method' rule was satisfied only shallowly: my criterion was 'cursor-agent starts', which a doomed hand-patch also satisfies. A durable criterion for artifact-level defects must include survival of regeneration.
