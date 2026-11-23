// E13's credential grep gate. A permission deny never matches the built-in grep (its evaluated
// resource is the search pattern), so the credential deny for grep rests on these two halves:
//   before: refuse a grep whose resolved path is a credential file or lies inside a credential directory;
//   after:  remove the blocks of credential files from the text the model receives and recount the header.
// E13_GATE = {"files": [abs path, ...], "dirs": [abs path, ...]}. E13_GATE_LOG gets decisions only,
// never the unfiltered text (the hook's own result.output still holds it).
import { appendFileSync } from "node:fs";
import { resolve, sep } from "node:path";

const GATE = JSON.parse(process.env.E13_GATE || "{}");
const LOG = process.env.E13_GATE_LOG || "/dev/null";
// "content" is the gate; "output" and "metadata" rewrite only that field, so a case can show it does not reach the model.
const MODE = process.env.E13_GATE_MODE || "content";
const FILES = new Set((GATE.files || []).map((p) => resolve(p)));
const DIRS = (GATE.dirs || []).map((p) => resolve(p));

const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\n");

function insideCredentialDir(path) {
  return DIRS.some((dir) => path === dir || path.startsWith(dir + sep));
}

function refused(path) {
  return FILES.has(path) || insideCredentialDir(path);
}

// "Found N matches", then per file a line "<abs path>:" and indented "Line N: ..." lines (a match line carries
// the line's own newline, so blank lines also sit between the lines of one file), then possibly an
// unindented note such as "(Results are truncated ...)". A file's block runs to the next path line or note.
function filterText(text) {
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
  return { text: `Found ${matches} matches\n${kept.join("\n")}`, removed, matches };
}

export default {
  id: "e13-grep-gate",
  setup(ctx) {
    ctx.tool.hook("execute.before", (input) => {
      if (input.tool !== "grep") return;
      const path = resolve(process.cwd(), input.input?.path || ".");
      if (!refused(path)) {
        log({ at: "before", id: input.id, path, refused: false });
        return;
      }
      log({ at: "before", id: input.id, path, refused: true });
      throw new Error("e13 gate: grep of a credential path is refused");
    });
    ctx.tool.hook("execute.after", (input) => {
      if (input.tool !== "grep") return;
      if (MODE !== "content") {
        if (MODE === "output") input.result.output = [];
        if (MODE === "metadata") input.result.metadata = { matches: 0, truncated: false };
        log({ at: "after", id: input.id, mode: MODE });
        return;
      }
      let removed = 0;
      for (const part of input.result?.content || []) {
        if (part?.type !== "text" || typeof part.text !== "string") continue;
        const filtered = filterText(part.text);
        part.text = filtered.text;
        removed += filtered.removed;
      }
      log({ at: "after", id: input.id, removed_blocks: removed, truncated: Boolean(input.result?.metadata?.truncated) });
    });
  },
};
