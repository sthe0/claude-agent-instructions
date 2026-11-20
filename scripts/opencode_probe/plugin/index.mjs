// The probe plugin: logs every hook call to PROBE_LOG and, driven by PROBE_RULES, refuses calls.
//   PROBE_RULES = {"throw":[{"name":"shell","contains":"marker-x"}],"deny":[...],
//                  "env":{"KEY":"value|{sessionID}"},"idle_continue":"text","tool":"probe_tool",
//                  "refuse_prompt":true,"command_prefix_env":{"KEY":"value|{sessionID}"}}
// PROBE_INERT=1 keeps every hook registered and logging but makes the throw and the deny inert.
import { appendFileSync, writeFileSync } from "node:fs";

const LOG = process.env.PROBE_LOG || "/dev/null";
const RULES = JSON.parse(process.env.PROBE_RULES || "{}");
const INERT = process.env.PROBE_INERT === "1";

function plain(value, depth = 0) {
  if (value === null || value === undefined) return value ?? null;
  if (typeof value === "function") return "[fn]";
  if (typeof value === "string") return value.length > 400 ? value.slice(0, 400) + "…" : value;
  if (typeof value !== "object") return value;
  if (depth > 5) return "[deep]";
  if (Array.isArray(value)) return value.slice(0, 12).map((v) => plain(v, depth + 1));
  const out = {};
  for (const key of Object.keys(value).slice(0, 40)) {
    if (key === "env") out[key] = Object.keys(value[key] || {});
    else out[key] = plain(value[key], depth + 1);
  }
  return out;
}

const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");

function matching(rules, name, payload) {
  const text = JSON.stringify(payload ?? null);
  return (rules || []).find((rule) => (rule.name === "*" || rule.name === name) && (!rule.contains || text.includes(rule.contains)));
}

function register(domain, hookName, label, callback) {
  try {
    domain.hook(hookName, callback);
    log({ at: "register", hook: label, ok: true });
  } catch (error) {
    log({ at: "register", hook: label, ok: false, error: String(error) });
  }
}

export default {
  id: "opencode-probe-plugin",
  setup(ctx) {
    log({ at: "loaded", rules: RULES, inert: INERT });
    register(ctx.tool, "execute.before", "PreToolUse", (input) => {
      log({ at: "tool.execute.before", input: plain(input) });
      if (RULES.command_prefix_env && input.tool === "shell" && typeof input.input?.command === "string") {
        const exports = Object.entries(RULES.command_prefix_env)
          .map(([key, value]) => `export ${key}=${String(value).replace("{sessionID}", input.sessionID)};`);
        input.input.command = `${exports.join(" ")} ${input.input.command}`;
      }
      const rule = matching(RULES.throw, input.tool, input.input);
      if (rule && !INERT) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool: input.tool });
        throw new Error(`probe throw: ${input.tool}`);
      }
    });
    register(ctx.tool, "execute.after", "PostToolUse", (input) => {
      log({ at: "tool.execute.after", input: plain(input) });
    });
    register(ctx.permission, "evaluate", "permission.evaluate", (input) => {
      log({ at: "permission.evaluate", input: plain(input) });
      const rule = matching(RULES.deny, input.action, input.resources);
      if (rule && !INERT) {
        log({ at: "refusal", mechanism: "permission.evaluate", id: input.source?.id, action: input.action });
        input.effect = "deny";
        input.message = `probe deny: ${input.action}`;
      }
    });
    register(ctx.session, "prompt", "UserPromptSubmit", (input) => {
      log({ at: "session.prompt", input: plain(input) });
      if (RULES.refuse_prompt && !INERT) {
        log({ at: "refusal", mechanism: "UserPromptSubmit", sessionID: input.sessionID });
        throw new Error("probe throw: prompt");
      }
    });
    register(ctx.session, "context", "context", (input) => {
      log({ at: "session.context", sessionID: input.sessionID, messages: plain(input.messages), system: plain(input.system) });
    });
    register(ctx.session, "compaction", "compaction", (input) => {
      log({ at: "session.compaction", input: plain(input) });
    });
    let continued = new Set();
    register(ctx.session, "experimental.idle", "Stop", async (input) => {
      log({ at: "session.experimental.idle", input: plain(input) });
      if (RULES.idle_continue && !continued.has(input.sessionID)) {
        continued.add(input.sessionID);
        await ctx.session.synthetic({ sessionID: input.sessionID, text: RULES.idle_continue, metadata: { source: "probe" } });
        log({ at: "idle.continued", sessionID: input.sessionID });
      }
    });
    register(ctx.shell, "create.before", "shell.create.before", (input) => {
      log({ at: "shell.create.before", input: plain(input) });
      for (const [key, value] of Object.entries(RULES.env || {})) {
        input.env[key] = String(value).replace("{sessionID}", input.sessionID ?? input.toolCall?.sessionID ?? "");
      }
    });
    try {
      const sub = ctx.event.subscribe((event) => log({ at: "event", event: plain(event) }));
      const shape = sub === null || sub === undefined ? String(sub) : typeof sub;
      log({ at: "register", hook: "event.subscribe", ok: true, returned: shape,
            keys: sub && typeof sub === "object" ? Object.keys(sub).slice(0, 10) : [],
            async_iterable: Boolean(sub && sub[Symbol.asyncIterator]) });
      if (sub && sub[Symbol.asyncIterator]) {
        (async () => {
          for await (const event of sub) log({ at: "event", event: plain(event) });
        })().catch((error) => log({ at: "event.stream.error", error: String(error) }));
      }
    } catch (error) {
      log({ at: "register", hook: "event.subscribe", ok: false, error: String(error) });
    }
    if (RULES.tool) {
      ctx.tool.transform((tools) => {
        tools.add({
          name: RULES.tool,
          options: { codemode: false },
          description: "probe tool that writes its marker",
          input: { type: "object", properties: {} },
          output: { type: "string" },
          execute: () => {
            writeFileSync(process.env.PROBE_TOOL_MARKER || "/dev/null", "ran");
            log({ at: "plugin-tool.executed", name: RULES.tool });
            return { output: "ok" };
          },
        });
      });
    }
  },
};
