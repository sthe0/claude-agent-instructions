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
command -v python3 >/dev/null || { echo "install-reminder-hooks: python3 required" >&2; exit 1; }

[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"

SCRIPTS_DIR="$REPO/scripts" python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys

settings_path = sys.argv[1]
scripts = os.environ["SCRIPTS_DIR"]

# (event, matcher-or-None, script-basename [+ optional args], timeout)
DESIRED = [
    ("UserPromptSubmit", None,    "hook-context-growth-reminder.py", 5),
    ("UserPromptSubmit", None,    "hook-engine-start.py",            5),
    ("UserPromptSubmit", None,    "hook-resolution-reminder.py",     5),
    ("UserPromptSubmit", None,    "hook-self-improvement-reminder.py", 5),
    ("UserPromptSubmit", None,    "hook-tracker-reminder.py",        5),
    ("UserPromptSubmit", None,    "hook-tracker-publish-reminder.py", 5),
    ("UserPromptSubmit", None,    "hook-ticket-plan-sync.py",        5),
    ("UserPromptSubmit", None,    "hook-experience-record-reminder.py", 5),
    ("PreToolUse",       "Bash",  "hook-push-confirmation-reminder.py", 5),
    ("PreToolUse",       "Bash",  "hook-readme-currency-reminder.py", 5),
    ("PreToolUse",       "Edit|Write", "hook-memory-consistency.py",         5),
    ("PreToolUse",       "Edit|Write", "hook-prewrite-plan-check.py", 5),
    ("PreToolUse",       "Edit|Write", "hook-state-gate.py",          5),
    # Hard gate: deny a plan-approval AskUserQuestion issued the same turn the
    # plan was submitted — pre-tool-call text may never render, so the click-
    # question would arrive with nothing behind it ("Я не вижу плана").
    ("PreToolUse",       "AskUserQuestion", "hook-plan-delivery-gate.py", 5),
    # General text-then-buttons gate: deny ANY AskUserQuestion preceded by
    # substantive same-turn assistant text (>200 chars) — that text may never
    # render; deliver it as the turn's final message and ask next turn.
    ("PreToolUse",       "AskUserQuestion", "hook-ask-text-split.py", 5),
    # Pre-emptive primary gate: deny an AskUserQuestion that escalates an external-
    # service failure to the user WITHOUT a recorded diagnosis (present-tense outage
    # cue + user-facing ask, and neither overcome-difficulty invoked nor a declared
    # difficulty). Reproduce with the real client + enumerate hypotheses first.
    ("PreToolUse",       "AskUserQuestion", "hook-escalation-diagnosis-gate.py", 5),
    # session_scope: deny/warn on a LIVE cross-session filesystem-scope overlap
    # (Component B wiring). Runs AFTER the plan-approval gate above; blocks only a
    # gated path already held by another live session, otherwise warns — silent
    # single-session (isolate, don't serialize).
    ("PreToolUse",       "Edit|Write", "hook-scope-conflict.py",      5),
    ("PreToolUse",       "Bash",  "hook-retry-detector.py",          5),
    # Advisory determinization nudges (never block): arm long-job monitoring,
    # prefer a domain Skill over hand-rolled CLI, reply in the user's language.
    ("PreToolUse",       "Bash",  "hook-long-job-arm.py",            5),
    ("PreToolUse",       "Bash",  "hook-skill-first.py",             5),
    ("UserPromptSubmit", None,    "hook-language-reminder.py",       5),
    # Daily proactive OFFER to refresh Core + project-layer instructions (replaces the
    # silent 10-min auto-pull cron/timer). Higher timeout: up to two bounded git fetches.
    ("UserPromptSubmit", None,    "hook-instructions-refresh-due.py", 10),
    # Proactive OFFER (per-file debounced) to run the instruction-grooming skill once
    # a governed file crosses lint-prose-length.py's 90% WARN threshold.
    ("UserPromptSubmit", None,    "hook-instruction-grooming-due.py", 5),
    ("PreToolUse",       "Bash|Grep|Glob", "hook-arc-mount-search-guard.py", 5),
    # Hard gate: deny a recursive rm that (worst-case, with any empty $VAR) targets
    # /, $HOME, ~/.claude, or the instruction repo — the agent's own memory/config.
    ("PreToolUse",       "Bash",  "hook-guard-destructive-rm.py",    5),
    # Hard gate: deny an Edit/Write or `git commit` in canon (the serving/PRIMARY
    # Core checkout, on ANY branch, plus any machine-local canon-roots entry) —
    # feature work must go in a linked worktree or second mount, so live hooks
    # stay deterministic. Fail-open otherwise.
    ("PreToolUse",       "Edit|Write", "hook-guard-canon-readonly.py", 5),
    ("PreToolUse",       "Bash",  "hook-guard-canon-readonly.py", 5),
    ("PostToolUse",      "Write", "hook-self-critique-reminder.py",  5),
    # Nudge when AskUserQuestion times out: any answer written mid-turn must be
    # restated in the turn's FINAL message or it never reaches the user.
    ("PostToolUse",      "AskUserQuestion", "hook-answer-delivery-reminder.py", 5),
    # Nudge when an AskUserQuestion answer is free text rather than an offered
    # option label: a correction delivered this way bypasses the
    # UserPromptSubmit self-improvement reminder, which only sees prompts.
    ("PostToolUse",      "AskUserQuestion", "hook-si-freetext-answer.py", 5),
    # session_scope: heartbeat + touched-path accumulation (Component A wiring).
    # Non-blocking by design — never emits a permissionDecision.
    ("PostToolUse",      "Edit|Write", "hook-scope-track.py",        5),
    ("PostToolUse",      "Bash",  "hook-scope-track.py",             5),
    ("SessionStart",     None,    "hook-policy-scorecard-due.py",    5),
    # Throttled nudge (once/7d): runs budget-calibration.py --check and speaks only
    # when a spawn budget tier looks miscalibrated against realized spend, routing
    # to self-improvement to adjust the config.md tier values. Fail-open, never blocks.
    ("SessionStart",     None,    "hook-budget-calibration-due.py",  10),
    ("SessionStart",     None,    "hook-sigma-sentinel-due.py",  5),
    # Standing proactive self-diagnosis: run self-diagnose.py's read-only scan
    # for self-friction (oversized memory index, dangling pointer, instruction
    # file near its ceiling) and surface any worklist to stderr. Self-throttled,
    # fail-open — never blocks or slows session start.
    ("SessionStart",     None,    "hook-self-diagnose-due.py",   5),
    # End-of-turn GATE (not advisory): a loop-safe shell running a registry of
    # pure turn-boundary guardians. Blocks a stop when any guardian reports an
    # unmet obligation (today: the last user message carried an agent-behavior-
    # feedback signal but neither self-improvement nor overcome-difficulty was
    # engaged this turn). Loop-guarded (stop_hook_active + a durable per-message
    # marker under state/turn-gate/) and blockers from every guardian aggregate
    # into one block, so the worst case is exactly one extra model turn.
    ("Stop",             None,    "hook-turn-end-gate.py",   5),
    # Structure/confirmation gates on memory-leaf Writes. These run on ANY
    # Write (any repo), so they are the only enforcement point for project
    # memory (whose own git pre-commit does not run verify-all).
    ("PreToolUse",       "Write", "verify-leaf-structure.py --hook", 5),
    ("PreToolUse",       "Write", "verify-experience-leaf.py --hook", 5),
    # Reject a Write that would carry a git conflict marker into any file.
    ("PreToolUse",       "Write", "verify-no-conflict-markers.py --hook", 5),
    # Stop hook: warn when a turn promises to ask via buttons "next message" but
    # never arms the `sleep 2` background timer that opens that next turn.
    ("Stop",             None,    "hook-ask-defer-timer.py",        5),
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
