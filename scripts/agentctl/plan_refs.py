"""Hash-pinned references in a plan (ADR-0011).

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

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Callable

from .directive import Directive
from .plan import PlanError, load_plan, parse_plan, stage_closure_digest

MAX_DEPTH = 8

_SHA_RE = re.compile(r"[0-9a-f]{64}")

NON_STRING_FIELDS: dict[str, frozenset[str]] = {
    "stage": frozenset({
        "index", "depends_on", "material_refs", "knowledge_refs", "guard_exempt_paths",
        "expected_exit", "landed", "principle", "supplies", "output_artifacts", "renorms",
        "grants", "effects", "optional",
    }),
    "meta": frozenset({"order"}),
    "order": frozenset({"requirements", "requires_traceability", "coverage"}),
}

EXTERNALIZABLE_FIELDS: dict[str, frozenset[str]] = {
    "stage": frozenset({
        "title", "executor", "expected_result_image", "done_criterion", "cost_tier",
        "material", "invariants", "means", "method", "procedure", "capability_required",
        "criterion_type", "verify_command", "verify_venue", "verify_kind",
        "verify_venue_at_final", "negative_control", "negative_control_waiver",
        "ephemeral_artifacts_waiver", "conditions", "preconditions", "knowledge",
        "backlog_issue",
    }),
    "meta": frozenset({
        "task_id", "goal", "done_criterion", "criterion_type", "weight_class",
        "external_research", "repo_root", "delivery_worktree", "landing_waiver",
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


def _guard(value: object, where: Position) -> object:
    """A value in a position that may not hold a reference: refuse a reference anywhere
    inside it, and leave the value itself alone."""
    if isinstance(value, dict):
        kind = _classify(value)
        if kind == "malformed":
            raise _refuse("malformed", _named(value),
                          "a reference table must carry exactly `ref` (a non-empty path) "
                          "and `sha256` (64 lowercase hex characters)")
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


def _map_node(table: dict, position: Position, kind: str, text: Callable) -> dict:
    out = {}
    for key, value in table.items():
        here = (*position, key)
        if kind == "meta" and key == "order" and isinstance(value, dict):
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
                    elements.append(_map_node(element, (i,), "stage", text))
                else:
                    elements.append(stage(element, i))
            out[key] = elements
        else:
            out[key] = _guard(value, (key,))
    return out


class _Walker:
    """Confinement, cycle and depth checks shared by the resolver and the sealer."""

    def __init__(self, root: Path, index_path: Path | None):
        self.root = root
        self.index_path = index_path.resolve() if index_path is not None else None

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


def _read_file(path: Path, sha256: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _refuse("missing", str(path), f"cannot read the referenced file: {exc}") from exc


def _decode(data: bytes, ref: object) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _refuse("malformed", ref, f"not valid UTF-8: {exc}") from exc


class _Resolver(_Walker):
    def __init__(self, root: Path, index_path: Path | None, read: Reader):
        super().__init__(root, index_path)
        self.read = read
        self.stack: list[Path] = []
        self.visited: Visited = []

    def _bytes(self, table: dict, path: Path) -> bytes:
        data = self.read(path, table["sha256"])
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
        pushed = 0
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
                    raise _refuse("malformed", _named(parsed),
                                  "a reference table must carry exactly `ref` and `sha256`")
                table, base = parsed, path.parent
            if "index" in parsed and parsed["index"] != i:
                raise _refuse("index", table["ref"],
                              f"the file declares index {parsed['index']!r} but sits at "
                              f"position {i}")
            return _map_node(parsed, (i,), "stage",
                             lambda t, p, b=path.parent: self.text(t, p, b))
        finally:
            if pushed:
                del self.stack[-pushed:]

    def resolve(self, raw: dict, base: Path) -> dict:
        return _map_plan(
            raw,
            stage=lambda t, i: self.stage(t, i, base),
            text=lambda t, p: self.text(t, p, base),
        )


def resolve_refs(
    raw: dict,
    base_dir: str | Path,
    root: str | Path,
    *,
    read: Reader | None = None,
    index_path: str | Path | None = None,
) -> tuple[dict, Visited]:
    """Substitute every reference in the parsed plan `raw` and return `(resolved,
    visited)`: `visited` lists `(position, confined resolved path, declared sha256)` per
    reference in document order, nested ones included. A position is `("meta", field)`,
    `("meta.order", field)`, `(n,)` for a whole stage (every link of a chain too) or
    `(n, field)`.

    `read(path, sha256) -> bytes` supplies a file's bytes; the default reads the file
    system and raises `missing`. The declared hash is checked against whatever `read`
    returns, and a `PlanError` it raises propagates unchanged. `index_path` names the file
    `raw` came from, so a reference back to it is a cycle."""
    resolver = _Resolver(
        Path(root).resolve(), Path(index_path) if index_path is not None else None,
        read or _read_file)
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


def _parse(path: Path) -> dict:
    try:
        return tomllib.loads(_decode(path.read_bytes(), str(path)))
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"malformed TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise _refuse("missing", str(path), f"cannot read: {exc}") from exc


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
    if done < expected:
        raise PlanError(
            f"cannot locate the reference to {ref!r} in the file text to reseal it "
            f"(found {done} of {expected})")
    return text


class _Sealer(_Walker):
    def seal_file(self, path: Path, kind: str, stack: list[Path]) -> str:
        """Reseal every reference in `path`, children first; return the file's new
        sha256. `kind` is 'plan' for an index and 'stage' for a stage file."""
        text = _decode(path.read_bytes(), str(path))
        raw = _parse(path)
        edits: list[tuple[str, str, str]] = []
        inner = [*stack, path]

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

        if kind == "stage" and _classify(raw) == "ref":
            child(raw, "stage")
        elif kind == "plan":
            _map_plan(raw, stage=lambda t, i: child(t, "stage") or {},
                      text=lambda t, p: child(t, None) or "")
        else:
            _map_node(raw, (0,), "stage", lambda t, p: child(t, None) or "")

        counts: dict[tuple[str, str, str], int] = {}
        for edit in edits:
            counts[edit] = counts.get(edit, 0) + 1
        for (ref, old, new), expected in counts.items():
            if old != new:
                text = _rewrite_hash(text, ref, old, new, expected)
        data = text.encode("utf-8")
        if data != path.read_bytes():
            path.write_bytes(data)
        return _sha256(data)


def seal(index_path: str | Path, *, root: str | Path | None = None) -> str:
    """Recompute every reference hash under the index bottom-up, rewriting only the
    `sha256` values; return the new root digest (the sha256 of the index bytes)."""
    index = Path(index_path).resolve()
    sealer = _Sealer(Path(root).resolve() if root is not None else default_root(index), index)
    return sealer.seal_file(index, "plan", [])


# --- line-level rewrite of one field -------------------------------------------

_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*=")
_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _value_end(lines: list[str], start: int) -> int:
    """Index of the last line of the value that begins on `lines[start]`."""
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
    def __init__(self, name: str, is_array: bool):
        self.name = name
        self.is_array = is_array
        self.keys: dict[str, tuple[int, int, str]] = {}


def _scan(lines: list[str]) -> list[_Block]:
    blocks = [_Block("", False)]
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        header = _HEADER_RE.match(line)
        key = _KEY_RE.match(line)
        if header:
            blocks.append(_Block(header.group(2), header.group(1) == "[["))
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

    walker_root = Path(root).resolve() if root is not None else default_root(index)
    before = load_plan(index, root=walker_root)
    raw = _parse(index)
    walker = _Walker(walker_root, index)
    chain: list[Path] = []
    holder = index
    block_name, array_position = "", None
    if kind == "stage":
        resolved, _ = resolve_refs(raw, index.parent, walker_root, index_path=index)
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
    touched = {p: p.read_bytes() for p in (index, holder, *chain)}
    created = not field_path.exists()
    try:
        sha = write_field_file(field_path, value)
        rel = Path(os.path.relpath(field_path, holder.parent)).as_posix()
        holder.write_bytes(_replace_field(
            _decode(touched[holder], str(holder)), block_name, array_position,
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
        raise
    return {"field_file": str(field_path), "sha256": sha, "holder": str(holder),
            "root_digest": _sha256(index.read_bytes())}


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
        raw, index.parent, Path(root) if root is not None else default_root(index),
        index_path=index)
    doc = parse_plan(resolved)
    wanted = [stage] if stage is not None else [s.index for s in doc.stages]
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
    return Directive(True, "(plan-refs)", "inspect",
                     f"externalized into {done['field_file']}; root digest {done['root_digest']}",
                     data=done)


def cmd_plan_seal(args, *, store=None) -> Directive:
    try:
        digest = seal(args.plan)
    except PlanError as exc:
        return Directive(False, "(plan-refs)", "error", str(exc))
    return Directive(True, "(plan-refs)", "inspect", f"sealed; root digest {digest}",
                     data={"root_digest": digest})
