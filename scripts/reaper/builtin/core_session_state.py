"""Reaper `core-session-state`: per-session evidence and review directories of sessions that are over.

Difficulty removed: every agentctl session leaves a directory under the evidence store (a stage's
verification logs and artifacts) and a busy machine's volume fills with directories of sessions
that ended long ago, while nothing reclaims them.

Candidates are the top-level entries of two stores: the engine's evidence store, found through the
engine's own resolver (`agentctl.cli_common._evidence_session_dir`, so `AGENTCTL_EVIDENCE_ROOT` and
`XDG_STATE_HOME` decide), and `agent-reviews` under the same XDG state root. An entry is removed only
when every clause below holds; a failing clause keeps it with its own slug (`REASONS`).

1. `retention-unset` / `retention-below-floor` — the retention is an integer of days, at least
   `RETENTION_FLOOR_DAYS`, from `[params.core-session-state] retention_days` of the opt-in file or
   `--param core-session-state.retention_days=N`. Nothing is removed without it.
2. `no-session` — the entry name is the safe id of a state file in the engine state dir.
3. `lineage-unreadable` — the lineage of that session can be read in full: following
   `parent_link.parent_session` upwards reaches a root, and every member found is parseable.
   The lineage is the root and every descendant (a session whose `parent_link` names a member, and
   each member's `effort_child_runs[].child_session`, recursively).
4. `not-resolved` — the lineage root is RESOLVED. A linked child is never RESOLVED itself.
5. `retention` — the newest state-file mtime across the lineage and the newest run `ended` are older
   than the retention. (The `resolve` history event carries no timestamp.)
6. `lineage-live` — no member has a run that is open (`ended` null) under a living dispatcher
   (`child_runs.pid_alive`). A run with `ended` set, or open under a dead dispatcher, releases.
7. The proof of death in `reaper.liveness.judge_residue` with the idle floor set to the retention:
   no process holds the entry, no live session owns it, no mount or nested `.git` inside it, no
   symlink followed, no walk across a filesystem; every keep slug of that proof passes through.

The reaper only reads. It never closes a dead dispatcher's run and never writes engine state.

Removal is opt-in: `core-session-state = true` in `<agent home>/reaper/opt-in.toml`. Without it a
live pass keeps what it would remove (`opt-in-off-would-remove`) and `remove()` refuses; a dry run
still shows `remove`. `remove()` judges every clause again, with fresh state and a fresh /proc
sweep, immediately before deleting through the parent's directory fd.

The two store roots are the pressure paths (`runner --on-pressure`): a pressure pass makes the
reaper due before its throttle, under the same rules.

`summary()` prints one `store=` line per existing store (removable, kept and untied counts and
bytes) and one `reason=` line per keep reason; the reason counts add up to the stores' kept counts.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
from pathlib import Path

from agentctl import child_runs
from agentctl.store import safe_session_id
from lib import config_root
from reaper import liveness, opt_in
from reaper.contract import KEEP, REMOVE, ReapContext, RemovalRefused, Verdict

NAME = "core-session-state"
THROTTLE_HOURS = 24.0
RETENTION_KEY = "retention_days"
RETENTION_FLOOR_DAYS = 3
RESOLVED_NODE = "RESOLVED"
REVIEWS_DIR = "agent-reviews"

NOT_A_CANDIDATE = "not-a-candidate"
NO_SESSION = "no-session"
LINEAGE_UNREADABLE = "lineage-unreadable"
NOT_RESOLVED = "not-resolved"
RETENTION = "retention"
LINEAGE_LIVE = "lineage-live"
RETENTION_UNSET = "retention-unset"
RETENTION_BELOW_FLOOR = "retention-below-floor"
OPT_IN_OFF = "opt-in-off-would-remove"
REASONS = (
    RETENTION_UNSET, RETENTION_BELOW_FLOOR, NO_SESSION, LINEAGE_UNREADABLE, NOT_RESOLVED, RETENTION, LINEAGE_LIVE,
    liveness.NOT_A_DIRECTORY, liveness.FOREIGN_OWNER, liveness.MOUNT_INSIDE, liveness.SESSION_OWNED,
    liveness.HELD, liveness.HOLDER_SWEEP_INCOMPLETE, liveness.NESTED_GIT, liveness.IDLE_FLOOR,
    liveness.OS_ERROR, OPT_IN_OFF,
)
UNTIED_TAG = "tied=0"


@dataclasses.dataclass(frozen=True)
class Store:
    base: str
    dev: int


@dataclasses.dataclass
class Lineage:
    """The sessions one entry's root spawned, read from the state dir."""

    root: str = ""
    members: "set[str]" = dataclasses.field(default_factory=set)
    unreadable: "str | None" = None
    newest: float = 0.0
    live: "str | None" = None


@dataclasses.dataclass
class World:
    """What one judging pass reads once: the state files, the retention, the /proc sweep, the mounts."""

    states: "dict[str, dict | None]"
    state_dir: Path
    retention_days: "int | None"
    retention_problem: "str | None"
    sweep: liveness.HolderSweep
    mounts: "frozenset[str] | None"


def _evidence_root() -> "str | None":
    """The engine's evidence store, from its own resolver; None when it resolves to none (a scratch root)."""
    try:
        from agentctl import cli_common
        return str(cli_common._evidence_session_dir("probe", "x").parent.parent)
    except Exception:
        return None


def _reviews_root() -> str:
    xdg = os.environ.get("XDG_STATE_HOME")
    state_root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "state"
    return str(state_root / REVIEWS_DIR)


def _stores() -> "list[Store]":
    stores: "list[Store]" = []
    for raw in (_evidence_root(), _reviews_root()):
        if raw is None:
            continue
        real = os.path.realpath(raw)
        if any(s.base == real for s in stores):
            continue
        try:
            st = os.stat(real)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            stores.append(Store(real, st.st_dev))
    return stores


def _entries(store: Store) -> "list[str]":
    """Paths of the top-level entries of the store, as listed (a symlink stays a symlink)."""
    try:
        names = sorted(os.listdir(store.base))
    except OSError:
        return []
    return [os.path.join(store.base, name) for name in names]


def pressure_paths(ctx: ReapContext) -> "list[str]":
    return [s.base for s in _stores()]


def _read_state(path: Path) -> "dict | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_states(state_dir: Path) -> "dict[str, dict | None]":
    """Every state file by session id; None for one that cannot be read or parsed."""
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        return {}
    return {
        name[: -len(".json")]: _read_state(state_dir / name)
        for name in names if liveness.STATE_FILE_NAME.fullmatch(name)
    }


def _parent_of(data: dict) -> "tuple[str | None, bool]":
    """``(parent session id, well formed)`` of a state: (None, True) for a root."""
    link = data.get("parent_link")
    if link is None:
        return None, True
    parent = link.get("parent_session") if isinstance(link, dict) else None
    if not isinstance(parent, str) or not parent:
        return None, False
    return safe_session_id(parent), True


def _children_of(member: str, data: dict, states: "dict[str, dict | None]") -> "tuple[set[str], bool]":
    """``(sessions spawned by member, well formed)``: by `parent_link` in any state and by the member's own runs."""
    found = {
        sid for sid, other in states.items()
        if other is not None and isinstance(other.get("parent_link"), dict)
        and isinstance(other["parent_link"].get("parent_session"), str)
        and safe_session_id(other["parent_link"]["parent_session"]) == member
    }
    runs = data.get("effort_child_runs")
    if runs is None:
        return found, True
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        return found, False
    found.update(
        safe_session_id(run["child_session"]) for run in runs
        if isinstance(run.get("child_session"), str) and run["child_session"]
    )
    return found, True


def _lineage(entry: str, world: World) -> Lineage:
    """The tree of the session `entry` names: up to its root, then every descendant, read-only."""
    tree = Lineage()
    seen = []
    current = entry
    while True:
        data = world.states.get(current)
        if current in seen or data is None:
            tree.unreadable = f"{current}: state missing or unreadable"
            return tree
        seen.append(current)
        parent, well_formed = _parent_of(data)
        if not well_formed:
            tree.unreadable = f"{current}: malformed parent_link"
            return tree
        if parent is None:
            tree.root = current
            break
        current = parent
    pending = [tree.root]
    while pending:
        member = pending.pop()
        if member in tree.members:
            continue
        data = world.states.get(member)
        if member not in world.states:
            continue
        if data is None:
            tree.unreadable = f"{member}: state unreadable"
            return tree
        tree.members.add(member)
        kids, well_formed = _children_of(member, data, world.states)
        if not well_formed:
            tree.unreadable = f"{member}: malformed effort_child_runs"
            return tree
        pending.extend(kids)
    for member in tree.members:
        try:
            tree.newest = max(tree.newest, (world.state_dir / f"{member}.json").stat().st_mtime)
        except OSError as exc:
            tree.unreadable = f"{member}: {type(exc).__name__}"
            return tree
        for run in world.states[member].get("effort_child_runs") or ():
            ended = run.get("ended")
            if isinstance(ended, (int, float)) and not isinstance(ended, bool):
                tree.newest = max(tree.newest, float(ended))
            if _run_holds(run):
                tree.live = f"{member} has a run under living dispatcher pid {run.get('dispatcher_pid')}"
    return tree


def _run_holds(run: dict) -> bool:
    """An open run (`ended` null) whose dispatcher process is alive; a closed run, or one a dead dispatcher left open, holds nothing."""
    return run.get("ended") is None and child_runs.pid_alive(run.get("dispatcher_pid"))


def _retention(ctx: ReapContext) -> "tuple[int | None, str | None]":
    """``(days, None)`` or ``(None, slug)``: the retention of this pass."""
    value = ctx.params.get(NAME, {}).get(RETENTION_KEY)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        return None, RETENTION_UNSET
    if value < RETENTION_FLOOR_DAYS:
        return None, RETENTION_BELOW_FLOOR
    return value, None


def _world(ctx: ReapContext) -> World:
    days, problem = _retention(ctx)
    state_dir = config_root.agentctl_state_dir()
    try:
        mounts: "frozenset[str] | None" = liveness.read_mount_points()
    except OSError:
        mounts = None
    return World(_read_states(state_dir), state_dir, days, problem, liveness.sweep_holders(), mounts)


def _size(path: str, store: Store) -> int:
    """Disk bytes of an entry for the report; 0 when it cannot be measured."""
    try:
        top = os.lstat(path)
        if not stat.S_ISDIR(top.st_mode):
            return top.st_blocks * liveness.BLOCK_BYTES
        return liveness.walk_tree(path, top, store.dev).size
    except OSError:
        return 0


def _judge(path: str, store: Store, ctx: ReapContext, world: World) -> "tuple[liveness.Judgment, bool]":
    """``(judgment, tied)`` of one entry; a remove judgment names no clause that failed."""
    name = os.path.basename(path)
    tied = name in world.states

    def keep(slug: str, detail: str) -> "tuple[liveness.Judgment, bool]":
        return liveness.Judgment(KEEP, slug, detail, _size(path, store)), tied

    if world.retention_problem or world.retention_days is None:
        return keep(world.retention_problem or RETENTION_UNSET, f"{RETENTION_KEY} must be an integer of at least {RETENTION_FLOOR_DAYS} days")
    retention_s = world.retention_days * 86400.0
    if not tied:
        return keep(NO_SESSION, "no state file of the engine names this entry")
    tree = _lineage(name, world)
    if tree.unreadable:
        return keep(LINEAGE_UNREADABLE, tree.unreadable)
    root_state = world.states[tree.root] or {}
    if root_state.get("node") != RESOLVED_NODE:
        return keep(NOT_RESOLVED, f"lineage root {tree.root} is at {root_state.get('node')!r}")
    idle = ctx.now - tree.newest
    if idle < retention_s:
        return keep(RETENTION, f"lineage idle {idle / 86400.0:.1f}d, under the {world.retention_days}d retention")
    if tree.live:
        return keep(LINEAGE_LIVE, tree.live)
    judgment = liveness.judge_residue(
        path, root_dev=store.dev, now=ctx.now, floor_s=retention_s, sweep=world.sweep, mounts=world.mounts,
        owned=lambda p: ctx.owned_path(p) or ctx.owned_session(name),
    )
    return judgment, tied


def _verdict(store: Store, path: str, judgment: liveness.Judgment, tied: bool, ctx: ReapContext, enabled: bool) -> Verdict:
    tags = (f"store={store.base}",) + (() if tied else (UNTIED_TAG,))
    if judgment.action == REMOVE:
        if not ctx.dry_run and not enabled:
            reason = f"{OPT_IN_OFF}: {judgment.detail}; set {NAME} = true in the machine's reaper opt-in file to remove"
            return Verdict(path, KEEP, reason, tags=tags + (f"reason={OPT_IN_OFF}",), size=judgment.size)
        return Verdict(path, REMOVE, judgment.detail, tags=tags, size=judgment.size)
    return Verdict(path, KEEP, f"{judgment.slug}: {judgment.detail}", tags=tags + (f"reason={judgment.slug}",), size=judgment.size)


def scan(ctx: ReapContext) -> "list[Verdict]":
    stores = _stores()
    candidates = [(store, path) for store in stores for path in _entries(store)]
    if not candidates:
        return []
    enabled = opt_in.enabled(NAME)
    world = _world(ctx)
    verdicts = []
    for store, path in candidates:
        judgment, tied = _judge(path, store, ctx, world)
        verdicts.append(_verdict(store, path, judgment, tied, ctx, enabled))
    return verdicts


def remove(path: str, ctx: ReapContext) -> bool:
    if not opt_in.enabled(NAME):
        raise RemovalRefused(f"{OPT_IN_OFF}: {NAME} is not true in the machine's reaper opt-in file")
    parent = os.path.realpath(os.path.dirname(path))
    store = next((s for s in _stores() if s.base == parent), None)
    if store is None:
        raise RemovalRefused(f"{NOT_A_CANDIDATE}: not a top-level entry of a session-state store")
    candidate = os.path.join(parent, os.path.basename(path))
    judgment, _ = _judge(candidate, store, ctx, _world(ctx))
    if judgment.action != REMOVE:
        raise RemovalRefused(f"{judgment.slug}: {judgment.detail}")
    liveness.remove_tree(parent, os.path.basename(candidate))
    return True


def summary(verdicts: "list[Verdict]") -> str:
    stores = [s.base for s in _stores()]
    counts = {
        base: {"removable": 0, "removable_bytes": 0, "kept": 0, "kept_bytes": 0, "untied": 0, "untied_bytes": 0}
        for base in stores
    }
    reasons: "dict[str, list[int]]" = {slug: [0, 0] for slug in REASONS}
    for verdict in verdicts:
        tags = dict(tag.split("=", 1) for tag in verdict.tags if "=" in tag)
        base = tags.get("store")
        if base not in counts:
            continue
        bucket = "removable" if verdict.action == REMOVE else "kept"
        counts[base][bucket] += 1
        counts[base][f"{bucket}_bytes"] += verdict.size
        if tags.get("tied") == "0":
            counts[base]["untied"] += 1
            counts[base]["untied_bytes"] += verdict.size
        slug = tags.get("reason")
        if verdict.action == KEEP and slug in reasons:
            reasons[slug][0] += 1
            reasons[slug][1] += verdict.size
    lines = [
        f"{NAME} store={base} removable={c['removable']} removable_bytes={c['removable_bytes']}"
        f" kept={c['kept']} kept_bytes={c['kept_bytes']} untied={c['untied']} untied_bytes={c['untied_bytes']}"
        for base, c in counts.items()
    ]
    lines.extend(f"{NAME} reason={slug} count={n} bytes={b}" for slug, (n, b) in reasons.items() if n)
    return "\n".join(lines)
