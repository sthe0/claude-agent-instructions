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
    ("hook-deferring-disposition-gate.py",
     "denies an ask whose every option defers or refuses work the agent could "
     "do itself right now"),
    ("hook-multi-mount-search-guard.py",
     "denies a search that would fan out across every mount"),
    ("hook-turn-end-gate.py",
     "the Stop-event guardian shell; absent, no turn-boundary obligation "
     "(self-improvement engagement, resolution) is ever blocked on"),
)

# The TIMEOUT axis: how long a registration must be allowed to run.
#
# Presence is not enough for a hook that calls a `claude -p` judge. The harness
# kills a hook at its registered timeout, so a hook registered at 5s whose judge
# needs 10.5-47s (measured) is wired, green to every presence probe, and dead on
# every single call — the verdict is computed and thrown away. The minimum here
# is the hook's OWN whole-invocation judge budget; the registration must be at
# least that, and in practice carries interpreter-start headroom on top.
#
# Scope: this table is NOT a subset of GATE_BEARING_HOOKS and must not be
# derived from it. The two answer different questions — "does absence disable
# enforcement" versus "does this registration have to allow for a slow judge" —
# and a hook can be in either without the other.
TIMEOUT_REQUIREMENTS: "tuple[tuple[str, int, str], ...]" = (
    ("hook-escalation-diagnosis-gate.py", 30,
     "one outage-escalation judge under a 30s whole-invocation budget"),
    ("hook-deferring-disposition-gate.py", 45,
     "one deferring-disposition judge, on the first fired menu, under a 45s "
     "whole-invocation budget"),
    ("hook-turn-end-gate.py", 52,
     "up to three judges in one invocation under a 52s whole-invocation budget"),
)

# Each TIMEOUT_REQUIREMENTS minimum, above, is a copy of a number the hook
# module itself already owns as a constant — the whole-invocation budget it
# passes to `judge_budget.JudgeBudget`. This is the machine link back to that
# owning constant, so a test can assert the two stay equal by IMPORT rather
# than by two literals that happen to match today. Keyed separately from
# TIMEOUT_REQUIREMENTS (instead of widening its tuple) so the existing 3-tuple
# unpack at every current call site does not have to change.
TIMEOUT_REQUIREMENT_OWN_CONSTANT: "dict[str, str]" = {
    "hook-escalation-diagnosis-gate.py": "_JUDGE_BUDGET_S",
    "hook-deferring-disposition-gate.py": "_ASK_JUDGE_BUDGET_S",
    "hook-turn-end-gate.py": "_TURN_JUDGE_BUDGET_S",
}

# K: how many judge calls one invocation of each hook may make. Keyed as a
# SIBLING dict for the same reason TIMEOUT_REQUIREMENT_OWN_CONSTANT is one —
# widening the 3-tuple above would force every existing unpack of it to change.
#
# K is what makes the budget above checkable rather than merely plausible: a
# budget must cover the medians of the calls that precede the last one plus a
# floor for the last (lib/judge_latency.required_budget_s). At K = 1 that reduces
# to one floor, and the hook's per-call ceiling equals its whole budget — capping
# the only call lower would forfeit budget for nothing.
#
# These are DECLARED limits, not measurements of what a run happens to do: both
# single-call hooks can encounter a second candidate (a second fired menu, a
# second escalating turn) and deliberately do not judge it, because the floor
# left after the first call cannot fit another. lib/judge_latency.
# HOOK_CALL_SEQUENCE names WHICH judges those calls are, in order; a test asserts
# the two agree.
TIMEOUT_REQUIREMENT_CALLS: "dict[str, int]" = {
    "hook-escalation-diagnosis-gate.py": 1,
    "hook-deferring-disposition-gate.py": 1,
    "hook-turn-end-gate.py": 3,
}


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


@dataclass(frozen=True)
class Registration:
    """One live entry wiring a hook: where it sits and how long it may run.

    ``timeout`` is None when the entry carries no explicit ``timeout`` key. That
    is UNKNOWN on the timeout axis, never "fine" — the harness's own default is
    a client-side constant this module deliberately does not read (it changes
    between releases, and every entry this system installs carries an explicit
    number anyway), so an absent key is a question mark and callers that must
    fail closed treat it as one.
    """

    event: str
    matcher: "str | None"
    command: str
    timeout: "int | None"
    member: Path


@dataclass
class Wiring:
    """The probe's answer for one hook basename against one config root."""

    basename: str
    root: Path
    status: str
    events: "dict[str, list[str]]" = field(default_factory=dict)
    registrations: "list[Registration]" = field(default_factory=list)
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


def _timeout_of(hook: dict) -> "int | None":
    """The entry's declared timeout, or None when it carries none or one this
    module cannot read as a number of seconds. A non-numeric value degrades to
    None rather than raising: on the timeout axis "unreadable" and "absent" are
    the same answer — UNKNOWN."""
    value = hook.get("timeout")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _scan_settings(
    settings: dict, basename: str, member: Path
) -> "tuple[dict[str, list[str]], list[Registration], bool]":
    """Every hook command mentioning `basename`, keyed by event name.

    Scans ALL event sections, not just PreToolUse. Returns (events,
    registrations, modelled): `modelled` is False when the settings carry a
    shape this function does not understand, which the caller must treat as
    UNKNOWN rather than ABSENT.
    """
    events: "dict[str, list[str]]" = {}
    registrations: "list[Registration]" = []
    modelled = True
    hooks = settings.get("hooks")
    if hooks is None:
        return events, registrations, True
    if not isinstance(hooks, dict):
        return events, registrations, False
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
                    matcher = grp.get("matcher")
                    registrations.append(Registration(
                        event=str(event),
                        matcher=matcher if isinstance(matcher, str) else None,
                        command=cmd,
                        timeout=_timeout_of(hook),
                        member=member,
                    ))
    return events, registrations, modelled


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
        found, registrations, member_modelled = _scan_settings(data, basename, member)
        modelled = modelled and member_modelled
        result.registrations.extend(registrations)
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


def timeout_shortfalls(wiring: Wiring, minimum: int) -> "list[str]":
    """Registrations whose timeout is ESTABLISHED below `minimum` seconds.

    Per-registration, not a single verdict over the hook: the rule is that every
    live registration must allow the hook's whole budget. That is stricter than
    the harness strictly needs (it deduplicates entries by command string, so
    identical commands collapse), and the strictness points fail-closed — an
    extra slow-judge registration pinned at 5s is reported instead of hidden
    behind a correct sibling.
    """
    out: "list[str]" = []
    for reg in wiring.registrations:
        if reg.timeout is not None and reg.timeout < minimum:
            out.append(
                f"{wiring.basename} registered with timeout {reg.timeout}s in "
                f"{reg.member} ({reg.event}, matcher {reg.matcher or '*'}) — "
                f"below its own {minimum}s judge budget, so the harness kills "
                f"it mid-judge on every call"
            )
    return out


def timeout_unknowns(wiring: Wiring) -> "list[str]":
    """Registrations carrying no readable timeout — the UNKNOWN half of the
    axis, kept apart from `timeout_shortfalls` because the two have opposite
    reporting rules: a shortfall is established and worth saying out loud in an
    advisory channel, an unknown is only actionable where the caller must fail
    closed."""
    return [
        f"{wiring.basename} registered in {reg.member} "
        f"({reg.event}, matcher {reg.matcher or '*'}) with no explicit timeout "
        f"— its effective limit cannot be established"
        for reg in wiring.registrations
        if reg.timeout is None
    ]


def duplicate_registration_note(wiring: Wiring) -> "str | None":
    """A line worth a look when one hook has more than one live registration.

    Deliberately not phrased as "it runs twice". Read from the client bundle:
    command hooks are deduplicated on `pluginRoot ∥ shell ∥ command ∥ args ∥
    if`, with matcher and timeout OUTSIDE that key — so duplicates whose command
    strings match collapse to one, and only DISTINCT commands genuinely run more
    than once. Both cases are worth surfacing (a second entry is at minimum a
    timeout the reconciler has to keep in step), but only the second is a double
    execution, and saying so of the first would be a fabricated finding.
    """
    if len(wiring.registrations) < 2:
        return None
    commands = {reg.command for reg in wiring.registrations}
    tail = (
        f"their commands differ ({len(commands)} distinct), so the harness runs "
        "the hook more than once per event"
        if len(commands) > 1
        else "their commands are identical, which the harness deduplicates"
    )
    return (
        f"{wiring.basename} has {len(wiring.registrations)} live registrations — "
        + tail
    )


def runs_more_than_once(wiring: Wiring) -> bool:
    """True only when the duplicate registrations genuinely double-execute, i.e.
    their command strings differ. Kept apart from
    ``duplicate_registration_note`` because the two callers need the DISTINCTION
    the note only words: an advisory channel that raises its alarm on a pair the
    harness silently deduplicates spends the reader's attention on a non-problem
    and teaches them to skip the block."""
    return len({reg.command for reg in wiring.registrations}) > 1
