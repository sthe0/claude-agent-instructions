# Documentation

The full documentation for this agent system, organized **general → specific**. The top-level [project README](../README.md) is the minimal entry point — what the system is, the core mental model, and how to start; this tree is everything below that. Read the sections top-to-bottom for a guided path, or jump to the one you need.

## Concepts — the four foundational ideas

The whole system rests on these. Everything else exists to serve one of them.

- [Difficulty](concepts/difficulty.md) — the foundational object: a divergence between a desired and an actual state; the agent's universal job is to remove it.
- [Task](concepts/task.md) — the form every action takes; classified by weight, with routing following from the class.
- [Universal manager-actor](concepts/manager-actor.md) — the single executor; one disciplined actor that resolves small tasks itself and coordinates specialists for large ones.
- [Memory model](concepts/memory-model.md) — the means of accumulating experience in overcoming difficulties; the three scopes and how to pick one.

## Architecture — how the system is built

How the layers compose, the coordination engine and its state machine, and the consensus architecture for distributing instructions across developers.

- [The seven-layer model](architecture/layers.md) — the seven layers (substrate → distribution) and how each constrains the one below it.
- [The coordination engine and its state machine](architecture/coordination-engine.md) — `agentctl`, the spine it drives, and the two non-skippable gates.
- [The consensus architecture](architecture/consensus-architecture.md) — the canonical narrative of distributing one evolving Core across a team.
- [Instruction layering](architecture/instruction-layering.md) — the applicable Core < Team < Personal precedence + replace-vs-merge contract.
- [Personal layer](architecture/personal-layer.md) — the highest-precedence, machine-local layer's scope and authority.
- [Core-difficulty mass threshold](architecture/core-difficulty-calibration.md) — calibration of the flagging threshold for the difficulty-accumulation channel.

## Processes — how a task moves through the system

The task lifecycle and the sub-processes it invokes: planning, difficulty/replan, self-improvement, resolution, partition.

## Components — the parts and where they live

The skills, agents, hooks, scripts, memory scopes, settings, and the Cursor mirror.

## Operations — running and maintaining the repo

Setup and distribution, the git workflow, the verification-guard suite, and layer maintenance.

## Decisions — architecture decision records

The [ADR index](adr/README.md) records the significant, hard-to-reverse design decisions.
