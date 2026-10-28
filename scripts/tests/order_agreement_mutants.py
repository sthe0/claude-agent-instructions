"""Planted defects for the order-understanding tests: proof that a test goes red.

A test that has only ever passed proves nothing about its own sensitivity. Each mutant
below plants ONE real defect in the production code under test, by monkeypatching, and a
test earns trust only when the suite fails with the mutant active. The mutant is chosen by
the environment variable `ORDER_AGREEMENT_MUTANT`, so a subprocess (or a person at the
shell) can run the same test file twice: once clean, once mutated.

    ORDER_AGREEMENT_MUTANT=verdict_unchecked python3 -m pytest scripts/tests/test_order_understanding.py

The table is closed. An unknown name, or a mutant whose target no longer exists, ends the
run with exit code 4 (a usage error) instead of letting the suite pass unmutated — a
mutant that silently does nothing would read as a test that "caught" nothing and then as
a green control. Exit 1 is reserved for what the mutant is for: a test failed.

To add a mutant, add a row to MUTANTS and a test that goes red under it; the other order-
agreement stages share this harness. A mutant is only killable if:

  * the targeted test module installs the plant itself — `from order_agreement_mutants
    import mutant` (an autouse fixture), or its own autouse fixture that calls
    `apply_active_mutant(monkeypatch)`; conftest does not do it for you;
  * the production code calls the patched function through a module global or attribute,
    and the test reaches it the same way (`submission.fn(...)`, never `from submission
    import fn`, which freezes the original before the plant);
  * the assertion that kills it runs in-process, in the plant's own interpreter — a test
    that shells out to a subprocess never sees the monkeypatch, so a subprocess test is
    never a mutant target.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ENV = "ORDER_AGREEMENT_MUTANT"
USAGE_ERROR = 4


def _verdict_unchecked(mp):
    """The submission seam stops grading a proposal's verdict against PROPOSAL_VERDICTS."""
    mp.setattr("agentctl.submission._invalid_verdicts", lambda proposals: [], raising=True)


def _digest_ignores_constraints(mp):
    """understanding_digest stops hashing the constraint (`K:<id>`) parts."""
    from agentctl import plan

    original = plan.understanding_parts
    mp.setattr(
        plan,
        "understanding_parts",
        lambda doc: {k: v for k, v in original(doc).items() if not k.startswith("K:")},
        raising=True,
    )


def _completeness_unchecked(mp):
    """The completeness grade reports nothing for an incomplete understanding."""
    mp.setattr(
        "agentctl.submission.order_understanding_completeness_violations",
        lambda meta: [],
        raising=True,
    )


def _place_contributes_when_empty(mp):
    """order_place / order_scope add the understanding even when the order states none,
    which moves the key of every order that predates it."""
    mp.setattr("agentctl.plan._understanding_empty", lambda order: False, raising=True)


def _order_review_unbound(mp):
    """A standing order review no longer has to be about the understanding now presented."""
    mp.setattr("agentctl.gates._order_review_binds", lambda review, digest: True, raising=True)


def _order_anchor_unchecked(mp):
    """An order rendering is no longer held to show exactly the order's items as anchors."""
    mp.setattr("agentctl.cli._order_anchor_problems", lambda doc, text: ([], []), raising=True)


def _order_receipt_unbound(mp):
    """An order receipt is recorded without the understanding digest it stands for."""
    mp.setattr("agentctl.cli._order_receipt_understanding", lambda digest: None, raising=True)


def _order_stale_draft_presented(mp):
    """present-plan --kind order stops checking that the draft is the one order-submit recorded."""
    mp.setattr("agentctl.cli._draft_is_submitted", lambda state, digest: True, raising=True)


def _agree_without_ask(mp):
    """order-agree stops requiring a recorded user answer choosing the marked option."""
    mp.setattr("agentctl.cli._agreement_ask_answered", lambda state_file, since, marker: (True, True),
               raising=True)


def _divergence_ignored(mp):
    """The gate stops noticing that the plan's understanding differs from the agreed one."""
    mp.setattr("agentctl.gates._agreement_divergence", lambda agreed, current: [], raising=True)


def _challenge_not_recorded(mp):
    """A blocking `understanding:` concern is no longer taken up as a challenge."""
    mp.setattr("agentctl.cli._record_understanding_challenges",
               lambda state, review, cp, ids: None, raising=True)


def _understanding_routes_order_token(mp):
    """The `understanding:` concern token is routed as `order:`, so it is never a challenge."""
    mp.setattr("agentctl.gates.UNDERSTANDING_TOKEN", "order:", raising=True)


def _reraise_unmatched(mp):
    """A re-raise (`re:<id>`) no longer finds the challenge it restates."""
    mp.setattr("agentctl.gates.challenge_for_restatement", lambda state, restates: None, raising=True)


def _explicit_host_arms(mp):
    """Classify arms on whatever host was named, not on the host observed in the environment."""
    mp.setattr("agentctl.cli._arming_host", lambda: "claude", raising=True)


def _depth_flag_exempts(mp):
    """A caller-supplied recursion depth on the session exempts it from the gate."""
    mp.setattr("agentctl.cli._spawned_child", lambda state: state.recursion_depth >= 1, raising=True)


def _headless_channel_ignored(mp):
    """A configured headless delivery channel no longer exempts the session."""
    mp.setattr("agentctl.cli._headless_channel", lambda: False, raising=True)


_SCRIPTS = Path(__file__).resolve().parent.parent
_script_modules: dict = {}


def script_module(filename: str):
    """The hyphenated script `scripts/<filename>` loaded once as a module, so that a test
    and a mutant patch the same object."""
    if filename not in _script_modules:
        name = "oa_" + Path(filename).stem.replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _script_modules[filename] = module
    return _script_modules[filename]


def _rejected_proposal_filtered(mp):
    """A base-<s> section stops listing the rejected proposals."""
    from agentctl import render

    original = render._base_edge_proposals
    mp.setattr(render, "_base_edge_proposals",
               lambda doc, service: [p for p in original(doc, service) if p.verdict != "reject"],
               raising=True)


def _unit_base_omits_proposals(mp):
    """The unit:base bundle stops inlining the order's understanding."""
    mp.setattr("agentctl.render.render_order_understanding_md", lambda doc: [], raising=True)


def _draft_walk_empty(mp):
    """The driver's walk of an order draft has no unit to review."""
    mp.setattr(script_module("plan-review-topological.py"), "draft_walk",
               lambda: {"levels": []}, raising=True)


def _agreed_label_unconditional(mp):
    """The user-agreed label appears whatever digest was passed."""
    mp.setattr("agentctl.render._agreement_matches", lambda doc, agreed: True, raising=True)


def _agreed_digest_not_forwarded(mp):
    """The driver's spawn of unit:base is not told the agreement digest."""
    mp.setattr(script_module("plan-review-topological.py"), "forwarded_unit_digest",
               lambda digest: None, raising=True)


def _pair_digest_not_forwarded(mp):
    """The driver's spawn of a base-<s> pair is not told the agreement digest."""
    mp.setattr(script_module("plan-review-topological.py"), "forwarded_pair_digest",
               lambda digest: None, raising=True)


def _spawner_digest_dropped(mp):
    """The spawner accepts --agreed-understanding but never hands it to the renderer."""
    spawner = script_module("spawn-specialist.py")
    original = spawner.build_site_prompt

    def dropping(*args, **kwargs):
        kwargs.pop("agreed_understanding", None)
        return original(*args, **kwargs)

    mp.setattr(spawner, "build_site_prompt", dropping, raising=True)


def _spawner_refuses_draft(mp):
    """The spawner no longer recognizes a stageless order draft."""
    mp.setattr(script_module("spawn-specialist.py"), "load_order_draft",
               lambda path: (None, True), raising=True)


def _draft_recorded_as_topo(mp):
    """The driver records a draft's verdict under scope topo:unit:base, not order."""
    driver = script_module("plan-review-topological.py")
    original = driver.record_argv

    def as_topo(*args, **kwargs):
        kwargs["draft"] = False
        return original(*args, **kwargs)

    mp.setattr(driver, "record_argv", as_topo, raising=True)


MUTANTS = {
    "rejected_proposal_filtered": _rejected_proposal_filtered,
    "unit_base_omits_proposals": _unit_base_omits_proposals,
    "draft_walk_empty": _draft_walk_empty,
    "agreed_label_unconditional": _agreed_label_unconditional,
    "agreed_digest_not_forwarded": _agreed_digest_not_forwarded,
    "pair_digest_not_forwarded": _pair_digest_not_forwarded,
    "spawner_digest_dropped": _spawner_digest_dropped,
    "spawner_refuses_draft": _spawner_refuses_draft,
    "draft_recorded_as_topo": _draft_recorded_as_topo,
    "agree_without_ask": _agree_without_ask,
    "divergence_ignored": _divergence_ignored,
    "challenge_not_recorded": _challenge_not_recorded,
    "understanding_routes_order_token": _understanding_routes_order_token,
    "reraise_unmatched": _reraise_unmatched,
    "explicit_host_arms": _explicit_host_arms,
    "depth_flag_exempts": _depth_flag_exempts,
    "headless_channel_ignored": _headless_channel_ignored,
    "verdict_unchecked": _verdict_unchecked,
    "digest_ignores_constraints": _digest_ignores_constraints,
    "completeness_unchecked": _completeness_unchecked,
    "place_contributes_when_empty": _place_contributes_when_empty,
    "order_review_unbound": _order_review_unbound,
    "order_anchor_unchecked": _order_anchor_unchecked,
    "order_receipt_unbound": _order_receipt_unbound,
    "order_stale_draft_presented": _order_stale_draft_presented,
}


def active_mutant() -> str | None:
    """The mutant named in the environment, or None when none is requested."""
    return os.environ.get(ENV) or None


def apply_active_mutant(monkeypatch) -> str | None:
    """Plant the requested mutant through `monkeypatch`; None when none is requested.
    Fails closed (exit 4) on a name outside MUTANTS or a target that does not exist."""
    name = active_mutant()
    if name is None:
        return None
    if name not in MUTANTS:
        _usage_error(f"{ENV}={name!r} is not a known mutant; known: {sorted(MUTANTS)}")
    try:
        MUTANTS[name](monkeypatch)
    except (AttributeError, ImportError) as exc:
        _usage_error(f"mutant {name!r} has no target to patch: {exc}")
    return name


@pytest.fixture(autouse=True)
def mutant(monkeypatch):
    """The stage contract's fixture: `from order_agreement_mutants import mutant` in a
    test module plants the requested mutant for every test in it."""
    return apply_active_mutant(monkeypatch)


def _usage_error(message: str):
    """End the process with USAGE_ERROR. `pytest.exit` is not used: raised from a fixture
    it crashes pytest's own warnings plugin in sessionfinish and the run ends with exit 1,
    the code that means 'a test caught the mutant'; raised at import it is a collection
    error, exit 2. Neither may be mistaken for a kill."""
    sys.stderr.write(f"order_agreement_mutants: {message}\n")
    sys.stderr.flush()
    os._exit(USAGE_ERROR)
