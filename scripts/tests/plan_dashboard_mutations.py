"""Pytest plugin that installs one named mutation on the plan-dashboard data layer.

Load it with ``-p plan_dashboard_mutations`` and name the mutation in
``PLAN_DASHBOARD_MUTATION``; unset or empty installs none. Each mutation wraps a public
function of the module and breaks one property the tests are meant to pin, so a mutation
that leaves the suite green is a test gap. Later dashboard stages add rows to MUTATIONS.
TUI_MUTATIONS is the console view's family: it is looked up by name next to MUTATIONS but its rows
are killed by ``test_plan_dashboard_tui.py`` (and, for the row that targets ``tui_app``, by the
textual pilot file), not by the model tests. WEB_MUTATIONS is the web page's family, killed by
``test_plan_dashboard_web.py``. WEB_PAGE_MUTATIONS is the page's own family, byte substitutions in
what ``web.page_bytes`` returns, killed by ``test_plan_dashboard_web_dom.py``. PUBLISH_MUTATIONS is the status digest's family, killed by
``test_plan_dashboard_publish.py``. ATTENTION_MUTATIONS is the attention overlay's and the session
locator's family, killed by ``test_plan_dashboard_attention.py``.

``test_plan_dashboard_mutation_catalogue.py`` runs every MUTATIONS row in a subprocess and asserts
the model tests go red, every other TUI_MUTATIONS row likewise against the console tests, and the
``tui_app`` row against the pilot file where textual is importable; to add a mutation, add a row
here and that test covers it. By hand:
``PLAN_DASHBOARD_MUTATION=<name> PYTHONPATH=scripts/tests:scripts python3 -m pytest -q -rf
-p no:cacheprovider -p plan_dashboard_mutations scripts/tests/test_plan_dashboard_model.py``.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import importlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ENV = "PLAN_DASHBOARD_MUTATION"
MODEL = "plan_dashboard.model"
applied: str | None = None


def _walk(items, visit):
    for item in items:
        visit(item)
        _walk(item.get("children", []), visit)
    return items


def _with_kwargs(**changes):
    def wrap(original):
        def mutated(*args, **kwargs):
            return original(*args, **{**kwargs, **changes})
        return mutated
    return wrap


def _collect_then(effect):
    def wrap(original):
        def mutated(state_root, *args, **kwargs):
            result = original(state_root, *args, **kwargs)
            effect(Path(state_root))
            return result
        return mutated
    return wrap


def _drops_unreadable(original):
    def mutated(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshot["tasks"] = [t for t in snapshot["tasks"] if t.get("readable", True)]
        return snapshot
    return mutated


def _touch_state_file(root: Path) -> None:
    (root / ".touched").write_text("x", encoding="utf-8")


def _lock_state_dir(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    finally:
        os.close(fd)


def _detail_writes(original):
    def mutated(state_root, *args, **kwargs):
        result = original(state_root, *args, **kwargs)
        (Path(state_root) / ".detail-touched").write_text("x", encoding="utf-8")
        return result
    return mutated


def _attribution_by_time(original):
    def mutated(row, *args, **kwargs):
        return original({**row, "stage_index": None, "review_pair": None}, *args, **kwargs)
    return mutated


def _estimate_unflagged(original):
    def mutated(*args, **kwargs):
        def clear(item):
            item["estimate"] = {key: False for key in item["estimate"]}
        return _walk(original(*args, **kwargs), clear)
    return mutated


def _root_missing_zero(original):
    def mutated(*args, **kwargs):
        def zero(item):
            if item.get("root_cost_usd") is None:
                item["root_cost_usd"] = 0
        return _walk(original(*args, **kwargs), zero)
    return mutated


def _transcript_reread(original):
    def mutated(*args, **kwargs):
        importlib.import_module(MODEL).clear_cache()
        return original(*args, **kwargs)
    return mutated


def _open_window_now(original):
    def mutated(*args, **kwargs):
        now = dt.datetime.now(dt.timezone.utc)

        def close(item):
            if item.get("start") and item.get("end") is None:
                item["end"] = now.isoformat(timespec="seconds")
                start = dt.datetime.fromisoformat(item["start"])
                item["wall_clock_s"] = (now - start).total_seconds()
        return _walk(original(*args, **kwargs), close)
    return mutated


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def locked(self):
        return False


def _cache_unlocked(original):
    def mutated(*args, **kwargs):
        model = importlib.import_module(MODEL)
        real = model.CACHE_LOCK
        model.CACHE_LOCK = _NoLock()
        try:
            return original(*args, **kwargs)
        finally:
            model.CACHE_LOCK = real
    return mutated


def _single_read(original):
    return _one_read


def _one_read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


def _retry_without_pause(original):
    def mutated(*args, **kwargs):
        model = importlib.import_module(MODEL)
        real = model.RETRY_PAUSE_S
        model.RETRY_PAUSE_S = 0
        try:
            return original(*args, **kwargs)
        finally:
            model.RETRY_PAUSE_S = real
    return mutated


def _sidecar_kept(original):
    def mutated(*args, **kwargs):
        return False
    return mutated


def _foreign_session_rows(original):
    def mutated(ledger, session_id, plan_path):
        own = original(ledger, session_id, plan_path)
        return own + [r for owner, rows in ledger["by_session"].items() if owner != session_id for r in rows]
    return mutated


def _foreign_plan_rows(original):
    def mutated(ledger, session_id, plan_path):
        own = original(ledger, session_id, plan_path)
        return own + [r for plan, rows in ledger["by_plan"].items() if plan != plan_path for r in rows]
    return mutated


def _root_before_first_kept(original):
    def mutated(windows, usage_rows, events, resolved):
        earliest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return original(windows, usage_rows, [(earliest, {})] + list(events), resolved)
    return mutated


def _root_after_resolve_kept(original):
    def mutated(windows, usage_rows, events, resolved):
        return original(windows, usage_rows, events, False)
    return mutated


def _nested_spawn_zero(original):
    def mutated(*args, **kwargs):
        def zero(item):
            for child in item["children"]:
                child["spawn_cost_usd"] = 0
                child["spawn_count"] = 0
        return _walk(original(*args, **kwargs), zero)
    return mutated


MUTATIONS = {
    "model-ignores-window": (MODEL, "collect", _with_kwargs(window_hours=10**7)),
    "model-ignores-selection": (MODEL, "collect", _with_kwargs(include_all=True)),
    "model-writes-state": (MODEL, "collect", _collect_then(_touch_state_file)),
    "model-locks": (MODEL, "collect", _collect_then(_lock_state_dir)),
    "model-drops-unreadable": (MODEL, "collect", _drops_unreadable),
    "model-detail-writes": (MODEL, "detail", _detail_writes),
    "model-attribution-by-time": (MODEL, "attribute_spawn", _attribution_by_time),
    "model-estimate-unflagged": (MODEL, "phases", _estimate_unflagged),
    "model-root-missing-zero": (MODEL, "phases", _root_missing_zero),
    "model-transcript-reread": (MODEL, "transcript_usage", _transcript_reread),
    "model-open-window-now": (MODEL, "phases", _open_window_now),
    "model-cache-unlocked": (MODEL, "detail", _cache_unlocked),
    "model-retry-single-read": (MODEL, "_read_state", _single_read),
    "model-retry-without-pause": (MODEL, "_read_state", _retry_without_pause),
    "model-sidecar-kept": (MODEL, "_is_sidecar", _sidecar_kept),
    "model-ledger-foreign-session": (MODEL, "_owned_spawn_rows", _foreign_session_rows),
    "model-ledger-foreign-plan": (MODEL, "_owned_spawn_rows", _foreign_plan_rows),
    "model-root-before-first-kept": (MODEL, "_slice_root", _root_before_first_kept),
    "model-root-after-resolve-kept": (MODEL, "_slice_root", _root_after_resolve_kept),
    "model-child-reads-parent-transcript": (MODEL, "_transcript_paths", _with_kwargs(linked_child=False)),
    "model-nested-spawn-zero": (MODEL, "phases", _nested_spawn_zero),
}


def _overview_blank(original):
    def mutated(*args, **kwargs):
        return [{**row, "cells": ("",) * len(row["cells"])} for row in original(*args, **kwargs)]
    return mutated


def _keys_ignored(original):
    def mutated(view, key):
        return view
    return mutated


def _since_real_clock(original):
    def mutated(now, moment):
        return original(dt.datetime.now(dt.timezone.utc), moment)
    return mutated


def _estimate_prefix_dropped(original):
    def mutated(value, estimate, partial=False):
        return original(value, False, False)
    return mutated


def _importers_blind(original):
    def mutated(paths):
        return []
    return mutated


def _refresh_never(original):
    def mutated(app):
        return None
    return mutated


def _tree_expanded(original):
    def mutated(tree, *args, **kwargs):
        result = original(tree, *args, **kwargs)
        tree.root.expand_all()
        return result
    return mutated


def _tree_state_forgotten(original):
    def mutated(tree, nodes, expanded, cursor):
        return original(tree, nodes, frozenset(), ())
    return mutated


def _state_kept_across_tasks(original):
    def mutated(state, task):
        return state
    return mutated


def _every_key_global(original):
    def mutated(app, action, parameters):
        return True
    return mutated


def _nothing(original):
    def mutated(*args, **kwargs):
        return None
    return mutated


TUI = "plan_dashboard.tui"
TUI_APP = "plan_dashboard.tui_app"
SUPPORT = "plan_dashboard_support"
TUI_MUTATIONS = {
    "tui-overview-blank": (TUI, "overview_rows", _overview_blank),
    "tui-keys-ignored": (TUI, "handle_key", _keys_ignored),
    "tui-ignores-now": (TUI, "_since", _since_real_clock),
    "tui-writes-state": (TUI, "once", _collect_then(_touch_state_file)),
    "tui-estimate-unmarked": (TUI, "fmt_value", _estimate_prefix_dropped),
    "tui-confinement-blind": (SUPPORT, "textual_importers", _importers_blind),
    "tui-app-no-refresh": (TUI_APP, "refresh_if_due", _refresh_never),
    "tui-app-tree-expanded": (TUI_APP, "fill_tree", _tree_expanded),
    "tui-app-detail-keys-swallowed": (TUI_APP, "route_key", _every_key_global),
    "tui-app-tree-state-forgotten": (TUI_APP, "fill_tree", _tree_state_forgotten),
    "tui-app-detail-unfocused": (TUI_APP, "focus_for", _nothing),
    "tui-app-expand-global": (TUI_APP, "tree_state_for", _state_kept_across_tasks),
}


def _always(value):
    def wrap(original):
        def mutated(*args, **kwargs):
            return value
        return mutated
    return wrap


def _post_answers_ok(original):
    def mutated(*args, **kwargs):
        handler = original(*args, **kwargs)

        def do_POST(self):
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
        handler.do_POST = do_POST
        return handler
    return mutated


def _touch_then_serve(original):
    def mutated(state_root, *args, **kwargs):
        _touch_state_file(Path(state_root))
        return original(state_root, *args, **kwargs)
    return mutated


def _head_streams(original):
    def mutated(*args, **kwargs):
        handler = original(*args, **kwargs)
        events = handler._events

        def _events(self):
            self.command = "GET"
            return events(self)
        handler._events = _events
        return handler
    return mutated


WEB = "plan_dashboard.web"
WEB_MUTATIONS = {
    "web-host-open": (WEB, "host_allowed", _always(True)),
    "web-bind-open": (WEB, "check_bind", _always(None)),
    "web-method-open": (WEB, "make_handler", _post_answers_ok),
    "web-events-every-tick": (WEB, "event_due", _always(True)),
    "web-writes-state": (WEB, "serve", _touch_then_serve),
    "web-head-streams": (WEB, "make_handler", _head_streams),
}


def _page_edit(target, replacement, *, count=1):
    """Substitute ``replacement`` for ``target`` in the page bytes; the page must hold exactly
    ``count`` occurrences of ``target`` when the plugin loads, so a rewritten page cannot leave
    the mutation a silent no-op."""
    def wrap(original):
        found = original().decode("utf-8").count(target)
        if found != count:
            raise pytest.UsageError(f"page mutation target {target!r} occurs {found} times, expected {count}")

        def mutated():
            return original().decode("utf-8").replace(target, replacement).encode("utf-8")
        return mutated
    return wrap


_THEME_PROPERTIES = ("text", "text-dim", "text-ok", "text-warn", "text-bad", "text-accent", "bg", "bg-panel",
                     "bg-hover", "bg-selected", "bg-live-active", "bg-live-idle", "bg-live-stale", "bg-live-resolved",
                     "bg-live-stub", "bg-wait-choice", "bg-wait-permission", "bg-wait-idle", "line")
ONE_COLOUR_FOR_EVERY_THEME_PROPERTY = (
    ":root, :root:root { " + " ".join(f"--{name}: #808080 !important;" for name in _THEME_PROPERTIES) + " }\n")

WEB_PAGE_MUTATIONS = {
    "web-detail-offscreen": (WEB, "page_bytes", _page_edit("#detail { position: fixed;", "#detail { position: static;")),
    "web-row-click-dead": (WEB, "page_bytes", _page_edit('tr.addEventListener("click",', 'tr.addEventListener("x-click",')),
    "web-detail-no-close": (WEB, "page_bytes", _page_edit("function closeDetail() {", "function closeDetail() { return;")),
    "web-colours-absent": (WEB, "page_bytes", _page_edit("</style>", ONE_COLOUR_FOR_EVERY_THEME_PROPERTY + "</style>")),
    "web-contrast-low": (WEB, "page_bytes", _page_edit("--text-dim: #a5b0bd", "--text-dim: #1c2330")),
    "web-detail-unscrollable": (WEB, "page_bytes", _page_edit("max-height: calc(100vh - 2rem); overflow-y: auto;", "")),
}


def _stop_after_one_pass(original):
    def mutated(*args, **kwargs):
        calls = []
        kwargs["stop"] = lambda: calls.append(1) or len(calls) > 2
        return original(*args, **kwargs)
    return mutated


def _health_unstubbed(original):
    def mutated(*args, **kwargs):
        return contextlib.nullcontext()
    return mutated


PUBLISH = "plan_dashboard.publish"
PUBLISH_MUTATIONS = {
    "publish-no-cap": (PUBLISH, "should_send", _with_kwargs(min_interval_s=0)),
    "publish-always-send": (PUBLISH, "should_send", _always(True)),
    "publish-watch-once": (PUBLISH, "watch", _stop_after_one_pass),
    "publish-writes-state": (PUBLISH, "run_once", _collect_then(_touch_state_file)),
    "publish-health-net-open": (PUBLISH, "network_blocked", _health_unstubbed),
    "publish-no-lock": (PUBLISH, "single_instance", _health_unstubbed),
    "publish-attempt-unrecorded": (PUBLISH, "record_attempt", _always(None)),
}


def _tail_without_pending(original):
    def mutated(tail, permission):
        if tail is not None:
            tail = copy.copy(tail)
            tail.pending = {}
        return original(tail, permission)
    return mutated


def _permission_first(original):
    def mutated(tail, permission):
        if tail is not None and permission:
            return "permission", tail.last_ts
        return original(tail, permission)
    return mutated


def _missing_transcript_reads_none(original):
    def mutated(tail, permission):
        return ("none", None) if tail is None else original(tail, permission)
    return mutated


def _idle_never(original):
    def mutated(tail, permission):
        waiting, since = original(tail, permission)
        return ("none" if waiting == "idle" else waiting), since
    return mutated


def _bind_first_found(original):
    def mutated(engine_session, locator, transcripts_root):
        attention = importlib.import_module(ATTENTION)
        bound = importlib.import_module("lib.engine_session_binding").harness_sessions_driving(engine_session)
        for candidate in dict.fromkeys([engine_session, *bound]):
            path = attention.find_transcript(candidate, locator, transcripts_root)
            if path is not None:
                return candidate, path, bound
        return (max(bound) if bound else None), None, bound
    return mutated


def _chat_never_cut(original):
    def mutated(role, text, tool):
        return {"role": role, "text": text, "tool": tool, "cut": False}
    return mutated


def _question_is_whole_input(original):
    def mutated(tool_input):
        return json.dumps(tool_input)
    return mutated


def _reread_always(original):
    def mutated(path):
        importlib.import_module(ATTENTION)._CACHE.clear()
        return original(path)
    return mutated


def _rows_ignore_binding(original):
    def mutated(engine_sessions, locator, transcripts_root):
        attention = importlib.import_module(ATTENTION)
        overlays, _ = original(engine_sessions, locator, transcripts_root)
        entries = sorted(importlib.import_module(LOCATE).sessions(locator), key=lambda e: e["harness_session"])
        return overlays, [attention.session_row(e, locator, transcripts_root) for e in entries]
    return mutated


def _digest_moves(original):
    def mutated(overlays, sessions):
        return original(overlays, sessions) + str(time.monotonic_ns())
    return mutated


def _outside_root_opened(original):
    def mutated(harness_session, locator, transcripts_root):
        found = original(harness_session, locator, transcripts_root)
        if found is None and transcripts_root is not None:
            config_root = importlib.import_module("lib.config_root")
            name = config_root.sanitize_session_id(harness_session)
            return next(iter(config_root.iter_transcripts(f"*/{name}.jsonl")), None)
        return found
    return mutated


def _jump_through_shell(original):
    def mutated(argv):
        return subprocess.run(argv, shell=True, timeout=5, capture_output=True, text=True)
    return mutated


def _waiting_always_none(original):
    def mutated(tail, permission):
        waiting, since = original(tail, permission)
        return ("none" if waiting else None), since
    return mutated


def _content_items(entry):
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _thinking_kept(original):
    def mutated(tail, entry):
        entry = copy.deepcopy(entry)
        for item in _content_items(entry):
            if isinstance(item, dict) and item.get("type") == "thinking":
                item["type"], item["text"] = "text", item.get("thinking", "")
        return original(tail, entry)
    return mutated


def _tool_result_kept(original):
    def mutated(tail, entry):
        original(tail, entry)
        attention = importlib.import_module(ATTENTION)
        for item in _content_items(entry):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                tail.chat.append(attention.chat_entry("user", str(item.get("content")), None))
    return mutated


def _chat_unbounded(original):
    def mutated(path):
        attention = importlib.import_module(ATTENTION)
        attention.CHAT_MAX_ENTRIES = attention.CHAT_TEXT_MAX = 10**9
        return original(path)
    return mutated


def _overlay_writes_state(original):
    def mutated(engine_sessions, locator, transcripts_root):
        result = original(engine_sessions, locator, transcripts_root)
        if transcripts_root is not None:
            _touch_state_file(Path(transcripts_root))
        return result
    return mutated


def _jump_lets_timeout_through(original):
    def mutated(target, *, runner=None):
        return (runner or importlib.import_module(LOCATE)._run)(list(target["argv"]))
    return mutated


ATTENTION = "plan_dashboard.attention"
LOCATE = "plan_dashboard.locate"
ATTENTION_MUTATIONS = {
    "attention-waiting-none": (ATTENTION, "waiting_state", _waiting_always_none),
    "attention-precedence-swapped": (ATTENTION, "waiting_state", _permission_first),
    "chat-thinking-leak": (ATTENTION, "_apply", _thinking_kept),
    "chat-tool-result-leak": (ATTENTION, "_apply", _tool_result_kept),
    "chat-unbounded": (ATTENTION, "read_tail", _chat_unbounded),
    "attention-writes-state": (ATTENTION, "collect", _overlay_writes_state),
    "attention-reread": (ATTENTION, "read_tail", _reread_always),
    "attention-event-every-tick": ("plan_dashboard.web", "event_due", _always(True)),
    "locator-ignored": (LOCATE, "permission_wait", _always(None)),
    "locator-health-net-open": (LOCATE, "health_stubbed", _health_unstubbed),
    "locator-jump-raises": (LOCATE, "jump", _jump_lets_timeout_through),
    "sessions-unbound-dropped": (LOCATE, "sessions", _always([])),
    "sessions-bound-twice": (ATTENTION, "collect", _rows_ignore_binding),
    "chat-ask-question-dropped": (ATTENTION, "question_text", _always("")),
    "attention-choice-ignored": (ATTENTION, "waiting_state", _tail_without_pending),
    "attention-no-transcript-reads-none": (ATTENTION, "waiting_state", _missing_transcript_reads_none),
    "attention-idle-never": (ATTENTION, "waiting_state", _idle_never),
    "attention-bindings-ignored": ("lib.engine_session_binding", "harness_sessions_driving", _always([])),
    "attention-bind-first-found": (ATTENTION, "bind_session", _bind_first_found),
    "attention-chat-never-cut": (ATTENTION, "chat_entry", _chat_never_cut),
    "attention-chat-leaks-input": (ATTENTION, "question_text", _question_is_whole_input),
    "attention-digest-moves": (ATTENTION, "attention_digest", _digest_moves),
    "attention-outside-root-opened": (ATTENTION, "find_transcript", _outside_root_opened),
    "locate-jump-through-shell": (LOCATE, "_run", _jump_through_shell),
}


def pytest_addoption(parser):
    parser.addoption("--plan-dashboard-mutation", default="", help=f"same as {ENV}; the variable wins")


def pytest_configure(config):
    global applied
    name = os.environ.get(ENV) or config.getoption("--plan-dashboard-mutation")
    if not name:
        return
    known = {**MUTATIONS, **TUI_MUTATIONS, **WEB_MUTATIONS, **WEB_PAGE_MUTATIONS, **PUBLISH_MUTATIONS,
             **ATTENTION_MUTATIONS}
    if name not in known:
        raise pytest.UsageError(f"{ENV}={name!r} is not a known mutation; known: {sorted(known)}")
    module_name, attr, wrapper = known[name]
    module = importlib.import_module(module_name)
    setattr(module, attr, wrapper(getattr(module, attr)))
    applied = name


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(f"plan-dashboard mutation: {applied + ' applied' if applied else 'none'}")
