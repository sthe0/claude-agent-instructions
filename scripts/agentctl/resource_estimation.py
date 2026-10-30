"""Compare an executor's resource request with what its child actually did, and keep the result.

Difficulty removed: an executor kind that under-estimates its resource needs (stage 10's
`[[meta.resource_request]]`) repeats the error in every task, because nothing recorded the
difference between the request and the uses the hook logged (`<state_root>/resource-uses/`,
scripts/hook-resource-grant-check.py). After a stage's dispatch the engine computes that
difference, stores it in `state.resource_estimates[<stage>]` and appends one ledger row per
(task, stage); scripts/self-diagnose.py's `scan_resource_estimate_drift` turns a repeat across
tasks into a finding that points at the kind's SKILL.md.

The comparison is two recorded sets, so it is mechanical:

  under  a logged CHANGE (a file written, a ref pushed...) that the request does not cover:
         no typed resource of the request covers it (`resources.covers`) and the row's own
         `matched` rule (the entry or wildcard pattern the hook says covered the call) is neither
         equal to nor a plain extension of a request rule. Reads never count, and a call a
         wildcard rule covered without logging a typed change is not a write the hook can name,
         so it never counts. A transcript denial of this dispatch counts when the command or
         path it carried resolves to a change the request does not cover.
  over   a request entry no logged use matched. Only entries whose use the log can show are
         graded: a rule or write-mode add_dir whose typed resources cover a logged change, or a
         rule the log's `matched` names (equal, or one a plain extension of the other). A
         read-only rule or add_dir leaves no typed trace in the log, so it is never reported
         as over-estimated.

The kind baseline (the workdir, writable under acceptEdits) is part of what the request is
read against, since the request lists what the executor needs beyond it.

A missing request row, a missing decision log or an unreadable plan records nothing and
raises nothing: the caller is the dispatch path and must not change its routing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from lib import resource_estimation_ledger as ledger

from . import resources as _resources
from . import tool_contracts
from .plan_resources import _narrower_than_pattern, resolve_rule_grant
from .store import safe_session_id

USES_DIRNAME = "resource-uses"


def uses_log_path(state_root: "Path | str", session_id: str, stage_index: int) -> Path:
    """Where the hook logs this stage's child's calls (the hook's own layout, `<parent>-<stage>.jsonl`)."""
    return Path(state_root) / USES_DIRNAME / f"{safe_session_id(session_id)}-{stage_index}.jsonl"


def read_uses(path: Path) -> "list[dict] | None":
    """The decision-log rows, or None when the log does not exist."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _is_change(resource) -> bool:
    return not (isinstance(resource, _resources.FileResource) and resource.mode == "read")


class _Request:
    def __init__(self, allow, add_dirs, venue, workdir):
        self.typed: list = []
        self.rules: list[str] = []
        self.entries: list[tuple[str, list, "tuple[str, str] | None"]] = []
        for rule in allow:
            try:
                res = resolve_rule_grant(rule, venue)
            except ValueError:
                res = None
            typed = list(res.resources) if res is not None and res.status == "resolved" else []
            self.typed.extend(typed)
            self.rules.append(rule)
            self.entries.append((rule, typed, None))
        for path, mode in add_dirs:
            try:
                typed = [_resources.FileResource(path, mode)]
            except ValueError:
                typed = []
            self.typed.extend(typed)
            self.entries.append((f"add_dir {path} ({mode})", typed, (path, mode)))
        if workdir:
            self.typed.append(_resources.FileResource(workdir, "write"))

    def covers(self, resource) -> bool:
        return any(t.covers(resource) for t in self.typed)

    def names_rule(self, matched: str) -> bool:
        return any(matched == r or _narrower_than_pattern(matched, r) for r in self.rules)


def _row_resources(row: dict) -> list:
    out = []
    for item in row.get("resources") or []:
        try:
            out.append(_resources.resource_from_dict(item))
        except (ValueError, TypeError):
            continue
    return out


def _describe(resource) -> dict:
    if isinstance(resource, _resources.FileResource):
        return {"class": "file", "text": f"{resource.path} ({resource.mode})"}
    return {"class": resource.kind, "text": json.dumps(_resources.resource_to_dict(resource), sort_keys=True)}


def _denial_resources(miss: dict, venue: str) -> list:
    """The changes a denied call (a `planning_misses` row of this dispatch) carried."""
    text = str(miss.get("command") or "")
    if not text:
        return []
    if miss.get("tool_name") == "Bash":
        try:
            split = tool_contracts.resolve_segments(text, venue, cdpath_unverified=True)
        except Exception:  # noqa: BLE001 - an unresolvable denial is simply not counted
            return []
        return [r for r in split.resources if _is_change(r)]
    try:
        return [_resources.FileResource(text, "write")]
    except ValueError:
        return []


def compare(allow, add_dirs, uses: "list[dict]", *, venue: "str | None", workdir: "str | None",
            denials: "list[dict] | None" = None) -> dict:
    """`{"under": [{class, text}...], "over": [entry text...]}` for one request and one stage's uses."""
    request = _Request(allow, add_dirs, venue, workdir)
    under: list[dict] = []
    logged: list = []
    matched_all: list[str] = []
    for row in uses:
        changes = [r for r in _row_resources(row) if _is_change(r)]
        matched = [str(m) for m in (row.get("matched") or [])]
        matched_all.extend(matched)
        logged.extend(changes)
        rule_covered = any(request.names_rule(m) for m in matched)
        for change in changes:
            if not request.covers(change) and not rule_covered:
                under.append(_describe(change))
    for miss in denials or []:
        for change in _denial_resources(miss, venue or ""):
            logged.append(change)
            if not request.covers(change):
                under.append(_describe(change))
    over: list[str] = []
    for label, typed, add_dir in request.entries:
        writes = [t for t in typed if _is_change(t)]
        if add_dir is not None:
            if not writes:
                continue
        elif typed and not writes:
            continue
        used = any(w.covers(c) for w in writes for c in logged)
        if not used and add_dir is None:
            used = any(label == m or _narrower_than_pattern(m, label) or _narrower_than_pattern(label, m)
                       for m in matched_all)
        if not used:
            over.append(label)
    return {"under": list({json.dumps(u, sort_keys=True): u for u in under}.values()),
            "over": list(dict.fromkeys(over))}


def request_row(doc, kind: "str | None", stage_index: int):
    for row in doc.meta.resource_requests:
        if row.kind == kind and row.stage == stage_index:
            return row
    return None


def record_stage_estimate(state, stage, *, state_root, new_misses=None, doc=None,
                          venue: "str | None" = None, ledger_file: "Path | None" = None) -> "dict | None":
    """Compute, store and append the comparison for `stage`; None (nothing recorded) without a request row or a log."""
    if state_root is None:
        return None
    try:
        if doc is None:
            from .plan import load_plan
            doc = load_plan(state.plan_path)
        row = request_row(doc, stage.spawn_kind(), stage.index)
        if row is None:
            return None
        uses = read_uses(uses_log_path(state_root, state.session_id, stage.index))
        if uses is None:
            return None
        if venue is None:
            venue = state.resolve_check_venue("delivery")
        result = compare(row.allow, row.add_dirs, uses, venue=venue, workdir=venue, denials=new_misses)
        record = {
            "kind": row.kind, "under": result["under"], "over": result["over"],
            "plan_sha": state.accepted_plan_digest or "",
        }
        state.resource_estimates[str(stage.index)] = record
        ledger.replace_row({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "task_id": state.plan_task_id or state.task_id,
            "session": state.session_id, "stage": stage.index, **record,
        }, ledger_file)
        return record
    except Exception:  # noqa: BLE001 - calibration must never change dispatch routing
        return None
