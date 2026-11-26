"""What the pinned OpenCode binary says about where it looks for a project's own files.

Difficulty removed: probe A6 must try every route by which a project brings code into OpenCode, and the list of
routes comes from the binary under test, not from what this probe's author remembers of the documentation. The
binary is a Bun executable whose bundled source is readable text; this module reads out of it the directory names
its config discovery scans, the config file names it reads, the directory names its plugin loader lists, and
whether any loader reads a ``tool`` directory. A list that cannot be found is ``None``, and the probe then reports
the routes it could not derive as unobserved.
"""
from __future__ import annotations

import re

from opencode_probe import core

# The scan list the plugin was written against (the pinned OpenCode): a binary whose list differs has a route this
# probe's reading of the loader did not cover.
EXPECTED_SCAN_NAMES = (".agents", ".claude", ".opencode")

_IDENT = r"[\w$]+"
_STRINGS = r'"[^"\]]+"(?:,"[^"\]]+")*'
DISCOVER = "ConfigDiscovery.discover"
PLUGIN_DISCOVER = "PluginSourceDirectory.discover"
TOOL_DIRECTORY_GLOB = re.compile(r'\["tools?","tools?"\]|\{tool,tools\}|"tools?/\*')


def binary_text() -> str:
    with open(core.opencode_bin(), "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def _names(listing: str) -> list[str]:
    return re.findall(r'"([^"]+)"', listing)


def context(text: str, pattern: str, before: int = 250, after: int = 350, limit: int = 6) -> list[str]:
    return [text[max(0, m.start() - before):m.end() + after].replace("\n", " ")
            for m in list(re.finditer(pattern, text))[:limit]]


def config_file_names(text: str) -> list[str] | None:
    """The config file names (``opencode.json``, ``opencode.jsonc`` in the pinned binary): the array the discovery scan list
    spreads in after the directory names."""
    names = _scan_declaration(text)
    return names[1] if names else None


def scan_names(text: str) -> list[str] | None:
    """The directory names config discovery looks for in the session directory and every parent (``.claude``,
    ``.agents``, ``.opencode`` in the pinned binary), sorted."""
    names = _scan_declaration(text)
    return sorted(names[0]) if names else None


def _scan_declaration(text: str) -> tuple[list[str], list[str]] | None:
    """``([directory names], [config file names])`` from ``b=[".claude",...,...F.toReversed()]`` inside the
    discovery function and the ``F=[...]`` array that spread names."""
    start = text.find(f'("{DISCOVER}")')
    if start < 0:
        return None
    scan = re.search(rf'=\[({_STRINGS}),\.\.\.({_IDENT})\.toReversed\(\)\]', text[start:start + 6000])
    if not scan:
        return None
    files = re.search(rf'(?<![\w$]){re.escape(scan.group(2))}=\[({_STRINGS})\]', text[max(0, start - 20000):start])
    if not files:
        return None
    return _names(scan.group(1)), _names(files.group(1))


def plugin_directory_names(text: str) -> list[str] | None:
    """The directory names the plugin loader lists inside each scanned directory (``plugin``, ``plugins``)."""
    found = re.search(rf'({_IDENT})=\[({_STRINGS})\],{_IDENT}=u\("{re.escape(PLUGIN_DISCOVER)}"\)', text)
    return _names(found.group(2)) if found else None


def has_tool_directory_glob(text: str) -> bool:
    """True when the binary lists a ``tool`` or ``tools`` directory the way the plugin loader lists ``plugin``."""
    return TOOL_DIRECTORY_GLOB.search(text) is not None


GLOBAL_HOME_DIRECTORY = re.compile(rf'\.join\({_IDENT}\.home,"(\.[\w.-]+)"\)')
GLOBAL_XDG_CONFIG = re.compile(rf'=({_IDENT})\.make\(({_IDENT})\.config\)')
SYSTEM_DIRECTORY = re.compile(rf'return {_IDENT}\.make\("(/etc/[\w./-]+)"\)')
EXPECTED_GLOBALS = {"home_directories": [".agents", ".claude"], "xdg_config": True, "system": "/etc/opencode"}


def global_names(text: str) -> dict | None:
    """The directories config discovery reads whatever the project says: the home-relative ones (``.agents``,
    ``.claude``), whether the XDG config directory is one, and the system directory the binary defaults to on
    Linux. ``None`` when the discovery function is not found."""
    start = text.find(f'("{DISCOVER}")')
    if start < 0:
        return None
    body = text[start:start + 1500]
    system = SYSTEM_DIRECTORY.search(text[max(0, start - 4000):start])
    return {"home_directories": sorted(set(GLOBAL_HOME_DIRECTORY.findall(body))),
            "xdg_config": GLOBAL_XDG_CONFIG.search(body) is not None,
            "system": system.group(1) if system else None}


def route_names() -> dict:
    text = binary_text()
    scans = scan_names(text)
    return {"scan_names": scans, "config_file_names": config_file_names(text),
            "plugin_directory_names": plugin_directory_names(text),
            "tool_directory_glob": has_tool_directory_glob(text),
            "scan_list_expected": scans == sorted(EXPECTED_SCAN_NAMES)}
