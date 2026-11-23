"""Per-layer hook parity: which wired hook can refuse a tool call, and which OpenCode mechanism carries it.

Difficulty removed: on a second host a gate is lost silently. Claude Code wires a hook by
(event, matcher, script); OpenCode has plugin events instead, and nothing said which of the 44
wired hooks can refuse a tool call, which OpenCode mechanism takes each one over, or on which
execution paths that mechanism was seen to refuse. A hand-written list of the important gates
leaves out its siblings of the same form, so the table here is keyed to the layer's own wiring
(Core: the installer's ``DESIRED`` list; another layer: the settings file its table names) and a
test pins the two equal.

A row's class is computed, never declared: a hook is *denying* when its event can refuse a tool
call (``EVENT_CAN_REFUSE``). Each row names the OpenCode mechanism that carries it (``opencode``),
the probe path ids it covers (``paths``) and the difficulty it removes (``difficulty``). The
table header copies, from the passing mechanisms record that ``pin.toml`` names, every probe
path with the mechanisms that refused on it and that fired on it, and every mechanism with
``fired`` / ``can_refuse``. The record names mechanisms by Claude-style labels; ``LABEL_OF`` is
the one map between those labels and the OpenCode names a row uses.

Plain checks (always): rows equal the layer's wiring, ``difficulty`` is non-empty, a mechanism
is a known one, a listed path is a ``probe_paths`` id, an ``exempt`` entry carries ``reason`` and
``covers_gap`` and sits on a non-denying row. Strict checks add S1-S4, all gated by
``STRICT_DENYING_ROW_RULES``:

- S1 a denying row that is exempt or mapped to none;
- S2 a denying row whose ``paths`` miss a ``probe_paths`` id, unless ``gaps`` lists that id as
  ``switched_off:<setting>`` (``child-run`` never passes; ``K7`` never passes), and every path it
  claims must have the row's mechanism in its ``refused_by``;
- S3 a row mapped to a mechanism that is not in ``probe_mechanisms`` with ``fired`` true (a
  denying row also ``can_refuse``); an exempt non-denying row is skipped;
- S4 a non-denying row mapped to none, or a PostToolUse row whose ``paths`` miss an id the probe
  executes (every id except ``child-run``) or list one whose ``fired_by`` lacks its mechanism,
  unless it is exempt.

``start_notice`` pins every layer's table against its own wiring at session start.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import config_root, hook_wiring  # noqa: E402

STRICT_DENYING_ROW_RULES = True

EVENT_CAN_REFUSE = frozenset({"PreToolUse", "PermissionRequest"})

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install-reminder-hooks.sh"
CORE_TABLE = REPO_ROOT / "integrations" / "opencode" / "parity.toml"
TABLE_NAME = "hook-parity.toml"
SETTINGS_NAMES = ("settings.json", "settings.local.json")

NO_MECHANISM = "none"
CHILD_RUN = "child-run"
SWITCHED_OFF = "switched_off:"

# OpenCode mechanism -> the label the probe record gives it (the plugin's registrations fix it).
LABEL_OF = {
    "tool.execute.before": "PreToolUse",
    "tool.execute.after": "PostToolUse",
    "session.prompt": "UserPromptSubmit",
    "session.created": "SessionStart",
    "session.deleted": "SessionEnd",
    "session.execution.succeeded": "SessionEnd",
    "experimental.idle": "Stop",
    "child-session idle": "SubagentStop",
    "permission.evaluate": "permission.evaluate",
}
MECHANISMS = frozenset(LABEL_OF) | {NO_MECHANISM}

REQUIRED_ROW_FIELDS = ("script", "event", "difficulty", "opencode")


class Finding(NamedTuple):
    rule: str
    script: str
    event: str
    matcher: str
    message: str

    def line(self) -> str:
        return f"{self.rule} {self.script} ({self.event} {self.matcher or '-'}): {self.message}"


def is_denying(event: str) -> bool:
    return event in EVENT_CAN_REFUSE


def row_key(row: dict) -> tuple:
    return (row["event"], row.get("matcher") or None, row["script"], row.get("args") or "")


def split_command(command: str) -> tuple[str, str]:
    """(script basename, remaining arguments) of a hook command."""
    path = hook_wiring.script_path(command)
    tokens = command.split()
    rest = tokens[tokens.index(path) + 1:] if path in tokens else []
    return os.path.basename(path), " ".join(rest)


def installer_rows(installer: Path = INSTALLER) -> list[dict]:
    """The rows the installer wires, read as test_telemetry_registry reads them: the literal."""
    text = installer.read_text(encoding="utf-8")
    start = text.index("DESIRED = [")
    desired = ast.literal_eval(text[start + len("DESIRED = "):text.index("\n]\n", start) + 2])
    rows = []
    for event, matcher, command, timeout in desired:
        script, args = split_command(command)
        rows.append({"event": event, "matcher": matcher, "script": script, "args": args,
                     "timeout_s": timeout})
    return rows


def settings_rows(settings: Path) -> list[dict]:
    """The command hooks a settings file wires (none when the file is absent)."""
    if not settings.is_file():
        return []
    hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks") or {}
    rows = []
    for event, groups in hooks.items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command") or ""
                if hook.get("type", "command") != "command" or not command.strip():
                    continue
                script, args = split_command(command)
                rows.append({"event": event, "matcher": group.get("matcher") or None, "script": script,
                             "args": args, "timeout_s": hook.get("timeout")})
    return rows


def load_table(path: Path) -> dict:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def _finding(rule: str, row: dict, message: str) -> Finding:
    return Finding(rule, row.get("script", "?"), row.get("event", "?"), row.get("matcher") or "", message)


def _label(row: dict) -> str | None:
    return LABEL_OF.get(row.get("opencode", ""))


def _plain_row_findings(row: dict, probe_ids: set[str]) -> list[Finding]:
    found = []
    for field in REQUIRED_ROW_FIELDS:
        if field not in row:
            found.append(_finding("C21", row, f"field {field} is missing"))
    if "difficulty" in row and not str(row["difficulty"]).strip():
        found.append(_finding("C21", row, "difficulty is empty"))
    mechanism = row.get("opencode")
    if mechanism is not None and mechanism not in MECHANISMS:
        found.append(_finding("C24", row, f"mechanism {mechanism!r} is not one the label map knows"))
    for pid in row.get("paths", []):
        if pid not in probe_ids:
            found.append(_finding("C25", row, f"path {pid} is not a probe_paths id"))
    for pid in row.get("gaps", {}):
        if pid not in probe_ids:
            found.append(_finding("C25", row, f"gap names path {pid}, not a probe_paths id"))
    exempt = row.get("exempt")
    if exempt is not None:
        if is_denying(row.get("event", "")):
            found.append(_finding("C23", row, "a denying row cannot be exempt"))
        for key in ("reason", "covers_gap"):
            if not str(exempt.get(key, "")).strip():
                found.append(_finding("C23", row, f"exempt needs a non-empty {key}"))
    return found


def _wiring_findings(rows: list[dict], wiring: list[dict]) -> list[Finding]:
    found = []
    table_keys = {}
    for row in rows:
        if not all(f in row for f in ("event", "script")):
            continue
        key = row_key(row)
        if key in table_keys:
            found.append(_finding("C20", row, "row appears twice"))
        table_keys[key] = row
    wired = {row_key(w): w for w in wiring}
    for key, w in wired.items():
        if key not in table_keys:
            found.append(_finding("C20", w, "wired hook has no row in the table"))
        elif w.get("timeout_s") != table_keys[key].get("timeout_s"):
            found.append(_finding("C20", w, f"table timeout_s {table_keys[key].get('timeout_s')} differs "
                                            f"from the wired {w.get('timeout_s')}"))
    for key, row in table_keys.items():
        if key not in wired:
            found.append(_finding("C20", row, "row has no wired hook in the layer"))
    return found


def _s2(row: dict, probe_by_id: dict, label: str) -> list[Finding]:
    found = []
    paths = set(row.get("paths", []))
    gaps = row.get("gaps", {})
    for pid in probe_by_id:
        if pid in paths:
            continue
        gap = gaps.get(pid)
        if pid != CHILD_RUN and gap and gap.startswith(SWITCHED_OFF) and gap[len(SWITCHED_OFF):].strip():
            continue
        shown = f"gap {gap!r} does not pass" if gap else "no gap named"
        found.append(_finding("S2", row, f"denying row misses path {pid}: {shown}"))
    for pid in row.get("paths", []):
        if pid in probe_by_id and label not in probe_by_id[pid].get("refused_by", []):
            found.append(_finding("S2", row, f"path {pid} was not refused by {row['opencode']} ({label})"))
    return found


def _s3(row: dict, probe_mechanisms: dict, label: str, denying: bool) -> list[Finding]:
    entry = probe_mechanisms.get(label)
    if not entry or not entry.get("fired"):
        return [_finding("S3", row, f"mechanism {row['opencode']} ({label}) is not proven: it did not fire")]
    if denying and not entry.get("can_refuse"):
        return [_finding("S3", row, f"mechanism {row['opencode']} ({label}) is not proven to refuse")]
    return []


def _s4_paths(row: dict, probe_by_id: dict, label: str) -> list[Finding]:
    found = []
    paths = set(row.get("paths", []))
    executed = [pid for pid in probe_by_id if pid != CHILD_RUN]
    for pid in executed:
        if pid not in paths:
            found.append(_finding("S4", row, f"PostToolUse row misses executed path {pid}"))
        elif label not in probe_by_id[pid].get("fired_by", []):
            found.append(_finding("S4", row, f"path {pid} was not fired by {row['opencode']} ({label})"))
    return found


def _strict_row_findings(row: dict, probe_by_id: dict, probe_mechanisms: dict) -> list[Finding]:
    denying = is_denying(row["event"])
    mechanism = row.get("opencode", NO_MECHANISM)
    exempt = row.get("exempt") is not None
    if denying:
        if exempt or mechanism == NO_MECHANISM:
            return [_finding("S1", row, "denying row is " + ("exempt" if exempt else "mapped to none"))]
    elif mechanism == NO_MECHANISM:
        return [] if exempt else [_finding("S4", row, "non-denying row mapped to none without exempt")]
    label = _label(row)
    if label is None:
        return []
    found = []
    if not (exempt and not denying):
        found += _s3(row, probe_mechanisms, label, denying)
    if denying:
        found += _s2(row, probe_by_id, label)
    elif label == "PostToolUse" and not exempt:
        found += _s4_paths(row, probe_by_id, label)
    return found


def check_rows(table: dict, wiring: list[dict], strict: bool = False) -> list[Finding]:
    """Every finding of ``table`` against the layer's ``wiring`` (rows as ``installer_rows`` makes them)."""
    rows = table.get("row") or []
    probe_by_id = {p["id"]: p for p in table.get("probe_paths", [])}
    probe_mechanisms = {m["mechanism"]: m for m in table.get("probe_mechanisms", [])}
    found = _wiring_findings(rows, wiring)
    for row in rows:
        found += _plain_row_findings(row, set(probe_by_id))
    if strict and STRICT_DENYING_ROW_RULES:
        for row in rows:
            if all(f in row for f in ("event", "script")):
                found += _strict_row_findings(row, probe_by_id, probe_mechanisms)
    return found


class LayerResult(NamedTuple):
    layer: str
    findings: list
    exempt: list


def table_settings_file(table: dict, root: Path) -> Path:
    return root / table.get("settings", "settings.json")


def check_layer(layer_root: Path | None = None, table_path: Path | None = None, strict: bool = False,
                settings_file: Path | None = None) -> LayerResult:
    """``check --layer core`` (no ``layer_root``) or ``check --layer-root DIR``.

    A layer root without ``DIR/hook-parity.toml`` fails (C22). A table with no rows is accepted when
    the settings file it names wires no hook.
    """
    if layer_root is None:
        table_path = table_path or CORE_TABLE
        label, wiring_of = "core", installer_rows
    else:
        table_path = table_path or layer_root / TABLE_NAME
        label = str(layer_root)
        wiring_of = None
    if not Path(table_path).is_file():
        missing = Finding("C22", "-", "-", "", f"layer {label} has no table file {table_path}")
        return LayerResult(label, [missing], [])
    table = load_table(table_path)
    if wiring_of is not None:
        wiring = wiring_of()
    else:
        wiring = settings_rows(settings_file or table_settings_file(table, layer_root))
    findings = check_rows(table, wiring, strict=strict)
    exempt = [r for r in table.get("row") or [] if r.get("exempt") is not None]
    return LayerResult(label, findings, exempt)


def probe_tables(rec: dict) -> tuple[list[dict], list[dict]]:
    """The ``probe_paths`` and ``probe_mechanisms`` a passing mechanisms record yields.

    ``refused_by`` comes from P1, ``fired_by`` from P7 (``child-run`` has none), and the mechanisms
    from P13.
    """
    fired_by = {p["id"]: p.get("fired_by") for p in rec["cases"]["P7"]["evidence"]["paths"]}
    paths = []
    for p in rec["cases"]["P1"]["paths"]:
        entry = {"id": p["id"], "refused_by": list(p["refused_by"])}
        if fired_by.get(p["id"]) is not None:
            entry["fired_by"] = list(fired_by[p["id"]])
        paths.append(entry)
    mechanisms = [{"mechanism": m["mechanism"], "fired": bool(m["fired"]), "can_refuse": bool(m["can_refuse"])}
                  for m in rec["cases"]["P13"]["mechanisms"]]
    return paths, mechanisms


def pinned_probe_tables() -> tuple[list[dict], list[dict]]:
    """``probe_tables`` of the record pin.toml's ``last_pass.mechanisms`` names; raises when it is not there."""
    from opencode_probe import core, record

    entry = core.read_pin().get("last_pass", {}).get("mechanisms")
    found = record.pinned_record(core.state_dir(), entry)
    if found is None:
        raise RuntimeError("pin.toml names no readable mechanisms record under the probe state directory")
    return probe_tables(found[1])


def _toml_list(values: list) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def render_probe_header(paths: list[dict], mechanisms: list[dict]) -> str:
    """The ``probe_paths`` / ``probe_mechanisms`` part of a table, as TOML text."""
    out = []
    for p in paths:
        out.append("[[probe_paths]]")
        out.append(f"id = {json.dumps(p['id'])}")
        out.append(f"refused_by = {_toml_list(p['refused_by'])}")
        if "fired_by" in p:
            out.append(f"fired_by = {_toml_list(p['fired_by'])}")
        out.append("")
    for m in mechanisms:
        out.append("[[probe_mechanisms]]")
        out.append(f"mechanism = {json.dumps(m['mechanism'])}")
        out.append(f"fired = {str(m['fired']).lower()}")
        out.append(f"can_refuse = {str(m['can_refuse']).lower()}")
        out.append("")
    return "\n".join(out)


def _resolved_units(root: Path) -> list[Path]:
    """The settings files a composed root links to (or holds), each resolved to its storage file."""
    units = []
    for name in SETTINGS_NAMES:
        candidate = root / name
        if candidate.is_symlink() or candidate.is_file():
            resolved = candidate.resolve()
            if resolved.is_file() and resolved not in units:
                units.append(resolved)
    return units


def start_notice(layer_roots: list[Path]) -> list[str]:
    """The lines an adapter shows at session start.

    The agent home (``config_root.agent_home()``) is the Core layer whatever its settings file is, so
    its notice is the ``check --layer core --strict`` result; its own ``settings.json`` is read for
    third-party hooks. Every other root contributes the storage directory each of its settings
    links resolves to, whose ``hook-parity.toml`` sits beside the settings file.
    """
    lines: list[str] = []
    agent_home = config_root.agent_home().resolve()
    roots = [Path(r) for r in layer_roots]
    home_given = any(r.resolve() == agent_home for r in roots)
    wired_scripts: set[str] = set()
    exempt_lines: list[str] = []

    def note(result: LayerResult, label: str) -> None:
        if result.findings:
            lines.append(f"parity: layer {label} fails check --strict ({len(result.findings)} findings, "
                         f"first: {result.findings[0].line()})")
        for row in result.exempt:
            exempt_lines.append(f"parity: exempt {row['script']} ({row['event']}) in layer {label}: "
                                f"{row['exempt'].get('reason', '?')}; covers gap: "
                                f"{row['exempt'].get('covers_gap', '?')}")

    if home_given:
        note(check_layer(strict=True), "core")
        wired_scripts |= {r["script"] for r in installer_rows()}
    for root in roots:
        if root.resolve() == agent_home:
            continue
        units = _resolved_units(root)
        if not units:
            lines.append(f"parity: layer {root} wires no settings file; it has no table")
        for unit in units:
            table_path = unit.parent / TABLE_NAME
            if not table_path.is_file():
                lines.append(f"parity: layer {unit.parent} has no {TABLE_NAME} (tableless)")
                wired_scripts |= {r["script"] for r in settings_rows(unit)}
                continue
            result = check_layer(unit.parent, strict=True, settings_file=unit)
            note(result, str(unit.parent))
            wired_scripts |= {r["script"] for r in settings_rows(unit)}
    lines += exempt_lines
    if home_given:
        for row in settings_rows(agent_home / "settings.json"):
            if row["script"] not in wired_scripts:
                lines.append(f"parity: third-party hook {row['script']} ({row['event']}) in "
                             f"{agent_home / 'settings.json'} is not carried to OpenCode")
    else:
        lines.append("parity: the agent home is not among the layer roots; its settings.json was not "
                     "scanned for third-party hooks")
    return lines
