#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge the agent to ask for explicit resolution
when the user's prompt is brief gratitude — ambiguous between "thanks
for the work" and "task is over".

At the resolution gate, also checks whether the working branch is
cleanly landable into trunk (`land-branch.py --check`) and, if so,
appends a branch-hygiene line so a confirmed-resolved task doesn't
leave an unmerged branch behind. The check is best-effort and silent
on any failure — it never blocks or alters the gate nudge itself.

Safety net for the prose rule in CLAUDE.md § On task resolution:
the agent should close substantive tasks proactively when the plan's
`## Final verification` has passed (recap + explicit ask). If that
proactive close was missed and the user replies with bare gratitude,
this hook prevents the agent from treating "спасибо" / "thanks" as
silent confirmation.

Recurring failure mode this addresses (see experience leaf
2026-05-25-resolution-gate-confirm-before-record): agent finishes
work, user thanks, agent closes without writing the experience leaf
or asking for resolution.

Matches either:
  (a) Brief gratitude: ≤ MAX_WORDS tokens AND a gratitude keyword.
  (b) Resolution meta-question: ≤ META_MAX_WORDS tokens AND a gratitude
      keyword AND a meta-keyword about asking / being resolved / done.
      Catches prompts like "спасибо, почему не спрашиваеш решена ли
      задача?" where the user explicitly reminds the agent of the gate
      but the prompt is too long for (a).

Detection is intentionally permissive — false positives (extra
reminder when the user is fine) are cheap; false negatives (silent
miss of a resolution gate) are expensive.

State-aware path: when an agentctl session is being driven and the
engine is parked at the resolution gate (node == RESOLUTION and
resolution.passed is falsy), the nudge fires regardless of the
user's phrasing — the gate is objectively open, so the agent must
not close without an explicit confirmation. Sessions with no state
file fall back to the gratitude/meta heuristics above (prose mode).

PreToolUse/AskUserQuestion branch (this module's second registration):
the UserPromptSubmit path above only ever runs when a NEW user message
arrives while the gate is open — when the coordinator drives straight
from a passing verify-final into composing its own resolution ask
within one turn, no user prompt intervenes and DIRECT_PUSH_NO_PR_HINT
never gets a chance to steer the menu before it is already shown. This
second entry point runs on every AskUserQuestion tool call instead: when
the gate is open AND direct_push_no_pr_hint applies to the delivery
repo, it consults the semantic judge
agentctl.advisor.judge_landing_discipline_ask on the ask's own text —
unconditionally, with NO regex/content-based prefilter gating whether
the judge is consulted (see that judge's docstring: an arbitrary-content
regex is a fragile classification mechanism even when demoted to a
filter, and must not gate consultation of the judge either) — and DENIES
the tool call only when the judge confirms the menu proposes a
PR/merge-review delivery path. Fail-open in every other case (gate
closed, hint inactive, judge unavailable/errors/times out, or the judge
says the menu already proposes direct push).

Exit 0 always; emit stdout (becomes additional system context) on the
UserPromptSubmit path, or a PreToolUse deny JSON on the PreToolUse path.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import judge_ledger  # noqa: E402

# judge_ledger itself must import cleanly above for this to record anything —
# it is stdlib-only (see its own module docstring) and is what every other
# import failure here needs a working ledger to be recorded against.
try:
    from lib import config_root  # noqa: E402
    from lib import judge_budget  # noqa: E402
    from lib.host_llm import JUDGE_CHILD_ENV_VAR  # noqa: E402
    from lib.ask_text import flat_text  # noqa: E402
    from agentctl import advisor as _advisor  # noqa: E402
except BaseException as exc:
    judge_ledger.import_failed("landing_discipline", f"{type(exc).__name__}: {exc}")
    raise

try:
    from difficulty_channel import authority  # noqa: E402
except Exception:
    authority = None  # degrade silently — the direct-push-no-PR hint just won't fire

LAND_BRANCH_SCRIPT = Path(__file__).resolve().parent / "land-branch.py"
LAND_BRANCH_TIMEOUT_SEC = 5
GIT_TIMEOUT_SEC = 5

# Branch names that are trunk / shared — a push to these needs explicit
# confirmation, so we do NOT nudge a proactive push for them.
SHARED_BRANCH_RE = re.compile(
    r"^(trunk|master|main|develop|release[-/]|releases[-/]|stable)",
    re.IGNORECASE,
)

BRANCH_HYGIENE_HINT = (
    "Also — a working branch is cleanly landable into trunk: run "
    "`python3 scripts/land-branch.py --check` to preview, then bundle a "
    "land+delete option into the SAME resolution AskUserQuestion (ref-only "
    "ff; trunk-push needs explicit confirmation). Don't leave the branch "
    "hanging. Full landing discipline: "
    "memory-global/leaves/landing-discipline.md."
)

MERGED_LEFTOVER_HINT = (
    "Also — these local branches are already merged into trunk (tips reachable "
    "from origin/{trunk}) but not deleted: {branches}. Branch deletion is PART "
    "of landing, not a separate ask (CLAUDE.md § On task resolution): delete "
    "them via `python3 scripts/land-branch.py` (which deletes by default) or "
    "raw git (`git branch -D <br>` + `git push origin --delete <br>`). Full "
    "landing discipline: memory-global/leaves/landing-discipline.md."
)

DIRECT_PUSH_NO_PR_HINT = (
    "Also — this is the Core instructions repo and you hold direct push "
    "rights to it with no distinct human reviewer gating merge. Per "
    "memory-global/leaves/landing-discipline.md (\"review-gated is defined "
    "by a distinct human reviewer, not surface type\"), land by DIRECT push "
    "/ fast-forward (`python3 scripts/land-branch.py`) — do NOT propose or "
    "open a PR here. A PR in this repo is the "
    "[[capability-before-offload]] anti-pattern: an extra merge click "
    "offloaded onto the user when you already hold the rights and no one "
    "else must review."
)

UNPUSHED_BRANCH_HINT = (
    "Also — the current working branch has commits not on its remote "
    "(unpushed / ahead of upstream, or no upstream yet). Per CLAUDE.md "
    "§ On task resolution, deliver committed work to its terminal VCS state "
    "proactively: bundle a **push** option into the SAME resolution "
    "AskUserQuestion, recommended-first — pushing a personal / working "
    "branch is pre-authorized (§ Acting without asking #4). Never leave the "
    "push as a passive 'tell me if you want to push'. Full landing "
    "discipline: memory-global/leaves/landing-discipline.md."
)

# Safe-by-default kill-switch for the PreToolUse judge consult: unset or any
# value other than "0" leaves the classifier enabled, matching every other
# semantic judge's env convention in this repo.
_LANDING_DISCIPLINE_KILLSWITCH_ENV = "CLAUDE_LANDING_DISCIPLINE_SEMANTIC"

# This hook's own whole-invocation judge budget for the single
# judge_landing_discipline_ask call (K=1 — HOOK_CALL_SEQUENCE in
# lib/judge_latency.py declares one call for this hook), so at K=1 the budget
# IS the per-call ceiling. lib/judge_latency.call_ceiling_s("landing_discipline")
# is 17s (ceil(max=15.38)+1); 22s gives 5s of real headroom over that measured
# ceiling, joining the `>=` shape every other single-call hook here already
# uses rather than an equality tie — see
# test_a_single_call_hooks_budget_is_never_what_truncates_its_call in
# scripts/tests/test_judge_latency.py.
_LANDING_DISCIPLINE_JUDGE_BUDGET_S = 22

# Below this remaining budget the call cannot plausibly finish and would only
# spend the wait on a guaranteed timeout — lib/judge_latency.call_floor_s(
# "landing_discipline") = ceil(p90=6.37) = 7, comfortably above the fastest
# run observed (3.88s).
_LANDING_DISCIPLINE_JUDGE_MIN_CALL_S = 7

_LANDING_DISCIPLINE_DENY_REASON = (
    "this ask's menu appears to propose a pull-request / merge-review "
    "delivery path, but this repo requires direct push/fast-forward into "
    "trunk with no distinct human reviewer gating merge (per "
    "memory-global/leaves/landing-discipline.md). Rework the option(s) to "
    "land by direct push (`python3 scripts/land-branch.py`) instead of "
    "opening a PR."
)

MAX_WORDS = 6
META_MAX_WORDS = 20

# Brief gratitude keywords across languages. Excludes "ok"/"good" (too
# common as mid-task acknowledgments) and "done" (often used by the
# agent / user about completing a sub-step, not the task).
GRATITUDE_RE = re.compile(
    r"\b(thanks|thank\s*you|thx|спасибо|спс|пасиба|merci|"
    r"perfect|идеально|отлично|cool|круто|super|супер|"
    r"great|превосходно|nice|wonderful|amazing|excellent|"
    r"окей|👍|🙏|❤️|💯|🎉)\b",
    re.IGNORECASE | re.UNICODE,
)
# Meta-keywords signaling a question about the resolution gate itself
# (the user pointing at "why didn't you ask if it's done?"). Paired with
# a gratitude keyword to keep the false-positive rate low.
META_RE = re.compile(
    r"(спрашивае(ш|шь)|спросил[аи]?|почему\s+не|реш(ен|ён)а?|"
    r"закрыт[аоы]?|готов[оаы]?|закры|ask(ed|ing)?|"
    r"why\s+(didn'?t|not|haven'?t|aren'?t|don'?t)|"
    r"resolved|done|finished|closed|ready)",
    re.IGNORECASE | re.UNICODE,
)
WORD_RE = re.compile(r"\w+", re.UNICODE)


def is_brief_gratitude(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt))


def is_resolution_meta_question(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > META_MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt) and META_RE.search(prompt))


def resolution_gate_open(session_id: str) -> bool:
    """True iff an agentctl state file says node==RESOLUTION and the resolution
    gate has not passed. Missing/corrupt state -> False (fall back to prose)."""
    if not session_id:
        return False
    path = config_root.resolve_agentctl_state_file(session_id)
    if path is None:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if data.get("node") != "RESOLUTION":
        return False
    resolution = data.get("resolution") or {}
    return not bool(resolution.get("passed"))


def _delivery_repo_dir(session_id: str, cwd: str) -> str:
    """The repo the resolution landing-probes should key off. When the active
    agentctl session declares a delivery tree (its plan's [meta] delivery_worktree,
    else repo_root — where the work actually lands), probe THAT tree, not the
    session cwd, so Core delivery coordinated from an unrelated project session
    still gets the correct land-vs-PR nudge. Falls back to cwd when there is no
    session state, no declared tree, or the declared path is gone. Reads the same
    state file as resolution_gate_open; the whole body is guarded so a non-dict
    JSON body or a non-str field value degrades to cwd rather than raising — the
    hook must never crash a resolution turn."""
    if not session_id:
        return cwd
    try:
        path = config_root.resolve_agentctl_state_file(session_id)
        if path is None:
            return cwd
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("delivery_worktree", "repo_root"):
            val = data.get(key)
            if val and Path(val).is_dir():
                return val
    except Exception:
        return cwd
    return cwd


def landable_branch_hint(repo_dir: str) -> str | None:
    """Best-effort: BRANCH_HYGIENE_HINT if `land-branch.py --check` reports
    LANDABLE in repo_dir, else None. Any failure (missing script, timeout,
    non-zero exit, exception) degrades silently to None."""
    try:
        proc = subprocess.run(
            [sys.executable, str(LAND_BRANCH_SCRIPT), "--check", "-C", repo_dir],
            capture_output=True,
            text=True,
            timeout=LAND_BRANCH_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return BRANCH_HYGIENE_HINT


def _git(repo_dir: str, *args: str) -> str | None:
    """Best-effort `git -C repo_dir <args>` -> stripped stdout, or None on any
    failure (non-zero exit, timeout, git absent, exception)."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def unpushed_branch_hint(repo_dir: str) -> str | None:
    """Best-effort UNPUSHED_BRANCH_HINT when the current branch is a personal
    working branch (not trunk/shared) carrying commits that are not on its
    remote — either ahead of its upstream, or with no upstream configured
    while local commits exist. Any failure degrades silently to None so the
    hook never breaks a resolution turn."""
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":  # empty repo / detached HEAD
        return None
    if SHARED_BRANCH_RE.match(branch):
        return None
    # Upstream configured? If so, compare ahead-count; else treat any local
    # commit as unpushed.
    upstream = _git(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        ahead = _git(repo_dir, "rev-list", "--count", "@{u}..HEAD")
        if ahead and ahead.isdigit() and int(ahead) > 0:
            return UNPUSHED_BRANCH_HINT
        return None
    # No upstream — is there at least one commit to push?
    head = _git(repo_dir, "rev-list", "--count", "HEAD")
    if head and head.isdigit() and int(head) > 0:
        return UNPUSHED_BRANCH_HINT
    return None


def direct_push_no_pr_hint(repo_dir: str) -> str | None:
    """Best-effort DIRECT_PUSH_NO_PR_HINT when repo_dir is at or under the Core
    instructions repo root AND the current machine holds direct push rights to
    it (authority.is_author(), a git push --dry-run capability probe). Matches
    on "at or under" rather than exact equality since a session's cwd is often
    a subdirectory (e.g. scripts/) — same semantics as the `git -C repo_dir`
    calls in the sibling hint functions above, which resolve correctly from
    any subdirectory of the repo. The probe is timeout-bounded by
    authority.probe_push_capability's own default runner, so a network stall
    cannot hang the turn; this hint therefore needs no runner of its own. Any
    failure (authority unimportable, repo_dir not resolvable, probe
    exception/timeout) degrades silently to None so the hook never breaks a
    resolution turn."""
    if authority is None:
        return None
    try:
        resolved = Path(repo_dir).resolve()
        root = authority.REPO_ROOT.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            return None
        if not authority.is_author():
            return None
    except Exception:
        return None
    return DIRECT_PUSH_NO_PR_HINT


def merged_leftover_hint(repo_dir: str, trunk: str = "main", remote: str = "origin") -> str | None:
    """Best-effort MERGED_LEFTOVER_HINT naming local branches whose tips are
    already reachable from <remote>/<trunk> (i.e. merged/landed) but not yet
    deleted. Shared/trunk branch names are excluded; caps the list at 5.

    Known limitation: the ancestry test uses the LOCAL <remote>/<trunk>
    tracking ref, which can be stale without a fetch — a merged branch may be
    missed until the next fetch. False negatives are acceptable for a
    best-effort nudge. Any git failure -> None (silent degradation)."""
    listing = _git(repo_dir, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if not listing:
        return None
    leftovers = []
    for branch in listing.splitlines():
        branch = branch.strip()
        if not branch or branch == trunk or SHARED_BRANCH_RE.match(branch):
            continue
        # merge-base --is-ancestor exits 0 iff the branch tip is reachable from
        # the trunk tracking ref (merged). _git returns None on any non-zero
        # exit, so an empty string ("" from a clean exit) marks "is ancestor".
        anc = _git(repo_dir, "merge-base", "--is-ancestor", branch, f"{remote}/{trunk}")
        if anc is not None:
            leftovers.append(branch)
        if len(leftovers) >= 5:
            break
    if not leftovers:
        return None
    return MERGED_LEFTOVER_HINT.format(trunk=trunk, branches=", ".join(leftovers))


def decide(payload: dict) -> tuple[str, str]:
    """PreToolUse/AskUserQuestion decision: ("deny", reason) or ("allow", "").

    Opened as the very first statement, before resolution_gate_open or
    _delivery_repo_dir ever touch a state file — mirroring every sibling
    decide()'s deadline-first discipline (see hook-plan-delivery-gate.py's
    decide() docstring): the deadline must be live before the first read or
    it quietly spends part of its own headroom before the budget ever knows
    the clock started."""
    budget = judge_budget.JudgeBudget(
        _LANDING_DISCIPLINE_JUDGE_BUDGET_S, _LANDING_DISCIPLINE_JUDGE_MIN_CALL_S,
        clock=time.monotonic,
    )
    if payload.get("tool_name") != "AskUserQuestion":
        return "allow", ""
    session_id = payload.get("session_id") or ""
    if not resolution_gate_open(session_id):
        return "allow", ""
    repo_dir = _delivery_repo_dir(
        session_id,
        payload.get("cwd") or str(Path(__file__).resolve().parent),
    )
    try:
        hint = direct_push_no_pr_hint(repo_dir)
    except Exception:
        hint = None
    if not hint:
        return "allow", ""

    ask_text = flat_text(payload.get("tool_input") or {})
    # No prefilter, by explicit design: judge_landing_discipline_ask's own
    # docstring states that an arbitrary-content regex is a fragile
    # classification mechanism even when demoted to a filter rather than the
    # decision-maker, and must not gate consultation of this judge either —
    # every ask that reaches this point (gate open, hint applies) consults
    # the judge directly.
    judge_ledger.entered("landing_discipline", prefilter_fired=True)
    remaining_before_call, call_timeout = budget.remaining_and_timeout(
        _LANDING_DISCIPLINE_JUDGE_BUDGET_S
    )
    if call_timeout is None:
        judge_ledger.decided(
            "landing_discipline", stage="budget", verdict=False,
            reason="budget exhausted before call (fail-open)",
            remaining=remaining_before_call, threshold=None,
            ceiling=_LANDING_DISCIPLINE_JUDGE_BUDGET_S,
        )
        return "allow", ""
    proposes_pr, _reason = _advisor.judge_landing_discipline_ask(
        ask_text,
        _advisor.subprocess_runner,
        enabled=os.environ.get(_LANDING_DISCIPLINE_KILLSWITCH_ENV) != "0",
        timeout=call_timeout,
        remaining=remaining_before_call,
        ceiling=_LANDING_DISCIPLINE_JUDGE_BUDGET_S,
    )
    if proposes_pr:
        # Printed here, in the same scope as the judge_landing_discipline_ask
        # call above, rather than via a helper main() invokes afterward --
        # crutch-inventory.py's judge-guard check credits a hard-outcome sink
        # (a permissionDecision=deny dict literal) only when a judge_* call
        # appears in that SAME function scope, not merely in the call graph.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _LANDING_DISCIPLINE_DENY_REASON,
            }
        }))
        return "deny", _LANDING_DISCIPLINE_DENY_REASON
    return "allow", ""


def main() -> int:
    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0  # a sandboxed judge subprocess, not a real user turn — allow, no opinion

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    if payload.get("tool_name") == "AskUserQuestion":
        judge_ledger.hook_start("landing_discipline")
        judge_ledger.source_from_payload(payload)
        try:
            decision, _reason = decide(payload)
            has_directive = decision == "deny"
            judge_ledger.final(has_directive=has_directive)
            # decide() itself prints the deny JSON (same scope as its judge
            # call, see decide()'s comment) -- reaching here without an
            # exception means that print, if any, already succeeded.
            judge_ledger.emitted(ok=True, had_directive=has_directive)
        except Exception as exc:
            judge_ledger.discarded(reason=repr(exc))
            return 0  # fail-open — a hook must never wedge the ask
        return 0

    if resolution_gate_open(payload.get("session_id") or ""):
        print(
            "[resolution-reminder] The agentctl session is parked at the "
            "resolution gate (node=RESOLUTION, not yet passed). Per CLAUDE.md "
            "§ On task resolution, do NOT close the task on this message "
            "regardless of its wording. Give a one-line recap "
            "(`Requested: X. Delivered: Y.`) and ask the user to confirm "
            "explicitly via AskUserQuestion, then run `agentctl resolve "
            "--by <user>` only after an unambiguous confirmation. Include the "
            "agent-proposed 1-5 quality rating as the first resolution option "
            "(see leaves/quality-regression-investigation.md)."
        )
        repo_dir = _delivery_repo_dir(
            payload.get("session_id") or "",
            payload.get("cwd") or str(Path(__file__).resolve().parent),
        )
        try:
            hint = landable_branch_hint(repo_dir)
        except Exception:
            hint = None
        if hint:
            print(hint)
        else:
            # Only nudge the plain push when the branch is not cleanly
            # landable (else the landable hint already covers delivery).
            try:
                push_hint = unpushed_branch_hint(repo_dir)
            except Exception:
                push_hint = None
            if push_hint:
                print(push_hint)
        # Independent of the landable/unpushed nudges: name any already-merged
        # local branches still hanging around, so deletion (part of landing)
        # isn't skipped.
        try:
            leftover_hint = merged_leftover_hint(repo_dir)
        except Exception:
            leftover_hint = None
        if leftover_hint:
            print(leftover_hint)
        # Independent of the above: when this machine holds direct push
        # rights to this exact (Core) repo, name the anti-PR default so a
        # review-gated-repo assumption doesn't leak in from context.
        try:
            no_pr_hint = direct_push_no_pr_hint(repo_dir)
        except Exception:
            no_pr_hint = None
        if no_pr_hint:
            print(no_pr_hint)
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if not (is_brief_gratitude(prompt) or is_resolution_meta_question(prompt)):
        return 0
    print(
        "[resolution-reminder] User prompt is brief gratitude — ambiguous "
        "between 'thanks for the work' and 'task is resolved'. Per "
        "CLAUDE.md § On task resolution, do NOT treat bare gratitude as "
        "confirmation. If the plan's Final verification has passed, close "
        "with a one-line recap (`Requested: X. Delivered: Y.`) and ask "
        "`Considered resolved?` explicitly. Otherwise continue the work."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
