// Adapter probe plugin: logs, per tool call, the arguments OpenCode handed the before-hook and the head of the
// text the call produced (or that it produced none), to FORMSPROBE_LOG. It changes nothing.
import { appendFileSync } from "node:fs";

const LOG = process.env.FORMSPROBE_LOG || "/dev/null";
const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");

export default {
  id: "forms-probe",
  setup(ctx) {
    ctx.tool.hook("execute.before", async (input) => {
      log({ at: "before", id: input.id, tool: input.tool, args: input.input });
    });
    ctx.tool.hook("execute.after", async (input) => {
      const result = input.result;
      const parts = result && Array.isArray(result.content) ? result.content : [];
      const text = parts.map((p) => (p && typeof p.text === "string" ? p.text : "")).join("");
      log({ at: "after", id: input.id, tool: input.tool, text_len: text.length, text_head: text.slice(0, 300) });
    });
  },
};
