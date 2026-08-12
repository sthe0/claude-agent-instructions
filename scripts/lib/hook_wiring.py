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
for the project ones. This probe reuses the variable and drops the cwd
fallback — see § Chain membership.

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

Chain membership — three members always, five when the harness says where
-------------------------------------------------------------------------

Always modelled: ``<root>/settings.json``, ``<root>/settings.local.json``, and
the managed-policy file. Those are the user-level members plus policy: every
caller can locate them identically from the root alone.

Modelled WHEN AND ONLY WHEN ``$CLAUDE_PROJECT_DIR`` is set: the project-level
``.claude/settings.json`` and ``.claude/settings.local.json``, under the root
that variable names. There is no cwd fallback, and the absence of one is the
whole discipline — the engine runs as ``cd <repo>/scripts && python3 -m
agentctl``, so a cwd-relative lookup resolves to ``<repo>/scripts/.claude`` and
misses the real one, while a SessionStart hook runs with the session's own cwd,
a third value again. Guessing that root is precisely how a false ABSENT is
manufactured, so an unset variable means the project member is not read at all
rather than read from a guess. (``self-diagnose.py`` does fall back to cwd; it
enumerates candidate homes for a human-facing report, where a spurious path
costs a line of output rather than a wrong causal claim.)

Which of these happened is not left to prose. ``Wiring.project_scope_covered``
records it per probe: True when the variable named a root AND every project
member was accounted for — parsed into a shape this module models, or answered
not-a-file by the filesystem — and False otherwise. There are THREE ways to
miss a project member, not two: the variable is unset; the member cannot be
read, either because it will not parse or because the filesystem will not even
say whether it is there; or it parses and then carries a settings shape
``_scan_settings`` does not model, whose entries are skipped unread. The third
is the quiet one, and the reason the predicate is written over
``members_unmodelled`` as well as ``members_unreadable``: that member IS opened,
so a predicate asking only "did every project member parse" answers yes and
certifies a scope the probe never reached. ``Wiring.absence_scope`` reads that
field, because an ABSENT sentence is about how wide the chain reached.

Whether the ANSWER is a fact or a qualified one is a second question, and
``Wiring.scope_fully_covered`` is the field for it: a member the probe opened
and could not turn into entries leaves a partial view whatever scope it sits
in, and a user-level member hides a registration exactly as well as a project
one. Callers that must not over-claim read that field instead of assuming; see
``dispatch_witness_snapshot.entry_for``, which derives the snapshot's
``scope_qualified`` from it.

Consequently ABSENT is never a bare "not registered": it is either "not
registered in any user-level settings member of <root>" or, once the project
member has been read, "not registered in any settings member of <root> or of
the project root <project root>, project-level included" — naming the second
root because that is where the member the wider claim rests on actually lives.

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
    ("hook-guard-permission-self-grant.py",
     "denies a call widening the agent's own permission surface in answer to a "
     "denial of an arming kind, and denies what it cannot evaluate; absent, a "
     "self-grant proceeds silently and the widened entry outlives the task"),
)

# The TIMEOUT axis: how long a registration must be allowed to run.
#
# Presence is not enough for a hook that calls a `claude -p` judge. The harness
# kills a hook at its registered timeout, so a hook registered at 5s whose judge
# needs 5.9-40.0s (measured, lib/judge_latency.py) is wired, green to every
# presence probe, and dead on
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


# The harness exports the project root to hook processes under this name, and
# it is the only thing this module will locate the project members from.
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"


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


def project_root() -> "Path | None":
    """The root ``$CLAUDE_PROJECT_DIR`` names, or None when it named none.

    None is what makes the project members unlocatable, and it is a different
    answer from "there is no project settings file" — see § Chain membership.
    """
    named = os.environ.get(PROJECT_DIR_ENV, "").strip()
    return Path(named).expanduser() if named else None


def project_settings_chain() -> "list[Path]":
    """The project-level members, or [] when the harness named no project root.

    An empty list means "not looked at", never "nothing there" — the two are
    different answers and ``probe`` keeps them apart on
    ``Wiring.project_scope_covered``. See § Chain membership for why there is
    no cwd fallback.
    """
    base = project_root()
    if base is None:
        return []
    return [base / ".claude" / "settings.json", base / ".claude" / "settings.local.json"]


def resolved(path: Path) -> Path:
    """`path` with symlinks and `..` collapsed, or `path` unchanged when the
    filesystem will not say. A comparison KEY only: every path this module
    reports is the one the settings chain actually names, so a reader can find
    it in the environment they configured.

    The loop case raises ``RuntimeError``, not ``OSError``, and the path it is
    raised for can come from ``$CLAUDE_PROJECT_DIR`` — an externally supplied
    value. Every caller of this module is wrapped in a catch-all that goes
    quiet, so catching only ``OSError`` would let one looping symlink turn the
    enforcement-is-OFF banner off, silently, every session.

    The fallback's safety is PER-USE, not intrinsic, so check your own
    direction before adopting it. Here and in ``settings_chain`` the use is
    dedup, where a failed comparison keeps both spellings — it over-reports,
    which is the harmless polarity. ``hook-canon-guard-wired-check.py`` uses
    the same helper for a boolean identity decision
    (``resolved(harness) != resolved(home)``), where a failed comparison reads
    as "different roots", takes the personal branch, and returns before the
    registry checks — it UNDER-reports. Nothing reaches that direction today:
    ``resolve()`` fails only on a loop, and a spelling containing one cannot
    name a real config root. A future caller has to re-answer the question.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def settings_chain(root: Path | None = None) -> "list[Path]":
    """The settings members this probe reads, for a given config root.

    See § Chain membership for what is in, what is conditional and why. Returns
    paths whether or not they exist — a member that is simply absent is not
    evidence of anything, while one that exists but cannot be parsed is (it
    degrades the answer to UNKNOWN).

    Deduplicated on the RESOLVED path, first spelling kept. First, because the
    head of the chain is spelled from the caller's own `root` while the tail is
    spelled from ``$CLAUDE_PROJECT_DIR``, and every path this module reports —
    ``members_read``, ``Registration.member``, ``describe()``'s UNKNOWN reasons
    — has to be findable in the environment the caller configured rather than
    under a root it never asked about.

    Deduplicated at all, because the user-level and project-level members are
    distinct files only while the two roots differ, and on a ``~/.claude``
    machine whose session project root is ``$HOME`` they are the same two files
    named twice. A chain that lists them twice reads every registration twice,
    and two copies of one entry are enough to make
    ``duplicate_registration_note`` and ``runs_more_than_once`` report a hook
    registered exactly once as wired more than once — a fabricated finding, on
    the SessionStart banner.
    """
    base = root if root is not None else config_root.harness_config_root()
    chain = [
        base / "settings.json",
        base / "settings.local.json",
        managed_settings_path(),
        *project_settings_chain(),
    ]
    seen: "set[Path]" = set()
    out: "list[Path]" = []
    for member in chain:
        key = resolved(member)
        if key not in seen:
            seen.add(key)
            out.append(member)
    return out


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
    # Members that parsed as JSON objects and then carried a settings shape
    # `_scan_settings` does not model. Kept apart from `members_unreadable`
    # because the two look different to a human — this file opens fine — while
    # meaning the same thing to every caller: its entries were skipped, so the
    # member was not accounted for.
    members_unmodelled: "list[Path]" = field(default_factory=list)
    # Did this probe reach the project-level members? Set by probe() from what
    # it actually read; the default is the honest one for a Wiring built by
    # hand, which reached nothing. See § Chain membership.
    project_scope_covered: bool = False
    # The root $CLAUDE_PROJECT_DIR named at probe time, or None when it named
    # none — so a covered answer can say WHERE the project members it reached
    # live, which is not under `root`.
    project_root: "Path | None" = None

    @property
    def wired(self) -> bool:
        return self.status == WIRED

    @property
    def scope_fully_covered(self) -> bool:
        """Was EVERY member of the chain accounted for — so this answer is a
        fact rather than a qualified one?

        Two public fields, two questions, and mixing them up is how a partial
        view gets spent as a whole one. ``project_scope_covered`` is about how
        wide the chain REACHED, which is what an ABSENT sentence has to word.
        This is about whether the answer may be spent unqualified, which is
        what a caller reasoning FROM the answer needs — and there a user-level
        member the probe opened and could not model hides a registration, or a
        larger timeout, exactly as well as a project one.
        """
        return (
            self.project_scope_covered
            and not self.members_unreadable
            and not self.members_unmodelled
        )

    def absence_scope(self) -> str:
        """How far this answer reaches — the qualification every caller quoting
        an ABSENT must quote with it.

        Derived from what the probe read, never asserted by the caller: a scope
        claimed rather than reached is exactly the over-claim that turns "not in
        the members we could see" into "was never wired".
        """
        if self.project_scope_covered:
            # Naming both roots, because the project member is NOT under
            # `self.root` — it lives under whatever $CLAUDE_PROJECT_DIR named,
            # and a sentence attributing it to the config root sends a reader
            # looking for a file that is not there. (A hand-built Wiring can
            # claim coverage without a root; it keeps the shorter sentence
            # rather than printing "None".)
            if self.project_root is not None:
                return (
                    f"any settings member of {self.root} or of the project root "
                    f"{self.project_root}, project-level included"
                )
            return f"any settings member of {self.root}, project-level included"
        return f"any user-level settings member of {self.root}"

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
            return f"{self.basename} is not registered in {self.absence_scope()}"
        why = ", ".join(
            [f"could not be read: {p}" for p in self.members_unreadable]
            + [f"settings shape not modelled: {p}" for p in self.members_unmodelled]
        ) or "no settings member could be read and modelled"
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

    Orthogonal to that verdict, the answer records how far it reached on
    ``project_scope_covered``, computed from what the read actually achieved
    rather than from whether the chain contained the paths. See § Chain
    membership for the three ways a probe misses the project members, and for
    which of the two coverage fields answers which question.
    """
    base = root if root is not None else config_root.harness_config_root()
    project_members = project_settings_chain()
    result = Wiring(
        basename=basename, root=base, status=UNKNOWN, project_root=project_root()
    )
    modelled = True
    for member in settings_chain(base):
        # `Path.is_file()` swallows only ENOENT/ENOTDIR/EBADF/ELOOP
        # (`pathlib._IGNORED_ERRNOS`); EACCES propagates, so a member under a
        # directory this process cannot search raises PermissionError here —
        # and every caller of this module sits under a catch-all that goes
        # quiet, so the raise turns the enforcement-is-OFF banner off with no
        # trace. The ordinary shape is not exotic: an enterprise-deployed
        # /etc/claude-code owned by root and mode 700 is in every chain on the
        # machine, for every normal user. A member whose existence cannot be
        # determined is not absent, it is unaccounted for.
        try:
            on_disk = member.is_file()
        except OSError:
            result.members_unreadable.append(member)
            modelled = False
            continue
        if not on_disk:
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
        if not member_modelled:
            result.members_unmodelled.append(member)
        result.registrations.extend(registrations)
        for event, cmds in found.items():
            result.events.setdefault(event, []).extend(cmds)

    # A project member the filesystem answers not-a-file for WAS accounted for
    # — there is nothing there to register the hook. Anything the probe could
    # not turn into entries was not, whether it failed at the stat, at the JSON
    # or at the shape, so BOTH miss-lists feed the predicate. Reading only
    # `members_unreadable` here is worse than not reading the member at all:
    # the unmodelled file gets opened, skipped, and then certified as covered.
    missed = {
        resolved(member)
        for member in (*result.members_unreadable, *result.members_unmodelled)
    }
    result.project_scope_covered = bool(project_members) and not any(
        resolved(member) in missed for member in project_members
    )

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
