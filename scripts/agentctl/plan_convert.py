"""Old-to-new plan converter (ADR-0011).

`convert` turns a single-file plan into a hypertext tree: an index, one file per stage and
one file per string field over the inline limit. It is a pure function of its arguments and
never rewords: every field keeps its bytes, and a node over the stage limit is converted as
it is and reported, since a reference does not shorten what a reader is shown.

Nothing is trusted before it is checked. The tree is built in a self-cleaned temporary
directory, every emitted TOML file is loaded back, and the tree is resolved through
`plan_refs.resolve_refs`: the resolved document must equal the parsed source (raw
equivalence), and, when the current loader accepts the source, every digest and effective
value the engine derives from a plan must agree between the source and the tree (parsed
equivalence). A failed check raises `PlanError` naming it, and nothing is written.

Two verbs use it: `plan-convert` (read-only: the tree goes to `--out` and nothing else is
written) and `plan-convert-live` (converts a session's registered plan in place and rebinds
the session through `state.plan_conversions`, or `--revert`s the last conversion).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path

from . import plan, plan_refs, plan_store, submission, toml_emit
from .config import PlanLimitError, plan_size_limits
from .directive import Directive
from .plan import PlanError

_REMEDY = "reformulate or split the node: a reference does not shorten what a reader is shown"
_ADDITIONS_REFUSAL = "{verb}: this plan cannot be converted in place yet"
_ORDER_REFUSAL = "{verb}: the converted plan would not run as the source does; nothing is linked"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _first_difference(a: object, b: object, path: str = "") -> str:
    """Where two parsed documents first differ, as `stage[2].method` (list positions are
    1-based, the way a plan numbers its stages)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in a:
            if key not in b:
                return f"{path}.{key}".lstrip(".") + " (missing)"
            if a[key] != b[key]:
                return _first_difference(a[key], b[key], f"{path}.{key}".lstrip("."))
        for key in b:
            if key not in a:
                return f"{path}.{key}".lstrip(".") + " (extra)"
    elif isinstance(a, list) and isinstance(b, list):
        for position, (x, y) in enumerate(zip(a, b), start=1):
            if x != y:
                return _first_difference(x, y, f"{path}[{position}]")
        return f"{path} (length {len(a)} against {len(b)})"
    return path or "the document"


def _emit_checked(table: dict, label: str) -> bytes:
    text = toml_emit.dumps(table)
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"round trip: the emitted {label} is not valid TOML: {exc}") from exc
    if loaded != table:
        raise PlanError(
            f"round trip: the emitted {label} loads back different at "
            f"{_first_difference(table, loaded)}")
    return text.encode("utf-8")


def _contains_reference(raw: dict) -> bool:
    found: list[bool] = []

    def hit(_table: dict, *_rest) -> dict:
        found.append(True)
        return {}

    plan_refs._map_plan(raw, stage=hit, text=hit)
    return bool(found)


def has_additions(report: dict) -> bool:
    """The one predicate for "the conversion changes what the plan says": the converter
    added a `depends_on` to some stage, an empty one included. Live conversion refuses on
    it, and a conversion that adds nothing is exactly its negation."""
    return bool(report["added_depends_on"])


def _oversize(raw: dict, stage_max_chars: int) -> list[dict]:
    """The nodes the submit gate blocks, taken from that gate's own function, so the report
    equals the block by construction. A stage node is labelled by its identity (declared
    `index`, else 1-based position), the gate's own label."""
    return [{"node": found["node"], "chars": found["chars"], "remedy": _REMEDY,
             "message": found["message"]}
            for found in submission.plan_size_blocks(raw, stage_max_chars)]


def _parsed_view(doc) -> dict:
    """Everything the engine derives from a parsed plan that a binding or a gate reads."""
    def closure(n: int) -> str:
        try:
            return plan.stage_closure_digest(doc, n)
        except PlanError as exc:
            return f"error: {exc}"

    return {
        "plan_content_digest": plan.plan_content_digest(doc),
        "plan_meta_digest": plan.plan_meta_digest(doc),
        "order_digest": plan.order_digest(doc),
        "plan_stage_digests": plan.plan_stage_digests(doc),
        "stage_closure_digests": {s.index: closure(s.index) for s in doc.stages},
        "depends_on": {s.index: list(s.depends_on) for s in doc.stages},
        "raw_depends_on": doc.raw_depends_on,
        "supplies": {s.index: repr(getattr(s, "supplies", None)) for s in doc.stages},
        "effective_grants": plan._grants_effective_map(doc),
        "final_checks": plan._final_check_place(doc),
        "parsed_document": doc,
    }


def _parsed_equivalence(
    raw: dict, expected: dict, index_path: Path, root: Path,
) -> tuple[str, str, str, str]:
    """`(verdict, detail, content_digest, source_content_digest)`: "ok", or "not-applicable"
    with the loader's message (and no digests) when the current loader refuses the source.
    A source the loader accepts must, once the converter's additions are made (`expected`:
    the source with its stage indices and dependencies, in index order), load to a plan the
    tree loads to as well, with the same derived values. `content_digest` is that plan's;
    `source_content_digest` is the unmodified source's, so a caller can tell whether the
    additions changed it."""
    try:
        source_doc = plan.parse_plan(copy.deepcopy(raw))
    except PlanError as exc:
        return "not-applicable", str(exc), "", ""
    try:
        expected_doc = plan.parse_plan(copy.deepcopy(expected))
    except PlanError as exc:
        raise PlanError(
            f"parsed equivalence: the source loads but not with the converter's additions: "
            f"{exc}") from exc
    try:
        tree_doc = plan.load_plan(index_path, root=root)
    except PlanError as exc:
        raise PlanError(
            f"parsed equivalence: the source loads but the converted tree does not: {exc}") from exc
    expected_view, tree_view = _parsed_view(expected_doc), _parsed_view(tree_doc)
    for part, value in expected_view.items():
        if value != tree_view[part]:
            raise PlanError(
                f"parsed equivalence: {part} differs between the source and the converted tree")
    return ("ok", "", expected_view["plan_content_digest"],
            plan.plan_content_digest(source_doc))


def convert_with_report(
    source_bytes: bytes, stem: str, *, field_max_chars: int, stage_max_chars: int,
) -> tuple[dict[str, bytes], dict]:
    """`(files, report)`: the tree as an ordered mapping of relative path to bytes (the
    index `<stem>.toml` first) and what the conversion did and proved."""
    if not stem or stem in (".", "..") or Path(stem).name != stem:
        raise PlanError(f"{stem!r} is not a plain file stem")
    try:
        raw = tomllib.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PlanError(f"malformed TOML in the source plan: {exc}") from exc
    if _contains_reference(raw):
        raise PlanError("already hypertext; use plan-seal")
    keys = plan_refs.plan_stage_keys(raw)
    stages = keys["stages"]
    oversize = _oversize(raw, stage_max_chars)
    index_rel = f"{stem}.toml"

    with tempfile.TemporaryDirectory(prefix="plan-convert-") as scratch:
        tmp = Path(scratch)
        order: list[str] = []
        externalized: list[dict] = []

        def externalize(table: dict, node: int | str, kind: str, holder_dir: Path) -> dict:
            out: dict = {}
            for key, value in table.items():
                if (key in plan_refs.EXTERNALIZABLE_FIELDS[kind] and isinstance(value, str)
                        and len(value) > field_max_chars):
                    rel = plan_refs.field_file_relpath(stem, node, key)
                    sha = plan_refs.write_field_file(tmp / rel, value)
                    order.append(rel)
                    externalized.append(
                        {"node": node, "field": key, "path": rel, "chars": len(value),
                         "sha256": sha})
                    out[key] = {"ref": Path(os.path.relpath(tmp / rel, holder_dir)).as_posix(),
                                "sha256": sha}
                else:
                    out[key] = value
            return out

        index_table: dict = {}
        for key, value in raw.items():
            if key == "meta" and isinstance(value, dict):
                meta: dict = {}
                for meta_key, meta_value in value.items():
                    if meta_key == "order" and isinstance(meta_value, dict):
                        meta[meta_key] = externalize(meta_value, "meta.order", "order", tmp)
                    else:
                        meta[meta_key] = meta_value
                index_table[key] = externalize(meta, "meta", "meta", tmp)
            elif key == "stage" and isinstance(value, list):
                refs = []
                for identity, table in stages:
                    stage_rel = plan_refs.stage_file_relpath(stem, identity)
                    holder_dir = (tmp / stage_rel).parent
                    stage_table = externalize(table, identity, "stage", holder_dir)
                    data = _emit_checked(stage_table, f"stage {identity} file")
                    (tmp / stage_rel).parent.mkdir(parents=True, exist_ok=True)
                    (tmp / stage_rel).write_bytes(data)
                    order.append(stage_rel)
                    refs.append({"ref": stage_rel, "sha256": _sha256(data)})
                index_table[key] = refs
            else:
                index_table[key] = value
        index_bytes = _emit_checked(index_table, "index")
        index_path = tmp / index_rel
        index_path.write_bytes(index_bytes)

        files = {index_rel: index_bytes}
        for rel in sorted(set(order), key=order.index):
            files[rel] = (tmp / rel).read_bytes()

        tree_raw = tomllib.loads(index_bytes.decode("utf-8"))
        try:
            resolved, _visited = plan_refs.resolve_refs(
                tree_raw, tmp, tmp, index_path=index_path)
        except PlanError as exc:
            raise PlanError(f"raw equivalence: the converted tree does not resolve: {exc}") from exc
        expected = {**raw, "stage": [table for _n, table in stages]} if stages else raw
        if resolved != expected:
            raise PlanError(
                "raw equivalence: the resolved tree differs from the source with the "
                f"converter's additions at {_first_difference(expected, resolved)}")
        parsed, parsed_detail, content_digest, source_content_digest = _parsed_equivalence(
            raw, expected, index_path, tmp)

    report = {
        "source_sha256": _sha256(source_bytes),
        "field_max_chars": field_max_chars,
        "stage_max_chars": stage_max_chars,
        "root_digest": _sha256(index_bytes),
        "files": [{"path": rel, "sha256": _sha256(data)} for rel, data in files.items()],
        "externalized": externalized,
        "added_indices": keys["added_indices"],
        "added_depends_on": keys["added_depends_on"],
        "run_order_source": keys["run_order_source"],
        "run_order_converted": keys["run_order_converted"],
        "oversize_stages": oversize,
        "round_trip": "ok",
        "raw_equivalence": "ok",
        "parsed_equivalence": parsed,
        "content_digest": content_digest,
        "source_content_digest": source_content_digest,
    }
    if parsed_detail:
        report["parsed_equivalence_detail"] = parsed_detail
    return files, report


def convert(source_bytes: bytes, stem: str, *, field_max_chars: int,
            stage_max_chars: int) -> dict[str, bytes]:
    """The converted tree: relative path -> bytes, the index `<stem>.toml` first."""
    return convert_with_report(
        source_bytes, stem, field_max_chars=field_max_chars, stage_max_chars=stage_max_chars)[0]


# --- verbs -----------------------------------------------------------------------

def _limits() -> tuple[int, int]:
    """`(stage_max_chars, field_max_chars)`; a stage limit that cannot be read refuses."""
    try:
        return plan_size_limits()
    except PlanLimitError as exc:
        raise PlanError(str(exc)) from exc


def _read_source(path: str | Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read plan file {path}: {exc}") from exc


def _report_lines(report: dict) -> list[str]:
    lines = [f"root digest: {report['root_digest']}",
             f"source sha256: {report['source_sha256']}",
             f"limits: plan-stage-max-chars {report['stage_max_chars']}, "
             f"plan-field-inline-max-chars {report['field_max_chars']}",
             f"round trip: {report['round_trip']}",
             f"raw equivalence: {report['raw_equivalence']}",
             f"parsed equivalence: {report['parsed_equivalence']}"]
    if report.get("parsed_equivalence_detail"):
        lines.append(f"  loader: {report['parsed_equivalence_detail']}")
    lines += [f"externalized {e['node']}.{e['field']} ({e['chars']} chars) -> {e['path']}"
              for e in report["externalized"]]
    if report.get("added_indices"):
        lines.append(f"added index to stage(s): {report['added_indices']}")
    lines += [f"added depends_on {a['depends_on']} to stage {a['stage']}"
              for a in report.get("added_depends_on", [])]
    lines.append(f"run order, source: {report['run_order_source']}")
    lines.append(f"run order, converted: {report['run_order_converted']}")
    lines += [f"OVERSIZE {o['message']}" for o in report["oversize_stages"]]
    return lines


def convert_into_dir(plan_path: str | Path, out: str | Path) -> dict:
    source = Path(plan_path)
    target = Path(out)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise PlanError(f"--out {target} exists and is not an empty directory")
    stage_max, field_max = _limits()
    files, report = convert_with_report(
        _read_source(source), source.stem, field_max_chars=field_max, stage_max_chars=stage_max)
    for rel, data in files.items():
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return {**report, "out": str(target), "index": str(target / f"{source.stem}.toml")}


def cmd_plan_convert(args, *, store=None) -> Directive:
    try:
        data = convert_into_dir(args.plan, args.out)
    except PlanError as exc:
        return Directive(False, "(plan-convert)", "error", str(exc))
    if getattr(args, "format", "text") == "json":
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        text = "\n".join([f"converted into {data['index']}", *_report_lines(data)])
    return Directive(True, "(plan-convert)", "inspect", text, data=data)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".partial")
    partial = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def _live_refusal(state, plan_file: Path | None, verb: str) -> str | None:
    from .state import Node, StageStatus

    if state.node == Node.RESOLVED.value:
        return (f"{verb}: the session is resolved or closed; use `agentctl plan-convert "
                "--plan <file> --out <dir>` for a read-only copy")
    if plan_file is None or not plan_file.is_file():
        return f"{verb}: the session has no registered plan file to convert"
    if any(s.outcome.status == StageStatus.ACTIVE.value for s in state.stages):
        return (f"{verb}: a stage is dispatched and not yet recorded; record its result "
                "first, so no running stage is bound to a plan that changes under it")
    return None


def cmd_plan_convert_live(args, *, store, runner=None) -> Directive:
    from . import cli, gates
    from .state import PlanConversion

    state = cli._require(store, args.session)
    revert = bool(getattr(args, "revert", False))
    verb = "plan-convert-live --revert" if revert else "plan-convert-live"
    plan_file = Path(state.plan_path) if state.plan_path else None
    refusal = _live_refusal(state, plan_file, verb)
    if refusal:
        return Directive(False, state.node, "noop", refusal)
    old_bytes = _read_source(plan_file)
    current = _sha256(old_bytes)
    if not gates.plan_digest_equivalent(state, state.accepted_plan_digest, current):
        return Directive(
            False, state.node, "noop",
            f"{verb}: the plan file's digest {current} differs from the session's accepted "
            f"digest {state.accepted_plan_digest}; submit or replan the edit first")
    by = args.by
    stem = plan_file.stem
    if revert:
        return _revert_live(state, store, plan_file, current, by, verb)

    stage_max, field_max = _limits()
    try:
        files, report = convert_with_report(
            old_bytes, stem, field_max_chars=field_max, stage_max_chars=stage_max)
    except PlanError as exc:
        return Directive(False, state.node, "noop", f"{verb}: {exc}")
    if report["parsed_equivalence"] != "ok":
        return Directive(
            False, state.node, "noop",
            f"{verb}: the current loader refuses this plan ({report['parsed_equivalence_detail']}), "
            "so the session's content digest cannot be rebound; use `agentctl plan-convert` for "
            "a read-only copy")
    if has_additions(report):
        return Directive(False, state.node, "noop", _ADDITIONS_REFUSAL.format(verb=verb))
    if (report["run_order_source"] != report["run_order_converted"]
            or report["content_digest"] != report["source_content_digest"]):
        return Directive(False, state.node, "noop", _ORDER_REFUSAL.format(verb=verb))
    index_rel = f"{stem}.toml"
    parts = {rel: data for rel, data in files.items() if rel != index_rel}
    for rel, data in parts.items():
        destination = plan_file.parent / rel
        if destination.exists() and destination.read_bytes() != data:
            return Directive(
                False, state.node, "noop",
                f"{verb}: {destination} already exists with different content; it would be "
                "overwritten")
    new_digest = report["root_digest"]
    content_digest = report["content_digest"]
    new_index = files[index_rel]
    for rel, data in parts.items():
        _write_atomic(plan_file.parent / rel, data)
    cli._write_plan_version_snapshot(store, state, old_bytes, current, plan_path=str(plan_file))
    cli._write_plan_version_snapshot(store, state, new_index, new_digest, plan_path=str(plan_file))
    try:
        plan_store.get(current)
        plan_store.get(new_digest)
    except PlanError as exc:
        return Directive(False, state.node, "noop", f"{verb}: {exc}")
    if _read_source(plan_file) != old_bytes:
        return Directive(
            False, state.node, "noop",
            f"{verb}: the plan file changed while it was being converted; nothing is linked, "
            "run the verb again")
    last = state.plan_conversions[-1] if state.plan_conversions else None
    if not (last and last.direction == "convert" and last.old_digest == current
            and last.new_digest == new_digest):
        state.plan_conversions.append(PlanConversion(
            old_digest=current, new_digest=new_digest, content_digest=content_digest,
            plan_path=str(plan_file), by=by, direction="convert"))
    state.accepted_plan_digest = new_digest
    state.log("plan_convert", by=by, old_digest=current, new_digest=new_digest,
              direction="convert")
    store.save(state)
    try:
        _write_atomic(plan_file, new_index)
    except OSError as exc:
        return Directive(
            False, state.node, "noop",
            f"{verb}: the conversion is linked in the session but the index could not be "
            f"written ({exc}); the plan file still holds the old bytes and every gate reads "
            "it through the link; run the verb again to finish")
    text = "\n".join([
        f"converted {plan_file} in place by {by}: root digest {current} -> {new_digest}",
        "every recorded binding keeps its value and is read through the conversion link; "
        "`plan-convert-live --revert` restores the old index and leaves the parts on disk",
        *_report_lines(report)])
    return Directive(True, state.node, "inspect", text, data={
        "by": by, "old_digest": current, "new_digest": new_digest,
        "content_digest": content_digest, "plan_path": str(plan_file),
        "direction": "convert", "oversize_stages": report["oversize_stages"],
        "files": report["files"], "externalized": report["externalized"]})


def _revert_live(state, store, plan_file: Path, current: str, by: str, verb: str) -> Directive:
    from .state import PlanConversion

    last = state.plan_conversions[-1] if state.plan_conversions else None
    if last is None or last.direction != "convert" or last.new_digest != current:
        return Directive(
            False, state.node, "noop",
            f"{verb}: the plan's root digest {current} is not the new digest of the last "
            "conversion; there is nothing to revert, or the plan changed after it")
    try:
        old_bytes = plan_store.get(last.old_digest)
    except PlanError as exc:
        return Directive(False, state.node, "noop", f"{verb}: {exc}")
    _write_atomic(plan_file, old_bytes)
    state.plan_conversions.append(PlanConversion(
        old_digest=current, new_digest=last.old_digest, content_digest=last.content_digest,
        plan_path=str(plan_file), by=by, direction="revert"))
    state.accepted_plan_digest = last.old_digest
    state.log("plan_convert", by=by, old_digest=current, new_digest=last.old_digest,
              direction="revert")
    store.save(state)
    text = (f"reverted {plan_file} by {by}: root digest {current} -> {last.old_digest}; the "
            "parts of the converted tree stay on disk")
    return Directive(True, state.node, "inspect", text, data={
        "by": by, "old_digest": current, "new_digest": last.old_digest,
        "content_digest": last.content_digest, "plan_path": str(plan_file),
        "direction": "revert", "parts_left_on_disk": True, "oversize_stages": []})
