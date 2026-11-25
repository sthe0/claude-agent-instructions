// Adapter probe module: loading it is the observation. It appends the name of the route it was reached by
// (ROUTE_NAME, replaced by the harness before the file is written) to ROUTEPROBE_LOG when the module is
// evaluated, and again from setup when OpenCode treats it as a plugin; setup also adds a tool named TOOL_NAME
// through the plugin context's tool editor, so a run's offered-tool list shows whether the plugin was set up.
// The default export carries both the plugin shape and the 1.x custom-tool shape so any route's loader finds
// what it expects. It changes nothing else.
import { appendFileSync } from "node:fs";

const LOG = process.env.ROUTEPROBE_LOG || "/dev/null";
const log = (entry) => appendFileSync(LOG, JSON.stringify({ route: "ROUTE_NAME", ...entry }) + "\n");

log({ at: "module" });

export default {
  id: "route-probe-TOOL_NAME",
  setup(ctx) {
    log({ at: "setup" });
    try {
      ctx.tool.transform((tools) => {
        tools.add({
          name: "TOOL_NAME",
          options: { codemode: false },
          description: "route probe tool",
          input: { type: "object", properties: {} },
          output: { type: "string" },
          execute: () => ({ output: "ok" }),
        });
      });
    } catch (error) {
      log({ at: "setup-error", error: String(error) });
    }
  },
  description: "route probe",
  args: {},
  execute: async () => "route-probe-tool-ran",
};
