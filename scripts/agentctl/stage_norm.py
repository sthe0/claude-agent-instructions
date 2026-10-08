"""StageNorm: a plan stage's norm as one serializable object.

Difficulty removed: three digests each claimed "this stage's norm did (not) change" --
`stage_question_key` (review/dispositions), `stage_carry_key` (PASSED carry-forward) and
`stage_interface_digest` (what a consumer relies on) -- while hand-maintaining three
overlapping field lists. A StageNorm holds the norm's fields once, and the three
identities are projections of it:

  review_digest     the whole stage as a reviewer saw it; byte-identical to the
                    `stage_question_key(stage)` it replaced (persisted in
                    `Question.disposed_at_key`, so its payload may not drift);
  interface_digest  what a consumer of the stage relies on (title, result image,
                    criterion, output artifacts);
  carry_digest      the stage's definition with its edges as typed deliveries, so
                    retyping an edge at an unchanged supplier index moves it.

The object is plain data (`to_dict` / `from_dict` round-trip through JSON): it is the
order a spawned specialist -- a depth n+1 manager -- would receive. It never needs a
PlanDoc; the one digest that reads beyond the stage itself (the interface of a stage with
a blank interface falls back to its rendered brief) takes that text as `interface_brief`
at construction.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, fields, replace

from .plan import (
    delivery_place,
    interface_empty,
    knowledge_place,
    negative_control_place,
    preconditions_place,
    procedure_place,
)
from .state import LandedSpec
from .text_shape import normalize_string as _normalize_string


def _sorted_edges(edges) -> tuple:
    """Edges in a canonical order. None sorts as the empty string so an edge that omits a
    field and one that names it compare; the tuples keep their None."""
    return tuple(sorted(edges, key=lambda e: (e[0], e[1] or "", e[2] or "", e[3] or "")))


def _tuplify(value):
    """Lists -> tuples, recursively: JSON has no tuple, and the digest payloads are
    `repr`s, where a list and a tuple differ."""
    if isinstance(value, (list, tuple)):
        return tuple(_tuplify(v) for v in value)
    return value


@dataclass(frozen=True)
class StageNorm:
    index: int
    title: str
    executor: str
    capability_required: str | None
    done_criterion: str
    criterion_type: str
    verify_command: str | None
    expected_exit: int
    verify_venue: str
    verify_kind: str
    landed: LandedSpec | None
    verify_venue_at_final: str | None
    material: str
    result: str
    invariants: str | None
    means: str
    method: str
    conditions: str | None
    principle: tuple | None
    edges: tuple  # declaration order, each (on, element, artifact, delivery)
    output_artifacts: tuple
    knowledge_place: tuple
    preconditions_place: tuple
    procedure_place: tuple
    negative_control_place: tuple
    delivery_place: tuple
    interface_brief: str | None = None
    supplier_interfaces: tuple = ()  # ((supplier index, its interface_digest), ...)

    @classmethod
    def from_stage(cls, stage, suppliers=None, *, doc=None) -> "StageNorm":
        """The norm of `stage`. `suppliers` maps a supplier index to its Stage; their
        interface digests are recorded so the object states what the stage relies on.
        `doc` is read only to render the full-brief fallback of a stage whose interface
        is blank (`plan.interface_empty`); without it such a stage's `interface_digest`
        is refused rather than guessed."""
        principle = stage.principle
        brief = None
        if doc is not None and interface_empty(stage):
            from .render import render_stage_interface
            brief = render_stage_interface(doc, stage.index, contract=True)
        norm = cls(
            index=stage.index,
            title=stage.title,
            executor=stage.actor.executor,
            capability_required=stage.actor.capability_required,
            done_criterion=stage.criterion.done_criterion,
            criterion_type=stage.criterion.criterion_type,
            verify_command=stage.criterion.verify_command,
            expected_exit=stage.criterion.expected_exit,
            verify_venue=stage.criterion.verify_venue,
            verify_kind=stage.criterion.verify_kind,
            landed=stage.criterion.landed,
            verify_venue_at_final=stage.criterion.verify_venue_at_final,
            material=stage.subject.material,
            result=stage.subject.result,
            invariants=stage.subject.invariants,
            means=stage.means.means,
            method=stage.means.method,
            conditions=stage.conditions,
            principle=(
                (principle.statement, principle.source, principle.derivation,
                 principle.confidence, principle.refutation)
                if principle is not None else None
            ),
            edges=tuple((s.on, s.element, s.artifact, s.delivery) for s in stage.supplies),
            output_artifacts=tuple(stage.output_artifacts),
            knowledge_place=knowledge_place(stage),
            preconditions_place=preconditions_place(stage),
            procedure_place=procedure_place(stage),
            negative_control_place=negative_control_place(stage),
            delivery_place=delivery_place(stage),
            interface_brief=brief,
        )
        if not suppliers:
            return norm
        return replace(norm, supplier_interfaces=tuple(
            (i, cls.from_stage(s, doc=doc).interface_digest())
            for i, s in sorted(suppliers.items())
        ))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StageNorm":
        known = {f.name for f in fields(cls)}
        raw = {k: v for k, v in d.items() if k in known}
        landed = raw.get("landed")
        if landed is not None:
            raw["landed"] = LandedSpec.from_dict(landed)
        for name in ("principle", "edges", "output_artifacts", "knowledge_place",
                     "preconditions_place", "procedure_place", "negative_control_place",
                     "delivery_place", "supplier_interfaces"):
            if name in raw:
                raw[name] = _tuplify(raw[name])
        return cls(**raw)

    def review_digest(self) -> str:
        """Whole-stage identity: what a reviewer saw and a disposed question was
        answered against. The payload is the one `stage_question_key` hashed before this
        object existed -- order and `repr` serialization included -- plus the conditional
        trailing `delivery_place`."""
        payload = repr((
            self.executor,
            self.capability_required,
            tuple(sorted({e[0] for e in self.edges})),
            self.done_criterion,
            self.criterion_type,
            self.verify_command,
            self.expected_exit,
            self.title,
            self.material,
            self.result,
            self.invariants,
            self.means,
            self.method,
            self.conditions,
            self.principle,
            tuple((e[0], e[1], e[2]) for e in self.edges),
            _normalize_string(self.verify_venue),
            _normalize_string(self.verify_kind),
            self.landed,
            *((_normalize_string(self.verify_venue_at_final),)
              if self.verify_venue_at_final else ()),
            *self.knowledge_place,
            *self.preconditions_place,
            *self.procedure_place,
            *self.negative_control_place,
            *self.delivery_place,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def interface_digest(self) -> str:
        """What a consumer relies on: title, result image, criterion type, done
        criterion and output artifacts -- never method/means/procedure. A stage with a
        blank interface hashes its rendered full brief instead (`interface_brief`)."""
        if self.interface_brief is not None:
            return hashlib.sha256(self.interface_brief.encode("utf-8")).hexdigest()
        if not self.result.strip() or not self.done_criterion.strip():
            raise ValueError(
                f"stage {self.index} has a blank interface: build its StageNorm with doc= "
                f"so the brief it falls back to is available"
            )
        payload = repr((self.title, self.result, self.criterion_type,
                        self.done_criterion, self.output_artifacts))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def carry_key(self) -> tuple:
        """The stage's definition for PASSED carry-forward: everything but
        `capability_required`, `material`, `principle` and the edges' provision detail,
        with the edges standing in as sorted typed deliveries (supplier index, element,
        artifact, delivery) so an edge retyped at the same supplier changes the key."""
        return (
            self.executor,
            _sorted_edges(self.edges),
            self.done_criterion,
            self.criterion_type,
            self.verify_command,
            self.expected_exit,
            self.title,
            self.result,
            self.invariants,
            self.means,
            self.method,
            self.conditions,
            _normalize_string(self.verify_venue),
            _normalize_string(self.verify_kind),
            self.landed,
            *((_normalize_string(self.verify_venue_at_final),)
              if self.verify_venue_at_final else ()),
            *self.knowledge_place,
            *self.preconditions_place,
            *self.procedure_place,
            *self.negative_control_place,
        )

    def carry_digest(self) -> str:
        return hashlib.sha256(repr(self.carry_key()).encode("utf-8")).hexdigest()
