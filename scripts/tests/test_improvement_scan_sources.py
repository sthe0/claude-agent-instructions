"""Tests for `improvement-scan.py sources`: the rules in improvement_scan_sources.py, the
reading in improvement_scan_fetch.py and the wiring in improvement-scan.py.

No test may reach the network: the autouse fixture below makes every real connection
attempt raise, and every fetch is an injected fake. Ideas are judged by the model in
production; here the model's closed-vocabulary JSON is written by hand.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import improvement_scan_fetch as fetcher  # noqa: E402
import improvement_scan_sources as s  # noqa: E402
import self_diagnose_store as sds  # noqa: E402
from ast_purity import impure_names  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("improvement_scan_for_sources", SCRIPTS_DIR / "improvement-scan.py")
scan = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = scan
_SPEC.loader.exec_module(scan)

REAL_SOURCES = SCRIPTS_DIR / "external-sources.toml"
ALPHA_URL = "https://alpha.example/feed.xml"
BETA_URL = "https://beta.example/feed.xml"
TG_URL = "https://t.me/s/example_channel"
CANDIDATE = "https://candidate.example/blog"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- builders -------------------------------------------------------------------

def rss(*entries: "tuple[str, str]") -> bytes:
    """entries: (slug, title)."""
    items = "".join(
        f"<item><title>{title}</title><link>https://blog.example/{slug}</link>"
        f"<guid>{slug}</guid><pubDate>Mon, 0{n + 1} Sep 2026 10:00:00 GMT</pubDate>"
        f"<description>about {slug}</description></item>"
        for n, (slug, title) in enumerate(entries)
    )
    return f"<rss version='2.0'><channel><title>t</title>{items}</channel></rss>".encode()


def telegram_page(*posts: str) -> bytes:
    body = "".join(
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="c/{n}">'
        f'<a class="tgme_widget_message_date" href="https://t.me/example_channel/{n}">'
        f'<time datetime="2026-09-0{n}T10:00:00+00:00">d</time></a>'
        f'<div class="tgme_widget_message_text">{text}</div></div></div>'
        for n, text in enumerate(posts, start=1)
    )
    return f"<html><body>{body}</body></html>".encode()


class Web:
    """A fake `fetch`: route prefixes to bytes (success) or a string (error)."""

    def __init__(self) -> None:
        self.routes: "dict[str, bytes | str]" = {}
        self.calls: "list[str]" = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append(url)
        for prefix, result in self.routes.items():
            if url.startswith(prefix):
                return (result, None) if isinstance(result, bytes) else (None, result)
        return None, "HTTP 404"


@pytest.fixture
def web(monkeypatch):
    fake = Web()
    monkeypatch.setattr(fetcher, "fetch", fake)
    return fake


def sources_text(sources=None, **meta) -> str:
    fields = {
        "armed": "2026-10-20", "review_after_weeks": 8, "enabled": "true", "max_items_per_run": 10,
        "queue_cap": 100, "max_external_calls_per_run": 30, **meta,
    }
    sources = sources if sources is not None else [
        ("alpha", ALPHA_URL, "rss", True), ("beta", BETA_URL, "rss", True),
    ]
    head = "[meta]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items())
    body = "".join(
        f'\n[[source]]\nid = "{sid}"\nname = "{sid} name"\nurl = "{url}"\nkind = "{kind}"\n'
        f'enabled = {str(on).lower()}\nreason = "because {sid}"\n'
        for sid, url, kind, on in sources
    )
    return head + body


class World:
    def __init__(self, tmp_path: Path, capsys) -> None:
        self.tmp = tmp_path
        self.capsys = capsys
        self.sources = tmp_path / "sources.toml"
        self.sources.write_text(sources_text(), encoding="utf-8")
        self.state = tmp_path / "state"
        self.store = tmp_path / "store.json"
        self.n = 0

    def run(self, *argv: str) -> "tuple[int, str, str]":
        code = scan.main(list(argv))
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def common(self) -> "list[str]":
        return ["--sources", str(self.sources), "--state-dir", str(self.state), "--store", str(self.store)]

    def write(self, name: str, payload) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def phase_a(self, *extra: str) -> "tuple[int, dict, str]":
        self.n += 1
        target = self.tmp / f"items-{self.n}.json"
        code, out, _ = self.run("sources", *self.common(), "--emit-items", str(target), *extra)
        return code, (json.loads(target.read_text()) if target.exists() else {}), out

    def phase_b(self, claims: list) -> "tuple[int, str, str]":
        return self.run("sources", *self.common(), "--claims", self.write("claims.json", {"claims": claims}))

    def judge_all(self, **over) -> "tuple[dict, list]":
        """Phase A, then a full claim for every emitted item."""
        _, out, _ = self.phase_a()
        claims = [claim(i["entry_id"], title=i["title"], **over) for i in out["items"]]
        code, _, err = self.phase_b(claims)
        assert code == 0, err
        return out, claims

    def add_difficulty(self, kind: str, signal: str, source_ref: str = "", usd: "float | None" = None) -> str:
        measured = usd is not None
        finding = scan.Finding(
            kind=kind, signal=signal, title=f"difficulty {signal}", functional_ground=f"ground {signal}",
            evidence=("ev",), cost_signal=scan.CostSignal(usd_per_week=usd, basis="b", measured=measured),
            source_ref=source_ref, recommended_next_step="planner",
        )
        scan.store_findings([finding], kinds=frozenset({kind}), store_path=self.store)
        return sds.finding_key(kind, signal)

    def report(self, *extra: str) -> str:
        code, out, err = self.run(
            "report", "--store", str(self.store), "--state-dir", str(self.state),
            "--sources", str(self.sources), "--board", str(self.tmp / "no-board.json"), *extra,
        )
        assert code == 0, err
        return out

    def state_files(self) -> "list[str]":
        return sorted(p.name for p in self.state.glob("*")) if self.state.exists() else []


@pytest.fixture
def world(tmp_path, capsys, web):
    web.routes[ALPHA_URL] = rss(("a1", "Alpha one"), ("a2", "Alpha two"))
    web.routes[BETA_URL] = rss(("b1", "Beta one"))
    return World(tmp_path, capsys)


def claim(entry_id: str, **over) -> dict:
    base = {
        "entry_id": entry_id, "title": "An idea", "difficulty_removed": "a recurring difficulty",
        "mechanism": "do the thing", "evidence_level": "measured", "evidence_basis": "ran it for a week",
        "addresses": [], "cited_sources": ["https://paper.example/p1"], "candidate_sources": [],
    }
    return {**base, **over}


def section(report: str, heading: str) -> str:
    start = report.index(heading + "\n")
    rest = report[start + len(heading):]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


# --- socket guard -----------------------------------------------------------------

def test_socket_guard_blocks_create_connection():
    with pytest.raises(AssertionError):
        socket.create_connection(("example.com", 80))


def test_socket_guard_blocks_socket_connect():
    with pytest.raises(AssertionError):
        socket.socket().connect(("example.com", 80))


def test_socket_guard_real_fetch_fails_as_a_value_not_an_exception():
    body, error = fetcher.fetch("https://alpha.example/feed.xml", opener=lambda req, timeout: (_ for _ in ()).throw(
        urllib.error.URLError("no route")))
    assert body is None and "URLError" in error


# --- cursor and queue ---------------------------------------------------------------

def test_cursor_entries_are_marked_only_after_phase_b(world):
    _, first, _ = world.phase_a()
    assert len(first["items"]) == 3
    _, again, _ = world.phase_a()
    assert [i["entry_id"] for i in again["items"]] == [i["entry_id"] for i in first["items"]]
    world.phase_b([{"entry_id": i["entry_id"], "skip": "off topic"} for i in first["items"]])
    _, after, _ = world.phase_a()
    assert after["items"] == []


def test_cursor_a_skip_is_remembered_and_never_cached(world):
    _, out, _ = world.phase_a()
    world.phase_b([{"entry_id": i["entry_id"], "skip": "off topic"} for i in out["items"]])
    assert json.loads((world.state / "claims.json").read_text()) == {}
    assert sds.advisory_open(sds.load_rows(world.store)) == []


def test_cursor_unit_new_until_marked():
    cursor = s.SeenCursor()
    assert cursor.is_new("alpha", "e1")
    cursor.mark("alpha", "e1")
    assert not cursor.is_new("alpha", "e1") and cursor.is_new("beta", "e1")


def test_queue_overflow_drops_oldest_and_prints_the_count(world):
    world.sources.write_text(sources_text(queue_cap=2), encoding="utf-8")
    code, out, printed = world.phase_a()
    assert code == 0
    assert "dropped the 1 oldest" in printed
    assert out["queued"] == 2
    seen = json.loads((world.state / "seen.json").read_text())
    assert sum(len(v) for v in seen.values()) == 1


def test_queue_is_fifo_and_capped_unit():
    cursor = s.SeenCursor()
    entries = [{"entry_id": f"e{n}", "source_id": "alpha", "published": f"2026-09-0{n}"} for n in range(1, 5)]
    queue, new, dropped = s.enqueue([], cursor, {"alpha": entries}, cap=3)
    assert [e["entry_id"] for e in queue] == ["e2", "e3", "e4"]
    assert (new, dropped) == ({"alpha": 4}, 1)
    assert not cursor.is_new("alpha", "e1")


def test_queue_items_stay_until_phase_b(world):
    world.phase_a()
    queued = json.loads((world.state / "queue.json").read_text())
    assert len(queued) == 3
    world.judge_all()
    assert json.loads((world.state / "queue.json").read_text()) == []


def test_queue_not_requeued_when_already_queued(world):
    world.phase_a()
    world.phase_a()
    assert len(json.loads((world.state / "queue.json").read_text())) == 3


# --- bounds -----------------------------------------------------------------------

def test_bound_items_per_run_comes_from_the_sources_file(world):
    world.sources.write_text(sources_text(max_items_per_run=1), encoding="utf-8")
    _, out, _ = world.phase_a()
    assert len(out["items"]) == 1 and out["queued"] == 3


def test_bound_selection_is_round_robin_across_sources():
    queue = [
        {"entry_id": "a1", "source_id": "alpha"}, {"entry_id": "a2", "source_id": "alpha"},
        {"entry_id": "b1", "source_id": "beta"},
    ]
    chosen = s.select_items(queue, ["alpha", "beta"], 2)
    assert [e["entry_id"] for e in chosen] == ["a1", "b1"]


def test_cli_max_items_lowers_the_bound(world):
    _, out, _ = world.phase_a("--max-items", "2")
    assert len(out["items"]) == 2


def test_cli_max_items_cannot_exceed_the_file_bound(world):
    world.sources.write_text(sources_text(max_items_per_run=1), encoding="utf-8")
    _, out, _ = world.phase_a("--max-items", "50")
    assert len(out["items"]) == 1


# --- statuses and failures ------------------------------------------------------------

def test_status_vocab_each_source_is_ok_or_error(world, web):
    web.routes[BETA_URL] = "HTTP 503"
    _, out, _ = world.phase_a()
    assert {row["status"] for row in out["sources"]} == {"ok", "error"}
    assert {row["status"] for row in out["sources"]} <= set(s.STATUS_VOCAB)


def test_status_vocab_dossier_rejects_a_status_outside_the_vocabulary():
    desc = {"statements": [{"text": "t", "url": "https://x.example/a"}]}
    signals = {name: {"status": "ok"} for name in s.DOSSIER_SIGNALS if name != "descriptive"}
    signals["hn"] = {"status": "maybe"}
    with pytest.raises(s.SourcesError):
        s.build_dossier("subject", desc, signals, "2026-10-20")


def test_multi_source_error_does_not_stop_the_other_sources(world, web):
    web.routes[ALPHA_URL] = "timeout"
    code, out, printed = world.phase_a()
    assert code == 0
    assert {i["source_id"] for i in out["items"]} == {"beta"}
    assert "source alpha: error, timeout" in printed


def test_multi_source_error_a_non_feed_body_is_that_sources_error(world, web):
    web.routes[ALPHA_URL] = b"<html><body>not a feed</body></html>"
    _, out, _ = world.phase_a()
    alpha = next(row for row in out["sources"] if row["id"] == "alpha")
    assert alpha["status"] == "error" and "not a feed" in alpha["error"]


def test_multi_source_error_disabled_sources_are_not_read(world, web):
    world.sources.write_text(
        sources_text([("alpha", ALPHA_URL, "rss", True), ("beta", BETA_URL, "rss", False)]), encoding="utf-8")
    world.phase_a()
    assert BETA_URL not in web.calls


def test_multi_source_error_disabled_producer_reads_nothing(world, web):
    world.sources.write_text(sources_text(enabled="false"), encoding="utf-8")
    code, _, printed = world.phase_a()
    assert code == 0 and "disabled" in printed and web.calls == []


# --- the closed vocabulary ---------------------------------------------------------------

def _items(world):
    _, out, _ = world.phase_a()
    return out["items"]


def test_vocab_unknown_evidence_level_exits_2_and_writes_nothing(world):
    items = _items(world)
    before = world.state_files()
    code, _, err = world.phase_b([claim(items[0]["entry_id"], evidence_level="rumour"),
                                  *[{"entry_id": i["entry_id"], "skip": "x"} for i in items[1:]]])
    assert code == 2 and "evidence_level" in err
    assert world.state_files() == before
    assert not world.store.exists()


@pytest.mark.parametrize("level", s.EVIDENCE_LEVELS)
def test_vocab_every_evidence_level_is_accepted(world, level):
    items = _items(world)
    claims = [claim(i["entry_id"], evidence_level=level) for i in items]
    assert world.phase_b(claims)[0] == 0


def test_vocab_unknown_claim_key_is_refused(world):
    items = _items(world)
    claims = [claim(i["entry_id"], extra="x") for i in items]
    code, _, err = world.phase_b(claims)
    assert code == 2 and "unknown keys" in err


def test_vocab_cited_source_must_be_http(world):
    items = _items(world)
    claims = [claim(i["entry_id"], cited_sources=["ftp://x.example/a"]) for i in items]
    assert world.phase_b(claims)[0] == 2


def test_vocab_every_emitted_entry_needs_one_disposition(world):
    items = _items(world)
    code, _, err = world.phase_b([claim(items[0]["entry_id"])])
    assert code == 2 and "no disposition" in err


def test_vocab_an_entry_that_was_not_emitted_is_refused(world):
    items = _items(world)
    claims = [claim(i["entry_id"]) for i in items] + [claim("stranger")]
    code, _, err = world.phase_b(claims)
    assert code == 2 and "stranger" in err


def test_vocab_two_dispositions_for_one_entry_are_refused(world):
    items = _items(world)
    claims = [claim(i["entry_id"]) for i in items] + [{"entry_id": items[0]["entry_id"], "skip": "again"}]
    assert world.phase_b(claims)[0] == 2


def test_vocab_skip_needs_a_reason(world):
    items = _items(world)
    claims = [{"entry_id": i["entry_id"], "skip": " "} for i in items]
    assert world.phase_b(claims)[0] == 2


def test_vocab_phase_b_without_phase_a_is_refused(world):
    code, _, err = world.phase_b([claim("e1")])
    assert code == 2 and "--emit-items" in err


def test_vocab_dossier_signal_names_and_evidence_levels_are_exactly_the_format():
    assert s.DOSSIER_SIGNALS == {
        "descriptive": "descriptive", "feed": "feed", "local_refs": "local", "hn": "external",
        "github": "external", "openalex": "external", "evidence_profile": "evidence",
    }
    assert s.EVIDENCE_LEVELS == ("measured", "anecdote", "opinion")


# --- rejoin --------------------------------------------------------------------------------

def test_rejoin_lists_a_difficulty_that_appeared_after_the_claim(world):
    world.judge_all()
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "late", usd=3.0)
    _, out, _ = world.phase_a()
    assert out["items"] == []
    assert {r["entry_id"] for r in out["rejoin"]} == set(json.loads((world.state / "claims.json").read_text()))
    assert key in {d["ref"] for r in out["rejoin"] for d in r["open_difficulties"]}


def test_rejoin_amendment_adds_the_address_and_marks_it_considered(world):
    world.judge_all()
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "late", usd=3.0)
    _, out, _ = world.phase_a()
    target = out["rejoin"][0]["entry_id"]
    assert world.phase_b([{"entry_id": target, "addresses": [key]}])[0] == 0
    cached = json.loads((world.state / "claims.json").read_text())
    assert key in cached[target]["addresses"]
    _, after, _ = world.phase_a()
    assert target not in {r["entry_id"] for r in after["rejoin"]}


def test_rejoin_amendment_has_exactly_two_keys(world):
    world.judge_all()
    world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "late", usd=3.0)
    _, out, _ = world.phase_a()
    target = out["rejoin"][0]["entry_id"]
    code, _, err = world.phase_b([{"entry_id": target, "addresses": ["x"], "title": "rewritten"}])
    assert code == 2 and "rejoin amendment" in err


def test_rejoin_a_declined_amendment_still_marks_the_difficulty_considered(world):
    world.judge_all()
    world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "late", usd=3.0)
    world.phase_a()
    assert world.phase_b([])[0] == 0
    _, after, _ = world.phase_a()
    assert after["rejoin"] == []


# --- the join --------------------------------------------------------------------------------

def test_join_validation_unresolved_address_goes_to_the_unmatched_lane():
    matched, unmatched = s.split_by_join([{"addresses": ["nope"], "first_seen": "1"}], {"real"})
    assert matched == [] and unmatched[0]["unresolved"] == ["nope"]


def test_join_validation_one_resolving_address_is_enough():
    matched, _ = s.split_by_join([{"addresses": ["nope", "real"]}], {"real"})
    assert matched[0]["resolved"] == ["real"] and matched[0]["unresolved"] == ["nope"]


def test_join_validation_matches_whole_refs_never_substrings():
    matched, unmatched = s.split_by_join([{"addresses": ["ref-1"], "first_seen": "1"}], {"ref-10"})
    assert matched == [] and len(unmatched) == 1


def test_join_validation_resolves_telemetry_keys_and_backlog_refs(world):
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    world.add_difficulty(sds.KIND_BACKLOG_ITEM, "b1", source_ref="BACKLOG-7")
    world.judge_all(addresses=[key, "BACKLOG-7"])
    matched = section(world.report(), "## External ideas addressing our difficulties")
    assert f"`{key}`" in matched and "`BACKLOG-7`" in matched


# --- ranking ------------------------------------------------------------------------------------

def _ranking_contract(rank_fn) -> None:
    ideas = [
        {"title": "op", "evidence_level": "opinion", "addresses": ["d1"], "first_seen": "1"},
        {"title": "an-late", "evidence_level": "anecdote", "addresses": ["d1"], "first_seen": "9"},
        {"title": "an-low", "evidence_level": "anecdote", "addresses": ["d2"], "first_seen": "1"},
        {"title": "an-early", "evidence_level": "anecdote", "addresses": ["d1"], "first_seen": "2"},
        {"title": "me", "evidence_level": "measured", "addresses": ["d2"], "first_seen": "5"},
    ]
    ranked = [i["title"] for i in rank_fn(ideas, {"d1": 0, "d2": 1})]
    assert ranked == ["me", "an-early", "an-late", "an-low", "op"]


def test_rank_orders_by_evidence_then_difficulty_rank_then_first_seen():
    _ranking_contract(s.rank_ideas)


def test_rank_uses_the_best_ranked_address():
    ideas = [
        {"title": "x", "evidence_level": "measured", "addresses": ["d2", "d1"], "first_seen": "1"},
        {"title": "y", "evidence_level": "measured", "addresses": ["d2"], "first_seen": "1"},
    ]
    assert [i["title"] for i in s.rank_ideas(ideas, {"d1": 0, "d2": 1})] == ["x", "y"]


def test_rank_mutant_is_caught():
    def by_title(ideas, difficulty_rank):
        return sorted(ideas, key=lambda i: i["title"])

    with pytest.raises(AssertionError):
        _ranking_contract(by_title)


def test_rank_report_lists_measured_before_opinion_through_the_module_attribute(world):
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    _, out, _ = world.phase_a()
    first, second, third = out["items"]
    levels = {first["entry_id"]: "opinion", second["entry_id"]: "measured", third["entry_id"]: "anecdote"}
    world.phase_b([claim(e, title=f"{lvl} idea", evidence_level=lvl, addresses=[key]) for e, lvl in levels.items()])
    text = section(world.report(), "## External ideas addressing our difficulties")
    assert [m.group(1) for m in re.finditer(r"^### (\w+) idea", text, re.MULTILINE)] == [
        "measured", "anecdote", "opinion"]


def test_rank_report_follows_a_patched_rank_ideas(world, monkeypatch):
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    _, out, _ = world.phase_a()
    first, second, _ = out["items"]
    world.phase_b([
        claim(first["entry_id"], title="zzz", evidence_level="measured", addresses=[key]),
        claim(second["entry_id"], title="aaa", evidence_level="opinion", addresses=[key]),
        {"entry_id": out["items"][2]["entry_id"], "skip": "x"},
    ])
    monkeypatch.setattr(s, "rank_ideas", lambda ideas, rank: sorted(ideas, key=lambda i: i["title"]))
    text = section(world.report(), "## External ideas addressing our difficulties")
    assert text.index("### aaa") < text.index("### zzz")


# --- the unmatched lane ----------------------------------------------------------------------------

def test_unmatched_ideas_are_listed_apart_and_unranked(world):
    world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([
        claim(ids[0], title="strong", evidence_level="measured", addresses=["no-such-ref"]),
        claim(ids[1], title="weak", evidence_level="opinion", addresses=[]),
        {"entry_id": ids[2], "skip": "x"},
    ])
    report = world.report()
    unmatched = section(report, "## External ideas with no matching difficulty - is there one?")
    assert "### strong" in unmatched and "### weak" in unmatched
    assert "(none)" in section(report, "## External ideas addressing our difficulties")


def test_unmatched_lane_notes_the_address_that_matched_nothing(world):
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([claim(ids[0], addresses=["no-such-ref"])] + [{"entry_id": e, "skip": "x"} for e in ids[1:]])
    assert "`no-such-ref`" in world.report()


def test_unmatched_ideas_are_never_mixed_into_the_matched_lane(world):
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([
        claim(ids[0], title="matched one", addresses=[key]), claim(ids[1], title="loose one"),
        {"entry_id": ids[2], "skip": "x"},
    ])
    report = world.report()
    assert "### loose one" not in section(report, "## External ideas addressing our difficulties")
    assert "### matched one" not in section(report, "## External ideas with no matching difficulty - is there one?")


# --- empty lanes and the report shape ------------------------------------------------------------------

def test_empty_lanes_all_three_headings_render_none(world):
    report = world.report()
    for heading in (
        "## External ideas addressing our difficulties",
        "## External ideas with no matching difficulty - is there one?",
        "## Candidate sources (not added)",
    ):
        assert section(report, heading).strip().startswith("(none)")
    assert "### Listed sources with a dossier" in report


def test_empty_lanes_come_after_every_difficulty_section(world):
    world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    world.add_difficulty(sds.KIND_BACKLOG_ITEM, "b1", source_ref="B-1")
    report = world.report()
    external = report.index("## External ideas addressing our difficulties")
    assert external > report.index("## Measured cost signal")
    assert external > report.index("## No measured cost signal")


def test_empty_lanes_json_carries_external_ideas(world):
    payload = json.loads(world.report("--format", "json"))
    assert payload["external_ideas"] == {"matched": [], "unmatched": [], "candidates": [], "listed": []}


# --- candidate sources -------------------------------------------------------------------------------------

def test_candidate_sources_are_deduplicated_and_cite_their_ideas(world):
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([
        claim(ids[0], title="one", candidate_sources=[CANDIDATE]),
        claim(ids[1], title="two", candidate_sources=[CANDIDATE, ALPHA_URL]),
        {"entry_id": ids[2], "skip": "x"},
    ])
    pool = s.candidate_pool(json.loads((world.state / "claims.json").read_text()), {ALPHA_URL, BETA_URL})
    assert pool == {CANDIDATE: ["one", "two"]}


def test_candidate_sources_already_listed_or_not_http_are_dropped_at_judgement(world):
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([claim(ids[0], candidate_sources=[ALPHA_URL, "mailto:a@b.example", CANDIDATE])]
                  + [{"entry_id": e, "skip": "x"} for e in ids[1:]])
    cached = json.loads((world.state / "claims.json").read_text())
    assert cached[ids[0]]["candidate_sources"] == [CANDIDATE]


def test_candidate_sources_lane_shows_a_pending_row_until_described(world):
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([claim(ids[0], title="citing", candidate_sources=[CANDIDATE])]
                  + [{"entry_id": e, "skip": "x"} for e in ids[1:]])
    lane = section(world.report(), "## Candidate sources (not added)")
    assert f"- {CANDIDATE} | dossier pending (cited by citing)" in lane


# --- store kinds ------------------------------------------------------------------------------------------------

def test_kinds_external_idea_is_a_registered_advisory_kind():
    kind = sds.KIND_EXTERNAL_IDEA
    assert kind in sds.EXTERNAL_KINDS and kind in sds.ADVISORY_KINDS and kind in sds.REMEDIATION
    assert not sds.is_actionable(kind)


def test_kinds_ideas_never_enter_the_difficulty_cost_bands(world):
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    world.judge_all(addresses=[key])
    payload = json.loads(world.report("--format", "json"))
    assert {f["source"] for f in payload["findings"]} == {sds.KIND_TELEMETRY_PATTERN}
    assert len(payload["external_ideas"]["matched"]) == 3


def test_kinds_storing_ideas_does_not_resolve_other_producers_rows(world):
    world.add_difficulty(sds.KIND_BACKLOG_ITEM, "b1", source_ref="B-1")
    world.judge_all()
    kinds = {r["kind"] for r in sds.load_rows(world.store)}
    assert kinds == {sds.KIND_BACKLOG_ITEM, sds.KIND_EXTERNAL_IDEA}


def test_kinds_idea_row_key_is_the_store_key_the_report_prints(world):
    _, claims = world.judge_all()
    row = next(r for r in sds.load_rows(world.store) if r["kind"] == sds.KIND_EXTERNAL_IDEA)
    assert row["key"] == sds.finding_key(sds.KIND_EXTERNAL_IDEA, row["path"])
    assert f"`{row['key']}`" in world.report()


# --- the sources file -----------------------------------------------------------------------------------------------

def _parse(text: str):
    return s.parse_sources(text, fetcher.is_http_url)


def test_sources_toml_the_shipped_file_parses():
    meta, listed = _parse(REAL_SOURCES.read_text(encoding="utf-8"))
    assert any(src["enabled"] for src in listed)
    assert all(not src["enabled"] for src in listed if src["kind"] == "html")
    assert all(src["url"].startswith(s.TELEGRAM_PREFIX) for src in listed if src["kind"] == "telegram")
    assert meta["queue_cap"] >= meta["max_items_per_run"]


@pytest.mark.parametrize("bad", [
    [("a", ALPHA_URL, "html", True)],
    [("a", ALPHA_URL, "rss", True), ("a", BETA_URL, "rss", True)],
    [("a", "ftp://x.example/f", "rss", True)],
    [("a", ALPHA_URL, "json", True)],
    [("a", "https://example.com/not-telegram", "telegram", True)],
    [("A B", ALPHA_URL, "rss", True)],
])
def test_sources_toml_rejects_a_bad_source(bad):
    with pytest.raises(s.SourcesError):
        _parse(sources_text(bad))


def test_sources_toml_a_source_needs_a_reason():
    text = sources_text().replace('reason = "because alpha"', 'reason = " "')
    with pytest.raises(s.SourcesError):
        _parse(text)


def test_sources_toml_meta_must_carry_every_bound():
    with pytest.raises(s.SourcesError):
        _parse(sources_text().replace("queue_cap = 100\n", ""))


def test_sources_toml_a_broken_file_exits_2_before_any_write(world):
    world.sources.write_text("[meta\n", encoding="utf-8")
    code, _, err = world.phase_a()[0], None, None
    assert code == 2 and world.state_files() == []


def test_sources_file_unchanged_by_every_mode(world):
    before = (hashlib.sha256(world.sources.read_bytes()).hexdigest(), world.sources.stat().st_mtime_ns)
    real = hashlib.sha256(REAL_SOURCES.read_bytes()).hexdigest()
    out, _ = world.judge_all(candidate_sources=[CANDIDATE])
    world.run("sources", *world.common(), "--emit-candidates", str(world.tmp / "c.json"), "--subject", "alpha")
    world.run("sources", *world.common(), "--describe", world.write("d.json", {"descriptions": [
        {"subject": CANDIDATE, "statements": [{"text": "t", "url": "https://x.example/a"}]}]}))
    world.run("sources", *world.common(), "--dossiers", str(world.tmp / "all.json"))
    world.report()
    assert (hashlib.sha256(world.sources.read_bytes()).hexdigest(), world.sources.stat().st_mtime_ns) == before
    assert hashlib.sha256(REAL_SOURCES.read_bytes()).hexdigest() == real


# --- fetch and parse ------------------------------------------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_fetch_returns_the_body_within_the_cap():
    assert fetcher.fetch(ALPHA_URL, 5.0, 100, opener=lambda r, timeout: _Response(b"abc")) == (b"abc", None)


def test_fetch_refuses_an_oversize_body():
    body, error = fetcher.fetch(ALPHA_URL, 5.0, 10, opener=lambda r, timeout: _Response(b"x" * 50))
    assert body is None and error.startswith("oversize")


def test_fetch_http_error_is_a_value():
    def opener(request, timeout):
        raise urllib.error.HTTPError(ALPHA_URL, 404, "nf", {}, None)

    assert fetcher.fetch(ALPHA_URL, opener=opener) == (None, "HTTP 404")


def test_fetch_timeout_is_a_value():
    def opener(request, timeout):
        raise TimeoutError()

    assert fetcher.fetch(ALPHA_URL, opener=opener) == (None, "timeout")


def test_fetch_passes_the_timeout_to_the_opener():
    seen = {}

    def opener(request, timeout):
        seen["timeout"] = timeout
        return _Response(b"")

    fetcher.fetch(ALPHA_URL, 7.5, opener=opener)
    assert seen["timeout"] == 7.5


def test_fetch_refuses_a_non_http_url_without_calling_out():
    called = []
    assert fetcher.fetch("file:///etc/passwd", opener=lambda r, timeout: called.append(1)) == (None, "not an http(s) url")
    assert called == []


def test_fetch_parses_rss_and_atom():
    entries = fetcher.parse_feed(rss(("p1", "First &lt;b&gt;post&lt;/b&gt;")), "alpha")
    assert entries[0]["title"] == "First post" and entries[0]["url"] == "https://blog.example/p1"
    atom = (b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>A</title><id>urn:1</id>"
            b"<link href='https://blog.example/a'/><updated>2026-09-01T10:00:00Z</updated></entry></feed>")
    parsed = fetcher.parse_feed(atom, "beta")
    assert parsed[0]["url"] == "https://blog.example/a" and parsed[0]["published"].startswith("2026-09-01")


def test_fetch_entry_id_is_stable_across_reads():
    assert fetcher.parse_feed(rss(("p1", "T")), "a")[0]["entry_id"] == fetcher.parse_feed(rss(("p1", "T2")), "a")[0]["entry_id"]


def test_fetch_refuses_entity_declarations():
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    with pytest.raises(ValueError):
        fetcher.parse_feed(bomb, "x")


def test_fetch_a_page_that_is_not_a_feed_raises():
    with pytest.raises(ValueError):
        fetcher.parse_feed(b"<html><body>hi</body></html>", "x")


def test_fetch_read_source_reports_the_reason_not_an_exception():
    source = {"id": "alpha", "url": ALPHA_URL, "kind": "rss"}
    assert fetcher.read_source(source, fetch_fn=lambda url: (None, "HTTP 500")) == ([], "HTTP 500")


# --- purity ----------------------------------------------------------------------------------------------------------

def test_purity_rules_and_wiring_name_no_transport_module():
    assert impure_names(s) == set()
    assert impure_names(scan) == set()


def test_purity_only_the_fetch_module_names_urllib():
    assert impure_names(fetcher) <= {"urllib"}


# --- the due hook ----------------------------------------------------------------------------------------------------------

def _hook():
    spec = importlib.util.spec_from_file_location("improvement_scan_due_for_sources", SCRIPTS_DIR / "hook-improvement-scan-due.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_due_hook_does_not_special_case_the_new_kind():
    assert "external-idea" not in (SCRIPTS_DIR / "hook-improvement-scan-due.py").read_text(encoding="utf-8")


def test_due_hook_counts_an_open_idea_like_any_advisory_row(tmp_path, monkeypatch):
    store = tmp_path / "store.json"
    monkeypatch.setenv(sds.STORE_ENV, str(store))
    sds.upsert_findings(
        [{"kind": sds.KIND_EXTERNAL_IDEA, "path": "external-idea:e1", "detail": "{}"}],
        path=store, source=sds.SOURCE_IMPROVEMENT_SCAN, kinds=frozenset({sds.KIND_EXTERNAL_IDEA}),
    )
    rows = _hook().open_findings()
    assert [r["kind"] for r in rows] == [sds.KIND_EXTERNAL_IDEA]


# --- review due ----------------------------------------------------------------------------------------------------------------

def test_review_due_date_arithmetic():
    meta, _ = _parse(sources_text())
    assert not s.review_due(meta, meta["armed"].replace(day=19))
    assert s.review_due(meta, meta["armed"].fromisoformat("2026-12-15"))
    assert not s.review_due(meta, meta["armed"].fromisoformat("2026-12-14"))


def test_review_due_line_is_printed_with_the_counts_once_the_weeks_passed(world):
    world.judge_all()
    _, out, printed = world.phase_a("--today", "2026-12-15")
    assert out["review_due"] is True
    assert re.search(r"REVIEW-DUE: .*2 runs.*3 cached claims", printed)


def test_review_due_is_silent_before_the_weeks_passed(world):
    _, out, printed = world.phase_a("--today", "2026-11-01")
    assert out["review_due"] is False and "REVIEW-DUE" not in printed


# --- dossier -----------------------------------------------------------------------------------------------------------------------

def _described(world, subject=CANDIDATE, **over):
    desc = {"subject": subject, "statements": [{"text": "writes about agent evals", "url": "https://candidate.example/about"}], **over}
    return world.run("sources", *world.common(), "--describe", world.write("desc.json", {"descriptions": [desc]}))


def _with_candidate(world):
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    assert world.phase_b([claim(ids[0], title="citing", candidate_sources=[CANDIDATE])]
                         + [{"entry_id": e, "skip": "x"} for e in ids[1:]])[0] == 0


def test_dossier_signal_every_format_signal_is_present_with_a_vocabulary_status(world, web):
    _with_candidate(world)
    web.routes["https://hn.algolia.com"] = json.dumps({"hits": [{"points": 5}], "nbHits": 1}).encode()
    code, _, err = _described(world)
    assert code == 0, err
    dossier = json.loads((world.state / "dossiers.json").read_text())[CANDIDATE]
    assert list(dossier["signals"]) == list(s.DOSSIER_SIGNALS)
    assert {sig["status"] for sig in dossier["signals"].values()} <= set(s.STATUS_VOCAB)
    assert dossier["signals"]["hn"]["status"] == "ok"
    assert "score" not in json.dumps(dossier)


def test_dossier_signal_a_failing_computation_is_that_signals_error(world, web):
    _with_candidate(world)
    web.routes["https://hn.algolia.com"] = "HTTP 500"
    _described(world)
    dossier = json.loads((world.state / "dossiers.json").read_text())[CANDIDATE]
    assert dossier["signals"]["hn"]["status"] == "error"
    assert dossier["signals"]["descriptive"]["status"] == "ok"


def test_dossier_signal_safe_wrapper_turns_an_exception_into_an_error():
    def boom():
        raise RuntimeError("bad")

    assert scan._safe_signal(boom) == {"status": "error", "note": "RuntimeError: bad"}


def test_dossier_signal_unknown_subject_is_refused(world):
    code, _, err = _described(world, subject="https://never-cited.example/")
    assert code == 2 and "neither a candidate" in err


def test_dossier_url_required_a_statement_without_one_exits_2_and_writes_nothing(world):
    _with_candidate(world)
    before = world.state_files()
    code, _, err = _described(world, statements=[{"text": "a claim with no source"}])
    assert code == 2 and "http(s) url" in err
    assert world.state_files() == before


def test_dossier_url_required_a_non_http_url_is_refused(world):
    _with_candidate(world)
    assert _described(world, statements=[{"text": "t", "url": "ftp://x.example/a"}])[0] == 2


def test_dossier_url_required_a_statement_list_may_not_be_empty(world):
    _with_candidate(world)
    assert _described(world, statements=[])[0] == 2


def test_dossier_call_bound_stops_external_calls(world, web):
    world.sources.write_text(sources_text(max_external_calls_per_run=1), encoding="utf-8")
    _with_candidate(world)
    web.routes["https://hn.algolia.com"] = json.dumps({"hits": [], "nbHits": 0}).encode()
    web.routes["https://api.github.com"] = json.dumps({"public_repos": 3, "followers": 9}).encode()
    _described(world, github_login="someone", author="Some One")
    dossier = json.loads((world.state / "dossiers.json").read_text())[CANDIDATE]
    assert dossier["signals"]["hn"]["status"] == "ok"
    assert dossier["signals"]["github"] == {"status": "error", "note": "call bound"}
    assert dossier["signals"]["openalex"]["status"] == "error"
    assert not any(url.startswith("https://api.github.com") or url.startswith("https://api.openalex.org")
                   for url in web.calls)


def test_dossier_call_bound_openalex_has_its_own_sub_bound():
    budget = fetcher.ExternalBudget(max_calls=100, max_openalex=2)
    assert [budget.take("openalex") for _ in range(3)] == [True, True, False]
    assert budget.take("hn")


def test_dossier_call_bound_a_shared_host_costs_no_call():
    budget = fetcher.ExternalBudget(max_calls=5)
    assert fetcher.hn_signal("github.com", budget, fetch_fn=lambda u: (None, "x"))["status"] == "n/a"
    assert budget.calls == 0


def test_dossier_local_refs_counts_listed_sources_naming_the_domain(world):
    _, out, _ = world.phase_a()
    ids = [i["entry_id"] for i in out["items"]]
    world.phase_b([claim(ids[0], cited_sources=[CANDIDATE + "/post"]), claim(ids[1]),
                   {"entry_id": ids[2], "skip": "x"}])
    claims = json.loads((world.state / "claims.json").read_text())
    signal = s.local_refs_signal("candidate.example", claims, {"alpha", "beta"}, fetcher.domain_of)
    assert signal == {"status": "ok", "referencing_entries": 1}


def test_dossier_local_refs_ignores_the_subjects_own_entries():
    claims = {"e1": {"source_id": "alpha", "cited_sources": ["https://alpha.example/x"], "candidate_sources": []}}
    assert s.local_refs_signal("alpha.example", claims, set(), fetcher.domain_of)["referencing_entries"] == 0


def test_dossier_local_evidence_profile_is_the_measured_share():
    claims = {
        "e1": {"source_id": "alpha", "evidence_level": "measured"},
        "e2": {"source_id": "alpha", "evidence_level": "opinion"},
    }
    assert s.evidence_profile_signal("alpha", claims) == {"status": "ok", "measured_share": 0.5, "n": 2}
    assert s.evidence_profile_signal("gamma", claims)["status"] == "n/a"


def test_dossier_lane_row_format_and_cited_statements(world, web):
    _with_candidate(world)
    _described(world)
    lane = section(world.report(), "## Candidate sources (not added)")
    row = next(line for line in lane.splitlines() if line.startswith(f"- {CANDIDATE} |"))
    assert [cell.split(":")[0].strip() for cell in row.split(" | ")[1:]] == list(s.DOSSIER_SIGNALS)
    assert "  - writes about agent evals (https://candidate.example/about)" in lane
    assert "dossier pending" not in lane


def test_dossier_lane_listed_source_goes_under_its_own_subsection(world):
    _with_candidate(world)
    _described(world, subject="alpha")
    report = world.report()
    assert "- alpha |" in report.split("### Listed sources with a dossier")[1]
    assert "- alpha |" not in section(report, "## Candidate sources (not added)").split("### Listed")[0]


def test_dossier_lane_emit_candidates_skips_those_already_described(world):
    _with_candidate(world)
    target = world.tmp / "cands.json"
    world.run("sources", *world.common(), "--emit-candidates", str(target), "--subject", "alpha")
    first = json.loads(target.read_text())
    assert [c["subject"] for c in first["candidates"]] == [CANDIDATE]
    assert [row["subject"] for row in first["listed"]] == ["alpha"]
    _described(world)
    world.run("sources", *world.common(), "--emit-candidates", str(target))
    assert json.loads(target.read_text())["candidates"] == []


def test_dossier_lane_emit_candidates_refuses_an_unlisted_subject(world):
    code, _, err = world.run("sources", *world.common(), "--emit-candidates", str(world.tmp / "c.json"), "--subject", "ghost")[:3]
    assert code == 2 and "ghost" in err


def test_dossier_lane_dossiers_flag_writes_the_stored_ones(world):
    _with_candidate(world)
    _described(world)
    target = world.tmp / "all.json"
    world.run("sources", *world.common(), "--dossiers", str(target))
    written = json.loads(target.read_text())["dossiers"]
    assert [d["subject"] for d in written] == [CANDIDATE]


# --- report --state-dir --------------------------------------------------------------------------------------------------------------

def test_report_state_dir_flag_selects_the_directory(world, tmp_path):
    key = world.add_difficulty(sds.KIND_TELEMETRY_PATTERN, "t1", usd=2.0)
    _with_candidate(world)
    assert CANDIDATE in world.report()
    elsewhere = tmp_path / "elsewhere"
    code, out, _ = world.run(
        "report", "--store", str(world.store), "--state-dir", str(elsewhere), "--sources", str(world.sources),
        "--board", str(tmp_path / "no-board.json"))
    assert code == 0 and CANDIDATE not in out and key


def test_report_state_dir_falls_back_to_the_environment(world, monkeypatch):
    _with_candidate(world)
    monkeypatch.setenv(scan.SOURCES_STATE_ENV, str(world.state))
    code, out, _ = world.run("report", "--store", str(world.store), "--sources", str(world.sources),
                             "--board", str(world.tmp / "no-board.json"))
    assert code == 0 and CANDIDATE in out


# --- telegram ------------------------------------------------------------------------------------------------------------------------------

def test_telegram_preview_posts_become_entries():
    entries = fetcher.parse_telegram(telegram_page("First post<br/>second line", "Another"), "tg")
    assert [e["title"] for e in entries] == ["First post", "Another"]
    assert entries[0]["url"] == "https://t.me/example_channel/1" and entries[0]["published"].startswith("2026-09-01")


def test_telegram_an_empty_preview_is_the_sources_error(world, web):
    world.sources.write_text(sources_text([("tg", TG_URL, "telegram", True)]), encoding="utf-8")
    web.routes[TG_URL] = b"<html><body>channel is private</body></html>"
    _, out, _ = world.phase_a()
    assert out["sources"] == [{"id": "tg", "status": "error", "error": "no preview"}]


def test_telegram_source_feeds_the_queue_like_any_other(world, web):
    world.sources.write_text(sources_text([("tg", TG_URL, "telegram", True)]), encoding="utf-8")
    web.routes[TG_URL] = telegram_page("Agents need evals", "Second")
    _, out, _ = world.phase_a()
    assert [i["title"] for i in out["items"]] == ["Agents need evals", "Second"]


def test_telegram_feed_signal_states_that_only_a_preview_was_read():
    page = telegram_page("hello")
    signal = fetcher.feed_signal(TG_URL, "telegram", fetcher._parse_date("2026-09-10T00:00:00+00:00"), fetch_fn=lambda u: (page, None))
    assert signal["status"] == "ok" and signal["basis"] == fetcher.TELEGRAM_BASIS


def test_telegram_hn_signal_is_not_applicable_on_the_shared_host():
    budget = fetcher.ExternalBudget(5)
    assert fetcher.hn_signal(fetcher.domain_of(TG_URL), budget, fetch_fn=lambda u: (None, "x")) == {
        "status": "n/a", "note": "shared host"}
