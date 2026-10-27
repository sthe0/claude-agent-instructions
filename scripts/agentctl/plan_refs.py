"""Hash-pinned references in a plan (ADR-0012).

A reference is an inline TOML table with exactly the keys `ref` (a path relative to the
file holding the reference) and `sha256` (the digest of the referenced file's bytes). It
stands where a string value or a whole `[[stage]]` table would stand. TOML has no include
mechanism, so resolution is a pre-pass over the parsed dict that substitutes each
reference with the file's text (or, for a stage, the file parsed as a TOML table) before
`parse_plan` runs; the plan's digest stays `sha256(index bytes)` and, because every
reference embeds its child's hash, that digest is a Merkle root over the whole tree.

The module also holds the authoring side: `seal` (recompute reference hashes bottom-up),
`write_field_file`, `plan_externalize` (move one string field into its own file) and the
three verbs built on them. Writes are line-level rewrites of only the touched field: the
stdlib reads TOML but cannot write it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Callable

from . import toml_emit
from .directive import Directive
from .plan import PlanError, load_plan, parse_plan, stage_closure_digest

MAX_DEPTH = 8

_SHA_RE = re.compile(r"[0-9a-f]{64}")

NON_STRING_FIELDS: dict[str, frozenset[str]] = {
    "stage": frozenset({
        "index", "depends_on", "material_refs", "knowledge_refs", "guard_exempt_paths",
        "expected_exit", "landed", "principle", "supplies", "output_artifacts", "renorms",
        "grants", "effects", "optional", "amendments",
    }),
    "meta": frozenset({"order", "assumption"}),
    "order": frozenset({"requirements", "requires_traceability", "coverage"}),
}

# Exactly the keys parse_plan and Order read (pinned by a test). `[meta] context` is not
# read by parse_plan, so it is in neither table and stays in the index.
EXTERNALIZABLE_FIELDS: dict[str, frozenset[str]] = {
    "stage": frozenset({
        "title", "executor", "expected_result_image", "done_criterion", "cost_tier",
        "material", "invariants", "means", "method", "procedure", "capability_required",
        "criterion_type", "verify_command", "verify_venue", "verify_kind",
        "verify_venue_at_final", "negative_control", "negative_control_waiver",
        "ephemeral_artifacts_waiver", "conditions", "preconditions", "knowledge",
        "backlog_issue", "branch",
    }),
    "meta": frozenset({
        "task_id", "goal", "done_criterion", "criterion_type", "weight_class",
        "external_research", "repo_root", "delivery_worktree", "landing_waiver", "branch",
    }),
    "order": frozenset({"customer_id", "customer", "functional_place", "parent_norm"}),
}

_NODE_KIND = {"meta": "meta", "meta.order": "order"}

Reader = Callable[[Path, str], bytes]
Position = tuple
Visited = list[tuple[Position, Path, str]]


def _refuse(kind: str, ref: object, detail: str) -> PlanError:
    err = PlanError(f"plan reference {ref!r}: {kind}: {detail}")
    err.ref_kind = kind
    return err


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_root(index_path: str | Path) -> Path:
    """The confinement root: the plans directory when the index lies under it, else the
    index file's own directory."""
    from lib import config_root

    index = Path(index_path).resolve()
    plans = config_root.plans_dir().resolve()
    return plans if index.is_relative_to(plans) else index.parent


def _classify(table: dict) -> str | None:
    """'ref' for a well-formed reference, 'malformed' for one that is not, None for an
    ordinary table."""
    keys = set(table)
    if keys == {"ref", "sha256"}:
        ref, sha = table["ref"], table["sha256"]
        if isinstance(ref, str) and ref and isinstance(sha, str) and _SHA_RE.fullmatch(sha):
            return "ref"
        return "malformed"
    if keys in ({"ref"}, {"sha256"}):
        return "malformed"
    return None


def _named(table: dict) -> object:
    return table.get("ref", table.get("sha256"))


def _malformed(table: dict) -> PlanError:
    return _refuse("malformed", _named(table),
                   "a reference table must carry exactly `ref` (a non-empty path) "
                   "and `sha256` (64 lowercase hex characters)")


def _guard(value: object, where: Position) -> object:
    """A value in a position that may not hold a reference: refuse a reference anywhere
    inside it, and leave the value itself alone."""
    if isinstance(value, dict):
        kind = _classify(value)
        if kind == "malformed":
            raise _malformed(value)
        if kind == "ref":
            raise _refuse("position", value["ref"],
                          f"a reference is not allowed at {_where(where)}")
        for inner in value.values():
            _guard(inner, where)
    elif isinstance(value, list):
        for inner in value:
            _guard(inner, where)
    return value


def _where(position: Position) -> str:
    return ".".join(str(part) for part in position)


def stage_identity(table: dict, position: int) -> int:
    """A stage's identity: its declared integer `index` (a bool is not one), else its
    1-based array position. This agrees with `parse_plan` for an integer or absent
    `index`; `parse_plan` coerces any other value with `int()`, which `plan-convert`
    refuses."""
    declared = table.get("index")
    return declared if type(declared) is int else position


def stage_tables(raw: dict) -> list[tuple[int, dict]]:
    """`(identity, table)` per stage of a plan with no reference in its stage list: the
    declared integer `index`, else the 1-based array position, which is the identity
    `parse_plan` gives it. Zero-based numbering and stages out of order are legitimate;
    two stages with one identity are not."""
    stages = raw.get("stage")
    if not isinstance(stages, list):
        return []
    found: list[tuple[int, dict]] = []
    seen: set[int] = set()
    for position, table in enumerate(stages, start=1):
        if not isinstance(table, dict):
            raise PlanError(f"stage {position} is not a table")
        if "index" in table and type(table["index"]) is not int:
            raise PlanError(
                f"stage index must be an integer: the stage at position {position} "
                f"declares {table['index']!r}")
        declared = stage_identity(table, position)
        if declared in seen:
            raise PlanError(f"stage index {declared} is declared twice")
        seen.add(declared)
        found.append((declared, table))
    return found


def run_dependencies(table: dict) -> frozenset[int]:
    """The stages `table` waits for, as `parse_plan` derives `Stage.depends_on`: its
    `depends_on` plus every `[[stage.supplies]]` `on`. An entry that is not an integer is
    skipped, so a plan the loader refuses can still be simulated."""
    found: set[int] = set()
    for value in table.get("depends_on") or []:
        if type(value) is int:
            found.add(value)
    for edge in table.get("supplies") or []:
        if isinstance(edge, dict) and type(edge.get("on")) is int:
            found.add(edge["on"])
    return frozenset(found)


def simulate_run_order(rows: list[tuple[int, frozenset[int]]]) -> list[int]:
    """The order the engine runs `rows` (`(identity, dependencies)` in list order) when every
    stage passes: repeatedly the first not-yet-run stage whose dependencies have all run,
    which is `SessionState.ready_stages` followed by `cmd_next_stage`'s `ready[0]`. A plan
    the loader accepts is acyclic, so a stuck state arises only for one it refuses; the
    first remaining stage is then taken, to keep the simulation total."""
    done: set[int] = set()
    order: list[int] = []
    pending = list(rows)
    while pending:
        pick = next((row for row in pending if row[1] <= done), pending[0])
        pending.remove(pick)
        order.append(pick[0])
        done.add(pick[0])
    return order


def _with_stage_keys(table: dict, identity: int, depends_on: list[int] | None) -> dict:
    """`table` with its identity written as `index` when absent and `depends_on` written
    right after `index` when `depends_on` is given; every other key keeps its place."""
    out: dict = {}
    if "index" not in table:
        out["index"] = identity
        if depends_on is not None:
            out["depends_on"] = depends_on
    for key, value in table.items():
        out[key] = value
        if key == "index" and depends_on is not None:
            out["depends_on"] = depends_on
    return out


def plan_stage_keys(raw: dict) -> dict:
    """What a hypertext form of the single-file plan `raw` needs written into its stages,
    and the run order it must keep.

    Every stage names itself by an integer `index` and declares `depends_on`. A stage that
    lacks `depends_on` is given `[p]`, p being the latest non-optional stage the engine runs
    before it (found by simulating the current engine on `raw`), or `[]` when there is none;
    a key present in the source is never touched. Every added edge points at a stage that
    runs earlier, so none closes a cycle, and none points at an optional stage, so the
    optional-stage rule O3 holds and a declined stage strands nothing.

    Returns `stages` (`(identity, table)` sorted by identity, with the keys written),
    `added_indices` and `added_depends_on` (`[{"stage", "depends_on"}]`, in identity order)
    and the simulated `run_order_source` / `run_order_converted` (the converted list is in
    index order)."""
    source = stage_tables(raw)
    order = simulate_run_order([(n, run_dependencies(t)) for n, t in source])
    rank = {n: k for k, n in enumerate(order)}
    required = {n for n, t in source if t.get("optional") is not True}
    added: dict[int, list[int]] = {}
    for identity, table in source:
        if "depends_on" not in table:
            added[identity] = [n for n in order[:rank[identity]] if n in required][-1:]
    stages = sorted(
        ((n, _with_stage_keys(t, n, added.get(n))) for n, t in source), key=lambda row: row[0])
    return {
        "stages": stages,
        "added_indices": sorted(n for n, t in source if "index" not in t),
        "added_depends_on": [{"stage": n, "depends_on": added[n]} for n, _ in stages if n in added],
        "run_order_source": order,
        "run_order_converted": simulate_run_order(
            [(n, run_dependencies(t)) for n, t in stages]),
    }


def _map_node(table: dict, position: Position, kind: str, text: Callable) -> dict:
    out = {}
    for key, value in table.items():
        here = (*position, key)
        if kind == "meta" and key == "order" and isinstance(value, dict) and _classify(value) is None:
            out[key] = _map_node(value, ("meta.order",), "order", text)
        elif key in NON_STRING_FIELDS[kind] or not isinstance(value, dict):
            out[key] = _guard(value, here)
        else:
            shape = _classify(value)
            if shape == "ref":
                out[key] = text(value, here)
            else:
                out[key] = _guard(value, here)
    return out


def _map_plan(raw: dict, stage: Callable, text: Callable) -> dict:
    out = {}
    for key, value in raw.items():
        if key == "meta" and isinstance(value, dict):
            out[key] = _map_node(value, ("meta",), "meta", text)
        elif key == "stage" and isinstance(value, list):
            elements = []
            for i, element in enumerate(value, start=1):
                if not isinstance(element, dict):
                    elements.append(element)
                elif _classify(element) is None:
                    elements.append(_map_node(element, (stage_identity(element, i),), "stage", text))
                elif _classify(element) == "malformed":
                    raise _malformed(element)
                else:
                    elements.append(stage(element, i))
            out[key] = elements
        else:
            out[key] = _guard(value, (key,))
    return out


def _require_depends_on(table: dict, ref: object, what: str) -> None:
    """A stage of a hypertext plan lists what it waits for, `[]` included: the file or the
    position it sits at is no source of run order."""
    if "depends_on" not in table:
        raise _refuse("depends_on", ref, f"{what} of a plan with references must declare "
                      "`depends_on` (an empty list when it waits for nothing)")


def _require_inline_stage_keys(raw: dict) -> None:
    """The `index` and `depends_on` rules for the stages the index holds itself."""
    for position, element in enumerate(raw.get("stage") or [], start=1):
        if not isinstance(element, dict) or _classify(element) is not None:
            continue
        what = f"the inline stage at position {position}"
        if type(element.get("index")) is not int:
            raise _refuse("index", what,
                          f"{what} of a plan with references must declare an integer `index`")
        _require_depends_on(element, what, what)


class _Walker:
    """Confinement, cycle and depth checks shared by the resolver and the sealer."""

    def __init__(self, root: Path | None, index_path: Path | None):
        self._root = root
        self.index_path = index_path.resolve() if index_path is not None else None

    @property
    def root(self) -> Path:
        # Looked up on the first reference only: a plan without any never needs it.
        if self._root is None:
            if self.index_path is None:
                raise PlanError("no confinement root given and no index path to derive one from")
            self._root = default_root(self.index_path)
        return self._root

    def enter(self, table: dict, base: Path, stack: list[Path]) -> Path:
        ref = table["ref"]
        if os.path.isabs(ref):
            raise _refuse("escape", ref, "an absolute path is not allowed")
        path = (base / ref).resolve()
        if not path.is_relative_to(self.root):
            raise _refuse("escape", ref, f"{path} lies outside the confinement root {self.root}")
        if path in stack or path == self.index_path:
            raise _refuse("cycle", ref, f"{path} is already being resolved")
        if len(stack) + 1 > MAX_DEPTH:
            raise _refuse("depth", ref, f"more than {MAX_DEPTH} nested references")
        return path


class _Unreadable(Exception):
    """The default reader could not read a file; `_Resolver._bytes` turns it into the
    `missing` refusal that names the reference as written."""


def read_referenced_file(path: Path, sha256: str) -> bytes:
    """The resolver's default reader; a caller wrapping it (to keep the bytes it returns)
    passes its own `read` to `resolve_refs` and delegates here. The hash check is the
    resolver's, not this function's."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _Unreadable(f"cannot read the referenced file {path}: {exc}") from exc


def _decode(data: bytes, ref: object) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _refuse("malformed", ref, f"not valid UTF-8: {exc}") from exc


class _Resolver(_Walker):
    def __init__(self, root: Path | None, index_path: Path | None, read: Reader):
        super().__init__(root, index_path)
        self.read = read
        self.stack: list[Path] = []
        self.visited: Visited = []

    def _bytes(self, table: dict, path: Path) -> bytes:
        try:
            data = self.read(path, table["sha256"])
        except _Unreadable as exc:
            raise _refuse("missing", table["ref"], str(exc)) from exc
        actual = _sha256(data)
        if actual != table["sha256"]:
            raise _refuse("mismatch", table["ref"],
                          f"expected sha256 {table['sha256']}, actual {actual} ({path})")
        return data

    def text(self, table: dict, position: Position, base: Path) -> str:
        path = self.enter(table, base, self.stack)
        data = self._bytes(table, path)
        self.visited.append((position, path, table["sha256"]))
        return _decode(data, table["ref"])

    def stage(self, table: dict, i: int, base: Path) -> dict:
        if _classify(table) != "ref":
            raise _malformed(table)
        pushed = 0
        first_visit = len(self.visited)
        try:
            while True:
                path = self.enter(table, base, self.stack)
                data = self._bytes(table, path)
                self.visited.append(((i,), path, table["sha256"]))
                try:
                    parsed = tomllib.loads(_decode(data, table["ref"]))
                except tomllib.TOMLDecodeError as exc:
                    raise _refuse("malformed", table["ref"], f"not valid TOML: {exc}") from exc
                self.stack.append(path)
                pushed += 1
                shape = _classify(parsed)
                if shape is None:
                    break
                if shape == "malformed":
                    raise _malformed(parsed)
                table, base = parsed, path.parent
            declared = parsed.get("index")
            if type(declared) is not int:
                detail = (f"a stage file must declare an integer `index`, got {declared!r}"
                          if "index" in parsed else "a stage file must declare an integer `index`")
                raise _refuse("index", table["ref"], detail)
            _require_depends_on(parsed, table["ref"], "a stage file")
            for k in range(first_visit, len(self.visited)):
                self.visited[k] = ((declared,), *self.visited[k][1:])
            return _map_node(parsed, (declared,), "stage",
                             lambda t, p, b=path.parent: self.text(t, p, b))
        finally:
            if pushed:
                del self.stack[-pushed:]

    def resolve(self, raw: dict, base: Path) -> dict:
        resolved = _map_plan(
            raw,
            stage=lambda t, i: self.stage(t, i, base),
            text=lambda t, p: self.text(t, p, base),
        )
        if self.visited:
            _require_inline_stage_keys(raw)
        return resolved


def resolve_refs(
    raw: dict,
    base_dir: str | Path,
    root: str | Path | None,
    *,
    read: Reader | None = None,
    index_path: str | Path | None = None,
) -> tuple[dict, Visited]:
    """Substitute every reference in the parsed plan `raw` and return `(resolved,
    visited)`: `visited` lists `(position, confined resolved path, declared sha256)` per
    reference in document order, nested ones included. A position is `("meta", field)`,
    `("meta.order", field)`, `(n,)` for a whole stage (every link of a chain too) or
    `(n, field)`, where n is the stage's identity (declared `index`, else 1-based position).

    `read(path, sha256) -> bytes` supplies a file's bytes; the default reads the file
    system and raises `missing`. The declared hash is checked against whatever `read`
    returns, and a `PlanError` it raises propagates unchanged. `index_path` names the file
    `raw` came from, so a reference back to it is a cycle; a caller that omits it loses
    that check (a reference back to the index then recurses until the depth bound) and,
    when `root` is None too, has no confinement root. `root=None` means the default root
    of `index_path`, looked up only when a reference is met."""
    resolver = _Resolver(
        Path(root).resolve() if root is not None else None,
        Path(index_path) if index_path is not None else None,
        read or read_referenced_file)
    resolved = resolver.resolve(raw, Path(base_dir))
    return resolved, resolver.visited


# --- authoring ------------------------------------------------------------------

def field_file_relpath(stem: str, node: int | str, field: str) -> str:
    if node == "meta":
        return f"{stem}.parts/meta-{field}.md"
    if node == "meta.order":
        return f"{stem}.parts/meta-order-{field}.md"
    return f"{stem}.parts/stage-{node}-{field}.md"


def stage_file_relpath(stem: str, n: int) -> str:
    return f"{stem}.parts/stage-{n}.toml"


def write_field_file(path: str | Path, text: str) -> str:
    """Write `text` as exact UTF-8 bytes (parents created) and return its sha256."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    target.write_bytes(data)
    return _sha256(data)


def _parse_text(text: str, path: Path) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"malformed TOML in {path}: {exc}") from exc


def _parse(path: Path) -> dict:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _refuse("missing", str(path), f"cannot read: {exc}") from exc
    return _parse_text(_decode(data, str(path)), path)


def _unsealed(value: object) -> object:
    """The parsed document with every reference's hash blanked, for comparing two texts
    that may differ in nothing else."""
    if isinstance(value, dict):
        if _classify(value) == "ref":
            return {"ref": value["ref"]}
        return {k: _unsealed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unsealed(v) for v in value]
    return value


def _rewrite_hash(text: str, ref: str, old: str, new: str, expected: int) -> str:
    r = re.escape(ref)
    gap = r"[ \t]*(?:#[^\n]*)?\n[ \t]*"
    patterns = (
        rf'(ref\s*=\s*"{r}"\s*,\s*sha256\s*=\s*"){old}(")',
        rf'(sha256\s*=\s*"){old}("\s*,\s*ref\s*=\s*"{r}")',
        rf'(ref\s*=\s*"{r}"{gap}sha256\s*=\s*"){old}(")',
        rf'(sha256\s*=\s*"){old}("{gap}ref\s*=\s*"{r}")',
    )
    done = 0
    for pattern in patterns:
        text, count = re.subn(pattern, rf"\g<1>{new}\g<2>", text)
        done += count
    if done != expected:
        raise PlanError(
            f"cannot reseal the reference to {ref!r}: its text occurs {done} times in the "
            f"file but the parsed plan holds {expected} (a copy in prose or a comment, or a "
            f"spelling the sealer does not recognise)")
    return text


def _stage_file_number(name: str) -> int | None:
    """The `<n>` of a stage file name `stage-<n>.toml`, else None."""
    if not (name.startswith("stage-") and name.endswith(".toml")):
        return None
    digits = name[len("stage-"):-len(".toml")]
    return int(digits) if digits.isascii() and digits.isdecimal() else None


class _Sealer(_Walker):
    def _stage_files(self, index: Path, inner: list[Path]) -> list[Path]:
        """The stage files in the index's parts folder, confined, in name order."""
        parts = index.parent / f"{index.stem}.parts"
        if not parts.is_dir():
            return []
        found = []
        for entry in sorted(parts.iterdir()):
            if entry.is_file() and entry.name.startswith("stage-") and entry.suffix == ".toml":
                if _stage_file_number(entry.name) is None:
                    raise _refuse("path", str(entry), "a stage file is named stage-<n>.toml with "
                                  "n the integer `index` it declares")
                found.append(self.enter({"ref": f"{parts.name}/{entry.name}"}, index.parent, inner))
        return found

    def _declared_index(self, file: Path, inner: list[Path]) -> int:
        """The `index` a stage file declares, through a chain of references, checked
        against the number in its name."""
        current, chain = file, [*inner]
        while True:
            parsed = _parse(current)
            shape = _classify(parsed)
            if shape is None:
                break
            if shape == "malformed":
                raise _malformed(parsed)
            chain.append(current)
            current = self.enter(parsed, current.parent, chain)
        declared = parsed.get("index")
        if type(declared) is not int:
            raise _refuse("index", str(file), "a stage file must declare an integer `index`")
        named = _stage_file_number(file.name)
        if declared != named:
            raise _refuse("index", str(file),
                          f"the file name says stage {named} but it declares `index` {declared}")
        return declared

    def _assemble(self, index: Path, text: str, raw: dict, inner: list[Path]) -> str:
        """The index text with its `stage` list built from the stage files in its parts
        folder, sorted by the index each declares, every reference carrying a fresh hash.

        An index that holds a stage inline is left alone, and a stage file it does not
        reference is refused: the plan would have two sources for one stage list. A
        reference already in the list must point at `<stem>.parts/stage-<n>.toml`. The list
        is rewritten only when the set of files differs from it, so a hand-formatted list
        and its comments survive a reseal."""
        files = self._stage_files(index, inner)
        stages = raw.get("stage")
        elements = stages if isinstance(stages, list) else []
        listed: list[Path] = []
        for element in elements:
            if isinstance(element, dict) and _classify(element) == "malformed":
                raise _malformed(element)
            if isinstance(element, dict) and _classify(element) == "ref":
                listed.append(self.enter(element, index.parent, inner))
        if len(listed) != len(elements):
            stray = [f for f in files if f not in listed]
            if stray:
                raise _refuse("path", str(stray[0]), "the index holds a stage inline but does "
                              "not reference this stage file; move every stage into a file or "
                              "remove the file")
            return text
        parts = (index.parent / f"{index.stem}.parts").resolve()
        for target in listed:
            if target.parent != parts or _stage_file_number(target.name) is None:
                raise _refuse("path", str(target), f"a stage reference of {index.name} must "
                              f"point at {index.stem}.parts/stage-<n>.toml")
        by_index: dict[int, Path] = {}
        for file in files:
            declared = self._declared_index(file, inner)
            if declared in by_index:
                raise _refuse("index", str(file),
                              f"stage {declared} is declared by {by_index[declared]} as well")
            by_index[declared] = file
        ordered = [by_index[n] for n in sorted(by_index)]
        if "stage" in raw and ordered == listed:
            return text
        if "stage" not in raw and not ordered:
            return text
        entries = [{"ref": f"{index.stem}.parts/{file.name}",
                    "sha256": self.seal_file(file, "stage", inner)} for file in ordered]
        line = toml_emit.assignment("stage", entries) + "\n"
        lines = text.splitlines(keepends=True)
        blocks = _scan(lines)
        if "stage" in blocks[0].keys:
            start, end, _indent = blocks[0].keys["stage"]
            lines[start:end + 1] = [line]
        else:
            at = blocks[1].header_line if len(blocks) > 1 else len(lines)
            if at and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            lines[at:at] = [line, "\n"] if at < len(lines) else [line]
        rewritten = "".join(lines)
        old, new = _parse_text(text, index), _parse_text(rewritten, index)
        if ({k: v for k, v in old.items() if k != "stage"} != {k: v for k, v in new.items()
                                                                if k != "stage"}
                or new.get("stage") != entries):
            raise PlanError(f"assembling the stage list of {index} would change more than the list")
        return rewritten

    def seal_file(self, path: Path, kind: str, stack: list[Path]) -> str:
        """Reseal every reference in `path`, children first; return the file's new
        sha256. `kind` is 'plan' for an index and 'stage' for a stage file."""
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise _refuse("missing", str(path), f"cannot read: {exc}") from exc
        text = _decode(original, str(path))
        raw = _parse_text(text, path)
        edits: list[tuple[str, str, str]] = []
        inner = [*stack, path]
        if kind == "plan":
            text = self._assemble(path, text, raw, inner)
            raw = _parse_text(text, path)

        def child(table: dict, subkind: str | None) -> None:
            target = self.enter(table, path.parent, inner)
            if subkind is None:
                try:
                    new = _sha256(target.read_bytes())
                except OSError as exc:
                    raise _refuse("missing", table["ref"], f"cannot read {target}: {exc}") from exc
            else:
                new = self.seal_file(target, subkind, inner)
            edits.append((table["ref"], table["sha256"], new))

        def seal_leaf(table: dict, position: Position) -> str:
            child(table, None)
            return ""

        def seal_stage(table: dict, i: int) -> dict:
            child(table, "stage")
            return {}

        if kind == "stage" and _classify(raw) == "ref":
            child(raw, "stage")
        elif kind == "stage" and _classify(raw) == "malformed":
            raise _malformed(raw)
        elif kind == "plan":
            _map_plan(raw, stage=seal_stage, text=seal_leaf)
            if edits:
                _require_inline_stage_keys(raw)
        else:
            if type(raw.get("index")) is not int:
                raise _refuse("index", str(path), "a stage file must declare an integer `index`")
            _require_depends_on(raw, str(path), "a stage file")
            _map_node(raw, (0,), "stage", seal_leaf)

        counts: dict[tuple[str, str, str], int] = {}
        for edit in edits:
            counts[edit] = counts.get(edit, 0) + 1
        for (ref, old, new), expected in counts.items():
            if old != new:
                text = _rewrite_hash(text, ref, old, new, expected)
        data = text.encode("utf-8")
        if data != original:
            if _unsealed(_parse_text(text, path)) != _unsealed(raw):
                raise PlanError(f"resealing {path} would change more than its sha256 values")
            path.write_bytes(data)
        return _sha256(data)


def seal(index_path: str | Path, *, root: str | Path | None = None) -> str:
    """Recompute every reference hash under the index bottom-up, rewriting only the
    `sha256` values; return the new root digest (the sha256 of the index bytes)."""
    index = Path(index_path).resolve()
    sealer = _Sealer(Path(root).resolve() if root is not None else None, index)
    return sealer.seal_file(index, "plan", [])


# --- line-level rewrite of one field -------------------------------------------

_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*=")
_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _value_end(lines: list[str], start: int) -> int:
    """Index of the last line of the value that begins on `lines[start]`. A rough scanner:
    it ignores backslash escapes inside triple-quoted strings and the 4-5 quote closing
    forms. A misread is caught by the reload-and-compare in `plan_externalize`, which then
    restores every file."""
    depth = 0
    multiline = None
    first = True
    for i in range(start, len(lines)):
        line = lines[i].rstrip("\r\n")
        j = line.index("=") + 1 if first else 0
        first = False
        while j < len(line):
            if multiline:
                k = line.find(multiline, j)
                if k < 0:
                    break
                j = k + 3
                multiline = None
                continue
            c = line[j]
            if c == "#":
                break
            if line.startswith('"""', j) or line.startswith("'''", j):
                multiline = line[j:j + 3]
                j += 3
            elif c == '"':
                j += 1
                while j < len(line) and line[j] != '"':
                    j += 2 if line[j] == "\\" else 1
                j += 1
            elif c == "'":
                k = line.find("'", j + 1)
                j = len(line) if k < 0 else k + 1
            else:
                if c in "[{":
                    depth += 1
                elif c in "]}":
                    depth -= 1
                j += 1
        if multiline is None and depth <= 0:
            return i
    raise PlanError("unterminated value while locating a field in the plan text")


class _Block:
    def __init__(self, name: str, is_array: bool, header_line: int = -1):
        self.name = name
        self.is_array = is_array
        self.header_line = header_line
        self.keys: dict[str, tuple[int, int, str]] = {}


def _scan(lines: list[str]) -> list[_Block]:
    blocks = [_Block("", False)]
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        header = _HEADER_RE.match(line)
        key = _KEY_RE.match(line)
        if header:
            blocks.append(_Block(header.group(2), header.group(1) == "[[", i))
            i += 1
        elif key:
            end = _value_end(lines, i)
            blocks[-1].keys[key.group(2)] = (i, end, key.group(1))
            i = end + 1
        else:
            i += 1
    return blocks


def _replace_field(text: str, block_name: str, array_position: int | None,
                   field: str, rel: str, sha: str) -> str:
    lines = text.splitlines(keepends=True)
    blocks = _scan(lines)
    if array_position is None:
        candidates = [b for b in blocks if b.name == block_name and not b.is_array]
    else:
        candidates = [b for b in blocks if b.name == block_name and b.is_array]
    index = 0 if array_position is None else array_position - 1
    if index >= len(candidates) or field not in candidates[index].keys:
        raise PlanError(f"cannot locate field {field!r} in the text of its table")
    start, end, indent = candidates[index].keys[field]
    eol = lines[end][len(lines[end].rstrip("\r\n")):]
    lines[start:end + 1] = [f'{indent}{field} = {{ ref = "{rel}", sha256 = "{sha}" }}{eol}']
    return "".join(lines)


def _insert_stage_keys(text: str, block_name: str, array_position: int | None,
                       add_index: int | None, add_depends_on: list[int] | None) -> str:
    """`text` with `index` and `depends_on` written as the first keys of a stage table
    (`depends_on` right after an `index` already there). `block_name` is "" for a stage
    file, which is one table, else the `[[stage]]` block at `array_position`."""
    lines = text.splitlines(keepends=True)
    blocks = _scan(lines)
    if block_name == "":
        block = blocks[0]
    else:
        candidates = [b for b in blocks if b.name == block_name and b.is_array]
        if (array_position or 0) > len(candidates):
            raise PlanError("cannot locate the stage to write its index and depends_on into")
        block = candidates[(array_position or 1) - 1]
    at = block.header_line + 1
    new = []
    if add_index is not None:
        new.append(f"index = {add_index}\n")
    if add_depends_on is not None:
        if add_index is None and "index" in block.keys:
            at = block.keys["index"][1] + 1
        new.append(toml_emit.assignment("depends_on", add_depends_on) + "\n")
    if at and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines[at:at] = new
    return "".join(lines)


def _with_added_keys(resolved: dict, keys: dict) -> dict:
    """`resolved` with the stage keys of `plan_stage_keys` written, the stage list keeping
    the order it has."""
    by_identity = dict(keys["stages"])
    return {**resolved, "stage": [by_identity[stage_identity(table, position)]
                                  for position, table in enumerate(resolved.get("stage", []),
                                                                   start=1)]}


def _stage_key_edits(raw: dict, resolved: dict, keys: dict, walker: _Walker,
                     index: Path) -> list[tuple]:
    """Where each key `plan_stage_keys` adds is written: `(holder file, the chain of files
    below the index it sits behind, block name, array position, index to add, depends_on
    to add)` per stage the keys touch."""
    new_index = set(keys["added_indices"])
    new_deps = {a["stage"]: a["depends_on"] for a in keys["added_depends_on"]}
    found = []
    for position, table in enumerate(resolved.get("stage", []), start=1):
        identity = stage_identity(table, position)
        if identity not in new_index and identity not in new_deps:
            continue
        element, holder, chain = raw["stage"][position - 1], index, []
        block_name, array_position = "stage", position
        while _classify(element) == "ref":
            holder = walker.enter(element, holder.parent, chain)
            chain.append(holder)
            element = _parse(holder)
            block_name, array_position = "", None
        found.append((holder, tuple(chain), block_name, array_position,
                      identity if identity in new_index else None, new_deps.get(identity)))
    return found


def plan_externalize(
    index_path: str | Path, *, stage: int | None = None, meta_field: str | None = None,
    order_field: str | None = None, field: str | None = None,
    root: str | Path | None = None,
) -> dict:
    """Move one string field into its own file and put a reference in its place,
    resealing every ancestor up to the index. The field's lines are the only lines
    rewritten. The plan is reloaded afterwards and must be equal to what it was; on any
    failure every touched file is restored."""
    index = Path(index_path).resolve()
    stage_target = (stage is not None) or (field is not None)
    if sum([stage_target, meta_field is not None, order_field is not None]) != 1 or (
            stage_target and (stage is None or field is None)):
        raise PlanError(
            "name exactly one field: --stage N --field F, --meta-field F or --order-field F")
    if stage_target:
        node: int | str = stage
        kind, name = "stage", field
    elif meta_field is not None:
        node, kind, name = "meta", "meta", meta_field
    else:
        node, kind, name = "meta.order", "order", order_field
    if name not in EXTERNALIZABLE_FIELDS[kind]:
        raise PlanError(
            f"field {name!r} cannot be externalized from a {kind}: only a string field can "
            f"(allowed: {sorted(EXTERNALIZABLE_FIELDS[kind])})")

    walker_root = Path(root).resolve() if root is not None else None
    before = load_plan(index, root=walker_root)
    raw = _parse(index)
    walker = _Walker(walker_root, index)
    resolved, _ = resolve_refs(raw, index.parent, walker_root, index_path=index)
    keys = plan_stage_keys(resolved)
    key_edits = _stage_key_edits(raw, resolved, keys, walker, index)
    if key_edits:
        before = parse_plan(copy.deepcopy(_with_added_keys(resolved, keys)))
    chain: list[Path] = []
    holder = index
    block_name, array_position = "", None
    if kind == "stage":
        position = next(
            (i for i, s in enumerate(resolved.get("stage", []), start=1)
             if int(s.get("index", i)) == stage), None)
        if position is None:
            raise PlanError(f"stage {stage} not found in the plan")
        table = raw["stage"][position - 1]
        while _classify(table) == "ref":
            holder = walker.enter(table, holder.parent, chain)
            chain.append(holder)
            table = _parse(holder)
        if holder == index:
            block_name, array_position = "stage", position
    elif kind == "meta":
        table = raw.get("meta", {})
        block_name = "meta"
    else:
        table = raw.get("meta", {}).get("order", {})
        block_name = "meta.order"
    value = table.get(name)
    if isinstance(value, dict) and _classify(value) == "ref":
        raise PlanError(f"field {name!r} is already a reference")
    if not isinstance(value, str):
        raise PlanError(f"field {name!r} is not a string field present on this node")

    field_path = index.parent / field_file_relpath(index.stem, node, name)
    if field_path.exists() and field_path.read_bytes() != value.encode("utf-8"):
        raise PlanError(f"{field_path} already exists with different content")
    touched = {p: p.read_bytes() for p in (
        index, holder, *chain, *(q for edit in key_edits for q in (edit[0], *edit[1])))}
    created = not field_path.exists()
    parts_dir_created = not field_path.parent.exists()
    try:
        for holder_path, _chain, edit_block, edit_position, add_index, add_deps in key_edits:
            holder_path.write_bytes(_insert_stage_keys(
                _decode(holder_path.read_bytes(), str(holder_path)), edit_block, edit_position,
                add_index, add_deps).encode("utf-8"))
        sha = write_field_file(field_path, value)
        rel = Path(os.path.relpath(field_path, holder.parent)).as_posix()
        holder.write_bytes(_replace_field(
            _decode(holder.read_bytes(), str(holder)), block_name, array_position,
            name, rel, sha).encode("utf-8"))
        seal(index, root=walker_root)
        after = load_plan(index, root=walker_root)
        if after != before:
            raise PlanError(
                f"externalizing {name!r} changed the parsed plan; the change was reverted")
    except Exception:
        for path, data in touched.items():
            path.write_bytes(data)
        if created and field_path.exists():
            field_path.unlink()
        if parts_dir_created and field_path.parent.is_dir() and not any(field_path.parent.iterdir()):
            field_path.parent.rmdir()
        raise
    return {"field_file": str(field_path), "sha256": sha, "holder": str(holder),
            "root_digest": _sha256(index.read_bytes()),
            "added_indices": keys["added_indices"],
            "added_depends_on": keys["added_depends_on"]}


# --- verbs -----------------------------------------------------------------------

def plan_digest(index_path: str | Path, *, stage: int | None = None,
                root: str | Path | None = None) -> dict:
    index = Path(index_path).resolve()
    if not index.exists():
        raise PlanError(f"plan file not found: {index}")
    data = index.read_bytes()
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"malformed TOML in plan file {index}: {exc}") from exc
    resolved, visited = resolve_refs(
        raw, index.parent, root, index_path=index)
    doc = parse_plan(resolved)
    known = [s.index for s in doc.stages]
    if stage is not None and stage not in known:
        raise PlanError(f"stage {stage} not found in the plan (stages: {known})")
    wanted = [stage] if stage is not None else known
    return {
        "root_digest": _sha256(data),
        "refs": [{"position": list(position), "path": str(path), "sha256": sha}
                 for position, path, sha in visited],
        "closure_digests": {str(n): stage_closure_digest(doc, n) for n in wanted},
    }


def cmd_plan_digest(args, *, store=None) -> Directive:
    try:
        data = plan_digest(args.plan, stage=getattr(args, "stage", None))
    except PlanError as exc:
        return Directive(False, "(plan-refs)", "error", str(exc))
    if getattr(args, "format", "text") == "json":
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        lines = [f"root digest: {data['root_digest']}"]
        lines += [f"ref {_where(tuple(r['position']))}: {r['path']} {r['sha256']}"
                  for r in data["refs"]]
        lines += [f"closure digest of stage {n}: {d}"
                  for n, d in data["closure_digests"].items()]
        text = "\n".join(lines)
    return Directive(True, "(plan-refs)", "inspect", text, data=data)


def cmd_plan_externalize(args, *, store=None) -> Directive:
    try:
        done = plan_externalize(
            args.plan, stage=getattr(args, "stage", None), field=getattr(args, "field", None),
            meta_field=getattr(args, "meta_field", None),
            order_field=getattr(args, "order_field", None))
    except PlanError as exc:
        return Directive(False, "(plan-refs)", "error", str(exc))
    lines = [f"externalized into {done['field_file']}; root digest {done['root_digest']}"]
    if done["added_indices"]:
        lines.append(f"added index to stage(s): {done['added_indices']}")
    lines += [f"added depends_on {a['depends_on']} to stage {a['stage']}"
              for a in done["added_depends_on"]]
    return Directive(True, "(plan-refs)", "inspect", "\n".join(lines), data=done)


def cmd_plan_seal(args, *, store=None) -> Directive:
    try:
        digest = seal(args.plan)
    except PlanError as exc:
        return Directive(False, "(plan-refs)", "error", str(exc))
    return Directive(True, "(plan-refs)", "inspect", f"sealed; root digest {digest}",
                     data={"root_digest": digest})
