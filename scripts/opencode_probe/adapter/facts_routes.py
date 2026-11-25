"""The runs of A6 that observe the project config of a session: which of the routes by which a project brings code
into OpenCode load, with and without ``OPENCODE_DISABLE_PROJECT_CONFIG``.

The routes are derived from the pinned binary (``binary_facts``), not listed by hand: every directory name its
config discovery scans (``.agents``, ``.claude``, ``.opencode`` in the pinned binary) crossed with every directory name its
plugin loader lists (``plugin``, ``plugins``) and the three forms a plugin takes there (flat ``.js``, flat
``.ts``, a directory with its own ``package.json``), and every config file name crossed with every place it is
read (the session directory and inside each scanned directory) and the two keys that name a plugin (``plugin``,
``plugins``). Every member is a file that, when OpenCode loads it, writes a marker, and as a plugin adds a tool, so a
member is seen three ways: its module was evaluated, its plugin was set up, its tool was offered.

Both runs have the same files; the control has no variable, the other has it. A member's state:

* ``off``: loaded in the control, not with the variable;
* ``on``: loaded with the variable set (K7: the gates cannot be secured);
* ``absent``: loaded in neither, and the same file form loaded in the control through another member, and the
  scan list is the one this probe was written against: OpenCode does not read that member's place at all;
* ``unobserved``: loaded in neither, and neither of the above shows it is not read.

The tool-file route (``.opencode/tool``, ``.opencode/tools``) is ``absent`` only when the control offered a plugin
tool (so a tool is seen when it loads), neither run saw a tool file's marker or tool, and the binary's scan list is
the expected one and has no tool-directory glob; one that loaded in the control is ``off`` when the variable stops it.
Skills, agents and commands (prompts, not code) are listed by a plugin loaded from the run's own config and
reported alongside, never gating. The session runs in ``work/sub`` of the isolated root, where a project's own
files sit."""
from __future__ import annotations

import json
import re

from opencode_probe.adapter import binary_facts, facts_a6, shared_a as sa

SUBDIR = facts_a6.SUBDIR
SOURCE = "route_probe.mjs"
MD_SOURCE = "md_probe.mjs"
MD_PLUGIN = "file://{root}/mdir"
PACKAGE = json.dumps({"name": "route-dir", "version": "0.0.0", "type": "module", "main": "index.mjs",
                      "exports": {".": "./index.mjs"}})
SCAN_PACKAGE = json.dumps({"name": "route-project", "version": "0.0.0", "type": "module"})
PLUGIN_FORMS = ("flat-js", "flat-ts", "dir")
CONFIG_KEYS = ("plugin", "plugins")
TOOL_DIRECTORIES = ("tool", "tools")
STATES = ("off", "absent", "unobserved", "on")
SCENARIO = {"models": {"m1": [{"text": "done"}]}}

# (name, kind, path): a skill's name is the one its file says, an agent's and a command's the file's own.
P = f"{SUBDIR}/.opencode"
MARKDOWN = [
    ("rskill-claude", "skill", f"{SUBDIR}/.claude/skills/rskill-claude/SKILL.md"),
    ("rskill-agents", "skill", f"{SUBDIR}/.agents/skills/rskill-agents/SKILL.md"),
    ("rskill-opencode", "skill", f"{P}/skill/rskill-opencode/SKILL.md"),
    ("rskill-opencode-plural", "skill", f"{P}/skills/rskill-opencode-plural/SKILL.md"),
    ("ragent-opencode", "agent", f"{P}/agent/ragent-opencode.md"),
    ("ragent-opencode-plural", "agent", f"{P}/agents/ragent-opencode-plural.md"),
    ("ragent-claude", "agent", f"{SUBDIR}/.claude/agents/ragent-claude.md"),
    ("rcmd-opencode", "command", f"{P}/command/rcmd-opencode.md"),
    ("rcmd-claude", "command", f"{SUBDIR}/.claude/commands/rcmd-claude.md"),
]


def tool_name(member_id: str) -> str:
    return "rt_" + re.sub(r"\W", "_", member_id)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def derive_members(names: dict) -> list[dict]:
    """The plugin-route members of ``names`` (``binary_facts.route_names()``): each has ``id``, ``kind``
    (``plugin-dir`` or ``config-file``), ``form`` (what its state is compared by), ``path`` (the file a loader
    reads, under the run root) and, for a config file member, ``key``, ``target`` (the plugin directory it names)."""
    members = []
    for scan in names["scan_names"] or ():
        for directory in names["plugin_directory_names"] or ():
            for form in PLUGIN_FORMS:
                member_id = f"{scan}/{directory}/{form}"
                leaf = {"flat-js": "route.js", "flat-ts": "route.ts", "dir": "route-dir/index.mjs"}[form]
                members.append({"id": member_id, "kind": "plugin-dir", "form": form,
                                "path": f"{SUBDIR}/{scan}/{directory}/{leaf}"})
    locations = ["", *[f"{scan}/" for scan in names["scan_names"] or ()]]
    for file_name in names["config_file_names"] or ():
        for location in locations:
            for key in CONFIG_KEYS:
                member_id = f"{location}{file_name}#{key}"
                members.append({"id": member_id, "kind": "config-file", "form": f"config-{key}", "key": key,
                                "path": f"{SUBDIR}/{location}{file_name}", "target": f"rdir-{_slug(member_id)}"})
    return members


def tool_file_members() -> list[dict]:
    members = []
    for directory in TOOL_DIRECTORIES:
        for leaf, form in (("route-js.js", "plugin"), ("route-ts.ts", "plugin"), ("route-named.js", "named")):
            members.append({"id": f".opencode/{directory}/{leaf}", "kind": "tool-file", "form": form,
                            "path": f"{P}/{directory}/{leaf}"})
    return members


def _markdown_text(name: str, kind: str) -> str:
    if kind == "skill":
        return f"---\nname: {name}\ndescription: route probe skill {name}\n---\nRoute probe skill.\n"
    if kind == "agent":
        return f"---\ndescription: route probe agent {name}\nmode: subagent\n---\nRoute probe agent.\n"
    return f"---\ndescription: route probe command {name}\n---\nRoute probe command.\n"


def _probe_text(member: dict) -> str:
    source = (sa.ASSETS / SOURCE).read_text(encoding="utf-8")
    text = source.replace("ROUTE_NAME", member["id"]).replace("TOOL_NAME", tool_name(member["id"]))
    return text.replace("export default {", "export const probe = {") if member["form"] == "named" else text


def member_files(plugin_members: list[dict], tool_members: list[dict], names: dict) -> dict[str, str]:
    """The files of the project, by path under the run root: one probe module per member, the config files that
    name a plugin directory, a ``package.json`` in each scanned directory, and the prompt files."""
    files = {}
    configs: dict[str, dict] = {}
    for member in plugin_members:
        if member["kind"] == "plugin-dir":
            files[member["path"]] = _probe_text(member)
            if member["form"] == "dir":
                files[member["path"].rsplit("/", 1)[0] + "/package.json"] = PACKAGE
        else:
            files[f"{member['target']}/index.mjs"] = _probe_text(member)
            files[f"{member['target']}/package.json"] = PACKAGE
            configs.setdefault(member["path"], {}).setdefault(member["key"], []).append(
                f"file://{{root}}/{member['target']}")
    for path, table in configs.items():
        files[path] = json.dumps(table)
    for member in tool_members:
        files[member["path"]] = _probe_text(member)
    for scan in names["scan_names"] or ():
        files[f"{SUBDIR}/{scan}/package.json"] = SCAN_PACKAGE
    for name, kind, path in MARKDOWN:
        files[path] = _markdown_text(name, kind)
    files["mdir/index.mjs"] = (sa.ASSETS / MD_SOURCE).read_text(encoding="utf-8")
    files["mdir/package.json"] = PACKAGE
    return files


def _log_entries(run, name: str) -> list[dict]:
    entries = []
    for line in (run.read(name) or "").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _seen(run) -> dict[str, dict[str, bool]]:
    seen: dict[str, dict[str, bool]] = {}
    for entry in _log_entries(run, "routes.log"):
        seen.setdefault(str(entry.get("route")), {})[str(entry.get("at"))] = True
    return seen


def _listed(run) -> dict[str, list[str] | str]:
    lists = {}
    for entry in _log_entries(run, "md.log"):
        if entry.get("at") == "list":
            lists[str(entry.get("area"))] = entry.get("names", entry.get("error", f"present={entry.get('present')}"))
    return lists


def _observed(run, members: list[dict]) -> dict:
    offered = run.tool_list()
    seen = _seen(run)
    listed = _listed(run)
    descriptions = {"skill": run.tool_description("skill"), "agent": run.tool_description("subagent")}
    return {
        "exit": run.exit_code,
        "members": {m["id"]: {"module": bool(seen.get(m["id"], {}).get("module")),
                              "setup": bool(seen.get(m["id"], {}).get("setup")),
                              "tool_offered": tool_name(m["id"]) in offered} for m in members},
        "markdown": {name: {"listed": name in (listed.get(kind) or []),
                            "in_description": name in descriptions.get(kind, "")}
                     for name, kind, _ in MARKDOWN},
        "plugin_tools_offered": sorted(t for t in offered if t.startswith("rt_")),
    }


def loaded(observation: dict, member_id: str) -> bool:
    seen = observation["members"][member_id]
    return seen["module"] or seen["setup"] or seen["tool_offered"]


def plugin_member_states(members: list[dict], control: dict, off: dict, *, scan_list_ok: bool) -> dict[str, str]:
    """``off`` / ``absent`` / ``unobserved`` / ``on`` per member (the module docstring has the rules)."""
    states: dict[str, str] = {}
    loaded_forms = {m["form"] for m in members if loaded(control, m["id"])}
    for member in members:
        in_control, with_variable = loaded(control, member["id"]), loaded(off, member["id"])
        if with_variable:
            states[member["id"]] = "on"
        elif in_control:
            states[member["id"]] = "off"
        elif member["form"] in loaded_forms and scan_list_ok:
            states[member["id"]] = "absent"
        else:
            states[member["id"]] = "unobserved"
    return states


def tool_file_state(tool_members: list[dict], control: dict, off: dict, *, plugin_tool_offered_control: bool,
                    scan_list_ok: bool, tool_directory_glob: bool) -> str:
    if any(loaded(off, m["id"]) for m in tool_members):
        return "on"
    if any(loaded(control, m["id"]) for m in tool_members):
        return "off"
    if plugin_tool_offered_control and scan_list_ok and not tool_directory_glob:
        return "absent"
    return "unobserved"


def project_config_off(states: dict[str, str], tool_state: str) -> bool:
    return bool(states) and all(s in ("off", "absent") for s in states.values()) and tool_state in ("off", "absent")


def project_routes(h, off_env: dict[str, str], names: dict | None = None) -> dict:
    """Two runs of one session in ``work/sub`` with the same project files: the control without the variable, and
    the run with ``off_env``. The record carries the derived names, every member with what each run loaded and
    its state, the tool-file route's state, and ``project_config_off`` (every member ``off`` or ``absent``, the
    tool-file route ``off`` or ``absent``). ``any_on`` is the K7 fact."""
    names = names or binary_facts.route_names()
    members = derive_members(names)
    tool_members = tool_file_members()
    files = member_files(members, tool_members, names)
    env = {"ROUTEPROBE_LOG": "{root}/routes.log", "MDPROBE_LOG": "{root}/md.log"}
    config = {"plugins": [sa.PROBE_PLUGIN, MD_PLUGIN]}
    control_run = h.run(SCENARIO, label="routes-control", config=config, files=files, env=env, cwd=SUBDIR,
                        timeout=180)
    off_run = h.run(SCENARIO, label="routes-off", config=config, files=files, env={**env, **off_env}, cwd=SUBDIR,
                    timeout=180)
    everything = members + tool_members
    control, off = _observed(control_run, everything), _observed(off_run, everything)
    scan_list_ok = bool(names["scan_names"]) and names["scan_list_expected"]
    offered_control = any(loaded(control, m["id"]) and control["members"][m["id"]]["tool_offered"] for m in members)
    states = plugin_member_states(members, control, off, scan_list_ok=scan_list_ok)
    tool_state = tool_file_state(tool_members, control, off, plugin_tool_offered_control=offered_control,
                                 scan_list_ok=scan_list_ok, tool_directory_glob=bool(names["tool_directory_glob"]))
    return {
        "variable": dict(off_env), "control_exit": control["exit"], "off_exit": off["exit"],
        "scan_names": names["scan_names"], "config_file_names": names["config_file_names"],
        "plugin_directory_names": names["plugin_directory_names"], "scan_list_expected": scan_list_ok,
        "tool_directory_glob": bool(names["tool_directory_glob"]),
        "plugin_tool_offered_control": offered_control,
        "project_routes": {m["id"]: {"kind": m["kind"], "form": m["form"], "path": m["path"],
                                     "control": loaded(control, m["id"]), "with_variable": loaded(off, m["id"]),
                                     "state": states[m["id"]]} for m in members},
        "tool_file_route": {"state": tool_state,
                            "members": {m["id"]: {"control": loaded(control, m["id"]),
                                                  "with_variable": loaded(off, m["id"])} for m in tool_members}},
        "any_on": "on" in states.values() or tool_state == "on",
        "project_config_off": project_config_off(states, tool_state),
        "markdown": {name: {"kind": kind, "control": control["markdown"][name], "off": off["markdown"][name]}
                     for name, kind, _ in MARKDOWN},
        "offered_tools_control": control["plugin_tools_offered"], "offered_tools_off": off["plugin_tools_offered"],
    }
