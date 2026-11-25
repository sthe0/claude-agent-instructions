"""`condition-decide`: the setter's level decides an objection to a binding condition.

Difficulty removed: a replan that would break a binding condition could be made acceptable by the
session that was bound by it, because the only record of a decision was the plan file the bound
session writes. A replan that names a condition it breaks (`replan --breaks-condition`) now mints
an objection with its own id, stored beside the record the replan's plan carries, and stays refused
until the level that set the condition has decided that objection here. The decision is written
where the reader of check 13 reads it (the setter level's own record, or `root_conditions` for the
root), and for a condition inherited from above the nearest level also in the record the raising
session's plan carries. The setter level's list is the single sequence every reader resolves a
condition's text and version from, so two sessions bound by one condition never see different
versions. The decision is never made by the raising session, and a session below the setter's
level cannot make it.
The predicate that says who may decide is `gates.condition_decider_blockers`.
"""
from __future__ import annotations

import os
import time

from lib import config_root

from . import gates, sub_plan
from .cli_common import _objection_decisions, _objection_site, _require, _state_dir, _state_lock
from .cli_order import _no_marked_answer_refusal, _user_answered_after
from .directive import Directive
from .dispatch import Runner
from .own_contour import OWN_SESSION_ENV
from .state import SessionState, condition_decide_marker
from .store import StateStore

AGENT_ACTOR = "agent"


def _descendants(state: SessionState, store: StateStore) -> list[SessionState]:
    """Every session linked below `state`, at any depth."""
    found, seen, queue = [], {state.session_id}, [state]
    while queue:
        for child in sub_plan.linked_children(queue.pop(0), store):
            if child.session_id not in seen:
                seen.add(child.session_id)
                found.append(child)
                queue.append(child)
    return found


def _holders(state: SessionState, store: StateStore) -> list[SessionState]:
    """The sessions whose objections this session may decide: itself and those linked below it,
    at any depth (a condition set at the root is decided at the root whichever session below it
    raised the objection)."""
    return [state, *_descendants(state, store)]


def _matches(state: SessionState, store: StateStore, objection_id: str) -> list[tuple[dict, SessionState]]:
    return [(o, holder) for holder in _holders(state, store)
            for o in sub_plan.session_objections(holder) if o.get("id") == objection_id]


_NOT_WRITTEN = ("the decision is recorded at the level that set the condition, but what belongs in "
                "session {session} (which raised objection {objection}) is not: {why}; run "
                "`condition-decide` again with the same objection to write it")


def _write_into_raising_session(state: SessionState, holder: SessionState, objection: dict,
                                decision: dict, store: StateStore) -> str | None:
    """Writes what the raising session keeps of `decision`, after the decider's own record holds
    it: a copy in the record its plan carries (for a condition that session inherits) and, for a
    `changed` text, the mark that makes its earlier plan reviews stale. The raising session is
    another one's state file, so the write is made under its state lock on a fresh load, as
    `_restamp_on_parent` does for the parent's approval; a lock that stays busy, or a failed write,
    gives the refusal text that names the repair (`_raising_session_behind`). A store with no state
    directory has no concurrent writer and needs no lock."""
    def write(raising: SessionState) -> None:
        if objection.get("inherited_into"):
            raising.sub_plan_records.setdefault(objection["inherited_into"]["key"], {}).setdefault(
                "setter_decisions", []).append(decision)
        if decision["value"] == "changed":
            raising.condition_changes[objection["id"]] = raising.next_record_seq

    if holder.session_id == state.session_id:
        write(holder)
        return None
    with _state_lock(store, holder.session_id) as locked:
        if not locked and _state_dir(store) is not None:
            return _NOT_WRITTEN.format(session=holder.session_id, objection=objection["id"],
                                       why="its state is being written by another command")
        try:
            fresh = store.load(holder.session_id) if locked else holder
            write(fresh)
            store.save(fresh)
        except OSError as failure:
            return _NOT_WRITTEN.format(session=holder.session_id, objection=objection["id"],
                                       why=f"writing it failed ({failure})")
    return None


def _recorded_decision(state: SessionState, objection: dict) -> dict | None:
    """The raw decision of the level that set the condition that answers `objection`, if its record
    holds one."""
    site = objection["site"]
    raw = ((state.root_conditions.get("decisions") if site.get("key") is None else
            (state.sub_plan_records.get(site["key"]) or {}).get("setter_decisions")) or [])
    return next((d for d in raw if d.get("objection") == objection["id"]), None)


def _raising_session_behind(holder: SessionState, objection: dict, decision: dict) -> bool:
    """Whether `holder` lacks what `_write_into_raising_session` writes for `decision`."""
    if decision["value"] == "changed" and objection["id"] not in holder.condition_changes:
        return True
    if not objection.get("inherited_into"):
        return False
    held = (holder.sub_plan_records.get(objection["inherited_into"]["key"]) or {}).get(
        "setter_decisions") or []
    return not any(d.get("objection") == objection["id"] for d in held)


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
    matches = _matches(state, store, args.objection)
    if not matches:
        open_ids = sorted(o["id"] for h in _holders(state, store) for o in sub_plan.session_objections(h)
                          if o.get("status") == sub_plan.OBJECTION_OPEN)
        return refuse(f"no objection {args.objection!r} is on record in this session or the "
                      f"sessions linked below it; open objections: {', '.join(open_ids) or 'none'}")
    if len(matches) > 1:
        return refuse(f"objection id {args.objection!r} is held by more than one session "
                      f"({', '.join(sorted(h.session_id for _, h in matches))}): the decision would "
                      f"answer an unknown one, so none is recorded")
    objection, holder = matches[0]
    by = (args.by or "").strip() or AGENT_ACTOR
    by_user = by.casefold() != AGENT_ACTOR
    blockers = gates.condition_decider_blockers(
        objection=objection, value=value, decider_session=state.session_id,
        decider_stack=len(state.plan_stack), by_user=by_user)
    if blockers:
        return refuse("; ".join(blockers), blockers=blockers)
    recorded = _recorded_decision(state, objection)
    if (recorded is not None and objection.get("status") == sub_plan.OBJECTION_OPEN
            and _raising_session_behind(holder, objection, recorded)):
        failed = _write_into_raising_session(state, holder, objection, recorded, store)
        if failed:
            return refuse(failed)
        store.save(state)
        return Directive(True, state.node, "noop",
                         f"objection {objection['id']} was already decided {recorded['value']}; what "
                         f"belongs in session {holder.session_id} is now written")
    earlier = _objection_decisions(objection, holder, store)
    if objection.get("status") != sub_plan.OBJECTION_OPEN or gates.objection_decision(objection, earlier):
        return refuse(f"objection {objection['id']} is already decided or used up, and takes no "
                      f"second decision: after `kept` run the replan with the condition kept, or "
                      f"run a replan naming the condition again, which raises a new objection")
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
    version = 0
    if value == "changed":
        version = gates.current_condition_text({"id": objection["condition"], "text": ""}, earlier)[1] + 1
    decision = {"condition": objection["condition"], "value": value, "setter": state.session_id,
                "decided_by": by, "objection": objection["id"],
                "new_text": new_text, "version": version}
    _objection_site(state, objection["site"]).write_decision(decision)
    state.log("condition_decide", objection=objection["id"], condition=objection["condition"],
              value=value, by=by, at=time.time())
    own_session = holder.session_id == state.session_id
    if own_session:
        _write_into_raising_session(state, holder, objection, decision, store)
    store.save(state)
    if not own_session:
        failed = _write_into_raising_session(state, holder, objection, decision, store)
        if failed:
            return refuse(failed)
    return Directive(True, state.node, "noop",
                     f"objection {objection['id']} is decided {value} by {by}"
                     + (f": condition {objection['condition']} now reads {new_text!r} (version {version})"
                        if value == "changed" else ""))


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
