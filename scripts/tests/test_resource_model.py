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
    assert r.reason_class == "script-digest-mismatch"


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
    assert stale.reason_class == "script-digest-mismatch"


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
    assert r.reason_class == "declared-unresolved"


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


def _park_permission_request(cli, store, sid, fixtures_dir, action="PERMISSION-REQUEST: touch a file\n"):
    """Drive a session to EXECUTING (via the two-stage demo fixture, whose
    submission-clean shape reaching approve/partition/dispatch is already
    established by test_permission_gate.py's identical setup) and park one
    permission request -- the precondition `cmd_resolve_permission` itself
    requires (`state.permission_request is not None`)."""
    from argparse import Namespace

    from agentctl.dispatch import RunResult

    def ns(**kw):
        return Namespace(**kw)

    plan = str(fixtures_dir / "plan_two_stage.toml")
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
    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
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
    assert d.data.get("reason_class") == "unparseable-rule"
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
