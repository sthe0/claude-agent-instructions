"""PreToolUse hook: answer a spawned child's tool call from the resources its stage was approved to change.

Difficulty removed: a spawned child runs headless, so a Bash call whose effect the plan approved but whose
spelling the child's allow list does not match (`cd X && git commit`, `python3 -c` writing the venue, a
`git -C` spelled with a different prefix) stops at an approval prompt nobody can answer, and the child
reports a permission stop for work the plan already sanctioned. The spawn wrapper writes the stage's typed
resources, its wildcard-tail rules and the child's final `permissions.allow` into the norm-handoff record
(`AGENTCTL_NORM_HANDOFF`); this hook resolves each segment of the call (`tool_contracts.resolve_segments`,
opaque code through `code_effects.effects_of`) and compares what it changes to that record.

Active only in a linked child: `AGENT_RECURSION_DEPTH >= 1` and a handoff record whose bytes match the
recorded sha256. Anything else, the kill switch included (config row `resource-grant-hook`,
`AGENTCTL_RESOURCE_HOOK`), makes no decision.

The ALLOW path is strict and the DENY path is narrow, and the two are not mirror images. An allow needs every
segment positively covered; a deny needs a positive case that the harness would stop the call.

Decisions (Bash):
  allow  every segment is covered: its changed resources lie within the record's resources, or the segment
         matches a record rule pattern (strictly, per segment); a segment that changes nothing must also read
         only inside the record's resources or the parent scratchpad. A segment that reads an environment
         variable the hook cannot check against the Bash shell (CDPATH, GIT_*, PYTHON*) is not covered. A line
         that runs git (a git segment of any state, or a segment whose effects include a ref) is allowed only
         while the live digest of what git would execute in that repository (`agentctl.git_surface`: hooks and
         their symlink targets, config entries naming programs, attributes, hook-framework files) equals the
         snapshot the spawn took into the record, and no other segment of the line could change it (opaque,
         rule-pattern or harness-allowed segments that are not read-only, and covered writes onto a pinned
         path). A record without a snapshot, a repository missing from it, or a surface that cannot be read
         is undeterminable, never allowed.
  deny   ALL of: the permission mode is acceptEdits (the only mode probe Q6 measured); the record carries
         `permissions_allow` and `add_dirs` explicitly; the settings files the harness merges with the record
         (user, config root, managed and the project's `.claude/settings[.local].json`) are all readable and
         no `permissions.allow` entry in the record or in them matches the call even leniently
         (`permission_entry_match.covers` on the whole call); some segment changes a resource the record
         does not cover that is outside the child's cwd and every add-dir (the record's and the settings'),
         or is not a file. The reason carries a `PERMISSION-REQUEST:` Rule:/Resource: template.
  none   everything else: plan mode, an unresolved or undeterminable segment, a harness-allowed call, a record
         without the explicit lists, opaque code that writes nothing, an error. The harness then decides
         exactly as it would without the hook.
Edit, Write and NotebookEdit: allow when the file is covered, never deny. Read: allow only inside the
record's resources or the parent scratchpad. A deny from another hook or a settings deny rule still wins
(probe note Q3/Q4), so this hook cannot widen past an existing guard.

Every call appends one row to `<state_root>/resource-uses/<parent_session>-<stage>.jsonl`.

Residuals that can still yield an ALLOW, named in docs/operations/resource-grant-hook.md: the hook sees its
own process environment, not the Bash tool's shell snapshot, so a GIT_* or PYTHON* value set only there is
invisible; a hook or filter that runs a file of the worktree by path runs whatever the child wrote there; the
parent scratchpad is recognised by path layout.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import advisor, code_effects, git_surface, resources, segment_resolver, tool_contracts  # noqa: E402
from agentctl import config as agentctl_config  # noqa: E402
from agentctl.norm_handoff import NORM_HANDOFF_ENV, state_root_of, verify_env_value  # noqa: E402
from agentctl.store import safe_session_id  # noqa: E402
from lib import config_root, permission_entry_match  # noqa: E402
from lib.host_llm import JUDGE_CHILD_ENV_VAR  # noqa: E402

RECURSION_ENV = "AGENT_RECURSION_DEPTH"
USES_DIRNAME = "resource-uses"
UNLINKED_LOG = "_unlinked"

BASH_TOOLS = ("Bash",)
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
READ_TOOLS = {"Read": "file_path"}

ACCEPT_EDITS = "acceptEdits"
PLAN_MODE = "plan"
#: Filesystem programs measured as run by acceptEdits inside the child's cwd (probe note Q6) that the
#: resolver resolves.
ACCEPT_EDITS_PROGRAMS = frozenset({"mkdir", "touch", "cp", "mv"})
#: Exported variables that move where git reads or writes, or which config it obeys. A segment that runs
#: git while one is set is not covered: the Bash shell's value may differ from the one this process sees.
GIT_ENV_EXACT = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_EXEC_PATH", "GIT_REPLACE_REF_BASE",
})
GIT_ENV_PREFIX = "GIT_CONFIG"
#: `PYTHON*` variables that change nothing about which code runs or where it writes.
PYTHON_ENV_HARMLESS = frozenset({
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
    "PYTHONWARNINGS", "PYTHONNOUSERSITE",
})
#: Programs that cannot reach git or python from their own arguments: every other program may start one,
#: so with a GIT_* or PYTHON* blocker exported it is not covered (a closed list, not a list of carriers).
NON_CARRIER_PROGRAMS = frozenset({
    "cd", "ls", "cat", "head", "tail", "wc", "mkdir", "touch", "cp", "mv", "rm", "echo", "printf", "pwd",
    "true", "false", "test", "[", "stat", "file", "diff", "pushd", "popd",
})
#: Programs that write nothing, however they are called (a redirect is a separate resolved effect).
READ_ONLY_PROGRAMS = frozenset({
    "cd", "ls", "cat", "head", "tail", "wc", "echo", "printf", "pwd", "true", "false", "test", "[", "stat",
    "file", "diff", "pushd", "popd",
})
#: git subcommands that leave the worktree's files alone; the others (checkout, merge, reset, ...) rewrite
#: tracked files, and so possibly a tracked hook.
WORKTREE_PRESERVING_GIT = frozenset({
    "status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "branch", "push", "add", "commit",
    "fetch", "tag", "remote",
})
GIT_PROBE_TIMEOUT_S = 5
#: Settings files the harness merges into the child's permissions besides the record.
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
SETTINGS_NAMES = ("settings.json", "settings.local.json")
#: A segment containing one of these is not matched against a rule: a redirect, a substitution or a
#: further command hides an effect the rule text does not name.
RULE_UNSAFE_CHARS = frozenset("<>`$\n\r;&|")
GLOB_CHARS = frozenset("*?[")

ALLOW, DENY, NONE = "allow", "deny", "none"
COVERED, UNCOVERED, UNDETERMINABLE = "covered", "uncovered", "undeterminable"
HARNESS = "harness"


@dataclass
class Approved:
    """What the handoff record sanctions, parsed once per call."""

    resources: list
    rule_patterns: list
    #: None when the record does not say; an empty list is a stated "no entries".
    allow_entries: "list | None"
    parent_session: str
    stage_index: int
    cwd: str
    mode: str
    protected: list = field(default_factory=list)
    #: None when the record does not say (written before the keys existed): never a reason to deny.
    add_dirs: "list | None" = None
    env: dict = field(default_factory=dict)
    #: The directory the child was started in, where the harness reads the project's settings from. The
    #: payload cwd for a record written before the key existed.
    launch_dir: str = ""
    #: `git_surface.snapshot_venues` as the spawn recorded it; None for a record without one.
    git_surface: "dict | None" = None
    #: The stage's declared `runs_worktree_code`: the executor said it runs code its worktree holds.
    runs_worktree_code: bool = False


@dataclass
class SegmentVerdict:
    text: str
    state: str
    changed: list = field(default_factory=list)
    uncovered: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    opaque: bool = False
    #: Covered by text match against a record rule pattern: what the segment writes was never resolved.
    pattern: bool = False
    reason: str = ""
    venue: str = ""
    #: The directory git would run in for this segment (its cwd moved by every `-C`), None when unknown.
    git_dir: "str | None" = None


@dataclass
class Verdict:
    decision: str
    reason_class: str
    resources: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    message: str = ""
    venue: str = ""


# --- activation -----------------------------------------------------------------------------------

def _depth(env) -> int:
    try:
        return int(env.get(RECURSION_ENV, "0"))
    except ValueError:
        return 0


def _real(path: str) -> str:
    return os.path.realpath(path)


def _tool_arg(tool_name: str, tool_input: dict) -> str:
    """The one input that names what the call acts on: the command, or the file path."""
    if tool_name in BASH_TOOLS:
        return str(tool_input.get("command") or "")
    key = EDIT_TOOLS.get(tool_name) or READ_TOOLS.get(tool_name)
    return str(tool_input.get(key) or "") if key else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --- the log --------------------------------------------------------------------------------------

def _log_path(state_root: Path, link: "dict | None") -> Path:
    directory = state_root / USES_DIRNAME
    if link is None:
        return directory / f"{UNLINKED_LOG}.jsonl"
    return directory / f"{safe_session_id(link['parent_session'])}-{link['stage_index']}.jsonl"


def append_row(path: Path, row: dict) -> None:
    """Best effort: a log that cannot be written never changes the decision."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _row(tool: str, text: str, verdict: Verdict) -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "call_sha": _sha(text),
        "decision": verdict.decision,
        "reason_class": verdict.reason_class,
        "resources": verdict.resources,
        "venue": verdict.venue,
        "matched": verdict.matched,
    }


# --- matching -------------------------------------------------------------------------------------

def _tokens(text: str) -> "list[str] | None":
    try:
        return shlex.split(text)
    except ValueError:
        return None


def rule_matches(entry: str, text: str) -> bool:
    """Strict reading of a `Bash(...)` rule against ONE segment: an exact spec, or a `prefix:*` spec on a
    whole-word boundary. Unlike `permission_entry_match.covers` it never errs toward covering: a segment
    with a redirect, a substitution or an unparsable quote matches nothing."""
    if not (entry.startswith("Bash(") and entry.endswith(")")):
        return False
    if any(ch in RULE_UNSAFE_CHARS for ch in text):
        return False
    spec = entry[len("Bash("):-1]
    tokens = _tokens(text)
    if not tokens:
        return False
    operand = " ".join(tokens)
    if spec.endswith(":*"):
        prefix_tokens = _tokens(spec[:-2])
        if not prefix_tokens:
            return False
        prefix = " ".join(prefix_tokens)
        return operand == prefix or operand.startswith(prefix + " ")
    spec_tokens = _tokens(spec)
    return bool(spec_tokens) and operand == " ".join(spec_tokens)


def _first_rule(entries: list, text: str) -> "str | None":
    return next((entry for entry in entries if rule_matches(entry, text)), None)


def _is_change(resource) -> bool:
    if isinstance(resource, resources.FileResource):
        return resource.mode == "write"
    if isinstance(resource, resources.ServiceResource):
        return resource.op_class != "read"
    return True


def _describe(resource) -> str:
    if isinstance(resource, resources.FileResource):
        return f"file {resource.mode} {resource.path}"
    if isinstance(resource, resources.VcsRefResource):
        return f"vcs_ref {resource.op} {resource.remote}:{resource.ref}"
    return json.dumps(resources.resource_to_dict(resource), sort_keys=True)


def covered_by(approved: Approved, resource) -> bool:
    return any(item.covers(resource, protected=approved.protected) for item in approved.resources)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def scratch_roots() -> set:
    return {_real(tempfile.gettempdir()), _real("/tmp")}


def parent_scratchpad_path(path: str, parent_session: str) -> bool:
    """True when `path` (already absolute) lies in the parent session's scratchpad: an OS scratch root
    holding `<...>/<parent session id>/scratchpad/...`. The layout is the harness's, not a field of the
    record, so the check is on the realpath: a symlink out of the scratchpad resolves to where it points."""
    real = _real(path)
    parts = Path(real).parts
    roots = scratch_roots()
    if not any(_under(real, root) for root in roots):
        return False
    return any(parts[i] == parent_session and parts[i + 1] == "scratchpad" for i in range(len(parts) - 1))


def readable(approved: Approved, path: str) -> "str | None":
    """What allows reading `path`: a record resource (a write covers a read) or the parent scratchpad."""
    if not os.path.isabs(path):
        return None
    if covered_by(approved, resources.FileResource(path, "read")):
        return "record-resource"
    if parent_scratchpad_path(path, approved.parent_session):
        return "parent-scratchpad"
    return None


def _operand_paths(tokens: list, venue: str) -> "list[str] | None":
    """Absolute paths the operands of a read-only program name, or None when an operand cannot be pinned
    down (a glob, `~`, a variable)."""
    out: list = []
    for tok in tokens[1:]:
        candidate = tok
        if tok.startswith("-"):
            if "=" not in tok:
                continue
            candidate = tok.split("=", 1)[1]
        if candidate.startswith("~") or "$" in candidate or any(ch in candidate for ch in GLOB_CHARS):
            return None
        pathlike = "/" in candidate or candidate.startswith(".") or os.path.lexists(os.path.join(venue, candidate))
        if not pathlike:
            continue
        out.append(candidate if os.path.isabs(candidate) else os.path.join(venue, candidate))
    return out


def reads_confined(approved: Approved, text: str, venue: str) -> "tuple[bool, list]":
    """Whether every path a segment's operands name may be read. git is exempt: its operands are
    repo-relative pathspecs the resolver already models (`-C` included)."""
    tokens = _tokens(text)
    if not tokens:
        return False, []
    if os.path.basename(tokens[0]) == "git":
        return True, []
    paths = _operand_paths(tokens, venue)
    if paths is None:
        return False, []
    kinds = []
    for path in paths:
        kind = readable(approved, path)
        if kind is None:
            return False, []
        kinds.append(kind)
    return True, sorted(set(kinds))


# --- harness-allowed ------------------------------------------------------------------------------

def segment_allow_entry(approved: Approved, text: str) -> "str | None":
    """The entry of the child's `permissions.allow` that covers THIS segment. Per segment on purpose: the
    harness's own whole-call match errs toward covering when any one segment matches, which would let an
    allowed prefix shelter an uncovered tail."""
    return next((e for e in (approved.allow_entries or []) if e in ("Bash", "Bash(*)") or rule_matches(e, text)), None)


@dataclass
class Ambient:
    """What the harness merges into the child's permissions besides the record: `permissions.allow` and
    `permissions.additionalDirectories` of every settings file it reads."""

    allow: list = field(default_factory=list)
    add_dirs: list = field(default_factory=list)


def _settings_files(approved: Approved) -> list:
    env = approved.env
    home = Path(env.get("HOME") or Path.home())
    roots = [home / ".claude"]
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME"):
        if env.get(name):
            roots.append(Path(env[name]).expanduser())
    launch = approved.launch_dir or approved.cwd
    for base in dict.fromkeys([launch, segment_resolver.worktree_root(launch) or launch]):
        roots.append(Path(base) / ".claude")
    files = [root / name for root in dict.fromkeys(roots) for name in SETTINGS_NAMES]
    return [*files, Path(MANAGED_SETTINGS)]


def _read_settings(path: Path) -> "Ambient | None":
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Ambient()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    permissions = data.get("permissions")
    if permissions is None:
        return Ambient()
    if not isinstance(permissions, dict):
        return None
    allow, dirs = permissions.get("allow", []), permissions.get("additionalDirectories", [])
    if not isinstance(allow, list) or not isinstance(dirs, list):
        return None
    root = path.parent.parent
    return Ambient([str(e) for e in allow], [os.path.join(root, os.path.expanduser(str(d))) for d in dirs])


def ambient_settings(approved: Approved) -> "Ambient | None":
    """The merged ambient settings; None when any of the files exists but cannot be read as settings: a
    file the hook cannot read may allow what the hook would deny, so no decision rests on it."""
    merged = Ambient()
    for path in _settings_files(approved):
        found = _read_settings(path)
        if found is None:
            return None
        merged.allow += found.allow
        merged.add_dirs += found.add_dirs
    return merged


def lenient_allow_entry(entries: list, command: str) -> "str | None":
    """An entry of `permissions.allow` that MAY let the harness run this call: the reading that errs toward
    covering (any segment matching, redirects and substitutions ignored). It can only keep the hook from
    denying, never make it allow."""
    return next((e for e in entries if permission_entry_match.covers(e, "Bash", {"command": command})), None)


def _all_files_in_cwd(approved: Approved, changed: list) -> bool:
    return all(isinstance(r, resources.FileResource) and _under(r.path, approved.cwd) for r in changed)


def accept_edits_runs(approved: Approved, text: str, changed: list) -> bool:
    """acceptEdits already runs the measured filesystem programs on a path inside the child's cwd."""
    if approved.mode != ACCEPT_EDITS or not changed:
        return False
    tokens = _tokens(text)
    if not tokens or os.path.basename(tokens[0]) not in ACCEPT_EDITS_PROGRAMS:
        return False
    return _all_files_in_cwd(approved, changed)


def outside_working_dirs(approved: Approved, ambient: Ambient, resource) -> bool:
    """A resource the harness has no reason to run a command against unprompted: not a file at all (a ref
    or a service), or a file outside the child's cwd and every directory it was started with."""
    if not isinstance(resource, resources.FileResource):
        return True
    path = _real(resource.path)
    roots = [approved.cwd, *(approved.add_dirs or []), *ambient.add_dirs]
    return not any(_under(path, _real(root)) for root in roots)


def may_deny(approved: Approved, ambient: "Ambient | None", command: str) -> bool:
    """The narrow positive case for a deny: a permission mode probe Q6 measured, a record that states
    `permissions_allow` and `add_dirs` itself, ambient settings that could all be read, and no allow entry
    of the record or of those settings matching the call even leniently."""
    return (
        approved.mode == ACCEPT_EDITS
        and approved.allow_entries is not None
        and approved.add_dirs is not None
        and ambient is not None
        and lenient_allow_entry([*approved.allow_entries, *ambient.allow], command) is None
    )


def env_blocker(approved: Approved, text: str) -> "str | None":
    """The exported variable that makes a segment unverifiable against the Bash shell. git answers to the
    GIT_* family and python to PYTHON*, but each starts the other (python runs git, a hook or alias may be
    python), so every program outside the closed non-carrier list is held to both families."""
    program = _program(text)
    if program in NON_CARRIER_PROGRAMS:
        return None
    for name in sorted(approved.env):
        if name in GIT_ENV_EXACT or name.startswith(GIT_ENV_PREFIX):
            return name
        if name.startswith("PYTHON") and name not in PYTHON_ENV_HARMLESS:
            return name
    return None


def _program(text: str) -> str:
    tokens = _tokens(text)
    return os.path.basename(tokens[0]) if tokens else ""


def _after_plain_options(text: str) -> list:
    """The tokens of a git segment from its subcommand on, or [] when an option other than `-C <dir>` comes
    first: `-c core.hooksPath=...` or `--exec-path` would move what git executes."""
    tokens = _tokens(text)[1:]
    while len(tokens) >= 2 and tokens[0] == "-C":
        tokens = tokens[2:]
    return [] if not tokens or tokens[0].startswith("-") else tokens


def _preserves_worktree(text: str) -> bool:
    """A git segment whose subcommand leaves tracked files alone and that names no option but `-C <dir>`."""
    tokens = _after_plain_options(text)
    return bool(tokens) and tokens[0] in WORKTREE_PRESERVING_GIT


def runs_git(verdict: "SegmentVerdict") -> bool:
    """Whether the segment starts git: a git call of any state, or a segment whose resolved effects include
    a ref (an opaque script the judge saw push). What other programs start by themselves is not modelled."""
    return _program(verdict.text) == "git" or any(isinstance(r, resources.VcsRefResource) for r in verdict.changed)


def may_change_git_surface(verdict: "SegmentVerdict", pinned: list) -> bool:
    """Whether a segment of a line that runs git could write what git executes before git does: code whose
    writes were never resolved (opaque, a rule pattern, a harness-allowed program that is not read-only), a
    git subcommand that rewrites tracked files, or a resolved write onto a pinned path."""
    program = _program(verdict.text)
    if program == "git":
        return not _preserves_worktree(verdict.text)
    if verdict.opaque or verdict.pattern:
        return True
    if verdict.state == HARNESS and not verdict.changed and program not in READ_ONLY_PROGRAMS:
        return True
    for resource in verdict.changed:
        if not isinstance(resource, resources.FileResource):
            continue
        real = _real(resource.path)
        if any(_under(real, path) or _under(path, real) for path in pinned):
            return True
    return False


def git_run_dir(text: str, cwd: "str | None") -> "str | None":
    """The directory a segment's git runs in: the segment's cwd moved by each leading `-C <dir>` (a relative
    one from the directory before it), or None when it cannot be pinned down. A segment that is not `git`
    runs in its cwd."""
    if cwd is None:
        return None
    base = _real(cwd)
    if _program(text) == "git":
        tokens = (_tokens(text) or [])[1:]
        while len(tokens) >= 2 and tokens[0] == "-C":
            target = tokens[1]
            if target.startswith("~") or "$" in target or any(ch in target for ch in GLOB_CHARS):
                return None
            base = _real(target if os.path.isabs(target) else os.path.join(base, target))
            tokens = tokens[2:]
    return base


def _ancestor_dot_git(directory: str) -> list:
    """Where a repository would have to appear for git to find one from `directory`."""
    return [os.path.join(item, ".git") for item in [directory, *Path(directory).parents]]


def rewrites_pinned_worktree_files(surface: "git_surface.Surface") -> bool:
    """Whether some pinned path lies in the worktree outside the git directory: the files a checkout or a merge
    rewrites. Every non-bare repository has some (the top-level `.gitattributes` is pinned even when absent)."""
    return surface.toplevel is not None and any(
        _under(path, surface.toplevel) and not _under(path, surface.repo) for path in surface.paths)


def rewrites_only_named_paths(text: str, run_dir: str, surface: "git_surface.Surface") -> bool:
    """Whether a segment is `git checkout [<tree-ish>] -- <literal paths>` and none of the paths overlaps a
    pinned worktree file: such a call rewrites exactly those paths, not the branch's tracked files."""
    tokens = _after_plain_options(text)
    if not tokens or tokens[0] != "checkout" or "--" not in tokens:
        return False
    head, named = tokens[1:tokens.index("--")], tokens[tokens.index("--") + 1:]
    if len(head) > 1 or any(part.startswith("-") for part in head) or not named:
        return False
    pinned = [path for path in surface.paths if surface.toplevel is not None
              and _under(path, surface.toplevel) and not _under(path, surface.repo)]
    for name in named:
        if name.startswith((":", "~", "-")) or "$" in name or any(ch in name for ch in GLOB_CHARS):
            return False
        target = _real(name if os.path.isabs(name) else os.path.join(run_dir, name))
        if any(_under(path, target) or _under(target, path) for path in pinned):
            return False
    return True


def worktree_code_blocker(approved: Approved, live: dict) -> "str | None":
    """Why the record's write grant inside a repository that holds code git starts blocks an allow: the child
    can edit what a hook or a framework file runs by path, which the snapshot digest does not cover, and the
    stage did not declare `runs_worktree_code`."""
    if approved.runs_worktree_code:
        return None
    for surface in live.values():
        if surface is None or not surface.runnable or surface.toplevel is None:
            continue
        for item in approved.resources:
            if (isinstance(item, resources.FileResource) and item.mode == "write"
                    and (_under(item.path, surface.toplevel) or _under(surface.toplevel, item.path))):
                return (f"{surface.repo} holds code git starts (a hook, a hook-framework file or a program its "
                        f"config names) and the record grants a write inside {surface.toplevel}; the stage "
                        "did not declare runs_worktree_code")
    return None


def git_surface_blocker(approved: Approved, verdicts: list, budget: "JudgeBudget") -> "str | None":
    """Why a line that runs git may not be allowed, or None. The repository each git-running segment runs in
    (its cwd moved by `-C`) must show the digest the spawn recorded (`git_surface`), and no other segment may
    be able to change it first: a covered `echo > .git/hooks/post-index-change && git status` matches the
    snapshot when the hook looks and not when git runs. Not-a-repository is no hook yet, but a neighbour that
    could create one still blocks; every other failure is a block."""
    runners = [v for v in verdicts if runs_git(v)]
    if not runners:
        return None
    live: dict = {}
    for verdict in runners:
        if verdict.git_dir is None:
            return f"cannot tell which repository git runs in: {verdict.text}"
        if verdict.git_dir in live:
            continue
        try:
            live[verdict.git_dir] = git_surface.capture(verdict.git_dir, budget.git_timeout)
        except git_surface.NotARepo:
            live[verdict.git_dir] = None
            continue
        except git_surface.Unreadable as exc:
            return f"cannot read what git would execute in {verdict.git_dir}: {exc}"
        why = git_surface.mismatch(approved.git_surface, live[verdict.git_dir])
        if why is not None:
            return why
    pinned = sorted({
        *(path for surface in live.values() if surface is not None for path in surface.paths),
        *(path for directory, surface in live.items() if surface is None for path in _ancestor_dot_git(directory)),
    })
    for verdict in verdicts:
        if len(runners) == 1 and verdict is runners[0] and _program(verdict.text) == "git":
            if not _after_plain_options(verdict.text):
                return f"a git option that moves what git executes: {verdict.text}"
            surface = live[verdict.git_dir]
            if (surface is not None and not _preserves_worktree(verdict.text) and rewrites_pinned_worktree_files(surface)
                    and not rewrites_only_named_paths(verdict.text, verdict.git_dir, surface)):
                return f"a git command that rewrites tracked files, among them what git executes: {verdict.text}"
        elif may_change_git_surface(verdict, pinned):
            return f"another segment of the line could change what git executes: {verdict.text}"
    return worktree_code_blocker(approved, live)


# --- segments -------------------------------------------------------------------------------------

class JudgeBudget:
    """One deadline for every opaque segment of a call: the harness kills the hook at its own timeout and
    falls through, so the judge calls together must end before it. Each call gets the judge's own timeout
    at most, and what is left of the deadline at least. The git probes of the hooks check draw from the
    same deadline."""

    def __init__(self, total_s: float, judge_s: float, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + total_s
        self.judge_s = judge_s

    def _left(self, cap: float) -> int:
        """Whole seconds up to `cap` still available, rounded up so a sliver of the deadline is a second,
        not nothing; 0 when the deadline is spent."""
        left = self.deadline - self.clock()
        return 0 if left <= 0 else max(1, math.ceil(min(cap, left)))

    def next_timeout(self) -> int:
        """Seconds the next judge call may take; 0 when the deadline is spent."""
        return self._left(self.judge_s)

    def git_timeout(self) -> int:
        """Seconds the next git probe may take; 0 when the deadline is spent."""
        return self._left(GIT_PROBE_TIMEOUT_S)


def evaluate_segment(approved: Approved, segment, line: list, runner, budget: JudgeBudget, cache_dir) -> SegmentVerdict:
    text = segment.text
    venue = _real(segment.venue or approved.cwd)
    entry = segment_allow_entry(approved, text)
    if entry is not None:
        return SegmentVerdict(text, HARNESS, venue=venue, matched=[entry])
    variable = env_blocker(approved, text)
    if variable is not None:
        return SegmentVerdict(text, UNDETERMINABLE, venue=venue, reason=f"{variable} is set and cannot be checked against the Bash shell")
    pattern = _first_rule(approved.rule_patterns, text)
    if pattern is not None:
        return SegmentVerdict(text, COVERED, venue=venue, matched=[pattern], pattern=True)
    opaque = segment.status == "opaque"
    if segment.status == "unresolved":
        return SegmentVerdict(text, UNDETERMINABLE, venue=venue, reason=f"unresolved: {segment.reason_class}")
    changed = list(segment.resources)
    judged: list = []
    if opaque:
        timeout = budget.next_timeout()
        if timeout < 1:
            return SegmentVerdict(text, UNDETERMINABLE, venue=venue, changed=changed, opaque=True, reason="judge budget spent")
        found = code_effects.effects_of(segment, line, runner=runner, timeout=timeout, cache_dir=cache_dir)
        if isinstance(found, code_effects.Undeterminable):
            return SegmentVerdict(text, UNDETERMINABLE, venue=venue, changed=changed, opaque=True, reason=found.reason)
        judged = [r for r in found.resources if _is_change(r)]
        changed += [r for r in found.resources if r not in changed]
    changes = [r for r in changed if _is_change(r)]
    uncovered = [r for r in changes if not covered_by(approved, r)]
    if accept_edits_runs(approved, text, changes):
        return SegmentVerdict(text, HARNESS, venue=venue, changed=changes, matched=["mode:acceptEdits"], opaque=opaque)
    if uncovered:
        return SegmentVerdict(text, UNCOVERED, venue=venue, changed=changes, uncovered=uncovered, opaque=opaque)
    if opaque and not judged:
        return SegmentVerdict(text, UNDETERMINABLE, venue=venue, changed=changes, opaque=True, reason="opaque code the judge found no write in: what it reads is not confined")
    confined, kinds = (True, []) if opaque else reads_confined(approved, text, venue)
    if not confined:
        return SegmentVerdict(text, UNDETERMINABLE, venue=venue, changed=changes, opaque=opaque, reason="reads outside the approved resources")
    return SegmentVerdict(text, COVERED, venue=venue, changed=changes, matched=kinds, opaque=opaque)


def deny_message(uncovered: list, text: str) -> str:
    resource_lines = "".join(f"Resource: {_describe(r)}\n" for r in uncovered)
    return (
        "Resource-grant check: this call changes a resource the approved stage does not cover "
        f"({'; '.join(_describe(r) for r in uncovered)}). Do not retry it. If the step needs it, end your "
        "reply with this request so the parent can decide:\n"
        "PERMISSION-REQUEST:\n"
        f"Action: {text}\n"
        "Why: the step's plan does not list this change\n"
        "Fallback if denied: stop the step\n"
        f"Rule: Bash({text})\n"
        f"{resource_lines}"
    )


def decide_bash(approved: Approved, command: str, runner, budget: JudgeBudget, cache_dir) -> Verdict:
    # Segments are resolved as if CDPATH were set: the Bash shell's own value cannot be read from here.
    split = tool_contracts.resolve_segments(command, approved.cwd, cdpath_unverified=True)
    if not split.segments:
        return Verdict(NONE, UNDETERMINABLE, venue=approved.cwd)
    verdicts = [evaluate_segment(approved, seg, split.segments, runner, budget, cache_dir) for seg in split.segments]
    for verdict, seg in zip(verdicts, split.segments):
        verdict.git_dir = git_run_dir(seg.text, seg.cwd)
    ambient = ambient_settings(approved)
    changed = list(dict.fromkeys(r for v in verdicts for r in v.changed))
    shown = [resources.resource_to_dict(r) for r in changed]
    matched = list(dict.fromkeys(m for v in verdicts for m in v.matched))
    opaque = any(v.opaque for v in verdicts)
    base = {"resources": shown, "matched": matched, "venue": approved.cwd}
    uncovered_segments = [v for v in verdicts if v.state == UNCOVERED]
    if uncovered_segments:
        denied = [v for v in uncovered_segments if any(outside_working_dirs(approved, ambient or Ambient(), r) for r in v.uncovered)]
        if denied and may_deny(approved, ambient, command):
            uncovered = list(dict.fromkeys(r for v in denied for r in v.uncovered))
            return Verdict(DENY, "uncovered-write", message=deny_message(uncovered, denied[0].text), **base)
        return Verdict(NONE, "uncovered-write", **base)
    if all(v.state == HARNESS for v in verdicts):
        return Verdict(NONE, "harness-allowed", **base)
    if all(v.state in (COVERED, HARNESS) for v in verdicts):
        why = git_surface_blocker(approved, verdicts, budget)
        if why is not None:
            return Verdict(NONE, UNDETERMINABLE, message=why, **base)
        return Verdict(ALLOW, "opaque-judged" if opaque else "covered", **base)
    return Verdict(NONE, UNDETERMINABLE, **base)


# --- file tools -----------------------------------------------------------------------------------

def _absolute(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def decide_edit(approved: Approved, path: str) -> Verdict:
    target = _absolute(path, approved.cwd)
    resource = resources.FileResource(target, "write")
    shown = [resources.resource_to_dict(resource)]
    if covered_by(approved, resource):
        return Verdict(ALLOW, "covered", resources=shown, matched=["record-resource"], venue=approved.cwd)
    return Verdict(NONE, "uncovered-write", resources=shown, venue=approved.cwd)


def decide_read(approved: Approved, path: str) -> Verdict:
    target = _absolute(path, approved.cwd)
    kind = readable(approved, target)
    if kind is None:
        return Verdict(NONE, UNDETERMINABLE, venue=approved.cwd)
    return Verdict(ALLOW, "covered", matched=[kind], venue=approved.cwd)


# --- the hook -------------------------------------------------------------------------------------

def _approved(link: dict, payload: dict, env) -> Approved:
    cwd = _real(str(payload.get("cwd") or os.getcwd()))
    typed = [resources.resource_from_dict(item) for item in (link.get("resources") or [])]
    root = segment_resolver.worktree_root(cwd)
    protected = resources.protected_permission_surfaces(
        repo_root=str(Path(__file__).resolve().parent.parent), delivery_worktree=root)
    return Approved(
        resources=typed,
        rule_patterns=[str(p) for p in (link.get("rule_patterns") or [])],
        allow_entries=_optional_strings(link.get("permissions_allow")),
        parent_session=str(link["parent_session"]),
        stage_index=int(link["stage_index"]),
        cwd=cwd,
        mode=str(payload.get("permission_mode") or ""),
        protected=protected,
        add_dirs=_optional_strings(link.get("add_dirs")),
        env=dict(env),
        launch_dir=_real(str(link["launch_dir"])) if link.get("launch_dir") else cwd,
        git_surface=link.get("git_surface"),
        runs_worktree_code=link.get("runs_worktree_code") is True,
    )


def _optional_strings(value) -> "list[str] | None":
    return [str(item) for item in value] if isinstance(value, list) else None


def decide(payload: dict, env, *, runner=None, cache_dir=None, constants=None) -> "tuple[Verdict, dict | None, Path | None]":
    """The verdict for one hook payload, the verified record link (None when inactive) and the state root.
    Never raises for a call it cannot judge: such a call is `none` with reason class `error`."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if _depth(env) < 1:
        return Verdict(NONE, "inactive"), None, None
    link, why = verify_env_value(env.get(NORM_HANDOFF_ENV, ""))
    if link is None:
        return Verdict(NONE, "inactive", message=why or ""), None, config_root.agentctl_state_dir()
    state_root = state_root_of(link["handoff"])
    if not agentctl_config.resource_grant_hook_enabled(env, constants):
        return Verdict(NONE, "kill-switch"), link, state_root
    if str(payload.get("permission_mode") or "") == PLAN_MODE:
        return Verdict(NONE, "plan-mode"), link, state_root
    try:
        approved = _approved(link, payload, env)
        arg = _tool_arg(tool, tool_input)
        if tool in BASH_TOOLS:
            total = max(agentctl_config.resource_grant_hook_timeout_s(constants) - 5, 1)
            budget = JudgeBudget(total, advisor.code_effects_judge_timeout_s())
            verdict = decide_bash(approved, arg, runner, budget, cache_dir)
        elif tool in EDIT_TOOLS:
            verdict = decide_edit(approved, arg)
        elif tool in READ_TOOLS:
            verdict = decide_read(approved, arg)
        else:
            verdict = Verdict(NONE, UNDETERMINABLE)
    except Exception as exc:  # noqa: BLE001 - a hook error must never become an allow
        return Verdict(NONE, "error", message=f"{type(exc).__name__}: {exc}"), link, state_root
    return verdict, link, state_root


def render(verdict: Verdict) -> str:
    """The harness's PreToolUse answer for an allow or a deny; the empty string for no decision."""
    if verdict.decision not in (ALLOW, DENY):
        return ""
    reason = verdict.message or f"resource-grant check: {verdict.reason_class}"
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": verdict.decision,
        "permissionDecisionReason": reason,
    }})


def run(stdin_text: str, env, *, runner=None, cache_dir=None, constants=None) -> str:
    """The whole hook: payload text in, the answer text out, one log row appended. Tests drive this."""
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    verdict, link, state_root = decide(payload, env, runner=runner, cache_dir=cache_dir, constants=constants)
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if state_root is not None:
        append_row(_log_path(state_root, link), _row(tool, _tool_arg(tool, tool_input), verdict))
    return render(verdict)


def main() -> int:
    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0
    try:
        answer = run(sys.stdin.read(), os.environ)
    except Exception as exc:  # noqa: BLE001
        print(f"hook-resource-grant-check: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if answer:
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
