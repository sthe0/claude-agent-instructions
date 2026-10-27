"""Headless customer channel: delivers what a gate shows the customer through a
command the engine runs itself, where no hook can observe the harness transcript
(`claude -p`).

The channel is configured ONLY at `<config root>/delivery-channel.toml` (the root
`lib.config_root.agent_home()` resolves from the environment the session was
started with) as `command = ["prog", "arg", ...]`. A file anywhere a session can
write — cwd, state root, plans directory — is never read.

Call contract: `<command> <kind>` once per delivery, `kind` one of
`essence`, `full`, `replan_diff`, `resolution_ask`; the delivered bytes arrive on
stdin. The first stdout line is the answer: `approve` for the first three kinds,
`confirm` for `resolution_ask`. A non-zero exit, no line, or any other first line
is no answer. The engine alone records the outcome (a `channel_delivery` history
event holding the sha256 of the exact bytes handed over) and alone writes the
`channel` delivery stamp, so a stamp the session writes by hand has no record to
match and is refused by the gate.
"""
from __future__ import annotations

import hashlib
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from lib import config_root

from .delivery import CHANNEL_EVENT as EVENT
from .delivery import SOURCE_CHANNEL

CONFIG_NAME = "delivery-channel.toml"
EXPECTED_ANSWER = {
    "essence": "approve",
    "full": "approve",
    "replan_diff": "approve",
    "resolution_ask": "confirm",
}
KINDS = tuple(EXPECTED_ANSWER)
TIMEOUT_S = 600


@dataclass
class Delivery:
    kind: str
    sha256: str
    answered: bool


def config_path() -> Path:
    return config_root.agent_home() / CONFIG_NAME


def load_command() -> list[str] | None:
    """The configured channel command, or None when none is configured or the
    file is unusable (a broken config behaves as no channel)."""
    try:
        data = tomllib.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    command = data.get("command")
    if (isinstance(command, list) and command
            and all(isinstance(part, str) and part for part in command)):
        return command
    return None


def deliver(kind: str, payload: bytes) -> Delivery | None:
    """Hand `payload` to the channel command. None when no channel is configured;
    otherwise a Delivery whose `answered` is True only on the expected answer."""
    if kind not in KINDS:
        raise ValueError(f"unknown delivery kind {kind!r}; expected one of {KINDS}")
    command = load_command()
    if command is None:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    try:
        proc = subprocess.run(
            [*command, kind], input=payload, capture_output=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return Delivery(kind, digest, False)
    if proc.returncode != 0:
        return Delivery(kind, digest, False)
    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    answer = lines[0].strip() if lines else ""
    return Delivery(kind, digest, answer == EXPECTED_ANSWER[kind])


def log_delivery(state, delivery_result: Delivery, *, plan_sha256: str | None) -> None:
    state.log(EVENT, source=SOURCE_CHANNEL, kind=delivery_result.kind,
              sha256=delivery_result.sha256, plan_sha256=plan_sha256,
              answered=delivery_result.answered)
