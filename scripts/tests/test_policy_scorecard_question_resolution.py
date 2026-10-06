"""policy-scorecard.py question / resolution counters: a pattern nominates, a model judge decides.

A human prompt the question prefilter nominates is a *question* only when the judge says YES, and
one the resolution prefilter nominates *confirms* the task only when the judge says YES. A
nomination no judge answered is counted separately (`n_user_questions_unjudged`,
`resolution_unjudged`), never decided silently; an unjudged-and-unconfirmed session leaves the
resolution-rate denominator, and a window with no decided session has no rate at all.

The new fields and functions are read base-tolerantly (`getattr(..., None)`, `.get`), so a missing
implementation fails an assertion rather than raising.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import re
import time
import types
from pathlib import Path

import pytest

from agentctl import advisor
from agentctl.dispatch import RunResult

SCRIPT = Path(__file__).resolve().parent.parent / "policy-scorecard.py"

QUESTION_DIGEST = "5cb7a183d8bdbbfd0c400a2a3babddd4dfc019d45af9d4575f8356dd28c257bc"
RESOLUTION_DIGEST = "d2547457d2d0fb12e5a40550b8ef18aa5ad17358d2d5bca38d645e90eb29cd91"
QUESTION_SALT = "user-question@" + QUESTION_DIGEST
RESOLUTION_SALT = "resolution-confirmation@" + RESOLUTION_DIGEST

CORRECTION_TEXT = "Actually, that's wrong, you need to redo it."
BOTH_TEXT = "Is it fully resolved? looks good to me"
QUESTION_ONLY_TEXT = "Why did the build fail on CI?"
PLAIN_TEXT = "Please add a test."

BASE_QUESTION_RE = re.compile(r"\?")
BASE_RESOLUTION_RE = re.compile(
    r"реш(?:ен|ён|и)|так и оставим|подтвержда|готово|all good|"
    r"\bresolved\b|looks good|считаем",
    re.IGNORECASE)


@pytest.fixture
def ps(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("policy_scorecard_qr_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("POLICY_CORRECTION_VERDICTS", str(tmp_path / "correction-verdicts.json"))
    monkeypatch.setenv("POLICY_QUESTION_VERDICTS", str(tmp_path / "question-verdicts.json"))
    monkeypatch.setenv("POLICY_RESOLUTION_VERDICTS", str(tmp_path / "resolution-verdicts.json"))
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(mod, "_CORRECTION_JUDGE_RUNNER", None, raising=False)
    return mod


class RoutingJudge:
    """One runner answering each of the three judges by the question its prompt asks."""

    def __init__(self, question="NO", resolution="NO", correction="NO"):
        self.answers = {"question": question, "resolution": resolution, "correction": correction}
        self.prompts: dict[str, list[str]] = {"question": [], "resolution": [], "correction": []}

    def __call__(self, argv, **kwargs):
        prompt = kwargs.get("stdin", "")
        if "ASKS the assistant a question" in prompt:
            kind = "question"
        elif "CONFIRMS that the task is resolved" in prompt:
            kind = "resolution"
        else:
            kind = "correction"
        self.prompts[kind].append(prompt)
        return RunResult(0, stdout=self.answers[kind], stderr="")

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.prompts.values())


def _write(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _human(text: str, ts: str = "2026-06-05T10:00:00Z") -> dict:
    return {"type": "user", "timestamp": ts, "origin": {"kind": "human"},
            "message": {"content": text}}


def _session(tmp_path: Path, entries: list[dict], name: str = "s1") -> Path:
    return _write(tmp_path / "projects" / "proj" / f"{name}.jsonl", entries)


def _key(salt: str, text: str) -> str:
    return hashlib.sha256("\x00".join([salt, " ".join(text.split())]).encode("utf-8")).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "question-verdicts.json", tmp_path / "resolution-verdicts.json"


def _upsert_with(ps, monkeypatch, f: Path, existing: dict | None):
    monkeypatch.setattr(ps, "in_window_files", lambda days, project: [f])
    if existing is not None:
        ps.write_ledger({f.stem: existing})
    return ps.upsert(7, None)


def _fixed(verdict: bool, reason: str, sink: list | None = None):
    def judge(text, runner, **kwargs):
        if sink is not None:
            sink.append(text)
        return verdict, reason
    return judge


# ----------------------------------------------------------------- the judge decides

def test_question_mark_alone_is_not_a_question_when_judge_says_no(ps, tmp_path):
    ps._CORRECTION_JUDGE_RUNNER = RoutingJudge(question="NO")
    f = _session(tmp_path, [_human(QUESTION_ONLY_TEXT)])

    row = ps._scan_session(f)

    assert row["user_signals"]["n_user_questions"] == 0
    assert row["user_signals"].get("n_user_questions_unjudged") == 0


def test_resolution_keyword_alone_is_not_confirmation_when_judge_says_no(ps, tmp_path):
    ps._CORRECTION_JUDGE_RUNNER = RoutingJudge(resolution="NO")
    f = _session(tmp_path, [_human("looks good so far, but keep going with the second half")])

    eff = ps._scan_session(f)["effectiveness"]

    assert eff["resolution_confirmed"] == 0
    assert eff.get("resolution_unjudged") == 0


def test_judged_yes_counts_question_and_confirms_resolution(ps, tmp_path):
    ps._CORRECTION_JUDGE_RUNNER = RoutingJudge(question="YES", resolution="YES")
    f = _session(tmp_path, [_human(BOTH_TEXT)])

    row = ps._scan_session(f)

    assert row["user_signals"]["n_user_questions"] == 1
    assert row["user_signals"].get("n_user_questions_unjudged") == 0
    assert row["effectiveness"]["resolution_confirmed"] == 1
    assert row["effectiveness"].get("resolution_unjudged") == 0


def test_unjudged_question_is_counted_separately(ps, tmp_path):
    f = _session(tmp_path, [_human(QUESTION_ONLY_TEXT)])

    signals = ps._scan_session(f)["user_signals"]

    assert signals["n_user_questions"] == 0
    assert signals.get("n_user_questions_unjudged") == 1


def test_budget_zero_leaves_prefilter_hits_unjudged(ps, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_CORRECTION_JUDGE_BUDGET_S", "0")
    judge = RoutingJudge(question="YES", resolution="YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(BOTH_TEXT)])

    rows, scanned, _ = _upsert_with(ps, monkeypatch, f, None)

    row = rows[f.stem]
    assert row["user_signals"]["n_user_questions"] == 0
    assert row["effectiveness"]["resolution_confirmed"] == 0
    assert row["user_signals"].get("n_user_questions_unjudged") == 1
    assert row["effectiveness"].get("resolution_unjudged") == 1
    assert judge.total == 0


# ------------------------------------------------------- caches: salts, keys, separation

def test_new_judge_salts_pinned_to_prompts(ps):
    pj = ps.prompt_judges
    captured = {}
    for name, text in (("judge_user_question", "Is it safe?"),
                       ("judge_resolution_confirmation", "Looks good, we are done")):
        judge = getattr(advisor, name, None)
        assert judge is not None, f"advisor.{name} is missing"
        seen: list[str] = []

        def runner(argv, **kwargs):
            seen.append(kwargs.get("stdin", ""))
            return RunResult(0, stdout="NO", stderr="")

        judge(text, runner)
        captured[name] = hashlib.sha256(seen[0].encode("utf-8")).hexdigest()

    assert captured["judge_user_question"] == QUESTION_DIGEST
    assert getattr(pj, "USER_QUESTION_JUDGE_SALT", None) == QUESTION_SALT
    assert captured["judge_resolution_confirmation"] == RESOLUTION_DIGEST
    assert getattr(pj, "RESOLUTION_JUDGE_SALT", None) == RESOLUTION_SALT


def test_per_judge_caches_are_separate(ps, tmp_path):
    qpath, rpath = _paths(tmp_path)
    qpath.write_text(json.dumps({_key(QUESTION_SALT, BOTH_TEXT): True}), encoding="utf-8")
    rpath.write_text(json.dumps({_key(RESOLUTION_SALT, BOTH_TEXT): False}), encoding="utf-8")
    judge = RoutingJudge(question="NO", resolution="YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(BOTH_TEXT)])

    row = ps._scan_session(f)

    assert row["user_signals"]["n_user_questions"] == 1
    assert row["effectiveness"]["resolution_confirmed"] == 0
    assert judge.prompts["question"] == [] and judge.prompts["resolution"] == []
    assert list(_load(qpath)) != list(_load(rpath))


def test_judged_verdicts_land_in_their_own_files(ps, tmp_path):
    qpath, rpath = _paths(tmp_path)
    ps._CORRECTION_JUDGE_RUNNER = RoutingJudge(question="YES", resolution="NO")
    f = _session(tmp_path, [_human(BOTH_TEXT)])

    ps._scan_session(f)

    assert _load(qpath) == {_key(QUESTION_SALT, BOTH_TEXT): True}
    assert _load(rpath) == {_key(RESOLUTION_SALT, BOTH_TEXT): False}


def test_fail_open_verdicts_are_never_cached(ps, tmp_path, monkeypatch):
    fail_open = "judge timed out (fail-open)"
    monkeypatch.setattr(ps.advisor, "judge_user_question", _fixed(False, fail_open), raising=False)
    monkeypatch.setattr(ps.advisor, "judge_resolution_confirmation",
                        _fixed(False, fail_open), raising=False)
    ps._CORRECTION_JUDGE_RUNNER = object()
    f = _session(tmp_path, [_human(BOTH_TEXT)])

    row = ps._scan_session(f)

    assert row["user_signals"]["n_user_questions"] == 0
    assert row["effectiveness"]["resolution_confirmed"] == 0
    assert row["user_signals"].get("n_user_questions_unjudged") == 1
    assert row["effectiveness"].get("resolution_unjudged") == 1
    qpath, rpath = _paths(tmp_path)
    assert not qpath.exists()
    assert not rpath.exists()


def test_question_and_resolution_caches_are_stage1_verdict_caches(ps, tmp_path, monkeypatch):
    from lib import semantic_join as real_module

    module = getattr(ps, "semantic_join", None) or types.ModuleType("semantic_join_stand_in")
    real_cls = real_module.VerdictCache
    made: list[tuple[str, str]] = []

    class Spy(real_cls):
        def __init__(self, path, salt, key_fn=None):
            made.append((str(path), salt))
            super().__init__(path, salt, key_fn)

    monkeypatch.setattr(module, "VerdictCache", Spy, raising=False)
    ps._CORRECTION_JUDGE_RUNNER = RoutingJudge(question="YES", resolution="YES")
    f = _session(tmp_path, [_human(BOTH_TEXT)])

    ps._scan_session(f)

    qpath, rpath = _paths(tmp_path)
    ours = {(p, s) for p, s in made if p in (str(qpath), str(rpath))}
    assert ours == {(str(qpath), QUESTION_SALT), (str(rpath), RESOLUTION_SALT)}


# --------------------------------------------------------------------------- prefilters

QUESTION_SAMPLES = ["why?", "ok? thanks", "что это такое?", "see https://x.example/?a=1",
                    "no punctuation here", "", "a?b?c?"]
RESOLUTION_SAMPLES = ["решено", "Решён вопрос", "решили", "так и оставим", "подтверждаю",
                      "готово", "ALL GOOD", "it is resolved", "unresolved", "Looks Good",
                      "считаем закрытым", "keep going", "", "the solution fails"]


def test_prefilter_recalls_every_base_regex_hit(ps):
    pj = ps.prompt_judges
    question = getattr(pj, "question_prefilter", None)
    resolution = getattr(pj, "resolution_prefilter", None)
    assert callable(question) and callable(resolution)

    missed_q = [t for t in QUESTION_SAMPLES if BASE_QUESTION_RE.search(t) and not question(t)]
    missed_r = [t for t in RESOLUTION_SAMPLES if BASE_RESOLUTION_RE.search(t) and not resolution(t)]

    assert missed_q == []
    assert missed_r == []


# ------------------------------------------------------------------------ shared budget

def test_one_refresh_shares_one_judge_budget_across_three_judges(ps, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_CORRECTION_JUDGE_BUDGET_S", "8")
    calls: dict[str, list[float]] = {"correction": [], "question": [], "resolution": []}

    def counting(kind):
        def judge(text, runner, **kwargs):
            calls[kind].append(time.monotonic() - t0)
            time.sleep(0.5)
            return False, ""
        return judge

    monkeypatch.setattr(ps.advisor, "judge_feedback_signal", counting("correction"))
    monkeypatch.setattr(ps.advisor, "judge_user_question", counting("question"), raising=False)
    monkeypatch.setattr(ps.advisor, "judge_resolution_confirmation",
                        counting("resolution"), raising=False)
    ps._CORRECTION_JUDGE_RUNNER = object()
    f = _session(tmp_path, [
        _human(f"{CORRECTION_TEXT} Item {i}: is it resolved? looks good",
               ts=f"2026-06-05T10:0{i}:00Z")
        for i in range(6)])

    t0 = time.monotonic()
    _upsert_with(ps, monkeypatch, f, None)

    assert len(calls["question"]) >= 1
    assert len(calls["resolution"]) >= 1
    assert len(calls["correction"]) >= 1
    started = [t for ts in calls.values() for t in ts]
    assert max(started) < 8
    assert len(started) <= 16


# ------------------------------------------------------------- rate, flag and report

def _placed_row(ps, tmp_path, name: str, when: dt.datetime, *, confirmed=0, unjudged=0) -> dict:
    f = _session(tmp_path, [_human(PLAIN_TEXT)], name=name)
    row = ps._scan_session(f)
    stamp = when.isoformat()
    row["first_ts"] = row["last_ts"] = stamp
    row["date"] = when.date().isoformat()
    row["effectiveness"]["resolution_confirmed"] = confirmed
    row["effectiveness"]["resolution_unjudged"] = unjudged
    return row


def _window(ps, tmp_path, tag: str, when: dt.datetime, spec: list[tuple[int, int]]) -> list[dict]:
    return [_placed_row(ps, tmp_path, f"{tag}{i}", when, confirmed=c, unjudged=u)
            for i, (c, u) in enumerate(spec)]


def _report(ps, tmp_path, monkeypatch, cur_rows: list[dict], prev_rows: list[dict]) -> str:
    empty = tmp_path / "task-quality.jsonl"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(ps, "TASK_QUALITY_LEDGER", empty)
    rows = {r["session_id"]: r for r in cur_rows + prev_rows}
    return ps.scorecard(rows, 7, None, spawn_rows=[])


def _resolution_line(report: str) -> str:
    return next(line for line in report.splitlines()
                if line.startswith("- Resolution-confirmed sessions"))


NOW = dt.datetime.now(dt.timezone.utc)
CUR_WHEN = NOW - dt.timedelta(days=1)
PREV_WHEN = NOW - dt.timedelta(days=9)


def test_unjudged_resolution_leaves_the_rate_denominator(ps, tmp_path):
    rows = _window(ps, tmp_path, "a", CUR_WHEN, [(1, 0), (0, 1), (0, 0)])

    agg = ps._aggregate(rows)

    assert agg["resolution_rate"] == pytest.approx(0.5)
    assert agg.get("resolution_unjudged") == 1


def test_resolution_flag_skipped_on_all_undecided_window(ps, tmp_path):
    cur = ps._aggregate(_window(ps, tmp_path, "c", CUR_WHEN, [(0, 1), (0, 1)]))
    prev = ps._aggregate(_window(ps, tmp_path, "p", PREV_WHEN, [(1, 0), (1, 0)]))

    flags = ps._flags(cur, prev)

    assert cur["resolution_rate"] is None
    assert not [f for f in flags if f.key.startswith("resolution-rate")]


def test_resolution_flag_skipped_on_all_undecided_previous_window(ps, tmp_path):
    cur = ps._aggregate(_window(ps, tmp_path, "c", CUR_WHEN, [(0, 0), (0, 0)]))
    prev = ps._aggregate(_window(ps, tmp_path, "p", PREV_WHEN, [(0, 1), (0, 1)]))

    flags = ps._flags(cur, prev)

    assert prev["resolution_rate"] is None
    assert not [f for f in flags if f.key.startswith("resolution-rate")]


def test_report_renders_undecided_resolution_rate(ps, tmp_path, monkeypatch):
    cur = _window(ps, tmp_path, "c", CUR_WHEN, [(0, 1), (0, 1)])
    prev = _window(ps, tmp_path, "p", PREV_WHEN, [(1, 0), (1, 0)])

    line = _resolution_line(_report(ps, tmp_path, monkeypatch, cur, prev))

    assert "undecided" in line
    assert re.search(r"resolution_unjudged\D*2\b", line)


def test_report_renders_undecided_previous_resolution_rate(ps, tmp_path, monkeypatch):
    cur = _window(ps, tmp_path, "c", CUR_WHEN, [(1, 0), (0, 0)])
    prev = _window(ps, tmp_path, "p", PREV_WHEN, [(0, 1), (0, 1)])

    line = _resolution_line(_report(ps, tmp_path, monkeypatch, cur, prev))

    assert "undecided" in line
    assert "1/2" in line


def test_report_prints_unjudged_counts(ps, tmp_path, monkeypatch):
    rows = []
    for i in range(2):
        row = _placed_row(ps, tmp_path, f"u{i}", CUR_WHEN, unjudged=1)
        row["user_signals"]["n_user_questions_unjudged"] = 3
        rows.append(row)

    report = _report(ps, tmp_path, monkeypatch, rows, [])

    assert re.search(r"questions_unjudged\D*6\b", report)
    assert re.search(r"resolution_unjudged\D*2\b", report)


# ------------------------------------------------------------------------- upsert

@pytest.mark.parametrize("field,section", [("n_user_questions_unjudged", "user_signals"),
                                           ("resolution_unjudged", "effectiveness")])
def test_row_with_question_or_resolution_unjudged_rescanned_while_judge_active(
        ps, tmp_path, monkeypatch, field, section):
    f = _session(tmp_path, [_human(PLAIN_TEXT)])
    existing = ps._scan_session(f)
    existing[section][field] = 1

    _, scanned_without_judge, _ = _upsert_with(ps, monkeypatch, f, existing)
    ps._CORRECTION_JUDGE_RUNNER = RoutingJudge()
    _, scanned_with_judge, _ = _upsert_with(ps, monkeypatch, f, existing)

    assert (scanned_without_judge, scanned_with_judge) == (0, 1)


def test_scan_version_three_rescans_old_rows(ps, tmp_path, monkeypatch):
    f = _session(tmp_path, [_human(PLAIN_TEXT)])
    existing = ps._scan_session(f)
    existing["scan_version"] = 2

    _, scanned, skipped = _upsert_with(ps, monkeypatch, f, existing)

    assert (scanned, skipped) == (1, 0)
    assert ps.SCAN_VERSION == 3
