"""cli.py's --host wiring across start/reset/classify (Host-isolated spawn plan,
step 3): best-effort bind at start/reset, the hard require=True gate at
classify, and the sticky-conflict refusal surfaced as Directive(ok=False)
rather than an uncaught exception.
"""
from __future__ import annotations

import json
from argparse import Namespace

from agentctl import cli
from lib.runtime_models import HOST_CLAUDE, HOST_CURSOR


def ns(**kw):
    return Namespace(**kw)


def _start(store, sid, host=None):
    return cli.cmd_start(
        ns(session=sid, task="t", goal="g", done_criterion="dc",
           criterion_type="measurable", recursion_depth=0, host=host),
        store=store,
    )


def _classify(store, sid, host=None):
    return cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False, host=host),
        store=store,
    )


# --- cmd_start: best-effort bind -------------------------------------------------

def test_cmd_start_explicit_host_binds_immediately(store):
    _start(store, "s1", host=HOST_CURSOR)
    assert store.load("s1").runtime_host == HOST_CURSOR


def test_cmd_start_auto_detects_claude_from_session_id_fixture(store):
    # The suite-wide autouse fixture sets CLAUDE_CODE_SESSION_ID, so an
    # unspecified --host still resolves at start (best-effort, not required).
    _start(store, "s2")
    assert store.load("s2").runtime_host == HOST_CLAUDE


def test_cmd_start_stays_unbound_when_ambiguous(store, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    from lib import host_llm
    monkeypatch.setattr(host_llm, "preflight", lambda host: (False, "no key"))
    _start(store, "s3")
    assert store.load("s3").runtime_host is None


# --- cmd_classify: the hard gate -------------------------------------------------

def test_cmd_classify_binds_from_session_id_fixture_when_unbound(store):
    _start(store, "s4", host=None)
    d = _classify(store, "s4")
    assert d.ok is True
    assert store.load("s4").runtime_host == HOST_CLAUDE


def test_cmd_classify_explicit_host_binds_when_unbound(store, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    from lib import host_llm
    monkeypatch.setattr(host_llm, "preflight", lambda host: (False, "no key"))
    _start(store, "s5", host=None)
    assert store.load("s5").runtime_host is None
    d = _classify(store, "s5", host=HOST_CURSOR)
    assert d.ok is True
    assert store.load("s5").runtime_host == HOST_CURSOR


def test_cmd_classify_returns_ok_false_directive_when_ambiguous(store, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    from lib import host_llm
    monkeypatch.setattr(host_llm, "preflight", lambda host: (False, "no key"))
    _start(store, "s6", host=None)
    d = _classify(store, "s6")
    assert d.ok is False
    assert store.load("s6").runtime_host is None
    # The session stays at its pre-classify node — classify never partially applied.
    assert store.load("s6").weight_class is None


def test_cmd_classify_matching_explicit_host_is_a_noop_after_start(store):
    _start(store, "s7", host=HOST_CLAUDE)
    d = _classify(store, "s7", host=HOST_CLAUDE)
    assert d.ok is True
    assert store.load("s7").runtime_host == HOST_CLAUDE


def test_cmd_classify_conflicting_explicit_host_refuses_without_raising(store):
    _start(store, "s8", host=HOST_CLAUDE)
    d = _classify(store, "s8", host=HOST_CURSOR)
    assert d.ok is False
    # Sticky: the original bind from start survives the refused rebind.
    assert store.load("s8").runtime_host == HOST_CLAUDE
    # Classify never partially applied on the refused bind.
    assert store.load("s8").weight_class is None

# --- CLI argparse surface: --host must reach cmd_classify ----------------------

def test_classify_parser_accepts_host_flag():
    args = cli.build_parser().parse_args([
        "classify", "--session", "s", "--host", "cursor", "--architectural",
    ])
    assert args.host == HOST_CURSOR


def test_cli_classify_conflicting_host_via_main_not_argparse(capsys, tmp_path):
    """Regression: classify --host <other> must not die in argparse; sticky
    HostConflict surfaces as Directive(ok=False) through cli.main."""
    root = str(tmp_path)
    rc = cli.main([
        "--state-root", root,
        "start", "--session", "cli-host", "--task", "t",
        "--host", HOST_CLAUDE,
    ])
    assert rc == 0
    capsys.readouterr()  # discard start directive JSON

    rc = cli.main([
        "--state-root", root,
        "classify", "--session", "cli-host", "--host", HOST_CURSOR,
        "--architectural",
    ])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "already bound" in payload["detail"]
    assert HOST_CLAUDE in payload["detail"]
    assert HOST_CURSOR in payload["detail"]

