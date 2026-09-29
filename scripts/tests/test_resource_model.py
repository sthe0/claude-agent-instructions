"""Tests for the typed-resource permission model (scripts/agentctl/resources.py
and its future companions — the contract-grounded resolver, the script-
effects registry, the order-approvals ledger, and the CLI surface they back).

Every test in this file asserts `importlib.util.find_spec` presence for the
module it exercises BEFORE importing it, so a test fails by ASSERTION (not
ImportError/collection error) on a tree that predates this stage — the
shape `nc.sh` requires to treat a test as discriminating. See nc.sh's own
docstring for why an ImportError-on-old-tree does not count as a genuine
negative control.
"""
from __future__ import annotations

import importlib.util

import pytest


def _resources_module():
    spec = importlib.util.find_spec("agentctl.resources")
    assert spec is not None, "agentctl.resources module not found"
    from agentctl import resources

    return resources


def _tool_contracts_module():
    spec = importlib.util.find_spec("agentctl.tool_contracts")
    assert spec is not None, "agentctl.tool_contracts module not found"
    from agentctl import tool_contracts

    return tool_contracts


def test_covers_file_by_containment_protected_roots_never(tmp_path, monkeypatch):
    resources = _resources_module()

    foo = tmp_path / "a" / "foo"
    foo.mkdir(parents=True)
    foobar = tmp_path / "a" / "foobar"
    foobar.mkdir(parents=True)
    inner = foo / "x"
    inner.write_text("hi")

    approved = resources.FileResource(str(foo), "write")
    assert approved.covers(resources.FileResource(str(inner), "write"))
    assert approved.covers(resources.FileResource(str(foo), "write"))
    # A sibling that merely shares a string prefix must never be covered.
    assert not approved.covers(resources.FileResource(str(foobar), "write"))
    # A write approval does not cover a request for a protected root itself,
    # no matter how the containment math alone would come out (matching
    # `widening_targets.add_dir_is_or_contains_protected_root`'s own bias);
    # a plain file nested under it, not itself a protected root or under the
    # narrower ~/.claude-equivalent set, is an ordinary covered target.
    home = tmp_path / "home_stand_in"
    home.mkdir()
    monkeypatch.setattr(
        "lib.widening_targets.protected_roots", lambda: [str(home.resolve())]
    )
    broad_approval = resources.FileResource(str(tmp_path), "write")
    assert not broad_approval.covers(resources.FileResource(str(home), "write"))
    nested = home / "nested"
    nested.mkdir()
    assert broad_approval.covers(resources.FileResource(str(nested), "write"))
    # read approval never covers a write request; a plain unrelated file
    # under the approved write dir is still covered (positive control).
    other = foo / "y"
    other.write_text("hi")
    read_only = resources.FileResource(str(foo), "read")
    assert not read_only.covers(resources.FileResource(str(other), "write"))
    assert read_only.covers(resources.FileResource(str(other), "read"))
    assert approved.covers(resources.FileResource(str(other), "write"))


def test_covers_vcs_ref_exact_including_op():
    resources = _resources_module()

    push = resources.VcsRefResource("origin", "main", "push")
    assert push.covers(resources.VcsRefResource("origin", "main", "push"))
    # Same remote/ref, different op: a push approval never covers a land
    # request, and a land approval never covers a push request.
    land = resources.VcsRefResource("origin", "main", "land")
    assert not push.covers(land)
    assert not land.covers(push)
    # Different remote or ref: never covered.
    assert not push.covers(resources.VcsRefResource("upstream", "main", "push"))
    assert not push.covers(resources.VcsRefResource("origin", "release", "push"))
    # A different resource kind entirely is never covered.
    assert not push.covers(resources.FileResource("/tmp", "read"))


def test_covers_specialist_kind_exact():
    resources = _resources_module()

    dev = resources.SpecialistResource("developer")
    assert dev.covers(resources.SpecialistResource("developer"))
    assert not dev.covers(resources.SpecialistResource("planner"))
    assert not dev.covers(resources.VcsRefResource("origin", "main", "push"))


def test_covers_service_exact():
    resources = _resources_module()

    svc = resources.ServiceResource("yandex-cloud")
    assert svc.covers(resources.ServiceResource("yandex-cloud"))
    assert not svc.covers(resources.ServiceResource("other-service"))
    assert not svc.covers(resources.SpecialistResource("developer"))


def test_covers_dataset_exact():
    resources = _resources_module()

    ds = resources.DatasetResource("clicks-daily")
    assert ds.covers(resources.DatasetResource("clicks-daily"))
    assert not ds.covers(resources.DatasetResource("clicks-hourly"))
    assert not ds.covers(resources.ServiceResource("clicks-daily"))


def test_contract_tables_and_ledger_dir_never_covered(tmp_path):
    resources = _resources_module()

    venue = tmp_path / "venue"
    (venue / "scripts" / "agentctl").mkdir(parents=True)
    (venue / "scripts" / "agentctl" / "tool_contracts.toml").write_text("")
    (venue / "scripts" / "script_effects.toml").write_text("")
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "order.json").write_text("{}")
    other_file = venue / "README.md"
    other_file.write_text("hi")

    protected = resources.protected_permission_surfaces(
        repo_root=str(venue), delivery_worktree=None, ledger_dir=str(ledger)
    )

    approved_whole_venue = resources.FileResource(str(venue), "write")

    # The contract table and the registry: never covered, even under a
    # write approval on the whole venue that would ordinarily contain them.
    assert not approved_whole_venue.covers(
        resources.FileResource(str(venue / "scripts" / "agentctl" / "tool_contracts.toml"), "write"),
        protected=protected,
    )
    assert not approved_whole_venue.covers(
        resources.FileResource(str(venue / "scripts" / "script_effects.toml"), "write"),
        protected=protected,
    )
    # A path under the ledger directory: never covered.
    assert not approved_whole_venue.covers(
        resources.FileResource(str(ledger / "order.json"), "write"), protected=protected
    )
    # A write add_dir request on scripts/ CONTAINS the protected tables and
    # is strictly narrower than the whole-venue approval — containment
    # alone would ordinarily cover it, but it is refused because only an
    # EXACT approval of scripts/ itself (not a broader one) may cover a
    # requested resource that contains a protected surface.
    assert not approved_whole_venue.covers(
        resources.FileResource(str(venue / "scripts"), "write"), protected=protected
    )

    # Positive controls: an ordinary venue file, and a request equal to the
    # approved resource itself, are both covered as usual.
    assert approved_whole_venue.covers(
        resources.FileResource(str(other_file), "write"), protected=protected
    )
    assert approved_whole_venue.covers(
        resources.FileResource(str(venue), "write"), protected=protected
    )
    # An EXACT approval of scripts/ itself DOES cover the request for
    # scripts/ itself (the containing-a-protected-surface rule requires
    # exact equality, which this is).
    exact_scripts_approval = resources.FileResource(str(venue / "scripts"), "write")
    assert exact_scripts_approval.covers(
        resources.FileResource(str(venue / "scripts"), "write"), protected=protected
    )


def _classify_module():
    spec = importlib.util.find_spec("agentctl.classify")
    assert spec is not None, "agentctl.classify module not found"
    from agentctl import classify

    return classify


def test_classify_action_awk_find_sed_not_side_effect_free():
    """REQ3: a coarse verb-only classifier must not call awk/sed/find
    side-effect-free — each can be argument-dependently writing (awk
    `print > "file"`, sed `-i`, find `-exec`), which classify_action cannot
    see from the verb alone. agentctl/tool_contracts.py's resolve_command()
    is the reviewed, argument-aware resolver these three now route through
    instead."""
    classify = _classify_module()

    for verb in ("awk", "sed", "find"):
        assert verb not in classify.READONLY_BASH
        assert classify.classify_action("Bash", verb=verb) != "side-effect-free"


def test_tool_contracts_toml_has_resolver_entries_for_removed_readonly_verbs():
    """Cross-module consistency: a verb removed from classify.py's
    READONLY_BASH because it needs argument-aware resolution must actually
    HAVE a reviewed resolver entry in tool_contracts.toml — otherwise the
    removal just makes the verb `unknown-program`-unresolved by omission
    rather than by a reviewed decision."""
    classify = _classify_module()
    tc = _tool_contracts_module()

    table = tc.load_contract_table()
    for verb in ("awk", "sed", "find"):
        assert verb not in classify.READONLY_BASH
        assert verb in table, f"{verb!r} missing from tool_contracts.toml"
