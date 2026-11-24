// @ts-nocheck
// OpenCode plugin shim of the agent's gates. It forwards OpenCode hook events to
// scripts/opencode-gate-bridge.py, which runs Core's unchanged hook scripts, and carries out the
// verdict: a throw in tool.execute.before refuses the call, a deny of the idle hook continues the turn.
// The shim holds no gate logic of its own beyond what no hook script can do on OpenCode: the grep
// credential gate (a permission deny never matches grep), the session-id prefix of every shell
// command, the idle loop cap, and the refusal of the tools that move a session or a worktree.
// Written without type annotations so a plain JavaScript runtime can load the same text.
//
// Environment: AGENT_GATE_BRIDGE (bridge script path), AGENT_GATE_PYTHON (interpreter, default python3),
// AGENT_GATE_LOG (a file that gets one JSON line per refusal and decision, never a tool result).
import { spawn, spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import { dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BRIDGE = process.env.AGENT_GATE_BRIDGE || resolve(HERE, "..", "..", "..", "scripts", "opencode-gate-bridge.py");
const PYTHON = process.env.AGENT_GATE_PYTHON || "python3";
const LOG = process.env.AGENT_GATE_LOG || "/dev/null";
const DENIED_TOOLS = new Set(["opencode_worktree_create", "opencode_worktree_remove", "opencode_session_move"]);

const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");

function refuse(message) {
  throw new Error(message); // gate:refuse
}

function quote(text) {
  return "'" + String(text).replace(/'/g, "'\\''") + "'";
}

function runBridge(args, input) {
  return new Promise((done) => {
    const child = spawn(PYTHON, [BRIDGE, ...args], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    child.stdout.on("data", (chunk) => (out += chunk));
    child.stderr.on("data", (chunk) => (err += chunk));
    child.on("error", (error) => done({ code: -1, out, err: String(error) }));
    child.on("close", (code) => done({ code, out, err }));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(input));
  });
}

async function verdict(name, input) {
  const result = await runBridge(["event", name], { ...input, cwd: process.cwd() });
  if (result.code !== 0) return { failed: `gate bridge failed (${name}, exit ${result.code}): ${result.err.trim().slice(0, 300)}` };
  try {
    return JSON.parse(result.out);
  } catch (error) {
    return { failed: `gate bridge answered no JSON (${name})` };
  }
}

function bridgeValue(command) {
  const result = spawnSync(PYTHON, [BRIDGE, command], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(`gate bridge ${command} failed: ${String(result.stderr).slice(0, 200)}`);
  return JSON.parse(result.stdout);
}

// "Found N matches", then per file a line "<abs path>:" and indented "Line N: ..." lines, possibly an
// unindented note. A file's block runs to the next path line or note.
function filterText(text, refused) {
  const lines = text.split("\n");
  if (!/^Found \d+ matches$/.test(lines[0])) return { text, removed: 0 };
  const kept = [];
  let keeping = true;
  let removed = 0;
  for (const line of lines.slice(1)) {
    if (line.startsWith(sep) && line.endsWith(":")) {
      keeping = !refused(resolve(line.slice(0, -1)));
      if (!keeping) removed += 1;
    } else if (line.length && !/^\s/.test(line)) {
      keeping = true;
    }
    if (keeping) kept.push(line);
  }
  if (removed === 0) return { text, removed };
  const matches = kept.filter((line) => /^ {2}Line \d+: /.test(line)).length;
  return { text: `Found ${matches} matches\n${kept.join("\n")}`, removed };
}

export default {
  id: "agent-gates",
  setup(ctx) {
    const credentials = bridgeValue("credential-paths").map((p) => resolve(p));
    const idleMax = bridgeValue("idle-cap");
    const refusedPath = (path) => credentials.some((c) => path === c || path.startsWith(c + sep));
    const idle = new Map();
    log({ at: "loaded", credential_paths: credentials.length });

    ctx.tool.hook("execute.before", async (input) => {
      const tool = input.tool;
      if (DENIED_TOOLS.has(tool)) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "denied by name" });
        refuse(`${tool} is denied by the agent gates`);
        return;
      }
      if (tool === "grep") {
        const path = resolve(process.cwd(), input.input?.path || ".");
        if (refusedPath(path)) {
          log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "credential path" });
          refuse(`credential path: ${path}`);
          return;
        }
      }
      const answer = await verdict("tool.execute.before", input);
      if (answer.failed || answer.decision === "deny") {
        const reason = answer.failed || answer.reason || "denied by a gate";
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, script: answer.script || "bridge" });
        refuse(reason);
        return;
      }
      if (tool === "shell" && typeof input.input?.command === "string") {
        input.input.command = `export AGENTCTL_HARNESS_SESSION_ID=${quote(input.sessionID)}; ${input.input.command}`;
      }
    });

    ctx.tool.hook("execute.after", async (input) => {
      if (input.tool === "grep") {
        let removed = 0;
        for (const part of input.result?.content || []) {
          if (part?.type !== "text" || typeof part.text !== "string") continue;
          const filtered = filterText(part.text, refusedPath);
          part.text = filtered.text;
          removed += filtered.removed;
        }
        log({ at: "grep-filter", id: input.id, removed_blocks: removed });
      }
      const answer = await verdict("tool.execute.after", input);
      if (answer.failed) log({ at: "bridge-error", event: "tool.execute.after", error: answer.failed });
    });

    ctx.session.hook("prompt", async (input) => {
      const answer = await verdict("session.prompt", input);
      if (answer.failed || answer.decision === "deny") {
        log({ at: "refusal", mechanism: "UserPromptSubmit", sessionID: input.sessionID });
        refuse(answer.failed || answer.reason || "prompt refused by a gate");
        return;
      }
      for (const line of answer.context || []) log({ at: "context", sessionID: input.sessionID, text: line });
    });

    ctx.session.hook("experimental.idle", async (input) => {
      const held = idle.get(input.sessionID) || 0;
      const answer = await verdict("experimental.idle", input);
      if (answer.failed) {
        log({ at: "bridge-error", event: "experimental.idle", error: answer.failed });
        return;
      }
      if (answer.decision !== "deny") return;
      if (held >= idleMax) {
        log({ at: "idle-cap", sessionID: input.sessionID, continues: held });
        return;
      }
      idle.set(input.sessionID, held + 1);
      await ctx.session.synthetic({ sessionID: input.sessionID, text: answer.reason || "a turn-end gate holds this turn", metadata: { source: "agent-gates" } }); // gate:hold
      log({ at: "idle-continued", sessionID: input.sessionID, continues: held + 1 });
    });

    try {
      ctx.shell.hook("create.before", (input) => {
        input.env.AGENTCTL_RUNTIME_HOST = "opencode";
      });
    } catch (error) {
      log({ at: "register", hook: "shell.create.before", ok: false, error: String(error) });
    }

    const forward = async (event) => {
      const names = { "session.created": "session.created", "session.deleted": "session.deleted" };
      const name = names[event?.type];
      if (!name) return;
      const answer = await verdict(name, { sessionID: event.data?.sessionID, ...event.data });
      if (answer.failed) log({ at: "bridge-error", event: name, error: answer.failed });
      for (const line of answer.context || []) log({ at: "context", sessionID: event.data?.sessionID, text: line });
    };
    try {
      const sub = ctx.event.subscribe(forward);
      if (sub && sub[Symbol.asyncIterator]) {
        (async () => {
          for await (const event of sub) await forward(event);
        })().catch((error) => log({ at: "event-stream-error", error: String(error) }));
      }
    } catch (error) {
      log({ at: "register", hook: "event.subscribe", ok: false, error: String(error) });
    }
  },
};
