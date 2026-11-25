"""The runs of A6 that observe how OpenCode itself behaves, with no gate loaded: what its ``patch`` tool does
with a patch text, and what its ``grep`` and long tool output look like to a plugin. The gate adapter's
patch reader, grep filter and session-directory handling copy these observations; they are the record of
where each came from."""
from __future__ import annotations

import json

from opencode_probe.adapter import shared_a as sa

PATCH_MODEL = "gpt-5"
SUBDIR = "work/sub"
PACKAGE = json.dumps({"name": "adapter-probe", "version": "0.0.0", "type": "module", "main": "index.mjs",
                      "exports": {".": "./index.mjs"}})
BEGIN, END = "*** Begin Patch", "*** End Patch"

SEEDS = {
    "upd-exact": "a\nb\nc\n", "upd-rstrip": "x  \ny\n", "upd-strip": "  x\ny\n", "upd-noeol": "a\nb",
    "upd-two": "1\n2\n3\n4\n5\n", "upd-repeat": "k\nk\nk\n",
    "upd-header": "def f():\n  x\ndef g():\n  x\n", "upd-eof": "a\nb\na\nb\n", "mv-src": "a\nb\n",
    "del-me": "bye\n", "upd-missing": "a\nb\n", "upd-crlf": "a\r\nb\r\n", "upd-ctxless": "p\nq\n",
    "upd-blank": "a\n\nb\nc\n",
}

# (label, patch body); every path is absolute except the relative ones, which are relative to the session
PATCHES = [
    ("indent-add", "  *** Add File: {root}/ind-add\n+x"),
    ("tab-add", "\t*** Add File: {root}/tab-add\n+x"),
    ("trailing-add", "*** Add File: {root}/trail-add \n+x"),
    ("add-body", "*** Add File: {root}/add-body\n+x\n+y"),
    ("relative-add", "*** Add File: rel-add\n+x"),
    ("upd-exact", "*** Update File: {root}/upd-exact\n@@\n a\n-b\n+B\n c"),
    ("upd-rstrip", "*** Update File: {root}/upd-rstrip\n@@\n x\n-y\n+Y"),
    ("upd-strip", "*** Update File: {root}/upd-strip\n@@\n x\n-y\n+Y"),
    ("upd-noeol", "*** Update File: {root}/upd-noeol\n@@\n a\n-b\n+B"),
    ("upd-two", "*** Update File: {root}/upd-two\n@@\n 1\n-2\n+two\n@@\n 4\n-5\n+five"),
    ("upd-repeat", "*** Update File: {root}/upd-repeat\n@@\n k\n-k\n+K"),
    ("upd-header", "*** Update File: {root}/upd-header\n@@ def g():\n-  x\n+  y"),
    ("upd-eof", "*** Update File: {root}/upd-eof\n@@\n a\n-b\n+B\n*** End of File"),
    ("upd-ctxless", "*** Update File: {root}/upd-ctxless\n@@\n-p\n+P"),
    ("upd-crlf", "*** Update File: {root}/upd-crlf\n@@\n a\n-b\n+B"),
    ("move", "*** Update File: {root}/mv-src\n*** Move to: {root}/mv-dst\n@@\n a\n-b\n+B"),
    ("delete", "*** Delete File: {root}/del-me"),
    ("upd-missing", "*** Update File: {root}/upd-missing\n@@\n zzz\n-b\n+B"),
    ("upd-blank", "*** Update File: {root}/upd-blank\n@@\n a\n\n-b\n+B"),
]
OUTPUT_FILES = ("ind-add", "tab-add", "trail-add", "trail-add ", "add-body", "upd-exact", "upd-rstrip",
                "upd-strip", "upd-noeol", "upd-two", "upd-repeat", "upd-header", "upd-eof", "upd-ctxless",
                "upd-crlf", "mv-src", "mv-dst", "del-me", "upd-missing", "upd-blank", "work/sub/rel-add",
                "rel-add")


def _plugin(name: str, source: str) -> dict[str, str]:
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


def _bytes(run, rel: str):
    path = run.root / rel
    return path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else None


def patch_facts(h) -> dict:
    """One session of the gpt family applying each patch text of PATCHES in turn, with no gate loaded: the
    bytes each file holds afterwards (None: absent), which is what the gate adapter's reader must reproduce."""
    turns = [{"tool_calls": [{"name": "patch", "arguments": {"patchText": f"{BEGIN}\n{body}\n{END}"}}]}
             for _, body in PATCHES]
    scenario = {"models": {PATCH_MODEL: [*turns, {"text": "done"}]}}
    config = {"plugins": [], "providers": {"stub": {"models": {PATCH_MODEL: {"name": PATCH_MODEL}}}}}
    run = h.run(scenario, label="patch-facts", config=config, files=SEEDS, env={}, model=f"stub/{PATCH_MODEL}",
                cwd=SUBDIR, timeout=240)
    return {"exit_code": run.exit_code, "labels": [label for label, _ in PATCHES],
            "files": {name: _bytes(run, name) for name in OUTPUT_FILES}}


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
    files = _plugin("adir", "after_probe.mjs")
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
