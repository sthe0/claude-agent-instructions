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
import re
import sys
import tempfile
import textwrap
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper import registry, runner  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext, RemovalRefused, Verdict  # noqa: E402

NOW = 2_000_000_000.0
HOUR = 3600.0


class Fake:
    """An in-memory reaper that records scans and removals."""

    def __init__(self, name, verdicts=(), *, throttle=24.0, scan_exc=None, remove_exc=None,
                 remove_result=None, summary_line=None, upkeep_lines=None, upkeep_exc=None, session_sources=None):
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
        if session_sources is not None:
            module.SESSION_SOURCES = session_sources
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

    def attempt_records(self):
        """The rows written before each removal; the outcome rows that follow them are left out."""
        return [r for r in self.log_records() if "outcome" not in r]


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("AGENT_REAPER_UNIT", raising=False)
    return Env(tmp_path.resolve())


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    for sub in ("home", "config", "project"):
        (root / sub).mkdir()
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("AGENT_REAPER_UNIT", raising=False)
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
    assert len(env.attempt_records()) == 1


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
    assert set(record) == {"ts", "reaper", "layer", "path", "reason", "trigger"}


def outcomes(env):
    return [(r["path"], r["outcome"], r["detail"]) for r in env.log_records() if "outcome" in r]


def test_each_attempt_is_followed_by_its_outcome_row(env):
    second, third = env.tmp / "second", env.tmp / "third"
    second.mkdir()
    third.mkdir()
    a = Fake("a", [rm(env.item, "stale one")])
    b = Fake("b", [rm(second)], remove_exc=OSError("busy"))
    c = Fake("c", [rm(third)], remove_exc=RemovalRefused("held: cwd of pid 7"))
    env.run([a, b, c])
    assert sorted(outcomes(env)) == sorted([
        (str(env.item), "removed", "stale one"),
        (str(second), "failed", "OSError: busy"),
        (str(third), "refused", "held: cwd of pid 7"),
    ])
    rows = env.log_records()
    for path in (env.item, second, third):
        kinds = ["outcome" in r for r in rows if r["path"] == str(path)]
        assert kinds == [False, True]
    assert len({r["trigger"] for r in rows}) == 1


def test_a_refused_removal_is_kept_with_its_reason_and_is_not_an_error(env):
    a = Fake("a", [rm(env.item, "no holder, idle 80h")], remove_exc=RemovalRefused("fresh: idle 1h"))
    observe = {"errors": 0, "reapers_run": 0}
    out, err = io.StringIO(), io.StringIO()
    env.kept = env.tmp / "kept"
    runner.execute_pass(
        [a.reaper], env.ctx(), dry_run=False, force_run=False, only=None,
        stamps=env.stamps, log_path=env.log, out=out, err=err, kept_dir=env.kept, observe=observe,
    )
    assert "refused to remove" in err.getvalue() and "FAILED" not in err.getvalue()
    assert observe["errors"] == 0 and runner._read_stamp(env.stamps, "a") == NOW
    assert json.loads((env.kept / "a.json").read_text())["kept"] == [{
        "path": str(env.item), "reason": "removal-refused", "detail": "fresh: idle 1h", "bytes": 0,
    }]


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


def test_summary_line_is_printed_for_due_reapers_and_in_a_dry_run(env):
    a = Fake("a", [], summary_line="a: something to look at")
    out, _ = env.run([a])
    assert out.strip() == "a: something to look at"
    out, _ = env.run([a], dry_run=True)
    assert out.strip() == "a: something to look at"


def test_a_summary_is_not_printed_for_a_reaper_that_is_not_selected_in_a_dry_run(env):
    a = Fake("a", [], summary_line="a: look")
    b = Fake("b", [], summary_line="b: look")
    out, _ = env.run([a, b], dry_run=True, only="b")
    assert out.strip() == "b: look"


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
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["path"], r.get("outcome")) for r in rows] == [(str(item), None), (str(item), "removed")]
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


# ── session gate ──────────────────────────────────────────────────────────

GATE_SOURCES = frozenset({"startup", "clear"})
GATE_LINE = "gated notice"


def session_start(hermetic, monkeypatch, fakes, *, payload, depth=None, entrypoint="cli", argv=()):
    """One ``runner.main`` pass as the SessionStart hook runs it: ``payload`` is the text on stdin
    (None: a terminal), ``depth`` AGENT_RECURSION_DEPTH, ``entrypoint`` CLAUDE_CODE_ENTRYPOINT."""
    stdin = io.StringIO(payload if payload is not None else "")
    if payload is None:
        stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    if depth is not None:
        monkeypatch.setenv("AGENT_RECURSION_DEPTH", depth)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    assert runner.main(list(argv), discover=lambda project, warn: [f.reaper for f in fakes]) == 0


def gated_stamp(hermetic, name="g"):
    return hermetic / "home" / ".local" / "state" / "claude-reaper" / f"{name}.stamp"


def source_payload(source):
    return json.dumps({"session_id": "s1", "cwd": "/w", "hook_event_name": "SessionStart", "source": source})


@pytest.mark.parametrize("source", ["startup", "clear", "compact", "resume"])
@pytest.mark.parametrize("depth", ["0", "1", None])
@pytest.mark.parametrize("entrypoint", ["cli", "sdk-cli"])
def test_a_gated_reaper_is_due_only_in_a_fresh_interactive_top_level_start(
    hermetic, monkeypatch, capsys, source, depth, entrypoint,
):
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    other = Fake("u", [], summary_line="ungated notice")
    shown = source in GATE_SOURCES and depth in (None, "0") and entrypoint == "cli"
    session_start(hermetic, monkeypatch, [gated, other], payload=source_payload(source), depth=depth, entrypoint=entrypoint)
    out = capsys.readouterr().out.splitlines()
    assert (GATE_LINE in out) is shown
    assert "ungated notice" in out
    assert gated_stamp(hermetic).is_file() is shown
    assert gated_stamp(hermetic, "u").is_file()
    assert gated.dues == [shown]


@pytest.mark.parametrize("payload", [None, "", "not json", "[]", '{"cwd": "/w"}', '{"source": 3}'])
def test_a_gated_reaper_is_never_due_without_a_usable_payload(hermetic, monkeypatch, capsys, payload):
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    other = Fake("u", [])
    session_start(hermetic, monkeypatch, [gated, other], payload=payload)
    assert GATE_LINE not in capsys.readouterr().out
    assert not gated_stamp(hermetic).exists()
    assert gated.dues == [False]


def test_a_second_fresh_start_inside_the_throttle_prints_nothing(hermetic, monkeypatch, capsys):
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("startup"))
    assert capsys.readouterr().out.splitlines() == [GATE_LINE]
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("clear"))
    assert capsys.readouterr().out == ""
    assert gated.scans == 1


def test_a_start_that_shows_nothing_does_not_spend_the_throttle_of_a_later_fresh_start(hermetic, monkeypatch, capsys):
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("compact"))
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("startup"), depth="1")
    assert capsys.readouterr().out == ""
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("startup"))
    assert capsys.readouterr().out.splitlines() == [GATE_LINE]


def test_a_reaper_without_session_sources_runs_on_any_start_as_before(hermetic, monkeypatch, capsys):
    plain = Fake("g", [], summary_line=GATE_LINE)
    session_start(hermetic, monkeypatch, [plain], payload=source_payload("compact"), depth="1", entrypoint="sdk-cli")
    assert capsys.readouterr().out.splitlines() == [GATE_LINE]
    assert gated_stamp(hermetic).is_file()


@pytest.mark.parametrize("argv", [["--force-run"], ["--dry-run"], ["--only", "g"]])
def test_manual_modes_ignore_the_gate(hermetic, monkeypatch, capsys, argv):
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("compact"), depth="1", entrypoint="sdk-cli", argv=argv)
    assert gated.dues == [True]
    assert GATE_LINE in capsys.readouterr().out
    assert not gated_stamp(hermetic).exists()


def test_a_start_with_no_gated_reaper_does_not_read_stdin(hermetic, monkeypatch):
    reads = []

    class Counting(io.StringIO):
        def read(self, *args):
            reads.append(1)
            return super().read(*args)

    monkeypatch.setattr(sys, "stdin", Counting("{}"))
    plain = Fake("g", [])
    assert runner.main([], discover=lambda project, warn: [plain.reaper]) == 0
    assert reads == []


def test_a_start_where_every_gated_reaper_is_inside_its_throttle_does_not_read_stdin(hermetic, monkeypatch, capsys):
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    session_start(hermetic, monkeypatch, [gated], payload=source_payload("startup"))
    assert capsys.readouterr().out.splitlines() == [GATE_LINE]

    reads = []

    class Counting(io.StringIO):
        def read(self, *args):
            reads.append(1)
            return super().read(*args)

    monkeypatch.setattr(sys, "stdin", Counting(source_payload("clear")))
    assert runner.main([], discover=lambda project, warn: [gated.reaper]) == 0
    assert gated.scans == 1 and reads == []


def test_a_gated_reaper_past_its_throttle_makes_the_start_read_stdin(hermetic, monkeypatch):
    reads = []

    class Counting(io.StringIO):
        def read(self, *args):
            reads.append(1)
            return super().read(*args)

    monkeypatch.setattr(sys, "stdin", Counting(source_payload("startup")))
    gated = Fake("g", [], summary_line=GATE_LINE, session_sources=GATE_SOURCES)
    assert runner.main([], discover=lambda project, warn: [gated.reaper]) == 0
    assert reads


def test_a_reaper_with_a_malformed_session_sources_is_skipped_at_default_discovery(hermetic, monkeypatch, capsys):
    plugins = hermetic / "plugins" / "reapers"
    write_reaper(plugins, "bad.py", "bad", 'SESSION_SOURCES = {"startup"}\n')
    write_reaper(plugins, "empty.py", "empty", "SESSION_SOURCES = frozenset()\n")
    write_reaper(plugins, "good.py", "good", 'SESSION_SOURCES = frozenset({"startup"})\n')
    assert runner.main(["--list"]) == 0
    captured = capsys.readouterr()
    assert [line.split()[0] for line in captured.out.splitlines() if "/plugins/" in line] == ["good"]
    assert "bad.py" in captured.err and "empty.py" in captured.err and "SESSION_SOURCES" in captured.err


def test_session_of_reads_source_depth_and_entrypoint():
    top = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
    assert runner.session_of({"source": "startup"}, top) == ("startup", True, True)
    assert runner.session_of({"source": "startup"}, {**top, "AGENT_RECURSION_DEPTH": "0"}) == ("startup", True, True)
    assert runner.session_of({"source": "startup"}, {**top, "AGENT_RECURSION_DEPTH": "2"}) == ("startup", False, True)
    assert runner.session_of({"source": "startup"}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) == ("startup", True, False)
    assert runner.session_of({"source": "startup"}, {}) == ("startup", True, False)
    assert runner.session_of({"source": ""}, top) is None
    assert runner.session_of(None, top) is None


def test_read_session_payload_takes_one_json_object_and_nothing_else():
    assert runner.read_session_payload(io.StringIO('{"source": "startup"}')) == {"source": "startup"}
    assert runner.read_session_payload(io.StringIO("[1]")) is None
    assert runner.read_session_payload(io.StringIO("")) is None
    assert runner.read_session_payload(io.StringIO('{"pad": "' + "x" * runner.SESSION_PAYLOAD_MAX_BYTES + '"}')) is None
    assert runner.read_session_payload(None) is None


def test_read_session_payload_does_not_wait_for_a_pipe_the_writer_keeps_open(monkeypatch):
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, b'{"source": "startup"}')
        with os.fdopen(os.dup(read_end), "r") as stream:
            assert runner.read_session_payload(stream) == {"source": "startup"}
    finally:
        os.close(read_end)
        os.close(write_end)


def test_read_session_payload_gives_up_on_a_pipe_that_stays_silent(monkeypatch):
    monkeypatch.setattr(runner, "SESSION_PAYLOAD_WAIT_S", 0.2)
    read_end, write_end = os.pipe()
    try:
        with os.fdopen(os.dup(read_end), "r") as stream:
            assert runner.read_session_payload(stream) is None
    finally:
        os.close(read_end)
        os.close(write_end)


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


def test_size_survives_normalization_and_defaults_to_zero():
    sized = types.SimpleNamespace(path="/p", action=KEEP, reason="r", size=4096)
    unsized = types.SimpleNamespace(path="/q", action=KEEP, reason="r")
    assert [v.size for v in runner._normalize([sized, unsized])] == [4096, 0]


# ── trigger, invocation id and the kept snapshot ──────────────────────────

def run_live(env, fakes, *, force_run=False, dry_run=False, kept=True):
    out, err = io.StringIO(), io.StringIO()
    runner.execute_pass(
        [f.reaper for f in fakes], env.ctx(dry_run=dry_run),
        dry_run=dry_run, force_run=force_run, only=None,
        stamps=env.stamps, log_path=env.log, out=out, err=err,
        kept_dir=env.tmp / "kept" if kept else None,
    )
    return out.getvalue(), err.getvalue()


def kept_snapshot(env, name):
    return json.loads((env.tmp / "kept" / f"{name}.json").read_text(encoding="utf-8"))


def test_a_pass_under_systemd_records_trigger_unit_and_the_invocation_id_in_row_and_snapshot(env, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    a = Fake("a", [rm(env.item), keep(env.tmp / "other", "busy")])
    run_live(env, [a])
    [row] = env.attempt_records()
    snapshot = kept_snapshot(env, "a")
    assert (row["trigger"], row["invocation_id"]) == ("unit", "0123abcd")
    assert (snapshot["trigger"], snapshot["invocation_id"]) == ("unit", "0123abcd")


def test_an_inherited_invocation_id_without_the_unit_marker_is_not_a_unit_pass(env, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    a = Fake("a", [rm(env.item), keep(env.tmp / "other", "busy")])
    run_live(env, [a])
    [row] = env.attempt_records()
    snapshot = kept_snapshot(env, "a")
    assert "invocation_id" not in row and "invocation_id" not in snapshot
    assert (row["trigger"], snapshot["trigger"]) == ("hook", "hook")


def test_the_unit_marker_without_an_invocation_id_is_not_a_unit_pass(env, monkeypatch):
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    run_live(env, [Fake("a", [rm(env.item)])])
    assert env.log_records()[0]["trigger"] == "hook"


def test_a_pass_outside_systemd_records_no_invocation_id(env):
    a = Fake("a", [rm(env.item), keep(env.tmp / "other", "busy")])
    run_live(env, [a])
    [row] = env.attempt_records()
    snapshot = kept_snapshot(env, "a")
    assert "invocation_id" not in row and "invocation_id" not in snapshot
    assert (row["trigger"], snapshot["trigger"]) == ("hook", "hook")


def test_a_manual_pass_records_trigger_manual(env):
    a = Fake("a", [rm(env.item)])
    run_live(env, [a], force_run=True)
    assert env.log_records()[0]["trigger"] == "manual"


def test_the_kept_snapshot_lists_every_keep_with_reason_and_bytes_and_none_of_the_removals(env):
    other = env.tmp / "other"
    kept_verdict = types.SimpleNamespace(path=str(other), action=KEEP, reason="busy: held", size=123)
    removed = types.SimpleNamespace(path=str(env.item), action=REMOVE, reason="stale", size=9)
    a = Fake("a", [])
    a.verdicts = [kept_verdict, removed]
    run_live(env, [a])
    snapshot = kept_snapshot(env, "a")
    assert snapshot["kept"] == [{"path": str(other), "reason": "unclassified", "detail": "busy: held", "bytes": 123}]
    assert snapshot["reaper"] == "a" and snapshot["pass_time"].endswith("+00:00")


def test_the_snapshot_and_its_entries_have_exactly_the_documented_keys(env, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    a = Fake("a", [keep(env.tmp / "one", "busy")])
    run_live(env, [a])
    snapshot = kept_snapshot(env, "a")
    assert set(snapshot) == {"reaper", "pass_time", "trigger", "invocation_id", "kept"}
    assert set(snapshot["kept"][0]) == {"path", "reason", "detail", "bytes"}


def test_the_removal_row_has_exactly_the_documented_keys(env, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    run_live(env, [Fake("a", [rm(env.item)])])
    [row, outcome] = env.log_records()
    assert set(row) == {"ts", "reaper", "layer", "path", "reason", "trigger", "invocation_id"}
    assert set(outcome) == {"ts", "reaper", "layer", "path", "outcome", "detail", "trigger", "invocation_id"}


def test_a_keep_verdicts_reason_tag_is_the_entry_slug_and_its_text_is_the_detail(env):
    tagged = Verdict(
        str(env.tmp / "one"), KEEP, "holder-sweep-incomplete: pid=7 name='x' argv0='x' errno=13 (EACCES): cwd",
        tags=("kind=tmp", "reason=holder-sweep-incomplete"),
    )
    run_live(env, [Fake("a", [tagged])])
    [entry] = kept_snapshot(env, "a")["kept"]
    assert entry["reason"] == "holder-sweep-incomplete"
    assert entry["detail"] == "holder-sweep-incomplete: pid=7 name='x' argv0='x' errno=13 (EACCES): cwd"


def test_the_reapers_snapshot_extra_fields_join_the_snapshot_and_a_failing_hook_does_not_lose_it(env):
    a = Fake("a", [keep(env.tmp / "one", "busy")])
    a.reaper.module.snapshot_extra = lambda verdicts: {"unreadable_helpers_skipped": len(verdicts) + 16}
    run_live(env, [a], force_run=True)
    assert kept_snapshot(env, "a")["unreadable_helpers_skipped"] == 17

    def broken(verdicts):
        raise RuntimeError("boom")

    a.reaper.module.snapshot_extra = broken
    _, err = run_live(env, [a], force_run=True)
    assert "snapshot_extra failed" in err and "unreadable_helpers_skipped" not in kept_snapshot(env, "a")


def test_a_dry_run_prints_the_reapers_notes_to_stderr_and_keeps_stdout_closed(env):
    a = Fake("a", [keep(env.tmp / "one", "busy")], summary_line="a summary")
    a.reaper.module.dry_run_notes = lambda verdicts: [f"a: note about {len(verdicts)} verdict(s)"]
    out, err = run_live(env, [a], dry_run=True)
    assert "a: note about 1 verdict(s)" in err and "note" not in out and "a summary" in out


def test_a_live_pass_prints_no_dry_run_notes(env):
    a = Fake("a", [keep(env.tmp / "one", "busy")])
    a.reaper.module.dry_run_notes = lambda verdicts: ["a: a note"]
    _, err = run_live(env, [a], force_run=True)
    assert "a note" not in err


def test_a_remove_vetoed_by_another_reaper_appears_in_the_snapshot_as_a_keep(env):
    a, b = Fake("a", [rm(env.item)]), Fake("b", [keep(env.item, "in use")])
    run_live(env, [a, b])
    assert kept_snapshot(env, "a")["kept"] == [
        {"path": str(env.item), "reason": "blocked", "detail": "kept by b", "bytes": 0},
    ]


def test_a_reaper_whose_scan_failed_gets_no_kept_snapshot_while_the_others_do(env):
    broken = Fake("a", [], scan_exc=RuntimeError("boom"))
    other = Fake("b", [keep(env.tmp / "one", "busy")])
    _, err = run_live(env, [broken, other], force_run=True)
    assert "reaper a: scan failed" in err
    assert sorted(p.name for p in (env.tmp / "kept").iterdir()) == ["b.json"]


def test_a_removal_that_returned_false_is_snapshotted_as_failed_with_its_remove_reason(env):
    failing = Fake("a", [rm(env.item, "no holder, idle 80h")], remove_result=False)
    _, err = run_live(env, [failing], force_run=True)
    assert "FAILED to remove" in err
    assert kept_snapshot(env, "a")["kept"] == [{
        "path": str(env.item), "reason": "removal-failed",
        "detail": "removal failed: no holder, idle 80h", "bytes": 0,
    }]


def test_a_removal_that_raised_is_snapshotted_as_failed(env):
    failing = Fake("a", [rm(env.item, "stale")], remove_exc=OSError("busy"))
    run_live(env, [failing], force_run=True)
    [entry] = kept_snapshot(env, "a")["kept"]
    assert (entry["reason"], entry["detail"]) == ("removal-failed", "removal failed: stale")


def test_the_kept_snapshot_is_replaced_by_each_pass_not_appended_to(env):
    a = Fake("a", [keep(env.tmp / "one", "busy")])
    run_live(env, [a], force_run=True)
    a.verdicts = [keep(env.tmp / "two", "busy")]
    run_live(env, [a], force_run=True)
    assert [k["path"] for k in kept_snapshot(env, "a")["kept"]] == [str(env.tmp / "two")]
    assert sorted(p.name for p in (env.tmp / "kept").iterdir()) == ["a.json"]


def test_no_snapshot_is_written_when_the_reaper_is_not_due(env):
    a = Fake("a", [keep(env.tmp / "one", "busy")])
    env.stamp("a", NOW)
    run_live(env, [a])
    assert not (env.tmp / "kept").exists()


def test_a_dry_run_writes_no_kept_snapshot_and_no_removal_row(env):
    a = Fake("a", [rm(env.item), keep(env.tmp / "one", "busy")])
    run_live(env, [a], dry_run=True)
    assert not (env.tmp / "kept").exists() and env.log_records() == [] and not env.stamps.exists()


def test_main_writes_the_kept_snapshot_under_the_config_root(hermetic):
    a = Fake("a", [keep(hermetic / "one", "busy")])
    assert runner.main(["--force-run"], discover=lambda project, warn: [a.reaper]) == 0
    snapshot = json.loads((hermetic / "config" / "reaper" / "kept" / "a.json").read_text(encoding="utf-8"))
    assert snapshot["kept"][0]["path"] == str(hermetic / "one")


# ── mutation catalogue ────────────────────────────────────────────────────

MUTANT_WORKERS = 6  # each worker is one scratch copy and one serial pytest child; bounded, never recursive


def mutant_workers(environ, soft_nproc_limit: int) -> int:
    """Six workers only under ``scripts/cap-run.sh``: in count mode it exports its cap as
    ``CAP_RUN_NPROC`` and the process's own RLIMIT_NPROC must equal it, so a stray or
    inherited variable does not count. In --own-tasks mode the supervisor keeps moving
    the soft limit after that snapshot, so equality stops holding under exactly the
    co-tenant churn the mode exists for; ``CAP_RUN_OWN_TASKS`` marks it instead (an
    inherited value still means a cap-run wraps this process)."""
    capped = (environ.get("CAP_RUN_OWN_TASKS", "").isdigit()
              or environ.get("CAP_RUN_NPROC") == str(soft_nproc_limit))
    return min(MUTANT_WORKERS, os.cpu_count() or 1) if capped else 1


def test_mutant_workers_fan_out_only_under_a_matching_cap():
    assert mutant_workers({}, 5000) == 1
    assert mutant_workers({"CAP_RUN_NPROC": "4000"}, 5000) == 1
    assert mutant_workers({"CAP_RUN_NPROC": "5000"}, 5000) == min(MUTANT_WORKERS, os.cpu_count() or 1)
    assert mutant_workers({"CAP_RUN_OWN_TASKS": "512", "CAP_RUN_NPROC": "4000"}, 5000) \
        == min(MUTANT_WORKERS, os.cpu_count() or 1)
    assert mutant_workers({"CAP_RUN_OWN_TASKS": ""}, 5000) == 1


def test_cap_run_exports_the_cap_it_set(tmp_path):
    import subprocess

    cap_run = Path(__file__).resolve().parents[1] / "cap-run.sh"
    done = subprocess.run(
        ["bash", str(cap_run), "--extra-tasks", "64", "--timeout", "60", "--", sys.executable, "-c",
         "import os, resource; print(os.environ['CAP_RUN_NPROC'], resource.getrlimit(resource.RLIMIT_NPROC)[0])"],
        capture_output=True, text=True, check=True,
    )
    exported, soft = done.stdout.split()
    assert exported == soft


@pytest.mark.skipif(os.environ.get("REAPER_MUTATION_CHILD") == "1", reason="running inside a mutant subprocess")
def test_every_catalogue_mutant_is_killed_and_the_control_is_green():
    import resource
    from concurrent.futures import ThreadPoolExecutor

    import reaper_mutation_control as control

    names = list(control.CATALOGUE)
    workers = mutant_workers(os.environ, resource.getrlimit(resource.RLIMIT_NPROC)[0])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        survivors = dict(zip(names, pool.map(control.run_mutant, names)))
    assert survivors == {name: control.KILLED for name in names}
    assert control.run_control() == 0


# ── explicit-invocation controls (--pressure-threshold, --on-pressure --only/--force-run, --param) ─────

SCRIPTS_DIR = Path(__file__).resolve().parents[1]


def pressured(name, *, free_paths, verdicts=()):
    fake = Fake(name, list(verdicts))
    fake.reaper.module.pressure_paths = lambda ctx: list(free_paths)
    return fake


def discover_of(*fakes):
    return lambda project, warn: [f.reaper for f in fakes]


@pytest.fixture
def armed(hermetic, monkeypatch):
    from agentctl import config as agentctl_config

    config = hermetic / "config.md"
    config.write_text("| Key | Value |\n|---|---|\n| `reaper-pressure-free-percent` | `10` | basis |\n", encoding="utf-8")
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", config)
    monkeypatch.setattr(runner, "free_percent", lambda path: 20.0)
    return hermetic


def pressure_rows(root):
    path = root / "config" / "reaper" / "pressure.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def cooldown_stamp(root, name="a"):
    return root / "home" / ".local" / "state" / "claude-reaper" / f"{name}.pressure.stamp"


def test_the_threshold_flag_makes_a_reaper_due_above_the_config_threshold_and_is_recorded(armed):
    victim = armed / "victim"
    victim.mkdir()
    a = pressured("a", free_paths=[str(armed)], verdicts=[rm(victim)])
    assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    assert a.removed == [] and pressure_rows(armed) == []
    assert runner.main(["--on-pressure", "--pressure-threshold", "30"], discover=discover_of(a)) == 0
    assert a.removed == [str(victim)]
    [row] = pressure_rows(armed)
    assert (row["threshold"], row["threshold_source"]) == (30.0, "flag")


def test_without_the_threshold_flag_the_config_row_rules_and_is_recorded(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    a = pressured("a", free_paths=[str(armed)])
    assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    [row] = pressure_rows(armed)
    assert (row["threshold"], row["threshold_source"]) == (10.0, "config")


@pytest.mark.parametrize("argv", [
    ["--on-pressure", "--pressure-threshold", "0"],
    ["--on-pressure", "--pressure-threshold", "-5"],
    ["--on-pressure", "--pressure-threshold", "100.5"],
    ["--on-pressure", "--pressure-threshold", "nan"],
    ["--on-pressure", "--pressure-threshold", "ten"],
    ["--pressure-threshold", "30"],
    ["--pressure-threshold", "30", "--dry-run"],
    ["--on-pressure", "--upkeep-only"],
    ["--param", "a.k=v"],
    ["--param", "no-dot-or-equals", "--dry-run"],
    ["--param", "a.k=v", "--list"],
    ["--param", "a.k=v", "--upkeep-only"],
    ["--param", "a.k=v", "--holders", "/x"],
])
def test_an_invalid_flag_or_combination_exits_two(armed, argv):
    with pytest.raises(SystemExit) as raised:
        runner.main(argv, discover=discover_of())
    assert raised.value.code == 2


def test_a_threshold_flag_without_on_pressure_exits_two(armed):
    with pytest.raises(SystemExit) as raised:
        runner.main(["--dry-run", "--pressure-threshold", "30"], discover=discover_of())
    assert raised.value.code == 2


def test_a_param_with_list_exits_two(armed):
    with pytest.raises(SystemExit) as raised:
        runner.main(["--list", "--dry-run", "--param", "a.k=v"], discover=discover_of())
    assert raised.value.code == 2


def test_a_param_naming_no_reaper_exits_two(armed):
    assert runner.main(["--dry-run", "--param", "ghost.k=v"], discover=discover_of(Fake("a"))) == 2


def test_a_threshold_of_exactly_one_hundred_is_accepted(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 99.0)
    a = pressured("a", free_paths=[str(armed)])
    assert runner.main(["--on-pressure", "--pressure-threshold", "100"], discover=discover_of(a)) == 0
    assert pressure_rows(armed)[0]["due"] == ["a"]


def test_the_threshold_flag_works_when_the_config_row_is_missing(hermetic, monkeypatch):
    from agentctl import config as agentctl_config

    monkeypatch.setattr(agentctl_config, "CONFIG_MD", hermetic / "absent.md")
    monkeypatch.setattr(runner, "free_percent", lambda path: 5.0)
    a = pressured("a", free_paths=[str(hermetic)])
    assert runner.main(["--on-pressure", "--pressure-threshold", "10"], discover=discover_of(a)) == 0
    assert pressure_rows(hermetic)[0]["threshold_source"] == "flag"


def test_only_under_pressure_runs_only_that_reaper(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    first, second = armed / "first", armed / "second"
    first.mkdir()
    second.mkdir()
    a = pressured("a", free_paths=[str(armed)], verdicts=[rm(first)])
    b = pressured("b", free_paths=[str(armed)], verdicts=[rm(second)])
    assert runner.main(["--on-pressure", "--only", "a"], discover=discover_of(a, b)) == 0
    assert (a.removed, b.removed) == ([str(first)], [])
    assert pressure_rows(armed)[0]["due"] == ["a"]
    assert cooldown_stamp(armed, "a").is_file() and not cooldown_stamp(armed, "b").exists()


def test_force_run_under_pressure_ignores_the_cooldown_and_writes_no_stamp(armed, monkeypatch):
    import time

    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    victim = armed / "victim"
    victim.mkdir()
    a = pressured("a", free_paths=[str(armed)], verdicts=[rm(victim)])
    stamp = cooldown_stamp(armed)
    stamp.parent.mkdir(parents=True)
    written = str(time.time())
    stamp.write_text(written, encoding="utf-8")
    assert runner.main(["--on-pressure"], discover=discover_of(a)) == 0
    assert a.removed == []
    assert runner.main(["--on-pressure", "--force-run"], discover=discover_of(a)) == 0
    assert a.removed == [str(victim)]
    assert stamp.read_text(encoding="utf-8") == written


def test_force_run_under_pressure_leaves_no_stamp_where_there_was_none(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    a = pressured("a", free_paths=[str(armed)])
    assert runner.main(["--on-pressure", "--force-run"], discover=discover_of(a)) == 0
    assert not cooldown_stamp(armed).exists() and len(pressure_rows(armed)) == 1


def capturing(fake):
    seen = []
    inner = fake.reaper.module.scan

    def scan(ctx):
        seen.append(ctx)
        return inner(ctx)

    fake.reaper.module.scan = scan
    return seen


def write_opt_in(root, text):
    path = root / "config" / "reaper" / "opt-in.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_param_is_laid_over_the_opt_in_table_and_reaches_a_dry_run_with_its_source(hermetic):
    write_opt_in(hermetic, 'a = true\n[params.a]\nk1 = "table1"\nk2 = "table2"\n')
    a = Fake("a")
    seen = capturing(a)
    assert runner.main(["--dry-run", "--param", "a.k2=cli2", "--param", "a.k3=cli3"], discover=discover_of(a)) == 0
    [ctx] = seen
    assert ctx.params == {"a": {"k1": "table1", "k2": "cli2", "k3": "cli3"}}
    assert ctx.param_sources == {"a": {"k1": "table", "k2": "cli", "k3": "cli"}}


def test_a_table_value_reaches_a_pass_with_param_source_table(hermetic):
    write_opt_in(hermetic, "[params.a]\nk1 = 7\n")
    a = Fake("a")
    seen = capturing(a)
    assert runner.main(["--dry-run", "--param", "b.x=1"], discover=discover_of(a, Fake("b"))) == 0
    assert seen[0].params["a"] == {"k1": 7} and seen[0].param_sources["a"] == {"k1": "table"}


def test_a_param_is_passed_to_a_pressure_pass(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    a = pressured("a", free_paths=[str(armed)])
    seen = capturing(a)
    assert runner.main(["--on-pressure", "--param", "a.limit=5"], discover=discover_of(a)) == 0
    assert seen[0].params == {"a": {"limit": "5"}} and seen[0].param_sources == {"a": {"limit": "cli"}}


def test_a_param_value_is_written_to_no_file_by_a_live_pass(hermetic, monkeypatch):
    victim = hermetic / "victim"
    victim.mkdir()
    a = Fake("a", [rm(victim)])
    monkeypatch.setattr(runner, "stamp_dir", lambda: hermetic / "stamps")
    value = "unique-secret-5c1f9e"
    assert runner.main(["--force-run", "--param", f"a.k={value}"], discover=discover_of(a)) == 0
    assert a.removed == [str(victim)]
    holders = [
        path for path in hermetic.rglob("*")
        if path.is_file() and value.encode() in path.read_bytes()
    ]
    assert holders == []


def test_an_opt_in_file_with_a_params_table_keeps_every_boolean_reaper_enabled(tmp_path):
    from reaper import opt_in

    path = tmp_path / "opt-in.toml"
    path.write_text('alpha = true\nbeta = false\n[params.alpha]\nk = "v"\n[params.gamma]\nn = 1\n', encoding="utf-8")
    assert opt_in.enabled("alpha", path) is True
    assert opt_in.enabled("beta", path) is False
    assert opt_in.enabled("gamma", path) is False
    assert opt_in.params("alpha", path) == {"k": "v"}
    assert opt_in.params("gamma", path) == {"n": 1}
    assert opt_in.params("beta", path) == {}


def test_a_missing_or_malformed_opt_in_file_has_no_params(tmp_path, capsys):
    from reaper import opt_in

    assert opt_in.params("a", tmp_path / "absent.toml") == {}
    assert capsys.readouterr().err == ""
    bad = tmp_path / "bad.toml"
    bad.write_text("a = [", encoding="utf-8")
    assert opt_in.params("a", bad) == {}
    assert "unreadable" in capsys.readouterr().err
    assert opt_in.enabled("a", bad) is False


def test_a_pressure_row_names_its_trigger(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    a = pressured("a", free_paths=[str(armed)])
    runner.main(["--on-pressure"], discover=discover_of(a))
    assert pressure_rows(armed)[0]["trigger"] == "pressure" and "invocation_id" not in pressure_rows(armed)[0]


def test_a_pressure_row_under_a_reaper_unit_carries_the_trigger_unit_and_the_invocation_id(armed, monkeypatch):
    monkeypatch.setattr(runner, "free_percent", lambda path: 4.0)
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pressure.service")
    monkeypatch.setenv("INVOCATION_ID", "cafe0123")
    a = pressured("a", free_paths=[str(armed)])
    runner.main(["--on-pressure"], discover=discover_of(a))
    row = pressure_rows(armed)[0]
    assert (row["trigger"], row["invocation_id"]) == ("unit", "cafe0123")


# The command lines the automatic triggers start: the timer's units, the branch-backup hook and the
# SessionStart hook. None may carry an explicit-invocation control.
HOOK_REAPER_FLAG_CALLS = r'_spawn_detached\("[^"]+"((?:,\s*"[^"]*")*)\)'


def trigger_offences(flags):
    offences = []
    for banned in ("--pressure-threshold", "--param"):
        if banned in flags:
            offences.append(f"{banned} on a trigger line")
    if "--force-run" in flags and flags != ["--force-run"]:
        offences.append("--force-run beyond the pass unit's plain line")
    if "--only" in flags and "--on-pressure" in flags:
        offences.append("--only with --on-pressure")
    return offences


def unit_exec_lines():
    import subprocess

    stubs = Path(__file__).resolve().parent / "fixtures" / "reaper_timer_stubs"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for sub in ("home", "tmp", "agent"):
            (root / sub).mkdir()
        env = {
            "PATH": os.environ["PATH"], "HOME": str(root / "home"), "SYSTEMD_USER_DIR": str(root / "units"),
            "SYSTEMCTL_BIN": str(stubs / "systemctl"), "LOGINCTL_BIN": str(stubs / "loginctl"),
            "JOURNALCTL_BIN": str(stubs / "journalctl"), "REAPER_TIMER_STUB_STATE": str(root / "stub"),
            "REAPER_TIMER_STUB_MANAGER": "none", "CLAUDE_CONFIG_DIR": str(root / "agent"), "TMPDIR": str(root / "tmp"),
        }
        done = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "install-reaper-timer.sh"), "--dry-run", "--tmpdir", str(root / "tmp")],
            env=env, capture_output=True, text=True, timeout=60, check=True,
        )
    return [line for line in done.stdout.splitlines() if line.startswith("ExecStart=")]


def hook_flag_lists():
    backup = (SCRIPTS_DIR / "hook-branch-backup.py").read_text(encoding="utf-8")
    calls = [re.findall(r'"([^"]*)"', args) for args in re.findall(HOOK_REAPER_FLAG_CALLS, backup)]
    installer = (SCRIPTS_DIR / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    session_start = [
        line for line in installer.splitlines() if re.search(r'"hook-reaper\.py"\s*,', line) and not line.lstrip().startswith("#")
    ]
    return calls, session_start


def test_no_trigger_command_line_carries_an_explicit_invocation_control():
    lines = unit_exec_lines()
    assert len(lines) == 3
    seen = []
    for line in lines:
        flags = [token for token in line.split() if token.startswith("--")]
        seen.append(flags)
        assert trigger_offences(flags) == [], line
    assert ["--on-pressure"] in seen and ["--force-run"] in seen and ["--only"] in seen
    calls, session_start = hook_flag_lists()
    assert ["--upkeep-only", "--no-wait"] in calls and ["--on-pressure"] in calls
    for flags in calls:
        assert trigger_offences(flags) == [], flags
    assert len(session_start) == 1 and "--" not in session_start[0]


def test_the_trigger_guard_flags_each_forbidden_shape():
    assert trigger_offences(["--on-pressure", "--pressure-threshold"]) != []
    assert trigger_offences(["--force-run", "--param"]) != []
    assert trigger_offences(["--force-run", "--on-pressure"]) != []
    assert trigger_offences(["--on-pressure", "--only"]) != []
    assert trigger_offences(["--only"]) == [] and trigger_offences(["--force-run"]) == []


# ── unit-pass observation (read by install-reaper-timer.sh --check) ──────────

def _unit_pass_files(hermetic):
    return sorted((hermetic / "config" / "reaper" / "unit-pass").glob("*.json"))


def _force_run_main(hermetic, monkeypatch, reaper):
    monkeypatch.setattr(runner, "stamp_dir", lambda: hermetic / "stamps")
    return runner.main(["--force-run"], discover=lambda project, warn: [reaper])


def test_a_unit_pass_records_what_it_saw(hermetic, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "feed0123")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    monkeypatch.setenv("TMPDIR", str(hermetic))
    monkeypatch.setattr("tempfile.tempdir", None)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/s")
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    assert _force_run_main(hermetic, monkeypatch, Fake("a", []).reaper) == 0
    [path] = _unit_pass_files(hermetic)
    assert path.name == "feed0123.json"
    assert json.loads(path.read_text()) == {
        "invocation_id": "feed0123", "unit": "agent-reaper-pass.service", "tmpdir": str(hermetic), "agent_home": str(hermetic / "config"),
        "credentials_present": {"SSH_AUTH_SOCK": True, "SSH_AGENT_PID": False, "GIT_ASKPASS": False, "SSH_ASKPASS": False},
        "reapers_run": 1, "errors": 0,
    }


def test_an_inherited_invocation_id_without_the_unit_marker_writes_no_observation(hermetic, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "feed0123")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/s")
    assert _force_run_main(hermetic, monkeypatch, Fake("a", []).reaper) == 0
    assert _unit_pass_files(hermetic) == []


def test_no_observation_is_written_outside_systemd(hermetic, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert _force_run_main(hermetic, monkeypatch, Fake("a", []).reaper) == 0
    assert _unit_pass_files(hermetic) == []


def test_a_dry_run_under_systemd_writes_no_observation(hermetic, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "feed0123")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    monkeypatch.setattr(runner, "stamp_dir", lambda: hermetic / "stamps")
    assert runner.main(["--dry-run"], discover=lambda project, warn: [Fake("a", []).reaper]) == 0
    assert _unit_pass_files(hermetic) == []


def test_reapers_run_counts_the_scans_that_completed_and_errors_the_failed_ones(hermetic, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "feed0123")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    monkeypatch.setattr(runner, "stamp_dir", lambda: hermetic / "stamps")
    good, bad = Fake("good", []), Fake("bad", scan_exc=RuntimeError("x"))
    runner.main(["--force-run"], discover=lambda project, warn: [good.reaper, bad.reaper])
    seen = json.loads(_unit_pass_files(hermetic)[0].read_text())
    assert (seen["reapers_run"], seen["errors"]) == (1, 1)


def test_a_runner_error_under_systemd_still_leaves_an_observation_with_an_error(hermetic, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "feed0123")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")

    def explode(project, warn):
        raise RuntimeError("boom")

    assert runner.main(["--force-run"], discover=explode) == 1
    seen = json.loads(_unit_pass_files(hermetic)[0].read_text())
    assert (seen["reapers_run"], seen["errors"]) == (0, 1)


def test_only_the_newest_fifty_observations_are_kept(hermetic, monkeypatch):
    directory = hermetic / "config" / "reaper" / "unit-pass"
    directory.mkdir(parents=True)
    for index in range(60):
        old = directory / f"old{index:02d}.json"
        old.write_text("{}")
        os.utime(old, (1_000_000 + index, 1_000_000 + index))
    monkeypatch.setenv("INVOCATION_ID", "feed0123")
    monkeypatch.setenv("AGENT_REAPER_UNIT", "agent-reaper-pass.service")
    _force_run_main(hermetic, monkeypatch, Fake("a", []).reaper)
    names = {path.name for path in _unit_pass_files(hermetic)}
    assert len(names) == runner.UNIT_PASS_KEEP and "feed0123.json" in names and "old00.json" not in names
