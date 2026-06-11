---
name: Cursor agent model/key config is client-side, not server-side
description: Difficulty it removes — you try to configure Cursor's custom LLM provider server-side (~/.cursor-server/) and it has no effect. Fact — that config lives client-side in Cursor.app on the local machine; only terminal-side CLI env vars can be set server-side.
type: reference
---

# Cursor agent mode: where the model/key actually lives

When a user asks to "configure Cursor on a server" to use a custom LLM (DeepSeek, etc.), the realistic answer:

- **Cursor agent mode calls the cursor.com backend from the client**, not from the remote server. The remote `~/.cursor-server/` only contains the VS Code Remote-SSH server binaries (`bin/linux-x64/`, `bin/multiplex-server`) plus VS Code-style `data/User/settings.json` (which does NOT hold API key / model selection).
- **The actual setting** (custom OpenAI-compatible base URL + API key + model list) lives in **Cursor.app on the local machine**: Settings → Models → Override OpenAI Base URL, OpenAI API Key, add custom model names.
- There is no plist, env var, or file on the remote server that overrides this.

**What CAN usefully be done on the server**: set env vars (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, vendor-specific keys) in the target user's shell rc, so terminal-side CLI / Python tooling run from inside Cursor's Remote-SSH terminal picks up the alternate provider. This does **not** affect Cursor's agent itself.

**`cursor-agent` CLI** is separate — a headless agent that ships as a binary, useful only for unattended/scripted runs (CI, cron, long detached sessions). Not needed for regular Remote-SSH interactive use.

> verified by: inspection of `/home/nick/.cursor-server/` on the0.fun on 2026-05-29 — `data/User/` had no `settings.json`, `data/Machine/` was empty, `bin/` held only VS Code server binaries.
