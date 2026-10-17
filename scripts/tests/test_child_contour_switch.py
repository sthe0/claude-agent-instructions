"""The child-contour kill switch: linking a spawned specialist into its own engine
contour is decided by `agentctl.config.child_contour_linking_enabled`, whose default is
config.md's `child-contour-linking` row (shipped `off`) and whose override is
AGENTCTL_CHILD_LINKING (`1` on, `0` off).

Each test sets or deletes the variable itself, so none depends on conftest's suite-wide
pin. The resolver is reached through `getattr` inside the test bodies, so on a tree
without it the resolver tests fail on a `BEHAVIOUR:` assertion, not at collection. The
three `*_linked_*` / `*_either_way` spawn tests hold on a tree without the switch too:
they pin that switching linking on, or naming no stage, behaves as before."""
from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl import config as agentctl_config

KEY = "child-contour-linking"
VAR = "AGENTCTL_CHILD_LINKING"
HANDOFF_ENV = "AGENTCTL_NORM_HANDOFF"
OWN_SESSION_ENV = "AGENTCTL_OWN_SESSION"
SPAWN_SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_module():
    spec = importlib.util.spec_from_file_location("spawn_specialist_child_switch", SPAWN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPAWN = _load_spawn_module()


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    monkeypatch.delenv("AGENTCTL_PARENT_DIFFICULTY", raising=False)
    monkeypatch.delenv(HANDOFF_ENV, raising=False)
    monkeypatch.delenv(OWN_SESSION_ENV, raising=False)
    norms = tmp_path / "norms"
    norms.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(norms))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _resolver():
    resolver = getattr(agentctl_config, "child_contour_linking_enabled", None)
    assert callable(resolver), \
        "BEHAVIOUR: agentctl.config defines no child_contour_linking_enabled resolver"
    return resolver


def ns(**kw):
    return Namespace(**kw)


def _executing(store, sid, venued_fixtures_dir):
    cli.cmd_start(ns(session=sid, task="child-switch", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / "plan_two_stage.toml")), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


class _Launched(Exception):
    def __init__(self, cmd, env):
        super().__init__("launched")
        self.cmd = list(cmd)
        self.env = env


def _spawn(monkeypatch, tmp_path, venued_fixtures_dir, session, *, stage_index=None):
    """Drive the spawner up to the child launch; returns `(cmd, env)` the child would get."""
    monkeypatch.setattr(SPAWN.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(cmd, dict(env or {}))

    monkeypatch.setattr(SPAWN.proc_tree, "launch_supervised", launch)
    argv = [
        "--kind", "thinker", "--plan", str(venued_fixtures_dir / "plan_two_stage.toml"),
        "--done-criterion", "a verdict", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
        "--session", session, "--state-root", str(tmp_path / "state"),
        "--workdir", str(tmp_path),
    ]
    if stage_index is not None:
        argv += ["--stage-index", str(stage_index)]
    try:
        rc = SPAWN.main(argv)
    except _Launched as launched:
        return launched.cmd, launched.env
    pytest.fail(f"setup: the spawn did not reach the launch (rc={rc})")


def _handoff_files(tmp_path):
    directory = tmp_path / "state" / "handoffs"
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _is_linked(cmd, env):
    return HANDOFF_ENV in env or OWN_SESSION_ENV in env or "--session-id" in cmd


def _assert_linked(cmd, env, tmp_path):
    assert env.get(HANDOFF_ENV), "BEHAVIOUR: a linked spawn exports no AGENTCTL_NORM_HANDOFF"
    own = env.get(OWN_SESSION_ENV)
    assert own, "BEHAVIOUR: a linked spawn exports no AGENTCTL_OWN_SESSION"
    assert "--session-id" in cmd and cmd[cmd.index("--session-id") + 1] == own, \
        "BEHAVIOUR: a linked spawn does not pin the child's --session-id to its own session"
    assert _handoff_files(tmp_path), "BEHAVIOUR: a linked spawn writes no handoff record"


def _assert_unlinked(cmd, env, tmp_path):
    assert HANDOFF_ENV not in env, "BEHAVIOUR: an unlinked spawn exports AGENTCTL_NORM_HANDOFF"
    assert OWN_SESSION_ENV not in env, "BEHAVIOUR: an unlinked spawn exports AGENTCTL_OWN_SESSION"
    assert "--session-id" not in cmd, "BEHAVIOUR: an unlinked spawn passes --session-id"
    assert not _handoff_files(tmp_path), "BEHAVIOUR: an unlinked spawn writes a handoff record"


def _lines_naming(text, *needles):
    return [line for line in text.splitlines() if all(n in line for n in needles)]


# --- the resolver -------------------------------------------------------------

def test_resolver_variable_overrides_config_both_ways():
    resolver = _resolver()
    assert resolver(env={VAR: "1"}, constants={KEY: "off"}) is True, \
        "BEHAVIOUR: AGENTCTL_CHILD_LINKING=1 does not turn linking on over an `off` row"
    assert resolver(env={VAR: "1"}, constants={}) is True, \
        "BEHAVIOUR: AGENTCTL_CHILD_LINKING=1 does not turn linking on without a row"
    assert resolver(env={VAR: "0"}, constants={KEY: "on"}) is False, \
        "BEHAVIOUR: AGENTCTL_CHILD_LINKING=0 does not turn linking off over an `on` row"


def test_resolver_config_decides_without_variable():
    resolver = _resolver()
    assert resolver(env={}, constants={KEY: "on"}) is True, \
        "BEHAVIOUR: an `on` row does not turn linking on when the variable is unset"
    assert resolver(env={}, constants={KEY: "off"}) is False, \
        "BEHAVIOUR: an `off` row does not keep linking off when the variable is unset"
    assert resolver(env={VAR: "yes"}, constants={KEY: "on"}) is True, \
        "BEHAVIOUR: a variable other than 1/0 does not fall through to an `on` row"
    assert resolver(env={VAR: ""}, constants={KEY: "off"}) is False, \
        "BEHAVIOUR: a variable other than 1/0 does not fall through to an `off` row"


def test_resolver_missing_or_unparsable_row_is_off():
    resolver = _resolver()
    for constants in ({}, {KEY: "maybe"}, {KEY: "ON"}, {KEY: "on "}, {KEY: ""}):
        assert resolver(env={}, constants=constants) is False, \
            f"BEHAVIOUR: the row {constants!r} turns linking on; only exactly `on` may"


def test_shipped_config_row_is_on(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    shipped = agentctl_config.parse_config_md(agentctl_config.CONFIG_MD)
    assert shipped.get(KEY) == "on", \
        f"BEHAVIOUR: config.md does not ship the `{KEY}` row with value `on` (got {shipped.get(KEY)!r})"
    assert _resolver()() is True, \
        "BEHAVIOUR: the resolver with default env and constants does not resolve the shipped config to on"


def test_resolver_never_raises_on_unreadable_config(tmp_path, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    resolver = _resolver()
    missing = tmp_path / "missing" / "config.md"
    directory = tmp_path / "directory"
    directory.mkdir()
    undecodable = tmp_path / "undecodable.md"
    undecodable.write_bytes(b"| `child-contour-linking` | `on` |\n\xff\xfe\xfa")
    for name, target in (("missing", missing), ("directory", directory), ("undecodable", undecodable)):
        monkeypatch.setattr(agentctl_config, "CONFIG_MD", target)
        try:
            result = resolver()
        except Exception as exc:  # noqa: BLE001 - the property under test is "never raises"
            pytest.fail(f"BEHAVIOUR: the resolver raised on a {name} config: {exc!r}")
        assert result is False, f"BEHAVIOUR: a {name} config resolves linking on"


# --- the spawn seam -----------------------------------------------------------

def test_spawn_is_unlinked_by_default(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, "0")
    _executing(store, "off1", venued_fixtures_dir)
    cmd, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "off1", stage_index=1)
    _assert_unlinked(cmd, env, tmp_path)


def test_unlinked_spawn_prints_one_notice_naming_key_and_variable(
        store, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(VAR, "0")
    _executing(store, "off2", venued_fixtures_dir)
    capsys.readouterr()
    _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "off2", stage_index=1)
    err = capsys.readouterr().err
    assert len(_lines_naming(err, KEY, VAR)) == 1, \
        f"BEHAVIOUR: an unlinked spawn does not print exactly one stderr line naming {KEY} and {VAR}"
    assert len(_lines_naming(err, KEY)) == 1 and len(_lines_naming(err, VAR)) == 1, \
        "BEHAVIOUR: the switch notice is repeated on stderr"


def test_spawn_is_linked_with_variable_on(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, "1")
    _executing(store, "on1", venued_fixtures_dir)
    cmd, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "on1", stage_index=1)
    _assert_linked(cmd, env, tmp_path)


def test_spawn_follows_config_row_on_without_variable(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    on_row = f"| `{KEY}` | `on` | test copy |"
    lines = Path(SPAWN.CONFIG_MD).read_text(encoding="utf-8").splitlines()
    replaced = [on_row if line.startswith(f"| `{KEY}` |") else line for line in lines]
    if replaced == lines:
        replaced.append(on_row)
    copy = tmp_path / "config.md"
    copy.write_text("\n".join(replaced) + "\n", encoding="utf-8")
    monkeypatch.setattr(SPAWN, "CONFIG_MD", copy)
    assert SPAWN.parse_config_md().get(KEY) == "on", "setup: the config copy carries no `on` row"
    _executing(store, "on2", venued_fixtures_dir)
    cmd, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "on2", stage_index=1)
    _assert_linked(cmd, env, tmp_path)


def test_spawn_without_stage_args_is_unlinked_either_way(
        store, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    for value in ("1", "0"):
        monkeypatch.setenv(VAR, value)
        sid = f"nostage-{value}"
        _executing(store, sid, venued_fixtures_dir)
        capsys.readouterr()
        cmd, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, sid)
        _assert_unlinked(cmd, env, tmp_path)
        assert not _lines_naming(capsys.readouterr().err, KEY), \
            f"BEHAVIOUR: a spawn naming no stage prints the {KEY} notice (variable {value})"
