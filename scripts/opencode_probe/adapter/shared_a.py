"""What the A1, A2 and A4 cases share: the shim's files (shipped or the harness's inert copy), the
environment that points the shim at the bridge, and the gate log the shim writes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from opencode_probe import core

REPO = core.REPO_ROOT
SHIM_DIR = REPO / "integrations" / "opencode" / "plugin"
BRIDGE = REPO / "scripts" / "opencode-gate-bridge.py"
ASSETS = Path(__file__).resolve().parent / "assets"
DENY_ALL = ASSETS / "deny_all.py"
DENY_TOUCH = ASSETS / "deny_touch.py"
STOP_BLOCK_ONCE = ASSETS / "stop_block_once.py"

REFUSE_MARK = "// gate:refuse"
HOLD_MARK = "// gate:hold"
GATES_DIR = "gdir"
GATES_PLUGIN = "file://{root}/" + GATES_DIR
PROBE_PLUGIN = "file://{root}/pdir"
DENY_REASON = "adapter-probe-deny"


def shim_files(*, inert: bool) -> dict[str, str]:
    """The plugin directory's files by name. The inert copy keeps every line of the shipped shim
    except the two marked ones: the refusal returns instead of throwing and the turn-end hold
    does not continue the session."""
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(SHIM_DIR.iterdir()) if p.is_file()}
    if not inert:
        return files
    lines = []
    for line in files["agent-gates.ts"].split("\n"):
        indent = line[:len(line) - len(line.lstrip())]
        if line.rstrip().endswith(REFUSE_MARK):
            line = f"{indent}return message; {REFUSE_MARK}"
        elif line.rstrip().endswith(HOLD_MARK):
            line = f"{indent}void 0; {HOLD_MARK}"
        lines.append(line)
    files["agent-gates.ts"] = "\n".join(lines)
    return files


def stop_rows_not_exempt(table: dict) -> list[str]:
    """Scripts of the table's Stop rows that lack an exemption with both a reason and a covers_gap."""
    missing = []
    for row in table.get("row", []):
        exempt = row.get("exempt") or {}
        if row.get("event") == "Stop" and not (str(exempt.get("reason", "")).strip()
                                               and str(exempt.get("covers_gap", "")).strip()):
            missing.append(row["script"])
    return missing


def gate_files(*, inert: bool) -> dict[str, str]:
    """``files=`` entries of a harness run that place the shim under the run root."""
    return {f"{GATES_DIR}/{name}": text for name, text in shim_files(inert=inert).items()}


def extra_rows(rows: list[dict], plugin_tools: tuple[str, ...] = ("probe_tool",)) -> str:
    """The table-shaped file ``AGENT_GATE_EXTRA_ROWS`` names: the rows, and the plugin tool ids the session
    declares (the probe plugin's ``probe_tool``)."""
    out = ["[plugin_tools]", f"ids = {json.dumps(list(plugin_tools))}", ""]
    for row in rows:
        out.append("[[row]]")
        for key, value in row.items():
            out.append(f"{key} = {json.dumps(value)}")
        out.append("")
    return "\n".join(out)


def gate_env(**more: str) -> dict[str, str]:
    """The environment that makes the shim reach the bridge and write its log under the run root; it carries the
    variable that switches the project's own OpenCode config off, as every launcher's does."""
    from lib import opencode_events as oe

    env = {**oe.PROJECT_CONFIG_OFF, "AGENT_GATE_BRIDGE": str(BRIDGE), "AGENT_GATE_PYTHON": sys.executable,
           "AGENT_GATE_LOG": "{root}/gates.log", "AGENT_GATE_EXTRA_ROWS": "{root}/extra-rows.toml",
           "AGENT_OPENCODE_HOME": "{root}/opencode-home", "AGENT_GATE_SETUP_MARKER": "{root}/setup.marker"}
    env.update(more)
    return env


def gate_config(extra: dict | None = None) -> dict:
    """Both plugins are listed because a list in the run's config replaces the harness's own."""
    return {**(extra or {}), "plugins": [PROBE_PLUGIN, GATES_PLUGIN]}


def gate_log(run) -> list[dict]:
    return parse_gate_log(run.read("gates.log") or "")


def parse_gate_log(text: str) -> list[dict]:
    entries = []
    for line in text.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append({"at": "unparsed", "raw": line[:200]})
    return entries


def refusal_logged(entries: list[dict], *, call_id: str | None = None, tool_prefix: str | None = None,
                   source: str | None = None) -> bool:
    """A refusal of the call is in the log. With ``source`` (``deny`` or ``refused``) only a refusal whose
    reason begins ``<source>:`` counts: a hook's deny reads ``deny: <script> ...`` and a refusal the bridge or
    shim makes itself reads ``refused: <class> ...``, and a case that rests on a denying row counts the first."""
    for entry in entries:
        if entry.get("at") != "refusal":
            continue
        if source is not None and not str(entry.get("detail", "")).startswith(f"{source}: "):
            continue
        if call_id is not None and entry.get("id") == call_id:
            return True
        if tool_prefix is not None and str(entry.get("tool", "")).startswith(tool_prefix):
            return True
    return False
