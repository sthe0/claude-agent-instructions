"""The price table is the model REGISTRY; the ledger says which table priced it.

Two drifts this covers:

  (1) Model-keyed structures were written in parallel with the price table —
      a hand-written MODEL_KEYS tuple, three literal spawn buckets in the
      emitted row, three more in the aggregate, three more in the printed line.
      A model released into that shape appears in some of them and not others,
      and its tokens land in the opus bucket. Everything model-keyed now derives
      from `cost_report.PRICING_USD_PER_MTOK`, so registering a model is one
      price row.

  (2) Rates change under a ledger that has no idea. Rows priced at the old table
      and rows priced at the new one sum into one dollar figure that describes
      no table at all. Each row now carries `priced_by` (a content hash of the
      table that priced it), the scorecard warns while any row is stale, and
      `reprice` re-prices in place — in place because a manual `quality_rating`
      exists nowhere but this file, so regenerating the ledger would buy
      identical dollars at the cost of every rating ever attached.

Cases (a)-(g) cover the registry, (h) reprice, (i) the staleness warning.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "policy-scorecard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("policy_scorecard_buckets", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ps(monkeypatch, tmp_path):
    """A fresh module instance with every real-machine path redirected into
    tmp_path — no test may read or rewrite this machine's actual ledger."""
    mod = _load_module()
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(mod, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
    return mod


def _ts(days_ago: float) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _row(ps, session_id: str, *, model_tokens: dict, agent_spawns: dict,
         cost_usd: float = 0.0, cache_read_usd: float = 0.0,
         priced_by: str | None = None, quality_rating=None, days_ago: float = 1) -> dict:
    """A stored ledger row. `model_tokens`/`agent_spawns` are passed in whole so
    a test can hand in a row written under an older shape."""
    row = {
        "session_id": session_id,
        "project": "proj",
        "date": _ts(days_ago)[:10],
        "first_ts": _ts(days_ago),
        "last_ts": _ts(days_ago),
        "instructions_head": None,
        "mtime": 0.0,
        "model_tokens": model_tokens,
        "cost_usd": cost_usd,
        "cache_read_usd": cache_read_usd,
        "main_read_bash": 0,
        "agent_spawns": agent_spawns,
        "missed_delegation_clusters": 0,
        "attention": {"askq": 0, "prompts": 1, "interrupts": 0, "corrections": 0},
        "user_signals": {"n_user_corrections": 0, "n_user_questions": 0,
                         "n_freetext_askuser_answers": 0, "n_interrupts": 0},
        "effectiveness": {"resolution_confirmed": 0, "replans": 0, "overcome_difficulty": 0,
                          "subagent_failures": 0, "rework_edits": 0},
        "quality_rating": quality_rating,
        "quality_note": None,
    }
    if priced_by is not None:
        row["priced_by"] = priced_by
    return row


def _tokens(ps, **per_model) -> dict:
    tok = ps._empty_model_tokens()
    for k, fields in per_model.items():
        tok[k].update(fields)
    return tok


def _spawns(ps, **per_model) -> dict:
    d = {"total": sum(per_model.values()), "no_explicit_model": 0, "inherit_opus": 0}
    d.update({k: per_model.get(k, 0) for k in ps.MODEL_KEYS})
    return d


# ------------------------------------------------------------ (a)-(c) registry

def test_a_model_keys_derive_from_the_price_table(ps):
    assert ps.MODEL_KEYS == tuple(ps.PRICING)
    assert "fable" in ps.MODEL_KEYS, "fable is a price row, so it is a bucket"


def test_b_every_price_row_gets_a_full_token_bucket(ps):
    tok = ps._empty_model_tokens()
    assert set(tok) == set(ps.PRICING)
    for k in ps.MODEL_KEYS:
        assert set(tok[k]) == {short for short, _ in ps.USAGE_FIELDS}


def test_c_cache_read_cost_prices_each_bucket_at_its_own_rate(ps):
    tok = _tokens(ps, fable={"cache_read": 1_000_000}, haiku={"cache_read": 1_000_000})
    expected = (ps.PRICING["fable"]["cache_read"] + ps.PRICING["haiku"]["cache_read"])
    assert ps._cache_read_cost(tok) == pytest.approx(expected)
    # fable's cache_read must be its own rate, not opus's fallback
    assert ps.PRICING["fable"]["cache_read"] != ps.PRICING["opus"]["cache_read"]


# ------------------------------------------- (d)-(e) aggregate over stored rows

def test_d_aggregate_tolerates_a_row_written_before_a_model_existed(ps):
    """The shape every row on disk was written in: three token buckets, three
    spawn buckets. Reading it must not raise, and the absent model reads zero."""
    old = _row(
        ps, "s-old",
        model_tokens={k: {"in": 5, "out": 5, "cache_read": 5, "cache_create": 5}
                      for k in ("opus", "sonnet", "haiku")},
        agent_spawns={"total": 2, "opus": 2, "sonnet": 0, "haiku": 0,
                      "no_explicit_model": 0, "inherit_opus": 0},
    )
    a = ps._aggregate([old])
    assert a["spawn_opus"] == 2
    assert a["spawn_fable"] == 0
    assert a["model_tokens"]["fable"] == {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0}
    assert a["model_tokens"]["opus"]["in"] == 5


def test_e_aggregate_sums_a_spawn_bucket_for_every_model(ps):
    rows = [
        _row(ps, "s1", model_tokens=_tokens(ps), agent_spawns=_spawns(ps, opus=1, fable=2)),
        _row(ps, "s2", model_tokens=_tokens(ps), agent_spawns=_spawns(ps, fable=1, haiku=3)),
    ]
    a = ps._aggregate(rows)
    assert a["spawn_fable"] == 3
    assert a["spawn_haiku"] == 3
    assert a["spawn_opus"] == 1
    assert a["spawns_total"] == 7
    for k in ps.MODEL_KEYS:
        assert f"spawn_{k}" in a


# --------------------------------------------------- (f)-(g) registry contract

def test_f_a_new_price_row_is_the_whole_registration(ps, monkeypatch):
    """The claim under test: adding a model is ONE row. A fictitious model is
    used so the check survives the next real release — patch the table, re-derive
    exactly as the module does, and the buckets/counts/report follow."""
    patched = dict(ps.PRICING)
    patched["zephyr"] = {"input": 2.0, "output": 9.0, "cache_write": 2.5, "cache_read": 0.2}
    monkeypatch.setattr(ps, "PRICING", patched)
    monkeypatch.setattr(ps, "MODEL_KEYS", tuple(patched))

    assert "zephyr" in ps._empty_model_tokens()
    tok = _tokens(ps, zephyr={"cache_read": 1_000_000})
    assert ps._cache_read_cost(tok) == pytest.approx(0.2)
    a = ps._aggregate([_row(ps, "s1", model_tokens=tok, agent_spawns=_spawns(ps, zephyr=4))])
    assert a["spawn_zephyr"] == 4
    report = ps.scorecard({"s1": _row(ps, "s1", model_tokens=tok,
                                      agent_spawns=_spawns(ps, zephyr=4))}, 7, None)
    assert "zephyr 4" in report


def test_g_no_price_key_is_a_substring_of_another(ps):
    """`_rates_for`/`_model_key` match a model id by substring, so an "opus"-in-
    "opus-mini" pair would route a model's tokens to the wrong bucket silently."""
    keys = list(ps.PRICING)
    for a in keys:
        for b in keys:
            assert a == b or a not in b, f"price key {a!r} is a substring of {b!r}"


def test_g_no_price_key_collides_with_a_reserved_row_field(ps):
    """The per-model buckets are splatted alongside fixed fields in the emitted
    `agent_spawns` dict, so a price key sharing one of their names would
    overwrite a counter with a spawn count and never say so."""
    reserved = {"total", "no_explicit_model", "inherit_opus"}
    assert not (set(ps.PRICING) & reserved)


# ---------------------------------------------------------------- (h) reprice

def test_h_reprice_rewrites_dollars_in_place_and_keeps_everything_else(ps, tmp_path):
    stale = _row(
        ps, "s-stale",
        # the shape rows were written in before fable joined the table
        model_tokens={"opus": {"in": 1_000_000, "out": 0, "cache_read": 200_000, "cache_create": 0},
                      "sonnet": {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0},
                      "haiku": {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0}},
        agent_spawns={"total": 1, "opus": 1, "sonnet": 0, "haiku": 0,
                      "no_explicit_model": 0, "inherit_opus": 0},
        cost_usd=15.3, cache_read_usd=0.3,  # priced at the pre-refresh opus rates
        quality_rating=4,
    )
    ps.write_ledger({"s-stale": stale})

    out = ps.reprice()
    rows = ps.load_ledger()
    r = rows["s-stale"]

    p = ps.PRICING["opus"]
    assert r["cost_usd"] == pytest.approx(
        (1_000_000 * p["input"] + 200_000 * p["cache_read"]) / 1_000_000)
    assert r["cache_read_usd"] == pytest.approx(200_000 * p["cache_read"] / 1_000_000)
    assert r["cost_usd"] != 15.3, "the point of repricing is that the dollars move"
    assert r["priced_by"] == ps.PRICING_SHA
    # the fields a rebuild would have destroyed
    assert r["quality_rating"] == 4
    assert r["agent_spawns"]["opus"] == 1
    assert r["model_tokens"]["opus"]["in"] == 1_000_000
    assert "repriced 1" in out
    backups = list(tmp_path.glob("ledger.jsonl.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8").strip())["cost_usd"] == 15.3


def test_h_reprice_is_idempotent(ps):
    ps.write_ledger({"s1": _row(ps, "s1", model_tokens=_tokens(ps, opus={"in": 1_000}),
                                agent_spawns=_spawns(ps))})
    ps.reprice()
    first = ps.load_ledger()
    assert "repriced 0" in ps.reprice()
    assert ps.load_ledger() == first


def test_h_reprice_dry_run_writes_nothing(ps, tmp_path):
    ps.write_ledger({"s1": _row(ps, "s1", model_tokens=_tokens(ps, opus={"in": 1_000_000}),
                                agent_spawns=_spawns(ps), cost_usd=15.0)})
    before = ps.LEDGER.read_text(encoding="utf-8")
    out = ps.reprice(dry_run=True)
    assert "dry-run" in out
    assert ps.LEDGER.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("ledger.jsonl.bak-*")) == []


def test_h_reprice_on_an_absent_ledger_is_a_no_op(ps):
    assert not ps.LEDGER.exists()
    assert "nothing to reprice" in ps.reprice()
    assert not ps.LEDGER.exists()


# ----------------------------------------------------- (i) staleness warning

def test_i_scorecard_warns_while_any_row_is_priced_by_an_older_table(ps):
    rows = {
        "s1": _row(ps, "s1", model_tokens=_tokens(ps), agent_spawns=_spawns(ps),
                   priced_by="deadbeefcafe"),
        "s2": _row(ps, "s2", model_tokens=_tokens(ps), agent_spawns=_spawns(ps),
                   priced_by=ps.PRICING_SHA),
    }
    report = ps.scorecard(rows, 7, None)
    assert "**1** ledger row(s) priced by an older rate table" in report
    assert "reprice" in report

    for r in rows.values():
        r["priced_by"] = ps.PRICING_SHA
    assert "older rate table" not in ps.scorecard(rows, 7, None)
