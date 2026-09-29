"""Contract-grounded command resolution: which resource(s), if any, a Bash
command line can change.

Difficulty removed: `agentctl/resources.py` types the RESOURCE a permission is
about, but something still has to turn a raw command STRING into the
resource(s) it touches — and that something must never guess from the
command's spelling (a false "covered"/"resolved" here lets `resolve-permission
--by agent` self-grant a call nobody reviewed). This module is that one
place: every decision traces to either (a) a reviewed `[[program]]` entry in
`tool_contracts.toml` naming the program's actual documented contract, (b) a
digest-matching entry in the script-effects registry (added alongside this
module in a later checkpoint — not yet consulted here), or (c) the dedicated
landed-spec resolver for `op="land"` (also a later checkpoint). Anything this
module cannot decide from (a)/(b)/(c) is `unresolved`, never `resolved` with
an empty or guessed resource set — the same fail-toward-unresolved bias
`resources.Resource.covers` already documents.

`op="land"` is NEVER produced here: no contract entry, and no custom
resolver in `_CUSTOM_RESOLVERS`, may ever return a `VcsRefResource` with
`op="land"` — landing is a distinct real-world effect from pushing (R1) and
is decided solely by the (not-yet-added) landed-spec resolver.
"""
from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lib import bash_write_targets, shell_tokens, widening_targets

from . import resources

_DEFAULT_TOML = Path(__file__).resolve().parent / "tool_contracts.toml"

_VALID_EFFECTS = frozenset({"none", "writes-operands", "unresolved", "resolver"})

#: Non-literal-path marker characters: a token containing any of these names
#: a target that depends on data this module cannot see (glob expansion, an
#: unexpanded variable) rather than a concrete path — always collapses
#: resolution/identity to unresolved/opaque, the same bias documented on
#: `resources.Resource.covers`.
_NON_LITERAL_MARKERS = "$*?["

#: Interpreters whose first non-flag operand is "the script" for identity
#: purposes. Mirrors `agentctl/grants.py`'s `_INTERPRETERS`; kept as a
#: separate constant here rather than imported, since importing a private
#: module-level constant across files for a two-name set is not worth the
#: coupling (grants.py does not export it).
_SCRIPT_INTERPRETERS = frozenset(
    {"bash", "sh", "zsh", "dash", "python", "python3", "node", "ruby", "perl"}
)

#: Cap on identity's directory-listing residual (REQ3): beyond either limit
#: the listing collapses to `("opaque",)` rather than a partial, silently
#: misleading listing.
_DIR_LISTING_MAX_FILES = 64
_DIR_LISTING_MAX_BYTES = 1024 * 1024


@dataclass
class Resolution:
    """The outcome of resolving one command line.

    `status` is `"resolved"` or `"unresolved"`. A resolved command's
    `resources` lists every `resources.Resource` it can change (empty is a
    valid resolved outcome — e.g. a pure read-only pipeline). An unresolved
    command's `resources` is always empty; `reason_class` is a short,
    machine-stable tag (`"nested-execution"`, `"unknown-program"`,
    `"declared-unresolved"`, `"force-or-delete-push"`, `"contract-unresolved"`,
    `"script-digest-mismatch"`,
    `"non-literal-target"`) and `identity` is the content-bound tuple REQ3
    requires so two textually-different unresolved commands with the same
    real effect (or the same command re-run unchanged) can be told apart from
    two that differ."""

    status: str
    resources: list[resources.Resource] = field(default_factory=list)
    reason_class: str | None = None
    reason: str | None = None
    identity: tuple | None = None


@dataclass(frozen=True)
class ContractEntry:
    name: str
    effect: str
    reason: str | None = None
    source: str | None = None


def load_contract_table(path: str | os.PathLike | None = None) -> dict[str, ContractEntry]:
    """Load `tool_contracts.toml` into `{program_name: ContractEntry}`.

    Enforces ONE entry per program name (REQ3): `tomllib` itself does not
    reject a duplicate `[[program]] name = "x"` pair (array-of-tables has no
    key uniqueness), so this loader does — a silently-shadowed second entry
    for the same program would let an unreviewed effect quietly replace a
    reviewed one."""
    toml_path = Path(path) if path is not None else _DEFAULT_TOML
    with open(toml_path, "rb") as fh:
        doc = tomllib.load(fh)

    table: dict[str, ContractEntry] = {}
    for raw in doc.get("program", []):
        name = raw.get("name")
        if not name:
            raise ValueError(f"tool_contracts.toml: a [[program]] entry is missing 'name': {raw!r}")
        if name in table:
            raise ValueError(f"tool_contracts.toml: duplicate [[program]] entry for {name!r}")
        effect = raw.get("effect")
        if effect not in _VALID_EFFECTS:
            raise ValueError(
                f"tool_contracts.toml: program {name!r} has invalid effect {effect!r}"
                f" (must be one of {sorted(_VALID_EFFECTS)})"
            )
        if effect == "unresolved" and not raw.get("reason"):
            raise ValueError(f"tool_contracts.toml: program {name!r} declares effect='unresolved' with no 'reason'")
        table[name] = ContractEntry(
            name=name, effect=effect, reason=raw.get("reason"), source=raw.get("source")
        )
    return table


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _abs_join(token: str, base: str) -> str:
    expanded = os.path.expanduser(token)
    return expanded if os.path.isabs(expanded) else os.path.join(base, expanded)


def _is_non_literal(token: str) -> bool:
    return any(ch in token for ch in _NON_LITERAL_MARKERS)


def _has_nested_execution(stripped_text: str) -> bool:
    """True iff `stripped_text` (heredoc bodies already stripped) contains a
    command-substitution or bare-subshell construct — `$(...)`, a backtick
    pair, or a segment opening with `(`. Any of these lets a command run
    NESTED work no per-segment, per-token analysis below can see, so the
    whole command collapses to unresolved before any further parsing is
    attempted (an unsound static read is worse than an honest refusal)."""
    if "$(" in stripped_text or "`" in stripped_text:
        return True
    try:
        tokens = shell_tokens.separator_exact_split(stripped_text)
    except Exception:
        return True
    for seg in bash_write_targets.split_segments(tokens):
        if seg and seg[0].startswith("("):
            return True
    return False


def _real_program(seg: list[str]) -> tuple[str, list[str]] | None:
    """`(casefolded_basename, operand_tokens)` of the real (wrapper-stripped)
    program a segment invokes, or `None` for an empty/assignment-only
    segment."""
    stripped = widening_targets.strip_wrappers(seg)
    if not stripped:
        return None
    return widening_targets.program_name(stripped[0]).casefold(), stripped[1:]


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_listing_identity(dir_path: str) -> tuple:
    """A capped, sorted `(filename, sha256)` tuple of `dir_path`'s immediate
    regular, non-symlink files — the "sibling files changed" residual REQ3
    asks for on an unresolved script's identity. Collapses to `("opaque",)`
    over either the file-count or total-byte cap, rather than silently
    reporting a partial listing that could mask a changed sibling outside
    the cap."""
    try:
        entries = sorted(os.listdir(dir_path))
    except OSError:
        return ("opaque",)
    listing: list[tuple[str, str]] = []
    total_bytes = 0
    for name in entries:
        full = os.path.join(dir_path, name)
        if os.path.islink(full) or not os.path.isfile(full):
            continue
        if len(listing) >= _DIR_LISTING_MAX_FILES:
            return ("opaque",)
        size = os.path.getsize(full)
        total_bytes += size
        if total_bytes > _DIR_LISTING_MAX_BYTES:
            return ("opaque",)
        listing.append((name, _hash_file(full)))
    return tuple(listing)


def _compute_identity(text: str, venue_real: str) -> tuple:
    """The content-bound identity of an unresolved command (REQ3): `text`
    itself, plus — for every literal operand token resolved OUTSIDE the
    venue subtree — its sha256 (or the literal string `"absent"` if the
    path does not exist), plus a capped directory-listing digest of the
    first interpreter-script operand's containing directory when that
    script itself lies outside the venue. ANY non-literal operand
    (`$`/`*`/`?`/`[`) collapses the whole identity to `("opaque", text,
    venue_real)` — a wildcard or variable expansion names a target this
    module cannot see, so no finer-grained identity would be honest."""
    stripped = shell_tokens.strip_heredoc_bodies(text)
    try:
        tokens = shell_tokens.separator_exact_split(stripped)
    except Exception:
        return ("opaque", text, venue_real)

    all_operands: list[str] = []
    script_candidate: str | None = None
    for seg in bash_write_targets.split_segments(tokens):
        real = _real_program(seg)
        if real is None:
            continue
        prog, operands = real
        literal_operands = [t for t in operands if not t.startswith("-")]
        all_operands.extend(literal_operands)
        if script_candidate is None and literal_operands and (
            prog in _SCRIPT_INTERPRETERS or widening_targets.INTERPRETER_RE.match(prog)
        ):
            script_candidate = literal_operands[0]

    if any(_is_non_literal(tok) for tok in all_operands):
        return ("opaque", text, venue_real)

    operand_identity: list[tuple[str, str]] = []
    for tok in sorted(set(all_operands)):
        abs_path = _abs_join(tok, venue_real)
        real_path = _realpath(abs_path)
        if real_path == venue_real or real_path.startswith(venue_real + os.sep):
            continue  # in-venue operands are already covered by any venue write resource
        if os.path.isfile(real_path):
            operand_identity.append((tok, _hash_file(real_path)))
        else:
            operand_identity.append((tok, "absent"))

    script_listing: tuple | None = None
    if script_candidate is not None:
        abs_script = _abs_join(script_candidate, venue_real)
        real_script = _realpath(abs_script)
        if not (real_script == venue_real or real_script.startswith(venue_real + os.sep)):
            script_listing = _dir_listing_identity(os.path.dirname(real_script) or venue_real)

    return ("unresolved", text, venue_real, tuple(operand_identity), script_listing)


# ---------------------------------------------------------------------------
# Custom resolvers (effect = "resolver" entries dispatch here by program name)
# ---------------------------------------------------------------------------

_GIT_READONLY_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "branch", "remote", "config", "rev-parse", "ls-files", "blame"}
)

#: Global `git` options that consume a following value token, so the
#: subcommand-finding scan below does not mistake a flag's value for the
#: subcommand itself (`git -C /some/dir push ...`).
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree"})

_GIT_PUSH_FORCE_FLAGS = frozenset({"-f", "--force"})


def _find_git_subcommand(operands: list[str]) -> tuple[str | None, list[str]]:
    i = 0
    n = len(operands)
    while i < n and operands[i].startswith("-"):
        if operands[i] in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
        else:
            i += 1
    if i >= n:
        return None, []
    return operands[i], operands[i + 1 :]


def _resolve_git(operands: list[str]) -> Resolution:
    subcommand, rest = _find_git_subcommand(operands)
    if subcommand is None:
        return Resolution("unresolved", reason_class="contract-unresolved", reason="bare `git` with no subcommand")
    if subcommand in _GIT_READONLY_SUBCOMMANDS:
        return Resolution("resolved", resources=[])
    if subcommand != "push":
        return Resolution(
            "unresolved",
            reason_class="declared-unresolved",
            reason=f"git subcommand {subcommand!r} is not resolved by this table",
        )

    is_force = False
    is_delete = False
    positionals: list[str] = []
    for tok in rest:
        if tok in _GIT_PUSH_FORCE_FLAGS or tok.startswith("--force-with-lease"):
            is_force = True
        elif tok in ("-d", "--delete"):
            is_delete = True
        elif tok.startswith("-"):
            continue
        else:
            positionals.append(tok)

    if is_force or is_delete:
        return Resolution(
            "unresolved",
            reason_class="force-or-delete-push",
            reason="a force or delete push variant is never covered by a plain-push approval (C5)",
        )

    # Zero-refspec push (bare `git push`, or `git push <remote>` with no
    # refspec): the ref actually pushed depends on the remote's upstream
    # config and `push.default`, neither visible on the command line — this
    # is the root's explicit constraint #1, NOT a "no resources" outcome.
    if len(positionals) < 2:
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="git push with no explicit refspec: the pushed ref depends on git config, not decidable from the command line",
        )

    remote, refspecs = positionals[0], positionals[1:]
    if _is_non_literal(remote):
        return Resolution("unresolved", reason_class="non-literal-target", reason="git push remote is non-literal")

    push_resources: list[resources.Resource] = []
    for refspec in refspecs:
        if _is_non_literal(refspec):
            return Resolution(
                "unresolved", reason_class="non-literal-target", reason="git push refspec is non-literal"
            )
        if refspec.startswith(":"):
            return Resolution(
                "unresolved",
                reason_class="force-or-delete-push",
                reason="a `:branch` refspec deletes the remote ref (C5)",
            )
        dst = refspec.split(":", 1)[1] if ":" in refspec else refspec
        if not dst:
            return Resolution(
                "unresolved",
                reason_class="force-or-delete-push",
                reason="an empty destination refspec deletes the remote ref (C5)",
            )
        push_resources.append(resources.VcsRefResource(remote, dst, "push"))
    return Resolution("resolved", resources=push_resources)


_FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _resolve_find(operands: list[str]) -> Resolution:
    if any(tok in _FIND_EXEC_FLAGS for tok in operands):
        return Resolution(
            "unresolved",
            reason_class="declared-unresolved",
            reason="find -exec/-execdir/-ok/-okdir runs an arbitrary command per matched file",
        )
    return Resolution("resolved", resources=[])


def _resolve_awk(operands: list[str]) -> Resolution:
    return Resolution(
        "unresolved",
        reason_class="declared-unresolved",
        reason="an awk program's own `print > \"file\"` can write anywhere the script names",
    )


def _resolve_pytest(venue_real: str) -> Resolution:
    return Resolution("resolved", resources=[resources.FileResource(venue_real, "write")])


def _script_and_argv(operands: list[str]) -> tuple[str | None, list[str]]:
    """The interpreter's first non-flag operand is "the script" (mirrors
    `_compute_identity`'s own `script_candidate` convention); everything
    after it is the script's OWN argv, flags included — an interpreter-level
    flag before the script (`python3 -u script.py --branch x`) is skipped,
    not mistaken for one of the script's own arguments."""
    for i, tok in enumerate(operands):
        if not tok.startswith("-"):
            return tok, operands[i + 1 :]
    return None, []


def _resolve_interpreter(operands: list[str], venue_real: str) -> Resolution:
    """Consult the script-effects registry (`script_effects.py`) for a
    reviewed, digest-pinned entry matching the script being run. No entry —
    or no script at all (e.g. `python3 -c "..."`) — falls back to the
    original unresolved verdict, unchanged from before the registry existed;
    the caller (`resolve_command`) computes content-bound identity for that
    case regardless of what this function returns for `identity`."""
    from . import script_effects  # deferred: see script_effects.py's own docstring

    script, script_argv = _script_and_argv(operands)
    if script is not None:
        resolved = script_effects.resolve_script(script, script_argv, venue_real)
        if resolved is not None:
            return resolved
    return Resolution("unresolved", reason_class="declared-unresolved", reason="no script-effects registry entry")


_CUSTOM_RESOLVERS = {
    "git": lambda operands, venue_real: _resolve_git(operands),
    "find": lambda operands, venue_real: _resolve_find(operands),
    "awk": lambda operands, venue_real: _resolve_awk(operands),
    "gawk": lambda operands, venue_real: _resolve_awk(operands),
    "nawk": lambda operands, venue_real: _resolve_awk(operands),
    "mawk": lambda operands, venue_real: _resolve_awk(operands),
    "pytest": lambda operands, venue_real: _resolve_pytest(venue_real),
    "python3": lambda operands, venue_real: _resolve_interpreter(operands, venue_real),
    "python": lambda operands, venue_real: _resolve_interpreter(operands, venue_real),
}


def _unresolved_with_identity(text: str, venue_real: str, reason_class: str, reason: str) -> Resolution:
    return Resolution(
        "unresolved",
        reason_class=reason_class,
        reason=reason,
        identity=_compute_identity(text, venue_real),
    )


def resolve_command(
    text: str, venue: str, *, contract_table: dict[str, ContractEntry] | None = None
) -> Resolution:
    """Resolve `text` (a full Bash command line, as it would appear in a
    `Bash(...)` rule or an actual invocation) run with `venue` as its
    effective cwd, to the resource(s) it can change.

    `venue` is realpath-resolved once and used both as the join base for
    every relative operand and as the write-target for `pytest`'s
    whole-subtree residual. Fail-safe throughout (REQ3): the first
    undecidable segment makes the WHOLE command unresolved — a command that
    is 90% readable and 10% opaque is not 90% approvable."""
    venue_real = _realpath(venue)
    table = contract_table if contract_table is not None else load_contract_table()

    stripped_text = shell_tokens.strip_heredoc_bodies(text)
    if _has_nested_execution(stripped_text):
        return _unresolved_with_identity(
            text, venue_real, "nested-execution", "command substitution or a subshell hides nested work"
        )

    try:
        tokens = shell_tokens.separator_exact_split(stripped_text)
    except Exception:
        return _unresolved_with_identity(text, venue_real, "nested-execution", "command line failed to tokenize")

    segments = list(bash_write_targets.split_segments(tokens))
    if not segments:
        return Resolution("resolved", resources=[])

    all_resources: list[resources.Resource] = []
    for seg in segments:
        write_candidates = bash_write_targets.segment_write_target(seg, venue_real)
        real = _real_program(seg)
        if real is None:
            # Assignment-only segment (e.g. `FOO=bar`): no program, no effect.
            continue
        prog, operands = real

        entry = table.get(prog)
        if entry is None:
            return _unresolved_with_identity(
                text, venue_real, "unknown-program", f"{prog!r} has no tool_contracts.toml entry"
            )

        if entry.effect == "unresolved":
            return _unresolved_with_identity(text, venue_real, "declared-unresolved", entry.reason or "")

        if entry.effect == "resolver":
            resolver = _CUSTOM_RESOLVERS.get(prog)
            if resolver is None:  # pragma: no cover - table/resolver drift guard
                raise ValueError(f"tool_contracts.toml declares {prog!r} as effect='resolver' with no matching resolver")
            seg_resolution = resolver(operands, venue_real)
            if seg_resolution.status == "unresolved":
                return _unresolved_with_identity(
                    text, venue_real, seg_resolution.reason_class or "declared-unresolved", seg_resolution.reason or ""
                )
            all_resources.extend(seg_resolution.resources)
            # A resolver-dispatched program may STILL redirect its stdout
            # (`git status > f`); the generic redirect candidates below are
            # collected regardless of the resolver's own verdict.

        # entry.effect in ("none", "writes-operands"): the program's OWN
        # write targets (if any) are exactly what bash_write_targets already
        # found in `write_candidates` — no further table dispatch needed.
        for candidate in write_candidates:
            all_resources.append(resources.FileResource(candidate, "write"))

    return Resolution("resolved", resources=all_resources)
