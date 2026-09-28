---
name: 2026-09-27-claude-code-cli-2-1-197-cpu-hang-pin-2-0-30
description: Diagnosing a Telegram bot's Claude-subscription provider that never replied traced to the claude-agent-sdk shelling out to a claude-code CLI binary installed via 'npm install -g @anthropic-ai/claude-code' with no version pin (fetches whatever is latest, currently 2.1.197). That version hangs indefinitely at ~89% CPU on ANY invocation including --help/--version, confirmed via /proc/<pid>/fd showing zero network sockets (only eventpoll/timerfd/eventfd) and thread state stuck in futex_wait_queue -- a pure CPU-spin bug, not network/DNS/permission related (ruled out DNS via getent hosts, ruled out root-vs-nonroot). Version 2.0.30 works correctly and instantly in the same container. Fix: pin the Dockerfile's npm install to @anthropic-ai/claude-code@2.0.30 with a comment explaining why, instead of installing unpinned 'latest'.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "the0"
refs: [src: /Users/the0/projects/the0fun-public-bot/Dockerfile, commit: 6018376]
created: 2026-09-27
last_verified: 2026-09-27
---

# npm-installed claude-code CLI 2.1.197 spins at 100% CPU forever on any invocation, zero sockets opened

## Difficulty
A Docker container that npm-installs '@anthropic-ai/claude-code' unpinned silently picks up a broken CLI release (2.1.197) that hangs forever on every invocation with zero observable network activity, making a working provider look like a network/auth failure and burning significant debugging time on the wrong hypotheses (DNS, permissions, org status) before the CLI version itself is suspected.

## Order & criterion
Deploy an invite-only Telegram bot with a Claude-subscription-backed LLM provider (claude-agent-sdk shelling out to the claude-code CLI) as its own Docker container; the live Telegram test must actually receive a reply.

**Acceptance check:** Live Telegram message to the deployed bot receives a reply within the container's normal turnaround time

## Contexts

### 2026-09-27 — initial
- Where it arose: the0fun-public-bot Docker container on the0.fun VPS, Dockerfile Node.js/npm install step for the Claude Code CLI dependency of the claude_subscription provider
- Working plan: /Users/the0/.claude-agent/plans/the0fun-public-bot.toml

## Cost
diagnosis required SSH process/proc-level forensics (ps, /proc/<pid>/fd, /proc/<pid>/task/*/stat) across several hypotheses before isolating the CLI version as the cause
