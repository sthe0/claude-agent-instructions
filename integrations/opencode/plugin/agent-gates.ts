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
// asks ctx.session.get for that directory per session and hands it to the bridge, which resolves every path
// the way OpenCode does (probe A6, path_forms) before a hook sees it. When the directory of a session cannot
// be had, every tool call of it is refused: the setup directory is not guessed in its place, since under
// ``opencode serve`` one process holds sessions of several directories.
//
// Environment: AGENT_GATE_BRIDGE (bridge script path), AGENT_GATE_PYTHON (interpreter, default the `python3`
// of PATH; resolved once at load to its real path, and run with -I),
// AGENT_GATE_LOG (a file that gets one JSON line per refusal and decision, never a tool result),
// AGENT_GATE_SETUP_MARKER (a file that gets {"status": "ok" | "failed", "failures": [...]} once setup ran).
import { spawn, spawnSync } from "node:child_process";
import { accessSync, appendFileSync, constants as fsConstants, realpathSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, delimiter, dirname, isAbsolute, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BRIDGE = process.env.AGENT_GATE_BRIDGE || resolve(HERE, "..", "..", "..", "scripts", "opencode-gate-bridge.py");
// The interpreter is resolved once, here, to the real path of an executable file: the configured value of the
// launch environment, else the first `python3` on its PATH. A later change of PATH (or a new `python3` earlier on
// it) cannot select another one. When nothing resolves the path stays a name no file has, so every bridge call fails
// and setup refuses.
function resolveInterpreter(name, searchPath) {
  const candidates = name.includes(sep) ? [name] : searchPath.split(delimiter).filter(Boolean).map((dir) => resolve(dir, name));
  for (const candidate of candidates) {
    try {
      accessSync(candidate, fsConstants.X_OK);
      const real = realpathSync(candidate);
      if (statSync(real).isFile()) return real;
    } catch (error) {
      // try the next PATH entry
    }
  }
  return resolve("/", "no-such-interpreter", name);
}
const PYTHON = resolveInterpreter(process.env.AGENT_GATE_PYTHON || "python3", process.env.PATH || "");
// -I: no PYTHON* variables, no user site, and the bridge's directory is not put first on sys.path (the bridge adds it last).
const PYTHON_FLAGS = ["-I"];
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

// A refusal the shim makes itself reads "refused: <class> <detail>" (the bridge's own refusals read the same);
// a hook's deny, which the bridge hands over, reads "deny: <script> <reason>". The class names are the bridge's
// (REFUSAL_CLASSES in scripts/lib/opencode_events.py).
const refused = (cls, detail) => `refused: ${cls} ${detail}`;

// OpenCode loads code from a project's own config unless OPENCODE_DISABLE_PROJECT_CONFIG=1 is set for the
// process, and OPENCODE_CONFIG_PROJECT_DISABLE, when set, wins over it (probe A6): a session started without the
// variable may be running code the gates were never written against.
const PROJECT_CONFIG_OFF = process.env.OPENCODE_DISABLE_PROJECT_CONFIG === "1" && process.env.OPENCODE_CONFIG_PROJECT_DISABLE === undefined;

// The digest of each session's rows snapshot, as the bridge answered it at the session's first event.
const digests = new Map();

// A bridge that does not answer in time is a failed bridge: the call is refused, not left waiting.
const BRIDGE_TIMEOUT_MS = Number(process.env.AGENT_GATE_BRIDGE_TIMEOUT_MS) > 0 ? Number(process.env.AGENT_GATE_BRIDGE_TIMEOUT_MS) : 300000;

function runBridge(args, input) {
  return new Promise((done) => {
    const child = spawn(PYTHON, [...PYTHON_FLAGS, BRIDGE, ...args], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      done({ code: -2, out, err: `timed out after ${BRIDGE_TIMEOUT_MS} ms` });
    }, BRIDGE_TIMEOUT_MS);
    child.stdout.on("data", (chunk) => (out += chunk));
    child.stderr.on("data", (chunk) => (err += chunk));
    child.on("error", (error) => (clearTimeout(timer), done({ code: -1, out, err: String(error) })));
    child.on("close", (code) => (clearTimeout(timer), done({ code, out, err })));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(input));
  });
}

async function verdict(name, input, cwd) {
  const session = input && input.sessionID;
  const sent = { ...input, cwd };
  if (session && digests.has(session)) sent.rowsDigest = digests.get(session);
  const result = await runBridge(["event", name], sent);
  if (result.code !== 0) return { failed: `gate bridge failed (${name}, exit ${result.code}): ${result.err.trim().slice(0, 300)}` };
  try {
    const answer = JSON.parse(result.out);
    if (session && answer && typeof answer.digest === "string" && !digests.has(session)) digests.set(session, answer.digest);
    return answer;
  } catch (error) {
    return { failed: `gate bridge answered no JSON (${name})` };
  }
}

function bridgeValue(command) {
  const result = spawnSync(PYTHON, [...PYTHON_FLAGS, BRIDGE, command], { encoding: "utf8" });
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

// The absolute path OpenCode 2.3.2 takes a path argument to name, for exactly the forms probe A6 recorded
// (the bridge's resolve_path is the same rule): empty or not given is the session directory; "~" and "~/..."
// are the home directory; a leading "/" is absolute; any other form (~user, $HOME, file://..., .., ./) is
// relative to the session directory. null for anything else (a value that is no string, a NUL byte) and when
// a directory it needs is unknown: a form that was not recorded is refused, not guessed.
function resolveForm(given, cwd) {
  if (given === undefined || given === null) given = "";
  if (typeof given !== "string" || given.includes("\0")) return null;
  if (given === "~" || given.startsWith("~/")) {
    const home = homedir();
    const rest = given.slice(2);
    return home && isAbsolute(home) && !rest.startsWith("/") ? resolve(home, rest) : null;
  }
  if (given.startsWith("/")) return resolve(given);
  return cwd && isAbsolute(cwd) ? resolve(cwd, given) : null;
}

// The absolute path a grep searches.
const searchRoot = (cwd, input) => resolveForm(input.input?.path, cwd);

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
  if (!PROJECT_CONFIG_OFF) failures.push("OPENCODE_DISABLE_PROJECT_CONFIG=1 is not set for this process");
  // The directory of a session, which is where OpenCode resolves a relative path and runs a command (probe
  // A6): what ctx.session.get answers for it (a hook input names none). Only a successful answer is kept.
  // When the answer cannot be had the directory is "" and the call is refused: neither this process's
  // working directory nor the directory OpenCode was started in is the session's, under ``opencode serve``.
  // An input that names no session (none of the tool hooks) gets the setup directory.
  const sessions = new Map();
  const directory = async (input) => {
    const id = input && input.sessionID;
    if (!id) return located;
    if (sessions.has(id)) return sessions.get(id);
    try {
      if (typeof ctx.session.get !== "function") throw new Error("ctx.session.get is not available");
      const found = (await ctx.session.get({ sessionID: id }))?.location?.directory;
      if (typeof found !== "string" || !isAbsolute(found)) throw new Error("ctx.session.get gave no absolute directory");
      sessions.set(id, found);
      return found;
    } catch (error) {
      log({ at: "session-directory-error", sessionID: id, error: String(error).slice(0, 200) });
      return "";
    }
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
      refuse(refused("gate-setup", `the agent gates did not set up (${unavailable}); ${tool} refused`));
      return;
    }
    if (!PROJECT_CONFIG_OFF) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "project config not off" });
      refuse(refused("project-config", "OPENCODE_DISABLE_PROJECT_CONFIG=1 is not set (or OPENCODE_CONFIG_PROJECT_DISABLE is), so the session may have loaded code from the project's own OpenCode config"));
      return;
    }
    if (DENIED_TOOLS.has(tool)) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "denied by name" });
      refuse(refused("refused-tool", `${tool} is denied by the agent gates`));
      return;
    }
    if (tool === "shell" && !shellBound) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "shell hook not registered" });
      refuse(refused("gate-setup", "shell is refused: the shell hook that binds a shell to the OpenCode session did not register"));
      return;
    }
    const cwd = await directory(input);
    if (!cwd) {
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "session directory unknown" });
      refuse(refused("directory", `${tool} refused: the directory its session runs in is unknown, so no path or command can be placed`));
      return;
    }
    if (tool === "grep") {
      const path = searchRoot(cwd, input);
      if (path === null) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "search path not resolvable" });
        refuse(refused("path-form", "grep refused: its search path is in a form OpenCode's resolver was not recorded to handle"));
        return;
      }
      if (gate().refused(path)) {
        log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, reason: "credential path" });
        refuse(`deny: agent-gates credential path: ${path}`);
        return;
      }
      greps.set(input.id, path);
    }
    const answer = await verdict("tool.execute.before", input, cwd);
    if (answer.failed || answer.decision !== "allow") {
      const reason = answer.failed ? refused("gate-setup", answer.failed) : answer.reason || refused("gate-setup", "the gate bridge answered no decision");
      log({ at: "refusal", mechanism: "PreToolUse", id: input.id, tool, script: answer.script || "bridge", detail: String(reason).slice(0, 200) });
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
