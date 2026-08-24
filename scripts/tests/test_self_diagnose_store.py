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

import ast
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import difficulty_channel as dc  # noqa: E402
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


def test_a_clean_scan_does_not_call_an_absent_store_into_existence(tmp_path):
    """The store's EXISTENCE is read as evidence that something once fired — the
    leaf tracking this instrument says exactly that of its own pending first
    firing. A clean scan writing an empty file would erase that reading, and every
    routed run is a clean scan until the day one is not.

    The guard is deliberately narrow, and the next test is why: an empty scan over
    an EXISTING store is the resolve-out signal, not a no-op."""
    store = tmp_path / "never-written.jsonl"

    assert sds.upsert_findings([], store, T0) == []
    assert not store.exists()


def test_a_clean_scan_still_resolves_out_an_existing_store(store_file):
    """The other side of the guard. Skipping the write whenever `findings` is
    empty would make a fixed condition accuse forever — absence from a completed
    scan is precisely how a finding closes."""
    sds.upsert_findings([ORPHAN], store_file, T0)

    assert sds.upsert_findings([], store_file, T0 + timedelta(days=1)) == []
    assert sds.load_rows(store_file, T0 + timedelta(days=1)) == []


# --- store: actionability table and the age debounce ------------------------

def test_actionability_table_is_exactly_the_declared_kinds():
    assert sds.is_actionable("orphan-leaf")
    assert sds.is_actionable("orphan-index")
    assert sds.is_actionable("broken-hook-registration")
    for advisory in sds.ADVISORY_KINDS:
        assert not sds.is_actionable(advisory), advisory


def _detector_kinds() -> "set[str]":
    """Every `kind` string self-diagnose.py can emit, read out of its source.

    Read rather than duplicated, so adding a ninth kind to the detector breaks
    this file instead of silently defaulting to advisory with the fallback
    remediation. `scan_orphans` picks its kind through a conditional bound to a
    local before the Difficulty(...) call, hence the second branch. An expression
    shape neither branch understands yields nothing and trips the caller's
    minimum-size assertion — the extraction fails loud, never vacuously."""

    def _value_strings(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.IfExp):
            yield from _value_strings(node.body)
            yield from _value_strings(node.orelse)

    tree = ast.parse((SCRIPTS_DIR / "self-diagnose.py").read_text(encoding="utf-8"))
    kinds: "set[str]" = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "Difficulty"
            and node.args
        ):
            kinds |= set(_value_strings(node.args[0]))
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "kind" for t in node.targets
        ):
            kinds |= set(_value_strings(node.value))
    return kinds


def test_every_detector_kind_is_placed_in_exactly_one_table():
    """A ninth kind added to self-diagnose.py must force a deliberate decision.

    Without this the detector and the store drift silently: an unplaced kind is
    advisory with the fallback remediation "investigate and close" and no test
    fails. The DEFAULT direction stays safe — `is_actionable` remains a membership
    test against ACTIONABLE_KINDS alone, so an unknown kind can still never
    block; what this adds is that it can no longer arrive unnoticed.

    EXTERNAL_KINDS is subtracted rather than tolerated: the store now serves a
    second producer (policy-scorecard.py), and its kinds are not in the
    detector's vocabulary. Declaring them in one named set keeps this an
    equality check — a kind belonging to neither the detector nor a declared
    external producer still fails."""
    detector = _detector_kinds()
    assert len(detector) >= 8, f"kind extraction broke: {detector}"
    assert sds.ACTIONABLE_KINDS & sds.ADVISORY_KINDS == frozenset()
    assert sds.EXTERNAL_KINDS & detector == frozenset()
    assert (sds.ACTIONABLE_KINDS | sds.ADVISORY_KINDS) - sds.EXTERNAL_KINDS == detector
    assert set(sds.REMEDIATION) - sds.EXTERNAL_KINDS == detector
    assert sds.EXTERNAL_KINDS <= set(sds.REMEDIATION)
    # …and placed in a table, not merely remediated. The four assertions above are
    # all satisfied by a kind declared in EXTERNAL_KINDS and REMEDIATION alone,
    # which then defaults to advisory — `is_actionable` is a membership test, so
    # absence reads as "advisory" rather than as "undeclared". For an external
    # producer that means its findings quietly stop blocking anything.
    assert sds.EXTERNAL_KINDS <= sds.ACTIONABLE_KINDS | sds.ADVISORY_KINDS


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


def test_the_three_views_partition_the_open_rows(store_file):
    """No open row may fall between the buckets the SessionStart summary reports."""
    fresh = _finding("orphan-leaf", "/mem/fresh.md")
    aged = _finding("orphan-leaf", "/mem/aged.md")
    sds.upsert_findings([aged], store_file, T0)
    sds.upsert_findings([aged, fresh, CEILING], store_file, T0 + timedelta(days=30))

    now = T0 + timedelta(days=30)
    rows = sds.load_rows(store_file, now)
    buckets = (
        sds.open_actionable(rows, now),
        sds.debounced_actionable(rows, now),
        sds.advisory_open(rows, now),
    )
    assert [len(b) for b in buckets] == [1, 1, 1]
    keys = [r["key"] for b in buckets for r in b]
    assert sorted(keys) == sorted(r["key"] for r in rows)
    assert len(set(keys)) == len(keys)


def test_a_duplicated_key_within_one_scan_is_one_row(store_file):
    """near-duplicate keys on the FIRST leaf of each pair, so A-vs-B and A-vs-C
    both arrive keyed on A in a single scan."""
    dup = _finding("near-duplicate", "/mem/a.md", "0.7 Jaccard vs b.md")
    rows = sds.upsert_findings([dup, dup], store_file, T0)
    assert len(rows) == 1
    assert rows[0]["times_surfaced"] == 1
    assert len(sds.load_rows(store_file, T0)) == 1


def test_save_rows_replaces_atomically(store_file, monkeypatch):
    """A crash between the write and the rename leaves the previous store intact
    rather than a truncated one."""
    sds.upsert_findings([ORPHAN], store_file, T0)
    before = store_file.read_bytes()

    def _die(src, dst):
        raise OSError("crash between write and rename")

    monkeypatch.setattr(os, "replace", _die)
    with pytest.raises(OSError):
        sds.save_rows([], store_file)

    assert store_file.read_bytes() == before


def test_a_non_string_kind_never_raises_out_of_a_view(store_file):
    """A hand-edited or partially-corrupt row must not traceback the CLI: an
    unhashable `kind` would make every `kind in ...` membership test a TypeError."""
    store_file.write_text(
        json.dumps({"key": "abc", "kind": ["orphan-leaf"], "path": "/x", "detail": "d",
                    "first_seen": sds._iso(T0), "last_seen": sds._iso(T0),
                    "times_surfaced": 1, "status": "open"}) + "\n",
        encoding="utf-8",
    )
    rows = sds.load_rows(store_file, T0)
    assert sds.open_actionable(rows, T0) == []
    assert len(sds.advisory_open(rows, T0)) == 1
    assert sds.describe(rows[0], T0)
    assert sds.digest_lines(rows, T0)
    assert sds.main(["--store", str(store_file), "--list"]) == 0


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


@pytest.fixture
def core_leaf(tmp_path):
    """(stand-in Core root, a real file inside it).

    The tier filter requires the candidate to EXIST, so these tests can no longer
    name a placeholder under the real repo — `leaves/x.md` was never there, and
    asserting on it asserted the shape of a string rather than a Core file. A tmp
    root with a real file makes the fixture honest and hermetic at once: nothing
    here now depends on which leaves the repo happens to carry today."""
    root = tmp_path / "core"
    leaf = root / "memory-global" / "leaves" / "x.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("leaf\n", encoding="utf-8")
    return root, str(leaf)


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


def test_backlog_files_a_core_repo_path(store_file, core_leaf):
    root, core = core_leaf
    rows = sds.upsert_findings([_finding("near-duplicate", core)], store_file, T0)
    filer = _recording_filer()
    filed = sds.route_advisory(rows, "backlog", filer, core_root=root, path=store_file)
    assert [r["path"] for r in filed] == [core]
    assert filer.calls == [core]
    assert sds.load_rows(store_file, T0)[0]["filed_ref"] == "issue-1"


def test_a_refused_filing_is_never_marked_filed(store_file, core_leaf):
    """file-difficulty.py exits 2 on an author machine — every filing here is
    refused. A non-zero exit must leave the row unfiled and in the digest; a
    channel that can silently swallow a finding is a drain in a channel's
    clothes."""
    root, core = core_leaf
    rows = sds.upsert_findings([_finding("near-duplicate", core)], store_file, T0)
    filer = _recording_filer(rc=2, ref="")
    assert sds.route_advisory(rows, "backlog", filer, core_root=root,
                              path=store_file) == []
    assert rows[0]["filed_ref"] is None
    assert len(sds.digest_lines(rows, T0)) == 1
    assert sds.load_rows(store_file, T0)[0]["filed_ref"] is None


@pytest.mark.parametrize("where", ["repo-root", "elsewhere"])
def test_the_tier_filter_does_not_move_with_the_working_directory(where, tmp_path, monkeypatch,
                                                                  core_leaf):
    """ceiling-proximity findings carry repo-RELATIVE paths. Resolving those
    against the process CWD made this guard answer differently for the same
    finding depending on where the caller was started — and a guard whose verdict
    moves with the caller's working directory is not a guard."""
    root, _ = core_leaf
    monkeypatch.chdir(SCRIPTS_DIR.parent if where == "repo-root" else tmp_path)
    rel = "memory-global/leaves/x.md"
    assert sds.inside_core(rel, root) is True
    assert sds.inside_core(str(Path.home() / ".claude-agent" / "projects" / "p" / "x.md"),
                           root) is False
    assert sds.inside_core("", root) is False
    # …and the DEFAULT base is the Core repo, not wherever the process started.
    assert sds.inside_core("CLAUDE.md") is True


def test_the_filter_classifies_the_path_shapes_production_actually_emits(tmp_path, core_leaf):
    """Every other test here feeds a repo-RELATIVE path. Production emits that
    shape for exactly one kind.

    `self-diagnose.py`'s memory scanners report paths relative to their own
    memory root, and `scan` then root-qualifies each one — so the six memory
    kinds arrive ABSOLUTE, through a symlink for Core-backed roots
    (~/.claude-agent/memory-global -> the repo's memory-global/). Only
    ceiling-proximity is repo-relative, and only external producers supply a
    non-path key. A filter guarding a public venue that has never been shown the
    shape its caller sends is untested where it matters, whichever way it
    happens to answer, so all four shapes are pinned here together."""
    core, _ = core_leaf
    # …as production emits it: absolute, and reached through the symlink.
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    (agent_home / "memory-global").symlink_to(core / "memory-global")
    assert sds.inside_core(f"{agent_home}/memory-global/leaves/x.md", core) is True

    # Personal memory: same absolute shape, resolves outside the repo.
    personal = tmp_path / "agent-home" / "projects" / "p" / "memory"
    personal.mkdir(parents=True)
    (personal / "MEMORY.md").write_text("index\n", encoding="utf-8")
    assert sds.inside_core(f"{personal}/MEMORY.md", core) is False

    # ceiling-proximity: repo-relative. An external key: not a path at all.
    assert sds.inside_core("memory-global/leaves/x.md", core) is True
    assert sds.inside_core("subagent-failure-rate/7d", core) is False


def test_a_finding_path_that_is_not_a_path_does_not_clear_the_tier_filter():
    """This filter guards a PUBLIC venue, and `Path.resolve()` is non-strict: any
    string at all resolves under the root and so had `root in parents` answering
    True. The store's second producer supplies exactly such strings —
    policy-scorecard.py's routed flags carry `subagent-failure-rate/7d` in the
    path field — which made internal fleet telemetry read as publishable Core
    content. Requiring the candidate to EXIST makes the guard test the property
    it names rather than the shape of a string."""
    for key in ("subagent-failure-rate/7d", "spend-rate/7d", "correction-rate/7d"):
        assert sds.inside_core(key) is False
    assert sds.inside_core("memory-global/leaves/no-such-leaf-here.md") is False


def test_no_external_kind_row_reaches_the_public_filer(store_file):
    """The end-to-end statement the unit test above only implies. A policy flag is
    ACTIONABLE, so `advisory_open` already excludes it — but that is one condition
    away from a leak, and the condition lives in a different function than the one
    a reader checks when asking "can this reach GitHub?". Pinned here so that
    reclassifying a policy flag as advisory cannot quietly open the path."""
    rows = sds.upsert_findings(
        [_finding(sds.KIND_POLICY_FLAG, "subagent-failure-rate/7d",
                  "rate 6.2% is 8.33x baseline")], store_file, T0)
    filer = _recording_filer()
    assert sds.route_advisory(rows, "backlog", filer, path=store_file) == []
    assert filer.calls == []


def test_a_filed_row_is_never_refiled(store_file, core_leaf):
    root, core = core_leaf
    rows = sds.upsert_findings([_finding("near-duplicate", core)], store_file, T0)
    sds.route_advisory(rows, "backlog", _recording_filer(), core_root=root,
                       path=store_file)
    again = _recording_filer()
    assert sds.route_advisory(sds.load_rows(store_file, T0), "backlog", again,
                              core_root=root, path=store_file) == []
    assert again.calls == []


# --- store: the default filer -----------------------------------------------
#
# Dead while ADVISORY_CHANNEL is 'digest', and never exercised against the real
# file-difficulty.py: on a machine holding Core push rights that script refuses
# every filing with exit 2, and the venue it would otherwise reach is a public
# repo. The argv shape and the timeout are exactly what will be wrong the day the
# switch flips, so they are pinned against a FAKE script.

def _fake_filer_script(tmp_path, body):
    (tmp_path / "file-difficulty.py").write_text(
        "#!/usr/bin/env python3\nimport sys\n" + body + "\n", encoding="utf-8"
    )
    return tmp_path


def test_default_filer_argv_shape_and_success(tmp_path, monkeypatch):
    argv_dump = tmp_path / "argv.json"
    _fake_filer_script(
        tmp_path,
        f"import json; json.dump(sys.argv[1:], open({str(argv_dump)!r}, 'w'))\n"
        "print('noise')\nprint('ISSUE-7')",
    )
    monkeypatch.setattr(sds, "SCRIPT_DIR", tmp_path)

    rc, ref = sds._default_filer(
        {"path": "/core/x.md", "kind": "near-duplicate", "detail": "0.7 Jaccard vs y.md"}
    )
    assert (rc, ref) == (0, "ISSUE-7")

    argv = json.loads(argv_dump.read_text(encoding="utf-8"))
    assert dict(zip(argv[::2], argv[1::2])) == {
        "--target": "/core/x.md",
        "--ground": "self-diagnose near-duplicate: 0.7 Jaccard vs y.md",
        "--severity": "low",
        "--stream": "backlog",
        "--reporter": "self-diagnose",
        "--cost-not-estimable": "machine-detected self-friction; not measured at detection time",
    }


def test_default_filer_reports_a_refusal_rather_than_a_ref(tmp_path, monkeypatch):
    _fake_filer_script(tmp_path, "sys.exit(2)")
    monkeypatch.setattr(sds, "SCRIPT_DIR", tmp_path)
    rc, ref = sds._default_filer({"path": "/core/x.md", "kind": "near-duplicate", "detail": "d"})
    assert rc == 2
    assert ref == ""


def test_default_filer_bounds_the_subprocess(tmp_path, monkeypatch):
    seen = {}

    def _fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sds, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert sds._default_filer({"path": "/core/x.md", "kind": "k", "detail": "d"}) == (1, "")
    assert seen["timeout"] == 60


def _load_file_difficulty():
    spec = importlib.util.spec_from_file_location(
        "file_difficulty_real_gate", SCRIPTS_DIR / "file-difficulty.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_filer_argv_passes_the_real_cost_gate(monkeypatch):
    """The stub-based tests above pin _default_filer's argv shape but never run it
    against the REAL file-difficulty.py, so a new required flag stranding this
    in-repo caller (exactly what happened here) would pass them silently. This
    captures the actual argv _default_filer builds, then drives that argv through
    the real CLI logic (loaded the way test_file_difficulty.py does) with
    authority.is_author patched False and a NullChannel standing in for the
    channel — offline, no network, no real submission — and asserts the cost
    gate accepts the invocation rather than exiting 2."""
    captured = {}

    def _capture_run(argv, **kwargs):
        captured["argv"] = argv

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(subprocess, "run", _capture_run)
    sds._default_filer({"path": "/core/x.md", "kind": "near-duplicate", "detail": "d"})
    real_args = captured["argv"][2:]  # drop [sys.executable, .../file-difficulty.py]

    file_difficulty = _load_file_difficulty()
    monkeypatch.setattr(file_difficulty.authority, "is_author", lambda: False)
    ch = dc.NullChannel()
    dc.register_channel("self-diagnose-real-gate-test", lambda: ch)

    rc = file_difficulty.main(real_args + ["--channel", "self-diagnose-real-gate-test"])
    assert rc == 0
    [r] = ch.pull()
    assert "not estimable" in r.cost_estimate


# --- presentation: one line per condition ------------------------------------

def test_rows_differing_only_in_directory_collapse_to_one_line():
    """The live population replicates one leaf across four project-memory roots:
    four rows naming one condition. Per-path identity is right — each row is
    independently closable — so the collapse is presentation only, and the line
    carries every key."""
    rows = sds.upsert_findings(
        [_finding("orphan-leaf", f"/mem/p{i}/memory/leaves/qprov.md", "not reachable")
         for i in range(4)]
        + [_finding("orphan-leaf", "/mem/other.md", "not reachable")],
        None,
        T0,
    )
    lines = sds.describe_rows(rows, T0)
    assert len(lines) == 2
    collapsed = next(line for line in lines if "qprov.md" in line)
    assert "4 paths" in collapsed
    for row in rows[:4]:
        assert row["key"] in collapsed
    assert "add a pointer line" in collapsed
    # the singleton keeps its full per-row rendering
    assert sds.describe(rows[4], T0) in lines


def test_external_kind_rows_do_not_collapse_on_their_window(store_file):
    """The collapse asks "one condition, several directories", and reads the
    basename to answer. An external producer's path is an opaque key whose
    basename is its WINDOW — "7d" for every policy flag alike — so three unrelated
    flags were one basename apart, held distinct only by their free-text detail.
    Two that worded it the same would have merged into a line naming neither."""
    rows = sds.upsert_findings(
        [_finding(sds.KIND_POLICY_FLAG, "subagent-failure-rate/7d", "same wording"),
         _finding(sds.KIND_POLICY_FLAG, "spend-rate/7d", "same wording"),
         _finding(sds.KIND_POLICY_FLAG, "correction-rate/7d", "same wording")],
        store_file, T0)

    lines = sds.describe_rows(rows, T0)
    assert len(lines) == 3
    assert not any("paths:" in line for line in lines)
    for row in rows:
        assert sds.describe(row, T0) in lines


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
