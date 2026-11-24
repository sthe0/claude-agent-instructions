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
import getpass
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config_root import harness_config_root
from .runtime_models import HOST_CLAUDE, HOST_CURSOR, HOST_OPENCODE, HOSTS

DEFAULT_CURSOR_API_KEY_FILE = Path.home() / ".cursor_api_key"

# A bare `claude -p ...` argv is a FULL Claude Code session: absent isolation it
# inherits the ambient CLAUDE_CONFIG_DIR and cwd, so it loads the fleet's own
# settings.json and registers the same hooks the coordinating session runs under.
# Measured on this machine: a judge prompt containing feedback-shaped example text
# (the self-improvement judge's own template) re-entered the
# self-improvement reminder of hook-prompt-reminders.py on itself, recursing 126 levels deep to a 111 364-char prompt, and
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
TOKEN_BORROWED_KEYCHAIN = "borrowed_keychain"
TOKEN_ENV_AUTH = "env_auth"
TOKEN_NONE_ABSENT = "no_credential_file"
TOKEN_NONE_UNREADABLE = "credential_unreadable"
TOKEN_NONE_MALFORMED = "credential_malformed"
TOKEN_NONE_SELF_REFERENTIAL = "credential_root_is_sandbox"
TOKEN_NONE_KEYCHAIN_ABSENT = "no_credential_keychain"
TOKEN_NONE_KEYCHAIN_MALFORMED = "credential_keychain_malformed"

# The statuses under which the child holds SOME credential. Its complement is
# the one failure mode this isolation seam can itself cause, which is why it
# gets a failure reason of its own rather than sharing the quota bucket: "we
# could not supply a token" and "the service refused us" have different
# operators and different fixes.
AUTHENTICATED_TOKEN_STATUSES = frozenset(
    {TOKEN_BORROWED, TOKEN_BORROWED_KEYCHAIN, TOKEN_ENV_AUTH}
)

# macOS Keychain service names the client's own credential writer uses,
# checked in this order. The plain name is the historical/default form; the
# hashed form is what a non-default CLAUDE_CONFIG_DIR gets (the writer mixes
# the config dir path into the service name so two config roots on one
# machine don't collide on one Keychain item). Confirmed empirically on this
# machine 2026-09-23: both forms resolved to a readable item, so trying the
# hashed one first costs nothing when the plain one would also have worked,
# and matters on a machine where only the hashed one is populated.
_KEYCHAIN_SERVICE_BASE = "Claude Code-credentials"


def _keychain_service_candidates(config_dir: Path) -> list[str]:
    """Service names to try, for `config_dir`, in preference order.

    The plain name is scoped to the actual default config root (`~/.claude`,
    the same root `lib.config_root.harness_config_root` falls back to when no
    `CLAUDE_CONFIG_DIR` override is set) rather than offered unconditionally.
    Without that guard, a non-default `config_dir` (a per-project or
    per-identity `CLAUDE_AGENT_HOME`) whose own hashed item was never written
    would silently borrow whatever account happens to be logged in under the
    plain item instead — a different identity than the one `ambient` names,
    which is exactly the identity-blurring `_lend_auth`'s other checks (the
    self-referential sandbox guard, the env-auth precedence) exist to avoid.
    """
    digest = hashlib.sha256(str(config_dir).encode("utf-8")).hexdigest()[:8]
    candidates = [f"{_KEYCHAIN_SERVICE_BASE}-{digest}"]
    if config_dir == Path.home() / ".claude":
        candidates.append(_KEYCHAIN_SERVICE_BASE)
    return candidates


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


def _read_oauth_token_from_keychain(config_dir: Path) -> tuple[str | None, str]:
    """Read claudeAiOauth.accessToken out of the macOS login Keychain.

    On macOS, `claude login` writes the OAuth blob into the Keychain (service
    "Claude Code-credentials", optionally suffixed with a hash of
    CLAUDE_CONFIG_DIR) instead of `.credentials.json` — per public docs and
    community reports (not independently re-verified against the CLI's
    source), the file is not created at all on that platform, at least as of
    the client version observed on this machine 2026-09-23. So
    `_read_oauth_token` finding it absent there is not a missing-login
    signal, it is the normal state. Never
    raises, for the same reason `_read_oauth_token` never does: the sole
    caller is `_lend_auth`, reached from inside `subprocess_runner`'s
    fail-open try block.

    No-op (returns absent) off macOS or when the `security` binary is
    missing — this is a fallback for one platform's storage choice, not a
    generic keyring integration.

    Per-candidate `timeout=2` keeps a worst-case two-candidate lookup well
    under the caller's own subprocess timeout — this call happens INSIDE
    `isolated_run_kwargs()`, before the judge's own `subprocess.run(...,
    timeout=...)` even starts, so it is not counted against that budget at
    all; a real `security` lookup normally answers in well under 100ms, so 2s
    is generous slack, not a tight ceiling.
    """
    if sys.platform != "darwin" or shutil.which("security") is None:
        return None, TOKEN_NONE_KEYCHAIN_ABSENT
    account = os.environ.get("USER") or getpass.getuser()
    for service in _keychain_service_candidates(config_dir):
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", account,
                 "-s", service, "-w"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            token = json.loads(result.stdout)["claudeAiOauth"]["accessToken"]
        except Exception:
            return None, TOKEN_NONE_KEYCHAIN_MALFORMED
        if isinstance(token, str) and token.strip():
            return token, TOKEN_BORROWED_KEYCHAIN
        return None, TOKEN_NONE_KEYCHAIN_MALFORMED
    return None, TOKEN_NONE_KEYCHAIN_ABSENT


def _lend_auth(env: dict) -> str:
    """Give `env` exactly one credential and return why it has the one it has.

    Never raises (see `_read_oauth_token`). The client's own auth-precedence
    ladder ranks env-carried auth ABOVE the stored credential, so the env is
    consulted FIRST: a machine that already carries env auth (an API key, an
    auth token, a Bedrock/Vertex gateway flag, or a previously-lent OAuth
    token) is left untouched — no file read, no borrow, no strip. Only a
    machine with no env auth reaches the credential store, and only then does
    the token get lent through the child's environment.

    Two stores are tried, in the order this seam checks them: `.credentials.json`
    first, then the macOS login Keychain (which store the client itself would
    prefer when both happen to hold a credential is not something this was
    verified against — only the Keychain-vs-absent-file case was probed live).
    This is not redundant with the file check — on macOS the client writes the OAuth blob
    ONLY to the Keychain and never creates the file at all, so a machine
    authenticated that way always fell through the file read; this seam
    stayed silent about it because a missing file "degrades" the same way a
    missing token does, until it was traced to a live machine that logs in
    successfully yet had no `.credentials.json` anywhere under its config
    root (see `_read_oauth_token_from_keychain`'s docstring). Off macOS this
    second lookup is a no-op and the behavior is unchanged.

    A machine authenticated by `apiKeyHelper` still genuinely loses auth under
    isolation, because the helper is a command name declared in settings.json
    and isolation is precisely the removal of settings.json; neither store
    holds a credential for that machine, so what this returns for it is
    loudness rather than function.
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
        # Only fall to the Keychain when the file itself said nothing —
        # present-but-broken (unreadable/malformed) is already the more
        # actionable signal than anything a second store could add, so
        # skip a `security` shell-out whose result would just be discarded.
        if status != TOKEN_NONE_ABSENT:
            return status
        token, keychain_status = _read_oauth_token_from_keychain(ambient)
        if token is None:
            # A plain absence on both stores collapses to the keychain
            # status only when it is itself informative (malformed).
            return keychain_status if keychain_status != TOKEN_NONE_KEYCHAIN_ABSENT else status
        status = TOKEN_BORROWED_KEYCHAIN

    # No strip loop: _has_env_auth returned False above, which means every
    # variable in _OTHER_AUTH_ENV_VARS was already absent. A pop here would be
    # a dead no-op and would read as a live guard the code no longer needs.
    env[_OAUTH_TOKEN_ENV_VAR] = token
    return status


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


# HOST_CLAUDE isolation bundle for a binary-verdict judge call, inserted before
# the trailing prompt argv element when `lean=True`. Copied verbatim from
# samples/judge-latency/ab.py's LEAN constant rather than reinvented: a clean,
# contention-free A/B (this session's /tmp/cc-scratch/clean-ab-feedback.py)
# showed feedback_signal scoring 0/3 under the bare invocation (2 timeouts, 1
# unparseable "I don't have direct access to..." answer) and 3/3 under this
# bundle, because a bare `claude -p` loads the full ambient CLAUDE.md and the
# judge drifts into role-playing as the root coordinator instead of returning
# a verdict — a collision specific to prompts that ask about agent behavior.
LEAN_ISOLATION_FLAGS = [
    "--system-prompt",
    "You are a strict binary classifier. Follow the user's instructions exactly.",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--no-session-persistence",
]

_HOST_BINARY_FAMILY = {
    HOST_CLAUDE: frozenset({"claude"}),
    HOST_CURSOR: frozenset({"agent", "cursor-agent"}),
    HOST_OPENCODE: frozenset({"opencode"}),
}


class CrossHostError(Exception):
    """Refused: the assembled command would invoke a binary outside the bound
    runtime_host's family."""


USAGE_NOT_REQUESTED = "not_requested"


def host_of_binary(binary) -> "str | None":
    """The host id whose family `binary` belongs to; None for a binary that is no LLM host."""
    name = Path(str(binary)).name
    return next((host for host, family in _HOST_BINARY_FAMILY.items() if name in family), None)


def binary_for(host: str) -> str:
    if host == HOST_CLAUDE:
        return "claude"
    if host == HOST_CURSOR:
        return shutil.which("agent") or shutil.which("cursor-agent") or "agent"
    if host == HOST_OPENCODE:
        return shutil.which("opencode") or "opencode"
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
    if host == HOST_OPENCODE:
        from . import opencode_env

        not_ready = opencode_env.not_ready()
        return (False, not_ready[1]) if not_ready else (True, "")
    raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")


STRUCTURED_OUTPUT_FLAGS = ["--output-format", "json"]
_OPENCODE_USAGE_FIELDS = {"input": "input_tokens", "output": "output_tokens"}


def _unwrap_opencode_events(stdout: str) -> "tuple[str, dict | None, str | None]":
    texts, usage, seen = [], None, False
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") if isinstance(event, dict) else None
        if not isinstance(part, dict):
            continue
        seen = True
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif event.get("type") == "step_finish" and isinstance(part.get("tokens"), dict):
            tokens = part["tokens"]
            usage = {to: tokens[frm] for frm, to in _OPENCODE_USAGE_FIELDS.items()
                     if isinstance(tokens.get(frm), int)}
    if not seen:
        return stdout, None, "unparsed"
    return "".join(texts), usage, None if usage else "no_usage"


def unwrap_structured(argv, stdout: str) -> "tuple[str, dict | None, str | None]":
    """(reply text, usage, reason usage is None) of one child's stdout. For an argv built with
    `structured_output` the stdout is the JSON envelope; any other stdout is returned unchanged.
    A Claude argv without the envelope request is `not_requested` (the caller could have had
    usage); a host whose output carries none is `usage_unavailable:<host id>`. An OpenCode argv
    always asks for the JSON event stream, so its stdout is read as events: the reply is the joined
    `text` parts, the usage the `step_finish` tokens."""
    if argv and host_of_binary(argv[0]) == HOST_OPENCODE:
        return _unwrap_opencode_events(stdout)
    if argv[-len(STRUCTURED_OUTPUT_FLAGS):] != STRUCTURED_OUTPUT_FLAGS:
        host = host_of_binary(argv[0]) if argv else None
        return stdout, None, USAGE_NOT_REQUESTED if host == HOST_CLAUDE else f"usage_unavailable:{host}"
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        return stdout, None, "unparsed"
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), str):
        return stdout, None, "unparsed"
    usage = envelope.get("usage")
    return envelope["result"], usage if isinstance(usage, dict) else None, None if isinstance(usage, dict) else "no_usage"


def build_launch_argv(
    host: str,
    model: str | None,
    *,
    workspace: "Path | str | None" = None,
    lean: bool = False,
    structured_output: bool = False,
) -> list[str]:
    """Assemble the launch argv for `host` WITHOUT the prompt.

    `structured_output` asks the Claude host for its JSON envelope (the reply
    under `result`, token counts under `usage`) — see `unwrap_structured`; the
    Cursor host has no usage in its output and ignores it.

    The prompt is never a member of this argv, on purpose: appended as a
    single argv string it can exceed Linux MAX_ARG_STRLEN (32 * PAGE_SIZE =
    131072 bytes), and execve then rejects the whole launch with E2BIG before
    the child even starts — the failure `agentctl.advisor`'s judge/enumerate
    calls hit on a large plan. A caller whose prompt can grow past that
    ceiling (a whole-plan judge/enumerate call) uses this function and
    delivers the prompt via the child's stdin instead, mirroring
    spawn-specialist.py's and spawn-cursor-specialist.py's own fix for the
    identical failure class (`claude -p` / `agent -p` both read the prompt
    from stdin when it is not given as a positional argv element).
    """
    binary = binary_for(host)
    if host == HOST_CLAUDE:
        if model is None:
            raise ValueError("model is required for HOST_CLAUDE")
        argv = [binary, "-p", "--model", model]
        if lean:
            argv += LEAN_ISOLATION_FLAGS
        if structured_output:
            argv += STRUCTURED_OUTPUT_FLAGS
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
    elif host == HOST_OPENCODE:
        from .opencode_env import AGENT_NAME

        argv = [binary, "run", "--standalone", "--auto", "--format", "json"]
        if model is not None:
            argv += ["--model", model]
        argv += ["--agent", AGENT_NAME]
    else:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")
    assert_same_family(binary, host)
    return argv


def build_prompt_argv(
    host: str,
    model: str | None,
    prompt: str,
    *,
    workspace: "Path | str | None" = None,
    lean: bool = False,
) -> list[str]:
    """`build_launch_argv` with the prompt appended as the trailing element.

    Kept, unchanged, for a caller whose prompt is bounded in practice
    (`lib.marker_extract`, extracting a return marker from an already-short
    specialist result). A caller whose prompt can grow past MAX_ARG_STRLEN
    must use `build_launch_argv` and deliver the prompt via the child's
    stdin instead — see `build_launch_argv`'s own docstring.
    """
    argv = build_launch_argv(host, model, workspace=workspace, lean=lean)
    argv.append(prompt)
    return argv


# --- the single-call seam -------------------------------------------------------------------------
#
# Every judge, extractor and enumerator call launches through `launch_single_call`. A site builds its
# argv for the host it was written for; `effective_host` swaps in OpenCode when the engine session
# bound to this process runs under it, so one function, not each site, decides which binary starts.

LEDGER_NAME = "claude-single-calls.jsonl"
OPENCODE_PROMPT_ARG_MAX_BYTES = 100000
OUTCOME_OK, OUTCOME_ERROR, OUTCOME_TIMEOUT = "ok", "error", "timeout"
OUTCOME_REFUSED, OUTCOME_SKIPPED = "refused", "skipped"
UNATTRIBUTED_SITE = "unattributed"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The facts of the launch-site registry (docs/operations/llm-prompt-sites.md § Single-call registry);
# the AST fixes which sites exist, this table fixes what is true of each. `consumer` is
# `hook:<script>:<event>` (denying by that script's integrations/opencode/parity.toml row),
# `engine:<what>` (the verdict refuses an engine transition, denying by R1) or `advisory:<what>`.
# `legacy_host` names a host the site's source hard-codes; under Cursor it keeps launching it.
# `exempt` is the reason a site is skipped under OpenCode. `timeout` names the source constant.
SINGLE_CALL_SITES: dict = {}


def _site(key, consumer, timeout, *, legacy_host=None, exempt=None):
    SINGLE_CALL_SITES[key] = {
        "consumer": consumer, "legacy_host": legacy_host, "exempt": exempt, "timeout": timeout,
    }


_ADV = "scripts/agentctl/advisor.py"
_TURN_END = "hook:hook-turn-end-gate.py:Stop"
_site(f"{_ADV}:enumerate_claims", "advisory:ledger-enumerate (agentctl/cli_session.py)", "ENUMERATE_TIMEOUT_S")
_site(f"{_ADV}:enumerate_subprocess_runner", "advisory:ledger-enumerate (agentctl/cli_session.py)", "ENUMERATE_TIMEOUT_S")
_site(f"{_ADV}:judge", "advisory:plan advisories (agentctl/cli_common.py)", "_ADVISOR_TIMEOUT_S")
_site(f"{_ADV}:acceptance_judge", "engine:stage result acceptance (agentctl/cli_results.py)", "_ACCEPTANCE_JUDGE_TIMEOUT_S")
_site(f"{_ADV}:judge_question_materiality", "engine:premise gate (agentctl/cli_questions.py)", "_QUESTION_MATERIALITY_TIMEOUT_S")
_site(f"{_ADV}:_judge_user_text", "advisory:no refusing caller (judge_user_question, judge_resolution_confirmation)", "_USER_QUESTION_TIMEOUT_S")
_site(f"{_ADV}:judge_approval_ask", "hook:hook-plan-delivery-gate.py:PreToolUse", "_APPROVAL_ASK_TIMEOUT_S")
_site(f"{_ADV}:judge_binary_ask", _TURN_END, "_BINARY_ASK_TIMEOUT_S")
_site(f"{_ADV}:judge_feedback_signal", _TURN_END, "_FEEDBACK_SIGNAL_TIMEOUT_S")
_site(f"{_ADV}:judge_silent_closure", _TURN_END, "_SILENT_CLOSURE_TIMEOUT_S")
_site(f"{_ADV}:judge_deferring_disposition", "hook:hook-deferring-disposition-gate.py:PreToolUse", "_DEFERRING_DISPOSITION_TIMEOUT_S")
_site(f"{_ADV}:judge_outage_escalation", "hook:hook-escalation-diagnosis-gate.py:PreToolUse", "_OUTAGE_ESCALATION_TIMEOUT_S")
_site(f"{_ADV}:judge_landing_discipline_ask", "hook:hook-resolution-reminder.py:PreToolUse", "_LANDING_DISCIPLINE_LAST_RESORT_TIMEOUT_S")
_site(f"{_ADV}:judge_published_attachment", "hook:hook-published-text-writer-gate.py:PreToolUse", "_PUBLISHED_ATTACHMENT_TIMEOUT_S")
_site(f"{_ADV}:judge_published_text_rules", "hook:hook-published-text-writer-gate.py:PreToolUse", "_PUBLISHED_TEXT_RULES_TIMEOUT_S")
_site(f"{_ADV}:judge_same_difficulty", "engine:new-leaf guard and semantic join (lib/semantic_join.py)", "_SAME_DIFFICULTY_TIMEOUT_S")
_site(f"{_ADV}:judge_code_effects", "engine:resource grant decision (agentctl/code_effects.py)", "code_effects_judge_timeout_s", legacy_host="claude")
_site("scripts/agentctl/gates.py:_semantic_invariants_coverage", "engine:plan lint (agentctl/gates.py)", "_ADVISOR_TIMEOUT_S", legacy_host="claude")
_site("scripts/agentctl/conditions.py:judge_restatement", "engine:plan submission (agentctl/submission.py)", "_JUDGE_TIMEOUT_S", legacy_host="claude")
_site("scripts/agentctl/procedure.py:judge_collapse", "engine:plan submission (agentctl/submission.py)", "_JUDGE_TIMEOUT_S", legacy_host="claude")
_site("scripts/agentctl/result_image.py:judge_echo", "engine:plan submission (agentctl/submission.py)", "_JUDGE_TIMEOUT_S", legacy_host="claude")
_site("scripts/agentctl/grounding_judge.py:_ask", "engine:grounding verdict (agentctl/grounding_judge.py)", "_JUDGE_TIMEOUT_S", legacy_host="claude")
_site("scripts/lib/marker_extract.py:extract", "engine:return marker extraction (lib/marker_extract.py)", "_EXTRACT_TIMEOUT_S")
_site("scripts/mandate_cycle/driver.py:real_judge", "engine:mandate cycle verdict (mandate_cycle/driver.py)", "JUDGE_TIMEOUT_S", legacy_host="claude")


def consumer_denies(consumer: str) -> bool:
    """The `denying` value a consumer implies. A hook is as denying as the event of its parity row;
    an engine transition refuses by R1; an advisory consumer never refuses."""
    kind, _, rest = consumer.partition(":")
    if kind == "engine":
        return True
    if kind != "hook":
        return False
    from . import hook_parity

    return hook_parity.is_denying(rest.rpartition(":")[2])


def site_denying(key) -> bool:
    entry = SINGLE_CALL_SITES.get(key)
    return bool(entry) and entry["exempt"] is None and consumer_denies(entry["consumer"])


def bound_host() -> "str | None":
    """`runtime_host` of the engine session this process belongs to, None when it has none."""
    from . import config_root, host_stamp

    session = host_stamp.task_key()
    path = config_root.resolve_agentctl_state_file(session) if session else None
    if path is None:
        return None
    try:
        host = json.loads(path.read_text(encoding="utf-8")).get("runtime_host")
    except (OSError, ValueError, AttributeError):
        return None
    return host if isinstance(host, str) else None


def effective_host(requested: str) -> str:
    """The host a single call launches on: OpenCode when the bound session runs under it, else the
    host the site asked for."""
    return HOST_OPENCODE if bound_host() == HOST_OPENCODE else requested


def _calling_site() -> "str | None":
    """The registry key of the innermost registered frame on the stack."""
    frame, depth = sys._getframe(2), 0
    while frame is not None and depth < 60:
        code = frame.f_code
        try:
            rel = Path(code.co_filename).resolve().relative_to(_REPO_ROOT).as_posix()
        except (ValueError, OSError):
            rel = None
        if rel:
            parts = code.co_qualname.replace(".<locals>.", ".").split(".")
            for end in range(len(parts), 0, -1):
                key = f"{rel}:{'.'.join(parts[:end])}"
                if key in SINGLE_CALL_SITES:
                    return key
        frame, depth = frame.f_back, depth + 1
    return None


def _model_of(argv) -> "list[str]":
    return [argv[i + 1] for i, element in enumerate(argv[:-1]) if element == "--model"][:1]


def _cost_usd(argv, stdout) -> "float | None":
    if argv[-len(STRUCTURED_OUTPUT_FLAGS):] != STRUCTURED_OUTPUT_FLAGS:
        return None
    try:
        cost = json.loads(stdout).get("total_cost_usd")
    except (TypeError, ValueError, AttributeError):
        return None
    return float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None


def _ledger_path() -> Path:
    return Path.home() / ".local" / "log" / LEDGER_NAME


def _write_ledger(site, host, argv, outcome, *, reason=None, cost_usd=None) -> None:
    from . import host_stamp

    row = host_stamp.stamp(
        host, os.environ.get(host_stamp.HARNESS_VERSION_ENV) or None, _model_of(argv),
        host_stamp.engine_session_binding.harness_session_id(os.environ) or None,
    )
    row.update({
        "ts": time.time(), "site": site or UNATTRIBUTED_SITE, "session": host_stamp.task_key(),
        "outcome": outcome, "denying": site_denying(site), "cost_usd": cost_usd,
    })
    if reason is not None:
        row["reason"] = reason
    try:
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


class SingleCallRefused(Exception):
    """A site whose verdict refuses an engine or hook step could not launch under the bound host."""

    def __init__(self, site, code, detail):
        super().__init__(f"{site}: {code}: {detail}")
        self.code, self.detail = code, detail


def exempt_skip(site) -> bool:
    """True, with a `skipped` row, when `site` is registered exempt and the bound host is OpenCode."""
    entry = SINGLE_CALL_SITES.get(site)
    if not entry or not entry["exempt"] or bound_host() != HOST_OPENCODE:
        return False
    _write_ledger(site, HOST_OPENCODE, [], OUTCOME_SKIPPED,
                  reason={"code": "exempt", "detail": entry["exempt"]})
    return True


def _opencode_delivery(argv, stdin):
    """(argv, stdin) with the prompt where OpenCode takes it: positional while it is short and does
    not look like an option, standard input otherwise."""
    from .opencode_env import AGENT_NAME

    if argv[-2:] == ["--agent", AGENT_NAME]:
        argv, prompt = list(argv), stdin or ""
    else:
        argv, prompt = list(argv[:-1]), argv[-1]
    if not prompt:
        return argv, stdin
    if len(prompt.encode("utf-8")) <= OPENCODE_PROMPT_ARG_MAX_BYTES and not prompt.startswith("-"):
        return argv + [prompt], ""
    return argv, prompt


def launch_single_call(argv, *, stdin=None, timeout) -> subprocess.CompletedProcess:
    """Run one single-turn model call and write its ledger row. The result carries the `run_kwargs`
    the child got. A timeout writes its row and raises `subprocess.TimeoutExpired`; a denying site
    that cannot launch under OpenCode raises `SingleCallRefused`, any other site gets a failed
    result (returncode 127), which its own fail-open default reads."""
    host = host_of_binary(argv[0])
    site = _calling_site()
    if host == HOST_OPENCODE:
        from . import opencode_env

        problem = opencode_env.not_ready()
        if problem:
            code, detail = problem
            denying = site_denying(site)
            _write_ledger(site, host, argv, OUTCOME_REFUSED if denying else OUTCOME_ERROR,
                          reason={"code": code, "detail": detail})
            if denying:
                raise SingleCallRefused(site, code, detail)
            failed = subprocess.CompletedProcess(argv, 127, "", detail)
            failed.run_kwargs = {}
            return failed
        argv, stdin = _opencode_delivery(argv, stdin)
        run_kwargs = opencode_env.run_kwargs()
        run_kwargs["env"]["AGENTCTL_RUNTIME_HOST"] = HOST_OPENCODE
    else:
        run_kwargs = isolated_run_kwargs()
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                              **run_kwargs)
    except subprocess.TimeoutExpired:
        _write_ledger(site, host, argv, OUTCOME_TIMEOUT)
        raise
    _write_ledger(site, host, argv, OUTCOME_OK if proc.returncode == 0 else OUTCOME_ERROR,
                  cost_usd=_cost_usd(argv, proc.stdout))
    proc.run_kwargs = run_kwargs
    return proc
