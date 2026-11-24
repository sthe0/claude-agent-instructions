// Adapter probe plugin: logs what OpenCode hands a plugin (the setup context and each hook input), as key
// names and scalar values only, to CTXPROBE_LOG. It changes nothing.
import { appendFileSync } from "node:fs";

const LOG = process.env.CTXPROBE_LOG || "/dev/null";
const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");
const scalars = (value) =>
  Object.fromEntries(
    Object.entries(value || {}).filter(([, v]) => ["string", "number", "boolean"].includes(typeof v)).map(([k, v]) => [k, String(v).slice(0, 300)]),
  );
const shape = (value) => (value && typeof value === "object" ? Object.keys(value) : typeof value);

export default {
  id: "ctx-probe",
  setup(ctx) {
    log({ at: "setup", ctx_keys: Object.keys(ctx), scalars: scalars(ctx), location: scalars(ctx.location), app: scalars(ctx.app), nested: Object.fromEntries(Object.keys(ctx).map((k) => [k, shape(ctx[k])])), process_cwd: process.cwd() });
    ctx.tool.hook("execute.before", async (input) => {
      log({ at: "tool.execute.before", keys: Object.keys(input), scalars: scalars(input), process_cwd: process.cwd() });
    });
    ctx.session.hook("prompt", async (input) => {
      log({ at: "session.prompt", keys: Object.keys(input), scalars: scalars(input) });
    });
    try {
      ctx.shell.hook("create.before", (input) => {
        log({ at: "shell.create.before", keys: Object.keys(input), scalars: scalars(input) });
      });
    } catch (error) {
      log({ at: "register", hook: "shell.create.before", ok: false, error: String(error) });
    }
  },
};
