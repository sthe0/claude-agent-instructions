"""P8: what a plain session with no integration config loads, and what it writes into its global dirs.

One ``run --standalone`` of the stub, without the probe plugin, inside an isolated root whose
agent home holds byte copies of the repo's instruction sources (``CLAUDE.md``, ``skills/``). Recorded:
whether the model request carried a distinctive line of that ``CLAUDE.md`` or the name of one of its
skills (what the host loads on its own), which skill names the request mentioned, and the files
that appeared under the isolated config, data, state and cache dirs and the home dir (outside the
copied agent home), and which agent-home files the run changed. This is characterization: pass when
the run completed and the listing was read; unobserved when the run did not complete.
"""
import hashlib
import json

from opencode_probe import core, record, runner

CASE_ID = "P8"
K7 = False
SCENARIO = {"models": {"m1": [{"text": "plain"}]}}
LISTED_DIRS = ("xdg/config", "xdg/data", "xdg/state", "xdg/cache", "opencode-config")
LIST_LIMIT = 40


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listing(root, rel):
    base = root / rel
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in base.rglob("*") if p.is_file())


def _changed_agent_home(root):
    agent_home = root / "home" / ".claude-agent"
    return sorted(str(p.relative_to(agent_home)) for p in agent_home.rglob("*")
                  if p.is_file() and (core.REPO_ROOT / p.relative_to(agent_home)).is_file()
                  and _digest(p) != _digest(core.REPO_ROOT / p.relative_to(agent_home)))


def _request_text(run):
    return json.dumps([r.get("messages") for r in run.requests if r.get("tools")])


def run(ctx):
    h = ctx.harness()
    plain = h.run(SCENARIO, label="plain", plugin=False, timeout=60)
    if plain.exit_code != 0:
        return runner.result(record.UNOBSERVED, {"exit_code": plain.exit_code, "stderr": plain.stderr[-300:]})
    root = plain.root
    text = _request_text(plain)
    instructions = (core.REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    first_line = next((line for line in instructions.splitlines() if line.strip()), "")
    skill_names = sorted(p.parent.name for p in (core.REPO_ROOT / "skills").rglob("SKILL.md"))
    listing = {rel: _listing(root, rel) for rel in LISTED_DIRS}
    home_extra = sorted(str(p.relative_to(root)) for p in (root / "home").rglob("*")
                        if p.is_file() and ".claude-agent" not in p.parts)
    evidence = {
        "exit_code": plain.exit_code,
        "agent_home_claude_md_line_in_request": bool(first_line) and json.dumps(first_line)[1:-1] in text,
        "skill_names_mentioned_in_request": [n for n in skill_names if n in text],
        "request_chars": len(text),
        "written": {rel: {"files": len(entries), "sample": entries[:LIST_LIMIT]} for rel, entries in listing.items()},
        "home_files_outside_agent_home": home_extra[:LIST_LIMIT],
        "agent_home_files_changed": _changed_agent_home(root),
    }
    return runner.result(record.PASS, evidence)
