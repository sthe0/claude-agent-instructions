"""The structured cause of a difficulty: four fields stated when the critique is recorded.

`cause_category` names what kind of cause it was, `machinery_induced` whether the agent
system's own process caused the rework, `mechanism` which mechanism of the registry did, and
`functional_place` the place whose filling should have caught it. The rules between them are
checked here, against the tables, with no member list in code:

- `mechanism` is a registry id exactly when `machinery_induced`; otherwise the literal `none`.
- A machinery-induced cause takes its place from the registry entry of its mechanism: a
  supplied place that differs from it, or is `none`, is refused (an omitted one is derived).
- A cause that is not machinery-induced names the place whose filling should have caught it,
  or `none` when no place is implicated.
- `other` needs a free-text note.
- `unknown` is the mechanism of a machinery-induced cause the recorded text does not identify;
  it is accepted only on an imported row, and its place must be a member other than `none`
  (which one is the importer's mapping rule).

A member retired from its table is refused on a new critique; rows that already name it stay
valid, because each stored field carries the table version it was validated against.

Stdlib and `lib.telemetry.tables` only.
"""
from __future__ import annotations

from lib.telemetry import tables

NO_MECHANISM = "none"
NO_PLACE = "none"
UNKNOWN_MECHANISM = "unknown"
OTHER_CATEGORY = "other"

CAUSE_FIELDS = ("cause_category", "machinery_induced", "mechanism", "functional_place")
NOTE_FIELD = "cause_note"
VERSION_FIELDS = {
    "cause_category": ("categories", "cause_category_table_version"),
    "mechanism": ("registry", "mechanism_table_version"),
    "functional_place": ("places", "functional_place_table_version"),
}


def parse_machinery_flag(value) -> "bool | None":
    """The bool a `--machinery-induced true|false` value stands for; None when it is neither."""
    if isinstance(value, bool):
        return value
    return {"true": True, "false": False}.get(value.strip().lower()) if isinstance(value, str) else None


def _live_member(table: str, value, root=None) -> "str | None":
    """None when `value` is a live member of `table`, else the reason it is not."""
    try:
        tables.validate(table, value, root=root)
    except tables.TableError as err:
        return str(err)
    return None


def registry_place(mechanism, root=None) -> "str | None":
    """The place of a registry entry, None when `mechanism` names no entry."""
    entry = tables.load("registry", root).members.get(mechanism) if isinstance(mechanism, str) else None
    return entry.attrs.get("place") if entry is not None else None


def derived_place(fields: dict, root=None) -> "str | None":
    """The place the registry dictates for a machinery-induced cause with a registry mechanism."""
    if fields.get("machinery_induced") is True:
        return registry_place(fields.get("mechanism"), root)
    return None


def validate_cause(fields: dict, imported: bool = False, root=None) -> "list[str]":
    """The rules `fields` violates, each naming its field; empty when the cause is valid.
    `fields` holds cause_category, machinery_induced (a bool), mechanism, functional_place (None
    is accepted where the place is derived) and, optionally, cause_note."""
    violated = []
    category = fields.get("cause_category")
    reason = _live_member("categories", category, root)
    if reason:
        violated.append(f"cause_category: {reason}")
    machinery = fields.get("machinery_induced")
    if not isinstance(machinery, bool):
        violated.append(f"machinery_induced: must be true or false, got {machinery!r}")
    mechanism = fields.get("mechanism")
    place = fields.get("functional_place")
    if machinery is True:
        violated += _machinery_violations(mechanism, place, imported, root)
    elif machinery is False:
        if mechanism != NO_MECHANISM:
            violated.append(f"mechanism: must be {NO_MECHANISM!r} when machinery_induced is false, got {mechanism!r}")
        reason = _live_member("places", place, root)
        if reason:
            violated.append(f"functional_place: {reason}")
    if category == OTHER_CATEGORY and not str(fields.get(NOTE_FIELD) or "").strip():
        violated.append(f"cause_note: required when cause_category is {OTHER_CATEGORY!r}")
    return violated


def _machinery_violations(mechanism, place, imported: bool, root) -> "list[str]":
    if mechanism == UNKNOWN_MECHANISM:
        if not imported:
            return [f"mechanism: {UNKNOWN_MECHANISM!r} is accepted only on an imported row"]
        reason = _live_member("places", place, root) if place is not None else "functional_place is required"
        if reason or place == NO_PLACE:
            return [f"functional_place: must be a place other than {NO_PLACE!r} when mechanism is {UNKNOWN_MECHANISM!r}"]
        return []
    if mechanism == NO_MECHANISM or not isinstance(mechanism, str):
        return [f"mechanism: a registry id is required when machinery_induced is true, got {mechanism!r}"]
    reason = _live_member("registry", mechanism, root)
    if reason:
        return [f"mechanism: {reason}"]
    expected = registry_place(mechanism, root)
    if place is not None and (place == NO_PLACE or place != expected):
        return [f"functional_place: must be {expected!r}, the place of registry entry {mechanism!r}, got {place!r}"]
    return []


def complete_cause(fields: dict, imported: bool = False, root=None) -> "tuple[dict | None, list[str]]":
    """(the cause to store, violated rules). A machinery-induced cause without a place gets the
    registry's; the stored dict holds the four fields, the note and the table version of every
    enumerated field. None when a rule is violated."""
    fields = dict(fields)
    if fields.get("functional_place") is None:
        fields["functional_place"] = derived_place(fields, root)
    violated = validate_cause(fields, imported, root)
    if violated:
        return None, violated
    cause = {name: fields[name] for name in CAUSE_FIELDS}
    cause[NOTE_FIELD] = (fields.get(NOTE_FIELD) or "").strip() or None
    for name, (table, column) in VERSION_FIELDS.items():
        cause[column] = tables.load(table, root).version
    return cause, []
