"""No test may append to the real agentctl gate log.

conftest's autouse `_isolate_gate_log` points `AGENTCTL_GATE_LOG` and `cli_common.GATE_LOG` at a
temporary file. A nonce session id is written through each path (in process, and through an
`agentctl-cli.py` subprocess that inherits the environment) and must appear in the temporary
log and be absent from the real one, whose path is resolved with the override removed."""
import os
import subprocess
import sys
import uuid
from argparse import Namespace
from pathlib import Path

from agentctl import cli, cli_common
from lib import config_root

CLI = Path(__file__).resolve().parents[1] / "agentctl-cli.py"


def _real_gate_log(monkeypatch) -> Path:
    monkeypatch.delenv("AGENTCTL_GATE_LOG")
    return config_root.agentctl_gate_log()


def _logged(path: Path, nonce: str) -> bool:
    return path.exists() and nonce in path.read_text(encoding="utf-8", errors="replace")


def test_env_override_sets_the_gate_log_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCTL_GATE_LOG", str(tmp_path / "x.jsonl"))
    assert config_root.agentctl_gate_log() == tmp_path / "x.jsonl"
    monkeypatch.delenv("AGENTCTL_GATE_LOG")
    assert config_root.agentctl_gate_log().name == "gate-log.jsonl"


def test_in_process_gate_evaluation_reaches_only_the_temporary_log(store, monkeypatch):
    nonce = f"canary-{uuid.uuid4().hex}"
    temp_log = cli_common.GATE_LOG
    cli.cmd_start(Namespace(session=nonce, task="t", goal="g", done_criterion="d",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli._log_gate(store.load(nonce), "plan_approval", ["canary"], passed=False)
    assert _logged(temp_log, nonce)
    assert not _logged(_real_gate_log(monkeypatch), nonce)


def test_subprocess_gate_evaluation_reaches_only_the_temporary_log(monkeypatch, tmp_path):
    nonce = f"canary-{uuid.uuid4().hex}"
    temp_log = Path(os.environ["AGENTCTL_GATE_LOG"])
    env = {**os.environ, "CLAUDE_AGENT_HOME": str(tmp_path / "home")}
    for verb in (["start", "--task", "t", "--goal", "g", "--done-criterion", "d",
                  "--criterion-type", "measurable"],
                 ["resolve", "--by", "canary"]):
        done = subprocess.run([sys.executable, str(CLI), verb[0], "--session", nonce, *verb[1:]],
                              env=env, capture_output=True, text=True, timeout=60)
    assert _logged(temp_log, nonce), done.stdout + done.stderr
    assert not _logged(_real_gate_log(monkeypatch), nonce)
