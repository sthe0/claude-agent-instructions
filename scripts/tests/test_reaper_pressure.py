"""Tests for the runner's disk-pressure mode (`--on-pressure`).

Hermetic: free space is injected (`runner.free_percent` is replaced), reapers are in-memory
fakes, and HOME, the config root and config.md are redirected to tmp_path, so no test reads
the real disk's free space, the real config.md, ~/.claude-agent or ~/.local/state.
"""
from __future__ import annotations

import fcntl
import io
import json
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import config as agentctl_config  # noqa: E402
from reaper import runner  # noqa: E402
from reaper.builtin import pytest_basetemp  # noqa: E402
from reaper.contract import ReapContext  # noqa: E402
from test_reaper_runner import NOW, Fake, keep, rm  # noqa: E402

THRESHOLD = 10.0
MINUTE = 60.0


def pressured_fake(name, verdicts, *paths, **kw):
    fake = Fake(name, verdicts, **kw)
    fake.reaper.module.pressure_paths = lambda ctx: [str(p) for p in paths]
    return fake


class PressureEnv:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.stamps = tmp / "stamps"
        self.log = tmp / "removed.jsonl"
        self.pressure_log = tmp / "pressure.jsonl"
        self.item = tmp / "item"
        self.item.mkdir()
        self.free: "dict[str, float | None]" = {}
        monkeypatch.setattr(runner, "free_percent", lambda path: self.free[path])

    def ctx(self, **kw):
        return ReapContext(
            now=kw.pop("now", NOW), dry_run=kw.pop("dry_run", False), project_dir=self.tmp,
            deadletter_dir=self.tmp / "deadletter", **kw,
        )

    def run(self, fakes, *, dry_run=False, now=NOW, threshold=THRESHOLD):
        out, err = io.StringIO(), io.StringIO()
        runner.execute_pressure(
            [f.reaper for f in fakes], self.ctx(dry_run=dry_run, now=now),
            threshold=threshold, stamps=self.stamps, log_path=self.log,
            pressure_log=self.pressure_log, out=out, err=err,
        )
        return out.getvalue(), err.getvalue()

    def readings(self, fakes, *, now=NOW, threshold=THRESHOLD, free_fn=None):
        return runner.pressure_readings(
            [f.reaper for f in fakes], self.ctx(now=now), threshold, self.stamps,
            free_fn or (lambda path: self.free[path]),
        )

    def log_records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def pressure_records(self):
        if not self.pressure_log.exists():
            return []
        return [json.loads(line) for line in self.pressure_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path, monkeypatch) -> PressureEnv:
    return PressureEnv(tmp_path.resolve(), monkeypatch)


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    for sub in ("home", "config", "project"):
        (root / sub).mkdir()
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("CLAUDE_REAPER_PLUGIN_DIR", str(root / "plugins"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root / "project"))
    return root


def write_config(path: Path, value: str) -> Path:
    path.write_text(f"| Key | Value |\n|---|---|\n| `reaper-pressure-free-percent` | `{value}` | basis |\n", encoding="utf-8")
    return path


@pytest.fixture
def armed(hermetic, monkeypatch):
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", write_config(hermetic / "config.md", "10"))
    return hermetic


def state_dir(root: Path) -> Path:
    return root / "home" / ".local" / "state" / "claude-reaper"


# ── when is a reaper due ──────────────────────────────────────────────────

def test_below_the_threshold_with_no_cooldown_stamp_is_due(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 9.9
    [reading] = env.readings([a])
    assert (reading.name, reading.free, reading.status) == ("a", 9.9, "due")


def test_equal_to_threshold_is_not_due(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = THRESHOLD
    assert [r.status for r in env.readings([a])] == ["ok"]


def test_above_the_threshold_is_ok(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 55.0
    assert [r.status for r in env.readings([a])] == ["ok"]


def test_cooldown_not_elapsed_is_not_due(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 1.0
    runner._write_stamp(env.stamps, "a.pressure", NOW - (runner.PRESSURE_COOLDOWN_MIN - 1) * MINUTE)
    assert [r.status for r in env.readings([a])] == ["cooldown"]


def test_cooldown_elapsed_is_due_again(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 1.0
    runner._write_stamp(env.stamps, "a.pressure", NOW - runner.PRESSURE_COOLDOWN_MIN * MINUTE)
    assert [r.status for r in env.readings([a])] == ["due"]


def test_the_throttle_stamp_does_not_start_the_cooldown(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 1.0
    env.stamps.mkdir()
    runner._write_stamp(env.stamps, "a", NOW)
    assert [r.status for r in env.readings([a])] == ["due"]


def test_the_lowest_free_percentage_among_the_paths_decides(env):
    roomy, tight = env.tmp / "roomy", env.tmp / "tight"
    a = pressured_fake("a", [], roomy, tight)
    env.free.update({str(roomy): 80.0, str(tight): 4.0})
    [reading] = env.readings([a])
    assert (reading.free, reading.status) == (4.0, "due")


def test_a_reaper_without_pressure_paths_gets_no_reading(env):
    plain = Fake("plain", [])
    env.free[str(env.tmp)] = 1.0
    assert env.readings([plain]) == []


def test_unreadable_free_space_gives_no_reading_so_never_due(env):
    a = pressured_fake("a", [], env.tmp)
    assert env.readings([a], free_fn=lambda path: None) == []


def test_pressure_paths_that_raise_give_no_reading(env):
    a = Fake("a", [])

    def boom(ctx):
        raise RuntimeError("no paths")

    a.reaper.module.pressure_paths = boom
    assert env.readings([a]) == []


# ── reading the disk and the config row ───────────────────────────────────

def test_free_percent_counts_blocks_available_to_the_user_not_free_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.os, "statvfs", lambda path: types.SimpleNamespace(f_bavail=1, f_bfree=3, f_blocks=4))
    assert runner.free_percent(str(tmp_path)) == 25.0


def test_free_percent_of_a_missing_path_measures_its_nearest_existing_ancestor(monkeypatch, tmp_path):
    probed = []

    def fake_statvfs(path):
        probed.append(path)
        return types.SimpleNamespace(f_bavail=1, f_bfree=1, f_blocks=2)

    monkeypatch.setattr(runner.os, "statvfs", fake_statvfs)
    assert runner.free_percent(str(tmp_path / "not" / "there")) == 50.0
    assert probed == [str(tmp_path)]


def test_free_percent_is_unreadable_for_a_filesystem_with_no_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.os, "statvfs", lambda path: types.SimpleNamespace(f_bavail=0, f_bfree=0, f_blocks=0))
    assert runner.free_percent(str(tmp_path)) is None


def test_the_threshold_is_read_from_the_config_row(tmp_path):
    assert runner.read_pressure_threshold(write_config(tmp_path / "config.md", "12.5")) == 12.5


def test_a_row_that_is_missing_or_not_a_percentage_disarms(tmp_path):
    bad = tmp_path / "config.md"
    for value in ("abc", "0", "100", "150", "-5", "nan", "inf"):
        assert runner.read_pressure_threshold(write_config(bad, value)) is None, value
    bad.write_text("| `other-key` | `10` | x |\n", encoding="utf-8")
    assert runner.read_pressure_threshold(bad) is None
    assert runner.read_pressure_threshold(tmp_path / "absent.md") is None


def test_the_shipped_config_row_arms_the_mode():
    assert runner.read_pressure_threshold(agentctl_config.REPO_ROOT / "config.md") is not None


def test_pytest_basetemp_names_its_root_as_its_pressure_path():
    assert pytest_basetemp.pressure_paths(None) == [str(pytest_basetemp.pytest_root())]


# ── what a pressured pass may do ──────────────────────────────────────────

def test_non_due_keep_vetoes_pressured_removal(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    b = Fake("b", [keep(env.item)])
    env.free[str(env.tmp)] = 3.0
    env.run([a, b])
    assert a.removed == []
    assert env.log_records() == []


def test_a_pressured_reaper_removes_what_nothing_vetoes(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    env.free[str(env.tmp)] = 3.0
    env.run([a])
    assert a.removed == [str(env.item)]
    assert [r["path"] for r in env.log_records()] == [str(env.item)]


def test_a_reaper_that_is_not_pressured_cannot_remove_even_what_it_proposes(env):
    a = pressured_fake("a", [], env.tmp)
    b = Fake("b", [rm(env.item)])
    env.free[str(env.tmp)] = 3.0
    env.run([a, b])
    assert b.removed == []


def test_every_reaper_scans_in_a_pressure_pass_but_only_the_pressured_one_is_due(env):
    a = pressured_fake("a", [], env.tmp)
    b = Fake("b", [])
    env.free[str(env.tmp)] = 3.0
    env.run([a, b])
    assert (a.scans, a.dues) == (1, [True])
    assert (b.scans, b.dues) == (1, [False])


def test_the_pressured_set_is_visible_to_scan_and_empty_in_a_normal_pass(env):
    seen = []
    a = pressured_fake("a", [], env.tmp)
    inner = a.reaper.module.scan
    a.reaper.module.scan = lambda ctx: (seen.append(ctx.pressured), inner(ctx))[1]
    env.free[str(env.tmp)] = 3.0
    env.run([a])
    runner.execute_pass(
        [a.reaper], env.ctx(), dry_run=False, force_run=True, only=None,
        stamps=env.stamps, log_path=env.log, out=io.StringIO(), err=io.StringIO(),
    )
    assert seen == [frozenset({"a"}), frozenset()]


def test_a_failed_scan_cancels_a_pressured_removal(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    broken = Fake("broken", [], scan_exc=RuntimeError("exploded"))
    env.free[str(env.tmp)] = 3.0
    env.run([a, broken])
    assert a.removed == []


def test_a_pressure_pass_writes_no_throttle_stamp(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 3.0
    env.run([a])
    assert not (env.stamps / "a.stamp").exists()


def test_a_pressure_pass_starts_the_cooldown(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 3.0
    env.run([a])
    assert float((env.stamps / "a.pressure.stamp").read_text(encoding="utf-8")) == NOW


def test_a_second_pass_inside_the_cooldown_does_not_run(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    env.free[str(env.tmp)] = 3.0
    env.run([a], now=NOW)
    env.run([a], now=NOW + MINUTE)
    assert (a.scans, a.removed) == (1, [str(env.item)])


def test_a_pass_after_the_cooldown_runs_again(env):
    a = pressured_fake("a", [], env.tmp)
    env.free[str(env.tmp)] = 3.0
    env.run([a], now=NOW)
    env.run([a], now=NOW + runner.PRESSURE_COOLDOWN_MIN * MINUTE)
    assert a.scans == 2


def test_an_ok_reading_runs_nothing_and_writes_nothing(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    env.free[str(env.tmp)] = 50.0
    out, err = env.run([a])
    assert (a.scans, out, err) == (0, "", "")
    assert not env.stamps.exists() and env.pressure_records() == []


def test_the_pass_is_recorded_with_the_free_percent_and_the_removed_count(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    env.free[str(env.tmp)] = 3.0
    env.run([a])
    [record] = env.pressure_records()
    assert (record["free"], record["due"], record["removed"], record["threshold"]) == ({"a": 3.0}, ["a"], 1, THRESHOLD)


# ── dry run ───────────────────────────────────────────────────────────────

def test_dry_run_prints_the_status_line_and_the_verdicts_and_changes_nothing(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    env.free[str(env.tmp)] = 3.0
    out, _ = env.run([a], dry_run=True)
    lines = out.splitlines()
    assert lines[0] == "a free 3.0% threshold 10.0% -> due"
    assert lines[1] == f"a REMOVE {env.item} (stale)"
    assert (a.removed, env.log_records(), env.pressure_records()) == ([], [], [])
    assert not env.stamps.exists()


def test_dry_run_status_line_has_the_documented_shape_for_every_status(env):
    a = pressured_fake("a", [], env.tmp)
    shape = re.compile(r"^a free [0-9.]+% threshold [0-9.]+% -> (due|ok|cooldown)$")
    for free, stamp in ((50.0, None), (3.0, None), (3.0, NOW)):
        env.free[str(env.tmp)] = free
        if stamp:
            runner._write_stamp(env.stamps, "a.pressure", stamp)
        out, _ = env.run([a], dry_run=True)
        assert shape.match(out.splitlines()[0]), out


def test_dry_run_prints_a_veto_as_keep(env):
    a = pressured_fake("a", [rm(env.item)], env.tmp)
    b = Fake("b", [keep(env.item)])
    env.free[str(env.tmp)] = 3.0
    out, _ = env.run([a, b], dry_run=True)
    assert f"a KEEP {env.item} (kept by b)" in out.splitlines()


# ── main ──────────────────────────────────────────────────────────────────

def discover_of(*fakes):
    return lambda project, warn: [f.reaper for f in fakes]


def test_main_runs_the_pressured_reaper_and_stamps_only_the_cooldown(armed, monkeypatch):
    victim = armed / "victim"
    victim.mkdir()
    a = pressured_fake("a", [rm(victim)], armed)
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    assert a.removed == [str(victim)]
    assert (state_dir(armed) / "a.pressure.stamp").is_file()
    assert not (state_dir(armed) / "a.stamp").exists()
    assert (armed / "config" / "reaper" / "pressure.jsonl").is_file()


def test_main_disarms_on_a_bad_row_and_runs_nothing(hermetic, monkeypatch, capsys):
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", write_config(hermetic / "config.md", "0"))
    a = pressured_fake("a", [], hermetic)
    monkeypatch.setattr(runner, "free_percent", lambda path: 1.0)
    assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    assert "disarmed" in capsys.readouterr().err
    assert a.scans == 0


def test_main_disarms_when_config_md_cannot_be_read(hermetic, monkeypatch, capsys):
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", hermetic / "absent.md")
    a = pressured_fake("a", [], hermetic)
    monkeypatch.setattr(runner, "free_percent", lambda path: 1.0)
    assert runner.main(["--on-pressure", "--dry-run"], discover=discover_of(a)) == 0
    assert "disarmed" in capsys.readouterr().err


def test_main_with_the_lock_held_exits_zero_without_running(armed, monkeypatch):
    a = pressured_fake("a", [], armed)
    monkeypatch.setattr(runner, "free_percent", lambda path: 1.0)
    path = runner.pressure_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    assert a.scans == 0


def test_main_dry_run_takes_no_lock_and_prints_its_line_while_a_run_holds_it(armed, monkeypatch, capsys):
    a = pressured_fake("a", [], armed)
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    path = runner.pressure_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert runner.main(["--on-pressure", "--dry-run"], discover=discover_of(a)) == 0
    assert capsys.readouterr().out.splitlines()[0] == "a free 4.0% threshold 10.0% -> due"


def test_main_releases_the_lock_after_the_run(armed, monkeypatch):
    a = pressured_fake("a", [], armed)
    monkeypatch.setattr(runner, "free_percent", lambda path: 1.0)
    assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    with runner.pressure_lock_path().open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_on_pressure_combines_only_with_dry_run(hermetic):
    for extra in (["--upkeep-only"], ["--only", "a"], ["--force-run"]):
        with pytest.raises(SystemExit) as raised:
            runner.main(["--on-pressure", *extra], discover=discover_of())
        assert raised.value.code == 2, extra


def test_a_live_pressure_run_exits_zero_even_if_discovery_fails(armed, capsys):
    def explode(project, warn):
        raise RuntimeError("no discovery")

    assert runner.main(["--on-pressure"], discover=explode) == 0
    assert "runner error" in capsys.readouterr().err


def test_a_normal_pass_ignores_the_pressure_paths(hermetic, monkeypatch):
    a = pressured_fake("a", [], hermetic)
    monkeypatch.setattr(runner, "free_percent", lambda path: 1.0 / 0.0)
    assert runner.main([], discover=discover_of(a)) == 0
    assert (state_dir(hermetic) / "a.stamp").is_file()
    assert not (state_dir(hermetic) / "a.pressure.stamp").exists()
