"""The runs of A6 that observe the GLOBAL plugin locations: the directories outside the project from which OpenCode
loads plugin code in a session started the way the adapter starts one (``OPENCODE_DISABLE_PROJECT_CONFIG`` set).

Difficulty removed: a session that may write a plugin file into a directory OpenCode loads code from can bring its
own code into the next session. The project routes are switched off by the variable; the directories read from
the user's own configuration are not necessarily, so the gate must protect each one that loads.

The locations come from the pinned binary (``binary_facts.global_names``): the system directory, the XDG config
directory, and the home-relative directories its config discovery lists, plus two this probe adds because a
documentation or an older reading names them (``$HOME/.config/opencode`` beside ``$XDG_CONFIG_HOME``, and the
directory above the binary's own ``bin``). Each is crossed with the directory names the plugin loader lists
(``plugin``, ``plugins``) and the three forms a plugin takes there. Everything is placed inside the run root: the
run's ``HOME`` and ``XDG_CONFIG_HOME`` point there, and the binary is a copy at ``<root>/bin/opencode``, so no
real global directory is read or written. The system directory (``/etc/opencode``) cannot be placed there and is
always ``unobserved``.

A location's state: ``on`` (a member loaded with the variable set), ``off`` (loaded in the control only),
``absent`` (loaded in neither while the same form loaded in the control through another location, and the
binary's list is the expected one), ``unobserved`` (anything else). The gate protects every location that is not
``absent`` (``lib/opencode_events.py``, ``global_config_locations``).
"""
from __future__ import annotations

from opencode_probe.adapter import binary_facts, facts_a6, facts_routes, shared_a as sa

SUBDIR = facts_a6.SUBDIR
SYSTEM = "system"
CONFIG_DIR_ENV = "OPENCODE_CONFIG_DIR"

# (id, path under the run root). ``root`` is the directory above ``bin/opencode``.
PROBED_LOCATIONS = (
    ("xdg-config", "xdg/config/opencode"),
    ("home-.config/opencode", "home/.config/opencode"),
    ("home-.claude", "home/.claude"),
    ("home-.agents", "home/.agents"),
    ("above-binary", ""),
)


def locations(names: dict | None) -> list[dict]:
    """The candidate locations: ``id``, ``path`` under the run root (``None`` for the system directory, which cannot
    be placed there), and ``derived`` (named by the binary, rather than added by this probe)."""
    derived = names or {}
    home_names = derived.get("home_directories") or []
    found = []
    for location_id, path in PROBED_LOCATIONS:
        from_binary = (location_id == "xdg-config" and bool(derived.get("xdg_config"))
                       or location_id.startswith("home-.") and location_id[len("home-"):] in home_names)
        found.append({"id": location_id, "path": path, "derived": from_binary})
    found.append({"id": SYSTEM, "path": None, "derived": bool(derived.get("system")),
                  "directory": derived.get("system")})
    return found


def derive_members(names: dict | None, plugin_directories: list[str] | None) -> list[dict]:
    members = []
    for location in locations(names):
        if location["path"] is None:
            continue
        for directory in plugin_directories or ():
            for form in facts_routes.PLUGIN_FORMS:
                leaf = {"flat-js": "route.js", "flat-ts": "route.ts", "dir": "route-dir/index.mjs"}[form]
                relative = "/".join(part for part in (location["path"], directory, leaf) if part)
                members.append({"id": f"{location['id']}/{directory}/{form}", "kind": "plugin-dir", "form": form,
                                "location": location["id"], "base": location["path"], "path": relative})
    return members


def member_files(members: list[dict]) -> dict[str, str]:
    """One probe module per member, and a ``package.json`` in each location directory: the project routes need
    one in each scanned directory to load a plugin, so a location is probed the same way."""
    files = {}
    for member in members:
        files["/".join(part for part in (member["base"], "package.json") if part)] = facts_routes.SCAN_PACKAGE
        files[member["path"]] = facts_routes._probe_text(member)
        if member["form"] == "dir":
            files[member["path"].rsplit("/", 1)[0] + "/package.json"] = facts_routes.PACKAGE
    return files


def location_states(candidates: list[dict], members: list[dict], control: dict, off: dict, *, list_ok: bool,
                    exits_ok: bool) -> dict[str, dict]:
    """Per location: what its members loaded in each run and its state. ``exits_ok`` (both runs exited 0) and a
    positive control (some member loaded in the control run) are the conditions without which ``absent`` and ``off``
    cannot be claimed."""
    member_state = facts_routes.plugin_member_states(members, control, off, scan_list_ok=list_ok and exits_ok)
    positive_control = any(facts_routes.loaded(control, m["id"]) for m in members)
    result = {}
    for candidate in candidates:
        own = [m for m in members if m["location"] == candidate["id"]]
        states = {member_state[m["id"]] for m in own}
        if candidate["path"] is None or not exits_ok or not positive_control:
            state = "unobserved"
        elif "on" in states:
            state = "on"
        elif "unobserved" in states:
            state = "unobserved"
        elif "off" in states:
            state = "off"
        else:
            state = "absent"
        result[candidate["id"]] = {
            "path": candidate["path"], "derived": candidate["derived"], "state": state,
            "control": any(facts_routes.loaded(control, m["id"]) for m in own),
            "with_variable": any(facts_routes.loaded(off, m["id"]) for m in own)}
    return result


def global_plugin_dirs(h, off_env: dict[str, str], names: dict | None = None,
                       plugin_directories: list[str] | None = None) -> dict:
    """Three runs of one session with the same files placed in every probed location: the control without the
    variable, and the run with ``off_env``, both with ``OPENCODE_CONFIG_DIR`` emptied (the harness sets it, and a
    set one makes the binary skip the XDG directory), and a third with ``off_env`` and the harness's config
    directory, recorded as ``loaded_with_config_dir``. The record carries the names derived from the binary, each
    location's state, and the exit codes of the runs."""
    if names is None:
        names = binary_facts.global_names(binary_facts.binary_text())
    if plugin_directories is None:
        plugin_directories = binary_facts.plugin_directory_names(binary_facts.binary_text())
    candidates = locations(names)
    members = derive_members(names, plugin_directories)
    files = member_files(members)
    env = {"ROUTEPROBE_LOG": "{root}/routes.log", CONFIG_DIR_ENV: ""}
    config = {"plugins": [sa.PROBE_PLUGIN]}
    control_run = h.run(facts_routes.SCENARIO, label="globals-control", config=config, files=files, env=env,
                        cwd=SUBDIR, timeout=180)
    off_run = h.run(facts_routes.SCENARIO, label="globals-off", config=config, files=files, env={**env, **off_env},
                    cwd=SUBDIR, timeout=180)
    with_config_dir = h.run(facts_routes.SCENARIO, label="globals-off-config-dir", config=config, files=files,
                            env={"ROUTEPROBE_LOG": "{root}/routes.log", **off_env}, cwd=SUBDIR, timeout=180)
    control, off = facts_routes._observed(control_run, members), facts_routes._observed(off_run, members)
    with_dir = facts_routes._observed(with_config_dir, members)
    list_ok = bool(names) and all(names.get(key) == value for key, value in binary_facts.EXPECTED_GLOBALS.items())
    exits_ok = control["exit"] == 0 and off["exit"] == 0
    states = location_states(candidates, members, control, off, list_ok=list_ok, exits_ok=exits_ok)
    return {"names": names, "plugin_directories": plugin_directories, "list_expected": list_ok,
            "control_exit": control["exit"], "off_exit": off["exit"], "variable": dict(off_env),
            "locations": states,
            "loaded_with_variable": sorted(k for k, v in states.items() if v["state"] == "on"),
            "with_config_dir_exit": with_dir["exit"],
            "loaded_with_config_dir": sorted(m["location"] for m in members if facts_routes.loaded(with_dir, m["id"])),
            "unobserved": sorted(k for k, v in states.items() if v["state"] == "unobserved"),
            "tools_offered_control": control["plugin_tools_offered"], "tools_offered_off": off["plugin_tools_offered"]}
