"""Core commit-authority detection (ADR-0001 § Submission (non-author, no push)).

A machine either holds Core commit authority or it does not. ``is_author()`` decides:
the explicit ``is_author`` flag in config.md WINS when set; absent the flag it falls back to a
``git push --dry-run`` capability probe. The decision drives self-improvement routing: a
non-author NEVER edits Core — it files a DifficultyRecord to the configured channel instead.

Both the config read and the push probe are injectable so tests run offline with no real push.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from .port import DifficultyRecord, get_channel

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.md"
IS_AUTHOR_KEY = "is_author"
DEFAULT_CHANNEL = "startrek"


def read_is_author_flag(config_path: Path = CONFIG_PATH) -> bool | None:
    """Parse the config.md ``is_author`` row -> True/False, or None if absent/unparseable."""
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if "`" + IS_AUTHOR_KEY + "`" in line and line.lstrip().startswith("|"):
                cells = [c.strip().strip("`").lower() for c in line.split("|")]
                for cell in cells:
                    if cell in ("true", "false"):
                        return cell == "true"
    except FileNotFoundError:
        pass
    return None


def probe_push_capability(
    runner: Callable[[list[str]], int] | None = None,
    remote: str = "origin",
    ref: str = "HEAD",
) -> bool:
    """`git push --dry-run` capability probe. No actual push (dry-run). Mockable via runner."""
    def _default_runner(cmd: list[str]) -> int:
        return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True).returncode

    run = runner or _default_runner
    return run(["git", "push", "--dry-run", remote, ref]) == 0


def is_author(
    config_path: Path = CONFIG_PATH,
    probe: Callable[[], bool] | None = None,
    flag: bool | None = None,
) -> bool:
    """Flag wins when set (explicit or read from config); else fall back to the push probe."""
    decided = flag if flag is not None else read_is_author_flag(config_path)
    if decided is not None:
        return decided
    probe_fn = probe or probe_push_capability
    return probe_fn()


# The two routing outcomes for a Core-target difficulty.
ROUTE_EDIT_CORE = "edit-core"        # author: normal planner -> approval -> developer spine
ROUTE_TO_CHANNEL = "route-to-channel"  # non-author: file a DifficultyRecord, never edit Core


def route_for_core_difficulty(author: bool) -> str:
    return ROUTE_EDIT_CORE if author else ROUTE_TO_CHANNEL


def file_core_difficulty(record: DifficultyRecord, channel: str = DEFAULT_CHANNEL) -> str:
    """Non-author path: submit the difficulty to a channel the machine already has write to.
    Returns the channel-native handle. NEVER edits Core."""
    return get_channel(channel).submit(record)
