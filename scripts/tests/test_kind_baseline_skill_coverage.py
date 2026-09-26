"""Guards the other direction of the kind-baseline contract test_spawn_pytest_allowlist_hygiene.py
and friends already check from the materialization side: every command a kind's own
SKILL.md/policy.md actually tells that kind to run must be reachable through its
KIND_BASELINES row, or the brief is silently asking for a command the child will get
denied on the moment it tries.

Placed alongside the planner policy.md edit that introduces the mandatory
"Planning-time grant research" step (this stage), since the planner's own advice to
'run both commands in exactly the absolute form the brief's File-access scope header
prints' only holds if this test also checks the planner's baseline covers those two
commands — the same discipline the planner step asks every stage author to apply is
applied here to the read-first docs themselves.

A "runnable command span" is a backtick code span in one of a kind's read-first
files that starts with one of a fixed set of prefixes (python3-invocation, the two
absolute/relative `cd ... && python3 -m agentctl` forms, or a bare `git ` verb) — a
narrow net, deliberately: it is not a general markdown-command extractor, only
enough to catch a change that quietly adds an instruction the baseline cannot serve.
A `<scripts>` placeholder is substituted with the kind's own absolute scripts/ dir
(the same substitution spawn-specialist.py performs when it builds a brief's
"File-access scope" header) before the coverage check runs.

Every matched span must be covered by grants.grant_covers_call against a StageGrants
built straight from KIND_BASELINES[kind] (fail-toward-not-covered, per that
function's own documented bias), or be listed in _EXEMPT below with a one-line
reason (a forbidden action a brief prohibits rather than instructs, or a
pre-existing illustrative example predating this test)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from agentctl import grants

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
REPO_ROOT = SCRIPT.parent.parent


def _load_spawn_specialist():
    spec = importlib.util.spec_from_file_location("spawn_specialist_kind_coverage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_spawn_specialist()

_PREFIXES = (
    "python3 ",
    "cd scripts && python3 -m agentctl",
    "cd ~/claude-agent-instructions/scripts && python3 -m agentctl",
    "git ",
)

# kind -> read-first files that carry that kind's own instructions (relative to
# REPO_ROOT). Kinds absent here (e.g. "default") have no dedicated brief and are
# not checked.
_KIND_FILES: dict[str, tuple[str, ...]] = {
    "developer": ("skills/specializations/developer/SKILL.md",),
    "code-reviewer": ("skills/specializations/code-reviewer/SKILL.md",),
    "thinker": ("skills/specializations/thinker/SKILL.md",),
    "planner": (
        "skills/specializations/planner/SKILL.md",
        "skills/specializations/planner/policy.md",
    ),
    "tech-writer": ("skills/specializations/tech-writer/SKILL.md",),
}

# (kind, span) -> one-line reason. Every entry here is a span the regex would flag
# that is NOT an instruction to run the command as written.
_EXEMPT: dict[tuple[str, str], str] = {
    ("developer", "git push --force"): "a forbidden action the brief prohibits, never one it instructs",
    ("developer", "git reset --hard"): "a forbidden action the brief prohibits, never one it instructs",
    ("developer", "git -C <worktree> commit …"): (
        "illustrative placeholder shape (<worktree> + trailing ellipsis) showing which "
        "flag to use, not a literal invocation"
    ),
    (
        "planner",
        'python3 scripts/record-experience.py search --tier system-knowledge "<task keywords>"',
    ): "pre-existing relative-path example predating this test; only resolves from the repo cwd, a known baseline gap",
    (
        "planner",
        'python3 scripts/record-experience.py search --tier principles "<stage keywords>"',
    ): "pre-existing relative-path example predating this test; only resolves from the repo cwd, a known baseline gap",
}


def _extract_spans(text: str) -> list[str]:
    return [s for s in re.findall(r"`([^`\n]+)`", text) if s.startswith(_PREFIXES)]


def _baseline_grants(kind: str) -> grants.StageGrants:
    baseline = MOD.KIND_BASELINES.get(kind, MOD.KIND_BASELINES["default"])
    return grants.StageGrants(allow=[grants.RuleGrant(rule=r, provenance="declared") for r in baseline])


def _spans_by_kind() -> dict[str, list[tuple[str, str]]]:
    """kind -> list of (file, span) pairs found in that kind's read-first files."""
    out: dict[str, list[tuple[str, str]]] = {}
    for kind, files in _KIND_FILES.items():
        pairs: list[tuple[str, str]] = []
        for rel in files:
            text = (REPO_ROOT / rel).read_text()
            for span in _extract_spans(text):
                span = span.replace("<scripts>", str(MOD.SCRIPTS_DIR))
                pairs.append((rel, span))
        out[kind] = pairs
    return out


def test_every_kind_has_at_least_one_read_first_file_row():
    # a change to KIND_BASELINES with no corresponding row here would silently
    # exempt the new kind from this whole test
    assert set(_KIND_FILES) <= set(MOD.KIND_BASELINES)


def test_kind_skill_command_spans_are_covered_by_the_kind_baseline_or_exempt():
    uncovered: list[str] = []
    for kind, pairs in _spans_by_kind().items():
        stage_grants = _baseline_grants(kind)
        for rel, span in pairs:
            if (kind, span) in _EXEMPT:
                continue
            if grants.grant_covers_call(stage_grants, "Bash", {"command": span}):
                continue
            uncovered.append(f"{kind}: {rel}: {span!r}")
    assert not uncovered, (
        "these SKILL.md/policy.md command spans are not covered by their kind's "
        "KIND_BASELINES row and are not in _EXEMPT:\n" + "\n".join(uncovered)
    )


def test_exempt_entries_are_actually_uncovered():
    """An _EXEMPT entry that the baseline already covers is stale bookkeeping —
    it would hide a real regression if the baseline later shrank."""
    stale: list[str] = []
    for (kind, span), _reason in _EXEMPT.items():
        stage_grants = _baseline_grants(kind)
        if grants.grant_covers_call(stage_grants, "Bash", {"command": span}):
            stale.append(f"{kind}: {span!r}")
    assert not stale, f"these _EXEMPT entries are already covered by the baseline: {stale}"


def test_exempt_entries_reference_spans_actually_present_in_their_kind_files():
    """An _EXEMPT entry for a span no longer in the file is dead bookkeeping that
    would silently stop protecting anything if the wording changed again."""
    present = {(kind, span) for kind, pairs in _spans_by_kind().items() for _rel, span in pairs}
    missing = [key for key in _EXEMPT if key not in present]
    assert not missing, f"these _EXEMPT entries no longer match any span: {missing}"
