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
    # Advisory (never blocks): warn when a Write/Edit would introduce a hit
    # against a discovered term ruleset (C1 org-neutrality mechanism).
    ("PreToolUse",       "Edit|Write", "hook-term-neutrality.py",     5),
    # Hard gate: deny a plan-approval AskUserQuestion issued the same turn the
    # plan was submitted — pre-tool-call text may never render, so the click-
    # question would arrive with nothing behind it ("Я не вижу плана").
    # 35 = the hook's own _APPROVAL_ASK_JUDGE_BUDGET_S=30 plus interpreter-start
    # headroom, the same shape as the three gates below. The previous 18 was
    # sized off approval-sample.json alone (n=32, max 11.42s); a second n=32
    # sample taken after production started recording timed_out:true rows
    # against that ceiling (approval2-sample.json) ran 14.12-19.14s, entirely
    # above the first sample's max, so the merged population's own ceiling
    # (lib/judge_latency.py, approval_ask row) moved to 21s and this
    # registration is sized off the hook's new 30s budget instead.
    ("PreToolUse",       "AskUserQuestion", "hook-plan-delivery-gate.py", 35),
    # Pre-emptive primary gate: deny an AskUserQuestion that escalates an external-
    # service failure to the user WITHOUT a recorded diagnosis (present-tense outage
    # cue + user-facing ask, and neither overcome-difficulty invoked nor a declared
    # difficulty). Reproduce with the real client + enumerate hypotheses first.
    # 35 = the hook's own _JUDGE_BUDGET_S=30 plus interpreter-start headroom, the
    # same shape as the deferring gate below; at 5 the harness killed the hook
    # mid-judge on every single call, and the superseded 25 still sat below this
    # judge's own p90 (19.16s over n=16, lib/judge_latency.py) once the hook's
    # budget was raised to cover it.
    ("PreToolUse",       "AskUserQuestion", "hook-escalation-diagnosis-gate.py", 35),
    # Hard gate: deny an AskUserQuestion whose EVERY option defers or refuses work
    # the agent holds the rights and the diagnosis to do now (ticket / backlog /
    # "leave as is"), with no branch that does it and no stated reason it cannot.
    # 50 = hook-deferring-disposition-gate.py's own _ASK_JUDGE_BUDGET_S=45 plus
    # interpreter-start headroom. The superseded 25 came from a four-run note
    # ("11.6-13.5s"); over n=18 this judge's median is 17.43s and its p90 37.58s
    # (lib/judge_latency.py), so the harness cap was binding below the hook's own
    # decide() deadline and killing the call before any verdict came back.
    ("PreToolUse",       "AskUserQuestion", "hook-deferring-disposition-gate.py", 50),
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
    ("PreToolUse",       "Bash|Grep|Glob", "hook-multi-mount-search-guard.py", 5),
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
    # Nudge when an AskUserQuestion answer is free text rather than an offered
    # option label: a correction delivered this way bypasses the
    # UserPromptSubmit self-improvement reminder, which only sees prompts.
    ("PostToolUse",      "AskUserQuestion", "hook-si-freetext-answer.py", 5),
    # session_scope: heartbeat + touched-path accumulation (Component A wiring).
    # Non-blocking by design — never emits a permissionDecision.
    ("PostToolUse",      "Edit|Write", "hook-scope-track.py",        5),
    ("PostToolUse",      "Bash",  "hook-scope-track.py",             5),
    # Autopilot for the review-mergeable mechanism: when one Bash call both
    # carried a review-create verb and printed that review's URL, launch the
    # detached poller for it — the ACT the Stop guardian below can only advise.
    # Silent no-op unless `review_probe=` is configured (Core ships no probe).
    # Spawns and returns immediately, so a short timeout is the right bound.
    ("PostToolUse",      "Bash",  "hook-review-monitor-arm.py",      5),
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
    # Fail-loud detector: the gate-bearing hooks are present in the repo but NOT
    # wired into the root THIS session loads from — i.e. canon may silently be
    # writable and the spine's gates silently off. Reports on stdout, so the
    # agent reads it and not only the terminal; states the live root on every
    # path, so its silence stops being the only signal. Non-blocking, fail-open.
    # The one row PRUNE_ONLY_ALSO_ADD also installs into the personal root — see
    # the comment there for why a detector is not the enforcement it sits beside.
    ("SessionStart",     None,    "hook-canon-guard-wired-check.py", 5),
    # Phase-3 forcing trigger: throttled (7d), speaks only when
    # rule-salience-report.py's phase3_readiness predicate says the deferred
    # instruction-surface compression phase is DUE (pressure + data-sufficiency
    # + reclaimable all satisfied). Establishes its own write-once baseline
    # stamp; never reimplements the predicate. Fail-open, never blocks.
    ("SessionStart",     None,    "hook-phase3-due.py",  10),
    # End-of-turn GATE (not advisory): a loop-safe shell running a registry of
    # pure turn-boundary guardians. Blocks a stop when any guardian reports an
    # unmet obligation (today: the last user message carried an agent-behavior-
    # feedback signal but neither self-improvement nor overcome-difficulty was
    # engaged this turn). Loop-guarded (stop_hook_active + a durable per-message
    # marker under state/turn-gate/) and blockers from every guardian aggregate
    # into one block, so the worst case is exactly one extra model turn.
    # 57 = the hook's own _TURN_JUDGE_BUDGET_S=52 plus interpreter-start headroom.
    # It runs up to THREE judges in one invocation, so its whole-invocation budget
    # is larger than the single-judge gates'; at 5 every one of them was killed,
    # and the superseded 30/35 pair covered barely two of the three medians
    # (11.86 + 7.46 + 10.89, lib/judge_latency.py) before the outage judge's own
    # floor was reached.
    ("Stop",             None,    "hook-turn-end-gate.py",   57),
    # Advisory (not a gate): nudge when a launched run/graph URL appeared in
    # this session's tool output but was never surfaced to the user in a chat
    # message — the structural guard for CLAUDE.md long-running-jobs /
    # outcome-format point 3 (recurring miss recorded 2026-07-28). Fail-open,
    # exit 0 always.
    ("Stop",             None,    "hook-run-url-surfaced-reminder.py", 5),
    # Advisory (not a gate): nudge when a review this session AUTHORED (a create
    # verb in a Bash command + its URL in tool output) has no monitor armed in
    # the monitored-reviews registry — the structural guard for
    # leaves/review-accompanies-code.md (the author drives an open review to
    # mergeable unprompted) and leaves/long-job-monitoring.md § Generalization
    # (the zero-token mechanism). Fail-open, exit 0 always.
    ("Stop",             None,    "hook-review-mergeable-guardian.py", 5),
    # Structure/confirmation gates on memory-leaf Writes. These run on ANY
    # Write (any repo), so they are the only enforcement point for project
    # memory (whose own git pre-commit does not run verify-all).
    ("PreToolUse",       "Write", "verify-leaf-structure.py --hook", 5),
    ("PreToolUse",       "Write", "verify-experience-leaf.py --hook", 5),
    # Reject a Write that would carry a git conflict marker into any file.
    ("PreToolUse",       "Write", "verify-no-conflict-markers.py --hook", 5),
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
