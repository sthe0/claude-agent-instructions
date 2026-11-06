"""Presence checks for the per-stage effort rule's documentation (order test 14).

Each test asserts that a named key, anchor, section, item label or identifier exists where a
reader looks for it. None compares a value or a wording, so tuning a key or rephrasing a
paragraph leaves them green.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config.md"
RATIONALE = ROOT / "docs" / "operations" / "config-rationale.md"
README = ROOT / "scripts" / "agentctl" / "README.md"
LEAF = ROOT / "memory-global" / "leaves" / "effort-divergence-trigger.md"
BOUNDARY_LEAF = ROOT / "memory-global" / "leaves" / "acting-without-asking.md"
PLANNER_POLICY = ROOT / "skills" / "specializations" / "planner" / "policy.md"

FLOOR_KEYS = ("effort-fire-floor-usd", "effort-fire-floor-minutes")
README_SECTION = "### Per-stage rule"


def _text(path):
    return path.read_text(encoding="utf-8")


def _key_row(path, key):
    """The table row of `key` in a config table, or None."""
    for line in _text(path).splitlines():
        if line.startswith(f"| `{key}` |"):
            return line
    return None


def _cells(row):
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _section(text, heading, level_marker):
    """The text from `heading` to the next heading of the same or a higher level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    depth = len(level_marker)
    end = re.search(rf"^#{{1,{depth}}} ", rest, flags=re.MULTILINE)
    return rest[: end.start()] if end else rest


def _readme_items():
    section = _section(_text(README), README_SECTION, "###")
    items = {}
    for paragraph in section.split("\n\n"):
        found = re.match(r"\*\*(.+?)\.\*\*", paragraph.strip())
        if found:
            items[found.group(1)] = paragraph
    return items


def _anchor_paragraph(name):
    found = re.search(rf'<a id="{re.escape(name)}"></a>\n(.+)', _text(RATIONALE))
    assert found, f"anchor {name} is missing from the config rationale"
    return found.group(1)


def _assert_names(text, names):
    missing = [name for name in names if name not in text]
    assert not missing, f"not named: {missing}"


def test_floor_keys_exist_in_config_md_with_a_basis_entry_each():
    for key in FLOOR_KEYS:
        row = _key_row(CONFIG, key)
        assert row, f"{key} has no row in config.md"
        assert _cells(row)[2], f"{key} has no one-line meaning in config.md"
        basis = _key_row(RATIONALE, key)
        assert basis, f"{key} has no entry in the config rationale"
        assert _cells(basis)[2], f"{key} has an empty basis entry"


def test_changed_config_keys_keep_their_entries():
    for key in ("review-cycle-active-minutes", "effort-divergence-multiple"):
        for path in (CONFIG, RATIONALE):
            row = _key_row(path, key)
            assert row and _cells(row)[2], f"{key} lacks an entry in {path.name}"


def test_rationale_anchor_planning_fire_point_wall_clock():
    assert _anchor_paragraph("planning-fire-point-wall-clock")


def test_rationale_anchor_floor_not_binding_planning_spend():
    assert _anchor_paragraph("floor-not-binding-planning-spend")


def test_rationale_anchor_floor_keys_not_following_tier_keys():
    assert _anchor_paragraph("floor-keys-not-following-tier-keys")


def test_anchor_planning_fire_point_wall_clock_cites_its_keys():
    _assert_names(_anchor_paragraph("planning-fire-point-wall-clock"),
                  ("review-cycle-active-minutes", "effort-divergence-multiple", "planning_estimate"))


def test_anchor_floor_not_binding_planning_spend_cites_its_keys():
    _assert_names(_anchor_paragraph("floor-not-binding-planning-spend"),
                  ("effort-fire-floor-usd", "budget-medium-usd", "effort-divergence-multiple"))


def test_anchor_floor_keys_not_following_tier_keys_names_both_tier_keys():
    _assert_names(_anchor_paragraph("floor-keys-not-following-tier-keys"),
                  FLOOR_KEYS + ("budget-large-usd", "effort-stage-minutes-large"))


def test_readme_has_a_per_stage_section():
    assert README_SECTION in _text(README)
    assert _readme_items()


def test_trigger_leaf_has_a_per_stage_section():
    text = _text(LEAF)
    assert "\n## Per-stage rule\n" in text
    assert "Superseded for these two scales by the per-stage rule" in text


def test_planner_policy_names_the_planning_estimate_and_the_lazy_field():
    text = _text(PLANNER_POLICY)
    _assert_names(text, ("planning_estimate", "active_minutes", "derivation", "lazy", "output_artifacts"))


def _label(label, *names):
    item = _readme_items().get(label)
    assert item, f"the per-stage subsection has no item labelled {label}"
    _assert_names(item, names)


def test_readme_item_label_segments():
    _label("Segments")


def test_readme_item_label_armings():
    _label("Armings", "figures", "ask")


def test_readme_item_label_judging_acts():
    _label("Judging acts")


def test_readme_item_label_spawned_planner_run_end():
    _label("Spawned planner run end")


def test_readme_item_label_planning_fires():
    _label("Planning fires", "effort-replan-absolute")


def test_readme_item_label_settlement():
    _label("Settlement")


def test_readme_item_label_floor():
    _label("Floor")


def test_readme_item_label_open_fire_gate():
    _label("Open-fire gate")


def test_readme_item_label_unfold():
    _label("Unfold", "lazy")


def test_readme_item_label_disclosure():
    _label("Disclosure", "closing line", "migrated")


def test_readme_item_label_migration():
    _label("Migration")


def test_readme_item_label_continuation():
    _label("Continuation", "check_changed")


def test_readme_disclosure_item_names_the_closing_line_and_the_migrated_flag():
    _label("Disclosure", "closing line", "migrated flag")


def test_boundary_docs_name_the_continuation_route():
    routing = _section(_text(README), "### Autonomous replanning within the approved boundary", "###")
    paragraph = next(p for p in routing.split("\n\n") if p.startswith("**Routing.**"))
    _assert_names(paragraph, ("PLAN-READY", "continuation"))
    boundary = next(line for line in _text(BOUNDARY_LEAF).splitlines()
                    if line.startswith("- **Outside the boundary**"))
    _assert_names(boundary, ("PLAN-READY", "continuation"))
