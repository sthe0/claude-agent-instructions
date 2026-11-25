// Adapter probe plugin: when the first prompt arrives it logs to MDPROBE_LOG which skills, agents and commands
// OpenCode lists to a plugin (names only), and which of those lists the plugin context has at all. It changes
// nothing.
import { appendFileSync } from "node:fs";

const LOG = process.env.MDPROBE_LOG || "/dev/null";
const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");
const names = (list) => {
  if (Array.isArray(list)) return list.map((item) => String(item?.name ?? item?.id ?? item)).sort();
  const items = (list && typeof list === "object" ? Object.values(list) : []).flat(2);
  if (items.length) return items.map((item) => (item && typeof item === "object" ? String(item.name ?? item.id ?? JSON.stringify(item).slice(0, 80)) : String(item))).sort();
  return "raw:" + JSON.stringify(list)?.slice(0, 1500);
};

export default {
  id: "md-probe",
  setup(ctx) {
    log({ at: "setup", ctx_keys: Object.keys(ctx) });
    let done = false;
    ctx.session.hook("prompt", async () => {
      if (done) return;
      done = true;
      for (const key of ["skill", "agent", "command", "mode", "reference"]) {
        try {
          const area = ctx[key];
          if (!area || typeof area.list !== "function") {
            log({ at: "list", area: key, present: Boolean(area) });
            continue;
          }
          log({ at: "list", area: key, present: true, names: names(await area.list()) });
        } catch (error) {
          log({ at: "list", area: key, error: String(error).slice(0, 300) });
        }
      }
    });
  },
};
