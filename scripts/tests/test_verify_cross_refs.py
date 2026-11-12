"""verify-cross-refs: an inline pytest node id is checked by the file it names."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "verify-cross-refs.py"
_spec = importlib.util.spec_from_file_location("verify_cross_refs", SCRIPT)
cross_refs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cross_refs)


def broken(line: str) -> list[tuple[int, str]]:
    return cross_refs.check_inline_paths(Path("doc.md"), [line])


def test_node_id_of_an_existing_test_file_is_not_broken():
    assert broken("`scripts/tests/test_verify_cross_refs.py::test_anything`") == []


def test_node_id_of_a_missing_test_file_is_broken():
    assert broken("`scripts/tests/test_no_such_file.py::test_anything`") == [
        (1, "`scripts/tests/test_no_such_file.py::test_anything`")]


def test_plain_missing_path_is_still_broken():
    assert broken("`scripts/no_such_script.py`") == [(1, "`scripts/no_such_script.py`")]
