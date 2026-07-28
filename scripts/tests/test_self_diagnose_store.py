"""Tests for self_diagnose_store.py and the turn-boundary guardian it feeds.

Two halves:

  - the STORE: stable-key dedup across scans, resolve-out when a condition is
    gone, the ack/snooze disposal primitives and their expiries, the per-kind
    actionability table, the age debounce, and the advisory tier filter plus its
    never-silently-drop contract;
  - the GUARDIAN: hook-turn-end-gate blocks once per session on an open
    actionable finding, stays silent for every disposed / advisory / too-fresh
    case, and fails open on a corrupt store.

Every test runs against a store in tmp_path (the autouse conftest fixture
redirects CLAUDE_SELF_DIAGNOSE_STORE) with an injected filer — never the real
store, never the real channel.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import self_diagnose_store as sds  # noqa: E402


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "hook_turn_end_gate_store", SCRIPTS_DIR / "hook-turn-end-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store_file(tmp_path):
    return tmp_path / "findings.jsonl"


def _finding(kind, path, detail="d"):
    return {"kind": kind, "path": path, "detail": detail}


ORPHAN = _finding("orphan-leaf", "/mem/leaves/x.md", "not reachable")
CEILING = _finding("ceiling-proximity", "CLAUDE.md", "38606 chars warn")


# --- store: dedup, resolve-out, counters ------------------------------------

def test_same_condition_is_one_row_across_scans(store_file):
    sds.upsert_findings([ORPHAN], store_file, T0)
    rows = sds.upsert_findings([ORPHAN], store_file, T0 + timedelta(days=1))
    assert len(rows) == 1
    assert rows[0]["times_surfaced"] == 2
    assert rows[0]["first_seen"] == sds._iso(T0)


def test_key_is_stable_for_kind_and_path():
    a = sds.finding_key("orphan-leaf", "/mem/x.md")
    assert a == sds.finding_key("orphan-leaf", "/mem/x.md")
    assert a != sds.finding_key("orphan-leaf", "/mem/y.md")
    assert a != sds.finding_key("orphan-index", "/mem/x.md")


def test_detail_updates_without_losing_history(store_file):
    sds.upsert_findings([ORPHAN], store_file, T0)
    rows = sds.upsert_findings(
        [_finding("orphan-leaf", "/mem/leaves/x.md", "still not reachable")],
        store_file,
        T0 + timedelta(days=3),
    )
    assert rows[0]["detail"] == "still not reachable"
    assert rows[0]["first_seen"] == sds._iso(T0)


def test_finding_gone_from_the_scan_is_resolved_out(store_file):
    sds.upsert_findings([ORPHAN, CEILING], store_file, T0)
    rows = sds.upsert_findings([CEILING], store_file, T0 + timedelta(days=1))
    assert [r["kind"] for r in rows] == ["ceiling-proximity"]
    assert sds.load_rows(store_file, T0 + timedelta(days=1)) == rows


# --- store: actionability table and the age debounce ------------------------

def test_actionability_table_is_exactly_the_declared_kinds():
    assert sds.is_actionable("orphan-leaf")
    assert sds.is_actionable("orphan-index")
    assert sds.is_actionable("broken-hook-registration")
    for advisory in (
        "ceiling-proximity",
        "near-duplicate",
        "dangling-pointer",
        "oversized-index",
        "no-root-index",
    ):
        assert not sds.is_actionable(advisory), advisory


def test_ceiling_proximity_never_blocks(store_file):
    """It already has a dedicated debounced channel (hook-instruction-grooming-due);
    a second inbox for one finding is alert fatigue, not a fix."""
    sds.upsert_findings([CEILING], store_file, T0)
    rows = sds.load_rows(store_file, T0 + timedelta(days=365))
    assert sds.open_actionable(rows, T0 + timedelta(days=365)) == []
    assert len(sds.advisory_open(rows, T0 + timedelta(days=365))) == 1


def test_fresh_finding_does_not_block_its_own_author(store_file):
    sds.upsert_findings([ORPHAN], store_file, T0)
    rows = sds.load_rows(store_file, T0 + timedelta(hours=1))
    assert sds.open_actionable(rows, T0 + timedelta(hours=1)) == []
    aged = T0 + timedelta(days=sds.ACTIONABLE_MIN_AGE_DAYS + 1)
    assert len(sds.open_actionable(sds.load_rows(store_file, aged), aged)) == 1


# --- store: disposal --------------------------------------------------------

def test_ack_suppresses_the_finding(store_file):
    sds.upsert_findings([ORPHAN], store_file, T0)
    key = sds.finding_key(ORPHAN["kind"], ORPHAN["path"])
    assert sds.ack(key, "draft in flight", store_file, T0)
    later = T0 + timedelta(days=5)
    assert sds.open_actionable(sds.load_rows(store_file, later), later) == []


def test_snooze_suppresses_then_expires(store_file):
    sds.upsert_findings([ORPHAN], store_file, T0)
    key = sds.finding_key(ORPHAN["kind"], ORPHAN["path"])
    assert sds.snooze(key, 7, store_file, T0)

    inside = T0 + timedelta(days=3)
    assert sds.open_actionable(sds.load_rows(store_file, inside), inside) == []

    after = T0 + timedelta(days=8)
    assert len(sds.open_actionable(sds.load_rows(store_file, after), after)) == 1


def test_expired_ack_reopens_and_the_counter_keeps_running(store_file):
    """The disposal rule ("fired and ignored more than twice in 30 days ->
    retire or downgrade") reads times_surfaced. A PERMANENT ack would freeze
    that counter, making the rule unobservable by construction — so an expired
    ack must return the row to open AND the counter must keep incrementing."""
    sds.upsert_findings([ORPHAN], store_file, T0)
    key = sds.finding_key(ORPHAN["kind"], ORPHAN["path"])
    sds.ack(key, "later", store_file, T0)

    before_ttl = T0 + timedelta(days=sds.ACK_TTL_DAYS - 1)
    assert sds.open_actionable(sds.load_rows(store_file, before_ttl), before_ttl) == []

    after_ttl = T0 + timedelta(days=sds.ACK_TTL_DAYS + 1)
    rows = sds.upsert_findings([ORPHAN], store_file, after_ttl)
    assert rows[0]["status"] == "open"
    assert rows[0]["times_surfaced"] == 2
    assert len(sds.open_actionable(rows, after_ttl)) == 1


def test_ack_of_an_unknown_key_is_a_miss(store_file):
    sds.upsert_findings([ORPHAN], store_file, T0)
    assert not sds.ack("deadbeef", "nope", store_file, T0)


# --- store: fail-open -------------------------------------------------------

def test_absent_store_yields_no_rows(tmp_path):
    assert sds.load_rows(tmp_path / "never-written.jsonl") == []


def test_corrupt_store_keeps_the_rows_it_can_parse(store_file):
    good = json.dumps({"key": "abc", "kind": "orphan-leaf", "path": "/x", "detail": "d",
                       "first_seen": sds._iso(T0), "last_seen": sds._iso(T0),
                       "times_surfaced": 1, "status": "open"})
    store_file.write_text(good + "\n{\"key\": \"trunc\", \"kind\"\n", encoding="utf-8")
    rows = sds.load_rows(store_file, T0)
    assert [r["key"] for r in rows] == ["abc"]


# --- store: advisory routing ------------------------------------------------

def _recording_filer(rc=0, ref="issue-1"):
    def filer(row):
        filer.calls.append(row["path"])
        return rc, ref
    filer.calls = []
    return filer


def test_digest_channel_files_nothing(store_file):
    rows = sds.upsert_findings([CEILING], store_file, T0)
    filer = _recording_filer()
    assert sds.route_advisory(rows, "digest", filer, path=store_file) == []
    assert filer.calls == []
    assert len(sds.digest_lines(rows, T0)) == 1


def test_backlog_tier_filter_refuses_a_personal_memory_path(store_file):
    """The Core backlog venue is a PUBLIC GitHub repo and publication is
    irrecoverable, so only a path inside the Core repo may ever auto-file."""
    personal = str(Path.home() / ".claude-agent" / "projects" / "p" / "memory" / "x.md")
    rows = sds.upsert_findings([_finding("near-duplicate", personal)], store_file, T0)
    filer = _recording_filer()
    assert sds.route_advisory(rows, "backlog", filer, path=store_file) == []
    assert filer.calls == []
    assert rows[0]["filed_ref"] is None


def test_backlog_files_a_core_repo_path(store_file):
    core = str(SCRIPTS_DIR.parent / "memory-global" / "leaves" / "x.md")
    rows = sds.upsert_findings([_finding("near-duplicate", core)], store_file, T0)
    filer = _recording_filer()
    filed = sds.route_advisory(rows, "backlog", filer, path=store_file)
    assert [r["path"] for r in filed] == [core]
    assert filer.calls == [core]
    assert sds.load_rows(store_file, T0)[0]["filed_ref"] == "issue-1"


def test_a_refused_filing_is_never_marked_filed(store_file):
    """file-difficulty.py exits 2 on an author machine — every filing here is
    refused. A non-zero exit must leave the row unfiled and in the digest; a
    channel that can silently swallow a finding is a drain in a channel's
    clothes."""
    core = str(SCRIPTS_DIR.parent / "memory-global" / "leaves" / "y.md")
    rows = sds.upsert_findings([_finding("near-duplicate", core)], store_file, T0)
    filer = _recording_filer(rc=2, ref="")
    assert sds.route_advisory(rows, "backlog", filer, path=store_file) == []
    assert rows[0]["filed_ref"] is None
    assert len(sds.digest_lines(rows, T0)) == 1
    assert sds.load_rows(store_file, T0)[0]["filed_ref"] is None


def test_a_filed_row_is_never_refiled(store_file):
    core = str(SCRIPTS_DIR.parent / "memory-global" / "leaves" / "z.md")
    rows = sds.upsert_findings([_finding("near-duplicate", core)], store_file, T0)
    sds.route_advisory(rows, "backlog", _recording_filer(), path=store_file)
    again = _recording_filer()
    assert sds.route_advisory(sds.load_rows(store_file, T0), "backlog", again,
                              path=store_file) == []
    assert again.calls == []


# --- the turn-boundary guardian ---------------------------------------------

def _user_line(text):
    return {"message": {"role": "user", "content": text}}


def _assistant_line(text):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _transcript(tmp_path, user_text, name="t.jsonl"):
    p = tmp_path / name
    p.write_text(
        "\n".join(json.dumps(l) for l in [_user_line(user_text), _assistant_line("ok")])
        + "\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def live_store(tmp_path, monkeypatch):
    """A store the gate will read via CLAUDE_SELF_DIAGNOSE_STORE, plus an
    isolated agent-home for the durable markers."""
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    path = tmp_path / "gate-findings.jsonl"
    monkeypatch.setenv("CLAUDE_SELF_DIAGNOSE_STORE", str(path))
    return path


def _seed(path, findings, age_days=10):
    """Write findings first seen `age_days` ago, so the debounce is satisfied."""
    sds.upsert_findings(findings, path, datetime.now(timezone.utc) - timedelta(days=age_days))


def _decide(tmp_path, text="add a parser for the config file", name="t.jsonl"):
    t = _transcript(tmp_path, text, name)
    return gate.decide({"transcript_path": str(t), "stop_hook_active": False,
                        "session_id": "sess-store"})


def test_guardian_blocks_on_an_open_actionable_finding(tmp_path, live_store):
    _seed(live_store, [ORPHAN])
    out = _decide(tmp_path)
    assert out is not None
    assert out["decision"] == "block"
    assert "orphan-leaf" in out["reason"]
    assert "self_diagnose_store.py --ack" in out["reason"]
    # remediation travels with the finding, not just its name
    assert "owning MEMORY.md" in out["reason"]


def test_guardian_silent_for_an_advisory_only_store(tmp_path, live_store):
    _seed(live_store, [CEILING])
    assert _decide(tmp_path) is None


def test_guardian_silent_when_acked(tmp_path, live_store):
    _seed(live_store, [ORPHAN])
    sds.ack(sds.finding_key(ORPHAN["kind"], ORPHAN["path"]), "deliberate", live_store)
    assert _decide(tmp_path) is None


def test_guardian_silent_when_snoozed(tmp_path, live_store):
    _seed(live_store, [ORPHAN])
    sds.snooze(sds.finding_key(ORPHAN["kind"], ORPHAN["path"]), 7, live_store)
    assert _decide(tmp_path) is None


def test_guardian_silent_for_a_finding_younger_than_the_debounce(tmp_path, live_store):
    _seed(live_store, [ORPHAN], age_days=0)
    assert _decide(tmp_path) is None


def test_guardian_blocks_at_most_once_per_session(tmp_path, live_store):
    _seed(live_store, [ORPHAN])
    assert _decide(tmp_path, "first message", "a.jsonl") is not None
    # a DIFFERENT triggering message, so the per-message marker cannot be what
    # silences it — only the per-session marker can.
    assert _decide(tmp_path, "second message", "b.jsonl") is None


def test_guardian_fails_open_on_a_corrupt_store(tmp_path, live_store):
    live_store.write_text("{not json at all\n", encoding="utf-8")
    assert _decide(tmp_path) is None


def test_guardian_fails_open_on_an_absent_store(tmp_path, live_store):
    assert not live_store.exists()
    assert _decide(tmp_path) is None


def test_guardian_is_pure_given_its_frozen_tuple():
    """The store read happens in the shell; the guardian decides from the tuple."""
    ctx = gate.TurnContext(
        last_user_text="x",
        invocations=frozenset(),
        transcript_path="/nonexistent.jsonl",
        session_key="s",
        agentctl_state=None,
        self_diagnose_findings=("[k] orphan-leaf: /mem/x.md — not reachable",),
    )
    assert len(gate.self_diagnose_findings_blockers(ctx)) == 1
    assert gate.self_diagnose_findings_blockers(
        gate.TurnContext(
            last_user_text="x",
            invocations=frozenset(),
            transcript_path="/nonexistent.jsonl",
            session_key="s",
            agentctl_state=None,
        )
    ) == []
