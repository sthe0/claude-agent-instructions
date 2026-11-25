"""The runs of A6 that observe how OpenCode itself resolves a path argument, with no gate loaded: which file
a ``write`` lands on, which a ``read`` returns, which directory a ``grep`` / ``glob`` search and in which a
``shell`` call runs, for each written form of a path (``~``, ``~/x``, ``~user/x``, ``//x``, ``..``, empty,
environment variables, ``file://``, a redundant ``./``). The gate adapter's resolver reproduces exactly the
forms recorded here and refuses every other; this is the record of where each came from.

The session runs in ``work/sub`` of the isolated root and HOME is ``home`` of it, so a form that expands to
the home directory, one that is taken relative to the session and one that goes up a level land in three
different places inside the root, and nothing is written outside it."""
from __future__ import annotations

import json

from opencode_probe.adapter import facts_a6, shared_a as sa

SUBDIR = facts_a6.SUBDIR
HOME_MARK, LITERAL_MARK, PLAIN_MARK = "HOME-SEED", "LITERAL-SEED", "PLAIN-SEED"

SEEDS = {
    "home/pf-seed": HOME_MARK,
    "work/sub/~/pf-seed": LITERAL_MARK,
    "work/sub/~nobody/pf-seed": LITERAL_MARK,
    "work/sub/~pf-nouser/pf-seed": LITERAL_MARK,
    "work/sub/$HOME/pf-seed": LITERAL_MARK,
    "work/sub/${HOME}/pf-seed": LITERAL_MARK,
    "work/sub/pf-seed": PLAIN_MARK,
    "work/pf-seed": "UP-SEED",
    "work/sub/sub2/pf-seed": PLAIN_MARK,
}

# (label, written form); a form that begins with ``{root}`` is an absolute one under the isolated root
WRITE_FORMS = [
    ("plain", "pf-w-plain"), ("dot-slash", "./pf-w-dotslash"), ("inner-dot", "sub2/./pf-w-innerdot"),
    ("dotdot", "../pf-w-dotdot"), ("dotdot-inside", "sub2/../pf-w-inside"), ("absolute", "{root}/pf-w-abs"),
    ("tilde-slash", "~/pf-w-tilde"), ("dollar-home", "$HOME/pf-w-envhome"),
    ("braced-home", "${HOME}/pf-w-envbraced"), ("file-url", "file://{root}/pf-w-fileurl"),
    ("double-slash-inside", "{root}//pf-w-dblinside"), ("backslash", "sub2\\pf-w-backslash"),
]
READ_FORMS = [
    ("tilde", "~"), ("tilde-slash", "~/"), ("tilde-file", "~/pf-seed"), ("tilde-user", "~nobody/pf-seed"),
    ("tilde-nouser", "~pf-nouser/pf-seed"), ("dollar-home", "$HOME/pf-seed"), ("braced-home", "${HOME}/pf-seed"),
    ("plain", "pf-seed"), ("dotdot", "../pf-seed"), ("double-slash", "//etc/hostname"),
    ("single-slash", "/etc/hostname"), ("empty", ""), ("file-url", "file://{root}/home/pf-seed"),
    ("tilde-tilde", "~/~/pf-seed"),
]
SEARCH_FORMS = [("tilde", "~"), ("tilde-slash", "~/"), ("dotdot", ".."), ("plain", "sub2"), ("empty", ""),
                ("dollar-home", "$HOME"), ("tilde-user", "~nobody")]
WORKDIR_FORMS = [("tilde", "~"), ("tilde-slash", "~/"), ("dotdot", ".."), ("plain", "sub2"), ("empty", ""),
                 ("dollar-home", "$HOME"), ("tilde-user", "~nobody"), ("absolute", "{root}/work"),
                 ("file-url", "file://{root}/work"), ("double-slash", "//tmp")]


def _calls() -> list[tuple[str, str, dict]]:
    calls = []
    for label, form in WRITE_FORMS:
        calls.append((f"write:{label}", "write", {"path": form, "content": f"W-{label}"}))
    for label, form in READ_FORMS:
        calls.append((f"read:{label}", "read", {"path": form}))
    for label, form in SEARCH_FORMS:
        calls.append((f"grep:{label}", "grep", {"pattern": "-SEED", "path": form}))
        calls.append((f"glob:{label}", "glob", {"pattern": "pf-seed", "path": form}))
    for label, form in WORKDIR_FORMS:
        calls.append((f"shell:{label}", "shell", {"command": f"pwd > {{root}}/wd-{label}.txt", "workdir": form}))
    calls.append(("shell:none", "shell", {"command": "pwd > {root}/wd-none.txt"}))
    return calls


def _entries(run) -> list[dict]:
    out = []
    for line in (run.read("forms.log") or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def path_forms(h) -> dict:
    """One session with no gate that makes each call of ``_calls`` in turn. The record carries, per call, the
    argument OpenCode handed the before-hook (is it the written form, or already resolved), the head of the
    text the call produced (None: the call produced none, it failed), and, afterwards, which files under
    the root hold the content each write carried and the directory each shell call reported."""
    calls = _calls()
    scenario = {"models": {"m1": [*({"tool_calls": [{"name": name, "arguments": args}]} for _, name, args in calls),
                                  {"text": "done"}]}}
    files = {**SEEDS, **facts_a6.plugin_files("fdir", "forms_probe.mjs")}
    run = h.run(scenario, label="path-forms", config={"plugins": ["file://{root}/fdir"]}, files=files,
                env={"FORMSPROBE_LOG": "{root}/forms.log"}, cwd=SUBDIR, timeout=300)
    entries = _entries(run)
    before = [e for e in entries if e.get("at") == "before"]
    after = {e.get("id"): e for e in entries if e.get("at") == "after"}
    root = run.root
    per_call = {}
    for (label, name, _), entry in zip(calls, before):
        seen = after.get(entry.get("id"))
        per_call[label] = {"before_args": entry.get("args"),
                           "after_text_head": None if seen is None else seen.get("text_head")}
    landed = {}
    for path in sorted(p for p in root.rglob("*pf-w-*") if p.is_file()):
        landed[str(path.relative_to(root))] = path.read_text(encoding="utf-8", errors="replace")[:60]
    workdirs = {}
    for path in sorted(root.glob("wd-*.txt")):
        workdirs[path.stem[3:]] = path.read_text(encoding="utf-8", errors="replace").strip()
    return {"exit_code": run.exit_code, "session_directory": SUBDIR, "home": "home", "calls": per_call,
            "written_files": landed, "shell_directories": workdirs, "before_count": len(before),
            "call_count": len(calls)}
