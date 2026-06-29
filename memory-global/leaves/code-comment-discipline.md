---
name: code-comment-discipline
description: Default no comments; comment only when the *why* is non-obvious. Build / config files are not exceptions — never annotate an import / PEERDIR / dependency line with "what this does".
type: feedback
created: 2026-05-27
last_verified: 2026-06-24
---

# Code comment discipline

Default to writing **no** comments. Add one only when the *why* would be invisible to a future reader from the names alone: a workaround for a specific bug, an ordering constraint, a pinned-version rationale, a hidden invariant. If removing the comment would not confuse a future reader, do not write it.

**Why:** PR review feedback on DEEPAGENT-414 (2026-05-26): "Многие комментарии в ревью выглядят избыточными. Твои инструкции должны говорить о том, чтобы предпочитать выразительность кода комментариям." A series of commits on that branch had landed comments like `# OAuth tokens via YAV — canonical arcadia client` above `library/python/vault_client`, `# Tracker / Startrek client used by tracker_fetch.py` above `library/python/startrek_python_client`, `# Standard arcadia deps` above a PEERDIR block, `# Prompts and model presets live alongside the code as plain files` above `RESOURCE_FILES`. Each reads the identifier and writes it back as prose — pure noise that ages worse than the code (the comment claims a relationship that may not survive the next refactor).

**How to apply:**

- *Build / config files* (`ya.make`, `a.yaml`, `Dockerfile`, `Makefile`, `pyproject.toml`, `setup.py`) **are not exceptions**. An `import` / `PEERDIR` / dependency / `RESOURCE_FILES` entry is its own documentation. Annotate only when the entry is genuinely surprising: a non-default flag, a workaround for an upstream bug, an ordering constraint imposed by the toolchain, a pinned version that exists for a specific compatibility reason.
- *Python / source files* — same rule. Do not introduce comments above class blocks or function bodies that restate the name. Module-level docstrings are fine when the module does something non-trivial; one-line restatements of the class/function name are not.
- *Comments to delete on sight:*
  - `# Standard <X> deps` / `# Core <Y> deps` headers above dependency blocks — the section's grouping is visible from the entries themselves.
  - `# <Library> for <feature>` above a single import where the library name already names the feature (e.g. `import requests` does not need `# HTTP client for the Tracker API`).
  - `# Used by <other_module>.py` — a cross-reference that turns stale the moment either side moves.
  - `# Added for <ticket-id> / <feature>` — historical context, belongs in the commit message.
- *Refactors / PR review* — when you encounter an excessive comment, prefer to **delete** it over rewriting it. A shorter file with no comment beats a tidier comment that still restates the name.
- *Commit messages and PR descriptions* are the right place for "added for ticket X", "this replaces module Y", "we picked library Z because of ...". That context lives in VCS history, not in code.

Linked instructions:

- `skills/specializations/developer/SKILL.md` § While developing — short rule (default no-comments) with concrete antipatterns; this is the form spawned developer agents see.

Related leaves: [[reasoning-and-task-solving]] — "understand before acting" (less code → less commentary).
