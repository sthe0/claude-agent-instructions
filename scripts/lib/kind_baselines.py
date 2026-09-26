"""Per-spawn-kind baseline permission set (`KIND_BASELINES`).

Lives under `scripts/lib/` rather than in `spawn-specialist.py` (the
launcher) so `agentctl/cli.py` can read it via a normal top-level import
instead of `importlib.util.spec_from_file_location`-loading the launcher
script -- the launcher imports `agentctl.grants`/`agentctl.plan`/
`agentctl.render`, so a module-load-time import of it from `agentctl/cli.py`
would be circular; this module has no such dependency and can be imported
from either side. `spawn-specialist.py` re-exports `KIND_BASELINES` and
`SCRIPTS_DIR` at module scope so existing callers/tests that read them off
the launcher module keep working unchanged.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Per-kind baseline permission set, injected into the child's --settings
# payload rather than settings/base.json (which is merged fleet-wide on
# `git pull` without a prompt and must stay read-only-only per
# lint-settings-base.py). See memory-global leaf settings-permission-tiers.md.
#
# Difficulty removed: before this table, only the developer kind got any Bash
# baseline at all (build_child_settings's old kind-equality gate) — every
# other kind ran under a bare acceptEdits-or-nothing mode with NO
# explicit Bash grant, so even side-effect-free inspection commands prompted
# for a decision no headless child could answer (acceptEdits auto-grants
# file writes and NOTHING else — unlike `defaultMode: auto`, it does not
# auto-approve Bash). Each kind's entries are the measured, justified
# minimum against 30 days of real spawned-child Bash-prefix/denial data —
# see /home/the0/.claude-agent/plans/evidence/spawn-permission-grant-model/
# kind-tool-inventory.md — not a guess at what a role "should" need.
#
# `sed` is deliberately absent from every bucket even though it is a top-3
# measured prefix for all five kinds: grants.py's `_WRITE_CAPABLE_PROGRAMS`
# treats ANY `sed` invocation as write-capable, so a wildcarded
# `Bash(sed:*)` is refused outright by validate_rule regardless of the fact
# that every measured call was `sed -n` (read-only) — no static settings
# rule can express "the -n form only". A `python3 -m agentctl` verb beyond
# classify/status (plan-review, code-review, stage-grants, ...) is likewise
# absent: those verbs are either AGENTCTL_USER_AUTHORITY_VERBS (refused
# unconditionally — see plugins_review_dispatch.py, whose directive text
# routes plan-review/code-review recording through the ROOT instead) or are
# exactly what the new --session/--stage-index engine-grant materialization
# below exists to supply per-stage instead of guessing a fleet-wide list.
_READ_ONLY_INSPECTION = [
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(stat:*)", "Bash(pwd)",
    "Bash(shasum:*)", "Bash(sha256sum:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(python3 -m agentctl classify:*)", "Bash(python3 -m agentctl status:*)",
]

# Absolute-path Bash rules for the planner kind: the harness matches a Bash
# rule against the LITERAL command string, so a relative `scripts/foo.py` or
# bare `foo.py` rule only ever matches when the child's cwd happens to be the
# right one — a planner spawned with --workdir outside this repo (the normal
# case) silently loses the grant. Every planner rule below is instead built
# from this repo's own absolute scripts/ dir, so it matches regardless of the
# child's cwd; see also PROJECT_SETTINGS_KINDS.
SCRIPTS_DIR = REPO_ROOT / "scripts"
PLANNER_CHECK_ORDER_COVERAGE_RULE = f"Bash(python3 {SCRIPTS_DIR}/check-order-coverage.py:*)"
PLANNER_PLAN_GRANTS_RULE = f"Bash(python3 {SCRIPTS_DIR}/agentctl-cli.py plan-grants:*)"
PLANNER_LIST_DENIED_RULE = f"Bash(python3 {SCRIPTS_DIR}/check-spawn-tool-run.py --list-denied:*)"


def _abs_and_relative_script_rules(*script_names: str) -> list[str]:
    """Both the absolute and repo-relative `Bash(python3 ...)` rule spelling
    for each script under `SCRIPTS_DIR` (round 3: `grants._segment_covered`
    matches a Bash rule against the LITERAL command string, with no notion
    that an absolute and a repo-relative spelling name the same file, same
    as the harness itself). Unlike the planner rules above -- deliberately
    absolute-only because a planner's cwd is normally OUTSIDE this repo --
    a developer or code-reviewer child's cwd normally IS this repo/worktree,
    so both `python3 <SCRIPTS_DIR>/x.py` and `python3 scripts/x.py` are real
    invocation shapes that each need their own literal, materialized rule
    (the round-2 "every baseline SCRIPT rule must match both absolute and
    repo-relative invocation forms" intent, previously implemented as an
    engine-side equivalence match instead of as two materialized rules --
    see the removed `_path_suffix_equivalent`/`_script_path_swapped` in
    `agentctl.grants`). Named residual, unchanged from round 2: the relative
    spelling also matches an unrelated project's own same-named
    `scripts/<file>` sitting below the child's actual cwd."""
    rules: list[str] = []
    for name in script_names:
        rules.append(f"Bash(python3 {SCRIPTS_DIR}/{name}:*)")
        rules.append(f"Bash(python3 scripts/{name}:*)")
    return rules


KIND_BASELINES: dict[str, list[str]] = {
    # thinker: 4563 Bash calls sampled, overwhelmingly read-only inspection
    # (grep/python3 -m agentctl introspection/ls/shasum/cat/git log); the
    # measured 42 `python3 -m agentctl` denials are user-authority verbs the
    # read-only bucket correctly excludes, not a baseline gap (see above).
    "thinker": list(_READ_ONLY_INSPECTION),
    # planner: 1629 Bash calls, same read-only shape as thinker, plus the
    # plan-authoring repo verifier it measurably runs on its own output (37
    # combined calls to check-order-coverage.py), plus two research commands
    # this stage's own "Prescribed research commands" prompt section
    # (assemble_prompt) tells the planner to run before drafting: plan-grants
    # (what a stage's own grants will materialize to) and --list-denied (what
    # a past transcript was actually refused). All three are absolute-path
    # rules (see SCRIPTS_DIR above), not the prior relative `scripts/foo.py`
    # shape, which only matched from this repo's own cwd. Edit access to
    # plans_dir() itself is a separate, pre-existing grant
    # (PLANS_WRITE_KINDS/plans_permission_rules) layered on in
    # build_child_settings, not duplicated here.
    "planner": list(_READ_ONLY_INSPECTION) + [
        PLANNER_CHECK_ORDER_COVERAGE_RULE,
        PLANNER_PLAN_GRANTS_RULE,
        PLANNER_LIST_DENIED_RULE,
    ],
    # code-reviewer: 1808 Bash calls, read-only bucket dominant (git
    # diff/show/log/status — all now common), plus the test
    # runner (63 measured calls, 2 denied) and the one repo verifier it was
    # measurably denied on (python3 scripts/verify-semantic-gates.py: 2
    # denials) — a reviewer needs to run checks, not just read diffs.
    "code-reviewer": list(_READ_ONLY_INSPECTION) + [
        "Bash(python3 -m pytest:*)",
        *_abs_and_relative_script_rules("verify-semantic-gates.py"),
    ],
    # tech-writer: only 13 Bash calls sampled across 10 transcripts (mostly
    # `wc`, covered by the read-only bucket) — too small a sample to justify
    # anything beyond the one measured, narrow, role-appropriate addition:
    # `gh issue` (ticket/README publication is this kind's actual job).
    "tech-writer": list(_READ_ONLY_INSPECTION) + [
        "Bash(gh issue:*)",
    ],
    # developer: the historical DEVELOPER_SETTINGS_ALLOW list, restructured
    # into this table verbatim except for the removed unbounded direct
    # `claude -p` grant below (see its own history) — every line here
    # already carries its own measured justification (see the per-grant
    # comments).
    "developer": list(_READ_ONLY_INSPECTION) + [
        # verification the brief mandates
        "Bash(python3 -m pytest:*)",
        *_abs_and_relative_script_rules(
            "verify-all.py", "verify-agentctl.py", "gen_crutch_registry.py",
        ),
        # recording work on the assigned branch — never `git push`
        "Bash(git add:*)", "Bash(git commit:*)",
        # integrating trunk INTO the assigned branch — the same defect one step
        # later. Observed 2026-08-10: a stage whose whole material was "merge
        # origin/main into the delivery branch and resolve the conflicts" was
        # dispatched with a grant that stopped at `git commit`, and every
        # mutating verb it needed was refused across five command shapes.
        # Reading trunk's own baseline needs the detached checkout; resolving a
        # conflict needs checkout/restore on a path; ff-vs-true-merge needs
        # merge-base and rev-list. Landing stays absent: `git push` is the
        # coordinator's.
        #
        # `git fetch` is narrowed to two EXACT (non-wildcard) invocations
        # (round 3: even `Bash(git fetch origin:*)`, narrowed to the `origin`
        # remote in round 2, still prefix-admits `git fetch origin
        # --upload-pack=<arbitrary program>` — a code-executing verb no
        # `KIND_BASELINES` rule may admit, see
        # `test_code_executing_git_subcommand_not_admitted_by_any_kind_baseline`).
        # These two exact forms are the only invocations this baseline's own
        # use case (reading trunk to merge it into the assigned branch) ever
        # needs: fetching `origin`'s default refs, and fetching `origin/main`
        # by name for the merge-base/rev-list classification above it reads.
        "Bash(git fetch origin)", "Bash(git fetch origin main)",
        "Bash(git merge:*)", "Bash(git merge-base:*)",
        "Bash(git rev-list:*)", "Bash(git checkout:*)", "Bash(git restore:*)",
        # spawn-outcome-typing stage 4 measures marker_extract's own latency via
        # real host calls — scoped to the driver script only. User-authorized
        # 2026-08-20 as a temporary unblock; a proper per-stage/plan-declared
        # permission mechanism (this stage's own --session/--stage-index engine
        # grant materialization) is what stage 2 of this same plan builds.
        #
        # A sibling grant for invoking `claude -p --model haiku` directly was
        # added alongside this one at first, then removed the same day on
        # code-review: the script drives claude via host_llm.build_prompt_argv +
        # marker_extract.subprocess_runner INSIDE this already-permitted
        # python3 process, never through the Bash tool directly, so the extra
        # grant was both unused and, being "claude -p ... :*" (unbounded
        # trailing args), a bypass of spawn-specialist.py's own outcome-typing
        # ledger for any developer that DID reach for it directly — the exact
        # defect this plan exists to fix.
        *_abs_and_relative_script_rules("measure-marker-extractor-latency.py"),
        # hook-resolution-reminder-pretooluse-gap stage 1 needs to compile its own
        # edits, check the engine's own worktree-local gate state, and run the new
        # judge's real-call latency sampler. User-authorized 2026-08-28 as another
        # narrow, named unblock — same precedent as the grant above, not a
        # broadening to "any python3". Root cause of needing this at all:
        # `agentctl resolve-permission --decision granted` only clears engine
        # state and returns a continuation string (continuations.py
        # permission_granted()) — it never writes to any permissions file and
        # never touches this list, so three consecutive PERMISSION-REQUEST/grant
        # cycles for this exact stage reproduced the identical block each time.
        "Bash(python3 -m py_compile:*)",
        "Bash(python3 samples/judge-latency/sample_landing_discipline.py:*)",
        # tech-writer-publication-gate stage 6's method is a real in-harness
        # observation: spawn two actual `claude -p` children (deny arm, allow
        # arm) against a scratch hook and independently re-check the recorded
        # timestamps/acts, which a stdin-fed rerun of the hook cannot establish.
        # User-authorized 2026-09-02 as the same narrow, named, temporary
        # unblock pattern as the two grants above. Unlike the removed
        # "claude -p --model haiku:*" grant noted above, a direct `claude -p`
        # spawn is this stage's actual deliverable, not an avoidable
        # implementation detail routed through an already-permitted python3
        # process — so the raw Bash grant is scoped here, not just the wrapper.
        *_abs_and_relative_script_rules(
            "check-in-harness-observation.py", "check-live-run-evidence.py",
        ),
        "Bash(python3 _ptg_scratch/probe/launch_probe.py:*)",
        # The direct `claude -p` grant REMOVED (was here through 2026-09-24):
        # unbounded trailing args on the one program grants.validate_rule refuses
        # unconditionally in every OTHER position (is_claude_program) — its
        # presence here was a pre-existing exception this stage's brief
        # explicitly names for removal, not a measured need (no measured Bash
        # call in the inventory actually invokes `claude` directly; every
        # in-harness-observation stage above drives it via host_llm's argv
        # builder inside the already-permitted python3 process).
    ],
    # default: any kind with no dedicated row above (e.g. a project-local
    # specialization under <cwd>/.claude/skills/specializations/) gets the
    # read-only bucket and nothing role-specific — same principle as every
    # other row: only a MEASURED usage pattern earns an addition here.
    "default": list(_READ_ONLY_INSPECTION),
}


def kind_baseline_sha256(kind: str) -> str:
    """Stable sha256 hex digest of one kind's baseline rule list (falling
    back to the `"default"` baseline for an unknown kind), same
    `repr(...)`-then-sha256 convention as `plan.grants_sha256` — sorted so
    the digest never depends on the list's declared order. Lets
    `plan-grants --format compact` name which baseline a spawned stage's
    child actually receives without a reader diffing the whole
    `KIND_BASELINES` table by eye."""
    baseline = KIND_BASELINES.get(kind, KIND_BASELINES["default"])
    payload = repr(tuple(sorted(baseline)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
