"""The ledger and transcript readers the report views share live once, in
lib/agent_report/common.py; a view module that grows a private copy fails here."""
import ast
import datetime as dt
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
PACKAGE = SCRIPTS / "lib" / "agent_report"
SHARED = (
    "sanitize_cwd",
    "parse_ts",
    "parse_ts_utc",
    "parse_ts_or_none",
    "read_jsonl_rows",
    "project_transcripts",
)

sys.path.insert(0, str(SCRIPTS))
from lib.agent_report import common  # noqa: E402


def _defining_files(name):
    return sorted(
        path.name
        for path in PACKAGE.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_each_shared_helper_is_defined_once_in_common():
    assert {name: _defining_files(name) for name in SHARED} == {
        name: ["common.py"] for name in SHARED
    }


def test_read_jsonl_rows_skips_blank_and_malformed_lines(tmp_path, capsys):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"n": 1}\n\n{not json\n{"n": 2}\n', encoding="utf-8")

    assert common.read_jsonl_rows(path) == [{"n": 1}, {"n": 2}]
    assert "malformed" in capsys.readouterr().err
    assert common.read_jsonl_rows(tmp_path / "absent.jsonl") == []


def test_parse_ts_utc_assumes_utc_for_a_naive_timestamp():
    assert common.parse_ts_utc("2026-07-01T00:00:00").tzinfo == dt.timezone.utc


def test_project_transcripts_unions_one_project_across_roots(tmp_path):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root, stem in zip(roots, ("x", "y")):
        (root / "proj").mkdir(parents=True)
        (root / "proj" / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")

    assert [p.stem for p in common.project_transcripts("proj", roots)] == ["x", "y"]
