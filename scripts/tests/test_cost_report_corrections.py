"""cost-report.py counts corrections by the rule policy-scorecard.py uses.

Who spoke is structure (origin.kind == "human"); whether a human prompt is a
correction is meaning: the si_feedback_detect prefilter nominates and the judge
decides, with verdicts cached in the one file both scripts share. A nomination
the judge did not answer is counted separately, never as a correction.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
COST_REPORT = SCRIPTS / "cost-report.py"
POLICY_SCORECARD = SCRIPTS / "policy-scorecard.py"
PROMPT_JUDGES = SCRIPTS / "lib" / "prompt_judges.py"

FLAGGED = "Actually, that's wrong, you need to redo it."
FLAGGED_2 = "Actually, that's wrong, you should redo the other one."
PLAIN = "Please list the files in the directory."


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cr():
    return _load(COST_REPORT, "cost_report_corrections_under_test")


def _strip(text: str) -> str:
    from si_feedback_detect import strip_injected_context
    return strip_injected_context(text)


def _key(text: str) -> str:
    return hashlib.sha256(_strip(text).encode("utf-8")).hexdigest()


def _verdict_file(monkeypatch, tmp_path: Path, seeded: dict[str, bool] | None = None) -> Path:
    path = tmp_path / "verdicts.json"
    if seeded is not None:
        path.write_text(json.dumps({_key(t): v for t, v in seeded.items()}), encoding="utf-8")
    monkeypatch.setenv("POLICY_CORRECTION_VERDICTS", str(path))
    return path


def _entry(text: str, origin=None, ts: str = "2026-06-05T10:00:00Z") -> dict:
    e = {"type": "user", "timestamp": ts, "message": {"content": text}}
    if origin is not None:
        e["origin"] = origin
    return e


def _human(text: str) -> dict:
    return _entry(text, origin={"kind": "human"})


def _transcripts(tmp_path: Path, entries: list[dict]) -> Path:
    d = tmp_path / "transcripts"
    d.mkdir(exist_ok=True)
    (d / "s1.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return d


class _Stub:
    """Counting stand-in for advisor.judge_feedback_signal."""

    def __init__(self, verdict: bool = True):
        self.verdict = verdict
        self.calls = 0

    def __call__(self, text, runner, **kwargs):
        self.calls += 1
        return self.verdict, ""


def _install_judge(monkeypatch, cr, stub) -> None:
    from agentctl import advisor
    monkeypatch.setattr(advisor, "judge_feedback_signal", stub)
    monkeypatch.setattr(cr, "_CORRECTION_JUDGE_RUNNER", object(), raising=False)


def _parse(cr, tmp_path: Path, entries: list[dict]) -> dict:
    d = _transcripts(tmp_path, entries)
    return cr.parse_transcripts(sorted(d.glob("*.jsonl")), classify=True)


def test_machine_text_never_counts_as_correction(cr, tmp_path, monkeypatch):
    texts = [FLAGGED, FLAGGED_2]
    _verdict_file(monkeypatch, tmp_path, {t: True for t in texts})
    stub = _Stub()
    _install_judge(monkeypatch, cr, stub)

    tr = _parse(cr, tmp_path, [_entry(texts[0]),
                               _entry(texts[1], origin={"kind": "task-notification"})])

    assert tr["corrections"] == 0
    assert stub.calls == 0


def test_human_prompt_counts_only_on_cached_yes(cr, tmp_path, monkeypatch):
    _verdict_file(monkeypatch, tmp_path, {FLAGGED: False, FLAGGED_2: True})
    stub = _Stub()
    _install_judge(monkeypatch, cr, stub)

    no = _parse(cr, tmp_path, [_human(FLAGGED)])
    yes = _parse(cr, tmp_path, [_human(FLAGGED_2)])

    assert no["corrections"] == 0
    assert yes["corrections"] == 1
    assert stub.calls == 0


def test_unjudged_hit_is_counted_separately(cr, tmp_path, monkeypatch):
    _verdict_file(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")

    tr = _parse(cr, tmp_path, [_human(FLAGGED)])

    assert tr["corrections"] == 0
    assert tr.get("corrections_unjudged") == 1


def test_cost_report_renders_judged_and_unjudged_lines(cr, tmp_path, monkeypatch, capsys):
    _verdict_file(monkeypatch, tmp_path, {FLAGGED: True})
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    d = _transcripts(tmp_path, [_human(FLAGGED), _human(FLAGGED_2),
                                _entry(FLAGGED, origin={"kind": "task-notification"})])

    cr.main(["--project", str(d), "--classify-corrections", "--log", str(tmp_path / "none.jsonl")])
    out = capsys.readouterr().out

    assert "heuristic, approximate" not in out
    judged = re.findall(r"^\s*judged corrections:\s+(\d+)\s*$", out, re.M)
    unjudged = re.findall(r"^\s*unjudged correction hits:\s+(\d+)\s*$", out, re.M)
    humans = re.findall(r"^\s*human prompts:\s+(\d+)\s*$", out, re.M)
    assert judged == ["1"]
    assert unjudged == ["1"]
    assert humans == ["2"]


def test_preexisting_verdict_file_still_valid(cr, tmp_path, monkeypatch):
    path = _verdict_file(monkeypatch, tmp_path)
    path.write_text(json.dumps({_key(FLAGGED): True}), encoding="utf-8")
    stub = _Stub(verdict=False)
    _install_judge(monkeypatch, cr, stub)

    tr = _parse(cr, tmp_path, [_human(FLAGGED)])

    assert tr["corrections"] == 1
    assert stub.calls == 0


def test_human_prompt_without_prefilter_hit_is_not_judged(cr, tmp_path, monkeypatch):
    _verdict_file(monkeypatch, tmp_path, {PLAIN: True})
    stub = _Stub()
    _install_judge(monkeypatch, cr, stub)
    from si_feedback_detect import find_signals
    assert not find_signals(_strip(PLAIN))

    tr = _parse(cr, tmp_path, [_human(PLAIN)])

    assert tr["corrections"] == 0
    assert stub.calls == 0


def test_cost_report_reads_verdicts_policy_scorecard_writes(cr, tmp_path, monkeypatch):
    _verdict_file(monkeypatch, tmp_path)
    ps = _load(POLICY_SCORECARD, "policy_scorecard_corrections_under_test")
    monkeypatch.setattr(ps, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ps, "REPO_ROOT", tmp_path / "no-instrepo")
    yes_text = FLAGGED + " ALPHA"
    no_text = FLAGGED_2 + " BETA"

    def judge(text, runner, **kwargs):
        return "ALPHA" in text, ""

    from agentctl import advisor
    monkeypatch.setattr(advisor, "judge_feedback_signal", judge)
    monkeypatch.setattr(ps, "_CORRECTION_JUDGE_RUNNER", object())
    session = tmp_path / "projects" / "proj" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("\n".join(json.dumps(_human(t)) for t in (yes_text, no_text)) + "\n",
                       encoding="utf-8")
    ps._scan_session(session)

    stub = _Stub(verdict=False)
    _install_judge(monkeypatch, cr, stub)
    tr = _parse(cr, tmp_path, [_human(yes_text), _human(no_text)])

    assert tr["corrections"] == 1
    assert stub.calls == 0


def _cli_env(monkeypatch, budget_zero: bool) -> None:
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    monkeypatch.delenv("CLAUDE_SI_FEEDBACK_SEMANTIC", raising=False)
    if budget_zero:
        monkeypatch.setenv("POLICY_CORRECTION_JUDGE_BUDGET_S", "0")
    else:
        monkeypatch.delenv("POLICY_CORRECTION_JUDGE_BUDGET_S", raising=False)


def test_advisor_on_uncached_flagged_prompt_is_judged_once(cr, tmp_path, monkeypatch, capsys):
    path = _verdict_file(monkeypatch, tmp_path, {})
    _cli_env(monkeypatch, budget_zero=False)
    stub = _Stub(verdict=True)
    from agentctl import advisor
    monkeypatch.setattr(advisor, "judge_feedback_signal", stub)
    d = _transcripts(tmp_path, [_human(FLAGGED)])

    cr.main(["--project", str(d), "--classify-corrections", "--log", str(tmp_path / "none.jsonl")])
    out = capsys.readouterr().out

    assert stub.calls == 1
    assert re.findall(r"^\s*judged corrections:\s+(\d+)\s*$", out, re.M) == ["1"]
    assert re.findall(r"^\s*unjudged correction hits:\s+(\d+)\s*$", out, re.M) == ["0"]
    assert json.loads(path.read_text(encoding="utf-8")).get(_key(FLAGGED)) is True


def test_budget_zero_advisor_on_uncached_prompt_is_unjudged(cr, tmp_path, monkeypatch, capsys):
    path = _verdict_file(monkeypatch, tmp_path, {})
    _cli_env(monkeypatch, budget_zero=True)
    stub = _Stub(verdict=True)
    from agentctl import advisor
    monkeypatch.setattr(advisor, "judge_feedback_signal", stub)
    d = _transcripts(tmp_path, [_human(FLAGGED)])

    cr.main(["--project", str(d), "--classify-corrections", "--log", str(tmp_path / "none.jsonl")])
    out = capsys.readouterr().out

    assert re.findall(r"^\s*unjudged correction hits:\s+(\d+)\s*$", out, re.M) == ["1"]
    assert re.findall(r"^\s*judged corrections:\s+(\d+)\s*$", out, re.M) == ["0"]
    assert stub.calls == 0
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    assert _key(FLAGGED) not in stored


def _function_names(tree: ast.AST) -> set[str]:
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _imports_prompt_judges(tree: ast.AST) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name == "lib.prompt_judges" for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and n.module == "lib.prompt_judges":
            return True
    return False


def test_verdict_cache_single_source(monkeypatch):
    assert PROMPT_JUDGES.exists()
    shared = ast.parse(PROMPT_JUDGES.read_text(encoding="utf-8"))
    shared_names = {n.name for n in shared.body if isinstance(n, ast.FunctionDef)}
    forbidden = shared_names | {"_verdicts_path", "_load_verdicts", "_store_verdict",
                                "_correction_verdict"}
    for script in (COST_REPORT, POLICY_SCORECARD):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        assert not (_function_names(tree) & forbidden), script.name
        assert _imports_prompt_judges(tree), script.name

    cr = _load(COST_REPORT, "cost_report_single_source_under_test")
    ps = _load(POLICY_SCORECARD, "policy_scorecard_single_source_under_test")
    judges = sys.modules["lib.prompt_judges"]
    for mod in (cr, ps):
        for name in shared_names:
            if hasattr(mod, name):
                assert getattr(mod, name) is getattr(judges, name), (mod.__name__, name)


class _SkipFunctions(ast.NodeVisitor):
    def __init__(self):
        self.compiles: list[int] = []

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_Lambda = visit_FunctionDef

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "compile" \
                and isinstance(f.value, ast.Name) and f.value.id == "re":
            self.compiles.append(node.lineno)
        self.generic_visit(node)


def test_cost_report_has_no_correction_regex_sites():
    tree = ast.parse(COST_REPORT.read_text(encoding="utf-8"))
    parsers = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "parse_transcripts"]
    assert len(parsers) == 1
    banned = {"search", "match", "fullmatch", "findall", "finditer", "sub", "subn"}
    regex_calls = [c.lineno for c in ast.walk(parsers[0])
                   if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr in banned]
    assert regex_calls == []
    visitor = _SkipFunctions()
    visitor.visit(tree)
    assert visitor.compiles == []


def test_base_key_cached_no_is_honored_without_judge(cr, tmp_path, monkeypatch):
    _verdict_file(monkeypatch, tmp_path, {FLAGGED: False})
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    stub = _Stub()
    from agentctl import advisor
    monkeypatch.setattr(advisor, "judge_feedback_signal", stub)

    tr = _parse(cr, tmp_path, [_human(FLAGGED)])

    assert tr["corrections"] == 0
    assert tr.get("corrections_unjudged") == 0
    assert stub.calls == 0
