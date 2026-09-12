"""Stage 4: the turn-driven effort-divergence watch — advisory, throttled, fail-open.

The hook exists because every other comparison in the trigger happens inside a
command the coordinator CHOSE to run. Running on every prompt is only safe if the
hook is inert in all four failure directions, so those are what is pinned here: it
never blocks and never speaks on a broken input, it stays quiet while the trigger
is unarmed or switched off for this session, it says a given scale at most once
per band, and each scale keeps its own band so one loud scale cannot mute
another.

The last two tests pin the CONTRACT WITH THE ENGINE rather than the hook's own
logic: `effort-check`'s stdout must be parseable JSON and nothing else (a stray
print or a future default-output change would make this hook permanently mute, in
silence), and a report whose replan count came from the cross-session accumulator
rather than from this session must still reach the hook's mouth — that is the
resolved-reentry shape the other half of this change manufactures.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import uuid
from argparse import Namespace
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "hook_effort_divergence_watch", SCRIPTS_DIR / "hook-effort-divergence-watch.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

from agentctl import cli, effort, task_accumulator  # noqa: E402
from agentctl.config import Thresholds  # noqa: E402

_UNSET = object()


def row(scale="spend", *, past=2.0, at_or_past=True, **kw):
    """One `scales[]` row in the shape `cmd_effort_check` emits."""
    out = {
        "scale": scale, "label": scale, "unit": "usd", "kind": "ratio",
        "actual": 12.0, "comparand": 3.0, "ratio": 4.0,
        "past_own_trigger": past, "at_or_past_threshold": at_or_past,
        "cross_session": False,
    }
    out.update(kw)
    return out


def directive(rows, *, armed=True, active=True, would_fire=_UNSET):
    """`would_fire` defaults to the first over-threshold row's scale, matching what
    the real CLI reports on an ordinary (not-yet-fired-and-acknowledged) divergence
    — the shape every test in this file except the belt-2 ones below wants. Pass
    `would_fire=None` explicitly to model the belt-2 window: a scale still over its
    threshold whose one-fire-per-replan budget is already spent.
    """
    over = [r["scale"] for r in rows if r["at_or_past_threshold"]]
    if would_fire is _UNSET:
        would_fire = over[0] if over else None
    return {
        "ok": True, "node": "APPROVED", "action": "report", "detail": "",
        "data": {"armed": armed, "active": active, "scales": rows,
                 "over_threshold": over, "would_fire": would_fire, "framing": ""},
    }


def _run(monkeypatch, capsys, tmp_path, report, *, session=None, payload=None):
    """Invoke main() with an isolated stamp dir and a canned engine report.

    The report is substituted at `effort_check` rather than at the subprocess, so
    these tests are about what the hook DOES with a verdict; the two tests at the
    bottom cover how the verdict is obtained.
    """
    monkeypatch.setenv(mod.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(mod, "effort_check", lambda sid: report)
    sid = session or f"s-{uuid.uuid4().hex[:8]}"
    if payload is None:
        payload = json.dumps({"session_id": sid})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = mod.main()
    return rc, capsys.readouterr().out, sid


# --- it speaks, once ----------------------------------------------------------

def test_a_scale_past_its_threshold_speaks(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, directive([row()]))
    assert rc == 0
    assert "[effort-divergence]" in out
    assert "agentctl declare" in out          # it names the cycle, not just the number


def test_the_same_band_is_said_only_once_per_session(monkeypatch, capsys, tmp_path):
    rep = directive([row()])
    rc, out, sid = _run(monkeypatch, capsys, tmp_path, rep)
    assert "[effort-divergence]" in out
    rc2, out2, _ = _run(monkeypatch, capsys, tmp_path, rep, session=sid)
    assert rc2 == 0
    assert out2 == ""


def test_a_scale_already_fired_and_awaiting_ack_names_fire_acknowledge(
    monkeypatch, capsys, tmp_path
):
    """Belt 2's one-fire-per-replan budget is already spent for this scale
    (`would_fire` is None even though the scale is still `over_threshold`): the
    coordinator's actual blocked next act is `agentctl fire-acknowledge`, not
    another `declare` — naming `declare` here would send it to a command that
    is not what is stuck."""
    rc, out, _ = _run(monkeypatch, capsys, tmp_path,
                      directive([row()], would_fire=None))
    assert rc == 0
    assert "[effort-divergence]" in out
    assert "agentctl fire-acknowledge" in out
    assert "agentctl declare" not in out


# --- and stays quiet in every direction that is not a divergence --------------

def test_an_unarmed_session_is_silent(monkeypatch, capsys, tmp_path):
    """No approval baseline means no norm; there is nothing to be past."""
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, directive([row()], armed=False))
    assert (rc, out) == (0, "")


def test_an_inactive_trigger_is_silent(monkeypatch, capsys, tmp_path):
    """`active` is `gates.effort_active` — the trigger switched off for this session
    (`AGENTCTL_EFFORT=0`, or a non-SUBSTANTIVE weight class) — not an in-flight fire;
    a watch that speaks for a switch the session itself turned off is noise."""
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, directive([row()], active=False))
    assert (rc, out) == (0, "")


def test_no_scale_over_its_threshold_is_silent(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path,
                      directive([row(past=0.4, at_or_past=False)]))
    assert (rc, out) == (0, "")


def test_no_scales_at_all_is_silent(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, directive([]))
    assert (rc, out) == (0, "")


# --- bands --------------------------------------------------------------------

def test_a_growing_divergence_speaks_again(monkeypatch, capsys, tmp_path):
    """The throttle silences a REPEAT, not a worsening: 2x said, 3x says it again."""
    _, out1, sid = _run(monkeypatch, capsys, tmp_path, directive([row(past=2.0)]))
    assert "[effort-divergence]" in out1
    _, out2, _ = _run(monkeypatch, capsys, tmp_path, directive([row(past=3.4)]),
                      session=sid)
    assert "[effort-divergence]" in out2


def test_a_shrinking_or_equal_band_stays_silent(monkeypatch, capsys, tmp_path):
    _, out1, sid = _run(monkeypatch, capsys, tmp_path, directive([row(past=3.0)]))
    assert "[effort-divergence]" in out1
    for past in (3.0, 2.0):
        _, out, _ = _run(monkeypatch, capsys, tmp_path, directive([row(past=past)]),
                         session=sid)
        assert out == ""


def test_band_for_floors_at_one():
    """A scale exactly at its trigger is band 1, not band 0 — otherwise the first
    thing the hook ever has to say would be throttled against a never-fired stamp."""
    assert mod.band_for({"past_own_trigger": 1.0}) == 1
    assert mod.band_for({"past_own_trigger": 0.0}) == 1
    assert mod.band_for({}) == 1
    assert mod.band_for({"past_own_trigger": 2.9}) == 2


def test_each_scale_keeps_its_own_band(monkeypatch, capsys, tmp_path):
    """The four scales are independent statements about one session. Announcing
    spend must not silence a replan count that reaches its absolute trigger later."""
    _, out1, sid = _run(monkeypatch, capsys, tmp_path,
                        directive([row("spend", past=2.0)]))
    assert "spend" in out1
    _, out2, _ = _run(monkeypatch, capsys, tmp_path,
                      directive([row("replans", past=1.0)]), session=sid)
    assert "replans" in out2
    # ...and spend's own stamp survived that: its band 2 is still spoken for.
    _, out3, _ = _run(monkeypatch, capsys, tmp_path,
                      directive([row("spend", past=2.0)]), session=sid)
    assert out3 == ""


def test_the_worst_scale_is_the_one_furthest_past_its_own_trigger():
    """Ranking on the raw ratio would always favour a ratio scale barely over its
    multiple against an absolute scale at twice its count."""
    ratio_scale = row("spend", past=1.1, ratio=5.5, kind="ratio")
    absolute_scale = row("replans", past=2.0, ratio=2.0, kind="absolute")
    worst = mod.worst_scale(directive([ratio_scale, absolute_scale]))
    assert worst["scale"] == "replans"


def test_a_row_under_its_threshold_is_never_the_worst():
    worst = mod.worst_scale(directive([row("spend", past=9.0, at_or_past=False),
                                       row("replans", past=1.0)]))
    assert worst["scale"] == "replans"


# --- fail-open, in every direction --------------------------------------------

def test_unparseable_stdin_is_silent(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, directive([row()]),
                      payload="not json at all")
    assert (rc, out) == (0, "")


def test_a_payload_without_a_session_id_is_silent(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, directive([row()]),
                      payload=json.dumps({"cwd": "/tmp"}))
    assert (rc, out) == (0, "")


def test_a_non_dict_verdict_is_silent(monkeypatch, capsys, tmp_path):
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, None)
    assert (rc, out) == (0, "")
    rc, out, _ = _run(monkeypatch, capsys, tmp_path, ["unexpected"])
    assert (rc, out) == (0, "")


def test_an_exception_anywhere_is_still_exit_zero_and_silent(monkeypatch, capsys,
                                                             tmp_path):
    """Two throws on opposite sides of the decision: the engine call itself, and
    the stamp write that happens after the hook has decided to speak. Neither may
    reach the user's prompt as anything but silence."""
    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setenv(mod.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-x"})))
    monkeypatch.setattr(mod, "effort_check", boom)
    assert mod.main() == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(mod, "effort_check", lambda sid: directive([row()]))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-y"})))
    monkeypatch.setattr(mod.band_throttle, "record_band", boom)
    assert mod.main() == 0
    assert capsys.readouterr().out == ""


def test_effort_check_swallows_a_broken_subprocess(monkeypatch):
    """The engine is driven as a subprocess precisely so a broken install cannot
    take the prompt down; that only holds if BOTH failure shapes are swallowed —
    a non-JSON stdout and a raised timeout."""
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **kw: Namespace(stdout="Traceback (most recent)"))
    assert mod.effort_check("s1") is None

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="agentctl", timeout=mod.CHECK_TIMEOUT_S)

    monkeypatch.setattr(mod.subprocess, "run", timeout)
    assert mod.effort_check("s1") is None


# --- the contract with the engine ---------------------------------------------

def test_effort_check_stdout_is_pure_json(tmp_path):
    """End to end against the real CLI: the hook parses stdout as JSON and treats
    anything else as silence, so a stray print — or a future human-readable default
    output — would mute this hook permanently and quietly. Pin the contract.

    A `--state-root` of its own and an unknown session, so this touches no real
    state and writes nothing anywhere.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agentctl", "--state-root", str(tmp_path),
         "effort-check", "--session", "nope"],
        cwd=str(SCRIPTS_DIR), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)          # the assertion: it parses, whole
    assert parsed["data"]["armed"] is False
    assert mod.worst_scale(parsed) is None


def test_it_speaks_on_a_divergence_the_accumulator_supplied(store, fixtures_dir,
                                                            monkeypatch, capsys,
                                                            tmp_path):
    """The resolved-reentry shape, driven through the REAL report.

    `reset` builds a fresh SessionState whose own replan count is 0 while the
    cross-session accumulator still holds the prior laps. Before the report and
    the gate were made to read one vector, `at_or_past_threshold` was computed
    session-locally and this hook — which speaks on the report, not on
    `would_fire` — went silent on exactly the session the trigger exists to catch.
    """
    sid = "watch-cross"
    task = "watch-cross-demo"
    cli.cmd_start(Namespace(session=sid, task=task, goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(Namespace(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session=sid), store=store)
    cli.cmd_submit_plan(Namespace(session=sid,
                                  plan=str(fixtures_dir / "plan_two_stage.toml")),
                        store=store)
    cli.cmd_approve(Namespace(session=sid, by="user"), store=store)
    assert effort.replan_count(store.load(sid)) == 0
    task_accumulator.add(task, "replan_count", Thresholds().effort_replan_absolute(),
                         session_id=sid, now=None)

    report = cli.cmd_effort_check(Namespace(session=sid, cost_log=None), store=store)
    # through json, because that is the only way the hook ever sees a Directive
    rc, out, _ = _run(monkeypatch, capsys, tmp_path,
                      json.loads(json.dumps(report.to_dict())), session=sid)
    assert rc == 0
    assert "[effort-divergence]" in out
    assert effort.SCALE_REPLANS in out
