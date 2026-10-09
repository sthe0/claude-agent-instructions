#!/usr/bin/env bash
# Idempotently wire the canonical reminder-hook set into the machine-local
# $CLAUDE_AGENT_HOME/settings.json. Hooks are a machine-specific settings key (see
# apply-settings.sh) — they are NOT merged from settings/base.json, so without
# this installer the reminder-hook scripts that live in the repo stay dead on a
# fresh machine (observed 2026-06-09: hook-resolution-reminder.py documented as
# "Enforced (UserPromptSubmit)" in CLAUDE.md but wired nowhere). Run from
# setup-symlinks.sh and safe to re-run.
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
source "$REPO/scripts/lib/config-root.sh"
SETTINGS="$CLAUDE_AGENT_HOME/settings.json"
# Files that get PRUNE-ONLY treatment: a dangling entry this installer owns is
# removed from them, and a missing or unparseable file is skipped rather than
# created or truncated. Of the DESIRED rows below, only the ones named in
# PRUNE_ONLY_ALSO_ADD are ever added here; everything else reaches
# $CLAUDE_AGENT_HOME alone.
PRUNE_ONLY_SETTINGS=("$HOME/.claude/settings.json")
# The single exemption, and why it is not the rule it appears to break: keeping
# ENFORCEMENT out of the personal root is the design, but a DETECTOR is not
# enforcement — it denies nothing and cannot. Registered only in the agent root,
# hook-canon-guard-wired-check.py can never observe the personal root, which is
# the one root where the gap it reports is real; a check present exclusively in
# the root it never needs to check is the sharpest form of the defect it exists
# to catch. Adding any gate-bearing hook here instead would import enforcement
# into personal sessions, which is deliberately out of scope.
PRUNE_ONLY_ALSO_ADD=("hook-canon-guard-wired-check.py")
command -v python3 >/dev/null || { echo "install-reminder-hooks: python3 required" >&2; exit 1; }

# Ledger-stamp resolution: THIS script's own location, not the canonical $REPO
# above (which may point at a different checkout) — so a worktree copy stamps
# via its own edit_ledger, provable pre-landing.
STAMP_REPO="$(cd "$(dirname "$0")/.." && pwd)"

[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"

SCRIPTS_DIR="$REPO/scripts" STAMP_SCRIPTS_DIR="$STAMP_REPO/scripts" \
PRUNE_ONLY_ALSO_ADD="${PRUNE_ONLY_ALSO_ADD[*]+${PRUNE_ONLY_ALSO_ADD[*]}}" \
python3 - "$SETTINGS" "${PRUNE_ONLY_SETTINGS[@]+"${PRUNE_ONLY_SETTINGS[@]}"}" <<'PY'
import importlib.util
import json, os, shutil, sys
from pathlib import Path

settings_path = sys.argv[1]
prune_only_paths = sys.argv[2:]
scripts = os.environ["SCRIPTS_DIR"]
sys.path.insert(0, os.environ["STAMP_SCRIPTS_DIR"])
from agentctl import edit_ledger

# Reuse self-diagnose.py's own absolute-script resolution so the prune pass
# below and the broken-hook-registration detector agree by construction.
# Loaded from STAMP_SCRIPTS_DIR (this script's own, always-real location)
# rather than SCRIPTS_DIR, which a test may point at a minimal fake repo.
_sd_spec = importlib.util.spec_from_file_location(
    "_install_reminder_self_diagnose",
    os.path.join(os.environ["STAMP_SCRIPTS_DIR"], "self-diagnose.py"),
)
_sd_mod = importlib.util.module_from_spec(_sd_spec)
sys.modules[_sd_spec.name] = _sd_mod
_sd_spec.loader.exec_module(_sd_mod)
_hook_script_path = _sd_mod._hook_script_path

from lib.hook_registry import claude_desired_tuples
# Corpus lives in scripts/hooks/desired.json. Claude-mapped rows only.
DESIRED = claude_desired_tuples()


with open(settings_path, encoding="utf-8") as fh:
    data = json.load(fh)

hooks = data.setdefault("hooks", {})


def basename_of(cmd: str) -> str:
    return os.path.basename((cmd or "").split()[0]) if cmd else ""


def group_for(event_groups, matcher):
    for g in event_groups:
        if (g.get("matcher") or None) == matcher:
            return g
    g = {} if matcher is None else {"matcher": matcher}
    g.setdefault("hooks", [])
    event_groups.append(g)
    return g


def add_rows(hooks, rows, reconcile=False):
    """Register `rows` (DESIRED tuples) in `hooks`, never adding a script whose
    basename is already in the target group. Shared by the full ADD pass and the
    prune-only roots' single-row exemption, so the two cannot drift.

    `reconcile` decides what happens to an entry that IS already there. Without
    it the row is skipped outright, which made this script insert-only: a
    corrected DESIRED timeout could never reach a root that already carried the
    hook, so the fix stayed in the repo and the live registration kept its old
    number forever. With it, an existing entry's `timeout` is brought to the
    DESIRED value.

    Three boundaries, all deliberate. Only `timeout` is reconciled — never
    `command`: an entry with the same basename under a different directory is a
    machine-local choice about WHAT runs, and silently retargeting it is
    qualitatively worse than leaving it slow (the wiring probe reports it as a
    divergence instead). Reconciliation is OFF by default because this function
    also serves the prune-only roots, where touching an entry the installer did
    not put there is exactly what "prune-only" promises not to do; the
    agent-root caller opts in explicitly. And the group a row's basename is
    looked up in is chosen by `group_for(groups, matcher)` keyed on the ROW's
    own matcher — so an existing registration of the same basename under a
    DIFFERENT matcher is a different group entirely, `present` for THIS row's
    group comes back empty, and the row is inserted as a second, correctly
    matchered entry rather than reconciling the first. The stale entry is left
    exactly as it was, forever, on every run: nothing here re-keys a live
    registration onto a new matcher, on the same "never silently retarget"
    reasoning as the command boundary above. Removing it is a manual edit.
    """
    added = []
    reconciled = []
    for event, matcher, script, timeout in rows:
        parts = script.split()
        script_base = os.path.basename(parts[0])
        cmd = os.path.join(scripts, parts[0])
        if len(parts) > 1:
            cmd += " " + " ".join(parts[1:])
        groups = hooks.setdefault(event, [])
        grp = group_for(groups, matcher)
        grp.setdefault("hooks", [])
        present = [h for h in grp["hooks"] if basename_of(h.get("command", "")) == script_base]
        if present:
            if reconcile:
                for hook in present:
                    if hook.get("timeout") != timeout:
                        was = hook.get("timeout")
                        hook["timeout"] = timeout
                        reconciled.append(
                            f"{event}/{matcher or '*'}: {script} timeout {was} -> {timeout}")
            continue
        grp["hooks"].append({"type": "command", "command": cmd, "timeout": timeout})
        added.append(f"{event}/{matcher or '*'}: {script}")
    return added, reconciled


also_add_names = set(os.environ.get("PRUNE_ONLY_ALSO_ADD", "").split())
ALSO_ADD_ROWS = [r for r in DESIRED if os.path.basename(r[2].split()[0]) in also_add_names]
missing_exemptions = also_add_names - {os.path.basename(r[2].split()[0]) for r in ALSO_ADD_ROWS}
if missing_exemptions:
    # A name that matches no DESIRED row would silently add nothing at all.
    sys.exit(f"install-reminder-hooks: PRUNE_ONLY_ALSO_ADD names no DESIRED hook: {sorted(missing_exemptions)}")

changed, reconciled = add_rows(hooks, DESIRED, reconcile=True)


def prune_dangling_managed_hooks(hooks, managed_dir):
    """Remove any hook entry whose resolved script lies under managed_dir
    (this installer's own scripts dir) and no longer exists on disk. An
    entry pointing outside managed_dir — a legitimate machine-local hook —
    is left alone even if dangling, so a hand-wired foreign hook is never
    silently deleted. A group emptied by pruning is dropped."""
    pruned = []
    managed = Path(managed_dir).resolve()
    for event in list(hooks.keys()):
        kept_groups = []
        for grp in hooks[event]:
            kept_hooks = []
            for hk in grp.get("hooks", []) or []:
                cmd = hk.get("command", "")
                script = _hook_script_path(cmd)
                if script is not None:
                    try:
                        resolved = script.resolve()
                    except OSError:
                        resolved = script
                    under_managed = resolved == managed or managed in resolved.parents
                    if under_managed and not script.exists():
                        pruned.append(f"{event}/{grp.get('matcher') or '*'}: {cmd}")
                        continue
                kept_hooks.append(hk)
            grp["hooks"] = kept_hooks
            if grp["hooks"]:
                kept_groups.append(grp)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return pruned


pruned = prune_dangling_managed_hooks(hooks, scripts)

if changed or reconciled or pruned:
    shutil.copy2(settings_path, settings_path + ".bak")
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    edit_ledger.stamp(settings_path, "script:install-reminder-hooks")
    if changed:
        print("install-reminder-hooks: wired " + str(len(changed)) + " hook(s):")
        for c in changed:
            print("  + " + c)
    if reconciled:
        print("install-reminder-hooks: reconciled " + str(len(reconciled)) + " hook timeout(s):")
        for r in reconciled:
            print("  ~ " + r)
    if pruned:
        print("install-reminder-hooks: pruned " + str(len(pruned)) + " dangling hook registration(s):")
        for p in pruned:
            print("  - " + p)
else:
    print("install-reminder-hooks: all canonical reminder hooks already wired")


# Prune-only pass: same ownership predicate (prune_dangling_managed_hooks),
# reused rather than reimplemented. Adds only the ALSO_ADD_ROWS exemption (see
# PRUNE_ONLY_ALSO_ADD above), never the rest of DESIRED; a missing or
# unparseable file is skipped, never created or truncated. It also does NOT
# reconcile timeouts (add_rows' default): an entry already present here is one
# this pass must leave exactly as it found it.
for path_str in prune_only_paths:
    path = Path(path_str)
    if not path.is_file():
        print(f"install-reminder-hooks: {path} not found, skipping prune-only pass", file=sys.stderr)
        continue
    try:
        with open(path, encoding="utf-8") as fh:
            other_data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"install-reminder-hooks: {path} unparseable ({exc}), skipping prune-only pass", file=sys.stderr)
        continue
    if not isinstance(other_data, dict):
        print(f"install-reminder-hooks: {path} is not a JSON object, skipping prune-only pass", file=sys.stderr)
        continue
    other_hooks = other_data.get("hooks")
    # A settings.json with no `hooks` key at all is a COMMON state for a
    # personal root, not an exotic one, and skipping it would leave exactly
    # those roots without the detector forever. The file-level protections are
    # what prune-only promises and they are untouched above: a missing file, an
    # unparseable one and a non-object one are all still skipped. Creating a key
    # inside a file that exists and parses as an object is not creating a file.
    if other_hooks is None and ALSO_ADD_ROWS:
        other_data["hooks"] = {}
        other_hooks = other_data["hooks"]
    if not isinstance(other_hooks, dict):
        continue
    other_pruned = prune_dangling_managed_hooks(other_hooks, scripts)
    other_added, _ = add_rows(other_hooks, ALSO_ADD_ROWS)
    if other_pruned or other_added:
        shutil.copy2(path, str(path) + ".bak")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(other_data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        edit_ledger.stamp(str(path), "script:install-reminder-hooks")
        if other_pruned:
            print(f"install-reminder-hooks: pruned {len(other_pruned)} dangling hook registration(s) in {path}:")
            for p in other_pruned:
                print("  - " + p)
        if other_added:
            print(f"install-reminder-hooks: wired {len(other_added)} detector hook(s) in {path}:")
            for a in other_added:
                print("  + " + a)
PY
