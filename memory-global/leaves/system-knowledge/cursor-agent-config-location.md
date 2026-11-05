---
name: Cursor agent model/key config is client-side, not server-side
description: Difficulty it removes — you try to configure Cursor's custom LLM provider server-side (~/.cursor-server/) and it has no effect. Fact — that config lives client-side in Cursor.app on the local machine; only terminal-side CLI env vars can be set server-side.
type: reference
schema: regularity/v1
converted_from: leaf/v1
given: measurement
given_source: /home/nick/.cursor-server/
created: 2026-05-29
last_verified: 2026-06-24
---

# Cursor agent mode: where the model/key actually lives

## Difficulty

You try to configure Cursor's custom LLM provider (API key, model, base URL) on the remote server under `~/.cursor-server/` and it has no effect — the change is silently ignored and Cursor keeps using the client-side setting. The remote `~/.cursor-server/` directory looks like it holds Cursor data, so editing it there seems like the right path.

## Regularity

On an inspection of /home/nick/.cursor-server/ on the host the0.fun on 2026-05-29, the directory held only VS Code Remote-SSH server binaries (bin/ with bin/linux-x64/ and bin/multiplex-server); data/User/ had no settings.json and data/Machine/ was empty, so no API key, base URL or model selection for Cursor's agent mode was found in that directory. The leaf states, without recording a separate observation, that Cursor's agent mode calls the cursor.com backend from the client and that the custom LLM provider setting (OpenAI-compatible base URL, API key, custom model names) is set in Cursor.app on the local machine under Settings, Models. The leaf also states, as its own inference, that env vars set in the remote user's shell rc (OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL) reach terminal-side CLI and Python tooling run from Cursor's Remote-SSH terminal and do not affect Cursor's agent itself; and that the cursor-agent CLI is a separate headless binary.

## Given

Given from measurement: `/home/nick/.cursor-server/`. The statement is taken as known from that source, not induced from recorded experience.

## Refutation

A file or env var under the remote ~/.cursor-server/ or in the remote shell rc that changes the model or key Cursor's interactive agent mode uses, or an inspection of that directory on the same host finding a settings.json, a populated data/Machine/ or a binary other than VS Code server binaries, would show the observed part of the statement false.

## Guidance

When a user asks to "configure Cursor on a server" to use a custom LLM (DeepSeek, etc.), the realistic answer:

- **Cursor agent mode calls the cursor.com backend from the client**, not from the remote server. The remote `~/.cursor-server/` only contains the VS Code Remote-SSH server binaries (`bin/linux-x64/`, `bin/multiplex-server`) plus VS Code-style `data/User/settings.json` (which does NOT hold API key / model selection).
- **The actual setting** (custom OpenAI-compatible base URL + API key + model list) lives in **Cursor.app on the local machine**: Settings → Models → Override OpenAI Base URL, OpenAI API Key, add custom model names.
- There is no plist, env var, or file on the remote server that overrides this.

**What CAN usefully be done on the server**: set env vars (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, vendor-specific keys) in the target user's shell rc, so terminal-side CLI / Python tooling run from inside Cursor's Remote-SSH terminal picks up the alternate provider. This does **not** affect Cursor's agent itself.

**`cursor-agent` CLI** is separate — a headless agent that ships as a binary, useful only for unattended/scripted runs (CI, cron, long detached sessions). Not needed for regular Remote-SSH interactive use.

> verified by: inspection of `/home/nick/.cursor-server/` on the0.fun on 2026-05-29 — `data/User/` had no `settings.json`, `data/Machine/` was empty, `bin/` held only VS Code server binaries.

## See also

- [[cursor-agent-cli-spawn]] — headless cursor-agent CLI for unattended runs
