"""Is hook X registered in config root Y? — a general, read-only wiring probe.

Difficulty removed: a gate-bearing hook that is present in the repo, declared in
install-reminder-hooks.sh, and perfectly correct can still be *dead* in a
session, because the config root the harness loads hooks from is not always the
root the system installed into (see ``config_root`` § Two roots). When that
happens the gate downstream of the hook does not fail loudly — it demands an
artifact whose only producer was never running, and blames whoever hit it. To
say that out loud a gate needs to be able to ask this question, and until now
only ``hook-canon-guard-wired-check.py`` could, for exactly one hook against
exactly one event pair. This module is that logic in its general form.

Prior art, not rediscovered: ``self-diagnose.py::default_settings_paths``
already enumerates six settings homes, using ``$CLAUDE_PROJECT_DIR`` else cwd
for the project ones. That asymmetry is why this probe does NOT model the
project member — see § Chain membership.

The harness's rule, confirmed
-----------------------------

Verified against the installed client bundle (``claude.exe``, 2.1.x) rather
than assumed:

- The user config root is ``$CLAUDE_CONFIG_DIR`` else ``~/.claude`` — the same
  rule ``config_root.harness_config_root()`` implements.
- The bundle's own hook-source table names five sources: ``userSettings``
  (``<root>/settings.json``), ``projectSettings`` (``.claude/settings.json``),
  ``localSettings`` (``.claude/settings.local.json``), ``policySettings``
  (the managed-settings.json policy file), plus plugin/session/built-in hooks
  registered internally.

So the harness merges *several* members. A probe that read one file would call
a hook ABSENT whenever it happens to be wired in another — a diagnosis worse
than the generic refusal it replaces.

Chain membership — a decision, not an omission
----------------------------------------------

Modelled: ``<root>/settings.json``, ``<root>/settings.local.json``, and the
managed-policy file. Those are the user-level members plus policy: every caller
can locate them identically from the root alone.

Deliberately EXCLUDED: the project-level ``.claude/settings.json`` and
``.claude/settings.local.json``. Neither caller can locate them honestly. The
engine runs as ``cd <repo>/scripts && python3 -m agentctl``, so a cwd-relative
lookup resolves to ``<repo>/scripts/.claude`` and misses the real one; a
SessionStart hook runs with the session's own cwd — a third value again. Only
the hook caller receives ``$CLAUDE_PROJECT_DIR`` from the harness (the engine's
environment has it unset), so the two callers could not answer alike. Guessing
that root is precisely how a false ABSENT is manufactured. When a project-wired
gate-bearing hook actually appears, extend ``settings_chain`` with the project
member for callers that can prove the project root — that is the documented
extension point.

Consequently ABSENT is always reported QUALIFIED — "not registered in any
user-level settings member of <root>" — never as a bare "not registered".

UNKNOWN is a first-class outcome, and the only safe answer under partial
information: any member that is unreadable, or a settings shape this module
does not model, degrades the answer to UNKNOWN rather than ABSENT.

Read-only by construction: nothing here writes, creates or repairs a settings
file. This module diagnoses; installing is ``install-reminder-hooks.sh``'s job.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import config_root  # noqa: E402

WIRED = "wired"
ABSENT = "absent"
UNKNOWN = "unknown"

# Gate-bearing hooks: the ones whose absence disables ENFORCEMENT rather than
# dropping an advisory. The predicate is "denies OR writes an artifact some gate
# requires" — the second half is load-bearing, not pedantic: the delivery gate
# mostly does not deny, it WRITES the stamp gates.plan_presentation_blockers
# demands, so a deny-only registry would omit the very hook this probe exists
# for. Each note names what stops being enforced when the entry goes missing.
GATE_BEARING_HOOKS: "tuple[tuple[str, str], ...]" = (
    ("hook-plan-delivery-gate.py",
     "writes the delivery stamp gates.plan_presentation_blockers requires; "
     "without it every approve refuses for want of a proof nothing can produce"),
    ("hook-state-gate.py",
     "denies production Edit/Write before an execution node — the spine's "
     "non-skippable plan-approval gate"),
    ("hook-guard-canon-readonly.py",
     "denies Edit/Write and git commit inside a canonical checkout; absent, "
     "canon looks protected but is writable"),
    ("hook-guard-destructive-rm.py",
     "denies a recursive rm that could target $HOME, either config root, or "
     "the instruction repo"),
    ("hook-scope-conflict.py",
     "denies an edit to a path another LIVE session holds — cross-session "
     "isolation stops being enforced"),
    ("hook-escalation-diagnosis-gate.py",
     "denies escalating an external-service failure to the user with no "
     "recorded diagnosis"),
    ("hook-multi-mount-search-guard.py",
     "denies a search that would fan out across every mount"),
    ("hook-turn-end-gate.py",
     "the Stop-event guardian shell; absent, no turn-boundary obligation "
     "(self-improvement engagement, resolution) is ever blocked on"),
)


def managed_settings_path() -> Path:
    """The managed-policy settings file for this platform.

    Enterprise-managed policy, outside any user root — included in the chain
    because a hook wired there is live for every session on the machine.
    """
    system = platform.system()
    if system == "Darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if system == "Windows":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(base) / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


def settings_chain(root: Path | None = None) -> "list[Path]":
    """The settings members this probe reads, for a given config root.

    See § Chain membership for what is in, what is out and why. Returns paths
    whether or not they exist — a member that is simply absent is not evidence
    of anything, while one that exists but cannot be parsed is (it degrades the
    answer to UNKNOWN).
    """
    base = root if root is not None else config_root.harness_config_root()
    return [base / "settings.json", base / "settings.local.json", managed_settings_path()]


@dataclass
class Wiring:
    """The probe's answer for one hook basename against one config root."""

    basename: str
    root: Path
    status: str
    events: "dict[str, list[str]]" = field(default_factory=dict)
    missing_script_paths: "list[str]" = field(default_factory=list)
    members_read: "list[Path]" = field(default_factory=list)
    members_unreadable: "list[Path]" = field(default_factory=list)

    @property
    def wired(self) -> bool:
        return self.status == WIRED

    def describe(self) -> str:
        """One line a gate can quote verbatim. ABSENT is always qualified with
        the scope of the claim — the probe never says a bare 'not registered'."""
        if self.status == WIRED:
            where = ", ".join(sorted(self.events)) or "?"
            line = f"{self.basename} is registered in {self.root} ({where})"
            if self.missing_script_paths:
                line += (
                    " but points at a script path that does not exist: "
                    + ", ".join(self.missing_script_paths)
                )
            return line
        if self.status == ABSENT:
            return (
                f"{self.basename} is not registered in any user-level settings "
                f"member of {self.root}"
            )
        why = ", ".join(str(p) for p in self.members_unreadable) or "unmodelled settings shape"
        return (
            f"whether {self.basename} is registered in {self.root} cannot be "
            f"determined ({why})"
        )


def _scan_settings(settings: dict, basename: str) -> "tuple[dict[str, list[str]], bool]":
    """Every hook command mentioning `basename`, keyed by event name.

    Scans ALL event sections, not just PreToolUse. Returns (events, modelled):
    `modelled` is False when the settings carry a shape this function does not
    understand, which the caller must treat as UNKNOWN rather than ABSENT.
    """
    events: "dict[str, list[str]]" = {}
    modelled = True
    hooks = settings.get("hooks")
    if hooks is None:
        return events, True
    if not isinstance(hooks, dict):
        return events, False
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            modelled = False
            continue
        for grp in groups:
            if not isinstance(grp, dict):
                modelled = False
                continue
            entries = grp.get("hooks")
            if entries is None:
                continue
            if not isinstance(entries, list):
                modelled = False
                continue
            for hook in entries:
                if not isinstance(hook, dict):
                    modelled = False
                    continue
                cmd = hook.get("command") or ""
                if not isinstance(cmd, str):
                    modelled = False
                    continue
                if basename in cmd:
                    events.setdefault(str(event), []).append(cmd)
    return events, modelled


_INTERPRETERS = frozenset({"python3", "python", "bash", "sh", "zsh", "node", "uv"})


def script_path(command: str) -> str:
    """The script a hook command runs.

    ``install-reminder-hooks.sh`` writes the absolute script path as the whole
    command, so the first token is normally it. A hand-wired entry may instead
    run the script through an interpreter, and reading `python3` as the script
    path would report every such entry as pointing at a missing file — a
    fabricated problem. So a leading interpreter token, and any flags after it,
    are skipped.
    """
    tokens = command.split()
    while tokens and (
        Path(tokens[0]).name in _INTERPRETERS or tokens[0].startswith("-")
    ):
        tokens = tokens[1:]
    return tokens[0] if tokens else ""


def probe(basename: str, root: Path | None = None) -> Wiring:
    """Is `basename` wired in `root`'s settings chain, and does it still exist?

    WIRED as soon as any member registers it (a later member cannot un-wire it).
    ABSENT only when every member was read successfully, all were modelled, and
    none mentioned it. Anything else is UNKNOWN.
    """
    base = root if root is not None else config_root.harness_config_root()
    result = Wiring(basename=basename, root=base, status=UNKNOWN)
    modelled = True
    for member in settings_chain(base):
        if not member.is_file():
            continue
        try:
            data = json.loads(member.read_text(encoding="utf-8"))
        except Exception:
            result.members_unreadable.append(member)
            modelled = False
            continue
        if not isinstance(data, dict):
            result.members_unreadable.append(member)
            modelled = False
            continue
        result.members_read.append(member)
        found, member_modelled = _scan_settings(data, basename)
        modelled = modelled and member_modelled
        for event, cmds in found.items():
            result.events.setdefault(event, []).extend(cmds)

    if result.events:
        result.status = WIRED
        for cmds in result.events.values():
            for cmd in cmds:
                path = script_path(cmd)
                if path and not os.path.exists(path):
                    result.missing_script_paths.append(path)
        return result
    result.status = ABSENT if modelled else UNKNOWN
    return result
