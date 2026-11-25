// Adapter probe plugin: logs what OpenCode hands a plugin after a tool ran (the shape of the result and the
// head and tail of its text) to AFTERPROBE_LOG, and, for the session of each tool call, the keys and scalar
// values of what ``ctx.session.get`` answers. It changes nothing.
import { appendFileSync } from "node:fs";

const LOG = process.env.AFTERPROBE_LOG || "/dev/null";
const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");
const scalars = (value) =>
  Object.fromEntries(
    Object.entries(value || {}).filter(([, v]) => ["string", "number", "boolean"].includes(typeof v)).map(([k, v]) => [k, String(v).slice(0, 300)]),
  );

export default {
  id: "after-probe",
  setup(ctx) {
    ctx.tool.hook("execute.after", async (input) => {
      const result = input.result;
      const parts = result && Array.isArray(result.content) ? result.content : [];
      const text = parts.map((p) => (p && typeof p.text === "string" ? p.text : "")).join("");
      log({
        at: "tool.execute.after",
        tool: input.tool,
        id: input.id,
        keys: Object.keys(input),
        result_keys: result && typeof result === "object" ? Object.keys(result) : typeof result,
        output_type: result && result.output === null ? "null" : typeof (result && result.output),
        part_keys: parts.map((p) => Object.keys(p || {})),
        text_len: text.length,
        text_head: text.slice(0, 400),
        text_tail: text.slice(-200),
      });
    });
    ctx.tool.hook("execute.before", async (input) => {
      const entry = { at: "session.get", tool: input.tool, id: input.id };
      for (const argument of [input.sessionID, { sessionID: input.sessionID }]) {
        try {
          const got = await ctx.session.get(argument);
          entry[typeof argument === "string" ? "by_id" : "by_object"] = { type: typeof got, keys: got && typeof got === "object" ? Object.keys(got) : [], scalars: scalars(got), location: scalars(got && got.location), location_keys: got && got.location && typeof got.location === "object" ? Object.keys(got.location) : typeof (got && got.location) };
        } catch (error) {
          entry[typeof argument === "string" ? "by_id" : "by_object"] = { error: String(error).slice(0, 200) };
        }
      }
      log(entry);
    });
  },
};
