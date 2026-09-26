"""G-target predicates: paths and programs a stage grant can never cover.

Difficulty removed: `agentctl/grants.py`'s validator needs one shared,
structural answer to "is this a widening of the agent's own permission
surface" so a grant that would let a spawned child rewrite its own
permissions, launch surface, or coordination-authority verbs can be refused
by SHAPE rather than by a list of literal strings scattered across the
validator, the guard hook, and a probe script. This module is that shape —
predicates only, no policy about what to DO with a match (that is the
validator's job).

Every predicate here fails toward TREATING SOMETHING AS A G-TARGET on
ambiguity — the caller (grants.validate_grants) refuses on True, so the
asymmetric cost of a false positive (a legitimate grant refused, surfaced to
the user as a plan-authoring error) is far cheaper than a false negative (a
widening grant silently accepted).
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from . import config_root

# --- settings documents -----------------------------------------------------

_SETTINGS_BASENAME_RE = re.compile(r"^settings[^/]*\.json$", re.IGNORECASE)


def _norm(path: str) -> str:
    """Expanduser + `..`/`.`-collapsing + POSIX-slash normalization, without
    resolving symlinks or requiring existence — a grant-form path is a string
    in a TOML file, not yet a filesystem entry. `os.path.normpath` collapses
    a `..` segment textually (e.g. `~/.claude/../../etc` -> `/etc`) so an
    escaping relative component cannot dodge a protected-root containment
    check by riding along uncollapsed."""
    return str(PurePosixPath(os.path.normpath(str(Path(path).expanduser()))))


def is_live_settings(path: str) -> bool:
    """True iff `path` names a settings document the harness or the system
    actually loads from: `$CLAUDE_AGENT_HOME/settings*.json`,
    `~/.claude/settings*.json`, or `.../.claude/settings*.json` at any depth
    (a project-local settings file). Broader than `enumerate_live_settings`'s
    four concrete locations on purpose — this predicate exists to REFUSE a
    grant, so it must catch every settings document a plan could name, not
    only the ones this machine happens to have today."""
    if not isinstance(path, str) or not path.strip():
        return False
    norm = _norm(path)
    basename = norm.rsplit("/", 1)[-1]
    if not _SETTINGS_BASENAME_RE.match(basename):
        return False
    # Case-insensitive-filesystem variants (finding S2): the directory-component
    # and prefix comparisons below casefold both sides so a `.Claude`/`.CLAUDE`
    # spelling is caught the same as the lowercase form.
    parts_cf = [p.casefold() for p in norm.split("/")[:-1]]
    if ".claude" in parts_cf or ".claude-agent" in parts_cf:
        return True
    norm_cf = norm.casefold()
    agent_home_cf = _norm(str(config_root.agent_home())).casefold()
    if norm_cf.startswith(agent_home_cf + "/") or norm_cf == agent_home_cf:
        return True
    return False


def enumerate_live_settings(child_cwd: str | None, root_cwd: str | None) -> list[str]:
    """The existing settings*.json documents among exactly the FOUR named
    locations a live harness could actually be loading from: the agent
    home's own settings, the personal `~/.claude` settings, and a
    project-local `.claude/settings*.json` under each of `child_cwd` and
    `root_cwd`. Deliberately does NOT walk the filesystem for other
    `.claude` directories (e.g. a sibling checkout) — this is a drift
    record of the locations THIS dispatch could plausibly affect, not a
    system-wide settings audit. Sorted, deduplicated by resolved path."""
    dirs: list[Path] = [config_root.agent_home(), config_root.harness_config_root()]
    for cwd in (child_cwd, root_cwd):
        if cwd:
            dirs.append(Path(cwd) / ".claude")
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for doc in sorted(d.glob("settings*.json")):
            try:
                key = str(doc.resolve())
            except OSError:  # pragma: no cover - unresolvable path
                key = str(doc)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return sorted(out)


# --- state dir / launch surfaces / protected roots --------------------------


def is_agentctl_state_path(path: str) -> bool:
    """True iff `path` is under agentctl's own state directory — a grant that
    could Edit/write there could forge a stage outcome or gate record."""
    if not isinstance(path, str) or not path.strip():
        return False
    norm = _norm(path)
    state_dir = _norm(str(config_root.agentctl_state_dir()))
    agentctl_dir = _norm(str(config_root.agentctl_dir()))
    return norm == state_dir or norm.startswith(state_dir + "/") or \
        norm == agentctl_dir or norm.startswith(agentctl_dir + "/")


_LAUNCH_SURFACE_SEGMENTS = (
    "Library/LaunchAgents",
    "Library/LaunchDaemons",
    ".config/systemd/user",
)


def is_launch_surface(path: str) -> bool:
    """True iff `path` is under a persistent-launch-registration surface: a
    macOS LaunchAgent/LaunchDaemon directory or a systemd user-unit
    directory. A grant reaching here could make a spawned child install
    something that runs again after this session ends."""
    if not isinstance(path, str) or not path.strip():
        return False
    norm = _norm(path)
    home_norm = _norm(str(Path.home()))
    for seg in _LAUNCH_SURFACE_SEGMENTS:
        target = _norm(f"{home_norm}/{seg}")
        if norm == target or norm.startswith(target + "/"):
            return True
    return False


def add_dir_is_or_contains_launch_surface(path: str) -> bool:
    """True iff an add_dir grant of `path` is under a launch surface
    (`is_launch_surface`'s own direction) OR IS/CONTAINS one — an ancestor
    of `~/Library/LaunchAgents` etc. — mirroring
    `add_dir_is_or_contains_protected_root`'s both-directions shape: a
    write add_dir at either end of that relationship hands a spawned child
    a path from which a launch-surface file is reachable via `Edit`."""
    if not isinstance(path, str) or not path.strip():
        return False
    if is_launch_surface(path):
        return True
    norm = _norm(path)
    prefix = norm if norm == "/" else norm + "/"
    home_norm = _norm(str(Path.home()))
    for seg in _LAUNCH_SURFACE_SEGMENTS:
        target = _norm(f"{home_norm}/{seg}")
        if target.startswith(prefix):
            return True
    return False


def is_crontab_target(command: str) -> bool:
    """True iff `command` invokes `crontab` — the third launch surface,
    named by program rather than by path (crontab has no file target a
    plan-authored rule could point at)."""
    if not isinstance(command, str):
        return False
    return re.search(r"(^|[/\s])crontab(\s|$)", command) is not None


_PROTECTED_ROOTS_ENV_RELATIVE = ("~",)


def protected_roots() -> list[str]:
    """The roots an add_dir grant may never be, or contain: `$HOME`,
    `$CLAUDE_AGENT_HOME`, `~/.claude`, and the agentctl state dir. Returned
    as normalized absolute path strings.

    `~/.claude` is named explicitly (finding S1), not only via
    `harness_config_root()`: on an isolated machine (`CLAUDE_CONFIG_DIR` set
    to `~/.claude-agent`) that accessor resolves away from `~/.claude`
    entirely, yet `~/.claude` commonly still exists as the legacy/personal
    root and must stay protected regardless of which root the running
    harness happens to be reading from."""
    return [
        _norm(str(Path.home())),
        _norm(str(Path.home() / ".claude")),
        _norm(str(config_root.agent_home())),
        _norm(str(config_root.harness_config_root())),
        _norm(str(config_root.agentctl_state_dir())),
    ]


def add_dir_is_or_contains_protected_root(path: str) -> bool:
    """True iff an add_dir grant of `path` IS a protected root, or CONTAINS
    one (an ancestor of a protected root) — either direction hands a
    spawned child write access to agent-home-level state. Does not itself
    check the opposite containment (`path` UNDER a protected root); that is
    `add_dir_under_protected_root` below, checked separately because the
    validator treats "under ~/.claude" as always-refused regardless of mode
    while "contains a protected root" is refused for a WRITE add_dir."""
    if not isinstance(path, str) or not path.strip():
        return False
    norm = _norm(path)
    # `norm` is always POSIX-absolute (starts with "/"); the ancestor prefix
    # is `norm` itself only when norm == "/" (its own trailing slash), else
    # `norm + "/"` — using `norm + "/"` unconditionally breaks at the
    # filesystem root, since "//" is not a prefix of any real path, silently
    # letting an add_dir of "/" (which contains every protected root) pass.
    # Both sides are casefolded (finding S2) so a case-insensitive-filesystem
    # variant (`.Claude`, `.GIT`) matches the same as the lowercase form.
    norm_cf = norm.casefold()
    prefix_cf = norm_cf if norm_cf == "/" else norm_cf + "/"
    for root in protected_roots():
        root_cf = root.casefold()
        if norm_cf == root_cf or root_cf.startswith(prefix_cf):
            return True
    return False


def add_dir_under_protected_root(path: str) -> bool:
    """True iff `path` is under `~/.claude` or the agentctl state dir
    specifically — refused in EITHER add_dir mode (read or write), unlike
    the broader protected-root containment check above which only bites a
    write add_dir."""
    if not isinstance(path, str) or not path.strip():
        return False
    norm_cf = _norm(path).casefold()
    for root in (
        _norm(str(Path.home() / ".claude")),
        _norm(str(config_root.harness_config_root())),
        _norm(str(config_root.agentctl_state_dir())),
    ):
        root_cf = root.casefold()
        if norm_cf == root_cf or norm_cf.startswith(root_cf + "/"):
            return True
    return False


# --- claude / agentctl program detection ------------------------------------

_WRAPPER_TOKENS = frozenset({
    "env", "npx", "exec", "nohup", "timeout", "command",
    "sudo", "doas", "xargs", "eval", "time", "nice", "stdbuf",
    "setsid", "ionice", "chrt", "taskset", "unbuffer", "flock",
})
INTERPRETER_RE = re.compile(r"^python[0-9.]*$")

# agentctl subcommands that exercise USER authority — approving a plan,
# resolving a permission request, closing out a session, etc. A grant that
# lets a spawned child invoke any of these bypasses the coordination spine
# entirely: the child could self-approve its own plan or self-grant its own
# permission request. Read-only introspection verbs (status, plan-render,
# stage-grants, plan-grants, grant-stats, question-list, question-check,
# order-list) are deliberately NOT in this set.
AGENTCTL_USER_AUTHORITY_VERBS = frozenset({
    "start", "reset", "approve", "resolve-permission", "resolve", "reject",
    "accept", "risk-accept", "plan-review", "code-review", "stage-review",
    "confirm-delivery", "present-plan", "submit-plan", "dispatch",
    "record-result", "verify-final", "replan", "close", "fire-acknowledge",
    "block", "unblock", "drive", "push-subplan", "pop-subplan", "task-reset",
    "declare", "investigate", "critique", "normalize", "partition",
    "partition-units", "next-stage", "plugin-activate", "plugin-deactivate",
    "plugin-record",
})


# Wrapper -> the set of its own short flags that consume a SEPARATE following
# token as their value (as opposed to a boolean flag, or a flag whose value is
# conventionally attached with no space, e.g. `-o0`). Finding B1: the prior
# form stopped at the wrapper token itself and left the flag's own value
# token (`-n 5`'s `5`, `-u root`'s `root`) as the apparent "program name",
# so a rule like `Bash(nice -n:*)` validated the flag token `-n` against
# every downstream check (find/write-capable/interpreter/...) instead of the
# real program the wildcard could still admit — none of those checks fire on
# a bare flag, so the rule was silently accepted. Consuming the flag AND its
# value here makes such a rule reduce to a bare wrapper with no operand,
# which the caller (`grants.validate_rule`) already refuses outright.
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-p", "-r", "-h"}),
    "doas": frozenset({"-u"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p", "-t"}),
    "chrt": frozenset({"-p"}),
    "taskset": frozenset({"-c", "-p"}),
    "time": frozenset({"-o", "-f"}),
    "xargs": frozenset({"-n", "-P", "-I", "-a", "-d", "-E", "-L", "-s", "-x"}),
    "flock": frozenset({"-w"}),
    "unbuffer": frozenset({"-p"}),
}


def strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop a leading run of wrapper tokens (`env FOO=bar`, `npx`, `exec`,
    `nohup`, `timeout 30`, `command`, `sudo -u x`, `nice -n 5`, ...) so the
    real program name surfaces. `env` consumes any leading `KEY=VALUE`
    assignments and a `-i`/`-u NAME` flag form; `timeout` consumes its
    duration operand; every other wrapper in `_WRAPPER_TOKENS` consumes its
    own known separate-value flags (`_WRAPPER_VALUE_FLAGS`) together with
    their value token, and any other `-`-prefixed token as a boolean/attached
    flag, stopping at the first non-flag token (the real program)."""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "env":
            i += 1
            while i < n and ("=" in tokens[i] or tokens[i].startswith("-")):
                if tokens[i].startswith("-u") and tokens[i] == "-u":
                    i += 1  # consume the flag's separate NAME operand too
                i += 1
            continue
        if tok == "timeout":
            i += 1
            while i < n and (tokens[i].startswith("-") or re.match(r"^[0-9.]+[smhd]?$", tokens[i])):
                i += 1
            continue
        if tok in _WRAPPER_TOKENS:
            value_flags = _WRAPPER_VALUE_FLAGS.get(tok, frozenset())
            i += 1
            while i < n and tokens[i].startswith("-"):
                if tokens[i] in value_flags:
                    i += 2  # consume the flag AND its separate value token
                else:
                    i += 1  # boolean flag, or a value attached with no space
            continue
        break
    return tokens[i:]


def program_name(token: str) -> str:
    """The basename of a program token, stripping a leading absolute/relative
    path so `/usr/bin/python3`, `./scripts/agentctl-cli.py`, and `python3`
    are all recognized the same way."""
    return token.rsplit("/", 1)[-1]


_CLAUDE_PROGRAM_NAMES = frozenset({"claude", "claude-code"})


def is_claude_program(tokens: list[str]) -> bool:
    """True iff, after stripping wrapper tokens, the leading program token is
    `claude` in any spelling (bare, absolute path, a `claude-code` alias, an
    npm-scoped `@anthropic-ai/claude-code` package name — whose basename per
    `program_name` is `claude-code` — or via a wrapper; finding B1).

    A wrapper's own options (`nice -n 5`, `sudo -u x`, `stdbuf -o0`) are not
    parsed per wrapper, so once any wrapper was stripped, `claude`/`claude-code`
    anywhere in the remaining tokens counts: fail toward refused."""
    stripped = strip_wrappers(tokens)
    if not stripped:
        return False
    if program_name(stripped[0]) in _CLAUDE_PROGRAM_NAMES:
        return True
    wrapped = len(stripped) < len(tokens)
    return wrapped and any(program_name(t) in _CLAUDE_PROGRAM_NAMES for t in stripped)


def agentctl_invocation_verb(tokens: list[str]) -> tuple[bool, str | None]:
    """`(invokes_agentctl, verb)` — `invokes_agentctl` is True iff `tokens`
    (after stripping wrapper tokens) invokes agentctl in either spelling this
    repo supports (`python3 -m agentctl <verb>`, or an entry-point script
    `.../agentctl-cli.py <verb>`); `verb` is the token immediately following,
    or `None` when no such token is present at all (a BARE invocation naming
    no verb, e.g. `python3 -m agentctl` or `agentctl-cli.py` alone).

    `invokes_agentctl is True and verb is None` matters on its own: a
    wildcarded rule built from such a bare invocation (`Bash(python3 -m
    agentctl:*)`) covers EVERY verb at materialization time, including a
    user-authority one, even though no single verb token is present in the
    rule text for `agentctl_user_authority_call` below to match against.

    The interpreter check matches any `python[0-9.]*` name (including a
    venv-path interpreter like `/home/x/.venv/bin/python3`), not only the
    literal `python3` spelling, so a grant cannot dodge this refusal by
    naming a differently-versioned or venv-relative interpreter."""
    stripped = strip_wrappers(tokens)
    if not stripped:
        return False, None
    prog = program_name(stripped[0])
    rest = stripped[1:]
    if INTERPRETER_RE.match(prog):
        if len(rest) >= 1 and rest[0] == "-m" and len(rest) >= 2 and rest[1] == "agentctl":
            return True, (rest[2] if len(rest) >= 3 else None)
        if rest and program_name(rest[0]).endswith("agentctl-cli.py"):
            return True, (rest[1] if len(rest) >= 2 else None)
        return False, None
    if prog.endswith("agentctl-cli.py"):
        return True, (rest[0] if rest else None)
    return False, None


def agentctl_user_authority_call(tokens: list[str]) -> str | None:
    """If `tokens` invokes agentctl (see `agentctl_invocation_verb`) and
    names a user-authority verb, returns that verb; else `None`. Deliberately
    does NOT flag a bare, verb-less agentctl invocation — that is
    `agentctl_invocation_verb`'s own `(True, None)` case, checked separately
    by `grants.validate_rule` against a rule's wildcard suffix, since a bare
    invocation names no verb for THIS function to recognize as unsafe."""
    invokes, verb = agentctl_invocation_verb(tokens)
    if not invokes:
        return None
    if verb in AGENTCTL_USER_AUTHORITY_VERBS:
        return verb
    return None


# --- settings-channel programs -----------------------------------------------

# Programs whose entire purpose is to write or install into a live settings
# surface. Refused in EVERY invocation form (bare, absolute path, via a
# wrapper) regardless of arguments — there is no safe argument subset for a
# program whose job is to mutate the harness's own permission/hook wiring.
# `scripts/tests/test_stage_grants.py::SETTINGS_REFERENCE_CLASSIFICATION`
# is the committed, self-testing classification of every script this repo's
# own settings-file references touch, so a newly added settings-writing
# script fails that test until it is added here too.
SETTINGS_CHANNEL_PROGRAMS = frozenset({
    "apply-settings.sh",
    "apply-mcp-local.sh",
    "install-reminder-hooks.sh",
    "set-context-cap.sh",
    "migrate-to-isolated.sh",
    "setup-symlinks.sh",
    "sync-instructions-repo.sh",
})


def is_settings_channel_program(tokens: list[str]) -> bool:
    """True iff, after stripping wrapper tokens, the leading program token's
    basename is a known settings-channel program — matched regardless of an
    interpreter prefix (`bash apply-settings.sh`), a relative or absolute
    path, or further arguments."""
    stripped = strip_wrappers(tokens)
    if not stripped:
        return False
    prog = program_name(stripped[0])
    if prog in SETTINGS_CHANNEL_PROGRAMS:
        return True
    # `bash <script>` / `sh <script>`: the interpreter is the leading token,
    # the script is the next one.
    if prog in ("bash", "sh") and len(stripped) >= 2:
        return program_name(stripped[1]) in SETTINGS_CHANNEL_PROGRAMS
    if INTERPRETER_RE.match(prog) and len(stripped) >= 2:
        return program_name(stripped[1]) in SETTINGS_CHANNEL_PROGRAMS
    return False
