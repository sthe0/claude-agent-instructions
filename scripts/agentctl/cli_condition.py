"""`condition-decide`: the setter's level decides an objection to a binding condition.

Difficulty removed: a replan that would break a binding condition could be made acceptable by the
session that was bound by it, because the only record of a decision was the plan file the bound
session writes. A replan that names a condition it breaks (`replan --breaks-condition`) now mints
an objection with its own id, stored beside the record the replan's plan carries, and stays refused
until the level that set the condition has decided that objection here. The decision is written
where the reader of check 13 reads it (the setter level's own record, or `root_conditions` for the
root), never into the raising session's record, and a session below the setter's level cannot make
it.
"""
from __future__ import annotations

import os
import time

from lib import config_root

from . import sub_plan
from .cli_common import SetterSite, _require
from .cli_order import _no_marked_answer_refusal, _user_answered_after
from .directive import Directive
from .dispatch import Runner
from .own_contour import OWN_SESSION_ENV
from .state import SessionState, condition_decide_marker
from .store import StateStore

AGENT_ACTOR = "agent"


def _objections_of(state: SessionState) -> list[dict]:
    """Every objection a session holds: in the records its plans carry and, for a root
    session, its own."""
    found = list(state.root_conditions.get("objections") or [])
    for raw in state.sub_plan_records.values():
        found += [o for o in raw.get("objections") or []]
    return found


def _visible_objections(state: SessionState, store: StateStore) -> list[dict]:
    """The objections this session may decide: its own and those of the sessions linked below."""
    found = _objections_of(state)
    for child in sub_plan.linked_children(state, store):
        found += _objections_of(child)
    return found


def condition_decider_blockers(*, objection: dict, value: str, decider: SessionState,
                               decider_stack: int, by_user: bool) -> list[str]:
    """Why `decider` may not decide `objection` ([] == it may). The setter level keeps its decision
    in its own session: only that session decides, an agent only while its running plan is the
    setter's own or one above it (a session below the setter's level is the one that raised the
    objection), and the root level's decision is the user's."""
    site = objection.get("site") or {}
    owner = site.get("owner", "")
    if owner != decider.session_id:
        return [f"objection {objection['id']!r} is decided by the level that set the condition, "
                f"which keeps its decisions in session {owner!r}: run `condition-decide` there"]
    if by_user:
        return []
    if site.get("key") is None:
        return [f"the condition {objection['condition']!r} was set at the root level, so only the "
                f"user decides the objection: put it to the user and run `condition-decide --by "
                f"<name>` once their answer choosing the option marked "
                f"{condition_decide_marker(objection['id'], value)!r} is recorded"]
    if decider_stack > int(site.get("depth") or 0):
        return [f"this session is running a plan below the level that set condition "
                f"{objection['condition']!r} (its plan stack is {decider_stack} deep, the setter's "
                f"plan runs at {site.get('depth')}): finish or `pop-subplan` back to the setter's "
                f"plan, then decide"]
    return []


def cmd_condition_decide(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)

    def refuse(msg: str, **data) -> Directive:
        return Directive(False, state.node, "noop", msg, data=data or None)

    own = os.environ.get(OWN_SESSION_ENV, "").strip()
    if own and config_root.sanitize_session_id(own) != config_root.sanitize_session_id(state.session_id):
        return refuse("condition-decide records the decision of the calling session's own level: a "
                      "linked child names its own session through agentctl-own.py")
    value = (args.value or "").strip()
    new_text = (args.new_text or "").strip()
    if value not in sub_plan.SETTER_VALUES:
        return refuse(f"--value is one of {', '.join(sub_plan.SETTER_VALUES)}, not {value!r}")
    if value == "changed" and not new_text:
        return refuse("a `changed` decision carries the condition's new text: add --new-text")
    if value != "changed" and new_text:
        return refuse(f"--new-text belongs to a `changed` decision only; a `{value}` decision "
                      f"leaves the text as it stands")
    known = _visible_objections(state, store)
    objection = next((o for o in known if o.get("id") == args.objection), None)
    if objection is None:
        ids = ", ".join(sorted(o["id"] for o in known if o.get("status") == sub_plan.OBJECTION_OPEN))
        return refuse(f"no objection {args.objection!r} is on record in this session or the "
                      f"sessions linked below it; open objections: {ids or 'none'}")
    by = (args.by or "").strip() or AGENT_ACTOR
    by_user = by.casefold() != AGENT_ACTOR
    blockers = condition_decider_blockers(
        objection=objection, value=value, decider=state,
        decider_stack=len(state.plan_stack), by_user=by_user)
    if blockers:
        return refuse("; ".join(blockers), blockers=blockers)
    if by_user:
        marker = condition_decide_marker(objection["id"], value)
        state_file = config_root.resolve_agentctl_state_file(state.session_id)
        if state_file is None:
            return refuse("no state file for this session to read the answer ledger beside")
        since = float(objection.get("at") or 0.0)
        chosen, ledger_read = _user_answered_after(state_file, since, marker)
        if not chosen:
            return refuse(_no_marked_answer_refusal(
                f"deciding objection {objection['id']!r} as {by}", marker, ledger_read, since))
    site = objection["site"]
    earlier = [d for d in _site_decisions(state, site)
               if d.condition == objection["condition"] and d.value == "changed"]
    decision = {"condition": objection["condition"], "value": value, "setter": state.session_id,
                "decided_by": by, "objection": objection["id"],
                "new_text": new_text, "version": len(earlier) + 1 if value == "changed" else 0}
    _write_decision(state, site, decision)
    state.log("condition_decide", objection=objection["id"], condition=objection["condition"],
              value=value, by=by, at=time.time())
    store.save(state)
    return Directive(True, state.node, "noop",
                     f"objection {objection['id']} is decided {value} by {by}"
                     + (f": condition {objection['condition']} now reads {new_text!r}"
                        if value == "changed" else ""))


def _setter_site_of(state: SessionState, site: dict) -> SetterSite:
    return SetterSite(state, site.get("key"), None, int(site.get("depth") or 0))


def _site_decisions(state: SessionState, site: dict) -> list:
    return _setter_site_of(state, site).decisions()


def _write_decision(state: SessionState, site: dict, decision: dict) -> None:
    _setter_site_of(state, site).write_decision(decision)


def register(sub, add):
    sp = add("condition-decide")
    sp.add_argument("--session", required=True)
    sp.add_argument("--objection", required=True,
                    help="the id of the objection a replan raised with --breaks-condition")
    sp.add_argument("--value", required=True, choices=list(sub_plan.SETTER_VALUES),
                    help="`changed` or `withdrawn` lifts the refusal of that objection; `kept` "
                         "keeps the condition and the refusal")
    sp.add_argument("--new-text", dest="new_text", default="",
                    help="the condition's new text; with `changed` only")
    sp.add_argument("--by", default=AGENT_ACTOR,
                    help="`agent` (default; the setter's own level, not the root), or the user's "
                         "name on the user's marked answer")
