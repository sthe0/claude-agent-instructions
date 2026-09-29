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
from pathlib import Path

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

    svc = resources.ServiceResource("example-cloud", "spend")
    assert svc.covers(resources.ServiceResource("example-cloud", "spend"))
    assert not svc.covers(resources.ServiceResource("other-service", "spend"))
    assert not svc.covers(resources.ServiceResource("example-cloud", "read"))
    assert not svc.covers(resources.SpecialistResource("developer"))


def test_covers_dataset_exact():
    resources = _resources_module()

    ds = resources.DatasetResource("warehouse", "clicks-daily")
    assert ds.covers(resources.DatasetResource("warehouse", "clicks-daily"))
    assert not ds.covers(resources.DatasetResource("warehouse", "clicks-hourly"))
    assert not ds.covers(resources.DatasetResource("other-system", "clicks-daily"))
    assert not ds.covers(resources.ServiceResource("clicks-daily", "read"))


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


# --- Checkpoint (b): script-effects registry, order digest, order-approvals ledger ---


def _script_effects_module():
    spec = importlib.util.find_spec("agentctl.script_effects")
    assert spec is not None, "agentctl.script_effects module not found"
    from agentctl import script_effects

    return script_effects


def _order_approvals_module():
    spec = importlib.util.find_spec("agentctl.order_approvals")
    assert spec is not None, "agentctl.order_approvals module not found"
    from agentctl import order_approvals

    return order_approvals


def _plan_module():
    spec = importlib.util.find_spec("agentctl.plan")
    assert spec is not None, "agentctl.plan module not found"
    from agentctl import plan

    return plan


def _state_module():
    spec = importlib.util.find_spec("agentctl.state")
    assert spec is not None, "agentctl.state module not found"
    from agentctl import state

    return state


def _submission_module():
    spec = importlib.util.find_spec("agentctl.submission")
    assert spec is not None, "agentctl.submission module not found"
    from agentctl import submission

    return submission


def _cli_module():
    spec = importlib.util.find_spec("agentctl.cli")
    assert spec is not None, "agentctl.cli module not found"
    from agentctl import cli

    return cli


def _land_branch_registry_venue(tmp_path, land_branch_text=None):
    """A minimal venue tree with `scripts/land-branch.py` and a matching
    `scripts/script_effects.toml` entry pinned to its live sha256."""
    import hashlib

    venue = tmp_path / "venue"
    (venue / "scripts").mkdir(parents=True)
    (venue / ".git").mkdir()
    real_land_branch = Path(__file__).resolve().parent.parent / "land-branch.py"
    text = land_branch_text if land_branch_text is not None else real_land_branch.read_text(encoding="utf-8")
    script_path = venue / "scripts" / "land-branch.py"
    script_path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (venue / "scripts" / "script_effects.toml").write_text(
        f'[[script]]\npath = "scripts/land-branch.py"\nsha256 = "{digest}"\nresolver = "land_branch"\n',
        encoding="utf-8",
    )
    return venue, digest


def test_repo_script_resolved_by_effects_registry(tmp_path):
    tc = _tool_contracts_module()
    venue, _digest = _land_branch_registry_venue(tmp_path)

    r = tc.resolve_command(
        "python3 scripts/land-branch.py --branch feature --keep-branch", str(venue)
    )
    assert r.status == "resolved"
    kinds = sorted(res.kind for res in r.resources)
    assert "vcs_ref" in kinds


def test_script_effects_entry_refused_on_digest_mismatch(tmp_path):
    tc = _tool_contracts_module()
    venue, _digest = _land_branch_registry_venue(tmp_path)
    # Edit the script after the registry entry was pinned.
    (venue / "scripts" / "land-branch.py").write_text("# tampered\n", encoding="utf-8")

    r = tc.resolve_command(
        "python3 scripts/land-branch.py --branch feature --keep-branch", str(venue)
    )
    assert r.status == "unresolved"
    assert r.reason_class == "adhoc-undeclared"


def test_stage_effects_untrusted_after_script_edit(tmp_path):
    """Same guarantee as digest-mismatch, exercised directly against
    `script_effects.resolve_script` (the registry's own entry point) rather
    than through the full command-line resolver, so the digest check is
    pinned independently of `tool_contracts.py`'s dispatch."""
    script_effects = _script_effects_module()
    venue, digest = _land_branch_registry_venue(tmp_path)
    table = script_effects.load_script_effects_table(str(venue / "scripts" / "script_effects.toml"))

    ok = script_effects.resolve_script(
        str(venue / "scripts" / "land-branch.py"), ["--branch", "x", "--keep-branch"], str(venue), table=table
    )
    assert ok is not None and ok.status == "resolved"

    (venue / "scripts" / "land-branch.py").write_text("changed\n", encoding="utf-8")
    stale = script_effects.resolve_script(
        str(venue / "scripts" / "land-branch.py"), ["--branch", "x", "--keep-branch"], str(venue), table=table
    )
    assert stale is not None and stale.status == "unresolved"
    assert stale.reason_class == "adhoc-undeclared"


def test_undeclared_adhoc_script_is_unresolved(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = outside / "adhoc.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    r = tc.resolve_command(f"python3 {script}", str(venue))
    assert r.status == "unresolved"
    assert r.reason_class == "contract-unresolved"


def test_helper_script_edit_changes_unresolved_command_identity(tmp_path):
    """Identity is content-bound (REQ3): a script OUTSIDE the venue subtree
    is hashed directly as a literal operand (in-venue operands are excluded
    from identity, since any venue write resource already covers them — see
    `_compute_identity`'s own docstring), so editing it must change the
    unresolved command's identity tuple."""
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = outside / "adhoc.py"
    script.write_text("print('one')\n", encoding="utf-8")

    r1 = tc.resolve_command(f"python3 {script}", str(venue))
    script.write_text("print('two')\n", encoding="utf-8")
    r2 = tc.resolve_command(f"python3 {script}", str(venue))

    assert r1.status == "unresolved" and r2.status == "unresolved"
    assert r1.identity != r2.identity


def test_missing_operand_encoded_absent_changes_identity_when_created(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = outside / "not_yet_created.py"

    r_missing = tc.resolve_command(f"python3 {script}", str(venue))
    script.write_text("print('hi')\n", encoding="utf-8")
    r_present = tc.resolve_command(f"python3 {script}", str(venue))

    assert r_missing.status == "unresolved" and r_present.status == "unresolved"
    assert r_missing.identity != r_present.identity


def test_sibling_helper_edit_changes_unresolved_identity(tmp_path):
    """A script's identity also folds in a capped directory-listing digest
    of its own containing directory when the script lies outside the venue
    (`_dir_listing_identity`) — so editing a SIBLING file the invoked script
    does not itself appear as an operand for still changes identity."""
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.py").write_text("import helper\n", encoding="utf-8")
    (outside / "helper.py").write_text("X = 1\n", encoding="utf-8")

    r1 = tc.resolve_command(f"python3 {outside / 'main.py'}", str(venue))
    (outside / "helper.py").write_text("X = 2\n", encoding="utf-8")
    r2 = tc.resolve_command(f"python3 {outside / 'main.py'}", str(venue))

    assert r1.status == "unresolved" and r2.status == "unresolved"
    assert r1.identity != r2.identity


def test_land_branch_registry_entry_lists_every_effect(tmp_path):
    """A `--branch`+`--keep-branch` (no --remote-only) invocation resolves to
    BOTH the push ref and the git-common-dir write — the two effects
    land-branch.py's own module docstring documents for that argv shape."""
    tc = _tool_contracts_module()
    venue, _digest = _land_branch_registry_venue(tmp_path)

    r = tc.resolve_command(
        "python3 scripts/land-branch.py --branch feature --keep-branch", str(venue)
    )
    assert r.status == "resolved"
    kinds = sorted(res.kind for res in r.resources)
    assert kinds == ["file", "vcs_ref"]


def test_no_command_or_registry_entry_resolves_to_land(tmp_path):
    """No code path anywhere in the resolver (contract table, custom
    resolvers, or the script-effects registry) may ever produce a
    `VcsRefResource` with `op="land"` — landing is decided solely by the
    separately checkpointed landed-spec resolver (R1/C1)."""
    tc = _tool_contracts_module()
    venue, _digest = _land_branch_registry_venue(tmp_path)

    r = tc.resolve_command(
        "python3 scripts/land-branch.py --branch feature --keep-branch", str(venue)
    )
    for res in r.resources:
        if res.kind == "vcs_ref":
            assert res.op != "land"


def test_order_digest_covers_order_fields_not_derived_ones():
    from argparse import Namespace

    plan = _plan_module()
    state = _state_module()

    def _doc(customer_id="alice", req_text="the thing works", coverage=None, malformed=None):
        order = state.Order(
            customer_id=customer_id,
            customer="the customer",
            functional_place="the norm this serves",
            requires_traceability=True,
            requirements=[state.Requirement(id="R1", text=req_text, derivation="d1")],
            coverage=coverage if coverage is not None else {"R1": ["stage 1"]},
            malformed=malformed if malformed is not None else [],
            requirements_dropped=[],
        )
        meta = Namespace(
            goal="ship the thing",
            done_criterion="it works",
            criterion_type="measurable",
            weight_class="substantive",
            order=order,
        )
        return Namespace(meta=meta)

    base = plan.order_digest(_doc())
    same_but_derived_changed = plan.order_digest(
        _doc(coverage={"R1": ["stage 2"]}, malformed=["something"])
    )
    assert base == same_but_derived_changed

    different_customer = plan.order_digest(_doc(customer_id="bob"))
    assert base != different_customer

    different_req_text = plan.order_digest(_doc(req_text="a different thing works"))
    assert base != different_req_text


def test_customer_approve_stamps_order_ledger_casefolded():
    order_approvals = _order_approvals_module()
    resources = _resources_module()

    order_sha = "deadbeef" * 8
    order_approvals.record_approval(
        order_sha,
        plan_sha256="plan1",
        resources=[resources.FileResource("/tmp/x", "write")],
        unresolved_identities=[],
        stage_effects=[],
        by="Alice",
        at="2026-09-29T00:00:00Z",
    )
    approved = order_approvals.approved_resources(order_sha)
    assert len(approved) == 1
    assert approved[0].path == "/tmp/x"


def test_non_customer_approve_does_not_stamp_ledger():
    order_approvals = _order_approvals_module()

    order_sha = "cafef00d" * 8
    # Simulate `cmd_approve` only calling record_approval for the customer —
    # a non-customer --by never reaches this module at all, so the ledger
    # for an order nobody has approved-as-customer stays empty.
    assert order_approvals.get(order_sha)["records"] == []
    assert order_approvals.approved_resources(order_sha) == []


def test_approve_by_reserved_agent_refused():
    order_approvals = _order_approvals_module()
    state = _state_module()

    with pytest.raises(ValueError):
        order_approvals.record_approval(
            "abc123" * 10,
            plan_sha256="plan1",
            resources=[],
            unresolved_identities=[],
            stage_effects=[],
            by=state.AGENT_ACTOR,
            at="2026-09-29T00:00:00Z",
        )
    with pytest.raises(ValueError):
        order_approvals.record_approval(
            "abc123" * 10,
            plan_sha256="plan1",
            resources=[],
            unresolved_identities=[],
            stage_effects=[],
            by=state.AGENT_ACTOR.upper(),
            at="2026-09-29T00:00:00Z",
        )


def test_customer_id_reserved_agent_identity_rejected():
    from argparse import Namespace

    submission = _submission_module()
    state = _state_module()

    order = state.Order(customer_id=state.AGENT_ACTOR, customer="c", functional_place="p")
    violations = submission._order_violations(Namespace(order=order))
    assert any(state.AGENT_ACTOR in v for v in violations)

    order_cased = state.Order(customer_id=state.AGENT_ACTOR.upper(), customer="c", functional_place="p")
    violations_cased = submission._order_violations(Namespace(order=order_cased))
    assert any(state.AGENT_ACTOR.upper() in v for v in violations_cased)


def test_ledger_survives_reset_renegotiation_and_new_session(tmp_path):
    order_approvals = _order_approvals_module()
    resources = _resources_module()

    order_sha = "0123abcd" * 8
    order_approvals.record_approval(
        order_sha,
        plan_sha256="plan1",
        resources=[resources.FileResource("/tmp/y", "write")],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-29T00:00:00Z",
    )
    # No reset/renegotiation call exists in this module at all — re-reading
    # under a brand new root argument (simulating "a new session, same
    # default root") still sees the record.
    again = order_approvals.approved_resources(order_sha)
    assert len(again) == 1


def test_same_task_id_other_order_reads_empty_ledger():
    order_approvals = _order_approvals_module()
    resources = _resources_module()

    order_a = "111111" * 10 + "1111"
    order_b = "222222" * 10 + "2222"
    order_approvals.record_approval(
        order_a,
        plan_sha256="plan1",
        resources=[resources.FileResource("/tmp/z", "write")],
        unresolved_identities=[],
        stage_effects=[],
        by="alice",
        at="2026-09-29T00:00:00Z",
    )
    assert order_approvals.approved_resources(order_b) == []


def test_stage_scoped_customer_grant_counts_once_scoped_does_not():
    order_approvals = _order_approvals_module()
    resources = _resources_module()

    order_sha = "abcdef01" * 8
    order_approvals.record_customer_grant(
        order_sha,
        resource=resources.FileResource("/tmp/scoped", "write"),
        by="alice",
        at="2026-09-29T00:00:00Z",
    )
    approved = order_approvals.approved_resources(order_sha)
    assert len(approved) == 1
    assert approved[0].path == "/tmp/scoped"
    # A --scope once grant is never recorded here at all (A2) — simulated by
    # simply never calling record_customer_grant for it; the ledger for a
    # once-only order stays empty.
    once_only_order = "fedcba98" * 8
    assert order_approvals.approved_resources(once_only_order) == []


def test_runtime_grant_under_other_order_not_counted():
    order_approvals = _order_approvals_module()
    resources = _resources_module()

    order_a = "aaaaaaaa" * 8
    order_b = "bbbbbbbb" * 8
    order_approvals.record_customer_grant(
        order_a,
        resource=resources.FileResource("/tmp/only-a", "write"),
        by="alice",
        at="2026-09-29T00:00:00Z",
    )
    assert order_approvals.approved_resources(order_b) == []
    assert len(order_approvals.approved_resources(order_a)) == 1


# --- REQ5: resolve-permission --by agent self-grant --------------------------

def _order_plan_path(tmp_path, customer_id="acme"):
    """A minimal, submission-agnostic plan file carrying `[meta.order]` --
    `plan.load_plan` (the LENIENT loader `cmd_resolve_permission`'s self-grant
    path reads via) never runs the submission validator, so this only needs
    to be loadable, not submission-clean."""
    path = tmp_path / "plan_order.toml"
    path.write_text(
        "[meta]\n"
        'weight_class = "small_change"\n'
        'task_id = "demo-order"\n'
        'goal = "g"\n'
        'done_criterion = "dc"\n'
        'criterion_type = "measurable"\n'
        "\n"
        "[meta.order]\n"
        f'customer_id = "{customer_id}"\n'
        'customer = "the customer"\n'
        'functional_place = "the norm this serves"\n'
        "\n"
        "[[stage]]\n"
        "index = 1\n"
        'title = "Scaffold module"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "module file exists"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "ok"\n'
        "depends_on = []\n",
        encoding="utf-8",
    )
    return path


def _order_plan_with_grant_path(tmp_path, customer_id="acme", allow_rules=()):
    """Like `_order_plan_path`, plus a declared `[stage.grants]` on stage 1 --
    needed by a test whose self_grant check must see the stage's own
    EFFECTIVE (declared) grants, not only the order-approvals ledger."""
    rules_toml = ", ".join(f'"{r}"' for r in allow_rules)
    path = tmp_path / "plan_order_grant.toml"
    path.write_text(
        "[meta]\n"
        'weight_class = "small_change"\n'
        'task_id = "demo-order-grant"\n'
        'goal = "g"\n'
        'done_criterion = "dc"\n'
        'criterion_type = "measurable"\n'
        "\n"
        "[meta.order]\n"
        f'customer_id = "{customer_id}"\n'
        'customer = "the customer"\n'
        'functional_place = "the norm this serves"\n'
        "\n"
        "[[stage]]\n"
        "index = 1\n"
        'title = "Scaffold module"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "module file exists"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "ok"\n'
        "depends_on = []\n"
        "\n"
        "[stage.grants]\n"
        f"allow = [{rules_toml}]\n",
        encoding="utf-8",
    )
    return path


def _park_permission_request(
    cli, store, sid, fixtures_dir, action="PERMISSION-REQUEST: touch a file\n", plan_path=None,
):
    """Drive a session to EXECUTING (via the two-stage demo fixture, whose
    submission-clean shape reaching approve/partition/dispatch is already
    established by test_permission_gate.py's identical setup) and park one
    permission request -- the precondition `cmd_resolve_permission` itself
    requires (`state.permission_request is not None`). `plan_path`, when
    given, is submitted/approved/dispatched INSTEAD of the two-stage demo
    fixture -- needed by any test whose self_grant check must run at
    DISPATCH time against a plan carrying `[meta.order]` or declared
    `[stage.grants]`, since `state.plan_path` is whatever `cmd_submit_plan`
    set at park time, not whatever a test overrides afterward."""
    from argparse import Namespace

    from agentctl.dispatch import RunResult

    def ns(**kw):
        return Namespace(**kw)

    plan = plan_path if plan_path is not None else str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="res-demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    runner = lambda argv: RunResult(0, stdout=action)
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner,
                     perm_checker=lambda a: False)


def test_resolve_permission_records_author(store, fixtures_dir):
    from argparse import Namespace

    cli = _cli_module()
    _park_permission_request(cli, store, "author1", fixtures_dir)
    d = cli.cmd_resolve_permission(
        Namespace(session="author1", decision="granted", scope="once", by="alice",
                  rules=None, add_dirs=None),
        store=store,
    )
    assert d.ok
    state = store.load("author1")
    assert state.history[-1]["event"] == "resolve_permission"
    assert state.history[-1]["by"] == "alice"


def test_agent_self_grant_within_approved_resources_accepted(store, fixtures_dir, tmp_path):
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    order_approvals = _order_approvals_module()
    resources = _resources_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    doc = plan_mod.load_plan(str(plan_path))
    target = tmp_path / "approved_file.txt"
    target.write_text("hi", encoding="utf-8")
    order_approvals.record_approval(
        plan_mod.order_digest(doc),
        plan_sha256="p1",
        resources=[resources.FileResource(str(target), "write")],
        unresolved_identities=[],
        stage_effects=[],
        by="acme",
        at="2026-09-29T00:00:00Z",
    )

    sid = "self-grant-ok"
    _park_permission_request(cli, store, sid, fixtures_dir)
    state = store.load(sid)
    state.plan_path = str(plan_path)
    store.save(state)

    d = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                  rules=[f"Edit(//{target})"], add_dirs=None),
        store=store,
    )
    assert d.ok, d.detail
    state = store.load(sid)
    assert state.permission_request is None


def test_agent_self_grant_outside_approved_resources_refused(store, fixtures_dir, tmp_path):
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    order_approvals = _order_approvals_module()
    resources = _resources_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    doc = plan_mod.load_plan(str(plan_path))
    approved_target = tmp_path / "approved_only.txt"
    approved_target.write_text("hi", encoding="utf-8")
    requested_target = tmp_path / "never_approved.txt"
    requested_target.write_text("hi", encoding="utf-8")
    order_approvals.record_approval(
        plan_mod.order_digest(doc),
        plan_sha256="p1",
        resources=[resources.FileResource(str(approved_target), "write")],
        unresolved_identities=[],
        stage_effects=[],
        by="acme",
        at="2026-09-29T00:00:00Z",
    )

    sid = "self-grant-outside"
    _park_permission_request(cli, store, sid, fixtures_dir)
    state = store.load(sid)
    state.plan_path = str(plan_path)
    store.save(state)

    d = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                  rules=[f"Edit(//{requested_target})"], add_dirs=None),
        store=store,
    )
    assert not d.ok
    assert d.data.get("reason_class") == "not-approved"
    state = store.load(sid)
    # A refused self-grant leaves the parked request untouched -- nothing
    # partially recorded, the same fail-closed shape validate_rule's own
    # refusal below gives a malformed human-materialized rule.
    assert state.permission_request is not None


def test_agent_self_grant_unresolved_rule_refused(store, fixtures_dir, tmp_path):
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")

    sid = "self-grant-unresolved"
    _park_permission_request(cli, store, sid, fixtures_dir)
    state = store.load(sid)
    state.plan_path = str(plan_path)
    store.save(state)

    d = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                  rules=["NotARule"], add_dirs=None),
        store=store,
    )
    assert not d.ok
    assert d.data.get("reason_class") == "contract-unresolved"
    state = store.load(sid)
    assert state.permission_request is not None


def test_agent_self_grant_excludes_once_scope_runtime_grant(store, fixtures_dir, tmp_path):
    """Finding #4 (review round 2, root cause): both self-grant call sites
    used to reuse `_effective_stage_grants` unmodified for their own
    self_approved coverage set -- the SAME function `cmd_dispatch` uses for
    ordinary denial/coverage classification, which correctly counts a
    `scope: "once"` runtime grant (a single-launch bypass) as coverage. A
    once-scope grant a HUMAN made for one specific re-launch must not let a
    LATER agent self-grant (`--scope stage`) lean on it as a standing
    approval for the same resource -- `_effective_stage_grants_for_self_grant`
    excludes it. No order-approvals ledger entry exists for this resource
    either, so the only way the self-grant could succeed is via the
    once-scope entry leaking into its coverage set."""
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    target = tmp_path / "once_scope_target.txt"
    target.write_text("hi", encoding="utf-8")

    sid = "self-grant-once-scope-excluded"
    _park_permission_request(cli, store, sid, fixtures_dir)
    state = store.load(sid)
    state.plan_path = str(plan_path)
    active_index = state.active_stage().index
    store.save(state)

    # A human grants the resource, but only for ONE re-launch (--scope once).
    d_user = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="once", by="alice",
                  rules=[f"Edit(//{target})"], add_dirs=None),
        store=store,
    )
    assert d_user.ok, d_user.detail
    state = store.load(sid)
    assert state.permission_request is None
    runtime_entries = state.runtime_grants.get(str(active_index), [])
    assert any(e.get("scope") == "once" for e in runtime_entries)

    # A fresh PERMISSION-REQUEST for the SAME resource is parked (e.g. a
    # later stage step needing the same edit again) -- constructed directly
    # rather than via a second `cmd_dispatch`, since dispatch's OWN (broader)
    # coverage check would short-circuit on the once-scope entry before ever
    # reaching self-grant classification; this test targets
    # `cmd_resolve_permission`'s self-grant coverage set specifically.
    state = store.load(sid)
    state.permission_request = state_mod.PermissionRequest(
        action="PERMISSION-REQUEST: edit the file again\n", stage_index=active_index,
    )
    store.save(state)

    d_agent = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                  rules=[f"Edit(//{target})"], add_dirs=None),
        store=store,
    )
    assert not d_agent.ok
    assert d_agent.data.get("reason_class") == "not-approved"
    state = store.load(sid)
    assert state.permission_request is not None


def test_agent_path_audit_records_env_overrides(store, fixtures_dir, tmp_path, monkeypatch):
    """The two env overrides that steer which ledger/contract-table a
    self-grant resolves against are recorded on the history entry
    unconditionally -- a human-attributed resolution reads them too, so the
    audit trail is uniform regardless of who `--by` names."""
    from argparse import Namespace

    cli = _cli_module()

    contracts_override = str(tmp_path / "custom_tool_contracts.toml")
    ledger_override = str(tmp_path / "custom_ledger_dir")
    monkeypatch.setenv("AGENTCTL_ORDER_APPROVALS_DIR", ledger_override)
    monkeypatch.setenv("AGENTCTL_TOOL_CONTRACTS", contracts_override)

    sid = "audit-env"
    _park_permission_request(cli, store, sid, fixtures_dir)
    d = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="once", by="alice",
                  rules=None, add_dirs=None),
        store=store,
    )
    assert d.ok
    entry = store.load(sid).history[-1]
    assert entry["order_approvals_dir_override"] == ledger_override
    assert entry["tool_contracts_override"] == contracts_override


# --- Checkpoint (c): plan-resources CLI + contract-resolution edge cases -----


def _plan_resources_module():
    spec = importlib.util.find_spec("agentctl.plan_resources")
    assert spec is not None, "agentctl.plan_resources module not found"
    from agentctl import plan_resources

    return plan_resources


def test_readonly_commands_need_no_resource(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()

    for cmd in ("ls -la", "cat foo.txt", "grep -n x foo.txt", "git status"):
        r = tc.resolve_command(cmd, str(venue))
        assert r.status == "resolved", cmd
        assert r.resources == [], cmd


def test_sed_in_place_writes_file_plain_sed_does_not(tmp_path):
    """sed's write target depends on -i's presence and on its own script
    text (an embedded w/W/e command writes or executes regardless of -i);
    neither is decidable from the closed command grammar alone, so both the
    plain and the -i form are unresolved."""
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    target = venue / "f.txt"
    target.write_text("hi", encoding="utf-8")

    r_plain = tc.resolve_command(f"sed 's/hi/bye/' {target}", str(venue))
    assert r_plain.status == "unresolved"
    assert r_plain.reason_class == "contract-unresolved"

    r_inplace = tc.resolve_command(f"sed -i 's/hi/bye/' {target}", str(venue))
    assert r_inplace.status == "unresolved"
    assert r_inplace.reason_class == "contract-unresolved"


def test_find_exec_and_awk_are_unresolved(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()

    # Each command below carries a non-literal marker of its own (`*` in the
    # glob, `{`/`$` in the -exec block and the awk script) ahead of find's
    # or awk's own program-effect status, so the command-shape check's
    # residual-syntax refusal fires before either program is ever dispatched.
    r_find_exec = tc.resolve_command("find . -name '*.py' -exec rm {} \\;", str(venue))
    assert r_find_exec.status == "unresolved"
    assert r_find_exec.reason_class == "residual-syntax"

    r_find_plain = tc.resolve_command("find . -name '*.py'", str(venue))
    assert r_find_plain.status == "unresolved"
    assert r_find_plain.reason_class == "residual-syntax"

    r_awk = tc.resolve_command("awk '{print $1}' foo.txt", str(venue))
    assert r_awk.status == "unresolved"
    assert r_awk.reason_class == "residual-syntax"


def test_redirect_cp_tee_targets_become_file_resources(tmp_path):
    """A generic shell redirect target and cp's/tee's own write targets are
    all outside the reviewed closed command grammar now -- none of them
    parses an arbitrary operand into a FileResource any more, despite this
    test's own (unchanged, name-pinned) name."""
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    src = venue / "src.txt"
    src.write_text("hi", encoding="utf-8")
    dst = venue / "dst.txt"
    redirected = venue / "out.txt"

    r_redirect = tc.resolve_command(f"echo hi > {redirected}", str(venue))
    assert r_redirect.status == "unresolved"
    assert r_redirect.reason_class == "contract-unresolved"

    r_cp = tc.resolve_command(f"cp {src} {dst}", str(venue))
    assert r_cp.status == "unresolved"
    assert r_cp.reason_class == "contract-unresolved"

    tee_target = venue / "tee_out.txt"
    r_tee = tc.resolve_command(f"tee {tee_target}", str(venue))
    assert r_tee.status == "unresolved"
    assert r_tee.reason_class == "contract-unresolved"


def test_dollar_backtick_heredoc_subshell_segments_unresolved(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()

    for cmd in ("echo $(date)", "echo `date`", "(cd /tmp && ls)"):
        r = tc.resolve_command(cmd, str(venue))
        assert r.status == "unresolved", cmd
        assert r.reason_class == "residual-syntax", cmd

    # A `$(...)` INSIDE a heredoc body is invisible to the nested-execution
    # check -- the body is stripped before that check ever runs -- so it
    # must never be classified `reason_class="residual-syntax"` on that
    # account.
    heredoc_cmd = "cat <<'EOF'\nprint($(date))\nEOF"
    r_heredoc = tc.resolve_command(heredoc_cmd, str(venue))
    assert not (r_heredoc.status == "unresolved" and r_heredoc.reason_class == "residual-syntax")


def test_negative_control_and_final_check_commands_resolved(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()

    r_nc = tc.resolve_command(
        "pytest -q scripts/tests/test_resource_model.py -p no:cacheprovider", str(venue)
    )
    assert r_nc.status == "resolved"

    # `echo` is outside the reviewed read-only program table (it was never
    # in the whitelist's explicit list), so a final-check command built on
    # it is unresolved like any other unreviewed program -- `git status` is
    # the in-whitelist equivalent for a no-op final check.
    r_final = tc.resolve_command("git status", str(venue))
    assert r_final.status == "resolved"
    assert r_final.resources == []


def test_pytest_resolves_to_venue_subtree(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()

    r = tc.resolve_command("pytest -q scripts/tests/test_resource_model.py", str(venue))
    assert r.status == "resolved"
    assert len(r.resources) == 1
    assert r.resources[0].kind == "file"
    assert r.resources[0].path == str(venue)


def test_contract_table_loader_one_entry_per_program(tmp_path):
    tc = _tool_contracts_module()
    dup_table = tmp_path / "dup_contracts.toml"
    dup_table.write_text(
        '[[program]]\nname = "ls"\neffect = "none"\n\n'
        '[[program]]\nname = "ls"\neffect = "none"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        tc.load_contract_table(str(dup_table))


def _bare_stage(state_mod, index, *, executor="in_thread", landed=None):
    verify_kind = state_mod.CheckKind.LANDED.value if landed is not None else state_mod.CheckKind.SHELL.value
    return state_mod.Stage(
        index=index, title=f"s{index}",
        subject=state_mod.Subject(material="m", result="img"),
        means=state_mod.Means(means="Edit", method="do"),
        actor=state_mod.Actor(executor=executor),
        criterion=state_mod.Criterion(
            criterion_type=state_mod.CriterionType.MEASURABLE.value, done_criterion="c",
            verify_kind=verify_kind, landed=landed,
        ),
        outcome=state_mod.Outcome(status=state_mod.StageStatus.ACTIVE.value),
    )


def test_spawn_and_landed_yield_specialist_and_vcs_ref():
    state_mod = _state_module()
    plan_resources = _plan_resources_module()
    resources = _resources_module()

    spawn_stage = _bare_stage(state_mod, 1, executor="spawn:developer")
    spawn_resources = plan_resources._stage_spawn_resources(spawn_stage)
    assert len(spawn_resources) == 1
    assert isinstance(spawn_resources[0], resources.SpecialistResource)
    assert spawn_resources[0].role == "developer"
    assert plan_resources._stage_landed_resources(spawn_stage) == []

    landed = state_mod.LandedSpec(target="main", remote="origin", delivered_stage=0)
    landed_stage = _bare_stage(state_mod, 2, landed=landed)
    landed_resources = plan_resources._stage_landed_resources(landed_stage)
    assert len(landed_resources) == 1
    assert isinstance(landed_resources[0], resources.VcsRefResource)
    assert landed_resources[0].op == "land"
    assert plan_resources._stage_spawn_resources(landed_stage) == []

    plain_stage = _bare_stage(state_mod, 3)
    assert plan_resources._stage_spawn_resources(plain_stage) == []
    assert plan_resources._stage_landed_resources(plain_stage) == []


def test_landed_resource_does_not_cover_push_grant():
    state_mod = _state_module()
    plan_resources = _plan_resources_module()
    resources = _resources_module()

    landed = state_mod.LandedSpec(target="main", remote="origin", delivered_stage=0)
    landed_stage = _bare_stage(state_mod, 1, landed=landed)
    landed_resources = plan_resources._stage_landed_resources(landed_stage)
    assert len(landed_resources) == 1
    landed_res = landed_resources[0]
    assert landed_res.op == "land"

    push_grant = resources.VcsRefResource("origin", "main", "push")
    assert not push_grant.covers(landed_res)
    assert not landed_res.covers(push_grant)


def test_executed_script_directory_hash_capped_falls_back_to_opaque(tmp_path):
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = outside / "main.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    for i in range(70):
        (outside / f"sibling_{i}.txt").write_text("x", encoding="utf-8")

    r = tc.resolve_command(f"python3 {script}", str(venue))
    assert r.status == "unresolved"
    assert r.identity[-1] == ("opaque",)


def test_covers_and_identity_compare_realpath(tmp_path):
    resources = _resources_module()

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "f.txt").write_text("hi", encoding="utf-8")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    approved = resources.FileResource(str(real_dir), "write")
    assert approved.covers(resources.FileResource(str(link_dir / "f.txt"), "write"))

    approved_via_link = resources.FileResource(str(link_dir), "write")
    assert approved_via_link == resources.FileResource(str(real_dir), "write")


#: (id, command, reason-substring): every force/delete/mirror/prune/all/tags-
#: equivalent push, every `+`/`:`/empty-destination refspec form (leading,
#: trailing, or flag-after-the-remote), and a zero-refspec push (with or
#: without a remote operand at all) resolves `unresolved`. Each force-or-
#: delete-equivalent variant is refused by the SAME C5 rule in
#: `_resolve_git` and therefore shares one reason substring; the two
#: zero-refspec variants share the other.
_C5_REFUSED_PUSH_CASES = [
    ("force", "git push --force origin main", "force-or-delete-push"),
    ("dash-f", "git push -f origin main", "force-or-delete-push"),
    ("force-with-lease", "git push --force-with-lease origin main", "force-or-delete-push"),
    ("force-with-lease-value", "git push --force-with-lease=deadbeef origin main", "force-or-delete-push"),
    ("force-if-includes", "git push --force-if-includes origin main", "force-or-delete-push"),
    ("mirror", "git push --mirror origin main", "force-or-delete-push"),
    ("prune", "git push --prune origin main", "force-or-delete-push"),
    ("all", "git push --all origin main", "force-or-delete-push"),
    ("tags", "git push --tags origin main", "force-or-delete-push"),
    ("delete", "git push --delete origin main", "force-or-delete-push"),
    ("dash-d-delete", "git push -d origin main", "force-or-delete-push"),
    ("delete-after-remote", "git push origin --delete main", "force-or-delete-push"),
    ("plus-refspec", "git push origin +HEAD:main", "force-or-delete-push"),
    ("colon-refspec", "git push origin :main", "force-or-delete-push"),
    ("empty-dest-refspec", "git push origin HEAD:", "force-or-delete-push"),
    ("zero-refspec", "git push origin", "no explicit refspec"),
    ("bare-push", "git push", "no explicit refspec"),
]
_C5_REFUSED_PUSH_IDS = [case[0] for case in _C5_REFUSED_PUSH_CASES]
_C5_REFUSED_PUSH_COMMANDS = [case[1] for case in _C5_REFUSED_PUSH_CASES]


def test_plain_push_resolves_to_single_vcs_ref_resource(tmp_path):
    """Positive control for the C5 refusal cluster below: an ordinary
    literal-refspec push (no force/delete/mirror/prune/all/tags flag, no
    +/:/empty-destination refspec, no zero-refspec form) resolves cleanly to
    exactly the one `VcsRefResource` it names -- proving the refusals in
    `test_force_and_delete_push_not_covered_by_push` are specific to the C5
    shapes, not to `git push` at large."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    plain = tc.resolve_command("git push origin main", str(tmp_path))
    assert plain.status == "resolved"
    assert plain.resources == [resources.VcsRefResource("origin", "main", "push")]


@pytest.mark.parametrize(
    "cmd,reason_substring", [(case[1], case[2]) for case in _C5_REFUSED_PUSH_CASES], ids=_C5_REFUSED_PUSH_IDS
)
def test_force_and_delete_push_not_covered_by_push(tmp_path, cmd, reason_substring):
    """C5: every force/delete/mirror/prune/all/tags-equivalent push, every
    +-prefixed or :-prefixed or empty-destination refspec, and a
    zero-refspec push resolves `unresolved` with an EMPTY resource list --
    never a "covered" resource set -- so none of them can ever be mistaken
    for a plain push to the same remote/branch a ledger approved."""
    tc = _tool_contracts_module()
    venue = tmp_path / "venue"
    venue.mkdir()

    r = tc.resolve_command(cmd, str(venue))
    assert r.status == "unresolved", cmd
    assert r.reason_class == "contract-unresolved", (cmd, r.reason_class)
    assert reason_substring in (r.reason or ""), (cmd, r.reason)
    assert r.resources == [], cmd


def test_force_and_delete_push_refused_via_resolve_permission_self_grant(store, fixtures_dir, tmp_path):
    """The same C5 refusals, exercised through the CLI self-grant path
    (`resolve-permission --by agent`), not just the bare resolver -- under a
    ledger holding only `vcs_ref(origin, main, push)`, with a positive
    control proving an ordinary (non-force, non-delete) push to that same
    destination DOES self-grant."""
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    order_approvals = _order_approvals_module()
    resources = _resources_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    doc = plan_mod.load_plan(str(plan_path))
    order_approvals.record_approval(
        plan_mod.order_digest(doc),
        plan_sha256="p1",
        resources=[resources.VcsRefResource("origin", "main", "push")],
        unresolved_identities=[],
        stage_effects=[],
        by="acme",
        at="2026-09-29T00:00:00Z",
    )

    for i, cmd in enumerate(_C5_REFUSED_PUSH_COMMANDS):
        sid = f"c5-cli-refused-{i}"
        _park_permission_request(cli, store, sid, fixtures_dir, plan_path=str(plan_path))
        d = cli.cmd_resolve_permission(
            Namespace(session=sid, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                      rules=[f"Bash({cmd})"], add_dirs=None),
            store=store,
        )
        assert not d.ok, (cmd, d.detail)
        assert d.data.get("reason_class") == "contract-unresolved", (cmd, d.data)
        state = store.load(sid)
        assert state.permission_request is not None, cmd

    # Positive control: a plain push to the SAME remote/destination the
    # ledger approved self-grants cleanly -- proving the refusals above are
    # specific to the C5 flags/refspec shapes, not to push commands at large.
    sid_ok = "c5-cli-positive-control"
    _park_permission_request(cli, store, sid_ok, fixtures_dir, plan_path=str(plan_path))
    d_ok = cli.cmd_resolve_permission(
        Namespace(session=sid_ok, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                  rules=["Bash(git push origin HEAD:main)"], add_dirs=None),
        store=store,
    )
    assert d_ok.ok, d_ok.detail
    assert store.load(sid_ok).permission_request is None


def test_plan_resources_cli_lists_typed_resources(fixtures_dir):
    from argparse import Namespace

    plan_resources = _plan_resources_module()

    args = Namespace(
        plan=str(fixtures_dir / "plan_two_stage.toml"),
        format="json", corpus=None, commands_file=None,
        dump_commands=None, report_json=None, require_no_unknown_program=False,
    )
    d = plan_resources.cmd_plan_resources(args)
    assert d.ok
    assert "stages" in d.data
    stage_1 = d.data["stages"]["1"]
    for key in ("rules", "add_dirs", "spawn", "landed", "stage_effects"):
        assert key in stage_1

    args_text = Namespace(
        plan=str(fixtures_dir / "plan_two_stage.toml"),
        format="compact", corpus=None, commands_file=None,
        dump_commands=None, report_json=None, require_no_unknown_program=False,
    )
    d_text = plan_resources.cmd_plan_resources(args_text)
    assert d_text.ok
    assert "# Plan resources" in d_text.detail


# --- Checkpoint (c5): dispatch's Resource:/Rule: self-grant resolution -------


def test_permission_request_resource_line_and_self_grant_directive(store, fixtures_dir, tmp_path):
    """A dispatched child's `Rule:` line, resolved by the ENGINE (never the
    child's own `Resource:` line), decides self_grant. An agreeing `Resource:`
    line changes nothing; a DISAGREEING one routes to the user with a reason
    naming both, rather than trusting either alone. `self_grant` only PARKS
    the request (finding #4) -- a follow-up `resolve-permission --by agent`
    is what actually materializes it and clears `permission_request`."""
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    order_approvals = _order_approvals_module()
    resources = _resources_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    doc = plan_mod.load_plan(str(plan_path))
    order_approvals.record_approval(
        plan_mod.order_digest(doc),
        plan_sha256="p1",
        resources=[resources.VcsRefResource("origin", "main", "push")],
        unresolved_identities=[],
        stage_effects=[],
        by="acme",
        at="2026-09-29T00:00:00Z",
    )

    # An agreeing Resource: line changes nothing -- self_grant still fires,
    # decided from the Rule: line's own resolution.
    d_agree = _park_permission_request(
        cli, store, "resource-line-agree", fixtures_dir,
        action=(
            "PERMISSION-REQUEST: push the branch\n"
            "Rule: Bash(git push origin main)\n"
            "Resource: vcs_ref(origin, main, push)\n"
        ),
        plan_path=str(plan_path),
    )
    assert d_agree.action == "self_grant", d_agree.detail
    assert store.load("resource-line-agree").permission_request is not None
    d_agree_grant = cli.cmd_resolve_permission(
        Namespace(session="resource-line-agree", decision="granted", scope="stage",
                  by=state_mod.AGENT_ACTOR, rules=["Bash(git push origin main)"], add_dirs=None),
        store=store,
    )
    assert d_agree_grant.ok, d_agree_grant.detail
    assert store.load("resource-line-agree").permission_request is None

    # A DISAGREEING Resource: line (names a different destination than the
    # Rule: line the engine itself resolves) must never be trusted over the
    # engine's own resolution -- routed to the user, reason names both.
    d_disagree = _park_permission_request(
        cli, store, "resource-line-disagree", fixtures_dir,
        action=(
            "PERMISSION-REQUEST: push the branch\n"
            "Rule: Bash(git push origin main)\n"
            "Resource: vcs_ref(origin, other-branch, push)\n"
        ),
        plan_path=str(plan_path),
    )
    assert d_disagree.action == "ask_user_permission"
    disagreement = d_disagree.data.get("resource_disagreement")
    assert disagreement, d_disagree.data
    assert "vcs_ref(origin, other-branch, push)" in disagreement
    assert "Bash(git push origin main)" in disagreement
    state = store.load("resource-line-disagree")
    assert state.permission_request is not None


def test_dispatch_self_grant_resolve_permission_redispatch_carries_grant(store, fixtures_dir, tmp_path):
    """Finding #4 end-to-end: a dispatch `self_grant` directive only PARKS
    the request (finding #4's bug: it used to clear `permission_request`
    with nothing actually persisted); `resolve-permission --by agent` is
    what MATERIALIZES it as a runtime grant on the stage
    (`state.runtime_grants`); a re-dispatch afterward carries that grant
    forward into the stage's pre-launch coverage (`_effective_stage_grants`),
    which is what dispatch's own `self_covered` check (`grants.
    grant_covers_call`) reads. Re-issuing the SAME already-materialized
    `Rule:` line on the next dispatch therefore no longer reaches
    self_grant/ask_user_permission at all -- it is caught as a
    materialization defect (the request claims to still need what the
    stage's own grants already cover), which is the proof the runtime grant
    carried forward: this exact classification is unreachable unless
    `coverage` on the second dispatch call already contains the resource
    `resolve-permission --by agent` persisted on the first."""
    from argparse import Namespace

    from agentctl.dispatch import RunResult

    cli = _cli_module()
    state_mod = _state_module()
    order_approvals = _order_approvals_module()
    resources = _resources_module()
    plan_mod = _plan_module()

    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    doc = plan_mod.load_plan(str(plan_path))
    order_approvals.record_approval(
        plan_mod.order_digest(doc),
        plan_sha256="p1",
        resources=[resources.VcsRefResource("origin", "main", "push")],
        unresolved_identities=[],
        stage_effects=[],
        by="acme",
        at="2026-09-29T00:00:00Z",
    )

    sid = "redispatch-carries-grant"
    action = "PERMISSION-REQUEST: push the branch\nRule: Bash(git push origin main)\n"
    d1 = _park_permission_request(cli, store, sid, fixtures_dir, action=action, plan_path=str(plan_path))
    assert d1.action == "self_grant", d1.detail
    assert store.load(sid).permission_request is not None

    d_grant = cli.cmd_resolve_permission(
        Namespace(session=sid, decision="granted", scope="stage",
                  by=state_mod.AGENT_ACTOR, rules=["Bash(git push origin main)"], add_dirs=None),
        store=store,
    )
    assert d_grant.ok, d_grant.detail
    state_after_grant = store.load(sid)
    assert state_after_grant.permission_request is None
    stage_index = state_after_grant.active_stage().index
    runtime_entries = state_after_grant.runtime_grants.get(str(stage_index), [])
    assert any(e.get("rule") == "Bash(git push origin main)" for e in runtime_entries), runtime_entries

    runner = lambda argv: RunResult(0, stdout=action)
    d2 = cli.cmd_dispatch(
        Namespace(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner, perm_checker=lambda a: False,
    )
    assert not d2.ok
    assert d2.marker == "OVERCOME-DIFFICULTY", d2.detail
    assert "already covered by its materialized grants" in d2.detail, d2.detail


def test_effective_rules_are_checked_against_resources(store, fixtures_dir, tmp_path):
    """The stage's own EFFECTIVE (declared+derived) grants are checked as
    RESOURCES, not as rule TEXT: a declared push rule naming a different
    source ref for the same destination still covers a differently-spelled
    request for that destination -- something the old string-only
    `grants.grant_covers_call` self_covered short-circuit cannot see (it
    requires literal command-string equality), making this a genuinely new
    self_grant path, not a restatement of the pre-existing one. `self_grant`
    only PARKS the request (finding #4) -- resolving it needs a follow-up
    `resolve-permission --by agent`."""
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()

    # No order-approvals ledger entry at all -- coverage must come purely
    # from the stage's own declared [stage.grants], not the customer ledger.
    plan_path = _order_plan_with_grant_path(
        tmp_path, customer_id="acme",
        allow_rules=["Bash(git push origin featureX:main)"],
    )

    d = _park_permission_request(
        cli, store, "effective-rules-resource-check", fixtures_dir,
        action=(
            "PERMISSION-REQUEST: push the branch\n"
            "Rule: Bash(git push origin HEAD:main)\n"
        ),
        plan_path=str(plan_path),
    )
    assert d.action == "self_grant", d.detail
    assert store.load("effective-rules-resource-check").permission_request is not None
    d_grant = cli.cmd_resolve_permission(
        Namespace(session="effective-rules-resource-check", decision="granted", scope="stage",
                  by=state_mod.AGENT_ACTOR, rules=["Bash(git push origin HEAD:main)"], add_dirs=None),
        store=store,
    )
    assert d_grant.ok, d_grant.detail
    assert store.load("effective-rules-resource-check").permission_request is None


def test_wildcard_rule_line_resolves_over_its_argument_tail(store, fixtures_dir, tmp_path):
    """A `:*`-wildcard-tail Bash rule is UNRESOLVED regardless of what its
    concrete argument would otherwise resolve to (REQ5 wildcard-tail case) --
    checked for equal `reason_class == "wildcard-tail"` across all three
    surfaces that resolve a rule: `resolve-permission --by agent`, `dispatch`,
    and `plan-resources --format json`. A literal (non-wildcard) push rule is
    the positive control, proving the refusal is specific to the wildcard
    tail and not to these two commands in general."""
    from argparse import Namespace

    cli = _cli_module()
    state_mod = _state_module()
    order_approvals = _order_approvals_module()
    resources = _resources_module()
    plan_mod = _plan_module()
    plan_resources = _plan_resources_module()

    wildcard_rules = ["Bash(find:*)", "Bash(python3 scripts/land-branch.py:*)"]

    # The stage declares NO grants at all here -- both the wildcard
    # land-branch.py rule used by part (2) and the literal push rule used by
    # part (3) must stay undeclared, so `grant_covers_call`'s literal-text
    # `self_covered` short-circuit (which matches a DECLARED rule's own
    # `:*`-stripped text regardless of the wildcard-tail RESOURCE semantics
    # `plan_resources.resolve_rule_grant` applies -- see `_segment_covered`)
    # never fires and both requests reach the new self_grant/ask_user_permission
    # fork this test is exercising, with coverage coming only from the
    # order-approvals ledger below.
    plan_path = _order_plan_path(tmp_path, customer_id="acme")
    doc = plan_mod.load_plan(str(plan_path))
    order_approvals.record_approval(
        plan_mod.order_digest(doc),
        plan_sha256="p1",
        resources=[resources.VcsRefResource("origin", "main", "push")],
        unresolved_identities=[],
        stage_effects=[],
        by="acme",
        at="2026-09-29T00:00:00Z",
    )

    # (1) resolve-permission --by agent refuses each outright.
    for i, wildcard_rule in enumerate(wildcard_rules):
        sid = f"wildcard-resolve-{i}"
        _park_permission_request(cli, store, sid, fixtures_dir, plan_path=str(plan_path))
        d = cli.cmd_resolve_permission(
            Namespace(session=sid, decision="granted", scope="stage", by=state_mod.AGENT_ACTOR,
                      rules=[wildcard_rule], add_dirs=None),
            store=store,
        )
        assert not d.ok
        assert d.data.get("reason_class") == "wildcard-tail", (wildcard_rule, d.data)

    # (2) dispatch: a Rule: line with a wildcard tail never self-grants, with
    # the SAME reason_class -- equal, not merely "also unresolved". Only the
    # land-branch.py rule is exercised here, not `find:*`: `find` sits in
    # every spawn kind's baseline read-only inspection bucket
    # (lib.kind_baselines._READ_ONLY_INSPECTION), unioned into the stage's
    # effective coverage BEFORE dispatch ever reaches rule resolution -- so a
    # `Rule: Bash(find:*)` request is already self_covered by that baseline
    # and correctly routes to the (distinct, also-correct) materialization-
    # defect branch instead. That path has no reason_class of its own to
    # compare, so asserting wildcard-tail equality there would assert
    # something structurally unreachable, not a real property of this rule.
    d_land_branch = _park_permission_request(
        cli, store, "wildcard-dispatch-land-branch", fixtures_dir,
        action="PERMISSION-REQUEST: run it\nRule: Bash(python3 scripts/land-branch.py:*)\n",
        plan_path=str(plan_path),
    )
    assert d_land_branch.action == "ask_user_permission", d_land_branch.detail
    assert d_land_branch.data.get("reason_class") == "wildcard-tail", d_land_branch.data

    # (3) dispatch positive control: a literal (non-wildcard) push rule,
    # undeclared on the stage but matching the ledger entry, DOES self-grant
    # via resource resolution -- proving (1)/(2) refuse the wildcard tail
    # specifically, not push commands, or dispatch's self_grant path, in
    # general.
    d_control = _park_permission_request(
        cli, store, "wildcard-control-push", fixtures_dir,
        action="PERMISSION-REQUEST: push the branch\nRule: Bash(git push origin main)\n",
        plan_path=str(plan_path),
    )
    assert d_control.action == "self_grant", d_control.detail

    # (4) plan-resources --format json: the same two wildcard rules, declared
    # on a plan, list as unresolved with the SAME reason_class.
    wildcard_plan_dir = tmp_path / "wildcard-plan-dir"
    wildcard_plan_dir.mkdir()
    wildcard_plan_path = _order_plan_with_grant_path(
        wildcard_plan_dir, customer_id="acme", allow_rules=wildcard_rules,
    )
    args = Namespace(
        plan=str(wildcard_plan_path), format="json", corpus=None, commands_file=None,
        dump_commands=None, report_json=None, require_no_unknown_program=False,
    )
    d_pr = plan_resources.cmd_plan_resources(args)
    assert d_pr.ok
    rules_by_text = {r["rule"]: r for r in d_pr.data["stages"]["1"]["rules"]}
    for wildcard_rule in wildcard_rules:
        assert rules_by_text[wildcard_rule]["status"] == "unresolved"
        assert rules_by_text[wildcard_rule]["reason_class"] == "wildcard-tail", wildcard_rule


def test_self_grants_fixture_resolves_declared_rules_to_resources(fixtures_dir, tmp_path):
    """`grant_plans/self-grants.toml` (test_grant_derivation.py /
    test_spawn_stage2_image.py's real-plan-shaped fixture, pinned verbatim by
    test_self_grants_fixture_declared_rules_present_verbatim) exercises the
    resource layer without touching the fixture's own rule text -- a
    modification there would risk weakening that verbatim pin."""
    plan_mod = _plan_module()
    plan_resources = _plan_resources_module()

    fixture_path = fixtures_dir / "grant_plans" / "self-grants.toml"
    text = fixture_path.read_text(encoding="utf-8").replace(
        "__CLAUDE_AGENT_HOME__", str(tmp_path / "agent-home")
    )
    plan_path = tmp_path / "self-grants.toml"
    plan_path.write_text(text, encoding="utf-8")

    doc = plan_mod.load_plan(str(plan_path), strict=False)
    venue = plan_mod._venue_for(doc)
    stage1 = next(s for s in doc.stages if s.index == 1)

    stage1_resources = plan_resources.compute_stage_resources(stage1, venue)

    # The literal push rule resolves to a concrete VcsRefResource, matching
    # `_resolve_git`'s destination-ref-only keying (git push origin
    # perm-grants -> refs/heads/perm-grants on origin).
    resources = _resources_module()
    assert resources.VcsRefResource("origin", "perm-grants", "push") in stage1_resources.resources

    # The wildcard-tail rule pinned verbatim alongside it stays unresolved,
    # by the SAME reason_class the dispatch/resolve-permission/plan-resources
    # surfaces above agree on -- not silently dropped or miscounted as
    # resolved just because it came from a real-plan-shaped fixture rather
    # than a synthetic one.
    assert ("wildcard-tail", "Bash(python3 scripts/probe-hook-decision-semantics.py:*)") in (
        stage1_resources.unresolved
    )
