"""Tests for scripts/lexical-meaning-inventory.py and its committed record."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "lexical-meaning-inventory.py"
BASE_REV = "a52127e8"

_spec = importlib.util.spec_from_file_location("lexical_meaning_inventory", TOOL)
inv = importlib.util.module_from_spec(_spec)
sys.modules["lexical_meaning_inventory"] = inv
_spec.loader.exec_module(inv)

LEAF_HEAD = "# Leaf\n\n## Guidance\n\n### Open violations\n\n"


def _record() -> dict:
    return tomllib.loads((ROOT / inv.RECORD).read_text(encoding="utf-8"))["sites"]


def _tree(tmp_path: Path, files: dict[str, str], record: str = "", leaf: str = "") -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    if record:
        rec = tmp_path / inv.RECORD
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(record, encoding="utf-8")
    if leaf:
        lf = tmp_path / inv.LEAF
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.write_text(leaf, encoding="utf-8")
    return tmp_path


def _sites(tmp_path: Path, source: str) -> dict[str, list[int]]:
    root = _tree(tmp_path, {"scripts/a.py": source})
    sites, broken = inv.enumerate_sites(root)
    assert broken == []
    return sites


def _entry(key: str, cls: str, hits: int = 1, extra: str = "") -> str:
    return f'[sites."{key}"]\nclass = "{cls}"\nground = "fixture ground"\nhits = {hits}\n{extra}\n'


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *args],
        capture_output=True, text=True,
    )


def test_inventory_check_passes_on_this_tree():
    assert inv.check(ROOT) == []
    done = subprocess.run(
        [sys.executable, str(TOOL), "--check"], cwd=ROOT, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout


def test_unclassified_site_fails_check(tmp_path):
    root = _tree(tmp_path, {"scripts/a.py": "import re\nre.search('x', 'y')\n"})
    done = _run(root, "--check")
    assert done.returncode == 1
    assert "unclassified: scripts/a.py::<module>::re.search" in done.stdout.splitlines()


def test_count_change_fails_check(tmp_path):
    root = _tree(
        tmp_path,
        {"scripts/a.py": "import re\nre.search('x', 'y')\nre.search('z', 'y')\n"},
        _entry("scripts/a.py::<module>::re.search", "structural", hits=1),
    )
    done = _run(root, "--check")
    assert done.returncode == 1
    assert "count-changed: scripts/a.py::<module>::re.search" in done.stdout.splitlines()


def test_stale_key_fails_check(tmp_path):
    root = _tree(
        tmp_path,
        {"scripts/a.py": "x = 1\n"},
        _entry("scripts/a.py::gone::re.match", "structural"),
    )
    done = _run(root, "--check")
    assert done.returncode == 1
    assert "stale: scripts/a.py::gone::re.match" in done.stdout.splitlines()


def test_compiled_pattern_method_call_is_enumerated(tmp_path):
    sites = _sites(
        tmp_path,
        "import re\nP = re.compile('x')\n\ndef f(s):\n    return P.match(s), P.sub('', s)\n",
    )
    assert sites["scripts/a.py::<module>::re.compile"] == [2]
    assert sites["scripts/a.py::f::pattern.match"] == [5]
    assert sites["scripts/a.py::f::pattern.sub"] == [5]


def test_overlap_helper_call_is_enumerated(tmp_path):
    sites = _sites(
        tmp_path,
        "from lib.lexical import nominate\nimport lib.lexical as lexical\n\n"
        "def f(a, b):\n    nominate(a, b)\n    lexical.term_score(a, b)\n    lexical.tokenize(a)\n",
    )
    assert sites["scripts/a.py::f::lexical.nominate"] == [5]
    assert sites["scripts/a.py::f::lexical.term_score"] == [6]
    assert sites["scripts/a.py::f::lexical.tokenize"] == [7]


def test_importlib_loaded_module_helper_call_is_enumerated(tmp_path):
    sites = _sites(
        tmp_path,
        "import importlib.util\n\ndef f(m, t, h, ts):\n    m.tokenize(t)\n    return m.term_score(h, ts)\n",
    )
    assert sites["scripts/a.py::f::lexical.tokenize"] == [4]
    assert sites["scripts/a.py::f::lexical.term_score"] == [5]


def test_aliased_import_is_resolved(tmp_path):
    sites = _sites(
        tmp_path,
        "import re as r\nfrom re import search as s\nimport difflib as d\n\n"
        "def f(x):\n    r.match('a', x)\n    s('a', x)\n    d.SequenceMatcher(None, x, x).ratio()\n",
    )
    assert sites["scripts/a.py::f::re.match"] == [6]
    assert sites["scripts/a.py::f::re.search"] == [7]
    assert sites["scripts/a.py::f::difflib.SequenceMatcher"] == [8]
    assert sites["scripts/a.py::f::difflib.ratio"] == [8]


def test_str_split_not_enumerated_unless_receiver_is_compiled(tmp_path):
    sites = _sites(
        tmp_path,
        "import re\nSEP = re.compile(',')\n\ndef f(s):\n    s.split(',')\n    return SEP.split(s)\n",
    )
    assert "scripts/a.py::f::pattern.split" in sites
    assert sites["scripts/a.py::f::pattern.split"] == [6]
    assert not any(k.endswith("::split") for k in sites)


def test_meaning_decision_needs_open_violation(tmp_path):
    key = "scripts/a.py::f::re.search"
    source = {"scripts/a.py": "import re\n\ndef f(x):\n    return re.search('thanks', x)\n"}
    bare = _tree(tmp_path / "bare", source, _entry(key, "meaning-decision"), LEAF_HEAD)
    assert f"meaning-decision-without-open-violation: {key}" in inv.check(bare)

    ref = 'open_violation = "decides gratitude by keyword"'
    bullet = "- `scripts/a.py` `f`: decides gratitude by keyword with no judge.\n"
    named = _tree(
        tmp_path / "named", source, _entry(key, "meaning-decision", extra=ref), LEAF_HEAD + bullet
    )
    assert inv.check(named) == []

    wrong = _tree(
        tmp_path / "wrong",
        source,
        _entry(key, "meaning-decision", extra=ref),
        LEAF_HEAD + "- `scripts/a.py` `g`: decides gratitude by keyword.\n",
    )
    assert f"meaning-decision-without-open-violation: {key}" in inv.check(wrong)


def test_mixed_hit_key_takes_strictest_class(tmp_path):
    key = "scripts/a.py::f::re.search"
    source = {
        "scripts/a.py": "import re\n\ndef f(x):\n    re.search('^v1', x)\n    return re.search('thanks', x)\n"
    }
    ref = 'open_violation = "decides gratitude"'
    bullet = "- `scripts/a.py` `f`: decides gratitude.\n"
    low = _tree(
        tmp_path / "low",
        source,
        _entry(key, "structural", hits=2, extra='hit_classes = ["structural", "meaning-decision"]'),
        LEAF_HEAD + bullet,
    )
    done = _run(low, "--check")
    assert done.returncode == 1
    assert f"class-not-strictest: {key}" in done.stdout.splitlines()

    ok = _tree(
        tmp_path / "ok",
        source,
        _entry(
            key, "meaning-decision", hits=2,
            extra='hit_classes = ["structural", "meaning-decision"]\n' + ref,
        ),
        LEAF_HEAD + bullet,
    )
    assert _run(ok, "--check").returncode == 0

    short = _tree(
        tmp_path / "short",
        source,
        _entry(key, "meaning-decision", hits=2, extra='hit_classes = ["meaning-decision"]\n' + ref),
        LEAF_HEAD + bullet,
    )
    assert f"hit-classes-mismatch: {key}" in inv.check(short)


def test_nested_non_test_file_enumerated_and_tests_excluded(tmp_path):
    body = "import re\nre.search('x', 'y')\n"
    root = _tree(
        tmp_path,
        {
            "scripts/lib/deep/a.py": body,
            "scripts/tests/test_a.py": body,
            "scripts/lib/tests/b.py": body,
        },
    )
    sites, _ = inv.enumerate_sites(root)
    assert list(sites) == ["scripts/lib/deep/a.py::<module>::re.search"]


def test_python_file_outside_scripts_is_enumerated(tmp_path):
    root = _tree(tmp_path, {"tools/x.py": "import re\nre.fullmatch('x', 'y')\n"})
    sites, _ = inv.enumerate_sites(root)
    assert "tools/x.py::<module>::re.fullmatch" in sites


def test_every_entry_has_class_and_non_empty_ground():
    for key, entry in _record().items():
        assert entry.get("class") in inv.CLASSES, key
        assert isinstance(entry.get("ground"), str) and entry["ground"].strip(), key


def test_converted_sites_are_not_meaning_decisions():
    record = _record()
    sites, _ = inv.enumerate_sites(ROOT)

    keys = set(sites) | set(record)

    def under(qualname: str):
        return [k for k in keys if k.split("::")[1].split(".")[0] == qualname]

    base_keys = {
        "scripts/cost-report.py::<module>::re.compile",
        "scripts/cost-report.py::parse_transcripts::pattern.search",
        "scripts/record-experience.py::_similarity::lexical.term_score",
        "scripts/record-experience.py::_similarity::lexical.tokenize",
    }
    assert not base_keys & keys
    for qual in (
        "cluster_by_ground", "classify_and_score", "cluster_records",
        "measure_condition_a", "measure_cheap_c", "_board_ground_match",
    ):
        assert under(qual) == [], qual
    assert not [
        k for k in keys
        if k.startswith("scripts/record-experience.py::cmd_new::") and "::lexical." in k
    ]

    def cls(key):
        return record[key]["class"]

    assert cls("scripts/si_feedback_detect.py::find_signals::pattern.search") == "candidate-generation"
    fd = [k for k in sites if k.startswith("scripts/file-difficulty.py::") and "::lexical." in k]
    assert fd and all(k.endswith("::lexical.nominate") for k in fd)
    assert all(cls(k) == "candidate-generation" for k in fd)

    for key, entry in record.items():
        if key.startswith(
            ("scripts/policy-scorecard.py::", "scripts/lib/prompt_judges.py::", "scripts/agentctl/advisor.py::")
        ):
            hit_classes = entry.get("hit_classes") or [entry["class"]]
            assert "meaning-decision" not in hit_classes, key
            assert entry["class"] != "meaning-decision", key
            assert entry.get("reviewed_by"), key

    shared = [
        "scripts/policy-scorecard.py::<module>::re.compile",
        "scripts/policy-scorecard.py::_scan_session::pattern.search",
    ]
    for key in shared:
        assert record[key]["hit_classes"].count("structural") >= 3, key
        for name in ("_MECH_BASH_FIRST", "_CURL_POLL", "SUBAGENT_FAIL_RE"):
            assert name in record[key]["ground"], (key, name)
    tree = ast.parse((ROOT / "scripts/policy-scorecard.py").read_text(encoding="utf-8"))
    compiled = {
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and ast.unparse(n.value.func) == "re.compile"
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert {"_MECH_BASH_FIRST", "_CURL_POLL", "SUBAGENT_FAIL_RE"} <= compiled

    nominate = [k for k in sites if k.endswith("::lexical.nominate")]
    assert nominate
    allowed = ("scripts/lib/semantic_join.py::", "scripts/file-difficulty.py::")
    assert all(k.startswith(allowed) for k in nominate)
    assert any(k.startswith("scripts/lib/semantic_join.py::") for k in nominate)
    assert all(cls(k) == "candidate-generation" for k in nominate)


def _base_registry() -> list[dict]:
    raw = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{BASE_REV}:scripts/crutch_registry.toml"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = tomllib.loads(raw)
    for value in data.values():
        if isinstance(value, list):
            return value
    raise AssertionError("base registry has no entry list")


def _crutch_inventory():
    spec = importlib.util.spec_from_file_location("crutch_inventory", ROOT / "scripts/crutch-inventory.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crutch_inventory"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_registry_regex_files_have_inventory_keys():
    ci = _crutch_inventory()
    sites, _ = inv.enumerate_sites(ROOT)
    keyed_files = {k.split("::")[0] for k in sites}
    regex_files = {
        s.file
        for s in ci.enumerate_code_sites(ROOT)
        if s.pattern_source and not inv._is_test_path(Path(s.file))
    }
    assert regex_files - keyed_files == set()


def test_reviewed_entries_carry_reviewer():
    ci = _crutch_inventory()
    base = _base_registry()
    structural_ids = {
        e["id"] for e in base if e.get("disposition", e.get("class")) == "structural"
    }
    for key, entry in _record().items():
        path, _, rest = key.partition("::")
        qual = rest.rpartition("::")[0]
        base_id = ci._stable_id("code", path, qual)
        if entry["class"] != "structural" or base_id not in structural_ids:
            assert entry.get("reviewed_by"), key
