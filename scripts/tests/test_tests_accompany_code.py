"""The tests-accompany-code advisory guard: warns when staged code lacks a
test, silent when a test rides along / on the escape trailer / on non-code."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_tests_accompany_code",
    Path(__file__).resolve().parents[1] / "verify-tests-accompany-code.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


# --- classification helpers ---------------------------------------------------

def test_is_code_and_is_test():
    assert mod.is_code("scripts/agentctl/cli.py")
    assert not mod.is_code("scripts/tests/test_cli.py")      # test file
    assert not mod.is_code("scripts/agentctl/cli.py".replace(".py", ".md"))  # not .py
    assert not mod.is_code("docs/readme.py")                 # not under scripts/
    assert mod.is_test("scripts/tests/test_x.py")
    assert mod.is_test("scripts/test_helpers.py")            # basename test_*


# --- evaluate: the pure core --------------------------------------------------

def test_warns_when_code_without_test():
    w = mod.evaluate(["scripts/agentctl/cli.py"], "fix the strand")
    assert w is not None
    assert "no accompanying test" in w


def test_silent_when_test_rides_along():
    w = mod.evaluate(
        ["scripts/agentctl/cli.py", "scripts/tests/test_cli_directives.py"],
        "fix + test",
    )
    assert w is None


def test_escape_trailer_suppresses():
    w = mod.evaluate(
        ["scripts/agentctl/cli.py"],
        "rename only\n\n[skip-test-guard: pure rename, no behavior change]",
    )
    assert w is None


def test_silent_on_non_code_change():
    w = mod.evaluate(["memory-global/leaves/x.md", "scripts/README.md"], "docs")
    assert w is None


def test_silent_on_empty_stage():
    assert mod.evaluate([], "anything") is None


def test_preview_truncates_many_files():
    files = [f"scripts/m{i}.py" for i in range(7)]
    w = mod.evaluate(files, "big change")
    assert w is not None and "…" in w


# --- main is always advisory (exit 0) -----------------------------------------

def test_main_returns_zero_even_when_warning(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(mod, "_staged_files", lambda: ["scripts/agentctl/cli.py"])
    msg = tmp_path / "MSG"
    msg.write_text("fix something\n", encoding="utf-8")
    rc = mod.main([str(msg)])
    assert rc == 0
    assert "tests-accompany-code" in capsys.readouterr().err


def test_main_zero_and_silent_on_escape(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(mod, "_staged_files", lambda: ["scripts/agentctl/cli.py"])
    msg = tmp_path / "MSG"
    msg.write_text("rename\n\n[skip-test-guard: pure move]\n", encoding="utf-8")
    rc = mod.main([str(msg)])
    assert rc == 0
    assert capsys.readouterr().err == ""
