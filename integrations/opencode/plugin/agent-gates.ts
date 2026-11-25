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
// outcome is written to the setup marker for the launcher to check. The marker is only ever overwritten,
// so a launcher must delete the one of an earlier launch before it starts OpenCode, or a plugin that was
// skipped would be read as the earlier launch's "ok".
//
// A relative path of a tool call is resolved by OpenCode against the directory of its session. The shim
// asks ctx.session.get for that directory per session and hands it to the bridge, which makes every path
// absolute before a hook sees it; when no directory is known a relative path is refused. Under
// ``opencode serve`` one process holds sessions of several directories, which is why the directory is
// asked per session and the setup directory is only the fallback.
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
const NO_MATCH = "No matches found"; // OpenCode 2.3.2's whole answer to a grep with no hit (probe A6, search_and_output)

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

// The absolute path a grep searches: its path (the session directory when none is given) made absolute
// against the session directory; null when it is relative and that directory is unknown.
function searchRoot(cwd, input) {
  const given = input.input?.path || ".";
  if (isAbsolute(given)) return resolve(given);
  return cwd ? resolve(cwd, given) : null;
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
  if (text === NO_MATCH) return { text, removed: 0 }; // what a search with no hit answers: nothing to hide
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

// Every field of the grep result is filtered or, for a search root that holds a credential path, dropped,
// whatever its type: the model reads content[].text, but the whole result goes on to the bridge, the
// transcript and the logs. A text field is filtered; any other value is scrubbed string by string, or, under
// a risky root, emptied, since no text filter can vouch for it. Only a part's "type" is kept as it is.
function sanitizeGrepResult(input, refused, risky) {
  let removed = 0;
  const filter = (text) => {
    const filtered = filterText(text, refused, risky);
    removed = Math.max(removed, filtered.removed); // the fields repeat one result
    return filtered.text;
  };
  const emptied = (value) => (typeof value === "string" ? "" : Array.isArray(value) ? [] : value && typeof value === "object" ? {} : value);
  const field = (value) => (typeof value === "string" ? filter(value) : risky ? emptied(value) : scrub(value, filter));
  const result = input.result;
  if (typeof result === "string") {
    input.result = filter(result);
    return removed;
  }
  if (!result || typeof result !== "object") return removed;
  for (const key of Object.keys(result)) {
    if (key !== "content" || !Array.isArray(result.content)) {
      result[key] = field(result[key]);
      continue;
    }
    result.content = result.content.map((part) => {
      if (!part || typeof part !== "object") return field(part);
      for (const name of Object.keys(part)) {
        if (name !== "type" || typeof part.type !== "string") part[name] = field(part[name]);
      }
      return part;
    });
  }
  return removed;
}

function install(ctx, failures) {
  const located = ctx.location && typeof ctx.location.directory === "string" && isAbsolute(ctx.location.directory) ? ctx.location.directory : "";
  if (!located) failures.push("ctx.location.directory is absent at setup");
  // The directory of a session, which is where OpenCode resolves a relative path (probe A6): what
  // ctx.session.get answers for it (a hook input names none), else the directory OpenCode was started in.
  // Never this process's own working directory, which a relative path was not resolved against; "" when
  // it is unknown, and then a relative path is refused, not guessed.
  const sessions = new Map();
  const directory = async (input) => {
    const id = input && input.sessionID;
    if (id && sessions.has(id)) return sessions.get(id);
    if (id && typeof ctx.session.get === "function") {
      try {
        const found = (await ctx.session.get({ sessionID: id }))?.location?.directory;
        if (typeof found === "string" && isAbsolute(found)) {
          sessions.set(id, found);
          return found;
        }
      } catch (error) {
        log({ at: "session-directory-error", sessionID: id, error: String(error).slice(0, 200) });
      }
    }
    return located;
  };
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
    const cwd = await directory(input);
    if (tool === "grep") {
      const path = searchRoot(cwd, input);
      if (path === null) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "session directory unknown" });
        refuse("grep refused: its search path is relative and the session directory is unknown");
        return;
      }
      if (gate().refused(path)) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "credential path" });
        refuse(`credential path: ${path}`);
        return;
      }
      greps.set(input.id, path);
    }
    const answer = await verdict("tool.execute.before", input, cwd);
    if (answer.failed || answer.decision !== "allow") {
      const reason = answer.failed || answer.reason || (answer.decision === "deny" ? "denied by a gate" : "the gate bridge answered no decision");
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
    const cwd = await directory(input);
    if (input.tool === "grep") {
      const path = greps.get(input.id) ?? searchRoot(cwd, input);
      greps.delete(input.id);
      let removed = 0;
      try {
        const paths = gate();
        removed = sanitizeGrepResult(input, paths.refused, path === null || paths.risky(path));
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
    const answer = await verdict("session.prompt", input, await directory(input));
    if (answer.failed || answer.decision === "deny") {
      log({ at: "refusal", mechanism: "UserPromptSubmit", sessionID: input.sessionID });
      refuse(answer.failed || answer.reason || "prompt refused by a gate");
      return;
    }
    for (const line of answer.context || []) log({ at: "context", sessionID: input.sessionID, text: line });
  });

  ctx.session.hook("experimental.idle", async (input) => {
    const held = idle.get(input.sessionID) || 0;
    const answer = await verdict("experimental.idle", input, await directory(input));
    if (answer.failed) {
      // Claude Code's Stop hook that fails does not hold the turn either: the turn ends
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
    const answer = await verdict(name, { sessionID: event.data?.sessionID, ...event.data }, await directory({ sessionID: event.data?.sessionID, ...event.data }));
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
