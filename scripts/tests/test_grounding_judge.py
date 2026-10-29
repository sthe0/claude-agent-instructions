"""The grounding judge: a fail-open model judge of each link between a prescription and its grounds.

Every test drives the real judge with a fixture runner that returns a canned reply and records the
prompt it was given -- no model is called. A test pins one case token (its name ends in the token),
so a mutation of one rule turns exactly the tests of that rule red by assertion: results are read
with `.get`, defect lists are compared whole, and a fixture never builds a state where the rule is
indifferent to the mutation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from agentctl.dispatch import RunResult

TODAY = date(2026, 10, 29)
SCRIPT = Path(__file__).resolve().parents[1] / "grounding-judge.py"

PRESCRIPTION = {"principle": "Cache the results of pure calls.", "grounded_in": ["regularity:reg-a"]}
GROUND = {"ref": "regularity:reg-a", "statement": "A pure call repeats its result."}
CANDIDATE = {"ref": "regularity:reg-b", "statement": "Memory reads cost less than recomputation."}
GIVEN = {"ref": "regularity:given-a", "statement": "The vendor limit is 10 rps.", "given": "doc",
         "given_source": "docs/vendor.md", "last_verified": "2025-01-01"}
ONTOLOGY = {"ref": "ontology:onto-a", "distinction": "A hot path against a cold one.", "adequacy": "Cuts by call rate."}

TABLE = '''
[[regularity]]
tier = "global"
slug = "reg-a"
statement = "A pure call repeats its result."
refutation = "A pure call that returns two results."
induced_from = ["experience:exp-a"]

[[leaf]]
tier = "global"
path = "principles/prin-a.md"
class = "P"
principle = "Cache the results of pure calls."
goal_class = "speed"
grounded_in = ["regularity:reg-a"]

[[leaf]]
tier = "global"
path = "principles/prin-b.md"
class = "P"
principle = "Prefer the table's in-place grounds."
goal_class = "speed"
grounded_in = ["regularity:k-ind", "ontology:onto-a"]
override = "accepted: the ontology is a sketch"

[[leaf]]
tier = "global"
path = "regularities/given-a.md"
class = "G"
statement = "The vendor limit is 10 rps."
refutation = "The vendor raises the limit."
given = "doc"
given_source = "docs/vendor.md"

[[leaf]]
tier = "global"
path = "ontology/onto-a.md"
class = "O"
distinction = "A hot path against a cold one."
adequacy = "Cuts by call rate."

[[leaf]]
tier = "global"
path = "regularities/k-rule.md"
class = "K"
statement = "A rule-derived statement."
refutation = "Its rule changes."
generality = "wide"
rule_derived = "rule: cache pure calls"

[[leaf]]
tier = "global"
path = "regularities/k-ind.md"
class = "K"
statement = "NEW statement of the in-place regularity."
refutation = "A counter-case."
generality = "narrow"
induced_from = ["experience:exp-a"]

[[stamp]]
tier = "global"
path = "principles/prin-s.md"
grounded_in = ["regularity:reg-a"]

[[rebase]]
tier = "project"
path = "principles/prin-r.md"
grounded_in = ["regularity:reg-a"]
'''
TAGS = {
    "global:principles/prin-a.md", "global:principles/prin-b.md", "global:regularities/given-a.md",
    "global:ontology/onto-a.md", "global:regularities/k-rule.md", "global:regularities/k-ind.md",
    "global:principles/prin-s.md", "project:principles/prin-r.md",
}
P_TAG = "global:principles/prin-a.md"
P2_TAG = "global:principles/prin-b.md"
G_TAG = "global:regularities/given-a.md"
O_TAG = "global:ontology/onto-a.md"
KR_TAG = "global:regularities/k-rule.md"
KI_TAG = "global:regularities/k-ind.md"


def reply(*defects, reason="because"):
    return json.dumps({"defects": list(defects), "reason": reason})


class Runner:
    """A fixture judge: returns `answer` (a string, or a function of the prompt) and records each prompt."""

    def __init__(self, answer=None, *, returncode=0, timed_out=False, raises=None):
        self.answer = reply() if answer is None else answer
        self.returncode, self.timed_out, self.raises = returncode, timed_out, raises
        self.calls = []

    def __call__(self, argv, *, timeout, stdin):
        self.calls.append(stdin)
        if self.raises is not None:
            raise self.raises
        text = self.answer(stdin) if callable(self.answer) else self.answer
        return RunResult(self.returncode, stdout=text, timed_out=self.timed_out)


def _gj():
    from agentctl import grounding_judge

    return grounding_judge


def _judge(runner, **kw):
    return _gj().judge_grounding_link(PRESCRIPTION, [GROUND], [CANDIDATE], runner=runner, **kw)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _table(tmp_path, text=TABLE) -> Path:
    return _write(tmp_path / "table.toml", text)


def _links(verdicts) -> dict:
    return verdicts.get("links") or {}


def _rec(verdicts, tag) -> dict:
    return _links(verdicts).get(tag) or {}


def _judge_table(tmp_path, runner, **kw):
    return _gj().judge_table(_table(tmp_path), runner=runner, cache=tmp_path / "cache", today=TODAY, **kw)


_REGULARITY = "---\nname: {slug}\nschema: regularity/v1\nlast_verified: {verified}\n{extra}---\n\n## Regularity\n{text}\n"
_PRINCIPLE = "---\nname: {slug}\nschema: principle/v1\ngrounded_in: {refs}\n{extra}---\n\n## Principle\n{text}\n"


def _corpus(tmp_path, *, principle_extra="converted_from: leaf/v1\n", reg_text="A pure call repeats its result."):
    from lib import principle_provenance as pp

    leaves = tmp_path / "leaves"
    _write(leaves / "regularities" / "reg-a.md", _REGULARITY.format(
        slug="reg-a", verified="2026-10-01", extra="induced_from: [experience:exp-a]\n", text=reg_text))
    _write(leaves / "regularities" / "k-ind.md", _REGULARITY.format(
        slug="k-ind", verified="2026-10-01", extra="induced_from: [experience:exp-a]\n", text="OLD statement."))
    _write(leaves / "regularities" / "given-a.md", _REGULARITY.format(
        slug="given-a", verified="2026-10-20", extra="given: doc\ngiven_source: docs/vendor.md\n",
        text="The vendor limit is 10 rps."))
    _write(leaves / "principles" / "prin-a.md", _PRINCIPLE.format(
        slug="prin-a", refs="[regularity:reg-a]", extra=principle_extra, text="Cache the results of pure calls."))
    _write(leaves / "principles" / "prin-s.md", _PRINCIPLE.format(
        slug="prin-s", refs="[regularity:reg-a]", extra="", text="Not part of the digest."))
    _write(leaves / "ontology" / "onto-a.md",
           "---\nname: onto-a\nschema: ontology/v1\n---\n\n## Distinction\nA hot path against a cold one.\n\n"
           "## Difficulty\nd\n\n## Adequacy\nCuts by call rate.\n")
    return pp.load_corpus(leaves)


# --- link defects and ground defects are told apart --------------------------------------------------


def _single_link_defect(name):
    runner = Runner(reply(name))
    out = _judge(runner)
    assert out.get("status") == "ok"
    assert out.get("verdict") == "defective"
    assert out.get("link_defects") == [name]
    assert out.get("ground_defects") == []
    assert out.get("blocking") is False


def test_irrelevant_link_refused():
    _single_link_defect("irrelevant")


def test_insufficient_link_refused():
    _single_link_defect("insufficient")


def test_scope_mismatch_link_refused():
    _single_link_defect("scope_mismatch")


def test_contradicting_link_refused():
    _single_link_defect("contradicting")


def test_defective_ground_reported_apart():
    out = _judge(Runner(reply("defective_ground", "irrelevant")))
    assert out.get("link_defects") == ["irrelevant"]
    assert out.get("ground_defects") == ["defective_ground"]
    assert out.get("verdict") == "defective"


def test_prescriptive_knowledge_flagged():
    out = _judge(Runner(reply("prescriptive_knowledge")))
    assert out.get("ground_defects") == ["prescriptive_knowledge"]
    assert out.get("link_defects") == []


def test_sound_link_passes():
    out = _judge(Runner(reply(reason="every ground carries")))
    assert out.get("status") == "ok"
    assert out.get("verdict") == "sound"
    assert out.get("link_defects") == []
    assert out.get("ground_defects") == []
    assert out.get("reason") == "every ground carries"


def test_override_recorded_and_surfaced():
    out = _judge(Runner(reply("irrelevant")), override="accepted: the ground is indirect")
    assert out.get("override") == "accepted: the ground is indirect"
    assert out.get("overridden") is True
    assert out.get("link_defects") == ["irrelevant"]
    record = _gj().table_record(out)
    assert record.get("override") == "accepted: the ground is indirect"
    assert record.get("overridden") is True
    sound = _judge(Runner(reply()), override="accepted: no defect to override")
    assert sound.get("overridden") is False
    assert _gj().apply_override({"link_defects": ["irrelevant"], "ground_defects": []}, "").get("override") is None


# --- a down judge fails open -------------------------------------------------------------------------


@pytest.mark.parametrize("runner", [
    Runner(returncode=1), Runner(timed_out=True), Runner(raises=OSError("no such judge")),
    Runner("not json at all"), Runner(reply("no_such_defect")), Runner(json.dumps({"defects": "irrelevant"})),
    None,
], ids=["exit", "timeout", "raises", "not-json", "unknown-defect", "defects-not-a-list", "no-runner"])
def test_judge_down_fails_open_warned(runner):
    out = _judge(runner)
    assert out.get("status") == "unavailable"
    assert out.get("verdict") is None
    assert out.get("link_defects") == []
    assert out.get("ground_defects") == []
    assert out.get("blocking") is False
    assert "unavailable" in (out.get("warning") or "")


# --- the question carries both counterfactual tests --------------------------------------------------


def test_prompt_carries_both_tests(tmp_path):
    corpus = _corpus(tmp_path)
    candidates = _gj().candidate_grounds(corpus)
    assert any(c.get("ref") == "regularity:given-a" for c in candidates)
    runner = Runner()
    out = _gj().judge_grounding_link(PRESCRIPTION, [GROUND], candidates, runner=runner)
    assert out.get("status") == "ok"
    prompt = runner.calls[0] if runner.calls else ""
    assert "Forward test" in prompt
    assert "Backward test" in prompt
    assert PRESCRIPTION["principle"] in prompt
    assert GROUND["statement"] in prompt
    assert "The vendor limit is 10 rps." in prompt


def test_candidates_are_every_regularity_and_ontology_uncapped(tmp_path):
    corpus = _corpus(tmp_path)
    refs = {c.get("ref") for c in _gj().candidate_grounds(corpus)}
    assert refs == {"regularity:reg-a", "regularity:k-ind", "regularity:given-a", "ontology:onto-a"}


# --- the verdict is cached by the digest of the link -------------------------------------------------


def test_verdict_cached_by_digest(tmp_path):
    gj = _gj()
    cache = tmp_path / "cache"
    runner = Runner(reply("irrelevant"))
    first = _judge(runner, cache=cache)
    digest = first.get("digest")
    assert digest == hashlib.sha256(gj.link_inputs(PRESCRIPTION, [GROUND]).encode()).hexdigest()
    assert (cache / f"{digest}.json").is_file()
    second = _judge(runner, cache=cache)
    assert len(runner.calls) == 1
    assert second.get("link_defects") == ["irrelevant"]
    other_ground = dict(GROUND, statement="A pure call may differ per call.")
    third = gj.judge_grounding_link(PRESCRIPTION, [other_ground], [CANDIDATE], runner=runner, cache=cache)
    assert third.get("digest") != digest
    assert len(runner.calls) == 2
    assert (cache / f"{third.get('digest')}.json").is_file()
    assert gj.link_digest(PRESCRIPTION, [GROUND]) != gj.link_digest(PRESCRIPTION, [other_ground])


def test_digest_covers_prescription_text_and_ignores_incidental_fields():
    gj = _gj()
    base = gj.link_digest(PRESCRIPTION, [GROUND])
    assert gj.link_digest(dict(PRESCRIPTION, principle="Cache nothing."), [GROUND]) != base
    assert gj.link_digest(dict(PRESCRIPTION, goal_class="speed", tier="global"), [GROUND]) == base
    assert gj.link_digest(PRESCRIPTION, [dict(GROUND, last_verified="2026-01-01")]) == base


# --- a given regularity's currency is decided by rule ------------------------------------------------


def test_stale_given_flagged():
    runner = Runner(reply())
    out = _gj().judge_grounding_link(PRESCRIPTION, [GIVEN], [CANDIDATE], runner=runner, today=TODAY)
    assert len(runner.calls) == 1
    assert out.get("ground_defects") == ["unverified_given"]
    assert out.get("verdict") == "defective"
    fresh = dict(GIVEN, last_verified="2026-10-01")
    ok = _gj().judge_grounding_link(PRESCRIPTION, [fresh], [CANDIDATE], runner=Runner(), today=TODAY)
    assert ok.get("ground_defects") == []
    undated = {k: v for k, v in GIVEN.items() if k != "last_verified"}
    out = _gj().judge_grounding_link(PRESCRIPTION, [undated], [CANDIDATE], runner=Runner(), today=TODAY)
    assert out.get("ground_defects") == ["unverified_given"]


def test_given_checked_by_citer_passes():
    out = _gj().judge_grounding_link(PRESCRIPTION, [GIVEN], [CANDIDATE], runner=Runner(), today=TODAY,
                                     checked_paths=("docs/vendor.md",))
    assert out.get("ground_defects") == []
    assert out.get("verdict") == "sound"
    assert _gj().given_currency(GIVEN, checked_paths=("docs/vendor.md",), today=TODAY) is True
    assert _gj().given_currency(GIVEN, checked_paths=("docs/other.md",), today=TODAY) is False


def test_given_currency_by_rule_not_model():
    gj = _gj()
    assert gj.given_currency({"ref": "regularity:reg-a", "statement": "s"}, today=TODAY) is True
    assert gj.given_currency(dict(GIVEN, last_verified="2026-05-02"), today=TODAY) is True
    assert gj.given_currency(dict(GIVEN, last_verified="2026-04-01"), today=TODAY) is False
    assert gj.given_currency(dict(GIVEN, last_verified="garbage"), today=TODAY) is False


def test_stale_given_stays_flagged_on_a_cache_hit(tmp_path):
    cache = tmp_path / "cache"
    runner = Runner(reply())
    gj = _gj()
    gj.judge_grounding_link(PRESCRIPTION, [GIVEN], [CANDIDATE], runner=runner, cache=cache, today=TODAY,
                            checked_paths=("docs/vendor.md",))
    again = gj.judge_grounding_link(PRESCRIPTION, [GIVEN], [CANDIDATE], runner=runner, cache=cache, today=TODAY)
    assert len(runner.calls) == 1
    assert again.get("ground_defects") == ["unverified_given"]


def test_max_age_read_from_config(monkeypatch):
    from agentctl import config

    gj = _gj()
    monkeypatch.setattr(config, "parse_config_md", lambda path=None: {"given-regularity-max-age-days": "30"})
    assert gj.max_age_days() == 30
    assert gj.given_currency(dict(GIVEN, last_verified="2026-10-01"), today=TODAY) is True
    assert gj.given_currency(dict(GIVEN, last_verified="2026-09-01"), today=TODAY) is False
    monkeypatch.setattr(config, "parse_config_md", lambda path=None: {"given-regularity-max-age-days": "400"})
    assert gj.max_age_days() == 400
    assert gj.given_currency(dict(GIVEN, last_verified="2026-01-01"), today=TODAY) is True


def test_max_age_days_default_is_180():
    assert _gj().max_age_days() == 180


# --- the ontology adequacy question ------------------------------------------------------------------


def test_ontology_adequacy_asked():
    runner = Runner(reply("inadequate_distinction"))
    out = _gj().judge_grounding_link(PRESCRIPTION, [GROUND, ONTOLOGY], [CANDIDATE], runner=runner)
    prompt = runner.calls[0] if runner.calls else ""
    assert "Ontology adequacy:" in prompt
    assert ONTOLOGY["distinction"] in prompt
    assert out.get("ground_defects") == ["inadequate_distinction"]
    plain = Runner()
    _judge(plain)
    assert "Ontology adequacy:" not in (plain.calls[0] if plain.calls else "")


# --- the table side ----------------------------------------------------------------------------------


def test_table_verdicts_written(tmp_path):
    table = _table(tmp_path)
    verdicts = _gj().judge_table(table, runner=Runner(), cache=tmp_path / "cache", today=TODAY,
                                 checked_paths=("docs/vendor.md",))
    assert verdicts.get("table_sha256") == hashlib.sha256(table.read_bytes()).hexdigest()
    assert verdicts.get("cache") == str(tmp_path / "cache")
    links = _links(verdicts)
    assert set(links) == TAGS
    names = {p.name for p in (tmp_path / "cache").iterdir()}
    for tag, record in links.items():
        assert record.get("status") == "ok", tag
        assert record.get("verdict") == "sound", tag
        assert f"{record.get('digest')}.json" in names, tag


def test_table_outage_recorded_null(tmp_path):
    verdicts = _judge_table(tmp_path, Runner(returncode=1))
    assert set(_links(verdicts)) == TAGS
    for tag in TAGS:
        record = _rec(verdicts, tag)
        assert record.get("verdict") is None, tag
        assert record.get("status") == "unavailable", tag
        assert "unavailable" in (record.get("warning") or ""), tag
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())


def test_table_reads_table_grounds(tmp_path):
    runner = Runner()
    _judge_table(tmp_path, runner)
    prompts = [p for p in runner.calls if "Cache the results of pure calls." in p]
    assert prompts, "the prompt of the P entry"
    assert "A pure call repeats its result." in prompts[0]
    data = __import__("tomllib").loads(TABLE)
    entry = dict(next(e for e in data["leaf"] if e["path"] == "principles/prin-a.md"))
    assert _gj().table_grounds(entry, data) == [
        {"ref": "regularity:reg-a", "statement": "A pure call repeats its result."}]


def test_table_reads_in_place_ground(tmp_path):
    corpus = _corpus(tmp_path)
    runner = Runner()
    _gj().judge_table(_table(tmp_path), runner=runner, cache=tmp_path / "cache", corpus=corpus, today=TODAY)
    prompts = [p for p in runner.calls if "Prefer the table's in-place grounds." in p]
    assert prompts, "the prompt of the in-place P entry"
    assert "NEW statement of the in-place regularity." in prompts[0]
    assert "A hot path against a cold one." in prompts[0]
    assert "Ontology adequacy:" in prompts[0]
    assert all("OLD statement." not in p for p in runner.calls)


def test_table_override_from_table(tmp_path):
    verdicts = _judge_table(tmp_path, Runner(reply("irrelevant")))
    overridden = _rec(verdicts, P2_TAG)
    assert overridden.get("override") == "accepted: the ontology is a sketch"
    assert overridden.get("overridden") is True
    plain = _rec(verdicts, P_TAG)
    assert plain.get("override") is None
    assert plain.get("link_defects") == ["irrelevant"]
    assert plain.get("overridden") is False


def test_table_keys_equal_bulk_entries(tmp_path):
    gj = _gj()
    data = __import__("tomllib").loads(TABLE)
    assert {tag for tag, _entry in gj.table_entries(data)} == TAGS
    verdicts = _judge_table(tmp_path, Runner())
    assert set(_links(verdicts)) == TAGS
    for kind in ("leaf", "stamp", "rebase"):
        for entry in data[kind]:
            assert f"{entry['tier']}:{entry['path']}" in _links(verdicts)


def test_table_digest_equals_leaf_digest(tmp_path):
    gj = _gj()
    corpus = _corpus(tmp_path)
    table = (
        '[[regularity]]\ntier = "global"\nslug = "reg-a"\nstatement = "A pure call repeats its result."\n'
        'refutation = "r"\ninduced_from = ["experience:exp-a"]\n\n'
        '[[leaf]]\ntier = "global"\npath = "principles/prin-a.md"\nclass = "P"\n'
        'principle = "Cache the results of pure calls."\ngoal_class = "speed"\ngrounded_in = ["regularity:reg-a"]\n\n'
        '[[stamp]]\ntier = "global"\npath = "principles/prin-s.md"\ngrounded_in = ["regularity:reg-a"]\n'
    )
    verdicts = gj.judge_table(_table(tmp_path, table), runner=Runner(), cache=tmp_path / "cache", today=TODAY)
    for slug, tag in (("prin-a", "global:principles/prin-a.md"), ("prin-s", "global:principles/prin-s.md")):
        leaf = corpus.find(slug)
        leaf_digest = hashlib.sha256(gj.leaf_link_inputs(leaf, corpus).encode()).hexdigest()
        assert _rec(verdicts, tag).get("digest") == leaf_digest, slug
        assert gj.judge_leaf(leaf, corpus, runner=Runner(), today=TODAY).get("digest") == leaf_digest, slug


def test_ground_only_entries_no_link_defect(tmp_path):
    gj = _gj()
    runner = Runner(reply("irrelevant", "defective_ground"))
    verdicts = _judge_table(tmp_path, runner, checked_paths=("docs/vendor.md",))
    for tag in (G_TAG, O_TAG, KI_TAG):
        record = _rec(verdicts, tag)
        assert record.get("link_defects") == [], tag
        assert record.get("ground_defects") == ["defective_ground"], tag
    for tag in (P_TAG, KR_TAG):
        record = _rec(verdicts, tag)
        assert record.get("link_defects") == ["irrelevant"], tag
        assert record.get("ground_defects") == ["defective_ground"], tag
    data = __import__("tomllib").loads(TABLE)
    questions = {tag: gj.entry_question(tag, entry) for tag, entry in gj.table_entries(data)}
    assert questions[G_TAG] == questions[O_TAG] == questions[KI_TAG] == "ground"
    assert questions[P_TAG] == questions[KR_TAG] == questions[P2_TAG] == "link"
    # the [[stamp]] and the [[rebase]] entry share one link, so the second is a cache hit
    assert sum("Backward test" in p for p in runner.calls) == 4
    assert sum("This entry is a ground and no prescription" in p for p in runner.calls) == 3


def test_table_given_currency_comes_from_the_corpus(tmp_path):
    corpus = _corpus(tmp_path)
    gj = _gj()
    with_corpus = gj.judge_table(_table(tmp_path), runner=Runner(), cache=tmp_path / "c1", corpus=corpus, today=TODAY)
    assert _rec(with_corpus, G_TAG).get("ground_defects") == []
    without = gj.judge_table(_table(tmp_path), runner=Runner(), cache=tmp_path / "c2", today=TODAY)
    assert _rec(without, G_TAG).get("ground_defects") == ["unverified_given"]
    cited = gj.judge_table(_table(tmp_path), runner=Runner(), cache=tmp_path / "c3", today=TODAY,
                           checked_paths=("docs/vendor.md",))
    assert _rec(cited, G_TAG).get("ground_defects") == []


def test_bulk_call_shapes_match(tmp_path, monkeypatch):
    gj = _gj()
    home = tmp_path / "home"
    monkeypatch.setattr(gj, "agentctl_dir", lambda: home)
    here, there = tmp_path / "cwd-a", tmp_path / "cwd-b"
    here.mkdir()
    there.mkdir()
    table = _table(tmp_path)
    monkeypatch.chdir(here)
    first = gj.judge_table(table, runner=Runner(reply("irrelevant")), today=TODAY)
    monkeypatch.chdir(there)
    second = gj.judge_table(table, runner=Runner(reply("irrelevant")), today=TODAY)
    expected = str(home / "grounding-judge-cache")
    assert first.get("cache") == expected
    assert second.get("cache") == expected
    assert Path(expected).is_dir()
    data = __import__("tomllib").loads(TABLE)
    entries = dict(gj.table_entries(data))
    for tag in TAGS:
        record = _rec(first, tag)
        assert record.get("digest") == gj.table_link_digest(entries[tag], data), tag
        cached = json.loads((Path(expected) / f"{record.get('digest')}.json").read_text(encoding="utf-8"))
        for key in ("verdict", "link_defects", "ground_defects"):
            assert cached.get(key) == record.get(key), (tag, key)


# --- stamping ----------------------------------------------------------------------------------------


def test_stamp_writes_digest():
    gj = _gj()
    sound = {"status": "ok", "verdict": "sound", "link_defects": [], "ground_defects": [], "digest": "abc"}
    assert gj.stamp_lines(sound) == ["grounding_judged: abc"]
    overridden = {"status": "ok", "verdict": "defective", "link_defects": ["irrelevant"], "ground_defects": [],
                  "digest": "abc", "override": "accepted: indirect"}
    assert gj.stamp_lines(overridden) == ["grounding_judged: abc", "grounding_override: accepted: indirect"]
    judged = _judge(Runner(reply()))
    assert gj.stamp_lines(judged) == [f"grounding_judged: {judged.get('digest')}"]


def test_defective_link_not_stamped():
    gj = _gj()
    for defects in ({"link_defects": ["irrelevant"], "ground_defects": []},
                    {"link_defects": [], "ground_defects": ["defective_ground"]}):
        record = {"status": "ok", "verdict": "defective", "digest": "abc", **defects}
        assert gj.stamp_lines(record) == []


def test_unavailable_not_stamped(tmp_path):
    gj = _gj()
    assert gj.stamp_lines(gj.table_record(gj.unavailable_outcome("down", "abc"))) == []
    assert gj.stamp_lines(_judge(Runner(returncode=1))) == []
    verdicts = _judge_table(tmp_path, Runner(returncode=1))
    assert set(_links(verdicts)) == TAGS
    for tag in TAGS:
        assert gj.stamp_lines(_rec(verdicts, tag)) == [], tag


# --- an order quote ----------------------------------------------------------------------------------


def test_order_quote_unfaithful_flagged():
    gj = _gj()
    bad = Runner(json.dumps({"faithful": False, "reason": "the quote says the opposite"}))
    assert gj.order_quote_defects("never cache", "cache everything", runner=bad) == ["unfaithful_quote"]
    assert gj.judge_order_quote("never cache", "cache everything", runner=bad).get("verdict") == "defective"
    good = Runner(json.dumps({"faithful": True, "reason": "same"}))
    assert gj.order_quote_defects("cache it", "cache everything", runner=good) == []
    with pytest.raises(gj.JudgeUnavailable):
        gj.order_quote_defects("q", "s", runner=Runner(returncode=2))
    down = gj.judge_order_quote("q", "s", runner=Runner(returncode=2))
    assert down.get("status") == "unavailable"
    assert down.get("verdict") is None


# --- the corpus side ---------------------------------------------------------------------------------


def test_judge_corpus_records_each_principle_that_names_grounds(tmp_path):
    corpus = _corpus(tmp_path)
    records = _gj().judge_corpus(corpus, runner=Runner(reply("insufficient")), cache=tmp_path / "cache", today=TODAY)
    assert set(records) == {"global:prin-a", "global:prin-s"}
    assert records["global:prin-a"].get("link_defects") == ["insufficient"]


# --- the command line --------------------------------------------------------------------------------


def _cli():
    spec = importlib.util.spec_from_file_location("grounding_judge_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_table_subcommand(tmp_path):
    out = tmp_path / "verdicts.json"
    rc = _cli().main(["table", str(_table(tmp_path)), "--out", str(out), "--cache", str(tmp_path / "cache")],
                     runner=Runner())
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["table_sha256"] == hashlib.sha256((tmp_path / "table.toml").read_bytes()).hexdigest()
    assert set(data["links"]) == TAGS


def test_cli_link_subcommand(tmp_path, capsys):
    spec = _write(tmp_path / "link.json", json.dumps(
        {"prescription": PRESCRIPTION, "grounds": [GROUND], "candidates": [CANDIDATE]}))
    rc = _cli().main(["link", str(spec), "--cache", str(tmp_path / "cache")], runner=Runner(reply("irrelevant")))
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["link_defects"] == ["irrelevant"]
    assert shown["digest"] == _gj().link_digest(PRESCRIPTION, [GROUND])


def test_cli_corpus_subcommand(tmp_path, capsys):
    _corpus(tmp_path)
    rc = _cli().main(["corpus", "--leaves", str(tmp_path / "leaves"), "--cache", str(tmp_path / "cache")],
                     runner=Runner())
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == {"global:prin-a", "global:prin-s"}


def test_cli_stamp_subcommand(tmp_path, capsys):
    verdicts = _write(tmp_path / "verdicts.json", json.dumps({"links": {
        "global:a.md": {"status": "ok", "link_defects": [], "ground_defects": [], "digest": "abc"},
        "global:b.md": {"status": "ok", "link_defects": ["irrelevant"], "ground_defects": [], "digest": "def"}}}))
    assert _cli().main(["stamp", str(verdicts), "global:a.md"]) == 0
    assert capsys.readouterr().out.splitlines() == ["grounding_judged: abc"]
    assert _cli().main(["stamp", str(verdicts), "global:b.md"]) == 1
    assert capsys.readouterr().out == ""
