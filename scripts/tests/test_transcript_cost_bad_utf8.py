import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.transcript_cost import iter_jsonl


def test_iter_jsonl_skips_undecodable_line(tmp_path, capsys):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'{"a": 1}\n' + b'{"x": "\xd1\x28"}\n' + b'{"b": 2}\n')
    assert list(iter_jsonl(p)) == [{"a": 1}, {"b": 2}]
    err = capsys.readouterr().err
    assert str(p) in err and "1" in err


def test_iter_jsonl_valid_file_is_silent(tmp_path, capsys):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'{"a": 1}\n')
    assert list(iter_jsonl(p)) == [{"a": 1}]
    assert capsys.readouterr().err == ""
