"""Carry-forward of review, premise and order-coverage records across a plan edit.

A replan and a PLAN_READY re-submission both replace the plan version the review records
are bound to. `plan.change_tier` says how far an edit reaches; this module decides, from
that tier, which records still describe the new version and re-binds them to it:

* tier 0-1 (bookkeeping, final venue): every record carries;
* tier 2 (check-only or operative leaves, interface untouched): every record carries
  except the review of each edited stage, and the pair records touching an edited stage
  are *held* — kept without a review of the new content, satisfied until a unit reviewer
  escalates them (`release_holds`);
* tier 3-4 and an edit whose tier cannot be computed: nothing changes, so every record is
  exactly as stale as it would be without this module.

A record counts only if it described the OLD version. One bound to the new version
already (its `plan_sha256` is the new file's) is left alone, and so is a record that was
already stale — carrying never revives a verdict. `apply` mutates `state` only when it
carries; it never records a review round.
"""
from __future__ import annotations

import copy

from .gates import pair_binding_hash
from .plan import (
    WHOLE_STAGE_ELEMENT,
    PlanChange,
    PlanDoc,
    is_legacy_review_id,
    is_unit_id,
    pair_binding,
    pair_content,
    pair_currency_hash,
    pair_currency_keys,
    part_digest_map,
    plan_interface_digests,
    plan_meta_digest,
    plan_meta_element_keys,
    plan_stage_digests,
    review_currency_hash,
    review_pairs,
    review_units,
    split_pair_id,
    stage_norm_keys,
    stage_token,
    unit_currency_hash,
    unit_node,
)
from .premise import (
    invalidate_stale_dispositions,
    invalidate_stale_order_dispositions,
    order_binding_key,
    parse_target,
)
from .stage_norm import StageNorm
from .state import PAIR_BINDING_KEYS, PAIR_CONTENT_KEYS, PlanPairReview, PlanReview, SessionState

#: Highest tier of an edit whose records carry.
MAX_CARRIED_TIER = 2

_KEY_BOUND = frozenset({"researched", "escalated", "assumed"})

#: A pair or unit record carries only if it is a pass or a decision; a `revise` stays as
#: stale as it was, so the review it asked for is still owed.
_HOLDABLE_VERDICTS = frozenset({"pass", "override"})


def apply(
    state: SessionState, old_doc: PlanDoc, new_doc: PlanDoc, *,
    new_path: str, new_sha: str, change: "PlanChange | None",
    old_path: "str | None" = None, old_sha: "str | None" = None,
) -> dict:
    """Carry `state`'s records from `old_doc` onto `new_doc` as far as `change` allows.
    Returns `{"tier", "carried", "owed", "records", "held"}`: `owed` the stages whose own
    review is owed again, `records` a label per record carried, `held` the pair ids
    carried without a review."""
    report = {"tier": None if change is None else change.tier, "carried": False,
              "owed": [], "records": [], "held": []}
    if change is None or change.tier > MAX_CARRIED_TIER:
        return report
    owed = _owed(change)
    ctx = _Ctx(state, old_doc, new_doc, new_path, new_sha, old_path, old_sha,
               change.tier, owed, report)
    ctx.reviews()
    ctx.pair_records()
    ctx.premise()
    report["owed"] = sorted(owed)
    report["carried"] = bool(report["records"])
    return report


def carry_premise(
    state: SessionState, old_doc: PlanDoc, new_doc: PlanDoc, change: "PlanChange | None",
) -> list[str]:
    """Carry only `state`'s premise-question and order-coverage stamps from `old_doc` onto
    `new_doc`, for a caller that must read the premise gate of an edit that is refused
    before `apply` runs. Returns a label per stamp moved; `[]` when the tier carries none."""
    report = {"records": []}
    if change is None or change.tier > MAX_CARRIED_TIER:
        return []
    _Ctx(state, old_doc, new_doc, "", "", None, None, change.tier, _owed(change), report).premise()
    return report["records"]


def _owed(change: PlanChange) -> set:
    """The stages whose own review an edit of this change owes again (stage tier 2)."""
    return {index for index, c in change.stages.items() if c.tier == 2}


def release_holds(state: SessionState, node: "int | str | None" = None) -> list[str]:
    """Clear the held mark of every pair (of those touching `node`, when given): the pairs
    are judged by their content again, so each is owed a review of the content the held
    pass never saw. Returns their ids."""
    released = []
    for pid, record in state.plan_pair_reviews.items():
        if record.held and (node is None or node in split_pair_id(pid)):
            record.held = ""
            released.append(pid)
    return sorted(released)


def describe(report: dict) -> str:
    """One line for a Directive: what was carried, at which tier, and what is owed."""
    if not report.get("carried"):
        return ""
    text = f"carried {len(report['records'])} record(s) onto the new version (tier {report['tier']})"
    if report["owed"]:
        text += f"; owed again: stage(s) {', '.join(str(i) for i in report['owed'])}"
    if report["held"]:
        text += f"; held without a review: {', '.join(report['held'])}"
    return text


class _Ctx:
    def __init__(self, state, old, new, new_path, new_sha, old_path, old_sha, tier, owed, report):
        self.state, self.old, self.new = state, old, new
        self.new_path, self.new_sha = new_path, new_sha
        self.old_path, self.old_sha = old_path, old_sha
        self.tier, self.owed, self.report = tier, owed, report
        self.old_keys = plan_stage_digests(old)
        self.new_keys = plan_stage_digests(new)
        self.old_ifaces = plan_interface_digests(old)
        self.new_ifaces = plan_interface_digests(new)
        self.old_parts = part_digest_map(old)
        self.new_parts = part_digest_map(new)
        self.old_meta, self.new_meta = plan_meta_digest(old), plan_meta_digest(new)

    def _touches_owed(self, review_id: str) -> bool:
        ends = (unit_node(review_id),) if is_unit_id(review_id) else split_pair_id(review_id)
        return any(end in self.owed for end in ends)

    def _binds_old(self, path: str, sha: str) -> bool:
        if self.old_path is not None and path != self.old_path:
            return False
        if not sha:
            return True
        if sha == self.new_sha:
            return False
        return self.old_sha is None or sha == self.old_sha

    def _restamped_sha(self, recorded: str) -> str:
        """The new version's digest for a record that attested the old one; an unattested
        record stays unattested — no reviewer ever gave the new bytes' digest."""
        return self.new_sha if recorded else ""

    def _mark(self, record, from_sha: str) -> None:
        record.carried_tier = max(record.carried_tier or 0, self.tier)
        record.carried_from_sha = from_sha or record.carried_from_sha

    # --- whole-plan, stage-scoped and pass records -------------------------------------

    def reviews(self) -> None:
        state = self.state
        if state.plan_review is not None:
            state.plan_review = self._restamp(state.plan_review, "whole")
        for scope, record in list(state.plan_stage_reviews.items()):
            state.plan_stage_reviews[scope] = self._restamp(record, scope)
        for scope, record in list(state.plan_review_passes.items()):
            state.plan_review_passes[scope] = self._restamp(record, f"pass:{scope}")

    def _restamp(self, record: PlanReview, label: str) -> PlanReview:
        if not self._binds_old(record.plan_path, record.plan_sha256):
            return record
        if record.reviewed_meta_digest != self.old_meta:
            return record
        scope_index = _stage_scope_index(record.scope)
        if scope_index in self.owed:
            return record
        updated = copy.copy(record)
        updated.plan_path = self.new_path
        updated.plan_sha256 = self._restamped_sha(record.plan_sha256)
        updated.reviewed_meta_digest = self.new_meta
        updated.reviewed_stage_keys = _rekey(
            record.reviewed_stage_keys, self.old_keys, self.new_keys, skip=self.owed)
        updated.reviewed_interface_keys = _rekey(
            record.reviewed_interface_keys, self.old_ifaces, self.new_ifaces)
        updated.part_digests = {
            token: (self.new_parts.get(token, digest)
                    if digest == self.old_parts.get(token) and _token_index(token) not in self.owed
                    else digest)
            for token, digest in record.part_digests.items()}
        if record.reviewed_pair_bindings is not None:
            updated.reviewed_pair_bindings = self._rebase(
                record.reviewed_pair_bindings, pair_binding_hash, review_pairs)
        if record.reviewed_pair_currency is not None:
            updated.reviewed_pair_currency = self._rebase(
                record.reviewed_pair_currency, pair_currency_hash, review_pairs)
        if record.reviewed_unit_currency is not None:
            updated.reviewed_unit_currency = self._rebase(
                record.reviewed_unit_currency, unit_currency_hash, review_units)
        self._mark(updated, self.old_sha or record.plan_sha256)
        self.report["records"].append(label)
        return updated

    def _rebase(self, recorded: dict, hash_of, ids_of) -> dict:
        """Re-stamp each id whose recorded hash is the old version's current one, except
        an id that touches an owed stage: that one must stay different from the new
        version's so the review of the edited content is still owed."""
        rebased = dict(recorded)
        old_ids, new_ids = set(ids_of(self.old)), set(ids_of(self.new))
        for rid, value in recorded.items():
            if rid not in old_ids or rid not in new_ids or self._touches_owed(rid):
                continue
            if value == hash_of(self.old, rid):
                rebased[rid] = hash_of(self.new, rid)
        return rebased

    # --- unit and pair records ---------------------------------------------------------

    def pair_records(self) -> None:
        old_ids = set(review_units(self.old)) | set(review_pairs(self.old))
        new_ids = set(review_units(self.new)) | set(review_pairs(self.new))
        for rid, record in self.state.plan_pair_reviews.items():
            if is_legacy_review_id(rid) or rid not in old_ids or rid not in new_ids:
                continue
            if record.verdict not in _HOLDABLE_VERDICTS:
                continue
            if not self._binds_old(record.plan_path, record.plan_sha256):
                continue
            was_held = self._was_held(record, rid)
            if not was_held and not self._was_current(record, rid):
                continue
            if is_unit_id(rid):
                if self._touches_owed(rid):
                    continue
                self._restamp_unit(record, rid)
            elif was_held or self._touches_owed(rid):
                self._hold(record, rid)
            else:
                self._restamp_pair(record, rid)
            self._mark(record, self.old_sha or record.plan_sha256)
            record.plan_path = self.new_path
            record.plan_sha256 = self._restamped_sha(record.plan_sha256)
            self.report["records"].append(rid)

    def _was_held(self, record: PlanPairReview, rid: str) -> bool:
        return bool(record.held) and not is_unit_id(rid) \
            and record.held == review_currency_hash(self.old, rid)

    def _was_current(self, record: PlanPairReview, rid: str) -> bool:
        if is_unit_id(rid):
            return bool(record.unit_norm) and record.unit_norm == unit_currency_hash(self.old, rid)
        if record.is_content_keyed:
            fresh = pair_content(self.old, rid)
            return all(getattr(record, key) == fresh[key] for key in PAIR_CONTENT_KEYS)
        if self.old_sha and record.plan_sha256 == self.old_sha:
            return True
        fresh = pair_binding(self.old, rid)
        return all(getattr(record, key) == fresh[key] for key in pair_currency_keys(self.old, rid))

    def _hold(self, record: PlanPairReview, rid: str) -> None:
        record.held = review_currency_hash(self.new, rid)
        self.report["held"].append(rid)

    def _restamp_unit(self, record: PlanPairReview, rid: str) -> None:
        record.unit_norm = unit_currency_hash(self.new, rid)

    def _restamp_pair(self, record: PlanPairReview, rid: str) -> None:
        if record.is_content_keyed:
            for key, value in pair_content(self.new, rid).items():
                setattr(record, key, value)
        else:
            for key, value in pair_binding(self.new, rid).items():
                if key in PAIR_BINDING_KEYS:
                    setattr(record, key, value)

    # --- premise questions and order coverage ------------------------------------------

    def premise(self) -> None:
        bag = self.state.plugins.get("premise") if isinstance(self.state.plugins, dict) else None
        if not bag:
            return
        old_stages = {s.index: s for s in self.old.stages}
        new_stages = {s.index: s for s in self.new.stages}
        old_keys = {i: stage_norm_keys(s) for i, s in old_stages.items()}
        new_keys = {i: stage_norm_keys(s) for i, s in new_stages.items()}
        for question in bag.get("questions", []):
            self._restamp_question(question, old_keys, new_keys)
        for element in bag.get("order_elements", []):
            self._restamp_order_element(element, old_stages, new_stages, old_keys, new_keys)
        invalidate_stale_dispositions(bag, new_keys, plan_meta_element_keys(self.new))
        invalidate_stale_order_dispositions(bag, new_keys)

    def _restamp_question(self, question: dict, old_keys: dict, new_keys: dict) -> None:
        if question.get("disposition") not in _KEY_BOUND:
            return
        parsed = parse_target(question.get("target", ""))
        if parsed is None or parsed[0] != "stage":
            return
        _, index, element = parsed
        if index not in old_keys or index not in new_keys:
            return
        old, new = old_keys[index], new_keys[index]
        if old.get(element) is None or old.get(element) != new.get(element):
            return
        stamp = question.get("disposed_at_key", "")
        if stamp in (old.get(element), old.get(WHOLE_STAGE_ELEMENT)) and stamp != new[element]:
            question["disposed_at_key"] = new[element]
            self.report["records"].append(f"premise:{question.get('id', '')}")

    def _restamp_order_element(
        self, element: dict, old_stages: dict, new_stages: dict, old_keys: dict, new_keys: dict,
    ) -> None:
        index = element.get("stage")
        if element.get("disposition") != "covered" or index not in old_stages or index not in new_stages:
            return
        if _interface_payload(old_stages[index]) != _interface_payload(new_stages[index]):
            return
        binding = order_binding_key(new_keys[index])
        stamp = element.get("content_digest", "")
        previous = (order_binding_key(old_keys[index]), old_keys[index].get(WHOLE_STAGE_ELEMENT))
        if binding and stamp in previous and stamp != binding:
            element["content_digest"] = binding
            self.report["records"].append(f"order:{element.get('id', '')}")


def _interface_payload(stage) -> tuple:
    norm = StageNorm.from_stage(stage)
    return (norm.title, norm.result, norm.criterion_type, norm.done_criterion,
            tuple(norm.output_artifacts))


def _rekey(recorded: dict, old: dict, new: dict, skip=frozenset()) -> dict:
    """`recorded` with each stage's entry moved to the new version's value where it was the
    old version's current one; a stage in `skip`, or one the record already disagreed
    about, keeps what it recorded."""
    rekeyed = dict(recorded)
    for key, value in recorded.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index in skip or index not in new or value != old.get(index):
            continue
        rekeyed[key] = new[index]
    return rekeyed


def _stage_scope_index(scope: str) -> "int | None":
    from .state import plan_review_scope_stage_index
    return plan_review_scope_stage_index(scope) if scope else None


def _token_index(token: str) -> "int | None":
    prefix = stage_token(0).rstrip("0")
    return int(token[len(prefix):]) if token.startswith(prefix) and token[len(prefix):].isdigit() else None

