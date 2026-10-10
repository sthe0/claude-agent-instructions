"""Digest-bound repo-script effects registry.

Difficulty removed: `tool_contracts.py`'s `_resolve_interpreter()` always
returns `unresolved` for `python3 <script> ...` — correctly so for an
arbitrary script, since resolving one requires knowing exactly what it does.
But a HANDFUL of repo scripts (`land-branch.py` chief among them) are
reviewed, stable, and invoked constantly by the engine's own dispatch/land
path; leaving every such invocation permanently unresolved means the
self-grant machinery this stage builds could never cover the engine's own
most common script calls. This module is the registry of exceptions: a
script earns an entry by review, pinned to the EXACT bytes reviewed
(`sha256`) so an edit to the script silently invalidates the entry rather
than silently keeping trust in bytes nobody reviewed.

Loading (`load_script_effects_table`) is TOTAL like `tool_contracts.
load_contract_table`: a malformed or duplicate entry raises at LOAD time (a
reviewed registry has no room for an ambiguous entry). Resolving a script
this registry has no entry for, or whose live bytes no longer match the
pinned digest, degrades to `unresolved` at RESOLVE time instead — the same
fail-closed bias `tool_contracts.py` documents throughout.

`tool_contracts.py` imports this module LAZILY (inside `_resolve_interpreter`,
not at module top) because this module's functions import `tool_contracts.
Resolution` back — a genuine mutual need (the registry produces the same
`Resolution` shape the rest of the resolver does), broken by making one side
of the cycle a deferred import rather than inventing a second, duplicate
result type."""
from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import resources as _resources

_DEFAULT_TABLE_PATH = Path(__file__).resolve().parent.parent / "script_effects.toml"


@dataclass(frozen=True)
class ScriptEntry:
    path: str  # repo-relative, e.g. "scripts/land-branch.py"
    sha256: str
    resolver: str


@dataclass(frozen=True)
class StageEffectDeclaration:
    """One `[[stage.effects]]` entry: a plan author's own claim that a
    specific script, pinned to the exact bytes they reviewed, resolves this
    stage's calls through `resolver`. Same shape as `ScriptEntry` (path/
    sha256/resolver) — kept as a distinct type rather than reused directly
    because its trust boundary is different: a GLOBAL `script_effects.toml`
    entry is trusted because it was reviewed into the repo; a plan-declared
    entry is trusted only for AS LONG AS the declaring plan itself stays the
    last user-approved version (cli.py's job, not this module's) — conflating
    the two types would make that distinction easy to lose at a call site."""
    path: str
    sha256: str
    resolver: str

    def to_dict(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "resolver": self.resolver}

    @classmethod
    def from_dict(cls, d: dict) -> "StageEffectDeclaration":
        return cls(path=d["path"], sha256=d["sha256"], resolver=d["resolver"])

    def as_script_entry(self) -> ScriptEntry:
        return ScriptEntry(path=self.path, sha256=self.sha256, resolver=self.resolver)


def load_script_effects_table(path: str | os.PathLike | None = None) -> dict[str, ScriptEntry]:
    """Load `script_effects.toml` into `{repo_relative_path: ScriptEntry}`.

    Enforces ONE entry per `path` (mirroring `tool_contracts.
    load_contract_table`'s one-entry-per-program rule): a silently-shadowed
    second entry for the same script would let an unreviewed digest quietly
    replace a reviewed one."""
    toml_path = Path(path) if path is not None else _DEFAULT_TABLE_PATH
    if not toml_path.exists():
        return {}
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    entries: dict[str, ScriptEntry] = {}
    for item in raw.get("script", []):
        script_path = str(item.get("path", ""))
        if not script_path:
            raise ValueError(f"script_effects.toml entry missing 'path': {item!r}")
        if script_path in entries:
            raise ValueError(f"script_effects.toml: duplicate path {script_path!r}")
        if item.get("op") == "land":
            # Same refusal as tool_contracts.load_contract_table -- no
            # registry entry may ever declare (let alone produce) op="land".
            raise ValueError(
                f"script_effects.toml: entry {script_path!r} declares op=\"land\", "
                f"which is refused -- no registry entry may ever produce an "
                f"op=\"land\" resource"
            )
        sha = str(item.get("sha256", ""))
        resolver = str(item.get("resolver", ""))
        if not sha or not resolver:
            raise ValueError(f"script_effects.toml entry {script_path!r} missing sha256/resolver")
        entries[script_path] = ScriptEntry(path=script_path, sha256=sha, resolver=resolver)
    return entries


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_common_dir(venue_real: str) -> str | None:
    """Realpath of the git common dir for `venue_real` (where refs actually
    live — the same dir across every linked worktree of one repo), or `None`
    if `venue_real` is not a git checkout at all. A `.git` DIRECTORY (a
    normal, non-worktree checkout) IS its own common dir; a `.git` FILE (a
    linked worktree) is followed via its documented one-line `gitdir: <path>`
    pointer, then via that gitdir's own `commondir` file when present —
    exactly git's own resolution, never guessed."""
    git_path = Path(venue_real) / ".git"
    if git_path.is_dir():
        return os.path.realpath(str(git_path))
    if not git_path.is_file():
        return None
    text = git_path.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:"):].strip()
    resolved = target if os.path.isabs(target) else str(Path(venue_real) / target)
    common_file = Path(resolved) / "commondir"
    if common_file.exists():
        common = common_file.read_text(encoding="utf-8").strip()
        base = common if os.path.isabs(common) else str(Path(resolved) / common)
        return os.path.realpath(base)
    return os.path.realpath(resolved)


def _script_real(script_path_arg: str, venue_real: str) -> str:
    expanded = os.path.expanduser(script_path_arg)
    abs_script = expanded if os.path.isabs(expanded) else os.path.join(venue_real, expanded)
    return os.path.realpath(abs_script)


def registered_entry(
    script_path_arg: str,
    venue_real: str,
    *,
    table: dict[str, ScriptEntry] | None = None,
) -> ScriptEntry | None:
    """The registry entry whose script `script_path_arg` names, matched by realpath
    against `venue_real` (so a symlink alias or `./x/../` spelling matches too), or
    `None`. The one matcher behind both `resolve_script` and `grants.bash_rule_identity`,
    so the resolver and the identity check cannot disagree on which scripts are
    registered."""
    table = table if table is not None else load_script_effects_table()
    script_real = _script_real(script_path_arg, venue_real)
    for entry in table.values():
        if os.path.realpath(str(Path(venue_real) / entry.path)) == script_real:
            return entry
    return None


def resolve_script(
    script_path_arg: str,
    argv: list[str],
    venue_real: str,
    *,
    table: dict[str, ScriptEntry] | None = None,
):
    """Resolve a `python3 <script> [argv...]` invocation via the registry, or
    return `None` if `script_path_arg` names no entry at all — the caller
    (`tool_contracts._resolve_interpreter`) then falls back to its own
    unresolved-with-identity default, unchanged from before this registry
    existed. Returns a `Resolution` (possibly ITSELF `unresolved` — e.g. on a
    digest mismatch or an unrecognized flag) whenever an entry DOES exist for
    this script path, so a stale/edited reviewed script is distinguishable
    (by `reason_class`) from a script that was never reviewed at all."""
    from .tool_contracts import Resolution  # deferred: see module docstring

    table = table if table is not None else load_script_effects_table()
    matched = registered_entry(script_path_arg, venue_real, table=table)
    if matched is None:
        return None
    script_real = _script_real(script_path_arg, venue_real)

    script_file = Path(script_real)
    if not script_file.is_file():
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason=f"script_effects entry {matched.path!r}: file not found at {script_real!r}",
        )
    live_digest = _sha256_of(script_file)
    if live_digest != matched.sha256:
        return Resolution(
            "unresolved", reason_class="adhoc-undeclared",
            reason=(
                f"script_effects entry for {matched.path!r} is pinned to sha256="
                f"{matched.sha256}, but the live file hashes to {live_digest} — an "
                f"edited script is untrusted until the registry entry is reviewed "
                f"and re-pinned"
            ),
        )

    resolver_fn = _RESOLVERS.get(matched.resolver)
    if resolver_fn is None:
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason=f"script_effects entry names unknown resolver {matched.resolver!r}",
        )
    return resolver_fn(argv, venue_real)


def _resolve_land_branch(argv: list[str], venue_real: str):
    """The full, reviewed argument grammar of `land-branch.py` (its own
    module docstring / argparse block is the source of truth this mirrors).
    Any flag OUTSIDE this set, or a recognized value-flag missing its value,
    makes the whole command unresolved — an unrecognized flag might change
    the script's effects in a way this resolver has no story for (fail-closed,
    same bias as every other resolver in this codebase)."""
    from .tool_contracts import Resolution, _abs_join, _realpath  # deferred: see module docstring

    known_bool_flags = {"--check", "--remote-only", "--keep-branch"}
    known_value_flags = {"-C", "--branch", "--trunk", "--remote"}

    values: dict[str, str] = {}
    bools: dict[str, bool] = {f: False for f in known_bool_flags}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in known_bool_flags:
            bools[tok] = True
            i += 1
            continue
        if tok in known_value_flags:
            if i + 1 >= len(argv):
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"land-branch.py: {tok!r} missing its value",
                )
            values[tok] = argv[i + 1]
            i += 2
            continue
        if "=" in tok and tok.split("=", 1)[0] in known_value_flags:
            key, val = tok.split("=", 1)
            values[key] = val
            i += 1
            continue
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason=f"land-branch.py: unrecognized argument {tok!r}",
        )

    if bools["--check"]:
        # Zero side effects by the script's own module docstring guarantee.
        return Resolution("resolved", resources=[])

    if not bools["--keep-branch"]:
        # Without --keep-branch, landing always includes the remote-branch
        # delete-push (a delete-push is NEVER a covered push resource) and
        # a worktree-remove whose path is discovered at runtime from
        # `git worktree list`, not from argv — neither is resolvable ahead
        # of execution.
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason=(
                "land-branch.py without --keep-branch deletes the remote "
                "branch (a delete-push, never a covered push resource) and "
                "removes a worktree path discovered at runtime, not from "
                "argv — neither is resolvable ahead of execution"
            ),
        )

    branch = values.get("--branch")
    if not branch:
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason=(
                "land-branch.py: --branch omitted infers the branch from the "
                "current git HEAD, which is not literal from argv"
            ),
        )

    dash_c = values.get("-C")
    if dash_c is not None:
        # A relative `-C` value must be joined against the VENUE -- the
        # resource this resolution decides whether to self-grant a
        # push for -- never against the analyzing process's own cwd (which
        # need not have any relation to the venue at all). Mirrors git's
        # own `-C`/`--git-dir`/`--work-tree` handling in
        # `tool_contracts._resolve_git`.
        dash_c_real = _realpath(_abs_join(dash_c, venue_real))
        if dash_c_real != venue_real:
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason=f"land-branch.py: -C {dash_c!r} targets a different checkout than the venue",
            )

    remote = values.get("--remote", "origin")
    trunk = values.get("--trunk", "main")

    out: list = [_resources.VcsRefResource(remote, trunk, "push")]
    if not bools["--remote-only"]:
        common_dir = _git_common_dir(venue_real)
        if common_dir is None:
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason="land-branch.py: could not resolve the git common dir for the venue",
            )
        out.append(_resources.FileResource(common_dir, "write"))

    return Resolution("resolved", resources=out)


_RESOLVERS = {
    "land_branch": _resolve_land_branch,
}
