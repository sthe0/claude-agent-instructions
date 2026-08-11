"""Recursive dataclass-field domain over `agentctl.state.Stage`.

A shared, non-`test_`-prefixed support module (sibling to `ast_purity.py`'s
convention) so a domain derived here can back both `test_contract_coverage.py`'s
change-decision enumerator and the renormalization residual totality test in
`test_renormalization.py` — the same domain, not two hand-typed lists that could
drift apart.

`leaf_paths` and `dataclasses_reached` both walk `dataclasses.fields`, unwrapping
`X | None` and `list[X]` annotations, and recursing whenever the unwrapped type is
itself a dataclass. Nothing here names a struct or a field: a dataclass added later
to the walk, at any depth, is picked up without editing a caller — the exact
property stage 10 of smd-act-defects-8 exists to establish, after an earlier draft
substituted a hand-written five-name apposition for this traversal and silently
dropped three of the eight structs actually reachable from `Stage`.
"""
from __future__ import annotations

import dataclasses
import types
import typing


def _unwrap(tp):
    """Strip a `list[X]` or `X | None` / `Optional[X]` annotation down to the type
    a nested dataclass would appear as. Recurses so a doubly-wrapped annotation
    (not present today, but not excluded by any field's declared type either)
    still resolves."""
    origin = typing.get_origin(tp)
    if origin is list:
        return _unwrap(typing.get_args(tp)[0])
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return _unwrap(args[0])
    return tp


def leaf_paths(cls, *, prefix: str = "") -> tuple[str, ...]:
    """Every leaf field reachable from `cls`, as dotted paths from the root, in
    field-declaration order, depth-first. A field whose unwrapped type is itself a
    dataclass is never a leaf: the traversal descends into it instead of yielding
    it, so `criterion.landed` contributes `criterion.landed.target` etc. rather
    than stopping at `criterion.landed` — the LandedSpec depth is REACHED, not
    named."""
    out: list[str] = []
    hints = typing.get_type_hints(cls)
    for f in dataclasses.fields(cls):
        nested = _unwrap(hints[f.name])
        path = f"{prefix}{f.name}"
        if dataclasses.is_dataclass(nested):
            out.extend(leaf_paths(nested, prefix=f"{path}."))
        else:
            out.append(path)
    return tuple(out)


def dataclasses_reached(cls) -> tuple[type, ...]:
    """Every dataclass TYPE reachable from `cls` by the same traversal
    `leaf_paths` uses, `cls` itself first, each type at most once, in
    first-encountered order. Used to prove the traversal reaches the count of
    nested structs a stage's own criterion names, without writing that count out
    as a literal list of struct names."""
    seen: list[type] = [cls]
    hints = typing.get_type_hints(cls)
    for f in dataclasses.fields(cls):
        nested = _unwrap(hints[f.name])
        if dataclasses.is_dataclass(nested) and nested not in seen:
            for reached in dataclasses_reached(nested):
                if reached not in seen:
                    seen.append(reached)
    return tuple(seen)
