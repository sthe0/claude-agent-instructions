"""Title the tmux pane of a top-level interactive session with the task it works on.

`classify --title` registers the task's short title; this module decides whether the session
owns a pane (the title belongs to the main task, never to a spawned child that merely inherited
`TMUX_PANE`), validates the title, and runs the one `tmux select-pane -T` call. Everything is a
pure function over explicit inputs -- the environment mapping, the session state, the config
constants and the injected runner -- so the tests run no tmux.

The pane title `✳ <title>` is the marker-plus-text shape the user's tmux
`automatic-rename-format` strips to name the window, so the iTerm tab and the ccgram Telegram
topic follow the task.
"""
from __future__ import annotations

import os
import subprocess
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from agentctl import config

MAX_TITLE_CHARS = 48
MARKER = "✳ "
TMUX_TIMEOUT_S = 5

TITLE_GUIDANCE = "2-5 words naming the task, in the dialogue language"

# Module attributes, not parameters of the callers' signatures: cmd_classify reads them through
# `pane_title.<name>` so a test (or the negative-control mutant) patches exactly one place.
RUNNER = subprocess.run


def read_env() -> Mapping[str, str]:
    return os.environ


def owns_pane(
    env: Mapping[str, str],
    state: Any,
    constants: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Whether this session may retitle the pane named by `TMUX_PANE`, and why not.

    `TMUX_PANE` is inherited by every child process, so the handle's presence proves nothing:
    ownership needs a top-level session (depth 0, no parent link, no spawn depth in the
    environment), no test harness, and the `task-title` switch on."""
    if not env.get("TMUX_PANE"):
        return False, "TMUX_PANE unset"
    if getattr(state, "parent_link", None):
        return False, "session is linked to a parent"
    if getattr(state, "recursion_depth", 0) != 0:
        return False, "session depth is above 0"
    if env.get("AGENT_RECURSION_DEPTH", "0") not in ("", "0"):
        return False, "AGENT_RECURSION_DEPTH is above 0"
    if "PYTEST_CURRENT_TEST" in env:
        return False, "test harness running"
    if not config.task_title_enabled(env, constants):
        return False, "task-title switch is off"
    return True, ""


def normalize_title(raw: str) -> tuple[str, str]:
    """`(title, "")` for a well-formed title, `("", reason)` for a refused one.

    More than one line is refused before control characters are stripped: stripping first
    would silently join two lines into one."""
    if "\n" in raw or "\r" in raw:
        return "", "title must be one line"
    title = "".join(ch for ch in raw if unicodedata.category(ch) != "Cc").strip()
    if not title:
        return "", "title is blank"
    if len(title) > MAX_TITLE_CHARS:
        return "", f"title is {len(title)} characters, at most {MAX_TITLE_CHARS} allowed"
    return title, ""


def set_pane_title(
    env: Mapping[str, str],
    title: str,
    run: Callable[..., Any] = RUNNER,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Run `tmux select-pane -t $TMUX_PANE -T '✳ <title>'`; never raises.

    A failure (non-zero exit, timeout, missing tmux, a stale pane) is reported once to `log`
    and returned as False -- the caller's command must not depend on the pane."""
    pane = env.get("TMUX_PANE", "")
    try:
        proc = run(
            ["tmux", "select-pane", "-t", pane, "-T", MARKER + title],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
            check=False,
        )
        failure = "" if proc.returncode == 0 else f"tmux exited {proc.returncode}"
    except Exception as exc:  # noqa: BLE001 -- fail-open: any runner failure is only logged
        failure = f"{type(exc).__name__}: {exc}"
    if failure and log is not None:
        log(failure)
    return not failure
