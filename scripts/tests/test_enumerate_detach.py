"""What survives of the whole-plan enumeration cross-check after the standalone
enumerator's retirement for new plans (amendments-2.md E3).

The detached worker, its sidecar fold and the launch from `cmd_submit_plan` /
`cmd_replan` are gone; `advisor.enumerate_claims` (ledger path) and
`advisor.enumerate_questions_health` (kept for `measure-advisor-latency.py`)
still run at the widened `ENUMERATE_TIMEOUT_S` bound. Covers:
  - `cmd_ledger_enumerate` resolves `runner=None` to
    `advisor.enumerate_subprocess_runner`, which delegates to
    `advisor.subprocess_runner` at `ENUMERATE_TIMEOUT_S` -- a genuinely different
    bound from the judge timeouts;
  - the judge fallback (`cmd_record_result`'s acceptance-review path) is
    UNCHANGED: `advisor.subprocess_runner` at its own `_ACCEPTANCE_JUDGE_TIMEOUT_S`;
  - the shipped `_ENUMERATE_TIMEOUT_S_DEFAULT` (480) is what the calibration-dataset
    formula recomputes from the COMMITTED `docs/operations/advisor-calibration.jsonl`;
  - `_positive_int_env` falls back rather than dying at import;
  - `enumerate_sidecar`'s read/discard semantics (the module is still swept at
    session end, and a legacy sidecar can still sit on disk);
  - the retired commands `question-enumerate-worker` exits 2 and writes no sidecar;
  - the entry points' default runner signature is pinned, not inferred from a stub.
"""
from __future__ import annotations

import json
import math
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import advisor, cli, enumerate_sidecar, plugins
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CALIBRATION_PATH = SCRIPTS_DIR.parent / "docs" / "operations" / "advisor-calibration.jsonl"


def ns(**kw):
    return Namespace(**kw)


def _raise_if_called(*_a, **_kw):
    raise AssertionError("this runner must not be invoked")


def _make_acceptance_session(store, sid):
    """Construct a session with an acceptance_review stage at EXECUTING
    directly -- mirrors test_advisor.py's own helper of the same name."""
    state = SessionState(
        session_id=sid,
        task_id="acceptance-test",
        goal="verify UI feature",
        overall_done_criterion="user accepts on review",
        overall_criterion_type=CriterionType.ACCEPTANCE_REVIEW.value,
        weight_class=WeightClass.SMALL_CHANGE.value,
        route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="small-change-carve-out"),
        stages=[
            Stage(
                index=1,
                title="UI verification",
                subject=Subject(material="the feature", result="button is green"),
                means=Means(means="browser", method="open the page"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=CriterionType.ACCEPTANCE_REVIEW.value,
                    done_criterion="user sees green button",
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)


# --- runner-timeout binding: enumerate_subprocess_runner vs subprocess_runner ---

class TestEnumerateRunnerTimeoutBinding:
    def test_enumerate_subprocess_runner_delegates_at_enumerate_timeout(self, monkeypatch):
        calls = []

        def fake_subprocess_runner(argv, *, timeout=None, stdin=""):
            calls.append((argv, timeout))
            return RunResult(0, "", "")

        monkeypatch.setattr(advisor, "subprocess_runner", fake_subprocess_runner)

        result = advisor.enumerate_subprocess_runner(["claude", "-p", "x"])

        assert result.returncode == 0
        assert len(calls) == 1
        argv, timeout = calls[0]
        assert argv == ["claude", "-p", "x"]
        assert timeout == advisor.ENUMERATE_TIMEOUT_S
        assert timeout != advisor._ADVISOR_TIMEOUT_S


# --- CLI fallback of the surviving enumeration verb ------------------------------

class TestCliDefaultsToEnumerateRunner:
    def test_ledger_enumerate_defaults_to_enumerate_subprocess_runner(
            self, store, tmp_path, monkeypatch):
        state = SessionState(session_id="ledger-default", task_id="t")
        plugins.activate(state, "ledger")
        store.save(state)

        calls = []
        monkeypatch.setattr(
            advisor, "enumerate_subprocess_runner",
            lambda argv, **_kw: calls.append(argv) or RunResult(0, "", ""),
        )
        monkeypatch.setattr(advisor, "subprocess_runner", _raise_if_called)

        artifact = tmp_path / "deliverable.md"
        artifact.write_text("chose approach A because latency spiked 3x.", encoding="utf-8")

        d = cli.cmd_ledger_enumerate(
            ns(session="ledger-default", artifact=str(artifact)), store=store, runner=None,
        )
        assert d.ok is True
        assert calls  # advisor.enumerate_subprocess_runner (module attr) was invoked

    def test_retired_worker_exits_2_and_writes_no_sidecar(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        sidecar_root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", sidecar_root)
        monkeypatch.setattr(advisor, "enumerate_subprocess_runner", _raise_if_called)
        monkeypatch.setattr(advisor, "subprocess_runner", _raise_if_called)

        with pytest.raises(SystemExit) as exc:
            cli.cmd_question_enumerate_worker(
                ns(session="worker-retired",
                   plan=str(fixtures_dir / "plan_two_stage.toml"), digest="0" * 64),
                store=store,
            )

        assert exc.value.code == 2
        assert not sidecar_root.exists() or not any(sidecar_root.rglob("*.json"))


# --- judge fallback stays on the plain (short-timeout) runner ------------------

class TestJudgeFallbackUnaffectedByEnumerateTimeout:
    def test_record_result_acceptance_judge_still_uses_plain_subprocess_runner(
            self, store, monkeypatch):
        """acceptance_judge names its own ceiling at the call site, and it is a
        JUDGE ceiling: `_ACCEPTANCE_JUDGE_TIMEOUT_S`, computed by
        `lib/judge_latency.py::last_resort_ceiling_s` from measured haiku rows.
        Patch underneath `subprocess_runner` (its real `subprocess.run` call) so
        the number that actually reaches the subprocess is what gets exercised,
        and confirm `enumerate_subprocess_runner` -- the wider-timeout entry
        point sized for a whole-plan payload -- is never touched on this path."""
        monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "1")
        _make_acceptance_session(store, "judge-1")

        calls = []

        def fake_run(argv, *, capture_output, text, timeout, input=None, **kwargs):
            calls.append(timeout)
            return SimpleNamespace(returncode=0, stdout="YES\nlooks concrete", stderr="")

        monkeypatch.setattr(advisor.subprocess, "run", fake_run)
        monkeypatch.setattr(advisor, "enumerate_subprocess_runner", _raise_if_called)

        d = cli.cmd_record_result(
            ns(session="judge-1", status="passed", actual="observed",
               control=None, observation="the button turned green on load"),
            store=store, runner=None,
        )
        assert d.ok is True
        assert calls == [advisor._ACCEPTANCE_JUDGE_TIMEOUT_S]
        # Without this the assertion above stops discriminating the two ceilings.
        assert advisor._ACCEPTANCE_JUDGE_TIMEOUT_S != advisor.ENUMERATE_TIMEOUT_S


# --- the shipped default vs the committed calibration dataset ------------------

class TestCalibrationConstant:
    def test_default_timeout_matches_calibration_dataset_formula(self):
        rows = [
            json.loads(line)
            for line in CALIBRATION_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows, "calibration dataset must be non-empty"

        by_size: dict[int, list[float]] = {}
        for row in rows:
            by_size.setdefault(row["input_chars"], []).append(row["elapsed_s"])

        max_spread = max(max(vals) / min(vals) for vals in by_size.values())
        largest_size = max(by_size)
        min_elapsed_at_largest = min(by_size[largest_size])

        raw = max_spread * min_elapsed_at_largest
        expected = math.ceil(raw / 60.0) * 60

        assert expected == 480
        assert expected == advisor._ENUMERATE_TIMEOUT_S_DEFAULT

    def test_an_unset_environment_resolves_to_the_shipped_default(self, monkeypatch):
        """Re-resolves rather than reading `advisor.ENUMERATE_TIMEOUT_S`: that
        module-level value is bound at import, so asserting on it directly turns
        anyone who exports the override knob into a red suite — a false report about
        their shell, not about the constant this class exists to pin."""
        monkeypatch.delenv(advisor._ENUMERATE_TIMEOUT_ENV, raising=False)
        assert advisor._positive_int_env(
            advisor._ENUMERATE_TIMEOUT_ENV,
            advisor._ENUMERATE_TIMEOUT_S_DEFAULT) == advisor._ENUMERATE_TIMEOUT_S_DEFAULT


class TestTimeoutOverrideParsing:
    """`_positive_int_env` runs at IMPORT time and `cli` imports `advisor` at module
    scope, so its failure mode is not a bad timeout -- it is every agentctl command
    dying on a traceback before it can say which variable was at fault."""

    def _read(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv(advisor._ENUMERATE_TIMEOUT_ENV, raising=False)
        else:
            monkeypatch.setenv(advisor._ENUMERATE_TIMEOUT_ENV, raw)
        return advisor._positive_int_env(advisor._ENUMERATE_TIMEOUT_ENV, 480)

    def test_a_positive_integer_is_honoured(self, monkeypatch):
        assert self._read(monkeypatch, "900") == 900

    @pytest.mark.parametrize("raw", ["8m", "", "480.0", "eight hundred"])
    def test_unparseable_values_fall_back_and_name_themselves(
            self, monkeypatch, capsys, raw):
        assert self._read(monkeypatch, raw) == 480
        err = capsys.readouterr().err
        assert advisor._ENUMERATE_TIMEOUT_ENV in err
        assert repr(raw) in err

    @pytest.mark.parametrize("raw", ["0", "-30"])
    def test_non_positive_values_are_rejected_rather_than_obeyed(
            self, monkeypatch, capsys, raw):
        """`0` parses fine and is the dangerous one: obeyed, it makes every
        enumeration time out instantly. Nobody chooses that by typing a number,
        so it is refused."""
        assert self._read(monkeypatch, raw) == 480
        assert advisor._ENUMERATE_TIMEOUT_ENV in capsys.readouterr().err


# --- sidecar read/discard semantics (module kept: swept at session end) ---------

class TestSidecarDigestMismatchDiscard:
    def test_read_ignores_and_discards_a_sidecar_written_for_a_different_digest(
            self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-b", root=root)

        assert result is None
        assert not enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).exists()

    def test_read_is_idempotent_for_the_matching_digest(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a",
                                {"pairs": [["stage 1", "why?"]], "content_digest": "digest-a"},
                                root=root)

        first = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)
        second = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert first == second
        assert first["pairs"] == [["stage 1", "why?"]]
        assert enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).exists()

    def test_read_leaves_a_concurrent_workers_tempfile_alone(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)
        session_dir = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).parent
        tmp_file = session_dir / ".tmp-x.json"
        tmp_file.write_text("{}", encoding="utf-8")

        enumerate_sidecar.read_discarding_superseded("sess", "digest-b", root=root)

        assert tmp_file.exists()

    def test_read_discards_a_matching_sidecar_that_fails_to_parse(self, tmp_path):
        root = tmp_path / "sidecars"
        match = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root)
        match.parent.mkdir(parents=True, exist_ok=True)
        match.write_text("not json", encoding="utf-8")

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert result is None
        assert not match.exists()

    def test_read_discards_a_matching_sidecar_of_non_utf8_bytes(self, tmp_path):
        """`read_text(encoding='utf-8')` on corrupt bytes raises UnicodeDecodeError,
        a ValueError SIBLING of JSONDecodeError rather than a subclass."""
        root = tmp_path / "sidecars"
        match = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root)
        match.parent.mkdir(parents=True, exist_ok=True)
        match.write_bytes(b'{"pairs": "\xff\xfe not utf-8"}')

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert result is None
        assert not match.exists()

    def test_read_retains_a_matching_sidecar_on_transient_os_error(self, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)
        match = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root)

        real_read_text = Path.read_text

        def _flaky_read_text(self, *a, **kw):
            if self == match:
                raise OSError("transient failure")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _flaky_read_text)

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert result is None
        assert match.exists()

    def test_discard_all_for_session_sweeps_an_orphaned_tempfile(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)
        session_dir = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).parent
        tmp_file = session_dir / ".tmp-x.json"
        tmp_file.write_text("{}", encoding="utf-8")

        enumerate_sidecar.discard_all_for_session("sess", root=root)

        assert not tmp_file.exists()
        assert not session_dir.exists()


# --- the runner signature is pinned, not inferred from a stub ------------------

@pytest.mark.parametrize(
    "entry",
    ["enumerate_claims", "enumerate_questions_health"],
)
def test_enumerate_runner_signature_matches_what_the_entry_points_pass(entry, monkeypatch):
    """The two enumeration entry points call their DEFAULT runner, and the ceiling
    that reaches `subprocess_runner` is ENUMERATE_TIMEOUT_S. A stub runner accepts
    any signature, so only patching `advisor.subprocess_runner` (keeping the real
    `enumerate_subprocess_runner` in the path) can see a mismatch between what the
    call sites pass and what it accepts -- which a bare `except Exception` in both
    entry points would otherwise report as an UNHEALTHY RUNNER."""
    seen: dict = {}

    def fake_subprocess_runner(argv, *, timeout=None, stdin=""):
        seen["argv"] = argv
        seen["timeout"] = timeout
        seen["stdin"] = stdin
        return RunResult(0, "", "")

    monkeypatch.setattr(advisor, "subprocess_runner", fake_subprocess_runner)

    if entry == "enumerate_claims":
        advisor.enumerate_claims("artifact text")
    else:
        advisor.enumerate_questions_health("goal", "done criterion", "plan text")

    assert seen, (
        f"{entry} never reached subprocess_runner — its call site and "
        "enumerate_subprocess_runner disagree, and the TypeError was swallowed "
        "into an unhealthy-runner verdict"
    )
    assert seen["timeout"] == advisor.ENUMERATE_TIMEOUT_S
    assert seen["timeout"] != advisor._ADVISOR_TIMEOUT_S
