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

The name is narrower than the job. It is kept deliberately: renaming ripples
into install-reminder-hooks.sh, into live settings on two roots, and into the
config-root-refs allowlist, for zero functional gain.

REPORT, NEVER REPAIR. The divergence between the two roots is by design, and
this check must not "fix" it. install-reminder-hooks.sh is the in-repo evidence
for that reading rather than a preference of this file's:
``PRUNE_ONLY_SETTINGS=("$HOME/.claude/settings.json")`` — the installer's ADD
pass never writes to the personal root; it only ever prunes stale entries from
it. Reporting the gap is therefore what the installer's own intent asks for.

Scope of the warning: gate-bearing hooks only. Warning about the advisory
``*-due`` reminders missing from a personal root would fire every session on a
deliberate divergence and train the reader to ignore the whole block.

Non-blocking and fail-open by construction: this is a SessionStart hook, which
cannot hard-block, and any error (missing/unreadable settings, malformed JSON)
returns quietly — a detector that wedges session start would be worse than the
gap it reports. It only ever writes to stderr; it never denies. UNKNOWN is
never reported for the same reason: only a positively established ABSENT is
worth the reader's attention.

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
    """
    problems: "list[str]" = []
    for basename, consequence in hook_wiring.GATE_BEARING_HOOKS:
        if basename == GUARD_BASENAME:
            continue
        wiring = hook_wiring.probe(basename, root)
        if wiring.status == hook_wiring.ABSENT:
            problems.append(f"{basename} NOT registered — {consequence}")
    return problems


def main() -> int:
    try:
        primary = _primary_settings_path()
        if _load(primary) is None:
            return 0
        root = primary.parent
        chain = hook_wiring.settings_chain(root)
        problems = check_guard_chains(_pretooluse_groups(chain))
        problems += check_registry(root)
    except Exception:
        return 0

    if problems:
        print(
            "\n"
            "================================================================\n"
            "  GATE-BEARING HOOKS ARE NOT FULLY WIRED — enforcement is OFF\n"
            f"  harness config root: {root}\n"
            "================================================================",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "  This root is the one THIS session loads hooks from. It may differ\n"
            "  from the root the system installs into, and that divergence is by\n"
            "  design — so this is a report, not something to auto-repair.\n"
            "  If this root is meant to carry the system's hooks, install them:\n"
            "    bash ~/claude-agent-instructions/scripts/install-reminder-hooks.sh\n"
            "================================================================",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
