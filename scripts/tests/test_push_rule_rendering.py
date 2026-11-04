"""A spawned developer pushes its own branch through one exact harness allow rule. The
resource-grant hook abstains on every git line, so the child's static allow list alone
decides a push; these tests pin that the frozen DR-P push resource reaches that list as
`Bash(git push origin <branch>)` and nothing wider, that the rule is withheld when the
venue's git config would remap a colon-less push, and that the brief names the same command."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agentctl import plan as plan_mod, resources as R, tool_contracts
from test_grants_own_branch import _git_repo, _plan_doc
from test_norm_loop import SPAWN

RULE = "Bash(git push origin feat-a)"


def _repo(tmp_path):
    git = _git_repo(tmp_path)
    git("remote", "add", "origin", str(tmp_path / "remote.git"))
    return git


def _linked(tmp_path, executor="spawn:developer", frozen_ref=None):
    doc = _plan_doc(tmp_path, "feat-a")
    if executor != "spawn:developer":
        path = tmp_path / "plan-feat-a-e.toml"
        path.write_text(path.read_text().replace('"spawn:developer"', f'"{executor}"'))
        doc = plan_mod.load_plan(path, strict=False)
    entries = plan_mod.materialized_grant_entries(doc)
    if frozen_ref is not None:
        for resource in entries["1"]["branch"]["resources"]:
            resource["ref"] = frozen_ref
    state = SimpleNamespace(
        approved_grant_entries=entries,
        approved_grants_sha256=plan_mod.entries_grants_sha256(entries),
        stage=lambda i: doc.stages[0],
        resolve_check_venue=lambda _kind: str(tmp_path))
    return state, doc


def _settings(push_rules):
    return SPAWN.child_permission_settings(
        "developer", None, None, [], workdir=None, evidence_dir=None, guard_exempt_paths=(),
        read_dirs=[], own_session=None, push_rules=push_rules)


def test_the_frozen_push_resource_becomes_exactly_one_rule(tmp_path):
    _repo(tmp_path)
    rules = SPAWN.push_allow_rules(_linked(tmp_path), 1, "developer")
    assert rules == [RULE]
    allow = _settings(rules)["permissions"]["allow"]
    assert [r for r in allow if r.startswith("Bash(git push")] == [RULE]


def test_the_rule_denotes_the_resource_the_widening_check_reads(tmp_path):
    _repo(tmp_path)
    linked = _linked(tmp_path)
    [rule] = SPAWN.push_allow_rules(linked, 1, "developer")
    pushes = [r for r in SPAWN._stage_branch_resources(linked[0], 1) if r.op == "push"]
    resolved = tool_contracts.resolve_command(rule[len("Bash("):-1], str(tmp_path))
    assert pushes == [R.VcsRefResource("origin", "feat-a", "push")]
    assert list(resolved.resources) == pushes


@pytest.mark.parametrize("config", [
    ("push.default", "upstream"),
    ("remote.origin.push", "refs/heads/feat-a:refs/heads/other"),
    ("push.followTags", "true"),
    ("push.recurseSubmodules", "check"),
])
def test_a_config_that_remaps_the_push_withholds_the_rule(tmp_path, capsys, config):
    git = _repo(tmp_path)
    git("config", *config)
    assert SPAWN.push_allow_rules(_linked(tmp_path), 1, "developer") == []
    assert "no push rule for origin/feat-a" in capsys.readouterr().err


def test_a_frozen_branch_name_with_a_metacharacter_gets_no_rule(tmp_path, capsys):
    """DR-P already drops such a name at approve; the spawn does not rely on that."""
    _repo(tmp_path)
    assert SPAWN.push_allow_rules(_linked(tmp_path, frozen_ref="a;b"), 1, "developer") == []
    assert "outside [A-Za-z0-9._/-]" in capsys.readouterr().err


def test_no_rule_without_a_frozen_push_resource(tmp_path):
    _repo(tmp_path)
    assert SPAWN.push_allow_rules(_linked(tmp_path, executor="spawn:tech-writer"), 1, "tech-writer") == []
    assert SPAWN.push_allow_rules(_linked(tmp_path, executor="spawn:tech-writer"), 1, "developer") == []
    assert SPAWN.push_allow_rules(None, None, "developer") == []
    assert SPAWN.push_allow_rules(_linked(tmp_path), 1, "thinker") == []
    assert not any("git push" in r for r in _settings([])["permissions"]["allow"])


def test_the_brief_names_the_granted_command_or_says_commit_only():
    granted = SPAWN.push_checkpoint_text([RULE])
    assert "`git push origin feat-a`" in granted and "matches only that spelling" in granted
    assert "remote.<r>.pushurl" in granted and "pre-push" in granted
    assert "never land" in granted
    assert SPAWN.push_checkpoint_text([]).startswith("Commit only")


@pytest.mark.parametrize("config, landed", [
    ((), "refs/heads/feat"),
    (("push.default", "matching"), "refs/heads/feat"),
    (("remote.origin.push", "refs/heads/feat:refs/heads/other"), "refs/heads/other"),
    (("push.default", "upstream"), "refs/heads/up"),
])
def test_which_configs_remap_a_colon_less_push(tmp_path, config, landed):
    """Why the rule is gated on the config: a colon-less push follows `remote.<r>.push` and
    `push.default=upstream`, while `matching` leaves it alone."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "w"

    def git(*args):
        subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git("commit", "-q", "--allow-empty", "-m", "c")
    git("branch", "feat")
    git("remote", "add", "origin", str(remote))
    git("config", "branch.feat.remote", "origin")
    git("config", "branch.feat.merge", "refs/heads/up")
    if config:
        git("config", *config)
    git("push", "-q", "origin", "feat")
    refs = subprocess.run(["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"],
                          check=True, capture_output=True, text=True).stdout.split()
    assert refs == [landed]
