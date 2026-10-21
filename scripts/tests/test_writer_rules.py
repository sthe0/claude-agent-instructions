"""Sync tests between tech-writer SKILL.md and publish-rules.toml.

The expected rule id set is pinned literally here, so a parser that silently
merges or drops a rule cannot make the completeness checks pass by agreeing with
itself.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

import form_fixtures as ff
import form_picker_control as control

PINNED_IDS = {f"say-{n}" for n in range(1, 16)} | {f"how-{n}" for n in range(1, 14)}
REGISTRY_KEYS = {"id", "section", "number", "fingerprint", "status", "literals", "patterns", "reason"}


def wr():
    return importlib.import_module("lib.writer_rules")


def test_rule_ids_match_pinned_anchor():
    assert {r.id for r in wr().parse_skill()} == PINNED_IDS


def test_numbering_is_continuous_per_section():
    for section in ("say", "how"):
        numbers = [r.number for r in wr().parse_skill() if r.section == section]
        assert numbers == list(range(1, len(numbers) + 1))


def test_say_13_not_merged_with_say_12_and_owns_self_check():
    m = wr()
    say_12, say_13 = m.rule_text("say-12"), m.rule_text("say-13")
    assert "13. **A ticket comment" not in say_12
    assert "thread discipline" not in say_13
    assert "Recalling this rule is not enough" in say_13


def test_manager_call_list_is_not_captured():
    fingerprints = {r.fingerprint for r in wr().parse_skill()}
    assert not fingerprints & {"Authoring", "Polishing a plan", "Polishing a comment"}


def test_every_numbered_rule_classified_once():
    m = wr()
    ids = [r.id for r in m.load_registry()]
    assert sorted(ids) == sorted(PINNED_IDS)


def test_registry_ids_resolve_with_matching_fingerprint():
    assert wr().sync_problems() == []


def test_judge_literals_occur_verbatim_in_rule_text():
    m = wr()
    for rule in m.load_registry():
        text = m.rule_text(rule.id)
        for literal in rule.literals:
            assert literal in text, (rule.id, literal)


def test_every_judge_rule_has_a_handle():
    for rule in wr().load_registry():
        if rule.status == "judge":
            assert rule.literals or rule.patterns, rule.id


def test_every_pattern_has_justification():
    for rule in wr().load_registry():
        for source, justification in rule.patterns:
            assert source and justification.strip(), rule.id


def test_perception_entries_have_reason():
    for rule in wr().load_registry():
        if rule.status in ("perception", "judge-form"):
            assert rule.reason.strip(), rule.id
        else:
            assert rule.status == "judge", rule.id


def test_rule_text_is_verbatim_from_skill_md():
    m = wr()
    skill = m.SKILL_PATH.read_text(encoding="utf-8")
    for rule in m.parse_skill():
        assert rule.text in skill, rule.id


def test_how_7_text_includes_calque_table_section():
    text = wr().rule_text("how-7")
    assert "## Calque and jargon table" in text
    assert "запушить" in text


def test_say_13_is_judge_checkable():
    by_id = {r.id: r for r in wr().load_registry()}
    assert by_id["say-13"].status == "judge"


def test_registry_carries_no_rule_wording():
    import tomllib

    data = tomllib.loads(wr().REGISTRY_PATH.read_text(encoding="utf-8"))
    for entry in data["rule"]:
        assert set(entry) <= REGISTRY_KEYS, entry["id"]
        assert len(entry["fingerprint"]) <= 160, entry["id"]
        assert len(entry.get("reason", "")) <= 200, entry["id"]


def test_find_candidates_say_13_hits_address_not_lookalikes():
    m = wr()
    for body in ("вы написали", "от вас", "по вашему выбору", "вам отдельно", "вами", "as you said", "your plan"):
        assert "say-13" in dict(m.find_candidates(body)), body
    for body in ("вывод таков", "выход найден", "вашингтон", "you_id = 3", "layout"):
        assert "say-13" not in dict(m.find_candidates(body)), body


def test_find_candidates_silent_on_clean_body():
    assert wr().find_candidates("Проверили сборку: тесты зелёные, затем влили ветку.") == []


def test_find_candidates_reports_hit_lines():
    hits = dict(wr().find_candidates("первая\nпо вашему выбору\nтретья"))
    assert hits["say-13"] == ["по вашему выбору"]


@pytest.fixture
def skill_copy(tmp_path):
    src = wr().SKILL_PATH.read_text(encoding="utf-8")
    return tmp_path, src


def test_sync_detects_renumbered_skill(skill_copy):
    tmp, src = skill_copy
    swapped = src.replace("6. **Concrete over abstract.**", "7. **Concrete over abstract.**").replace(
        "7. **No English calques.**", "6. **No English calques.**"
    )
    assert swapped != src
    path = tmp / "SKILL.md"
    path.write_text(swapped, encoding="utf-8")
    assert any("how-7" in p for p in wr().sync_problems(skill_path=path))


def test_sync_detects_unclassified_new_rule(skill_copy):
    tmp, src = skill_copy
    added = src.replace("\n## Calque and jargon table", "\n14. **A brand new rule.** Text.\n\n## Calque and jargon table")
    assert added != src
    path = tmp / "SKILL.md"
    path.write_text(added, encoding="utf-8")
    assert "how-14: numbered rule in SKILL.md is not classified" in wr().sync_problems(skill_path=path)


def test_sync_detects_missing_literal(skill_copy):
    tmp, src = skill_copy
    path = tmp / "SKILL.md"
    path.write_text(src.replace("стоит отметить", "стоит заметить"), encoding="utf-8")
    assert any("how-5" in p and "стоит отметить" in p for p in wr().sync_problems(skill_path=path))


# --- form pickers (judge-form rules) ------------------------------------------------

def _ids(candidates):
    return {rule_id for rule_id, _ in candidates}


def _structured_shares(m, body):
    _, tail = m._split_at_offset(body)
    filled = [ln for ln in tail if ln.strip()]
    tables = sum(1 for ln in filled if m._is_table_row(ln))
    items = sum(1 for ln in filled if m._is_list_item(ln))
    return tables / len(filled), items / len(filled)


def test_form_tail_picker_nominates_telegraphic_body():
    m = wr()
    found = dict(m._tail_candidates(ff.TELEGRAPHIC_BODY))
    assert set(found) == {"how-3", "say-4"}
    hits = found["how-3"]
    assert 1 < len(hits) <= m._MAX_HIT_LINES
    assert all(h in ff.TELEGRAPHIC_BODY for h in hits)
    assert all(m._is_table_row(h) or m._is_list_item(h) for h in hits)
    positions = [ff.TELEGRAPHIC_BODY.index(h) for h in hits]
    assert positions == sorted(positions)
    assert positions[-1] - positions[0] > len(ff.TELEGRAPHIC_BODY) // 4


def test_form_header_picker_nominates_unitless_numeric_column():
    m = wr()
    assert m._header_candidates(ff.TELEGRAPHIC_BODY) == [("say-4", ["| Stage | Value | Note |"])]


def test_form_estimate_picker_nominates_buried_estimate():
    m = wr()
    body = ff.ESTIMATE_BODY
    offset = m._tail_offset(body)
    assert re.search(r"\*\*[^*\n]*\d+ (?:GB|hours)[^*\n]*\*\*", body[:offset])
    assert body.index(ff.ESTIMATE_ANSWER_HEADING) / len(body) > ff.ESTIMATE_ANSWER_MIN_POSITION
    found = m._estimate_candidates(body)
    assert _ids(found) == {"say-15"}
    hits = found[0][1]
    assert hits and len(hits) <= m._MAX_HIT_LINES
    for hit in hits:
        assert hit in body
        assert body.index(hit) >= offset
        assert m._unit_numbers(hit)
    assert m._tail_candidates(body) == [] and m._header_candidates(body) == []


def _long_with(extra: str) -> str:
    return ff.PROSE_BODY + extra


def _unit_header_body(header_cell: str) -> str:
    table = f"\n| Item | {header_cell} |\n|---|---|\n| a | 12 |\n| b | 45 |\n| c | 78 |\n"
    return _long_with(table)


def _lead_structured_body() -> str:
    items = [f"- {s}" for s in ff.prose_lines(0, 12)]
    lead = "\n".join(items) + "\n"
    rest = "\n".join(ff.long_prose_line(i) for i in range(7)) + "\n"
    return lead + rest


def _estimate_with_table_lead() -> str:
    return "| a | b |\n|---|---|\n| x | y |\n\n" + ff.ESTIMATE_BODY


def _estimate_below_count() -> str:
    drop = ("Storage grows", "A run of 14", "The final table")
    return "\n".join(ln for ln in ff.ESTIMATE_BODY.splitlines() if not ln.startswith(drop)) + "\n"


NEGATIVES = {
    "prose": lambda: ff.PROSE_BODY,
    "short-telegraphic": lambda: "".join(f"- item {n}\n| a | {n} |\n" for n in range(100)),
    "structured-only-in-lead": _lead_structured_body,
    "header-comma": lambda: _unit_header_body("Value, total"),
    "header-paren": lambda: _unit_header_body("Value (total)"),
    "header-percent": lambda: _unit_header_body("Value %"),
    "header-unit-word": lambda: _unit_header_body("Size GB"),
    "non-numeric-column": lambda: _long_with("\n| Item | Label |\n|---|---|\n| a | red |\n| b | blue |\n| c | green |\n"),
    "no-table": lambda: ff.PROSE_BODY + "\nplain closing line\n",
    "estimate-lead-table": _estimate_with_table_lead,
    "estimate-below-count": _estimate_below_count,
}


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_form_pickers_silent_on_negatives(name):
    m = wr()
    body = NEGATIVES[name]()
    if name == "short-telegraphic":
        assert len(body) < m.FORM_MIN_BODY_CHARS
    if name == "estimate-below-count":
        assert m._unit_numbers(body) < m.ESTIMATE_MIN_UNIT_NUMBERS
    if name == "structured-only-in-lead":
        lead, tail = m._split_at_offset(body)
        assert any(m._is_list_item(ln) for ln in lead) and not any(m._is_list_item(ln) for ln in tail)
    assert m.find_form_candidates(body) == []


def test_form_threshold_boundaries(monkeypatch):
    m = wr()
    # share: 5 of 10 non-blank tail lines is not above FORM_TAIL_SHARE, 6 of 10 is
    plain = [ff.long_prose_line(i) for i in range(6)]
    five = ff.assemble([f"- point {n}" for n in range(5)] + plain[:5])
    six = ff.assemble([f"- point {n}" for n in range(6)] + plain[:4])
    assert m._tail_candidates(five) == []
    assert _ids(m._tail_candidates(six)) == {"how-3", "say-4"}
    # length: one below / at FORM_MIN_BODY_CHARS
    for body, picker in ((ff.TELEGRAPHIC_BODY, m._tail_candidates), (ff.TELEGRAPHIC_BODY, m._header_candidates),
                         (ff.ESTIMATE_BODY, m._estimate_candidates)):
        monkeypatch.setattr(m, "FORM_MIN_BODY_CHARS", len(body) + 1)
        assert picker(body) == []
        monkeypatch.setattr(m, "FORM_MIN_BODY_CHARS", len(body))
        assert picker(body) != []
    # unit numbers: one below / at ESTIMATE_MIN_UNIT_NUMBERS
    monkeypatch.setattr(m, "FORM_MIN_BODY_CHARS", 0)
    count = m._unit_numbers(ff.ESTIMATE_BODY)
    monkeypatch.setattr(m, "ESTIMATE_MIN_UNIT_NUMBERS", count + 1)
    assert m._estimate_candidates(ff.ESTIMATE_BODY) == []
    monkeypatch.setattr(m, "ESTIMATE_MIN_UNIT_NUMBERS", count)
    assert m._estimate_candidates(ff.ESTIMATE_BODY) != []


def test_form_offset_counts_characters_not_lines():
    m = wr()
    items = [f"- {'point':<36}" for _ in range(35)]
    prose = [ff.long_prose_line(i) for i in range(6)]
    body = "\n".join(items + prose) + "\n"
    assert len(body) >= m.FORM_MIN_BODY_CHARS
    assert m._tail_candidates(body) == []
    lines = body.splitlines()
    line_tail = [ln for ln in lines[int(len(lines) * m.FORM_TAIL_START):] if ln.strip()]
    share = sum(1 for ln in line_tail if m._is_list_item(ln)) / len(line_tail)
    assert share > m.FORM_TAIL_SHARE


@pytest.mark.parametrize("gate_off", [False, True])
@pytest.mark.parametrize("case", [
    "", "x", "|", "|" * 7000, "|\n" * 4000, "no trailing newline", "- ", "\n\n\n",
    "prose\n" * 1500 + "| a |\n|---|",
    "prose\n" * 1500 + "| a | b |\n|---|---|\n",
    "prose\n" * 1500 + "| a | b |\n|---|---|\n| 1 |",
])
def test_form_pickers_never_raise(case, gate_off, monkeypatch):
    m = wr()
    if gate_off:
        monkeypatch.setattr(m, "FORM_MIN_BODY_CHARS", 0)
    for hit_rule, hits in m.find_all_candidates(case):
        assert all(h in case for h in hits), hit_rule


def test_form_fixtures_lexically_silent():
    m = wr()
    for body in (ff.TELEGRAPHIC_BODY, ff.ESTIMATE_BODY, ff.PROSE_BODY):
        assert m.find_candidates(body) == []
        assert len(body) >= m.FORM_MIN_BODY_CHARS


def test_form_fixture_nominations():
    m = wr()
    assert _ids(m.find_all_candidates(ff.TELEGRAPHIC_BODY)) == {"how-3", "say-4"}
    assert _ids(m.find_all_candidates(ff.ESTIMATE_BODY)) == {"say-15"}
    assert m.find_all_candidates(ff.PROSE_BODY) == []


def test_form_registry_status_judge_form(tmp_path):
    m = wr()
    by_id = {r.id: r for r in m.load_registry()}
    for rule_id in ("how-3", "say-4", "say-15"):
        assert by_id[rule_id].status == "judge-form", rule_id
        assert not by_id[rule_id].literals and not by_id[rule_id].patterns
    assert m.sync_problems() == []
    text = m.REGISTRY_PATH.read_text(encoding="utf-8")
    bogus = tmp_path / "bogus.toml"
    bogus.write_text(text.replace('\nstatus = "judge-form"', '\nstatus = "bogus"', 1), encoding="utf-8")
    assert any("unknown status" in p for p in m.sync_problems(registry_path=bogus))
    literal = tmp_path / "literal.toml"
    literal.write_text(text.replace('id = "say-4"\n', 'id = "say-4"\nliterals = ["zzz"]\n', 1), encoding="utf-8")
    assert any("judge-form entry carries" in p for p in m.sync_problems(registry_path=literal))
    assert "judge-form" in text.split("[[rule]]", 1)[0]


def test_form_find_all_merges_rule_ids(monkeypatch):
    m = wr()
    found = m.find_all_candidates(ff.TELEGRAPHIC_BODY)
    ids = [rule_id for rule_id, _ in found]
    assert len(ids) == len(set(ids))
    say4 = dict(found)["say-4"]
    assert say4[0] == "| Stage | Value | Note |"
    assert len(say4) <= m._MAX_HIT_LINES and len(set(say4)) == len(say4)
    mixed = m.find_all_candidates("по вашему выбору\n" + ff.TELEGRAPHIC_BODY)
    assert {"say-13", "how-3", "say-4"} <= _ids(mixed)
    monkeypatch.setattr(m, "find_candidates", lambda body: [("say-4", ["lexical hit", "| Stage | Value | Note |"])])
    merged = dict(m.find_all_candidates(ff.TELEGRAPHIC_BODY))["say-4"]
    assert merged[0] == "lexical hit" and merged.count("| Stage | Value | Note |") == 1


def test_form_tail_share_split():
    m = wr()
    tables, items = _structured_shares(m, ff.TELEGRAPHIC_BODY)
    assert tables <= m.FORM_TAIL_SHARE and items <= m.FORM_TAIL_SHARE
    assert tables + items > m.FORM_TAIL_SHARE


@pytest.mark.parametrize("name", sorted(ff.FORM_BLINDS))
def test_form_mutation_catalogue(name, monkeypatch):
    m = wr()
    attribute, replacement, pickers = ff.FORM_BLINDS[name]
    for picker in pickers:
        body = getattr(ff, ff.PICKER_FIXTURE[picker])
        assert getattr(m, picker)(body), (name, picker, "fixture does not fire unblinded")
        assert getattr(m, picker)(ff.PROSE_BODY) == []
    with monkeypatch.context() as mp:
        mp.setattr(m, attribute, replacement)
        for picker in pickers:
            body = getattr(ff, ff.PICKER_FIXTURE[picker])
            assert getattr(m, picker)(body) == [], (name, picker)
            assert getattr(m, picker)(ff.PROSE_BODY) == []


def test_form_control_kills_every_blind():
    assert control.run("picker", sorted(ff.FORM_BLINDS), ff) == control.KILLED


def test_form_control_survived_on_noop_blind(monkeypatch):
    m = wr()
    noop = ("FORM_TAIL_SHARE", m.FORM_TAIL_SHARE, ("_tail_candidates",))
    monkeypatch.setitem(ff.FORM_BLINDS, "noop", noop)
    assert control.run("picker", ["noop"], ff) == control.SURVIVED
    assert control.run("picker", ["share", "noop"], ff) == control.SURVIVED


def test_form_control_vacuous_on_silent_fixture(monkeypatch):
    monkeypatch.setitem(ff.PICKER_FIXTURE, "_tail_candidates", "PROSE_BODY")
    assert control.run("picker", ["share"], ff) == control.VACUOUS


def test_form_control_setup_error_on_missing_attribute(monkeypatch):
    monkeypatch.setitem(ff.FORM_BLINDS, "ghost", ("NO_SUCH_ATTRIBUTE", 0, ("_tail_candidates",)))
    assert control.run("picker", ["ghost"], ff) == control.SETUP_ERROR


def test_form_calibration_on_repo_prose(capsys):
    m = wr()
    root = Path(m.__file__).resolve().parents[2]
    paths = set(root.glob("docs/**/*.md")) | {root / "README.md"}
    paths |= set(root.glob("skills/**/SKILL.md")) | set(root.glob("memory-global/leaves/**/*.md"))
    bodies = []
    for path in sorted(paths):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if len(text) >= m.FORM_MIN_BODY_CHARS:
                bodies.append(text)
    assert len(bodies) >= 20
    rates = {}
    for label, picker in (("tail", m._tail_candidates), ("header", m._header_candidates),
                          ("estimate", m._estimate_candidates)):
        fired = sum(1 for body in bodies if picker(body))
        rates[label] = fired / len(bodies)
        assert rates[label] <= 0.5, (label, rates[label])
    with capsys.disabled():
        print(f"\nCalibration: tail {rates['tail']:.0%}, header {rates['header']:.0%}, "
              f"estimate {rates['estimate']:.0%} of {len(bodies)} repo prose bodies")
    doc = m.find_form_candidates.__doc__
    assert re.search(r"^\s*Calibration: tail \d+%, header \d+%, estimate \d+% of \d+ repo prose bodies", doc, re.M)
