"""Host-dispatching argv assembly for a short, single-turn LLM call.

`agentctl.advisor`'s warn-only judges and `lib.marker_extract`'s second-pass
marker extractor both need "run one prompt through this host's CLI and read
stdout" without hand-rolling the argv for whichever host they are bound to.
This module is the one seam that knows how to build that argv per host and
refuses to build one that crosses hosts.

Distinct from spawn-specialist.py / spawn-cursor-specialist.py: those own the
FULL specialist-spawn template (recursion cap, budget, permissions digest,
transcript discovery, cost logging). This module only assembles the argv for a
bare `<binary> -p ... <prompt>` call — the shared shape underlying both the
advisor's ~20s judge calls and the marker extractor's ~30s classification call.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

from .config_root import harness_config_root
from .runtime_models import HOST_CLAUDE, HOST_CURSOR, HOSTS

DEFAULT_CURSOR_API_KEY_FILE = Path.home() / ".cursor_api_key"

# A bare `claude -p ...` argv is a FULL Claude Code session: absent isolation it
# inherits the ambient CLAUDE_CONFIG_DIR and cwd, so it loads the fleet's own
# settings.json and registers the same hooks the coordinating session runs under.
# Measured on this machine: a judge prompt containing feedback-shaped example text
# (the self-improvement judge's own template) re-entered hook-self-improvement-
# reminder.py on itself, recursing 126 levels deep to a 111 364-char prompt, and
# 1583 such calls exhausted a 5-hour quota window in 48 minutes. Pointing
# CLAUDE_CONFIG_DIR at an empty, agent-owned directory and running from an empty
# cwd (no CLAUDE.md chain to discover) leaves no settings.json for any hook to
# register from, so the recursion cannot start regardless of prompt content. A
# live A/B on the same 989-byte prompt: ambient cache_creation_input_tokens
# 40 707 vs. isolated 8 499.
#
# The path is TMPDIR-dependent and moves with the caller's environment (on this
# fleet TMPDIR=/var/tmp, so it is NOT under /tmp). Every check, test and comment
# that needs it must import this name; a literal rots the first time TMPDIR
# differs.
_SANDBOX_ROOT = Path(tempfile.gettempdir()) / "claude-judge-sandbox"

# Root-level leftovers from an obsolete shape in which the ROOT itself was the
# slot, rather than `<root>/<pid>-<tid>/`. `_prune_dead_slots` keys a slot's
# liveness off its leading pid, so these two never age out — and `home/` is a
# populated config dir that goes on accumulating session state. No shipped code
# path builds either name at the root's top level any more (isolated_run_kwargs
# builds them one level down, inside a slot), so sweeping them can never race a
# live call. The standing guard behind that claim is a test that drives
# isolated_run_kwargs() and asserts the root gains no top-level entry outside
# the `<pid>-<tid>` shape: a future code path that reintroduces the name breaks
# that test instead of quietly becoming sweep fodder.
_ROOT_RESIDUE_NAMES = ("home", "cwd")

# Defense in depth alongside the sandbox above: CLAUDE_CONFIG_DIR isolation
# removes the hook-recursion TRIGGER (no settings.json for a hook to register
# from), but a hook that still runs inside this env (imported by a future
# caller that forgets to isolate, or invoked directly for some other reason)
# has no way to tell "I am the sandboxed child" from "I am the coordinating
# session". Every hook that itself calls a judge checks this marker first and
# exits before doing any work — a second, independent line of defense that
# does not depend on the sandbox actually being wired up correctly everywhere.
JUDGE_CHILD_ENV_VAR = "AGENTCTL_JUDGE_CHILD"


class SandboxRootUnsafe(Exception):
    """Refused: _SANDBOX_ROOT is not a real directory owned by this uid.

    Raised rather than degraded, unlike a credential problem: a judge that
    cannot authenticate fails loudly and costs one verdict, whereas continuing
    into a root somebody else controls hands them the slot contents and points
    `_prune_dead_slots`' rmtree wherever they aimed the symlink.
    """


def _harden_root() -> None:
    """Make _SANDBOX_ROOT a uid-owned 0700 real directory, or refuse.

    The root sits at a predictable path under a world-writable parent, so an
    attacker can pre-plant it — most dangerously as a SYMLINK to a directory
    that would itself pass every ownership check (~/.ssh), after which pruning
    deletes inside their target rather than ours. Ownership is therefore checked
    on a DESCRIPTOR, not on the path twice: O_NOFOLLOW makes the open itself
    refuse a symlink, and fstat/fchmod act on the inode that fd already pins, so
    there is no window in which the checked object and the changed object can
    differ.

    Over-permissive-but-ours is REMEDIATED, not refused. The live root was
    created 0775 by this module's own earlier revision (mkdir under the fleet's
    0002 umask), so a bare "refuse unless 0700" would refuse on the first judge
    call after shipping and disarm every judge on the machine. Tightening it to
    0700 cannot break a concurrent sibling either: owner rwx is retained and
    every judge call on a given machine runs as one uid — a call from a
    DIFFERENT uid losing access is the point of the hardening, not a regression.
    """
    try:
        _SANDBOX_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        pass  # a symlink or a non-directory; the O_NOFOLLOW open below names it
    except OSError as exc:
        raise SandboxRootUnsafe(f"cannot create {_SANDBOX_ROOT}: {exc}") from exc

    try:
        fd = os.open(_SANDBOX_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SandboxRootUnsafe(
            f"refusing {_SANDBOX_ROOT}: not a directory this process may open "
            f"without following a symlink ({exc})"
        ) from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise SandboxRootUnsafe(
                f"refusing {_SANDBOX_ROOT}: owned by uid {st.st_uid}, not {os.getuid()}"
            )
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _prune_dead_slots() -> list[str]:
    """Remove sandbox slots whose owning process is gone; return what was left.

    A slot is named `<pid>-<tid>`, so its owner is decidable rather than guessed
    at by age: a slot whose pid no longer exists can never be written to again,
    while a slot belonging to a live process is skipped however old it is. Purely
    best-effort — a concurrent pruner may unlink the same tree first, and a
    recycled pid only leaves one stale slot behind.

    Entries that are not slots used to be skipped unconditionally, which is how
    `_ROOT_RESIDUE_NAMES` came to sit at the root indefinitely. They are now
    ACCOUNTED FOR instead: the two known residue names are swept, and anything
    else is returned so a caller can see it rather than have it vanish into the
    same silent `continue`.
    """
    try:
        entries = list(_SANDBOX_ROOT.iterdir())
    except OSError:
        return []
    unaccounted: list[str] = []
    for entry in entries:
        pid_part, sep, _ = entry.name.partition("-")
        if sep and pid_part.isdigit():
            try:
                os.kill(int(pid_part), 0)
            except ProcessLookupError:
                _remove(entry)
            except OSError:
                pass
            continue
        if entry.name in _ROOT_RESIDUE_NAMES:
            _remove(entry)
            continue
        unaccounted.append(entry.name)
    return unaccounted


def _remove(entry: Path) -> None:
    """Delete a root-level entry without ever following it out of the root.

    `shutil.rmtree` recurses only into `is_dir(follow_symlinks=False)` children,
    so a symlink INSIDE a slot is unlinked rather than walked — but handed a
    symlinked slot itself it raises, which `ignore_errors` would swallow into a
    leak. Unlink that case explicitly instead.
    """
    if entry.is_symlink():
        try:
            entry.unlink()
        except OSError:
            pass
        return
    shutil.rmtree(entry, ignore_errors=True)


# --- borrowed authentication -------------------------------------------------
#
# Auth on this fleet is FILE-carried: the client resolves its credential at
# `CLAUDE_CONFIG_DIR ?? ~/.claude`, so overriding that root severs the child
# from the live file. That is the whole story of both the defect and the fix.
# Measured 2026-08-25 on the shipped isolation, same prompt, one script: the
# ambient control returned is_error=false, the isolated call is_error=true /
# "Not logged in · Please run /login" / cost 0 — and the advisor being
# fail-open, every judge on the machine had been silently answering nothing.
#
# The repair LENDS rather than SHARES. The token is read fresh per call and
# handed to the child through its environment; the file is never copied, never
# linked, and never reachable from the child at all. `CLAUDE_CODE_OAUTH_TOKEN`
# is a first-class auth source in the client's own enum and is explicitly
# non-adopting ("keeping the user-supplied CLAUDE_CODE_OAUTH_TOKEN instead of
# adopting the stored credential"), so a child cannot write a rotated refresh
# token back over the fleet's one credential. An earlier repair that symlinked
# the file into each slot was rejected for exactly that blast radius: the
# refresh token ROTATES, and these calls run concurrently under a
# ThreadPoolExecutor.
#
# The genuine downgrade, stated rather than glossed: /proc/<pid>/environ is
# 0400 owner-only — the same uid boundary the 0600 file has — but the token now
# sits in every descendant's environment and in anything that dumps one, where
# the file was read only on demand. The accepted cost: a call landing between
# expiry and the parent's next refresh gets a 401 it cannot self-recover from —
# loud, bounded, and surfaced by its own failure reason, traded against
# unbounded silent corruption of the credential the interactive session runs on.
_CREDENTIALS_FILENAME = ".credentials.json"
_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
# Env-carried auth sources OTHER than the borrowed OAuth token. Their presence
# means the child already has a credential the client's own precedence ladder
# ranks ABOVE the stored file — so borrowing is suppressed and these are left
# untouched. Stripping them would destroy the only credential a proxy- or
# API-key-authenticated machine has.
_OTHER_AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Gateway-mode flags: when either is truthy the client routes through
# Bedrock/Vertex instead of the Anthropic API and picks up AWS/GCP credentials
# from the ambient environment. They rank above every other auth source on the
# client's ladder, so a machine that carries one is env-authenticated even
# without an ANTHROPIC_* variable set — otherwise a stored credential file
# would drag such a machine into TOKEN_BORROWED and mislabel every failure.
_GATEWAY_MODE_ENV_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

# Why the child has the auth it has, recorded in its own environment so the
# caller can read it back off the kwargs it just built — no module-level state
# to race, and nothing splat-incompatible added to the subprocess.run kwargs.
JUDGE_TOKEN_STATUS_ENV_VAR = "AGENTCTL_JUDGE_TOKEN_STATUS"

TOKEN_BORROWED = "borrowed"
TOKEN_ENV_AUTH = "env_auth"
TOKEN_NONE_ABSENT = "no_credential_file"
TOKEN_NONE_UNREADABLE = "credential_unreadable"
TOKEN_NONE_MALFORMED = "credential_malformed"
TOKEN_NONE_SELF_REFERENTIAL = "credential_root_is_sandbox"

# The statuses under which the child holds SOME credential. Its complement is
# the one failure mode this isolation seam can itself cause, which is why it
# gets a failure reason of its own rather than sharing the quota bucket: "we
# could not supply a token" and "the service refused us" have different
# operators and different fixes.
AUTHENTICATED_TOKEN_STATUSES = frozenset({TOKEN_BORROWED, TOKEN_ENV_AUTH})


def _read_oauth_token(path: Path) -> tuple[str | None, str]:
    """Read claudeAiOauth.accessToken, or say why not. Never raises.

    Every arm returns rather than propagating, because the sole caller runs
    inside `advisor.subprocess_runner`'s try block, whose `except Exception` arm
    ledgers and RE-RAISES — an exception here would turn a missing credential
    into a crash through a judge whose entire contract is to fail open. The torn
    read is reachable rather than theoretical: the client's credential writer
    has an in-place arm (O_WRONLY|O_CREAT|O_NOFOLLOW then truncate then write),
    and its own reader carries an explicit "corrupt" state.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, TOKEN_NONE_ABSENT
    except OSError:
        return None, TOKEN_NONE_UNREADABLE
    try:
        token = json.loads(raw)["claudeAiOauth"]["accessToken"]
    except Exception:
        return None, TOKEN_NONE_MALFORMED
    if not isinstance(token, str) or not token.strip():
        return None, TOKEN_NONE_MALFORMED
    return token, TOKEN_BORROWED


def _lend_auth(env: dict) -> str:
    """Give `env` exactly one credential and return why it has the one it has.

    Never raises (see `_read_oauth_token`). The client's own auth-precedence
    ladder ranks env-carried auth ABOVE the stored credential, so the env is
    consulted FIRST: a machine that already carries env auth (an API key, an
    auth token, a Bedrock/Vertex gateway flag, or a previously-lent OAuth
    token) is left untouched — no file read, no borrow, no strip. Only a
    machine with no env auth reaches the credential file, and only then does
    the token get lent through the child's environment.

    A machine authenticated by `apiKeyHelper` genuinely loses auth under
    isolation, because the helper is a command name declared in settings.json
    and isolation is precisely the removal of settings.json; no borrow can fix
    that, and what this returns for it is loudness rather than function.
    """
    if _has_env_auth(env):
        # Env-auth outranks the stored credential on the client's ladder, so
        # borrowing would either be overridden (Bedrock/Vertex) or make which
        # credential the child ran on unknowable (an ANTHROPIC_* key alongside
        # a lent OAuth token). Neither is what the seam wants to hand a judge.
        return TOKEN_ENV_AUTH

    ambient = harness_config_root()
    if ambient == _SANDBOX_ROOT or _SANDBOX_ROOT in ambient.parents:
        # Already inside a sandbox slot: there is no credential file to borrow
        # from, and env auth would have already returned above if present.
        return TOKEN_NONE_SELF_REFERENTIAL

    token, status = _read_oauth_token(ambient / _CREDENTIALS_FILENAME)
    if token is None:
        return status

    # No strip loop: _has_env_auth returned False above, which means every
    # variable in _OTHER_AUTH_ENV_VARS was already absent. A pop here would be
    # a dead no-op and would read as a live guard the code no longer needs.
    env[_OAUTH_TOKEN_ENV_VAR] = token
    return TOKEN_BORROWED


def _has_env_auth(env: dict) -> bool:
    return any(
        (env.get(var) or "").strip()
        for var in (_OAUTH_TOKEN_ENV_VAR,) + _OTHER_AUTH_ENV_VARS + _GATEWAY_MODE_ENV_VARS
    )


# --- r5: what the ambient config root supplies, and what isolation does to it -
#
# Isolation must remove no capability the judge needs. The auth loss above was
# ONE instance of that class, and it hid for two stages, so the requirement is
# discharged by an enumeration rather than by recall: every top-level entry of
# the ambient root and every top-level settings.json key carries a disposition
# here, and `undispositioned_ambient_items` reports whatever does not — so an
# item nobody thought of surfaces as an unanswered name instead of being
# silently absent. Keys may be fnmatch globs; runtime state that accretes under
# the root is matched by pattern rather than enumerated file by file.
_AMBIENT_ENTRY_DISPOSITIONS: dict[str, str] = {
    ".credentials.json": (
        "NEEDED. Dropping it is the defect this stage repairs; restored by "
        "borrowing the access token into the child's env, never by copying"
    ),
    "settings.json": (
        "DROPPED ON PURPOSE — its `hooks` block is the recursion, and its absence "
        "is what keeps the child from registering any. Cost-shaping keys only are "
        "rebuilt, from an allowlist, into verify-judge-isolation.py's baseline"
    ),
    "settings.json.bak*": "inert backup; the client never reads it",
    "CLAUDE.md": "DROPPED deliberately — this surface IS the context the isolation saves",
    "config.md": "dropped with CLAUDE.md, which imports it",
    "memory-global": "dropped with CLAUDE.md, which imports its index",
    "skills": (
        "dropped; a single-turn judge prompt invokes no skill. A future judge that "
        "needs one fails visibly (unknown skill), not silently"
    ),
    "skills-local": "dropped, as skills/",
    "agents": "dropped; the judge spawns no subagent — that it cannot is the point",
    "plugins": "dropped; no judge prompt loads a plugin",
    "difficulty-channel-plugins": "our own tooling's seam, read by us, never by the child",
    "project-entry-plugins": "our own tooling's seam, read by us, never by the child",
    ".mcp.json": "dropped, and a saving: no MCP server is started for the child",
    ".claude.json": "SURVIVABLE — onboarding/config cache; costs one gate fetch per slot",
    "history.jsonl": "dropped; prompt history is per-slot and carries no capability",
    "projects": "dropped by design — a fresh projects/ is what lets the probe count transcripts",
    "sessions": "dropped with projects/",
    "backups": "dropped with projects/",
    "shell-snapshots": "dropped; the child runs no shell",
    "file-history": "dropped; the child edits no file",
    "cache": "dropped; a cache miss costs time, never capability",
    "paste-cache": "dropped, as cache/",
    "gh-pr-status-cache.json": "dropped, as cache/",
    # Fleet-side state that OUR hooks and scripts reach through
    # harness_config_root(). The child runs no hook, so dropping these removes
    # nothing it uses — and keeping them out of its root is also what denies a
    # judge subprocess any write path to the session's own bookkeeping.
    "agentctl": "fleet state, read by our hooks — not by the child",
    "state": "fleet state, read by our hooks — not by the child",
    "tasks": "fleet state, read by our hooks — not by the child",
    "jobs": "fleet state, read by our hooks — not by the child",
    "teams": "fleet state, read by our hooks — not by the child",
    "plans": "fleet state, read by our hooks — not by the child",
    "artifacts": "fleet state, read by our hooks — not by the child",
    "session-env": "fleet state, read by our hooks — not by the child",
    "orphan-worktree-deadletter": "fleet state, read by our hooks — not by the child",
    "monitored-reviews.json*": "fleet state, read by our hooks — not by the child",
    "daemon*": "the fleet's own daemon and its status/lock files; nothing in the child's path",
    ".last-cleanup": "fleet housekeeping stamp",
    ".last-update-result.json": "fleet housekeeping stamp",
    "term-rulesets": "per-machine config for our linters",
    "*.local": "per-machine config for our own tooling; the child consults none of it",
}

_SETTINGS_KEY_DISPOSITIONS: dict[str, str] = {
    "hooks": "DROPPED ON PURPOSE. This key is the recursion; removing it is the fix",
    "env": "harmless — env reaches the child as a copy of os.environ, not through settings",
    "model": (
        "harmless on HOST_CLAUDE, which requires --model. Also harmless on "
        "HOST_CURSOR, contrary to the suspicion this enumeration was written to "
        "check: model=None there is the documented way to ask Cursor for Auto "
        "(every CURSOR_COMPLEXITY_MODEL tier is None), and the `agent` binary "
        "reads neither this file nor CLAUDE_CONFIG_DIR, so isolation cannot "
        "change which model it picks — the sandbox root has no settings.json for "
        "anything to default from in the first place"
    ),
    "permissions": (
        "harmless today: no judge prompt expects tool use. A future judge that "
        "reads a file would be denied, and would fail silently — record it here "
        "rather than rediscover it"
    ),
    "autoCompactWindow": "irrelevant to a single-turn call, which never compacts",
    "skipAutoPermissionPrompt": "irrelevant under -p, which is non-interactive",
    "theme": "display only",
    "tui": "display only",
}


def undispositioned_ambient_items(
    root: Path | None = None,
) -> tuple[list[str], list[str]]:
    """(root entries, settings.json keys) carrying no recorded disposition.

    Reads the live ambient root, so it answers what isolation drops on THIS
    machine rather than what a table written once claimed it dropped.
    """
    root = root if root is not None else harness_config_root()
    try:
        names = sorted(p.name for p in root.iterdir())
    except OSError:
        names = []
    entries = [n for n in names if not _dispositioned(n, _AMBIENT_ENTRY_DISPOSITIONS)]

    try:
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        settings = {}
    keys = sorted(settings) if isinstance(settings, dict) else []
    return entries, [k for k in keys if not _dispositioned(k, _SETTINGS_KEY_DISPOSITIONS)]


def _dispositioned(name: str, table: dict) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in table)


def isolated_run_kwargs() -> dict:
    """kwargs for subprocess.run() that isolate a single-turn `claude -p ...` call
    from the fleet's own hook registrations (see _SANDBOX_ROOT's comment above).

    `env` is os.environ COPIED, never replaced, with CLAUDE_CONFIG_DIR overridden
    and exactly one auth source present (see the borrowed-authentication section
    above). `cwd` is a separate empty directory so no project-level CLAUDE.md
    chain is discovered from the working directory either.

    The sandbox is per (process, thread), NOT one shared pair: `claude` treats
    CLAUDE_CONFIG_DIR as writable live state (`.claude.json`, `history.jsonl`),
    and these calls genuinely do run concurrently — measure-marker-extractor-
    latency.py drives them through a ThreadPoolExecutor — so one shared root
    would have N subprocesses racing on the same files, trading the recursion
    this function removes for a corruption that degrades the judges just as
    silently. Keyed by pid+tid rather than a fresh mkdtemp per call so a caller
    making many calls reuses one directory instead of leaving one behind each
    time; slots of exited processes are reaped by _prune_dead_slots.

    Raises `SandboxRootUnsafe` — and nothing else — when the sandbox root is a
    symlink or belongs to another uid. Every OTHER failure degrades: a missing,
    unreadable or malformed credential yields a child with no borrowed token and
    a typed status in `env[JUDGE_TOKEN_STATUS_ENV_VAR]`, never an exception. The
    split is deliberate. `advisor.subprocess_runner` evaluates this function
    INSIDE its try block, whose `except Exception` arm ledgers and re-raises, so
    a credential problem must not propagate through a judge that exists to fail
    open; a root somebody else controls must, because continuing would hand them
    the slot contents and aim `_prune_dead_slots`' rmtree wherever they chose.
    """
    _harden_root()
    _prune_dead_slots()
    slot = _SANDBOX_ROOT / f"{os.getpid()}-{threading.get_ident()}"
    home = slot / "home"
    cwd = slot / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    env[JUDGE_CHILD_ENV_VAR] = "1"
    env[JUDGE_TOKEN_STATUS_ENV_VAR] = _lend_auth(env)
    return {"cwd": str(cwd), "env": env}


_HOST_BINARY_FAMILY = {
    HOST_CLAUDE: frozenset({"claude"}),
    HOST_CURSOR: frozenset({"agent", "cursor-agent"}),
}


class CrossHostError(Exception):
    """Refused: the assembled command would invoke a binary outside the bound
    runtime_host's family."""


def binary_for(host: str) -> str:
    if host == HOST_CLAUDE:
        return "claude"
    if host == HOST_CURSOR:
        return shutil.which("agent") or shutil.which("cursor-agent") or "agent"
    raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")


def assert_same_family(binary: str, host: str) -> None:
    if host not in _HOST_BINARY_FAMILY:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")
    name = Path(binary).name
    family = _HOST_BINARY_FAMILY[host]
    if name not in family:
        raise CrossHostError(
            f"refused: runtime_host={host!r} must never invoke {name!r} "
            f"(allowed binaries: {sorted(family)})"
        )


def cursor_api_key_present(api_key_file: Path = DEFAULT_CURSOR_API_KEY_FILE) -> bool:
    if os.environ.get("CURSOR_API_KEY", "").strip():
        return True
    try:
        return api_key_file.is_file() and bool(api_key_file.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def preflight(host: str) -> tuple[bool, str]:
    if host == HOST_CLAUDE:
        if shutil.which("claude") is None:
            return False, "`claude` not on PATH"
        return True, ""
    if host == HOST_CURSOR:
        if shutil.which("agent") is None and shutil.which("cursor-agent") is None:
            return False, "neither `agent` nor `cursor-agent` on PATH"
        if not cursor_api_key_present():
            return False, (
                "CURSOR_API_KEY not set and no readable key at "
                f"{DEFAULT_CURSOR_API_KEY_FILE}"
            )
        return True, ""
    raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")


def build_prompt_argv(
    host: str, model: str | None, prompt: str, *, workspace: "Path | str | None" = None
) -> list[str]:
    binary = binary_for(host)
    if host == HOST_CLAUDE:
        if model is None:
            raise ValueError("model is required for HOST_CLAUDE")
        argv = [binary, "-p", "--model", model, prompt]
    elif host == HOST_CURSOR:
        # `model is None` is a FIRST-CLASS value here, not an omission: every tier
        # of CURSOR_COMPLEXITY_MODEL is None, and omitting --model is how a caller
        # asks Cursor for Auto (spawn-cursor-specialist.py's --complexity help says
        # so verbatim). It is also isolation-safe: the only root isolation replaces
        # is CLAUDE_CONFIG_DIR, which the `agent` binary never reads, and HOME is
        # inherited untouched — so Auto resolves identically inside and outside the
        # sandbox. See the r5 disposition table below.
        argv = [binary, "-p", "--trust", "--force", "--approve-mcps",
                "--output-format", "text"]
        if model is not None:
            argv += ["--model", model]
        if workspace is not None:
            argv += ["--workspace", str(workspace)]
        argv.append(prompt)
    else:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")
    assert_same_family(binary, host)
    return argv
