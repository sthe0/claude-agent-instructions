"""Tests for the `core-session-state` reaper (scripts/reaper/builtin/core_session_state.py).

Hermetic: the agent home (hence the engine's state dir), the evidence store, the agent-reviews store
and HOME are built under tmp_path and reach the module through the seams it reads (CLAUDE_CONFIG_DIR,
AGENTCTL_EVIDENCE_ROOT, XDG_STATE_HOME). Session states are written with the engine's own store and
`SessionState`; a RESOLVED state is written the same way with its node set, since reaching RESOLVED
through the spine is what test_resolved_reentry_gate does. Time is ``ctx.now`` (a state file's mtime
and a directory's ctime cannot be set backwards, so the clock moves forward); the /proc sweep is
replaced by an empty one except where a test plants a holder. A dispatcher pid is this process (alive)
or a reaped child (dead); a run closed with ``ended`` set is built by hand, since closing one through
the engine books effort.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import config as agentctl_config  # noqa: E402
from agentctl.state import SessionState  # noqa: E402
from agentctl.store import FileStateStore  # noqa: E402
from reaper import liveness, registry, runner  # noqa: E402
from reaper.builtin import core_session_state as css  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext, RemovalRefused  # noqa: E402

DAY = 86400.0
SCRIPTS = Path(__file__).resolve().parents[1]
SLUG = re.compile(r"[a-z]+(-[a-z]+)*")


class World:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.state_dir = self.home / "agentctl" / "state"
        self.evidence = root / "evidence"
        self.reviews = root / "xdg" / "agent-reviews"
        for path in (self.state_dir, self.evidence, self.reviews):
            path.mkdir(parents=True)
        self.store = FileStateStore(self.state_dir)
        self.opt_in()

    def opt_in(self, enabled: bool = True, retention: "int | None" = None) -> None:
        path = self.home / "reaper" / "opt-in.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = f"{css.NAME} = {'true' if enabled else 'false'}\n"
        if retention is not None:
            text += f"[params.{css.NAME}]\n{css.RETENTION_KEY} = {retention}\n"
        path.write_text(text, encoding="utf-8")

    def session(self, sid: str, *, node: str = "RESOLVED", parent: "str | None" = None, runs=()) -> str:
        state = SessionState(session_id=sid, task_id=f"task-{sid}")
        state.node = node
        state.resolution.passed = node == "RESOLVED"
        state.approval.passed = node == "EXECUTING"
        if parent is not None:
            state.parent_link = {"parent_session": parent}
        state.effort_child_runs = [dict(run) for run in runs]
        self.store.save(state)
        return sid

    def dir(self, base: Path, name: str, payload: int = 3000) -> Path:
        path = base / name
        (path / "inner").mkdir(parents=True)
        (path / "inner" / "blob").write_bytes(b"x" * payload)
        return path

    def entry(self, sid: str, **kw) -> Path:
        """A session state plus its evidence directory."""
        self.session(sid, **kw)
        return self.dir(self.evidence, sid)

    def ctx(self, *, days: float = 20.0, retention: "int | None" = 3, dry_run: bool = False, **kw) -> ReapContext:
        params = {css.NAME: {css.RETENTION_KEY: retention}} if retention is not None else {}
        return ReapContext(
            now=time.time() + days * DAY, dry_run=dry_run, project_dir=self.root / "project",
            deadletter_dir=self.root / "deadletter", params=params, **kw,
        )

    def judged(self, ctx: "ReapContext | None" = None) -> "dict[str, str]":
        """Entry name -> ``remove`` or the keep slug, for every entry of both stores."""
        return {Path(v.path).name: outcome(v) for v in css.scan(ctx or self.ctx())}

    def state_snapshot(self) -> "list[tuple[str, int, bytes]]":
        return [(p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(self.state_dir.iterdir())]


def outcome(verdict) -> str:
    if verdict.action == REMOVE:
        return "remove"
    return dict(tag.split("=", 1) for tag in verdict.tags if "=" in tag)["reason"]


def run(*, child: "str | None" = None, ended: "float | None" = None, pid: "int | None" = None) -> dict:
    return {
        "child_session": child, "stage_index": 1, "started": time.time() - 60.0, "ended": ended,
        "dispatcher_pid": os.getpid() if pid is None else pid,
    }


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    built = World(tmp_path.resolve())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(built.home))
    monkeypatch.setenv("HOME", str(built.root / "userhome"))
    monkeypatch.setenv("AGENTCTL_EVIDENCE_ROOT", str(built.evidence))
    monkeypatch.setenv("XDG_STATE_HOME", str(built.root / "xdg"))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(liveness, "sweep_holders", lambda *a, **k: liveness.HolderSweep())
    return built


def hold(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(
        liveness, "sweep_holders",
        lambda *a, **k: liveness.HolderSweep(held={"cwd": [str(path)], "exe": [], "fd": [], "maps": []}),
    )


def refusal(path, ctx: ReapContext) -> str:
    with pytest.raises(RemovalRefused) as refused:
        css.remove(str(path), ctx)
    return str(refused.value)


# ── the clauses, one keep each ─────────────────────────────────────────────

def test_an_entry_of_a_resolved_idle_session_in_either_store_is_removable(world):
    world.entry("s1")
    world.session("s2")
    world.dir(world.reviews, "s2")
    verdicts = {(v.path): v for v in css.scan(world.ctx())}
    assert {Path(p).parent.name: v.action for p, v in verdicts.items()} == {"evidence": REMOVE, "agent-reviews": REMOVE}
    assert all(v.size > 0 and f"store={Path(p).parent}" in v.tags for p, v in verdicts.items())


def test_without_a_retention_nothing_is_removable_and_the_slug_says_why(world):
    world.entry("s1")
    assert world.judged(world.ctx(retention=None)) == {"s1": css.RETENTION_UNSET}
    assert world.judged(world.ctx(retention="many")) == {"s1": css.RETENTION_UNSET}
    assert world.judged(world.ctx(retention=True)) == {"s1": css.RETENTION_UNSET}


def test_a_retention_below_the_floor_keeps_and_the_floor_itself_is_accepted(world):
    world.entry("s1")
    assert css.RETENTION_FLOOR_DAYS == 3
    assert world.judged(world.ctx(retention=2)) == {"s1": css.RETENTION_BELOW_FLOOR}
    assert world.judged(world.ctx(retention=3)) == {"s1": "remove"}
    assert world.judged(world.ctx(retention="3")) == {"s1": "remove"}


def test_an_entry_no_state_names_is_kept_untied(world):
    world.dir(world.evidence, "ghost")
    [verdict] = css.scan(world.ctx())
    assert outcome(verdict) == css.NO_SESSION and css.UNTIED_TAG in verdict.tags and verdict.size > 0


def test_a_lineage_root_that_is_not_resolved_keeps_the_entry(world):
    world.entry("live", node="EXECUTING")
    assert world.judged() == {"live": css.NOT_RESOLVED}


def test_a_lineage_younger_than_the_retention_is_kept(world):
    world.entry("s1")
    assert world.judged(world.ctx(days=2)) == {"s1": css.RETENTION}
    assert world.judged(world.ctx(days=4)) == {"s1": "remove"}


def test_the_end_of_a_child_run_counts_as_the_lineage_age(world):
    ended = time.time() + 19 * DAY
    world.session("root", runs=[run(child="kid", ended=ended)])
    world.session("kid", node="EXECUTING", parent="root")
    world.dir(world.evidence, "root")
    assert world.judged() == {"root": css.RETENTION}


def test_a_child_is_judged_by_its_lineage_root_not_by_its_own_node(world):
    world.session("root", runs=[run(child="kid", ended=time.time())])
    world.session("kid", node="EXECUTING", parent="root")
    world.dir(world.evidence, "kid")
    assert world.judged() == {"kid": "remove"}


def test_a_running_child_holds_the_resolved_root_and_every_session_of_the_lineage(world):
    world.session("root")
    world.session("mid", node="EXECUTING", parent="root")
    world.session("leaf", node="EXECUTING", parent="mid", runs=[run(child="x")])
    for sid in ("root", "mid", "leaf"):
        world.dir(world.evidence, sid)
    assert world.judged() == {sid: css.LINEAGE_LIVE for sid in ("root", "mid", "leaf")}


def test_a_live_run_of_an_ancestor_holds_the_descendants_entries(world):
    world.session("root", runs=[run(child="mid")])
    world.session("mid", node="EXECUTING", parent="root")
    world.session("leaf", node="EXECUTING", parent="mid")
    for sid in ("root", "mid", "leaf"):
        world.dir(world.evidence, sid)
    assert world.judged() == {sid: css.LINEAGE_LIVE for sid in ("root", "mid", "leaf")}


def test_a_run_with_ended_set_releases_even_under_a_living_dispatcher(world):
    world.session("root", runs=[run(child="kid", ended=time.time())])
    world.session("kid", node="EXECUTING", parent="root")
    world.dir(world.evidence, "root")
    assert world.judged() == {"root": "remove"}


def test_an_open_run_under_a_dead_dispatcher_releases(world):
    world.session("root", runs=[run(child="kid", pid=dead_pid())])
    world.session("kid", node="EXECUTING", parent="root")
    world.dir(world.evidence, "root")
    assert world.judged() == {"root": "remove"}


def test_a_closed_run_under_a_dead_dispatcher_releases(world):
    world.session("root", runs=[run(child="kid", ended=time.time(), pid=dead_pid())])
    world.session("kid", node="EXECUTING", parent="root")
    world.dir(world.evidence, "root")
    assert world.judged() == {"root": "remove"}


def test_a_lineage_that_cannot_be_read_in_full_keeps_every_entry_of_it(world):
    world.session("good-root", runs=[run(child="good-root-kid", ended=time.time())])
    world.session("kid", node="EXECUTING", parent="broken-root")
    (world.state_dir / "broken-root.json").write_text("{not json", encoding="utf-8")
    world.session("loop-a", node="EXECUTING", parent="loop-b")
    world.session("loop-b", node="EXECUTING", parent="loop-a")
    world.session("odd", runs=[run(child="gone")])
    state = json.loads((world.state_dir / "odd.json").read_text(encoding="utf-8"))
    state["effort_child_runs"] = "not-a-list"
    (world.state_dir / "odd.json").write_text(json.dumps(state), encoding="utf-8")
    (world.state_dir / "good-root-kid.json").write_text("[]", encoding="utf-8")
    for sid in ("kid", "broken-root", "loop-a", "odd", "good-root"):
        world.dir(world.evidence, sid)
    assert world.judged() == {
        "kid": css.LINEAGE_UNREADABLE, "broken-root": css.LINEAGE_UNREADABLE, "loop-a": css.LINEAGE_UNREADABLE,
        "odd": css.LINEAGE_UNREADABLE, "good-root": css.LINEAGE_UNREADABLE,
    }


def test_a_process_holding_the_entry_keeps_it(world, monkeypatch):
    held = world.entry("s1")
    world.entry("s2")
    hold(monkeypatch, held / "inner")
    assert world.judged() == {"s1": liveness.HELD, "s2": "remove"}


def test_a_mount_point_inside_the_entry_keeps_it(world, monkeypatch):
    mounted = world.entry("s1")
    world.entry("s2")
    monkeypatch.setattr(liveness, "read_mount_points", lambda *a, **k: frozenset({str(mounted / "inner")}))
    assert world.judged() == {"s1": liveness.MOUNT_INSIDE, "s2": "remove"}


def test_an_unreadable_mount_table_keeps_everything(world, monkeypatch):
    world.entry("s1")

    def unreadable(*a, **k):
        raise OSError("no mountinfo")

    monkeypatch.setattr(liveness, "read_mount_points", unreadable)
    assert world.judged() == {"s1": liveness.OS_ERROR}


def test_a_nested_git_keeps_the_entry(world):
    nested = world.entry("s1")
    world.entry("s2")
    (nested / "inner" / ".git").mkdir()
    assert world.judged() == {"s1": liveness.NESTED_GIT, "s2": "remove"}


def test_fresh_content_keeps_the_entry_for_the_retention_not_for_a_shorter_floor(world):
    fresh = world.entry("s1")
    stamp = time.time() + 18 * DAY
    os.utime(fresh / "inner" / "blob", (stamp, stamp))
    assert world.judged(world.ctx(days=20, retention=3)) == {"s1": liveness.IDLE_FLOOR}
    stamp = time.time() + 14 * DAY
    os.utime(fresh / "inner" / "blob", (stamp, stamp))
    assert world.judged(world.ctx(days=20, retention=10)) == {"s1": liveness.IDLE_FLOOR}
    assert world.judged(world.ctx(days=20, retention=5)) == {"s1": "remove"}


def test_a_symlink_in_a_store_is_not_followed_and_one_inside_an_entry_is_not_entered(world):
    outside = world.dir(world.root, "outside")
    world.session("link")
    (world.evidence / "link").symlink_to(outside)
    inside = world.entry("s1")
    (inside / "inner" / "pointer").symlink_to(outside)
    assert world.judged() == {"link": liveness.NOT_A_DIRECTORY, "s1": "remove"}
    assert css.remove(str(inside), world.ctx()) is True
    assert not inside.exists() and (outside / "inner" / "blob").exists()
    assert (world.evidence / "link").is_symlink() and (outside / "inner" / "blob").exists()


def test_a_directory_on_another_filesystem_is_not_walked_into(world, monkeypatch):
    crossing = world.entry("crossing")
    world.entry("plain")
    real_judge = liveness.judge_residue

    def foreign_below_crossing(path, **kw):
        def lstat(p):
            real = os.lstat(p)
            if "crossing" not in p or p == path:
                return real
            fields = {n: getattr(real, n) for n in dir(real) if n.startswith("st_")}
            fields["st_dev"] = real.st_dev + 1
            return types.SimpleNamespace(**fields)

        return real_judge(path, **{**kw, "lstat_fn": lstat})

    monkeypatch.setattr(liveness, "judge_residue", foreign_below_crossing)
    assert world.judged() == {"crossing": liveness.MOUNT_INSIDE, "plain": "remove"}
    assert crossing.exists()


# ── opt-in, remove, parameters ─────────────────────────────────────────────

def test_without_the_opt_in_a_live_pass_keeps_what_it_would_remove_and_a_dry_run_shows_remove(world):
    world.entry("s1")
    world.opt_in(enabled=False)
    [kept] = css.scan(world.ctx())
    assert kept.action == KEEP and outcome(kept) == css.OPT_IN_OFF and kept.reason.startswith(css.OPT_IN_OFF)
    [shown] = css.scan(world.ctx(dry_run=True))
    assert shown.action == REMOVE


def test_remove_deletes_the_entry_and_nothing_beside_it(world):
    target = world.entry("s1")
    sibling = world.entry("s2")
    assert css.remove(str(target), world.ctx()) is True
    assert not target.exists() and sibling.exists()


def test_remove_refuses_without_the_opt_in_and_outside_the_stores(world):
    target = world.entry("s1")
    stranger = world.dir(world.root, "s1")
    assert css.NOT_A_CANDIDATE in refusal(stranger, world.ctx())
    world.opt_in(enabled=False)
    assert css.OPT_IN_OFF in refusal(target, world.ctx())
    assert target.exists() and stranger.exists()


def test_remove_judges_again_and_refuses_what_became_held_or_young(world, monkeypatch):
    held = world.entry("s1")
    young = world.entry("s2")
    assert world.judged() == {"s1": "remove", "s2": "remove"}
    hold(monkeypatch, held / "inner")
    assert liveness.HELD in refusal(held, world.ctx())
    assert css.RETENTION in refusal(young, world.ctx(days=1))
    assert held.exists() and young.exists()


def test_the_reaper_reads_engine_state_and_never_writes_it(world):
    world.session("root", runs=[run(child="kid", pid=dead_pid())])
    world.session("kid", node="EXECUTING", parent="root")
    removable = world.dir(world.evidence, "root")
    world.dir(world.evidence, "kid")
    before = world.state_snapshot()
    css.scan(world.ctx())
    css.remove(str(removable), world.ctx())
    assert world.state_snapshot() == before


def only_reaper(project, warn):
    return [r for r in registry.discover(project, warn=warn) if r.name == css.NAME]


def clock_forward(monkeypatch, days: float) -> None:
    original = time.time
    monkeypatch.setattr(time, "time", lambda: original() + days * DAY)


def test_a_param_overrides_the_table_in_a_dry_run_and_the_retention_is_not_stored(world, capsys, monkeypatch):
    entry = world.entry("s1")
    world.opt_in(retention=10)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.root / "project"))
    monkeypatch.setenv("CLAUDE_REAPER_PLUGIN_DIR", str(world.root / "plugins"))
    clock_forward(monkeypatch, 5)
    assert runner.main(["--dry-run", "--only", css.NAME], discover=only_reaper) == 0
    out = capsys.readouterr().out
    assert f"{css.NAME} KEEP {entry}" in out and f"{css.NAME} REMOVE {entry}" not in out
    assert runner.main(
        ["--dry-run", "--only", css.NAME, "--param", f"{css.NAME}.{css.RETENTION_KEY}=3"], discover=only_reaper,
    ) == 0
    assert f"{css.NAME} REMOVE {entry}" in capsys.readouterr().out
    assert entry.exists() and "retention_days = 10" in (world.home / "reaper" / "opt-in.toml").read_text(encoding="utf-8")


def test_a_param_below_the_floor_removes_nothing_in_a_dry_run(world, capsys, monkeypatch):
    entry = world.entry("s1")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.root / "project"))
    monkeypatch.setenv("CLAUDE_REAPER_PLUGIN_DIR", str(world.root / "plugins"))
    clock_forward(monkeypatch, 20)
    runner.main(["--dry-run", "--only", css.NAME, "--param", f"{css.NAME}.{css.RETENTION_KEY}=1"], discover=only_reaper)
    out = capsys.readouterr().out
    assert f"{css.NAME} KEEP {entry}" in out and f"{css.NAME} REMOVE" not in out


# ── summary ────────────────────────────────────────────────────────────────

def test_the_summary_has_a_line_per_store_and_a_slug_line_per_keep_reason_that_add_up(world):
    world.entry("gone")
    world.entry("young-root")
    world.entry("live", node="EXECUTING")
    world.dir(world.evidence, "ghost")
    world.session("gone")
    world.dir(world.reviews, "gone")
    os.utime(world.state_dir / "young-root.json", (time.time() + 19 * DAY, time.time() + 19 * DAY))
    verdicts = css.scan(world.ctx())
    lines = css.summary(verdicts).splitlines()
    stores = [l for l in lines if " store=" in l]
    assert [re.search(r"store=(\S+)", l).group(1) for l in stores] == [str(world.evidence), str(world.reviews)]
    pattern = (
        rf"{css.NAME} store=\S+ removable=(\d+) removable_bytes=(\d+) kept=(\d+) kept_bytes=(\d+) untied=(\d+) untied_bytes=(\d+)"
    )
    numbers = [list(map(int, re.fullmatch(pattern, l).groups())) for l in stores]
    assert numbers[0][0] == 1 and numbers[0][2] == 3 and numbers[0][4] == 1 and numbers[0][1] > 0 and numbers[0][5] > 0
    reasons = [l for l in lines if " reason=" in l]
    assert all(re.fullmatch(rf"{css.NAME} reason=[a-z-]+ count=\d+ bytes=\d+", l) for l in reasons)
    parsed = [re.fullmatch(rf"{css.NAME} reason=(\S+) count=(\d+) bytes=(\d+)", l).groups() for l in reasons]
    assert {slug for slug, _, _ in parsed} == {css.NO_SESSION, css.NOT_RESOLVED, css.RETENTION}
    assert sum(int(c) for _, c, _ in parsed) == sum(n[2] for n in numbers)
    assert sum(int(b) for _, _, b in parsed) == sum(n[3] for n in numbers)


def test_every_reason_the_module_can_give_is_a_slug_it_declares():
    emitted = {
        css.RETENTION_UNSET, css.RETENTION_BELOW_FLOOR, css.NO_SESSION, css.LINEAGE_UNREADABLE, css.NOT_RESOLVED,
        css.RETENTION, css.LINEAGE_LIVE, css.OPT_IN_OFF, liveness.NOT_A_DIRECTORY, liveness.FOREIGN_OWNER,
        liveness.MOUNT_INSIDE, liveness.SESSION_OWNED, liveness.HELD, liveness.HOLDER_SWEEP_INCOMPLETE,
        liveness.NESTED_GIT, liveness.IDLE_FLOOR, liveness.OS_ERROR,
    }
    assert emitted == set(css.REASONS) and all(SLUG.fullmatch(s) for s in emitted)


# ── disk pressure ──────────────────────────────────────────────────────────

def test_the_pressure_paths_are_the_existing_store_roots(world):
    assert css.pressure_paths(world.ctx()) == [str(world.evidence), str(world.reviews)]
    os.rmdir(world.reviews)
    assert css.pressure_paths(world.ctx()) == [str(world.evidence)]


def test_a_pressure_pass_removes_the_removable_entries_and_nothing_else(world, monkeypatch):
    gone = [world.entry("old1"), world.entry("old2")]
    kept = [
        world.entry("running", node="EXECUTING"), world.dir(world.evidence, "ghost"),
        world.entry("held-one"),
    ]
    hold(monkeypatch, kept[2] / "inner")
    config = world.root / "config.md"
    config.write_text("| Key | Value |\n|---|---|\n| `reaper-pressure-free-percent` | `10` | basis |\n", encoding="utf-8")
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", config)
    monkeypatch.setattr(runner, "free_percent", lambda path: 2.0)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.root / "project"))
    monkeypatch.setenv("CLAUDE_REAPER_PLUGIN_DIR", str(world.root / "plugins"))
    world.opt_in(retention=3)
    clock_forward(monkeypatch, 20)
    assert runner.main(["--on-pressure"], discover=only_reaper) == 0
    assert not any(path.exists() for path in gone) and all(path.exists() for path in kept)
    [record] = [json.loads(line) for line in runner.pressure_log_path().read_text(encoding="utf-8").splitlines()]
    assert record["removed"] == 2 and record["due"] == [css.NAME]
    rows = [json.loads(line) for line in runner.removal_log_path().read_text(encoding="utf-8").splitlines()]
    assert {r["trigger"] for r in rows} == {"pressure"} and len(rows) == 4


# ── nothing outside the session may read what this reaper removes ──────────

IN_SESSION_READERS = {
    "agentctl/cli.py", "agentctl/cli_common.py", "agentctl/cli_dispatch.py", "agentctl/cli_session.py",
    "spawn-specialist.py", "reaper/builtin/core_session_state.py",
}
EVIDENCE_NAMES = (
    "_evidence_session_dir", "evidence_dir_for", "agentctl-evidence", "AGENTCTL_EVIDENCE_ROOT", "agent-reviews",
)


def test_no_script_outside_the_in_session_readers_names_the_evidence_resolver_path_or_agent_reviews():
    assert all((SCRIPTS / rel).is_file() for rel in IN_SESSION_READERS)
    offenders = {}
    for path in sorted(SCRIPTS.rglob("*")):
        rel = path.relative_to(SCRIPTS).as_posix()
        if (
            not path.is_file() or path.suffix not in {".py", ".sh", ".toml", ".json"}
            or rel.startswith("tests/") or "__pycache__" in path.parts or rel in IN_SESSION_READERS
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        named = [name for name in EVIDENCE_NAMES if name in text]
        if named:
            offenders[rel] = named
    assert offenders == {}
