// Adapter probe plugin whose setup throws: shows what OpenCode does with a plugin that fails to set up.
export default {
  id: "throwing-setup",
  setup() {
    throw new Error("adapter-probe-setup-throw");
  },
};
