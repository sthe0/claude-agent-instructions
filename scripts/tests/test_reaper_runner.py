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
                 remove_result=None, summary_line=None, upkeep_lines=None, upkeep_exc=None):
        self.name = name
        self.verdicts = list(verdicts)
        self.scans = 0
        self.dues = []
        self.removed = []
        self.upkeep_ctxs = []
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
        if upkeep_lines is not None or upkeep_exc is not None:
            def upkeep(ctx):
                self.upkeep_ctxs.append(ctx)
                if upkeep_exc:
                    raise upkeep_exc
                return list(upkeep_lines)

            module.upkeep = upkeep
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


@pytest.mark.parametrize("throttle", ["0", "-1.5", "float('nan')", "float('inf')"])
def test_throttle_hours_that_is_not_finite_and_positive_violates_the_contract(tmp_path, throttle):
    root = tmp_path.resolve()
    plugin = root / "pl" / "reapers"
    plugin.mkdir(parents=True)
    plugin.joinpath("bad.py").write_text(
        f'NAME = "bad"\nTHROTTLE_HOURS = {throttle}\ndef scan(c): return []\ndef remove(p, c): pass\n',
        encoding="utf-8",
    )
    lines = []
    found = registry.discover(root, builtin_dir=root / "x" / "builtin", plugin_root=root / "pl", warn=lines.append)
    assert found == [] and len(lines) == 1 and "THROTTLE_HOURS" in lines[0]


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


def test_keep_on_a_directory_vetoes_a_remove_of_a_path_inside_it(env):
    inner = env.item / "inner"
    inner.mkdir()
    a, b = Fake("a", [rm(inner)]), Fake("b", [keep(env.item)])
    env.run([a, b])
    assert a.removed == []


def test_keep_on_a_path_vetoes_a_remove_of_a_directory_containing_it(env):
    inner = env.item / "inner"
    inner.mkdir()
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(inner)])
    env.run([a, b])
    assert a.removed == []


def test_keep_on_a_sibling_with_a_shared_name_prefix_does_not_veto(env):
    sibling = env.tmp / (env.item.name + "-other")
    sibling.mkdir()
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(sibling)])
    env.run([a, b])
    assert a.removed == [str(env.item)]


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


def test_unreadable_scope_registry_cancels_every_removal_and_advances_no_stamp(env):
    a = Fake("a", [rm(env.item)])
    out, err = io.StringIO(), io.StringIO()
    runner.execute_pass(
        [a.reaper], env.ctx(scope_registry_error="ValueError: bad json"),
        dry_run=False, force_run=False, only=None,
        stamps=env.stamps, log_path=env.log, out=out, err=err,
    )
    assert a.removed == [] and env.log_records() == []
    assert not env.stamps.exists()


def test_unreadable_scope_registry_shows_as_keep_in_a_dry_run(env):
    a = Fake("a", [rm(env.item)])
    out = io.StringIO()
    runner.execute_pass(
        [a.reaper], env.ctx(dry_run=True, scope_registry_error="ValueError: bad json"),
        dry_run=True, force_run=False, only=None,
        stamps=env.stamps, log_path=env.log, out=out, err=io.StringIO(),
    )
    assert out.getvalue().splitlines() == [
        f"a KEEP {env.item} (pass cancelled: scope registry unreadable: ValueError: bad json)"
    ]


def test_a_module_skipped_at_discovery_cancels_every_removal_but_still_prints(env):
    a = Fake("a", [rm(env.item)], summary_line="a: summary")
    out, err = io.StringIO(), io.StringIO()
    runner.execute_pass(
        [a.reaper], env.ctx(),
        dry_run=False, force_run=False, only=None,
        stamps=env.stamps, log_path=env.log, out=out, err=err,
        skipped_modules=["reaper: skipped /p/broken.py: no scan"],
    )
    assert a.removed == [] and env.log_records() == []
    assert not env.stamps.exists()
    assert out.getvalue().strip() == "a: summary"


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
    assert runner.main([], discover=lambda project, warn: [boom.reaper]) == 0
    assert "scan failed" in capsys.readouterr().err


def test_session_start_mode_exits_zero_even_if_discovery_itself_fails(hermetic, capsys):
    def explode(project, warn):
        raise RuntimeError("no discovery")

    assert runner.main([], discover=explode) == 0
    assert "runner error" in capsys.readouterr().err


def test_manual_mode_reports_a_runner_error_with_exit_one(hermetic):
    def explode(project, warn):
        raise RuntimeError("no discovery")

    assert runner.main(["--force-run"], discover=explode) == 1


def test_unknown_only_name_exits_two(hermetic):
    assert runner.main(["--only", "nope"], discover=lambda project, warn: []) == 2


def test_main_force_run_writes_the_removal_log_under_the_config_root(hermetic):
    item = hermetic / "victim"
    item.mkdir()
    a = Fake("a", [rm(item)])
    assert runner.main(["--force-run"], discover=lambda project, warn: [a.reaper]) == 0
    log = hermetic / "config" / "reaper" / "removed.jsonl"
    assert [json.loads(l)["path"] for l in log.read_text(encoding="utf-8").splitlines()] == [str(item)]
    assert not (hermetic / "home" / ".local" / "state" / "claude-reaper").exists()


def test_main_session_start_writes_stamps_under_the_home_state_dir(hermetic):
    a = Fake("a", [])
    assert runner.main([], discover=lambda project, warn: [a.reaper]) == 0
    assert (hermetic / "home" / ".local" / "state" / "claude-reaper" / "a.stamp").is_file()


def test_main_removes_nothing_when_one_scope_record_is_corrupt(hermetic, capsys):
    from lib import config_root

    scopes = config_root.agentctl_scopes_dir()
    scopes.mkdir(parents=True)
    (scopes / "broken-session.json").write_text("{not json", encoding="utf-8")
    item = hermetic / "victim"
    item.mkdir()
    a = Fake("a", [rm(item)])
    assert runner.main(["--force-run"], discover=lambda project, warn: [a.reaper]) == 0
    assert a.removed == []
    assert not (hermetic / "config" / "reaper" / "removed.jsonl").exists()


def test_main_removes_nothing_when_discovery_skipped_a_module(hermetic):
    item = hermetic / "victim"
    item.mkdir()
    a = Fake("a", [rm(item)])

    def discover(project, warn):
        warn("reaper: skipped /plugins/reapers/veto.py: import failed")
        return [a.reaper]

    assert runner.main(["--force-run"], discover=discover) == 0
    assert a.removed == []


def test_main_default_discovery_reports_a_broken_project_reaper_as_a_skip(hermetic, monkeypatch):
    project_reapers = hermetic / "project" / ".claude" / "reapers"
    project_reapers.mkdir(parents=True)
    (project_reapers / "veto.py").write_text("raise ImportError('missing dep')\n", encoding="utf-8")
    seen = {}

    def spy(reapers, ctx, **kw):
        seen["skipped"] = list(kw["skipped_modules"])

    monkeypatch.setattr(runner, "execute_pass", spy)
    assert runner.main(["--force-run"]) == 0
    assert len(seen["skipped"]) == 1 and "veto.py" in seen["skipped"][0]


def test_load_all_skips_a_corrupt_record_by_default_and_raises_when_strict(tmp_path):
    from session_scope import registry as scope_registry

    scope_registry.save(tmp_path, scope_registry.ScopeRecord(session_id="good"))
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert [r.session_id for r in scope_registry.load_all(tmp_path)] == ["good"]
    with pytest.raises(ValueError):
        scope_registry.load_all(tmp_path, strict=True)


def test_hook_script_lists_the_builtin_reaper(hermetic):
    import subprocess

    script = Path(__file__).resolve().parent.parent / "hook-reaper.py"
    out = subprocess.run(
        [sys.executable, str(script), "--list"], capture_output=True, text=True, check=True, env=os.environ.copy(),
    )
    assert any(line.startswith("git-worktrees builtin ") for line in out.stdout.splitlines())


# ── upkeep mode ───────────────────────────────────────────────────────────

def hold_upkeep_lock():
    import fcntl

    path = runner.upkeep_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def test_upkeep_only_prints_each_reapers_lines_and_neither_scans_nor_stamps(hermetic, capsys):
    a = Fake("a", [], upkeep_lines=["a backed up x"])
    plain = Fake("plain", [])
    assert runner.main(["--upkeep-only"], discover=lambda project, warn: [a.reaper, plain.reaper]) == 0
    assert capsys.readouterr().out.splitlines() == ["a backed up x"]
    assert a.scans == plain.scans == 0
    assert not list((hermetic / "home" / ".local" / "state" / "claude-reaper").glob("*.stamp"))


def test_an_upkeep_that_raises_costs_one_stderr_line_and_the_others_still_run(hermetic, capsys):
    boom = Fake("boom", [], upkeep_exc=RuntimeError("nope"))
    fine = Fake("fine", [], upkeep_lines=["fine did y"])
    assert runner.main(["--upkeep-only"], discover=lambda project, warn: [boom.reaper, fine.reaper]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["fine did y"]
    assert captured.err.splitlines() == ["reaper boom: upkeep failed: RuntimeError: nope"]


@pytest.mark.parametrize("argv", [[], ["--force-run"], ["--dry-run"], ["--only", "a"]])
def test_a_pass_without_upkeep_only_never_calls_upkeep(hermetic, argv):
    a = Fake("a", [], upkeep_lines=["pushed"])
    assert runner.main(argv, discover=lambda project, warn: [a.reaper]) == 0
    assert a.upkeep_ctxs == []


def test_upkeep_only_passes_dry_run_and_honours_only(hermetic):
    a = Fake("a", [], upkeep_lines=[])
    b = Fake("b", [], upkeep_lines=[])
    argv = ["--upkeep-only", "--dry-run", "--only", "a"]
    assert runner.main(argv, discover=lambda project, warn: [a.reaper, b.reaper]) == 0
    assert [c.dry_run for c in a.upkeep_ctxs] == [True] and b.upkeep_ctxs == []


@pytest.mark.parametrize("argv", [["--no-wait"], ["--upkeep-only", "--bogus"]])
def test_invalid_argv_exits_nonzero(hermetic, argv):
    with pytest.raises(SystemExit) as exc:
        runner.main(argv, discover=lambda project, warn: [])
    assert exc.value.code not in (0, None)


def test_upkeep_only_exits_zero_even_if_discovery_fails(hermetic, capsys):
    def explode(project, warn):
        raise RuntimeError("no discovery")

    assert runner.main(["--upkeep-only"], discover=explode) == 0
    assert "runner error" in capsys.readouterr().err


def test_held_lock_with_no_wait_exits_zero_without_running_upkeep(hermetic):
    a = Fake("a", [], upkeep_lines=["pushed"])
    handle = hold_upkeep_lock()
    try:
        assert runner.main(["--upkeep-only", "--no-wait"], discover=lambda project, warn: [a.reaper]) == 0
    finally:
        handle.close()
    assert a.upkeep_ctxs == []


def test_held_lock_without_no_wait_gives_up_after_the_wait_and_exits_zero(hermetic, monkeypatch):
    a = Fake("a", [], upkeep_lines=["pushed"])
    monkeypatch.setattr(runner, "UPKEEP_LOCK_WAIT_S", 0.3)
    monkeypatch.setattr(runner, "UPKEEP_LOCK_POLL_S", 0.05)
    handle = hold_upkeep_lock()
    try:
        assert runner.main(["--upkeep-only"], discover=lambda project, warn: [a.reaper]) == 0
    finally:
        handle.close()
    assert a.upkeep_ctxs == []


def test_a_waiting_run_proceeds_once_the_lock_is_released(hermetic, monkeypatch):
    import threading

    a = Fake("a", [], upkeep_lines=["pushed"])
    monkeypatch.setattr(runner, "UPKEEP_LOCK_WAIT_S", 5.0)
    monkeypatch.setattr(runner, "UPKEEP_LOCK_POLL_S", 0.05)
    handle = hold_upkeep_lock()
    threading.Timer(0.3, handle.close).start()
    assert runner.main(["--upkeep-only"], discover=lambda project, warn: [a.reaper]) == 0
    assert len(a.upkeep_ctxs) == 1


def test_upkeep_lock_is_released_after_the_run(hermetic):
    import fcntl

    a = Fake("a", [], upkeep_lines=[])
    assert runner.main(["--upkeep-only", "--no-wait"], discover=lambda project, warn: [a.reaper]) == 0
    with runner.upkeep_lock_path().open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_tags_survive_normalization():
    tagged = types.SimpleNamespace(path="/p", action=KEEP, reason="r", report=False, tags=["unbacked"])
    untagged = types.SimpleNamespace(path="/q", action=KEEP, reason="r")
    assert [v.tags for v in runner._normalize([tagged, untagged])] == [("unbacked",), ()]


# ── mutation catalogue ────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("REAPER_MUTATION_CHILD") == "1", reason="running inside a mutant subprocess")
def test_every_catalogue_mutant_is_killed_and_the_control_is_green():
    import reaper_mutation_control as control

    survivors = {name: control.run_mutant(name) for name in control.CATALOGUE}
    assert survivors == {name: control.KILLED for name in control.CATALOGUE}
    assert control.run_control() == 0
