"""scripts/inline-size-calibrate.py: the p90 rule, population filters, dedup, and --check / --recompute."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "inline-size-calibrate.py"
_spec = importlib.util.spec_from_file_location("inline_size_calibrate", SCRIPT)
calib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calib)

KEY = calib.KEY
BELOW_MIN_N = calib.MIN_N - 1
N_B = 5
N_A_AT_MIN_N = calib.MIN_N - N_B
NAMED_VALUE = calib.PREVIOUS_BOUND + 1
DECIDED_ON = "2026-10-26"


def _git(repo, *argv):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _write(repo, rel, lines, binary=False):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(b"\0\1\2" * lines)
    else:
        path.write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")


def _sha(i):
    return f"{i:040x}"


def _rows(n_a, n_b, lines=lambda i: i + 1):
    rows = [calib._row("A", 100 + i, _sha(i), _sha(1000 + i), lines(i)) for i in range(n_a)]
    rows += [calib._row("B", f"session-{i}", _sha(2000 + i), _sha(3000 + i), lines(n_a + i)) for i in range(n_b)]
    return rows


# ---- the rule

def test_p90_index_rounds_up_where_floor_would_not():
    values = list(range(1, 13))
    assert round(0.9 * 11) == 10 and int(0.9 * 11) == 9
    assert calib.p90(values) == 11
    assert calib.candidate(values) == 11


def test_p90_is_the_last_value_for_small_n_and_none_for_empty():
    assert calib.p90([7]) == 7
    assert calib.p90([]) is None
    assert calib.candidate([]) is None


def test_assemble_below_min_n_is_a_pending_user_decision():
    doc = calib.assemble(_rows(BELOW_MIN_N - 4, 4), [], {}, 0)
    assert doc["n"] == BELOW_MIN_N < calib.MIN_N
    assert doc["decision"] == "user" and doc["bound_value"] is None


def test_assemble_at_min_n_derives_the_candidate():
    doc = calib.assemble(_rows(N_A_AT_MIN_N, N_B), [], {}, 0)
    assert doc["decision"] == "derived"
    assert doc["bound_value"] == calib.candidate([r["production_lines"] for r in doc["rows"]])


def test_assemble_populations_and_sensitivity_use_open_rows():
    open_rows = [calib._row("open", i, _sha(9), _sha(10), 500) for i in range(5)]
    doc = calib.assemble(_rows(N_A_AT_MIN_N, N_B), open_rows, {}, 0)
    assert doc["populations"]["A"]["n"] == N_A_AT_MIN_N and doc["populations"]["B"]["n"] == N_B
    assert doc["populations"]["P"]["n"] == calib.MIN_N
    assert doc["sensitivity"]["largest_source_share"] == round(N_A_AT_MIN_N / calib.MIN_N, 4)
    assert doc["sensitivity"]["p90_A_plus_open_rows"] == 500


# ---- --check

def _world(tmp_path, n_a=N_A_AT_MIN_N, n_b=N_B, decision=None):
    open_rows = [calib._row("open", 7, _sha(9), _sha(10), 120)]
    doc = calib.assemble(_rows(n_a, n_b), open_rows, {"A": calib._skipped_block([]), "B": calib._skipped_block([])},
                         0, decision)
    root = tmp_path / "root"
    (root / "docs" / "operations").mkdir(parents=True)
    cal = root / calib.REL_PATH
    cal.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_config(root, doc)
    return root, cal, doc


def _write_config(root, doc, rationale_text=None):
    bound = doc["bound_value"]
    (root / "config.md").write_text(
        f"| `other` | `1` | x |\n| `{KEY}` | `{bound}` | Bound; see `{calib.REL_PATH}`. |\n", encoding="utf-8")
    basis = rationale_text if rationale_text is not None else calib.basis_text(doc)
    (root / "docs" / "operations" / "config-rationale.md").write_text(
        f"| `other` | `1` | y |\n| `{KEY}` | `{bound}` | The class. {basis} |\n", encoding="utf-8")


def _rewrite(cal, mutate):
    doc = json.loads(cal.read_text(encoding="utf-8"))
    mutate(doc)
    cal.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def test_check_passes_on_a_derived_world(tmp_path):
    root, cal, _ = _world(tmp_path)
    assert calib.check(cal, root) == []


@pytest.mark.parametrize("option", [1, 2, 3, 4])
def test_check_passes_on_each_user_option(tmp_path, option):
    lines = [r["production_lines"] for r in _rows(BELOW_MIN_N, 0)]
    value = {1: calib.candidate(lines), 2: calib.candidate(lines + [120]), 3: calib.PREVIOUS_BOUND,
             4: NAMED_VALUE}[option]
    decision = {"option": option, "user_value": value, "decided_on": DECIDED_ON}
    root, cal, _ = _world(tmp_path, n_a=BELOW_MIN_N, n_b=0, decision=decision)
    assert calib.check(cal, root) == []


def test_check_fails_on_a_tampered_bound_value(tmp_path):
    root, cal, _ = _world(tmp_path)
    _rewrite(cal, lambda d: d.update(bound_value=99999))
    assert calib.check(cal, root)


def test_check_fails_on_a_tampered_row(tmp_path):
    root, cal, _ = _world(tmp_path)
    _rewrite(cal, lambda d: d["rows"][0].update(production_lines=d["rows"][0]["production_lines"] + 50))
    assert any("inputs_sha256" in d for d in calib.check(cal, root))


def test_check_fails_on_an_extra_row_key(tmp_path):
    root, cal, _ = _world(tmp_path)
    _rewrite(cal, lambda d: d["rows"][0].update(title="private goal text"))
    assert any("keys differ" in d for d in calib.check(cal, root))


def test_check_fails_on_a_tampered_open_row(tmp_path):
    root, cal, _ = _world(tmp_path)
    _rewrite(cal, lambda d: d["sensitivity"]["open_rows"]["rows"][0].update(production_lines=1))
    assert any("open_rows sha256" in d for d in calib.check(cal, root))


def test_check_fails_when_config_md_holds_another_value(tmp_path):
    root, cal, doc = _world(tmp_path)
    (root / "config.md").write_text(
        f"| `{KEY}` | `{doc['bound_value'] + 1}` | see `{calib.REL_PATH}` |\n", encoding="utf-8")
    assert any("config.md" in d for d in calib.check(cal, root))


def test_check_fails_when_config_md_does_not_point_to_the_json(tmp_path):
    root, cal, doc = _world(tmp_path)
    (root / "config.md").write_text(f"| `{KEY}` | `{doc['bound_value']}` | a bound |\n", encoding="utf-8")
    assert any("does not name" in d for d in calib.check(cal, root))


RATIONALE_WORD_NAMES = ("path", "inputs_sha256", "formula", "A n=", "B n=", "P n=", "decision",
                        "p90(A)", "p90(B)", "p90(A plus open PRs)")


@pytest.mark.parametrize("missing", RATIONALE_WORD_NAMES)
def test_check_fails_when_the_rationale_row_lacks_a_word(tmp_path, missing):
    root, cal, doc = _world(tmp_path)
    words = dict(zip(RATIONALE_WORD_NAMES, calib.rationale_words(doc)))
    text = calib.basis_text(doc).replace(words[missing], "")
    _write_config(root, doc, text)
    assert any("config-rationale.md" in d for d in calib.check(cal, root))


def test_check_fails_on_a_wrong_population_label(tmp_path):
    n_a, n_b = calib.MIN_N - 8, 8
    root, cal, doc = _world(tmp_path, n_a=n_a, n_b=n_b)
    text = (calib.basis_text(doc).replace(f"A n={n_a}", "A n=@@").replace(f"B n={n_b}", f"B n={n_a}")
            .replace("A n=@@", f"A n={n_b}"))
    _write_config(root, doc, text)
    diffs = calib.check(cal, root)
    assert any(f"'A n={n_a}'" in d for d in diffs) and any(f"'B n={n_b}'" in d for d in diffs)


def test_check_fails_when_the_rationale_value_differs(tmp_path):
    root, cal, doc = _world(tmp_path)
    path = root / "docs" / "operations" / "config-rationale.md"
    path.write_text(path.read_text(encoding="utf-8").replace(f"`{doc['bound_value']}`", "`1`", 1), encoding="utf-8")
    assert any("value is not" in d for d in calib.check(cal, root))


@pytest.mark.parametrize("field,value", [("formula", "p50"), ("min_n", calib.MIN_N + 1), ("rule", "other")])
def test_check_fails_when_a_constant_differs_from_the_scripts(tmp_path, field, value):
    root, cal, _ = _world(tmp_path)
    _rewrite(cal, lambda d: d.update({field: value}))
    assert any(field in d for d in calib.check(cal, root))


def test_check_fails_on_derived_below_min_n(tmp_path):
    decision = {"option": 3, "user_value": calib.PREVIOUS_BOUND, "decided_on": DECIDED_ON}
    root, cal, _ = _world(tmp_path, n_a=BELOW_MIN_N, n_b=0, decision=decision)
    _rewrite(cal, lambda d: d.update(decision="derived"))
    assert any("requires 'user'" in d for d in calib.check(cal, root))


def test_check_fails_on_user_at_or_above_min_n(tmp_path):
    root, cal, _ = _world(tmp_path)
    _rewrite(cal, lambda d: d.update(decision="user"))
    assert any("requires 'derived'" in d for d in calib.check(cal, root))


def test_check_fails_when_option_1_value_is_not_the_candidate(tmp_path):
    decision = {"option": 1, "user_value": 3, "decided_on": DECIDED_ON}
    root, cal, _ = _world(tmp_path, n_a=BELOW_MIN_N, n_b=0, decision=decision)
    assert any("option 1 gives" in d for d in calib.check(cal, root))


def test_check_fails_on_option_5(tmp_path):
    decision = {"option": max(calib.OPTIONS) + 1, "user_value": calib.PREVIOUS_BOUND, "decided_on": DECIDED_ON}
    root, cal, _ = _world(tmp_path, n_a=BELOW_MIN_N, n_b=0, decision=decision)
    assert any("outside" in d for d in calib.check(cal, root))


def test_check_fails_on_a_non_iso_decided_on(tmp_path):
    decision = {"option": 3, "user_value": calib.PREVIOUS_BOUND, "decided_on": "yesterday"}
    root, cal, _ = _world(tmp_path, n_a=BELOW_MIN_N, n_b=0, decision=decision)
    assert any("ISO date" in d for d in calib.check(cal, root))


def test_check_reports_a_pending_decision(tmp_path):
    root, cal, _ = _world(tmp_path, n_a=BELOW_MIN_N, n_b=0)
    assert any("pending" in d for d in calib.check(cal, root))


def test_check_raises_malformed_on_unreadable_input(tmp_path):
    with pytest.raises(calib.Malformed):
        calib.check(tmp_path / "missing.json", tmp_path)


def test_main_check_exit_codes(tmp_path):
    root, cal, _ = _world(tmp_path)
    assert calib.main(["--check", "--root", str(root), "--calibration", str(tmp_path / "missing.json")]) == 2
    assert calib.main(["--check", "--root", str(root)]) == 0
    _rewrite(cal, lambda d: d.update(bound_value=99999))
    assert calib.main(["--check", "--root", str(root)]) == 1


def test_main_basis_prints_the_row_text_check_expects(tmp_path, capsys):
    root, _, doc = _world(tmp_path)
    assert calib.main(["--basis", "--root", str(root)]) == 0
    assert capsys.readouterr().out.strip() == calib.basis_text(doc)


def test_main_funnel_requires_a_snapshot():
    with pytest.raises(SystemExit) as exc:
        calib.main(["--funnel"])
    assert exc.value.code == 2


def test_basis_text_renders_a_missing_p90_as_none(tmp_path):
    _, _, doc = _world(tmp_path, n_a=calib.MIN_N, n_b=0)
    doc["sensitivity"]["p90_B"] = None
    assert "p90(B) none," in calib.basis_text(doc)
    assert "None" not in calib.basis_text(doc)


def test_check_fails_when_a_sensitivity_figure_in_the_rationale_is_stale(tmp_path):
    root, cal, doc = _world(tmp_path)
    stale = calib.basis_text(doc).replace(f"p90(A) {doc['sensitivity']['p90_A']}", "p90(A) 1", 1)
    _write_config(root, doc, stale)
    assert any("p90(A) " in d for d in calib.check(cal, root))


# ---- sessions (population B)

def _session(**over):
    state = {
        "session_id": "s", "node": "RESOLVED", "stages": [{"index": 1}],
        "history": [{"event": "approve"}, {"event": "record_landing", "head": "h", "base": "b"}],
    }
    state.update(over)
    return state


def test_session_landing_returned_for_the_clean_session():
    assert calib.session_landing(_session())["head"] == "h"


def test_session_landing_uses_the_latest_landing():
    history = [{"event": "approve"}, {"event": "record_landing", "head": "h1", "base": "b"},
               {"event": "record_landing", "head": "h2", "base": "b"}]
    assert calib.session_landing(_session(history=history))["head"] == "h2"


@pytest.mark.parametrize("over", [
    {"node": "EXECUTING"},
    {"stages": [{"index": 1}, {"index": 2}]},
    {"history": [{"event": "approve"}]},
    {"history": [{"event": "approve"}, {"event": "approve"}, {"event": "record_landing", "head": "h", "base": "b"}]},
    {"history": [{"event": "approve"}, {"event": "replan"}, {"event": "record_landing", "head": "h", "base": "b"}]},
    {"history": [{"event": "record_landing", "head": "h", "base": "b"}]},
])
def test_session_landing_none_for_an_excluded_session(over):
    assert calib.session_landing(_session(**over)) is None


def test_session_landing_allows_a_replan_before_the_approval():
    history = [{"event": "replan"}, {"event": "approve"}, {"event": "record_landing", "head": "h", "base": "b"}]
    assert calib.session_landing(_session(history=history)) is not None


# ---- git-backed measurement, filters and dedup

@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitattributes").write_text("gen/out.toml linguist-generated=true\n", encoding="utf-8")
    _write(root, "src/m.py", 1)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _merged_pr(repo, number, files, head="mandate/{n}-x", owner="o", binary=()):
    """A squash-merged PR: the branch tip T and the merge commit M on main."""
    parent = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", f"pr{number}")
    for rel, lines in files.items():
        _write(repo, rel, lines, binary=rel in binary)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", f"pr{number}")
    _git(repo, "commit", "-q", "-m", "squash")
    merge = _git(repo, "rev-parse", "HEAD")
    return {"number": number, "headRefName": head.format(n=number), "headRefOid": tip,
            "mergeCommit": {"oid": merge}, "mergedBy": {"login": owner}, "author": {"login": owner}}, parent


def test_population_a_measures_hand_written_lines_over_the_pr_head(repo):
    pr, parent = _merged_pr(repo, 1, {"src/m.py": 6, "gen/out.toml": 300, "scripts/tests/test_m.py": 40})
    rows, merge_oids, patch_ids, skipped = calib.population_a(repo, [pr], None)
    assert rows == [calib._row("A", 1, parent, pr["headRefOid"], 5)]
    assert merge_oids == {pr["mergeCommit"]["oid"]} and len(patch_ids) == 1 and skipped == []


def test_population_a_filters_by_head_pattern_and_merger(repo):
    keep, _ = _merged_pr(repo, 1, {"src/a.py": 3})
    salvage, _ = _merged_pr(repo, 2, {"src/b.py": 3}, head="fix/{n}-salvage")
    wrong_branch, _ = _merged_pr(repo, 3, {"src/c.py": 3}, head="feature/{n}")
    wrong_salvage, _ = _merged_pr(repo, 4, {"src/d.py": 3}, head="fix/{n}-other")
    other_merger, _ = _merged_pr(repo, 5, {"src/e.py": 3})
    other_merger["mergedBy"] = {"login": "someone-else"}
    rows, *_ = calib.population_a(repo, [keep, salvage, wrong_branch, wrong_salvage, other_merger], None)
    assert [r["id"] for r in rows] == [1, 2]
    rows_owner, *_ = calib.population_a(repo, [keep, other_merger], "someone-else")
    assert [r["id"] for r in rows_owner] == [5]


def test_population_a_skips_binary_zero_line_and_unfetchable_heads(repo):
    binary, _ = _merged_pr(repo, 1, {"src/blob.py": 3}, binary=("src/blob.py",))
    zero, _ = _merged_pr(repo, 2, {"scripts/tests/test_z.py": 9})
    missing, _ = _merged_pr(repo, 3, {"src/c.py": 3})
    missing["headRefOid"] = _sha(12345)
    rows, _, _, skipped = calib.population_a(repo, [binary, zero, missing], None)
    assert rows == []
    assert sorted(skipped) == ["binary path counted", "head not in the object store or fetchable",
                               "no counted lines"]


def _write_session(snapshot, name, head, base):
    snapshot.mkdir(exist_ok=True)
    state = _session(session_id=name)
    state["history"] = [{"event": "approve"}, {"event": "record_landing", "head": head, "base": base}]
    (snapshot / f"{name}.json").write_text(json.dumps(state), encoding="utf-8")


def test_population_b_drops_a_head_equal_to_an_a_merge_commit_and_a_matching_patch_id(repo, tmp_path):
    pr, parent = _merged_pr(repo, 1, {"src/m.py": 11})
    merge = pr["mergeCommit"]["oid"]
    _git(repo, "checkout", "-q", "-b", "twin", parent)
    _write(repo, "src/m.py", 11)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "same change, other commit")
    twin = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    assert twin != pr["headRefOid"] and twin != merge
    _write(repo, "src/other.py", 4)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "distinct")
    distinct = _git(repo, "rev-parse", "HEAD")
    snapshot = tmp_path / "state"
    _write_session(snapshot, "by-head", merge, parent)
    _write_session(snapshot, "by-patch", twin, parent)
    _write_session(snapshot, "distinct", distinct, merge)
    rows_a, merge_oids, patch_ids, _ = calib.population_a(repo, [pr], None)
    rows_b, skipped, dropped = calib.population_b(repo, snapshot, merge_oids, patch_ids)
    assert dropped == 2 and skipped == []
    assert [(r["id"], r["production_lines"]) for r in rows_b] == [("distinct", 4)]
    assert len(rows_a) == 1


def test_b_funnel_counts_each_filter(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "src/m.py", 3)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    head = _git(repo, "rev-parse", "HEAD")
    snapshot = tmp_path / "state"
    _write_session(snapshot, "landed", head, base)
    (snapshot / "open.json").write_text(json.dumps(_session(session_id="open", node="EXECUTING")), encoding="utf-8")
    funnel = calib.b_funnel(repo, snapshot)
    assert (funnel["files"], funnel["sessions"], funnel["resolved"], funnel["commits_present"]) == (2, 2, 1, 1)
    assert funnel["any_record_landing_event"] == 2


def test_population_b_ignores_sessions_whose_commits_are_absent(repo, tmp_path):
    snapshot = tmp_path / "state"
    _write_session(snapshot, "ghost", _sha(1), _sha(2))
    (snapshot / "notes.json").write_text("{}", encoding="utf-8")
    (snapshot / "broken.json").write_text("not json", encoding="utf-8")
    assert calib.population_b(repo, snapshot, set(), set()) == ([], [], 0)


def test_population_b_skips_zero_line_and_binary_ranges(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "scripts/tests/test_only.py", 5)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tests only")
    tests_only = _git(repo, "rev-parse", "HEAD")
    _write(repo, "src/blob.py", 3, binary=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "binary")
    binary = _git(repo, "rev-parse", "HEAD")
    snapshot = tmp_path / "state"
    _write_session(snapshot, "zero", tests_only, base)
    _write_session(snapshot, "bin", binary, tests_only)
    rows, skipped, _ = calib.population_b(repo, snapshot, set(), set())
    assert rows == [] and sorted(skipped) == ["binary path counted", "no counted lines"]


def test_population_open_measures_against_the_trunk_merge_base(repo):
    _git(repo, "checkout", "-q", "-b", "open1")
    _write(repo, "src/m.py", 8)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "open change")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    pr = {"number": 9, "headRefName": "mandate/9-x", "headRefOid": tip}
    other = {"number": 10, "headRefName": "feature/10", "headRefOid": tip}
    rows = calib.population_open(repo, [pr, other], trunk="main")
    assert rows == [calib._row("open", 9, _git(repo, "rev-parse", "main"), tip, 7)]


def test_build_end_to_end_and_recompute(repo, tmp_path):
    pr, _ = _merged_pr(repo, 1, {"src/m.py": 6})
    doc = calib.build(repo, tmp_path / "no-state", [pr], [], None, None)
    assert doc["populations"]["A"]["n"] == 1 and doc["decision"] == "user"
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    assert calib.recompute(cal, repo) == []
    _rewrite(cal, lambda d: d["rows"][0].update(production_lines=99))
    assert any("git gives" in d for d in calib.recompute(cal, repo))
    _rewrite(cal, lambda d: d["rows"][0].update(tip=_sha(77)))
    assert any("not in the object store" in d for d in calib.recompute(cal, repo))


def test_skipped_block_counts_reasons():
    assert calib._skipped_block(["a", "b", "a"]) == {"count": 3, "reasons": {"a": 2, "b": 1}}


def test_basis_text_carries_every_word_check_demands(tmp_path):
    _, _, doc = _world(tmp_path)
    text = calib.basis_text(doc)
    assert all(word in text for word in calib.rationale_words(doc))
