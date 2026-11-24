// @ts-nocheck
// OpenCode plugin shim of the agent's gates. It forwards OpenCode hook events to
// scripts/opencode-gate-bridge.py, which runs Core's unchanged hook scripts, and carries out the
// verdict: a throw in tool.execute.before refuses the call, a deny of the idle hook continues the turn.
// The shim holds no gate logic of its own beyond what no hook script can do on OpenCode: the grep
// credential gate (a permission deny never matches grep), the session-id prefix of every shell
// command, the idle loop cap, and the refusal of the tools that move a session or a worktree.
// Written without type annotations so a plain JavaScript runtime can load the same text.
//
// OpenCode skips, without a word, a plugin whose setup throws, which would leave every tool call ungated.
// So setup never throws: a failure it cannot survive leaves hooks that refuse the tool calls, and the
// outcome is written to the setup marker for the launcher to check.
//
// Environment: AGENT_GATE_BRIDGE (bridge script path), AGENT_GATE_PYTHON (interpreter, default python3),
// AGENT_GATE_LOG (a file that gets one JSON line per refusal and decision, never a tool result),
// AGENT_GATE_SETUP_MARKER (a file that gets {"status": "ok" | "failed", "failures": [...]} once setup ran).
import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, realpathSync, writeFileSync } from "node:fs";
import { basename, dirname, isAbsolute, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BRIDGE = process.env.AGENT_GATE_BRIDGE || resolve(HERE, "..", "..", "..", "scripts", "opencode-gate-bridge.py");
const PYTHON = process.env.AGENT_GATE_PYTHON || "python3";
const LOG = process.env.AGENT_GATE_LOG || "/dev/null";
const MARKER = process.env.AGENT_GATE_SETUP_MARKER || "";
const DENIED_TOOLS = new Set(["opencode_worktree_create", "opencode_worktree_remove", "opencode_session_move"]);
const WITHHELD = "[search output withheld: it could not be checked against the credential paths]";

function log(entry) {
  try {
    appendFileSync(LOG, JSON.stringify(entry) + "\n");
  } catch (error) {
    // an unwritable log must not unload the gates
  }
}

function writeMarker(failures) {
  if (!MARKER) return;
  try {
    writeFileSync(MARKER, JSON.stringify({ status: failures.length ? "failed" : "ok", failures }) + "\n");
  } catch (error) {
    log({ at: "marker-error", error: String(error) });
  }
}

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

async function verdict(name, input, cwd) {
  const result = await runBridge(["event", name], { ...input, cwd });
  if (result.code !== 0) return { failed: `gate bridge failed (${name}, exit ${result.code}): ${result.err.trim().slice(0, 300)}` };
  try {
    return JSON.parse(result.out);
  } catch (error) {
    return { failed: `gate bridge answered no JSON (${name})` };
  }
}

function bridgeValue(command) {
  const result = spawnSync(PYTHON, [BRIDGE, command], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(`gate bridge ${command} failed: ${String(result.error || result.stderr).slice(0, 200)}`);
  return JSON.parse(result.stdout);
}

// The path with its symlinks resolved; the tail that does not exist yet is kept as written, under the
// real path of its nearest existing ancestor.
function realPath(path) {
  const tail = [];
  let head = resolve(path);
  for (;;) {
    try {
      return resolve(realpathSync(head), ...tail);
    } catch (error) {
      const parent = dirname(head);
      if (parent === head) return resolve(path);
      tail.unshift(basename(head));
      head = parent;
    }
  }
}

const within = (path, root) => path === root || path.startsWith(root + sep);

// A path is judged by its lexical form and by its real path, against the lexical and the real form of
// every credential path, so a symlink on either side cannot move one out of the other's reach.
// The credential paths are resolved again at each call, so a link made after setup is still seen.
function pathGate(credentials) {
  const forms = (path) => [resolve(path), realPath(path)];
  return () => {
    const roots = credentials.flatMap(forms);
    return {
      refused: (path) => forms(path).some((p) => roots.some((c) => within(p, c))),
      risky: (path) => forms(path).some((p) => roots.some((c) => within(c, p))),
    };
  };
}

// "Found N matches", then per file a line "<abs path>:" and indented "Line N: ..." lines, possibly an
// unindented note. A file's block runs to the next path line; a note never ends a refused block.
// A shape that is not this one cannot be checked, so when the search root holds a credential path the
// text is withheld instead of passed.
function filterText(text, refused, risky) {
  const unchecked = risky ? { text: WITHHELD, removed: 1 } : { text, removed: 0 };
  const lines = text.split("\n");
  if (!/^Found \d+ matches$/.test(lines[0])) return unchecked;
  const kept = [];
  let keeping = true;
  let removed = 0;
  for (const line of lines.slice(1)) {
    if (line.length && !/^\s/.test(line) && line.endsWith(":")) {
      if (!isAbsolute(line)) return unchecked;
      keeping = !refused(line.slice(0, -1));
      if (!keeping) removed += 1;
    }
    if (keeping) kept.push(line);
  }
  if (removed === 0) return { text, removed };
  const matches = kept.filter((line) => /^ {2}Line \d+: /.test(line)).length;
  return { text: `Found ${matches} matches\n${kept.join("\n")}`, removed };
}

function scrub(value, filter, depth = 0) {
  if (typeof value === "string") return filter(value);
  if (depth > 8 || !value || typeof value !== "object") return value;
  for (const key of Object.keys(value)) value[key] = scrub(value[key], filter, depth + 1);
  return value;
}

// Every field of the grep result is filtered or, for a search root that holds a credential path, dropped:
// the model reads content[].text, but the whole result goes on to the bridge, the transcript and the logs.
function sanitizeGrepResult(input, refused, risky) {
  let removed = 0;
  const filter = (text) => {
    const filtered = filterText(text, refused, risky);
    removed = Math.max(removed, filtered.removed); // the fields repeat one result
    return filtered.text;
  };
  const result = input.result;
  if (typeof result === "string") {
    input.result = filter(result);
    return removed;
  }
  if (!result || typeof result !== "object") return removed;
  for (const part of Array.isArray(result.content) ? result.content : []) {
    if (part && typeof part.text === "string") part.text = filter(part.text);
  }
  if (typeof result.output === "string") result.output = filter(result.output);
  for (const key of Object.keys(result)) {
    if (key === "content" || key === "output") continue;
    if (!risky) result[key] = scrub(result[key], filter);
    else if (typeof result[key] === "string") result[key] = "";
    else if (result[key] && typeof result[key] === "object") result[key] = Array.isArray(result[key]) ? [] : {};
  }
  return removed;
}

function install(ctx, failures) {
  const located = ctx.location && typeof ctx.location.directory === "string" ? ctx.location.directory : "";
  const directory = (input) => (input && (input.directory || input.cwd)) || located || process.cwd();
  let credentials = [];
  let idleMax = 0;
  let unavailable = "";
  try {
    credentials = bridgeValue("credential-paths");
    idleMax = bridgeValue("idle-cap");
  } catch (error) {
    unavailable = String(error.message || error);
    failures.push(`bridge: ${unavailable}`);
  }
  const gate = pathGate(credentials);
  const idle = new Map();
  const greps = new Map();
  let shellBound = true;
  log({ at: "loaded", credential_paths: credentials.length });

  ctx.tool.hook("execute.before", async (input) => {
    const tool = input.tool;
    if (unavailable) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "setup failed" });
      refuse(`the agent gates did not set up (${unavailable}); ${tool} refused`);
      return;
    }
    if (DENIED_TOOLS.has(tool)) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "denied by name" });
      refuse(`${tool} is denied by the agent gates`);
      return;
    }
    if (tool === "shell" && !shellBound) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "shell hook not registered" });
      refuse("shell is refused: the shell hook that binds a shell to the OpenCode session did not register");
      return;
    }
    const cwd = directory(input);
    if (tool === "grep") {
      const path = resolve(cwd, input.input?.path || ".");
      if (gate().refused(path)) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "credential path" });
        refuse(`credential path: ${path}`);
        return;
      }
      greps.set(input.id, path);
    }
    const answer = await verdict("tool.execute.before", input, cwd);
    if (answer.failed || answer.decision === "deny") {
      const reason = answer.failed || answer.reason || "denied by a gate";
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, script: answer.script || "bridge" });
      greps.delete(input.id);
      refuse(reason);
      return;
    }
    if (tool === "shell" && typeof input.input?.command === "string") {
      input.input.command = `export AGENTCTL_HARNESS_SESSION_ID=${quote(input.sessionID)}; ${input.input.command}`;
    }
  });

  ctx.tool.hook("execute.after", async (input) => {
    const cwd = directory(input);
    if (input.tool === "grep") {
      const path = greps.get(input.id) ?? resolve(cwd, input.input?.path || ".");
      greps.delete(input.id);
      let removed = 0;
      try {
        const paths = gate();
        removed = sanitizeGrepResult(input, paths.refused, paths.risky(path));
      } catch (error) {
        input.result = { content: [{ type: "text", text: WITHHELD }] };
        log({ at: "grep-filter-error", id: input.id, error: String(error) });
      }
      log({ at: "grep-filter", id: input.id, removed_blocks: removed });
    }
    const answer = await verdict("tool.execute.after", input, cwd);
    if (answer.failed) log({ at: "bridge-error", event: "tool.execute.after", error: answer.failed });
  });

  ctx.session.hook("prompt", async (input) => {
    const answer = await verdict("session.prompt", input, directory(input));
    if (answer.failed || answer.decision === "deny") {
      log({ at: "refusal", mechanism: "UserPromptSubmit", sessionID: input.sessionID });
      refuse(answer.failed || answer.reason || "prompt refused by a gate");
      return;
    }
    for (const line of answer.context || []) log({ at: "context", sessionID: input.sessionID, text: line });
  });

  ctx.session.hook("experimental.idle", async (input) => {
    const held = idle.get(input.sessionID) || 0;
    const answer = await verdict("experimental.idle", input, directory(input));
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
    shellBound = false;
    failures.push(`shell.create.before: ${String(error)}`);
    log({ at: "register", hook: "shell.create.before", ok: false, error: String(error) });
  }

  const forward = async (event) => {
    const names = { "session.created": "session.created", "session.deleted": "session.deleted" };
    const name = names[event?.type];
    if (!name) return;
    const answer = await verdict(name, { sessionID: event.data?.sessionID, ...event.data }, directory(event.data));
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
}

export default {
  id: "agent-gates",
  setup(ctx) {
    const failures = [];
    try {
      install(ctx, failures);
    } catch (error) {
      const reason = `setup threw: ${String(error)}`;
      failures.push(reason);
      try {
        ctx.tool.hook("execute.before", async (input) => {
          log({ at: "refusal", mechanism: "PreToolUse", id: input?.id, tool: input?.tool, reason: "setup threw" });
          refuse(`the agent gates did not set up (${reason}); ${input?.tool} refused`);
        });
      } catch (hookError) {
        failures.push(`no refusing hook could be registered: ${String(hookError)}`);
      }
    }
    writeMarker(failures);
  },
};
