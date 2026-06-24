"""Self-improvement no longer has a dedicated engine side-flow: it runs on the
standard plan-approval spine like any other state-changing task. These tests pin
the retirement of the si-propose/si-apply two-beat gate and the backward-compat
loading of state files written under the old schema (which carried a
`self_improvement` GateRecord)."""
from agentctl import cli
from agentctl.state import SCHEMA_VERSION, SessionState


def test_si_commands_removed():
    assert "si-propose" not in cli.COMMANDS
    assert "si-apply" not in cli.COMMANDS
    assert not hasattr(cli, "cmd_si_propose")
    assert not hasattr(cli, "cmd_si_apply")


def test_session_state_has_no_self_improvement_field():
    state = SessionState(session_id="si0", task_id="t")
    assert not hasattr(state, "self_improvement")
    assert "self_improvement" not in state.to_dict()


def test_schema_version_bumped():
    # bumped from 4 when the self_improvement field was dropped
    assert SCHEMA_VERSION >= 5


def test_legacy_state_with_si_field_still_loads():
    # a state file written under schema <=4 carries a `self_improvement` GateRecord;
    # from_dict must drop it rather than choke on the unexpected key.
    state = SessionState(session_id="si7", task_id="t")
    data = state.to_dict()
    data["self_improvement"] = {
        "name": "self_improvement", "armed": True, "passed": False,
        "by": None, "note": None,
    }
    rebuilt = SessionState.from_dict(data)
    assert not hasattr(rebuilt, "self_improvement")
    assert rebuilt.session_id == "si7"
