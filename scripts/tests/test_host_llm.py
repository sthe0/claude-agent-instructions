"""lib.host_llm: the one seam that assembles a bare `<binary> -p ... <prompt>`
argv per coordination host and refuses to build one that crosses hosts
(Host-isolated spawn plan, step 4).

Every test monkeypatches host_llm.shutil.which / host_llm.os.environ directly
rather than depending on whether `claude`/`agent`/`cursor-agent` are actually
installed, or on ~/.cursor_api_key's real contents.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from lib import host_llm
from lib.runtime_models import CURSOR_COMPLEXITY_MODEL, HOST_CLAUDE, HOST_CURSOR


def _load_probe():
    """verify-judge-isolation.py is hyphenated and therefore unimportable by
    name; load it by path so its pure comparisons can be unit-tested without
    spending the live quota its main() spends."""
    path = Path(__file__).resolve().parent.parent / "verify-judge-isolation.py"
    spec = importlib.util.spec_from_file_location("verify_judge_isolation", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()


# --- binary_for ------------------------------------------------------------------

def test_binary_for_claude_is_the_literal_name_regardless_of_path(monkeypatch):
    # No shutil.which call at all for HOST_CLAUDE (see the docstring's
    # hermeticity rationale) — pin which() to prove it is never consulted.
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: (_ for _ in ()).throw(
        AssertionError(f"shutil.which({name!r}) should not be called for HOST_CLAUDE")
    ))
    assert host_llm.binary_for(HOST_CLAUDE) == "claude"


def test_binary_for_cursor_prefers_agent_over_cursor_agent(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert host_llm.binary_for(HOST_CURSOR) == "/usr/bin/agent"


def test_binary_for_cursor_falls_back_to_cursor_agent(monkeypatch):
    monkeypatch.setattr(
        host_llm.shutil, "which", lambda name: "/usr/bin/cursor-agent" if name == "cursor-agent" else None
    )
    assert host_llm.binary_for(HOST_CURSOR) == "/usr/bin/cursor-agent"


def test_binary_for_cursor_falls_back_to_agent_literal_when_neither_installed(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: None)
    assert host_llm.binary_for(HOST_CURSOR) == "agent"


def test_binary_for_unknown_host_raises():
    with pytest.raises(ValueError):
        host_llm.binary_for("windows")


# --- assert_same_family: the cross-host refusal ------------------------------------

def test_assert_same_family_accepts_matching_pairs():
    host_llm.assert_same_family("claude", HOST_CLAUDE)
    host_llm.assert_same_family("agent", HOST_CURSOR)
    host_llm.assert_same_family("/usr/local/bin/cursor-agent", HOST_CURSOR)


def test_assert_same_family_refuses_claude_binary_under_cursor_host():
    with pytest.raises(host_llm.CrossHostError):
        host_llm.assert_same_family("claude", HOST_CURSOR)


def test_assert_same_family_refuses_agent_binary_under_claude_host():
    with pytest.raises(host_llm.CrossHostError):
        host_llm.assert_same_family("agent", HOST_CLAUDE)


def test_assert_same_family_unknown_host_raises_value_error():
    with pytest.raises(ValueError):
        host_llm.assert_same_family("claude", "windows")


# --- cursor_api_key_present ---------------------------------------------------------

def test_cursor_api_key_present_true_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CURSOR_API_KEY", "sk-live-123")
    assert host_llm.cursor_api_key_present(tmp_path / "nonexistent") is True


def test_cursor_api_key_present_true_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text("sk-file-456\n", encoding="utf-8")
    assert host_llm.cursor_api_key_present(key_file) is True


def test_cursor_api_key_present_false_when_missing_and_no_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert host_llm.cursor_api_key_present(tmp_path / "nonexistent") is False


def test_cursor_api_key_present_false_when_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text("   \n", encoding="utf-8")
    assert host_llm.cursor_api_key_present(key_file) is False


# --- preflight -----------------------------------------------------------------------

def test_preflight_claude_ok_when_binary_present(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    ok, reason = host_llm.preflight(HOST_CLAUDE)
    assert ok is True
    assert reason == ""


def test_preflight_claude_fails_when_binary_absent(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: None)
    ok, reason = host_llm.preflight(HOST_CLAUDE)
    assert ok is False
    assert "claude" in reason


def test_preflight_cursor_fails_when_no_binary(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: None)
    ok, reason = host_llm.preflight(HOST_CURSOR)
    assert ok is False
    assert "PATH" in reason


def test_preflight_cursor_fails_when_binary_present_but_no_key(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    # cursor_api_key_present's own default arg is bound at def time, so
    # patching DEFAULT_CURSOR_API_KEY_FILE would not reach preflight's
    # no-arg call — patch the function itself instead.
    monkeypatch.setattr(host_llm, "cursor_api_key_present", lambda *a, **k: False)
    ok, reason = host_llm.preflight(HOST_CURSOR)
    assert ok is False
    assert "CURSOR_API_KEY" in reason


def test_preflight_cursor_ok_when_binary_and_key_present(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    monkeypatch.setenv("CURSOR_API_KEY", "sk-live-123")
    ok, reason = host_llm.preflight(HOST_CURSOR)
    assert ok is True
    assert reason == ""


def test_preflight_unknown_host_raises():
    with pytest.raises(ValueError):
        host_llm.preflight("windows")


# --- build_prompt_argv ---------------------------------------------------------------

def test_build_prompt_argv_claude_shape(monkeypatch):
    argv = host_llm.build_prompt_argv(HOST_CLAUDE, "sonnet", "do the thing")
    assert argv == ["claude", "-p", "--model", "sonnet", "do the thing"]


def test_build_prompt_argv_cursor_shape(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    argv = host_llm.build_prompt_argv(HOST_CURSOR, "auto", "do the thing")
    assert argv[0] == "/usr/bin/agent"
    assert argv[-1] == "do the thing"  # prompt is always last, both hosts
    assert "--trust" in argv and "--force" in argv and "--approve-mcps" in argv
    assert argv[argv.index("--model") + 1] == "auto"
    assert "--workspace" not in argv


def test_build_prompt_argv_cursor_includes_workspace_when_given(monkeypatch, tmp_path):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    argv = host_llm.build_prompt_argv(HOST_CURSOR, "auto", "prompt", workspace=tmp_path)
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)
    assert argv[-1] == "prompt"  # workspace inserted before the trailing prompt


def test_build_prompt_argv_cursor_treats_an_absent_model_as_auto(monkeypatch):
    """The r5 enumeration's one suspected code change, pinned at its REFUTED
    disposition. The suspicion was that model=None on HOST_CURSOR would, under
    isolation, take the sandbox root's default model. It cannot: `agent` reads
    neither settings.json nor CLAUDE_CONFIG_DIR, and every tier of
    CURSOR_COMPLEXITY_MODEL is None precisely because omitting --model is how
    this repo asks Cursor for Auto. Refusing None here would instead have
    disarmed every judge on a cursor host silently, the advisor being fail-open —
    the exact defect class this stage exists to remove."""
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    assert all(tier is None for tier in CURSOR_COMPLEXITY_MODEL.values()), (
        "if a tier ever names a real model, the Auto contract below is no longer "
        "what the judges run under and this disposition needs re-deciding"
    )

    argv = host_llm.build_prompt_argv(HOST_CURSOR, None, "prompt")

    assert "--model" not in argv
    assert argv[-1] == "prompt"


def test_build_prompt_argv_unknown_host_raises():
    with pytest.raises(ValueError):
        host_llm.build_prompt_argv("windows", "model", "prompt")


# --- isolated_run_kwargs -------------------------------------------------------------

def _ambient(monkeypatch, tmp_path, *, token=None, raw=None):
    """Point CLAUDE_CONFIG_DIR at a throwaway ambient root and clear every auth
    variable, so a test says which credential the child had rather than
    inheriting whichever one the developer's machine happens to carry."""
    root = tmp_path / "ambient"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    for var in (host_llm._OAUTH_TOKEN_ENV_VAR,) + host_llm._OTHER_AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    if token is not None:
        raw = json.dumps({"claudeAiOauth": {"accessToken": token}})
    if raw is not None:
        (root / host_llm._CREDENTIALS_FILENAME).write_text(raw, encoding="utf-8")
    return root


def _is_slot_name(name: str) -> bool:
    """The `<pid>-<tid>` shape isolated_run_kwargs() builds, spelled the same way
    _prune_dead_slots decides it."""
    pid, sep, tid = name.partition("-")
    return bool(sep) and pid.isdigit() and tid.isdigit()


def _sandbox(monkeypatch, tmp_path):
    """Redirect _SANDBOX_ROOT at a throwaway path.

    Always by monkeypatching the module attribute — never by writing the literal
    the live root resolves to, which is TMPDIR-dependent and moves with the
    caller's environment.
    """
    root = tmp_path / "sandbox"
    monkeypatch.setattr(host_llm, "_SANDBOX_ROOT", root)
    return root



def test_isolated_run_kwargs_preserves_ambient_env_and_overrides_config_dir(monkeypatch, tmp_path):
    """env is COPIED and preserved, never replaced — only CLAUDE_CONFIG_DIR and
    the judge-child/auth variables are ours to set."""
    _ambient(monkeypatch, tmp_path, token="tok-abc")
    monkeypatch.setenv("HOST_LLM_ISOLATION_SENTINEL", "still-here")
    kwargs = host_llm.isolated_run_kwargs()
    assert kwargs["env"]["HOST_LLM_ISOLATION_SENTINEL"] == "still-here"
    assert "claude-judge-sandbox" in kwargs["env"]["CLAUDE_CONFIG_DIR"]


def test_isolated_run_kwargs_sets_judge_child_marker():
    """A hook that runs inside this env (even though CLAUDE_CONFIG_DIR isolation
    already removes its own recursion trigger) must be able to tell it is a
    sandboxed judge child and refuse to do any work — the second, independent
    line of defense the marker exists for."""
    kwargs = host_llm.isolated_run_kwargs()
    assert kwargs["env"][host_llm.JUDGE_CHILD_ENV_VAR] == "1"


def test_isolated_run_kwargs_cwd_and_config_dir_are_created_and_empty():
    kwargs = host_llm.isolated_run_kwargs()
    cwd = Path(kwargs["cwd"])
    config_dir = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
    assert cwd.is_dir()
    assert config_dir.is_dir()
    assert not (cwd / "CLAUDE.md").exists()
    assert not (config_dir / "settings.json").exists()


def test_isolated_run_kwargs_reuses_one_slot_per_thread():
    """Repeated calls from one thread must not leave a directory behind each time."""
    first = host_llm.isolated_run_kwargs()
    second = host_llm.isolated_run_kwargs()
    assert first["cwd"] == second["cwd"]
    assert first["env"]["CLAUDE_CONFIG_DIR"] == second["env"]["CLAUDE_CONFIG_DIR"]


def test_isolated_run_kwargs_gives_concurrent_threads_distinct_slots():
    """`claude` writes live state into CLAUDE_CONFIG_DIR, and
    measure-marker-extractor-latency.py drives these calls through a
    ThreadPoolExecutor — so concurrent callers must not share one config root.

    The barrier is load-bearing, not ceremony: the slot key is pid+tid, and
    CPython reuses a tid once its thread exits, so without it an early thread
    can finish before a later one starts and legitimately hand back the same
    slot. That serial reuse is the intended behaviour (one directory per
    caller, not one per call) — what must never happen is two threads holding
    the same slot AT THE SAME TIME, which is what this pins.
    """
    seen: list[dict] = []
    lock = threading.Lock()
    all_inside = threading.Barrier(4, timeout=10)

    def collect():
        kwargs = host_llm.isolated_run_kwargs()
        with lock:
            seen.append(kwargs)
        all_inside.wait()

    threads = [threading.Thread(target=collect) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    config_dirs = {k["env"]["CLAUDE_CONFIG_DIR"] for k in seen}
    cwds = {k["cwd"] for k in seen}
    assert len(config_dirs) == 4
    assert len(cwds) == 4


def test_isolated_run_kwargs_prunes_slots_of_exited_processes(monkeypatch):
    root = host_llm._SANDBOX_ROOT
    root.mkdir(parents=True, exist_ok=True)
    dead = root / "2147483646-1"  # above /proc/sys/kernel/pid_max on Linux
    (dead / "home").mkdir(parents=True, exist_ok=True)
    live = root / f"{os.getpid()}-999999"
    (live / "home").mkdir(parents=True, exist_ok=True)

    host_llm.isolated_run_kwargs()

    assert not dead.exists()
    assert live.exists()


# --- sandbox root hardening ----------------------------------------------------------
#
# The root sits at a predictable path under a world-writable parent, so it can be
# pre-planted. What we own is remediated; what we do not is refused.

def test_harden_root_creates_it_private(monkeypatch, tmp_path):
    root = _sandbox(monkeypatch, tmp_path)
    host_llm._harden_root()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_harden_root_remediates_an_over_permissive_root_we_own(monkeypatch, tmp_path):
    """The live root exists at 0775, created by this module's own earlier
    revision under the fleet's 0002 umask. A bare "refuse unless 0700" would
    refuse on the first judge call after shipping and disarm every judge on the
    machine, so over-permissive-but-ours is tightened, not rejected."""
    root = _sandbox(monkeypatch, tmp_path)
    root.mkdir()
    root.chmod(0o775)
    host_llm._harden_root()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_harden_root_refuses_a_root_owned_by_another_uid(monkeypatch, tmp_path):
    root = _sandbox(monkeypatch, tmp_path)
    root.mkdir(mode=0o700)
    # chown needs privileges we do not have, so the OTHER side of the comparison
    # is moved instead — the predicate under test is "root uid != our uid".
    monkeypatch.setattr(host_llm.os, "getuid", lambda: 999999)
    with pytest.raises(host_llm.SandboxRootUnsafe):
        host_llm._harden_root()


def test_harden_root_refuses_a_symlinked_root_and_leaves_the_target_alone(monkeypatch, tmp_path):
    """The dangerous plant is a symlink to a directory that would itself pass
    every ownership check (~/.ssh being the obvious target): a naive stat sees a
    uid-owned 0700 directory and approves, after which the prune's rmtree runs
    inside the attacker's chosen tree."""
    target = tmp_path / "precious"
    target.mkdir(mode=0o700)
    (target / "id_rsa").write_text("secret", encoding="utf-8")
    root = _sandbox(monkeypatch, tmp_path)
    root.symlink_to(target)

    with pytest.raises(host_llm.SandboxRootUnsafe):
        host_llm._harden_root()
    assert (target / "id_rsa").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_isolated_run_kwargs_refuses_a_hijacked_root(monkeypatch, tmp_path):
    """A credential problem degrades; a root somebody else controls does not."""
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    root = _sandbox(monkeypatch, tmp_path)
    root.symlink_to(target)
    with pytest.raises(host_llm.SandboxRootUnsafe):
        host_llm.isolated_run_kwargs()


# --- root residue ---------------------------------------------------------------------

def test_prune_sweeps_the_root_level_home_and_cwd_residue(monkeypatch, tmp_path):
    """`home/` and `cwd/` sit at the root beside the slots, left by an obsolete
    shape in which the root itself was the slot. _prune_dead_slots keyed liveness
    off a leading pid and skipped them forever, so `home/` went on accumulating
    session state indefinitely."""
    root = _sandbox(monkeypatch, tmp_path)
    root.mkdir(mode=0o700)
    for name in host_llm._ROOT_RESIDUE_NAMES:
        (root / name / "nested").mkdir(parents=True)

    unaccounted = host_llm._prune_dead_slots()

    assert unaccounted == []
    for name in host_llm._ROOT_RESIDUE_NAMES:
        assert not (root / name).exists()


def test_prune_reports_a_non_conforming_entry_instead_of_skipping_it(monkeypatch, tmp_path):
    """The old prune `continue`d past anything that was not a slot, which is how
    the residue became permanent. Unknown entries are now accounted for."""
    root = _sandbox(monkeypatch, tmp_path)
    root.mkdir(mode=0o700)
    (root / "something-nobody-planned").mkdir()

    unaccounted = host_llm._prune_dead_slots()

    assert unaccounted == ["something-nobody-planned"]
    assert (root / "something-nobody-planned").exists()  # reported, never deleted


def test_prune_does_not_follow_symlinks_out_of_the_root(monkeypatch, tmp_path):
    """rmtree does not walk a symlinked CHILD, but handed a symlinked slot itself
    it raises — which ignore_errors would swallow into a permanent leak."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep-me").write_text("x", encoding="utf-8")

    root = _sandbox(monkeypatch, tmp_path)
    root.mkdir(mode=0o700)
    dead_link = root / "2147483646-1"  # above /proc/sys/kernel/pid_max on Linux
    dead_link.symlink_to(outside)
    dead_dir = root / "2147483646-2"
    dead_dir.mkdir()
    (dead_dir / "escape").symlink_to(outside)

    host_llm._prune_dead_slots()

    assert not dead_link.exists() and not dead_link.is_symlink()
    assert not dead_dir.exists()
    assert (outside / "keep-me").exists()


def test_isolated_run_kwargs_adds_no_top_level_entry_outside_the_slot_shape(monkeypatch, tmp_path):
    """The standing guard behind the residue sweep. The sweep deletes two literal
    names, which is only safe while no live code path builds them at the root's
    top level — so a future path that reintroduces one breaks this test instead
    of quietly becoming sweep fodder."""
    root = _sandbox(monkeypatch, tmp_path)
    _ambient(monkeypatch, tmp_path, token="tok")

    host_llm.isolated_run_kwargs()

    entries = sorted(p.name for p in root.iterdir())
    assert entries, "the call created no slot at all"
    assert all(_is_slot_name(name) for name in entries), entries


# --- borrowed authentication ----------------------------------------------------------

def test_token_is_borrowed_into_the_env_and_written_nowhere(monkeypatch, tmp_path):
    root = _sandbox(monkeypatch, tmp_path)
    _ambient(monkeypatch, tmp_path, token="tok-borrowed-123")

    kwargs = host_llm.isolated_run_kwargs()

    assert kwargs["env"][host_llm._OAUTH_TOKEN_ENV_VAR] == "tok-borrowed-123"
    assert kwargs["env"][host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] == host_llm.TOKEN_BORROWED
    on_disk = [p for p in root.rglob("*") if p.is_file()]
    assert not any("tok-borrowed-123" in p.read_text(encoding="utf-8", errors="ignore")
                   for p in on_disk)
    assert not list(root.rglob(host_llm._CREDENTIALS_FILENAME))


def test_borrowing_leaves_the_fleets_credential_file_untouched(monkeypatch, tmp_path):
    ambient = _ambient(monkeypatch, tmp_path, token="tok-1")
    _sandbox(monkeypatch, tmp_path)
    cred = ambient / host_llm._CREDENTIALS_FILENAME
    before = (cred.read_bytes(), cred.stat().st_mode, cred.stat().st_mtime_ns)

    def refuse_write(*a, **k):
        raise AssertionError("isolated_run_kwargs() wrote through pathlib")

    monkeypatch.setattr(Path, "write_text", refuse_write)
    monkeypatch.setattr(Path, "write_bytes", refuse_write)
    host_llm.isolated_run_kwargs()

    assert (cred.read_bytes(), cred.stat().st_mode, cred.stat().st_mtime_ns) == before


def test_other_auth_variables_are_stripped_only_when_a_token_was_borrowed(monkeypatch, tmp_path):
    """Exactly one auth source reaches the child. Leaving a second beside the
    borrowed one makes which credential it used unknowable — the same ambiguity
    that let the false env-carried-auth premise survive stage 1 unnoticed."""
    _sandbox(monkeypatch, tmp_path)
    _ambient(monkeypatch, tmp_path, token="tok-2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    env = host_llm.isolated_run_kwargs()["env"]

    assert env[host_llm._OAUTH_TOKEN_ENV_VAR] == "tok-2"
    assert "ANTHROPIC_API_KEY" not in env


def test_an_env_authenticated_machine_keeps_its_key(monkeypatch, tmp_path):
    """Machine shape (b): a plain API key and no stored token. Stripping
    unconditionally would destroy the only credential it has."""
    _sandbox(monkeypatch, tmp_path)
    _ambient(monkeypatch, tmp_path)  # no credential file at all
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    env = host_llm.isolated_run_kwargs()["env"]

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert host_llm._OAUTH_TOKEN_ENV_VAR not in env
    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] == host_llm.TOKEN_ENV_AUTH
    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] in host_llm.AUTHENTICATED_TOKEN_STATUSES


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"claudeAiOauth": {"accessTok', host_llm.TOKEN_NONE_MALFORMED),   # torn write
        ("", host_llm.TOKEN_NONE_MALFORMED),
        ("{}", host_llm.TOKEN_NONE_MALFORMED),
        ('{"claudeAiOauth": {"accessToken": ""}}', host_llm.TOKEN_NONE_MALFORMED),
        ('{"claudeAiOauth": {"accessToken": null}}', host_llm.TOKEN_NONE_MALFORMED),
    ],
)
def test_a_malformed_credential_degrades_with_a_typed_signal(monkeypatch, tmp_path, raw, expected):
    """advisor.subprocess_runner evaluates isolated_run_kwargs() inside its try
    block, whose `except Exception` arm ledgers and RE-RAISES — so a credential
    problem must never propagate through a judge whose contract is to fail open.
    The torn read is reachable, not theoretical: the client's credential writer
    has an in-place truncate-then-write arm."""
    _sandbox(monkeypatch, tmp_path)
    _ambient(monkeypatch, tmp_path, raw=raw)

    env = host_llm.isolated_run_kwargs()["env"]  # must not raise

    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] == expected
    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] not in host_llm.AUTHENTICATED_TOKEN_STATUSES
    assert host_llm._OAUTH_TOKEN_ENV_VAR not in env


def test_a_missing_credential_degrades_with_its_own_signal(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _ambient(monkeypatch, tmp_path)

    env = host_llm.isolated_run_kwargs()["env"]

    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] == host_llm.TOKEN_NONE_ABSENT
    assert host_llm._OAUTH_TOKEN_ENV_VAR not in env


def test_an_unreadable_credential_degrades_with_its_own_signal(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    ambient = _ambient(monkeypatch, tmp_path, token="tok")
    (ambient / host_llm._CREDENTIALS_FILENAME).chmod(0o000)
    try:
        env = host_llm.isolated_run_kwargs()["env"]
    finally:
        (ambient / host_llm._CREDENTIALS_FILENAME).chmod(0o600)

    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] == host_llm.TOKEN_NONE_UNREADABLE


def test_the_seam_refuses_to_borrow_from_itself(monkeypatch, tmp_path):
    """A judge running inside a sandbox slot has no credential file to borrow
    from — and must not treat the slot's own home as an ambient root."""
    root = _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "1234-5" / "home"))
    for var in (host_llm._OAUTH_TOKEN_ENV_VAR,) + host_llm._OTHER_AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    env = host_llm.isolated_run_kwargs()["env"]

    assert env[host_llm.JUDGE_TOKEN_STATUS_ENV_VAR] == host_llm.TOKEN_NONE_SELF_REFERENTIAL
    assert host_llm._OAUTH_TOKEN_ENV_VAR not in env


# --- r5 enumeration -------------------------------------------------------------------

def test_undispositioned_items_reports_what_no_disposition_covers(monkeypatch, tmp_path):
    root = tmp_path / "amb"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps({"hooks": {}, "brandNewKey": 1}), encoding="utf-8"
    )
    (root / "CLAUDE.md").write_text("x", encoding="utf-8")
    (root / "brand-new-thing").mkdir()

    entries, keys = host_llm.undispositioned_ambient_items(root)

    assert entries == ["brand-new-thing"]  # settings.json and CLAUDE.md are dispositioned
    assert keys == ["brandNewKey"]


def test_dispositions_cover_the_items_the_plan_enumerated():
    """The point of a disposition table is that an item nobody thought of shows
    up as an unanswered name rather than being silently absent — so the entries
    the incident actually turned on are pinned here."""
    for name in (".credentials.json", "settings.json", "CLAUDE.md", ".mcp.json", ".claude.json"):
        assert host_llm._dispositioned(name, host_llm._AMBIENT_ENTRY_DISPOSITIONS), name
    for key in ("hooks", "env", "model", "permissions"):
        assert host_llm._dispositioned(key, host_llm._SETTINGS_KEY_DISPOSITIONS), key


# --- verify-judge-isolation.py's pure comparisons -------------------------------------
#
# --self-test gives RED to only the transcript arm, and this stage's principle
# forbids shipping a control whose RED has never been observed — so each arm's
# RED is exercised directly here.

def test_probe_ratio_arm_green_and_red():
    assert probe.check_ratio(23047, 85288) is None
    assert probe.check_ratio(85000, 85288) is not None      # no saving at all
    assert probe.check_ratio(42644, 85288) is not None      # exactly at the limit
    assert probe.check_ratio(1000, 0) is not None           # no baseline to divide by


def test_probe_floor_arm_green_and_red():
    assert probe.check_floor(23047) is None
    assert probe.check_floor(0) is not None                 # the unauthenticated reading
    assert probe.check_floor(probe.TOKEN_FLOOR) is not None  # boundary is RED


def test_probe_answer_arm_green_and_red():
    assert probe.check_answer("YES") is None
    assert probe.check_answer(" yes.\n") is None
    assert probe.check_answer("NO") is not None
    assert probe.check_answer("") is not None
    assert probe.check_answer("I think yes, because 7 > 3") is not None


def test_probe_witness_arm_green_and_red(tmp_path):
    one = [tmp_path / "a.jsonl"]
    assert probe.check_transcript_count(one) is None
    assert probe.check_transcript_count([]) is not None
    assert probe.check_transcript_count(one + [tmp_path / "b.jsonl"]) is not None


def test_probe_counts_every_input_side_token():
    """input_tokens alone undercounts by an order of magnitude once the ambient
    surface is cached, which would make the ratio arm compare noise."""
    usage = {
        "input_tokens": 4,
        "cache_creation_input_tokens": 40707,
        "cache_read_input_tokens": 44577,
        "output_tokens": 9,
    }
    assert probe.total_input_tokens(usage) == 85288
    assert probe.total_input_tokens({}) == 0


def test_probe_snapshot_carries_no_credential_and_no_hooks(monkeypatch, tmp_path):
    """The baseline arm must be live but SIDE-EFFECT-FREE. A wholesale copy would
    duplicate the credential onto disk and violate the very requirement the probe
    exists to enforce; a settings.json built by DELETING `hooks` would keep
    mcpServers, statusLine and apiKeyHelper, each of which runs something."""
    ambient = tmp_path / "amb"
    ambient.mkdir()
    (ambient / "CLAUDE.md").write_text("big context", encoding="utf-8")
    (ambient / host_llm._CREDENTIALS_FILENAME).write_text("secret", encoding="utf-8")
    (ambient / "settings.json").write_text(
        json.dumps({
            "env": {"A": "1"}, "hooks": {"Stop": []},
            "mcpServers": {"x": {}}, "statusLine": {"command": "boom"},
            "apiKeyHelper": "boom",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))

    dest = probe.build_ambient_snapshot(tmp_path / "snap")

    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == "big context"
    assert not (dest / host_llm._CREDENTIALS_FILENAME).exists()
    kept = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    assert kept == {"env": {"A": "1"}}


def test_probe_ancestor_walk_finds_planted_project_context(tmp_path):
    """The client discovers project context by walking cwd's ancestors, so a
    directory under a shared /var/tmp is only neutral by accident."""
    cwd = tmp_path / "a" / "b" / "cwd"
    cwd.mkdir(parents=True)
    clean: list[str] = []
    probe.assert_no_ancestor_project_context(cwd, clean)
    assert clean == []

    (tmp_path / "a" / "CLAUDE.md").write_text("x", encoding="utf-8")
    dirty: list[str] = []
    probe.assert_no_ancestor_project_context(cwd, dirty)
    assert len(dirty) == 1 and "CLAUDE.md" in dirty[0]
