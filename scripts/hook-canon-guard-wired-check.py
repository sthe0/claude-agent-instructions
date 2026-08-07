#!/usr/bin/env python3
"""SessionStart detector: are the gate-bearing hooks actually wired live?

Difficulty removed: a hook can be present in the repo and declared in
install-reminder-hooks.sh yet completely DEAD in a session, because the
installer historically ran only on the one-time legacy-migration path — so a
hook added after this machine was onboarded never reached live settings and
NOTHING detected the absence. A guard that silently does not enforce is the
worst failure mode there is: canon looks protected but isn't; the delivery gate
demands a stamp whose only producer was never running, and blames whoever hit
it. This check closes the whole "versioned-but-not-applied" class for every
GATE-BEARING hook — the ones whose absence disables enforcement rather than
dropping an advisory (``lib/hook_wiring.GATE_BEARING_HOOKS``).

WHICH ROOT. It probes the root the HARNESS loads from
(``config_root.harness_config_root()``), not the root the system installs into
(``agent_home()``). Asking the wrong one is how this check used to report a
confident all-clear: under a bare ``claude`` on a migrated machine it read
``~/.claude-agent``, where everything is correctly wired, while the session's
hooks were being loaded from ``~/.claude``, where none of them are. A green
report from a root that is not the one enforcing anything is worse than no
report.

TWO AXES, TWO POLARITIES. Presence is one axis; the registered TIMEOUT is the
other, and it was invisible until a hook whose judge needs 10.5-47s was found
registered at 5s — wired, green to every presence probe, and killed mid-judge on
every call. The SessionStart path reports both (fail-open, established problems
only). The one-shot ``--check-timeouts`` mode answers the same timeout question
FAIL-CLOSED: a problem, an UNKNOWN, or an exception inside the check all exit
non-zero. A check that shares this file's "return 0 on every path" posture would
certify exactly the silence it exists to break.

The name is narrower than the job. It is kept deliberately: renaming ripples
into install-reminder-hooks.sh, into live settings on two roots, and into the
config-root-refs allowlist, for zero functional gain.

REPORT, NEVER REPAIR. The divergence between the two roots is by design, and
this check must not "fix" it. install-reminder-hooks.sh is the in-repo evidence
for that reading rather than a preference of this file's, and the reading is
narrower than it used to be: the installer's ADD pass writes every ENFORCEMENT
hook to the agent root only and gives ``$HOME/.claude/settings.json`` prune-only
treatment — with exactly one exemption, this detector, which it does add there
(``PRUNE_ONLY_ALSO_ADD``). Enforcement never, detection always. A detector
denies nothing and cannot; registered only in the root that is always correctly
wired, it could never observe the one root where the gap is real. Reporting the
gap is what the installer's own intent asks for; repairing it is not.

WHICH BRANCH. What this check says depends on which root the harness loaded:

- harness root IS the agent root — the full report, unchanged. Absence there is
  a real defect and the alarm is correct.
- harness root is the PERSONAL root — the gate-bearing hooks are absent BY
  DESIGN and nobody will ever "fix" it, so the banner would fire every session
  on a deliberate divergence, which is precisely what § Scope below rejects.
  Instead: one quiet line, and only when the session is doing SYSTEM work
  (``in_system_work_venue()``). A personal session in a personal root has no
  defect to hear about, and gets absolute silence on both channels.

Scope of the warning: gate-bearing hooks only. Warning about the advisory
``*-due`` reminders missing from a personal root would fire every session on a
deliberate divergence and train the reader to ignore the whole block. The
unconditional ``[config-root]`` status line is not such a warning and this
paragraph does not argue against it: it carries no remediation and nothing to
fix, so it is a datum the reader consults rather than an alert the reader obeys,
and being tuned out costs a datum nothing. It is also scoped BY the branch split
above rather than bolted outside it, so the silence that split buys is not spent.

Non-blocking and fail-open by construction: this is a SessionStart hook, which
cannot hard-block, and any error (missing/unreadable settings, malformed JSON)
returns quietly — a detector that wedges session start would be worse than the
gap it reports. It writes to STDOUT and never denies. The channel is the point:
a SessionStart hook's stdout is attached to the session as context, stderr
reaches only the human's terminal, and the one reader who can act on "the gates
are not wired in this root" is the agent about to write to a gated file.
UNKNOWN is never reported, for the same reason the scope is narrow: only a
positively established ABSENT is worth the reader's attention.

Settings source: the chain of the harness root, overridable via
``$CLAUDE_CANON_GUARD_SETTINGS`` (test seam). The override designates the
chain's PRIMARY member; the remaining members are derived from that file's
parent directory, which is what lets the seam stay file-valued while the chain
is modelled rather than assumed to be one file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402
from lib import hook_wiring  # noqa: E402

GUARD_BASENAME = "hook-guard-canon-readonly.py"

# Marks a checkout of the instruction repo — the canonical one and every linked
# worktree of it alike.
SYSTEM_WORK_SENTINEL = Path("scripts") / "agentctl" / "machine.py"


def in_system_work_venue(cwd=None) -> bool:
    """Is this session working on the agent system itself?

    The signal is STRUCTURAL — a tree carrying ``scripts/agentctl/machine.py``
    at or above the cwd — rather than a prefix test against a literal checkout
    path. A prefix test would go permanently silent in a delivery WORKTREE,
    which is where this fleet does most of its system work, and silence is the
    one failure this branch exists to end.
    """
    try:
        start = (Path(cwd) if cwd is not None else Path.cwd()).resolve()
    except OSError:
        return False
    for candidate in (start, *start.parents):
        if (candidate / SYSTEM_WORK_SENTINEL).is_file():
            return True
    return False


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - unresolvable path, compare the raw one
        return path


def _primary_settings_path() -> Path:
    override = os.environ.get("CLAUDE_CANON_GUARD_SETTINGS")
    if override:
        return Path(override).expanduser()
    return config_root.harness_config_root() / "settings.json"


def _load(path: Path) -> "dict | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _pretooluse_groups(chain: "list[Path]") -> list:
    """Every PreToolUse group across the readable chain members.

    The canon guard's requirement is finer-grained than "is it wired at all":
    it must be in BOTH the Edit|Write and the Bash chain, and matchers are
    exactly what ``hook_wiring`` deliberately does not model. So this hook keeps
    its own matcher-aware pass over the merged groups.
    """
    groups: list = []
    for member in chain:
        data = _load(member) if member.is_file() else None
        if data is None:
            continue
        hooks = data.get("hooks") or {}
        if not isinstance(hooks, dict):
            continue
        pre = hooks.get("PreToolUse")
        if isinstance(pre, list):
            groups.extend(pre)
    return groups


def _guard_commands_for(groups: list, matcher_needle: str) -> "list[str]":
    """Every wired command in the PreToolUse groups whose matcher contains
    `matcher_needle` and whose command runs the canon guard."""
    out: "list[str]" = []
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        if matcher_needle not in (grp.get("matcher") or ""):
            continue
        for hook in grp.get("hooks", []) or []:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command", "") or ""
            if GUARD_BASENAME in cmd:
                out.append(cmd)
    return out


def _script_path(command: str) -> str:
    """The script path a hook command runs (shared with lib/hook_wiring so the
    two agree on what counts as 'the script' when an interpreter prefixes it)."""
    return hook_wiring.script_path(command)


def check_guard_chains(groups: list) -> "list[str]":
    """Problems specific to the canon guard: absent from the Edit|Write chain,
    absent from the Bash chain, or wired to a script path that does not exist."""
    problems: "list[str]" = []
    edit_cmds = _guard_commands_for(groups, "Edit")
    bash_cmds = _guard_commands_for(groups, "Bash")
    if not edit_cmds:
        problems.append("canon guard NOT wired in the PreToolUse Edit|Write chain")
    if not bash_cmds:
        problems.append("canon guard NOT wired in the PreToolUse Bash chain")
    for cmd in edit_cmds + bash_cmds:
        path = _script_path(cmd)
        if path and not os.path.exists(path):
            problems.append(f"canon guard wired to a missing script path: {path}")
    return problems


def check_registry(root: Path) -> "list[str]":
    """Gate-bearing hooks positively established as ABSENT from `root`'s chain.

    UNKNOWN is skipped, never reported: an unreadable member or an unmodelled
    settings shape is not evidence of absence, and a false alarm here would
    train the reader to ignore the real ones. The canon guard is excluded — it
    gets the finer per-chain treatment above.

    The line comes from ``Wiring.describe()`` rather than being composed here,
    for the reason that method exists: how far an ABSENT reaches depends on
    which members the probe got to, and this is the one caller a human reads
    every session. A bare "NOT registered" means one thing on a machine where
    the project member was read and a weaker thing where it was not, with no
    way for the reader to tell the two runs apart.
    """
    problems: "list[str]" = []
    for basename, consequence in hook_wiring.GATE_BEARING_HOOKS:
        if basename == GUARD_BASENAME:
            continue
        wiring = hook_wiring.probe(basename, root)
        if wiring.status == hook_wiring.ABSENT:
            problems.append(f"{wiring.describe()} — {consequence}")
    return problems


def check_timeout_axis(root: Path, *, strict: bool) -> "list[str]":
    """Problems on the TIMEOUT axis for every hook in
    ``hook_wiring.TIMEOUT_REQUIREMENTS``: a registration below the hook's own
    judge budget, and — under `strict` — a registration with no readable
    timeout or a hook that is not positively WIRED at all.

    `strict` is the polarity switch, and the two callers want opposite things.
    The SessionStart path is advisory and fail-open: it reports only what is
    positively established, because a false alarm every session trains the
    reader to skip the block that carries the real ones. The one-shot
    ``--check-timeouts`` path is a CHECK: there, "cannot be established" is a
    failure, since the whole point is to refuse to certify what it could not
    read.

    The checked set is TIMEOUT_REQUIREMENTS, not GATE_BEARING_HOOKS: a hook
    needs a generous registration because it calls a slow judge, which is a
    different property from being gate-bearing.
    """
    problems: "list[str]" = []
    for basename, minimum, why in hook_wiring.TIMEOUT_REQUIREMENTS:
        wiring = hook_wiring.probe(basename, root)
        if wiring.status != hook_wiring.WIRED:
            if strict:
                problems.append(f"{wiring.describe()} — needs {minimum}s: {why}")
            continue
        problems += hook_wiring.timeout_shortfalls(wiring, minimum)
        if strict:
            problems += hook_wiring.timeout_unknowns(wiring)
        note = hook_wiring.duplicate_registration_note(wiring)
        # Advisorily, only a pair that genuinely double-executes is worth the
        # banner; a pair the harness deduplicates is a look-at-this for the
        # reconciler, not enforcement being off. The strict caller wants both,
        # since a second entry is a second timeout to keep in step.
        if note and (strict or hook_wiring.runs_more_than_once(wiring)):
            problems.append(note)
    return problems


def check_timeouts_main() -> int:
    """One-shot ``--check-timeouts``: is every judge-calling hook wired with a
    timeout that actually allows its judge to finish?

    FAIL-CLOSED, the exact opposite of the SessionStart path below, and
    deliberately so: they share this file and would otherwise share main()'s
    "return 0 on every path", which is right for a hook that must never wedge
    session start and catastrophic for a check whose only job is to say no. A
    problem, an UNKNOWN, and an exception inside the check all exit non-zero.
    """
    try:
        root = _primary_settings_path().parent
        problems = check_timeout_axis(root, strict=True)
    except Exception as exc:  # fail-CLOSED: an unrunnable check certifies nothing
        print(f"[check-timeouts] the check itself failed: {exc!r}")
        return 2
    if problems:
        print(f"[check-timeouts] FAIL — harness config root: {root}")
        for p in problems:
            print(f"  - {p}")
        print(
            "  Install to reconcile the registered timeouts:\n"
            "    bash ~/claude-agent-instructions/scripts/install-reminder-hooks.sh\n"
            "  Then RELOAD the config (open /hooks, or restart) — the harness\n"
            "  captures its hook set once at session start.\n"
            "  This reconciles a registration ONLY under the (event, matcher)\n"
            "  group install-reminder-hooks.sh's DESIRED table wires that hook to.\n"
            "  A registration listed above under a DIFFERENT matcher is a group\n"
            "  the installer never touches (add_rows()'s own boundary) — remove it\n"
            "  by hand instead."
        )
        return 1
    print(f"[check-timeouts] OK — harness config root: {root}")
    for basename, minimum, _ in hook_wiring.TIMEOUT_REQUIREMENTS:
        print(f"  - {basename}: every registration >= {minimum}s")
    return 0


def main() -> int:
    if "--check-timeouts" in sys.argv[1:]:
        return check_timeouts_main()
    try:
        harness = config_root.harness_config_root()
        home = config_root.agent_home()
        personal = _resolved(harness) != _resolved(home)
        # The branch decision precedes the first print, or the personal-root
        # non-system-work path loses the byte-silence it is owed to the very
        # line being added.
        if personal and not in_system_work_venue():
            return 0
        # Above the settings read on purpose: which root is live does not depend
        # on that file parsing, so an unreadable settings.json makes the wiring
        # REPORT unknown but never the ROOT. `harness`, not `primary.parent` —
        # the two diverge under the $CLAUDE_CANON_GUARD_SETTINGS seam, and a
        # status line naming the wrong root is worse than none.
        suffix = f"!= agent home {home}" if personal else "= agent home"
        print(f"[config-root] harness={harness} ({suffix})")
        if personal:
            print(
                "  The agent's gate-bearing hooks are not wired in this root. That is\n"
                "  EXPECTED here — the system installs into its own root by design.\n"
                "  For system work use `claude-agent` (or `claude-task`), not a bare `claude`."
            )
            return 0

        primary = _primary_settings_path()
        if _load(primary) is None:
            return 0
        root = primary.parent
        chain = hook_wiring.settings_chain(root)
        problems = check_guard_chains(_pretooluse_groups(chain))
        problems += check_registry(root)
        problems += check_timeout_axis(root, strict=False)
    except Exception:
        return 0

    if problems:
        print(
            "\n"
            "================================================================\n"
            "  GATE-BEARING HOOKS ARE NOT FULLY WIRED — enforcement is OFF\n"
            f"  harness config root: {root}\n"
            "================================================================"
        )
        for p in problems:
            print(f"  - {p}")
        print(
            "  This root is the one THIS session loads hooks from. It may differ\n"
            "  from the root the system installs into, and that divergence is by\n"
            "  design — so this is a report, not something to auto-repair.\n"
            "  If this root is meant to carry the system's hooks, install them:\n"
            "    bash ~/claude-agent-instructions/scripts/install-reminder-hooks.sh\n"
            "================================================================"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
