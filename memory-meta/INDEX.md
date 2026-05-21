# Memory index (deprecated)

Short map. Details in linked files. **Prefer** `~/.claude/memory-global/` and `~/.claude/memory/INDEX.md` — see [README.md](README.md).

## deepagent (robot/deepagent)

| Topic | File |
|-------|------|
| Prod training dataset: pipeline, top50, instruct, repeats, 367/415 | [deepagent/datasets-prod-pipeline.md](deepagent/datasets-prod-pipeline.md) |
| Term **dsv3** and suffixes in YT paths | [deepagent/dsv3-judge-naming.md](deepagent/dsv3-judge-naming.md) |
| Eliza/Zeliboba: where to find LLMs (free communal vs paid external) | [deepagent/eliza-and-zeliboba-models.md](deepagent/eliza-and-zeliboba-models.md) |
| test_quality: full eval, compute_metrics retest (no launcher in Arc) | [deepagent/test-quality-retest.md](deepagent/test-quality-retest.md) |
| DEEPAGENT-416: compute_metrics OOM, agent errors, ephemeral retest | [deepagent/compute-metrics-oom-de416.md](deepagent/compute-metrics-oom-de416.md) |
| Nirvana TTL (deepagent): see [claude-code/nirvana-vh3-ttl-layers.md](claude-code/nirvana-vh3-ttl-layers.md); entry `_build_eval_graph` | [deepagent/nirvana-ttl-retention.md](deepagent/nirvana-ttl-retention.md) |
| train-eval-meta: republish vs convert-only vs full meta (do not rerun train) | [deepagent/train-eval-meta-relaunch.md](deepagent/train-eval-meta-relaunch.md) |

## claude-code (agent setup)

| Topic | File |
|-------|------|
| Parallel arc mounts: `cd ~` before mount, background + `[mounted]` | [claude-code/arc-parallel-mounts.md](claude-code/arc-parallel-mounts.md) |
| Agent instructions: pull → commit → push, cron/systemd pull | [claude-code/instructions-git-sync.md](claude-code/instructions-git-sync.md) |
| Session retrospective 2026-05: mistakes, ticket checklist, gates | [claude-code/session-retrospective-2026-05.md](claude-code/session-retrospective-2026-05.md) |
| Claude Code + Cursor: symlinks, single source, project overlay | [claude-code/claude-cursor-instructions.md](claude-code/claude-cursor-instructions.md) |
| Agent cooperation (role summary) | [../README.md](../README.md) § Agent cooperation |
| PreToolUse hook: word boundaries, false positives on `~/arcadia_*` | [claude-code/hook-word-boundaries.md](claude-code/hook-word-boundaries.md) |
| Nirvana WI: status polling, multiple graphs, "monitoring complete" handoff | [claude-code/nirvana-wi-watch.md](claude-code/nirvana-wi-watch.md) |
| Nirvana/VH3: three TTL layers (WI retention vs operation job vs wait) | [claude-code/nirvana-vh3-ttl-layers.md](claude-code/nirvana-vh3-ttl-layers.md) |
