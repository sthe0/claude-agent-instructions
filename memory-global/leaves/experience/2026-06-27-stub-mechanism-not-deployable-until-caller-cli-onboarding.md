---
name: 2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding
description: Difficulty — the difficulty-channel (port + Startrek/External adapters + tests) looked done but could not be handed to other people: no production code path called .submit() (only tests did), the hardcoded queue (CORE-INSTR) never existed, there was no CLI entry, the channel was not config-selectable, and no onboarding step provisioned identity/credentials. Plus per-machine authority (is_author) was stored in config.md, which is git-shared via symlink, so it could not actually vary per machine.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [docs/adr/0001-consensus-architecture.md, scripts/difficulty_channel/, scripts/file-difficulty.py, 2026-06-25-critique-primitive-unifies-conflict-and-principle.md, 2026-06-25-state-gate-needs-acting-session-at-executing-via-toml.md, 2026-06-24-gate-exemption-is-category-error-for-result-images.md]
created: 2026-06-29
last_verified: 2026-06-29
---

# A designed port/adapter mechanism is not deployable until it has a production caller, a CLI, live preconditions, and onboarding

## Difficulty
A mechanism with a clean port/adapter design and green unit tests reads as 'finished', but distributing it to other operators exposed five gaps that unit tests never touch: (1) no production caller — authority.file_core_difficulty() was invoked only from tests; (2) the adapter targeted a queue (CORE-INSTR) that was never created; (3) no human-facing CLI to file a difficulty; (4) the channel was hardcoded, not chosen per machine; (5) no onboarding wired the per-machine identity/credentials. Separately, per-machine authority was read from config.md — a file symlinked from the git repo, so a 'per-machine' flag was in fact shared by every clone.

## Order & criterion
Sequenced as a TOML plan the agentctl engine could track stage-by-stage: (1) probe external preconditions read-only BEFORE coding (Startrek queue reachable + GitHub repo reachable) — caught that the queue name needed verifying and gh CLI was absent; (2) retarget the Startrek adapter constant to the real queue OOSEVENREPORT and purge the dead CORE-INSTR literal; (3) implement the GitHub Issues adapter mirroring the Startrek pattern (pure mapping + injectable http + token resolution), with functional_ground round-tripping via a label; (4) replace the is_author config flag with a git-push-capability probe + a gitignored machine-local ~/.claude/agent-identity.local for channel selection; (5) add the file-difficulty.py CLI delegating to authority.file_core_difficulty(); (6) wire the self-improvement skill to the concrete CLI command (not prose); (7) onboarding bootstrap (configure-identity.sh, idempotent, wired into setup-symlinks.sh) + ADR/README/operations docs; (8) live E2E submit+pull on BOTH channels.

**Acceptance check:** measurable — a single CLI command (file-difficulty.py) on a non-author machine lands a record in the machine's configured channel (Startrek OOSEVENREPORT or GitHub Issues), and the author-side core-difficulty-digest.py pulls and clusters it from both channels. Verified by a live E2E: real OOSEVENREPORT-1 + github issues/1 created, pulled by the digest, then closed.

## Contexts

### 2026-06-27 — agent-system self-improvement / distribution
- Where it arose: ~/claude-agent-instructions (the agent-instructions repo); scripts/difficulty_channel/ port+adapters, scripts/file-difficulty.py, scripts/core-difficulty-digest.py, skills/self-improvement/, docs/adr/0001 + docs/operations/.
- Working plan: /home/the0/.claude/plans/difficulty-channel-distribution.toml


### 2026-06-29 — 2026-06-29 — autodetect channel from hardware signals
- Where it arose: ~/claude-agent-instructions; scripts/difficulty_channel/detect.py (new, pure+injected probes) + tests/test_detect.py; scripts/configure-identity.sh wired to it; docs/operations/difficulty-channel-onboarding.md
- Working plan: /home/the0/.claude/plans/difficulty-channel-autodetect.toml

## Common core & variations
**Common:** Continues lesson (3): per-machine config must derive from machine-local signals, not git-shared config. The earlier step replaced the is_author flag with a push-probe and made the channel manually selectable in a gitignored file; this step removes the manual step — configure-identity.sh auto-detects the channel at install from hardware signals (corp hostname, ya+arc, skotty, /etc/yandex, tracker/github tokens) via a pure injection-tested detect_channel().

**Variations:** New boundary: detection must use PRECEDENCE, not credential-presence. A single machine can carry BOTH internal and external credentials at once (this dev box has ~/.tracker-token AND ~/.github-token), so a naive 'which token exists' rule misroutes; a strong internal hardware signal (hostname/toolchain/skotty) must outrank github-cred presence. Also: detection belongs in a pure injected function (package idiom, offline-tested) with the install bash a thin guarded caller that never aborts on detect failure (falls back to startrek). Closes gap (4) 'channel hardcoded, not chosen per machine' from this leaf's original Difficulty.

## Cost
8-stage substantive plan; 8 developer spawns (depth 1, budget medium), total ~$9.4 spawn cost; one overcome-difficulty cycle (stage-1 gh-absent precondition) navigated via scratchpad-edit + structural replan past the plan-freeze; two commits on main (f8b1ee9, e7c0980), not pushed.

## Self-critique of the agent system
Reusable lessons: (1) a port/adapter + unit tests is a STUB, not a deployable mechanism, until it has a production caller, a human CLI, verified live preconditions, and an onboarding step — 'designed' != 'works for other people'. (2) Verify an external integration's preconditions (resource exists, credential has scope) with a read-only probe BEFORE writing code against it — a hardcoded id and a stub adapter are not evidence the endpoint is real or writable. (3) Per-machine config must NOT live in a git-shared (symlinked) file — derive authority from a capability probe (git push --dry-run) and keep machine-local selection in a gitignored file. (4) The plan-freeze catch-22 (cannot edit a frozen plan to fix a failed stage) is navigated by editing a scratchpad copy then replanning with a STRUCTURAL diff so the engine reaches PLAN_READY — see the state-gate leaf.
