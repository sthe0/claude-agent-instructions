"""usage-digest.py `pull`: cross-installation aggregator over per-channel sink comments.

NO live network: sink comment lists are injected as fixtures. The load-bearing invariants
under test are: sum across disjoint (installation, period) rows with no double-count, dedup
of re-emitted periods, skipping human chatter / malformed comments, a rated-row-WEIGHTED mean
quality (NOT invocation-weighted), channel segmentation (the public segment excludes an org
segment), and fail-soft on an unreachable sink. Each has a mutation twin that turns RED if the
guard is dropped.

Channel names other than the `github` built-in are synthetic here: an org channel's adapter
lives in the machine-local plugin dir (ADR-0001 B1), so pull's non-builtin branch is exercised
through the injected ``plugin_list_comments`` seam and these assertions hold on any machine.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "usage-digest.py"
_spec = importlib.util.spec_from_file_location("usage_digest", SCRIPT)
usage_digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage_digest)

from difficulty_channel.adapters import BUILTIN_NAMES  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _agg(installation, period, channel, *, inv=0, resolved=0, precedents=0,
         spawns=0, cost=0.0, n_rated=0, mean_q=None):
    return {
        "schema": "usage/v1",
        "period": period,
        "installation_id": installation,
        "channel": channel,
        "n_invocations": inv,
        "n_resolved": resolved,
        "n_quality_rated": n_rated,
        "n_marked_precedents": precedents,
        "mean_quality": mean_q,
        "total_cost_usd": cost,
        "n_spawns": spawns,
    }


def _comment(payload) -> str:
    """A tracker-agnostic comment text carrying a fenced-JSON aggregate."""
    return usage_digest.format_comment(payload)


def _gh_lister(bodies):
    return lambda sink, http=None: [{"body": b} for b in bodies]


def _plugin_lister(texts):
    """A plugin adapter whose comments carry `text`, not github's `body`."""
    return lambda key, http=None: [{"text": t} for t in texts]


# ── extract_aggregate: chatter is skipped, well-formed blocks parse ───────────

def test_extract_aggregate_parses_well_formed_block():
    payload = _agg("inst-a", "2026-W01", "github", inv=5)
    got = usage_digest.extract_aggregate(_comment(payload))
    assert got is not None and got["installation_id"] == "inst-a" and got["n_invocations"] == 5


def test_extract_aggregate_skips_human_chatter():
    assert usage_digest.extract_aggregate("just a human note, no aggregate here") is None


def test_extract_aggregate_skips_marker_without_valid_json():
    text = f"{usage_digest.AGGREGATE_MARKER}\n```json\n{{not valid json,,,}}\n```"
    assert usage_digest.extract_aggregate(text) is None


def test_extract_aggregate_rejects_wrong_schema():
    payload = _agg("inst-a", "2026-W01", "github")
    payload["schema"] = "something-else"
    assert usage_digest.extract_aggregate(_comment(payload)) is None


# ── rollup: sum across disjoint (installation, period) rows, no double-count ───

def test_rollup_sums_three_installations_over_two_disjoint_weeks():
    aggs = [
        _agg("inst-a", "2026-W01", "github", inv=10, resolved=2, spawns=1, cost=1.0),
        _agg("inst-a", "2026-W02", "github", inv=20, resolved=3, spawns=2, cost=2.0),
        _agg("inst-b", "2026-W01", "github", inv=5, resolved=1, spawns=0, cost=0.5),
    ]
    result = usage_digest.rollup(aggs)
    total = result["total"]
    # Disjoint periods -> a plain sum is correct (no overlapping-window double-count).
    assert total["n_invocations"] == 35
    assert total["n_resolved"] == 6
    assert total["n_spawns"] == 3
    assert total["total_cost_usd"] == 3.5
    assert total["n_installations"] == 2  # inst-a and inst-b
    assert result["n_aggregates"] == 3


def test_rollup_dedups_a_reemitted_period_keeping_the_latest():
    # inst-a re-emits 2026-W01 (a corrected count). Latest (list-order) must win, not sum.
    aggs = [
        _agg("inst-a", "2026-W01", "github", inv=10),
        _agg("inst-a", "2026-W01", "github", inv=17),  # re-emit, supersedes
    ]
    result = usage_digest.rollup(aggs)
    # MUTATION: dropping the (installation, period) dedup would count 10+17=27 here.
    assert result["total"]["n_invocations"] == 17
    assert result["n_aggregates"] == 1


def test_rollup_mean_quality_is_rated_row_weighted_not_invocation_weighted():
    # inst-a: huge traffic, few rated rows, low quality. inst-b: little traffic, many rated, high.
    aggs = [
        _agg("inst-a", "2026-W01", "github", inv=1000, n_rated=2, mean_q=1.0),
        _agg("inst-b", "2026-W01", "github", inv=10, n_rated=8, mean_q=5.0),
    ]
    result = usage_digest.rollup(aggs)
    # Rated-row weighted: (1.0*2 + 5.0*8) / (2+8) = 42/10 = 4.2.
    # MUTATION: weighting by n_invocations would give ~1.04, dominated by inst-a. This asserts 4.2.
    assert result["total"]["mean_quality"] == 4.2
    assert result["total"]["n_quality_rated"] == 10


def test_rollup_mean_quality_none_when_no_rated_rows():
    result = usage_digest.rollup([_agg("inst-a", "2026-W01", "github", inv=5)])
    assert result["total"]["mean_quality"] is None


# ── channel segmentation: the public segment excludes an org segment ──────────

def test_rollup_segments_by_channel_public_excludes_an_org_channel():
    aggs = [
        _agg("inst-gh", "2026-W01", "github", inv=10),
        _agg("inst-org", "2026-W01", "orgchan", inv=7),
    ]
    result = usage_digest.rollup(aggs)
    seg = result["by_segment"]
    # MUTATION: collapsing all rows into one segment would put 17 in each. Segments must be disjoint.
    assert seg[usage_digest.PUBLIC_SEGMENT]["n_invocations"] == 10
    assert seg["orgchan"]["n_invocations"] == 7
    assert seg[usage_digest.PUBLIC_SEGMENT]["n_installations"] == 1
    assert result["total"]["n_invocations"] == 17


# ── pull: end-to-end over injected sink listers, both channels ────────────────

def test_pull_reads_both_channels_and_sums():
    gh_bodies = [
        "human chatter — ignore me",
        _comment(_agg("inst-gh", "2026-W01", "github", inv=10, resolved=2)),
    ]
    org_texts = [
        _comment(_agg("inst-org", "2026-W01", "orgchan", inv=7, resolved=1)),
    ]
    result = usage_digest.pull(
        sinks={"github": "org/repo#1", "orgchan": "USAGE-1"},
        github_list_comments=_gh_lister(gh_bodies),
        plugin_list_comments=_plugin_lister(org_texts),
    )
    # Chatter skipped; both real aggregates summed.
    assert result["n_aggregates"] == 2
    assert result["total"]["n_invocations"] == 17
    assert result["by_segment"][usage_digest.PUBLIC_SEGMENT]["n_invocations"] == 10
    assert result["by_segment"]["orgchan"]["n_invocations"] == 7


def test_pull_skips_unconfigured_sink():
    result = usage_digest.pull(
        sinks={"github": "org/repo#1", "orgchan": ""},  # orgchan unconfigured
        github_list_comments=_gh_lister([_comment(_agg("inst-gh", "2026-W01", "github", inv=4))]),
        plugin_list_comments=_plugin_lister(["should never be read"]),
    )
    assert result["total"]["n_invocations"] == 4
    assert "orgchan" not in result["by_segment"]


@pytest.mark.parametrize("channel", sorted(BUILTIN_NAMES))
def test_pull_reads_every_builtin_sink_through_the_builtin_lister(channel):
    """Sink listing dispatches on BUILTIN_NAMES membership, not one built-in's literal name:
    routing a built-in to the plugin loader yields None, whose AttributeError the fail-soft
    handler swallows into an empty (silently wrong) rollup."""
    def must_not_be_called(sink, http=None):
        raise AssertionError("a built-in channel must not take the plugin path")

    result = usage_digest.pull(
        sinks={channel: "org/repo#1"},
        github_list_comments=_gh_lister(
            [_comment(_agg("inst-gh", "2026-W01", channel, inv=6))]
        ),
        plugin_list_comments=must_not_be_called,
        log=lambda m: None,
    )
    assert result["total"]["n_invocations"] == 6
    assert result["by_segment"][usage_digest.PUBLIC_SEGMENT]["n_invocations"] == 6


@pytest.mark.parametrize("channel", sorted(BUILTIN_NAMES))
def test_resolve_sinks_seeds_every_builtin_not_one_literal_name(channel, monkeypatch):
    """Sink seeding dispatches on BUILTIN_NAMES, not one built-in's literal name: seeding
    only `github` leaves the other built-in absent from the sink map, so a `pull` silently
    never reads it. MUTATION: reverting to `{"github": USAGE_SINK_GITHUB}` turns this RED."""
    monkeypatch.setattr(usage_digest, "USAGE_SINK_GITHUB", "org/repo#1")
    args = usage_digest.build_arg_parser().parse_args(["pull"])
    assert usage_digest._resolve_sinks(args, {})[channel] == "org/repo#1"


def test_pull_fail_soft_on_unreachable_sink():
    def boom(sink, http=None):
        raise RuntimeError("network down")

    logs = []
    result = usage_digest.pull(
        sinks={"github": "org/repo#1", "orgchan": "USAGE-1"},
        github_list_comments=boom,  # github down
        plugin_list_comments=_plugin_lister(
            [_comment(_agg("inst-org", "2026-W01", "orgchan", inv=9))]
        ),
        log=logs.append,
    )
    # A down channel degrades to the other channel's rollup, never a crash.
    assert result["total"]["n_invocations"] == 9
    assert usage_digest.PUBLIC_SEGMENT not in result["by_segment"]
    assert any("failed" in m for m in logs)


# ── CLI: `pull` keeps its "always returns 0" promise ──────────────────────────

@pytest.mark.parametrize("kind", ["non-utf8", "directory"])
def test_cli_pull_exits_zero_on_an_unreadable_identity(kind, tmp_path, capsys):
    """cmd_pull's docstring promises a fail-soft exit 0. An identity path that cannot be
    decoded — undecodable bytes, or a directory (which `exists()` accepts) — must degrade
    to a message, not a traceback. MUTATION: dropping cmd_pull's try/except raises
    UnicodeDecodeError / IsADirectoryError here and the exit code becomes 1.
    Offline: the read fails before any sink is contacted."""
    if kind == "non-utf8":
        identity = tmp_path / "agent-identity.local"
        identity.write_bytes(b"difficulty_channel=\xff\xfe\n")
    else:
        identity = tmp_path / "identity-as-a-directory"
        identity.mkdir()
    rc = usage_digest.main(["pull", "--identity", str(identity)])
    assert rc == 0
    assert "pull skipped" in capsys.readouterr().out
