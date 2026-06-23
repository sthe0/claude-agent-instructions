#!/usr/bin/env bash
# Idempotently wire the canonical reminder-hook set into the machine-local
# ~/.claude/settings.json. Hooks are a machine-specific settings key (see
# apply-settings.sh) — they are NOT merged from settings/base.json, so without
# this installer the reminder-hook scripts that live in the repo stay dead on a
# fresh machine (observed 2026-06-09: hook-resolution-reminder.py documented as
# "Enforced (UserPromptSubmit)" in CLAUDE.md but wired nowhere). Run from
# setup-symlinks.sh and safe to re-run.
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
SETTINGS="$HOME/.claude/settings.json"
command -v python3 >/dev/null || { echo "install-reminder-hooks: python3 required" >&2; exit 1; }

[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"

SCRIPTS_DIR="$REPO/scripts" python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys

settings_path = sys.argv[1]
scripts = os.environ["SCRIPTS_DIR"]

# (event, matcher-or-None, script-basename [+ optional args], timeout)
DESIRED = [
    ("UserPromptSubmit", None,    "hook-context-growth-reminder.py", 5),
    ("UserPromptSubmit", None,    "hook-resolution-reminder.py",     5),
    ("UserPromptSubmit", None,    "hook-tracker-reminder.py",        5),
    ("PreToolUse",       "Bash",  "hook-push-confirmation-reminder.py", 5),
    ("PreToolUse",       "Bash",  "hook-readme-currency-reminder.py", 5),
    ("PreToolUse",       "Edit|Write", "hook-prewrite-plan-check.py", 5),
    ("PreToolUse",       "Edit|Write", "hook-state-gate.py",          5),
    ("PreToolUse",       "Bash",  "hook-retry-detector.py",          5),
    ("PostToolUse",      "Write", "hook-self-critique-reminder.py",  5),
    ("SessionStart",     None,    "hook-policy-scorecard-due.py",    5),
    # Difficulty/confirmation gates on memory-leaf Writes. These run on ANY
    # Write (any repo), so they are the only enforcement point for project
    # memory (whose own git pre-commit does not run verify-all).
    ("PreToolUse",       "Write", "verify-difficulty-lead.py --hook", 5),
    ("PreToolUse",       "Write", "verify-experience-leaf.py --hook", 5),
]

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


changed = []
for event, matcher, script, timeout in DESIRED:
    parts = script.split()
    script_base = os.path.basename(parts[0])
    cmd = os.path.join(scripts, parts[0])
    if len(parts) > 1:
        cmd += " " + " ".join(parts[1:])
    groups = hooks.setdefault(event, [])
    grp = group_for(groups, matcher)
    grp.setdefault("hooks", [])
    if any(basename_of(h.get("command", "")) == script_base for h in grp["hooks"]):
        continue
    grp["hooks"].append({"type": "command", "command": cmd, "timeout": timeout})
    changed.append(f"{event}/{matcher or '*'}: {script}")

if changed:
    shutil.copy2(settings_path, settings_path + ".bak")
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("install-reminder-hooks: wired " + str(len(changed)) + " hook(s):")
    for c in changed:
        print("  + " + c)
else:
    print("install-reminder-hooks: all canonical reminder hooks already wired")
PY
