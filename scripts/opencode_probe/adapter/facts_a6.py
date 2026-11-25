"""The runs of A6 that observe how OpenCode itself behaves, with no gate loaded: what its ``grep`` and long
tool output look like to a plugin. The gate adapter's grep filter and session-directory handling copy these
observations (``facts_paths`` holds the path-resolution ones); they are the record of where each came from."""
from __future__ import annotations

import json

from opencode_probe.adapter import shared_a as sa

SUBDIR = "work/sub"
PACKAGE = json.dumps({"name": "adapter-probe", "version": "0.0.0", "type": "module", "main": "index.mjs",
                      "exports": {".": "./index.mjs"}})


def plugin_files(name: str, source: str) -> dict[str, str]:
    text = (sa.ASSETS / source).read_text(encoding="utf-8")
    return {f"{name}/index.mjs": text, f"{name}/package.json": PACKAGE}


def _after_log(run) -> list[dict]:
    entries = []
    for line in (run.read("after.log") or "").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def search_facts(h) -> dict:
    """One session with no gate: a directory with a symlink to a directory and to a file outside it, searched
    by grep for a word only the outside files hold and for a word nothing holds, and a shell call whose output
    is far past any tool-output limit. The record carries what the model-visible result text of each looked
    like to ``tool.execute.after`` and whether the full long output was saved to a file."""
    shell = ("mkdir {root}/scan && mkdir {root}/outside && echo OUTSIDE-WORD > {root}/outside/s.txt"
             " && ln -s {root}/outside {root}/scan/dirlink && ln -s {root}/outside/s.txt {root}/scan/filelink"
             " && echo INSIDE-WORD > {root}/scan/plain.txt")
    long_shell = "seq 1 30000"
    wide = ("mkdir {root}/wide && for i in $(seq 1 100); do printf 'WIDEWORD %s\\n' "
            "$(head -c 3000 /dev/zero | tr '\\0' x) > {root}/wide/f$i.txt; done")
    calls = [{"name": "shell", "arguments": {"command": shell}},
             {"name": "shell", "arguments": {"command": wide}},
             {"name": "grep", "arguments": {"pattern": "WIDEWORD", "path": "{root}/wide"}},
             {"name": "grep", "arguments": {"pattern": "OUTSIDE-WORD", "path": "{root}/scan"}},
             {"name": "grep", "arguments": {"pattern": "INSIDE-WORD", "path": "{root}/scan"}},
             {"name": "grep", "arguments": {"pattern": "NO-SUCH-WORD-ANYWHERE", "path": "{root}/scan"}},
             {"name": "shell", "arguments": {"command": long_shell}}]
    scenario = {"models": {"m1": [*({"tool_calls": [call]} for call in calls), {"text": "done"}]}}
    files = plugin_files("adir", "after_probe.mjs")
    run = h.run(scenario, label="search-facts", config={"plugins": ["file://{root}/adir"]}, files=files,
                env={"AFTERPROBE_LOG": "{root}/after.log"}, cwd=SUBDIR, timeout=240)
    entries = _after_log(run)
    saved = sorted(str(p.relative_to(run.root)) for p in run.root.rglob("*")
                   if p.is_file() and "\n29999\n" in p.read_text(encoding="utf-8", errors="ignore")[:5_000_000]
                   and p.name not in ("after.log", "scenario.json", "requests.jsonl"))
    grep_saved = sorted(str(p.relative_to(run.root)) for p in run.root.rglob("*")
                        if p.is_file() and not str(p.relative_to(run.root)).startswith(("wide/", "stub/"))
                        and p.name not in ("after.log", "scenario.json", "requests.jsonl")
                        and "WIDEWORD" in p.read_text(encoding="utf-8", errors="ignore")[:5_000_000])
    outputs = [{"tool": e.get("tool"), "result_keys": e.get("result_keys"), "output_type": e.get("output_type"),
                "part_keys": e.get("part_keys"), "text_len": e.get("text_len"), "text_head": e.get("text_head"),
                "text_tail": e.get("text_tail")} for e in entries if e.get("at") == "tool.execute.after"]
    return {"exit_code": run.exit_code, "outputs": outputs, "long_output_saved_to": saved[:10],
            "wide_grep_output_saved_to": grep_saved[:10],
            "session_get": [e for e in entries if e.get("at") == "session.get"][:2]}
