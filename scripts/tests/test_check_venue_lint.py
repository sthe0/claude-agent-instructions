"""check_venue_warnings (plan.py) — the venue lint rebased on declaration-vs-
command CONTRADICTION rather than guessing the intended venue from a `cd`
target. Schema 22 made the venue a declared field (Criterion.verify_venue /
FinalCheck.venue); this lint now warns only when a check's first `cd` target
disagrees with the venue IT ITSELF declares, and covers stage verify_commands
as well as [[final_check]] commands. Always advisory (never blocking).
"""
from agentctl.plan import check_venue_warnings
from agentctl.state import Actor, Criterion, FinalCheck, Means, Stage, Subject


def _stage(verify_command, verify_venue="delivery", index=1):
    return Stage(
        index=index, title="s%d" % index,
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command=verify_command, verify_venue=verify_venue,
        ),
    )


def test_delivery_venue_stage_cd_into_repo_root_warns(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(f"cd {repo_root} && pytest -q", verify_venue="delivery")
    warnings = check_venue_warnings([stage], [], str(repo_root), str(worktree))
    assert len(warnings) == 1
    assert "stage 1" in warnings[0]
    assert "delivery" in warnings[0]


def test_repo_root_venue_stage_cd_into_worktree_warns(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(f"cd {worktree} && pytest -q", verify_venue="repo_root")
    warnings = check_venue_warnings([stage], [], str(repo_root), str(worktree))
    assert len(warnings) == 1
    assert "stage 1" in warnings[0]
    assert "repo_root" in warnings[0]


def test_repo_root_venue_stage_cd_into_repo_root_is_silent(tmp_path):
    """A check that DECLARES venue = "repo_root" and cd's to canon is a
    deliberate post-landing confirmation, not a suspected mistake — the old
    guess-from-text lint could not express this and warned regardless."""
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(f"cd {repo_root} && pytest -q", verify_venue="repo_root")
    assert check_venue_warnings([stage], [], str(repo_root), str(worktree)) == []


def test_delivery_venue_stage_cd_into_worktree_is_silent(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(f"cd {worktree} && pytest -q", verify_venue="delivery")
    assert check_venue_warnings([stage], [], str(repo_root), str(worktree)) == []


def test_final_check_repo_root_venue_cd_into_repo_root_is_silent(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    fc = FinalCheck(command=f"cd {repo_root} && pytest -q", venue="repo_root", label="post-land")
    assert check_venue_warnings([], [fc], str(repo_root), str(worktree)) == []


def test_final_check_repo_root_venue_cd_into_worktree_warns(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    fc = FinalCheck(command=f"cd {worktree} && pytest -q", venue="repo_root", label="post-land")
    warnings = check_venue_warnings([], [fc], str(repo_root), str(worktree))
    assert len(warnings) == 1
    assert "post-land" in warnings[0]


def test_lint_silent_when_delivery_worktree_unset(tmp_path):
    repo_root = tmp_path / "repo"
    stage = _stage(f"cd {repo_root} && pytest -q", verify_venue="delivery")
    fc = FinalCheck(command=f"cd {repo_root} && pytest -q")
    assert check_venue_warnings([stage], [fc], str(repo_root), None) == []


def test_lint_covers_both_a_stage_and_a_final_check_in_one_call(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(f"cd {repo_root} && pytest -q", verify_venue="delivery", index=2)
    fc = FinalCheck(command=f"cd {worktree} && pytest -q", venue="repo_root", label="fc")
    warnings = check_venue_warnings([stage], [fc], str(repo_root), str(worktree))
    assert len(warnings) == 2
    assert any("stage 2" in w for w in warnings)
    assert any("fc" in w for w in warnings)
