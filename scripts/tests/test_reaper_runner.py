"""Tests for the reaper framework: discovery, arbitration, throttle, log and error isolation.

Hermetic: reapers here are in-memory fakes or throw-away files under tmp_path; HOME, the
config root, the plugin dir and the project dir are redirected there, so no test reads or
writes the real ~/.claude-agent or ~/.local/state, and none runs the real git-worktrees
reaper against a real checkout.
"""
from __future__ import annotations

import io
import json
import os
import sys
import textwrap
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper import registry, runner  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext, Verdict  # noqa: E402

NOW = 2_000_000_000.0
HOUR = 3600.0


class Fake:
    """An in-memory reaper that records scans and removals."""

    def __init__(self, name, verdicts=(), *, throttle=24.0, scan_exc=None, remove_exc=None,
                 remove_result=None, summary_line=None):
        self.name = name
        self.verdicts = list(verdicts)
        self.scans = 0
        self.dues = []
        self.removed = []
        self.remove_hook = None
        module = types.SimpleNamespace(NAME=name, THROTTLE_HOURS=throttle)

        def scan(ctx):
            self.scans += 1
            self.dues.append(ctx.due)
            if scan_exc:
                raise scan_exc
            return list(self.verdicts)

        def remove(path, ctx):
            if self.remove_hook:
                self.remove_hook(path)
            if remove_exc:
                raise remove_exc
            self.removed.append(path)
            return remove_result

        module.scan, module.remove = scan, remove
        if summary_line is not None:
            module.summary = lambda verdicts: summary_line
        self.reaper = registry.Reaper(name, "builtin", f"/fake/{name}.py", module)


def rm(path, reason="stale"):
    return Verdict(str(path), REMOVE, reason)


def keep(path, reason="in use"):
    return Verdict(str(path), KEEP, reason)


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.stamps = tmp / "stamps"
        self.log = tmp / "removed.jsonl"
        self.item = tmp / "item"
        self.item.mkdir()

    def ctx(self, **kw):
        return ReapContext(
            now=NOW, dry_run=kw.pop("dry_run", False), project_dir=self.tmp,
            deadletter_dir=self.tmp / "deadletter", **kw,
        )

    def run(self, fakes, *, dry_run=False, force_run=False, only=None):
        out, err = io.StringIO(), io.StringIO()
        runner.execute_pass(
            [f.reaper for f in fakes], self.ctx(dry_run=dry_run),
            dry_run=dry_run, force_run=force_run, only=only,
            stamps=self.stamps, log_path=self.log, out=out, err=err,
        )
        return out.getvalue(), err.getvalue()

    def stamp(self, name, at):
        runner._write_stamp(self.stamps, name, at)

    def log_records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path.resolve())


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


# ── discovery ─────────────────────────────────────────────────────────────

def write_reaper(directory: Path, filename: str, name: str, extra: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(textwrap.dedent(f'''
        from reaper.contract import Verdict
        NAME = "{name}"
        def scan(ctx):
            return []
        def remove(path, ctx):
            return None
    ''') + extra, encoding="utf-8")
    return path


def test_discovery_orders_layers_and_later_layer_replaces_same_name(tmp_path):
    root = tmp_path.resolve()
    builtin, plugin, project = root / "bi" / "builtin", root / "pl", root / "pr"
    write_reaper(builtin, "one.py", "alpha")
    write_reaper(builtin, "two.py", "beta")
    write_reaper(plugin / "reapers", "beta.py", "beta")
    write_reaper(project / ".claude" / "reapers", "gamma.py", "gamma")
    found = registry.discover(project, builtin_dir=builtin, plugin_root=plugin)
    assert [(r.name, r.layer) for r in found] == [("alpha", "builtin"), ("beta", "plugin"), ("gamma", "project")]
    assert Path(found[1].file) == plugin / "reapers" / "beta.py"


def test_project_layer_replaces_plugin_layer_of_same_name(tmp_path):
    root = tmp_path.resolve()
    write_reaper(root / "pl" / "reapers", "x.py", "shared")
    write_reaper(root / "pr" / ".claude" / "reapers", "x.py", "shared")
    found = registry.discover(root / "pr", builtin_dir=root / "empty" / "builtin", plugin_root=root / "pl")
    assert [(r.name, r.layer) for r in found] == [("shared", "project")]


def test_broken_modules_are_skipped_with_one_stderr_line_each(tmp_path):
    root = tmp_path.resolve()
    plugin = root / "pl" / "reapers"
    write_reaper(plugin, "good.py", "good")
    plugin.joinpath("syntax.py").write_text("def broken(:\n", encoding="utf-8")
    plugin.joinpath("exits.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    plugin.joinpath("noscan.py").write_text('NAME = "noscan"\n', encoding="utf-8")
    plugin.joinpath("badthrottle.py").write_text(
        'NAME = "bt"\nTHROTTLE_HOURS = "daily"\ndef scan(c): return []\ndef remove(p, c): pass\n',
        encoding="utf-8",
    )
    lines = []
    found = registry.discover(root, builtin_dir=root / "x" / "builtin", plugin_root=root / "pl", warn=lines.append)
    assert [r.name for r in found] == ["good"]
    assert len(lines) == 4 and all(line.startswith("reaper: skipped ") for line in lines)


def test_underscore_files_are_not_reapers(tmp_path):
    root = tmp_path.resolve()
    write_reaper(root / "pl" / "reapers", "_helper.py", "hidden")
    assert registry.discover(root, builtin_dir=root / "x" / "builtin", plugin_root=root / "pl") == []


def test_production_discovery_finds_git_worktrees_as_builtin(hermetic):
    found = registry.discover(hermetic / "project")
    assert ("git-worktrees", "builtin") in [(r.name, r.layer) for r in found]


def test_list_prints_name_layer_and_file_for_each_layer(hermetic, capsys):
    write_reaper(hermetic / "plugins" / "reapers", "mine.py", "plug-reaper")
    write_reaper(hermetic / "project" / ".claude" / "reapers", "local.py", "proj-reaper")
    assert runner.main(["--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("git-worktrees builtin ") for line in lines)
    assert f"plug-reaper plugin {hermetic / 'plugins' / 'reapers' / 'mine.py'}" in lines
    assert f"proj-reaper project {hermetic / 'project' / '.claude' / 'reapers' / 'local.py'}" in lines


# ── arbitration ───────────────────────────────────────────────────────────

def test_keep_wins_over_a_remove_on_the_same_path(env):
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(env.item)])
    env.run([a, b])
    assert a.removed == [] and env.log_records() == []


def test_keep_from_a_reaper_that_is_not_due_still_vetoes(env):
    env.stamp("b", NOW - HOUR)
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(env.item)])
    env.run([a, b])
    assert a.removed == [] and b.dues == [False]


def test_remove_proposed_only_by_a_reaper_that_is_not_due_is_not_executed(env):
    env.stamp("b", NOW - HOUR)
    a, b = Fake("a", []), Fake("b", [rm(env.item)])
    env.run([a, b])
    assert b.removed == []


def test_path_proposed_by_two_reapers_is_removed_once(env):
    a, b = Fake("a", [rm(env.item)]), Fake("b", [rm(env.item)])
    env.run([a, b])
    assert len(a.removed) + len(b.removed) == 1
    assert len(env.log_records()) == 1


def test_verdicts_are_joined_by_realpath(env):
    link = env.tmp / "alias"
    link.symlink_to(env.item)
    a, b = Fake("a", [rm(link)]), Fake("b", [keep(env.item)])
    env.run([a, b])
    assert a.removed == []


def test_invalid_verdict_action_counts_as_a_failed_scan(env):
    a = Fake("a", [Verdict(str(env.item), "delete", "bad")])
    b = Fake("b", [rm(env.tmp / "other")])
    _, err = env.run([a, b])
    assert "scan failed" in err and b.removed == []


# ── log, errors, stamps ───────────────────────────────────────────────────

def test_removal_is_logged_before_remove_runs(env):
    a = Fake("a", [rm(env.item, "stale one")])
    seen = []
    a.remove_hook = lambda path: seen.append(env.log_records())
    env.run([a])
    assert [[r["path"] for r in snapshot] for snapshot in seen] == [[str(env.item)]]
    record = env.log_records()[0]
    assert (record["reaper"], record["layer"], record["reason"]) == ("a", "builtin", "stale one")
    assert set(record) == {"ts", "reaper", "layer", "path", "reason"}


def test_scan_exception_cancels_every_removal_and_advances_no_stamp(env):
    a, b = Fake("a", [], scan_exc=RuntimeError("boom")), Fake("b", [rm(env.item)])
    _, err = env.run([a, b])
    assert b.removed == [] and env.log_records() == []
    assert not env.stamps.exists()
    assert "reaper a: scan failed" in err


def test_remove_exception_keeps_the_path_and_leaves_only_that_stamp_behind(env):
    second = env.tmp / "second"
    second.mkdir()
    a = Fake("a", [rm(env.item)], remove_exc=RuntimeError("busy"))
    b = Fake("b", [rm(second)])
    _, err = env.run([a, b])
    assert a.removed == [] and b.removed == [str(second)]
    assert runner._read_stamp(env.stamps, "a") is None
    assert runner._read_stamp(env.stamps, "b") == NOW
    assert "reaper a: FAILED to remove" in err


def test_remove_returning_false_is_a_failure(env):
    a = Fake("a", [rm(env.item)], remove_result=False)
    _, err = env.run([a])
    assert "FAILED to remove" in err and runner._read_stamp(env.stamps, "a") is None


def test_clean_pass_advances_the_stamp_of_each_due_reaper(env):
    a, b = Fake("a", []), Fake("b", [])
    env.run([a, b])
    assert runner._read_stamp(env.stamps, "a") == NOW == runner._read_stamp(env.stamps, "b")


# ── throttle and modes ────────────────────────────────────────────────────

def test_reaper_within_its_window_is_not_run(env):
    env.stamp("a", NOW - HOUR)
    a = Fake("a", [rm(env.item)])
    _, err = env.run([a])
    assert runner.THROTTLED_SENTINEL in err and a.scans == 0 and a.removed == []


def test_reaper_past_its_window_runs_and_restamps(env):
    env.stamp("a", NOW - 25 * HOUR)
    a = Fake("a", [rm(env.item)])
    env.run([a])
    assert a.removed == [str(env.item)]
    assert runner._read_stamp(env.stamps, "a") == NOW


def test_throttle_window_is_per_reaper(env):
    env.stamp("short", NOW - 2 * HOUR)
    env.stamp("long", NOW - 2 * HOUR)
    short, long = Fake("short", [], throttle=1.0), Fake("long", [], throttle=48.0)
    env.run([short, long])
    assert short.dues == [True] and long.dues == [False]


def test_dry_run_prints_one_line_per_verdict_and_changes_nothing(env):
    other = env.tmp / "other"
    other.mkdir()
    env.stamp("a", NOW - HOUR)
    a = Fake("a", [rm(env.item, "stale"), keep(other, "fresh")])
    out, _ = env.run([a], dry_run=True)
    assert out.splitlines() == [f"a REMOVE {env.item} (stale)", f"a KEEP {other} (fresh)"]
    assert a.removed == [] and not env.log.exists()
    assert runner._read_stamp(env.stamps, "a") == NOW - HOUR


def test_dry_run_prints_an_overridden_remove_as_keep_naming_the_vetoer(env):
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(env.item, "in use")])
    out, _ = env.run([a, b], dry_run=True)
    assert out.splitlines() == [f"a KEEP {env.item} (kept by b)", f"b KEEP {env.item} (in use)"]


def test_dry_run_under_a_failed_scan_shows_no_remove(env):
    a, b = Fake("a", [], scan_exc=RuntimeError("x")), Fake("b", [rm(env.item)])
    out, _ = env.run([a, b], dry_run=True)
    assert out.splitlines() == [f"b KEEP {env.item} (pass cancelled: scan of a failed)"]


def test_force_run_ignores_stamps_without_writing_them(env):
    env.stamp("a", NOW - HOUR)
    a = Fake("a", [rm(env.item)])
    env.run([a], force_run=True)
    assert a.removed == [str(env.item)]
    assert runner._read_stamp(env.stamps, "a") == NOW - HOUR


def test_only_restricts_removal_to_one_reaper_but_the_others_still_veto(env):
    second = env.tmp / "second"
    second.mkdir()
    a, b = Fake("a", [rm(env.item)]), Fake("b", [rm(second), keep(env.item)])
    env.stamp("b", NOW - HOUR)
    env.run([a, b], only="a")
    assert a.removed == [] and b.removed == []
    c = Fake("c", [rm(second)])
    env.run([a, c], only="a")
    assert c.removed == [] and a.scans >= 1
    assert runner._read_stamp(env.stamps, "a") is None


def test_only_dry_run_lists_just_that_reaper(env):
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(env.tmp / "elsewhere")])
    out, _ = env.run([a, b], dry_run=True, only="b")
    assert out.splitlines() == [f"b KEEP {env.tmp / 'elsewhere'} (in use)"]


def test_summary_line_is_printed_for_due_reapers_but_not_in_dry_run(env):
    a = Fake("a", [], summary_line="a: something to look at")
    out, _ = env.run([a])
    assert out.strip() == "a: something to look at"
    out, _ = env.run([a], dry_run=True)
    assert "something to look at" not in out


# ── CLI entry ─────────────────────────────────────────────────────────────

def test_session_start_mode_exits_zero_whatever_a_reaper_does(hermetic, capsys):
    boom = Fake("boom", [], scan_exc=RuntimeError("exploded"))
    assert runner.main([], discover=lambda project: [boom.reaper]) == 0
    assert "scan failed" in capsys.readouterr().err


def test_session_start_mode_exits_zero_even_if_discovery_itself_fails(hermetic, capsys):
    def explode(project):
        raise RuntimeError("no discovery")

    assert runner.main([], discover=explode) == 0
    assert "runner error" in capsys.readouterr().err


def test_manual_mode_reports_a_runner_error_with_exit_one(hermetic):
    def explode(project):
        raise RuntimeError("no discovery")

    assert runner.main(["--force-run"], discover=explode) == 1


def test_unknown_only_name_exits_two(hermetic):
    assert runner.main(["--only", "nope"], discover=lambda project: []) == 2


def test_main_force_run_writes_the_removal_log_under_the_config_root(hermetic):
    item = hermetic / "victim"
    item.mkdir()
    a = Fake("a", [rm(item)])
    assert runner.main(["--force-run"], discover=lambda project: [a.reaper]) == 0
    log = hermetic / "config" / "reaper" / "removed.jsonl"
    assert [json.loads(l)["path"] for l in log.read_text(encoding="utf-8").splitlines()] == [str(item)]
    assert not (hermetic / "home" / ".local" / "state" / "claude-reaper").exists()


def test_main_session_start_writes_stamps_under_the_home_state_dir(hermetic):
    a = Fake("a", [])
    assert runner.main([], discover=lambda project: [a.reaper]) == 0
    assert (hermetic / "home" / ".local" / "state" / "claude-reaper" / "a.stamp").is_file()


def test_hook_script_lists_the_builtin_reaper(hermetic):
    import subprocess

    script = Path(__file__).resolve().parent.parent / "hook-reaper.py"
    out = subprocess.run(
        [sys.executable, str(script), "--list"], capture_output=True, text=True, check=True, env=os.environ.copy(),
    )
    assert any(line.startswith("git-worktrees builtin ") for line in out.stdout.splitlines())


# ── mutation catalogue ────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("REAPER_MUTATION_CHILD") == "1", reason="running inside a mutant subprocess")
def test_every_catalogue_mutant_is_killed_and_the_control_is_green():
    import reaper_mutation_control as control

    survivors = {name: control.run_mutant(name) for name in control.CATALOGUE}
    assert survivors == {name: control.KILLED for name in control.CATALOGUE}
    assert control.run_control() == 0
