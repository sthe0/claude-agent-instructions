"""Reaper `agentctl-state`: session-state files that never left the first node.

Difficulty removed: agentctl writes `<state dir>/<session>.json` for every session it
classifies, and nothing deletes them, so the directory grows by one file per session.
Almost all of them are sessions that stopped at node CLASSIFIED (a chat, a small change)
and that no tool reads again.

scan() looks only at plain `<session>.json` names (a full match of [0-9A-Za-z_-]+\\.json) and proposes
removal of a file whose top-level `node` is CLASSIFIED, whose mtime (the store keeps no
update time, so mtime is the activity signal) is older than MIN_AGE_DAYS, and whose
session no live session-scope record owns. Everything else matched is kept with a reason
(`symlink`, `unreadable`, `node <X>`, `fresh`, `owned`). Other names are never looked at: approvals,
plan versions, delivery sidecars and backups outlive their session and have readers.

remove() re-judges the file before unlinking it, so a session that moved on between scan
and removal is kept.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from lib import config_root
from reaper.contract import KEEP, REMOVE, ReapContext, Verdict

NAME = "agentctl-state"
THROTTLE_HOURS = 24.0
MIN_AGE_DAYS = 30.0
RESIDUE_NODE = "CLASSIFIED"
STATE_FILE_NAME = re.compile(r"[0-9A-Za-z_-]+\.json")


def state_dir() -> Path:
    return config_root.agentctl_state_dir()


def judge(path: Path, ctx: ReapContext) -> Verdict:
    if path.is_symlink():
        return Verdict(str(path), KEEP, "symlink")
    try:
        node = json.loads(path.read_text(encoding="utf-8")).get("node")
        mtime = path.stat().st_mtime
    except (OSError, ValueError, AttributeError):
        return Verdict(str(path), KEEP, "unreadable")
    if node != RESIDUE_NODE:
        return Verdict(str(path), KEEP, f"node {node}")
    age_days = (ctx.now - mtime) / 86400.0
    if age_days < MIN_AGE_DAYS:
        return Verdict(str(path), KEEP, f"fresh ({age_days:.1f}d < {MIN_AGE_DAYS:.0f}d floor)")
    if ctx.owned_session(path.name[: -len(".json")]):
        return Verdict(str(path), KEEP, "owned")
    return Verdict(str(path), REMOVE, f"node {RESIDUE_NODE}, idle {age_days:.0f}d")


def scan(ctx: ReapContext) -> "list[Verdict]":
    root = state_dir()
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    return [judge(root / name, ctx) for name in names if STATE_FILE_NAME.fullmatch(name)]


def remove(path: str, ctx: ReapContext) -> bool:
    target = Path(path)
    if not STATE_FILE_NAME.fullmatch(target.name) or judge(target, ctx).action != REMOVE:
        return False
    target.unlink()
    return True
