"""Sync tests between tech-writer SKILL.md and publish-rules.toml.

The expected rule id set is pinned literally here, so a parser that silently
merges or drops a rule cannot make the completeness checks pass by agreeing with
itself.
"""
from __future__ import annotations

import importlib

import pytest

PINNED_IDS = {f"say-{n}" for n in range(1, 15)} | {f"how-{n}" for n in range(1, 14)}
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
        if rule.status == "perception":
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
