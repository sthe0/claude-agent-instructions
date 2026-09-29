"""Contract-grounded command resolution: which resource(s), if any, a Bash
command line can change.

Difficulty removed: `agentctl/resources.py` types the RESOURCE a permission is
about, but something still has to turn a raw command STRING into the
resource(s) it touches — and that something must never guess from the
command's spelling (a false "covered"/"resolved" here lets `resolve-permission
--by agent` self-grant a call nobody reviewed). This module is that one
place: every decision traces to either (a) a reviewed `[[program]]` entry in
`tool_contracts.toml` naming the program's actual documented contract, (b) a
digest-matching entry in `script_effects.py`'s registry (consulted from
`_resolve_interpreter` for a handful of reviewed repo scripts), or (c) the
dedicated landed-spec resolver for `op="land"` (a later checkpoint, not yet
added). Anything this module cannot decide from (a)/(b)/(c) is `unresolved`,
never `resolved` with an empty or guessed resource set — the same
fail-toward-unresolved bias `resources.Resource.covers` already documents.

`op="land"` is NEVER produced here: no contract entry, and no custom
resolver in `_CUSTOM_RESOLVERS`, may ever return a `VcsRefResource` with
`op="land"` — landing is a distinct real-world effect from pushing (R1) and
is decided solely by the (not-yet-added) landed-spec resolver.
"""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lib import bash_write_targets, shell_tokens, widening_targets

from . import resources

_DEFAULT_TOML = Path(__file__).resolve().parent / "tool_contracts.toml"

_VALID_EFFECTS = frozenset({"none", "unresolved", "resolver"})

#: Non-literal-path marker characters: a token containing any of these names
#: a target that depends on data this module cannot see (glob expansion, an
#: unexpanded variable, command substitution) rather than a concrete path —
#: always collapses resolution/identity to unresolved/opaque, the same bias
#: documented on `resources.Resource.covers`. A leading `~` (home-directory
#: expansion) is checked separately in `_is_non_literal` below, since it only
#: names a variable target at the START of a token, not wherever it appears.
_NON_LITERAL_MARKERS = "$*?[`{"

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
    command's `resources` is always empty; `reason_class` is one of the five
    canonical tags (`"residual-syntax"`, `"adhoc-undeclared"`,
    `"contract-unresolved"`, `"wildcard-tail"`, `"unknown-program"`) and
    `identity` is the content-bound tuple REQ3
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
    #: `effect="none"` only: the closed allowlist of flags reviewed as
    #: incapable of writing anywhere -- an operand flag outside this set
    #: collapses resolution to unresolved (see `resolve_command`). The
    #: single-element set `{"*"}` is the reviewed declaration "this
    #: program has NO write-capable flag at all", not a wildcard that
    #: skips the check.
    safe_flags: frozenset[str] | None = None


def load_contract_table(path: str | os.PathLike | None = None) -> dict[str, ContractEntry]:
    """Load `tool_contracts.toml` into `{program_name: ContractEntry}`.

    Enforces ONE entry per program name (REQ3): `tomllib` itself does not
    reject a duplicate `[[program]] name = "x"` pair (array-of-tables has no
    key uniqueness), so this loader does — a silently-shadowed second entry
    for the same program would let an unreviewed effect quietly replace a
    reviewed one. Also enforces that every `effect="none"` entry declares
    its own `safe_flags` allowlist: a `none` program is exempt from the
    generic write-candidate scan (`bash_write_targets`), so its own flags
    are the ONLY thing standing between "reads only" and "some flag on
    this program writes a file" -- an entry with no reviewed allowlist
    would let a future write-capable flag slip through unresolved-by-
    omission."""
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
        if raw.get("op") == "land":
            # Landing is categorically more privileged than pushing, decided
            # solely by the dedicated landed-spec comparison in resources.py's
            # VcsRefResource — never by a contract table entry.
            raise ValueError(
                f"tool_contracts.toml: program {name!r} declares op=\"land\", which "
                f"is refused -- no contract entry may ever produce an op=\"land\" resource"
            )
        effect = raw.get("effect")
        if effect not in _VALID_EFFECTS:
            raise ValueError(
                f"tool_contracts.toml: program {name!r} has invalid effect {effect!r}"
                f" (must be one of {sorted(_VALID_EFFECTS)})"
            )
        if effect == "unresolved" and not raw.get("reason"):
            raise ValueError(f"tool_contracts.toml: program {name!r} declares effect='unresolved' with no 'reason'")
        safe_flags_raw = raw.get("safe_flags")
        if effect == "none":
            if not safe_flags_raw:
                raise ValueError(
                    f"tool_contracts.toml: program {name!r} declares effect='none' with no 'safe_flags' allowlist"
                )
            safe_flags = frozenset(safe_flags_raw)
        elif safe_flags_raw:
            raise ValueError(
                f"tool_contracts.toml: program {name!r} declares 'safe_flags' but effect={effect!r} != 'none'"
            )
        else:
            safe_flags = None
        table[name] = ContractEntry(
            name=name, effect=effect, reason=raw.get("reason"), source=raw.get("source"), safe_flags=safe_flags
        )
    return table


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _abs_join(token: str, base: str) -> str:
    expanded = os.path.expanduser(token)
    return expanded if os.path.isabs(expanded) else os.path.join(base, expanded)


def _is_non_literal(token: str) -> bool:
    return any(ch in token for ch in _NON_LITERAL_MARKERS) or token.startswith("~")


def _has_nested_execution(stripped_text: str) -> bool:
    """True iff `stripped_text` (heredoc bodies already stripped) contains a
    command-substitution or bare-subshell construct — `$(...)`, a backtick
    pair, or a segment opening with `(`. Any of these lets a command run
    NESTED work no per-segment, per-token analysis below can see, so the
    whole command collapses to unresolved before any further parsing is
    attempted (an unsound static read is worse than an honest refusal)."""
    if "$(" in stripped_text or "`" in stripped_text:
        return True
    if shell_tokens.has_process_substitution(stripped_text):
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


#: A leading `NAME=value` assignment (no dash) disqualifies a segment
#: outright (item (a) of the closed command grammar) -- an env assignment
#: can change ANY program's behavior in ways this module cannot see (a
#: different config file, a different PATH, an injected interpreter flag via
#: a `*_OPTS`-style variable), so there is no "reviewed benign" exception:
#: every occurrence, on every segment, is unconditionally unresolved.
_BARE_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Wrapper programs never in the reviewed closed set (item (a)): each can
#: change what actually runs, or how, in ways the segment's own program
#: token does not show -- `env`/`nice`/`timeout`/`nohup` used to get a
#: closed-world stripping pass of their own; that pass is gone, and a
#: wrapper token is now refused outright, wherever it leads a segment.
_FORBIDDEN_WRAPPER_TOKENS = frozenset(
    {
        "env", "nice", "timeout", "nohup", "eval", "xargs", "sudo",
        "time", "flock", "command", "exec", "builtin",
    }
)

#: The one reviewed exception to item (a)'s redirect-operator prohibition:
#: `2>&1` (stderr merged into stdout), as its own whitespace-delimited unit.
#: Not a single lexer token -- `shell_tokens.separator_exact_split` has no
#: two-character `>&` operator, so this splits into four ordinary tokens
#: (`2`, `>`, `&`, `1`); recognizing it is therefore a text-level check, run
#: before tokenization, rather than a token-equality check after it.
_STANDALONE_REDIRECT_TO_STDOUT = re.compile(r"(?:(?<=\s)|^)2>&1(?:(?=\s)|$)")


def _strip_standalone_stderr_merge(text: str) -> str:
    """Substitutes the one reviewed redirect exception (`2>&1`) away with a
    space wherever it appears as its own whitespace-delimited unit. Every
    tokenization pass over a shape-clean command line -- inside
    `_check_command_shape` and again in `resolve_command`'s own segment
    split -- must run on this substituted text, not the raw one: the raw
    text's `2>&1` splits into the bare tokens `2`, `>`, `&`, `1`, and `&`
    alone is the never-allowed background operator, so tokenizing the raw
    text would refuse the very case this exception exists to allow."""
    return _STANDALONE_REDIRECT_TO_STDOUT.sub(" ", text)


def _check_command_shape(stripped_text: str) -> tuple[str, str] | None:
    """Item (a) of the closed command grammar: the one positive shape a
    command line must have before ANY per-segment/per-program dispatch
    runs. Returns `None` when the shape is clean, or `(reason_class,
    reason)` to refuse the WHOLE command -- a command that is mostly clean
    and 10% opaque is not 90% approvable, the same bias `resolve_command`'s
    own docstring already states for a single undecidable segment.

    Runs on the text BEFORE segment splitting, deliberately: several of its
    conditions (a stray `&`/`||`/`|&`, a redirection operator) are exactly
    the characters `bash_write_targets.split_segments`'s own separator set
    would otherwise fold into an ordinary segment boundary, or a per-program
    resolver would otherwise trip over piecemeal, one segment at a time."""
    # Redirects: checked on the raw text, ahead of tokenization, since the
    # one reviewed exception (`2>&1`) does not survive tokenization as a
    # single unit (see `_STANDALONE_REDIRECT_TO_STDOUT`'s docstring) and
    # every OTHER redirect form (`>`, `>>`, `>|`, `<`, `<<`, `<<<`, `&>`,
    # `>&`, a fd-numbered form, process substitution) contains a `<` or `>`
    # character -- so removing the one allowed exception first and then
    # checking for either character catches every forbidden form at once.
    # `residual` (exception substituted away) is also what gets tokenized
    # below: the untouched text would still split `2>&1` into the bare
    # tokens `2`, `>`, `&`, `1`, and `&` alone is the never-allowed
    # background operator -- so tokenizing the original text would refuse
    # the one case this exception exists to allow.
    residual = _strip_standalone_stderr_merge(stripped_text)
    if "<" in residual or ">" in residual:
        return (
            "contract-unresolved",
            "a redirection operator is present outside the one reviewed exception, the standalone token `2>&1`",
        )

    try:
        tokens = shell_tokens.separator_exact_split(residual)
    except Exception:
        return "residual-syntax", "command line failed to tokenize"

    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in ("&&", ";", "|"):
            segments.append([])
            continue
        if tok in ("||", "|&", "&"):
            return "contract-unresolved", f"the {tok!r} operator is never in the reviewed closed set"
        if tok in ("(", ")"):
            return "residual-syntax", "a subshell is never in the reviewed closed set"
        segments[-1].append(tok)

    for seg in segments:
        if not seg:
            return "contract-unresolved", "a command segment is empty -- a leading, trailing, or doubled separator"
        first = seg[0]
        # Assignment is checked ahead of path-qualification: an assignment's
        # VALUE half routinely contains a `/` (`GIT_DIR=/tmp/other`), and
        # that value is not the program token this path-qualification check
        # means to catch -- checking `/` first would misreport an
        # assignment as a path-qualified program.
        if _BARE_ASSIGNMENT_RE.match(first):
            return (
                "contract-unresolved",
                f"leading environment assignment {first!r} is never in the reviewed closed set",
            )
        if "/" in first:
            return (
                "contract-unresolved",
                f"program token {first!r} is path-qualified, never in the reviewed closed set",
            )
        if widening_targets.program_name(first).casefold() in _FORBIDDEN_WRAPPER_TOKENS:
            return (
                "contract-unresolved",
                f"wrapper token {first!r} is never in the reviewed closed set",
            )
        for tok in seg:
            if _is_non_literal(tok):
                return "residual-syntax", f"token {tok!r} is non-literal"
    return None


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
    {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame"}
)

#: Global `git` options whose VALUE must equal the venue (checked in
#: `_resolve_git`) — the only value-taking global flags this table
#: resolves at all. Each is refused if it appears more than once: a second
#: occurrence could retarget git at a different repository AFTER the first
#: value was checked against the venue.
_GIT_VENUE_FLAGS = ("-C", "--git-dir", "--work-tree")
_GIT_VENUE_VALUE_FLAGS = frozenset(_GIT_VENUE_FLAGS)

#: Closed allowlist of git GLOBAL flags that carry no config/executable/
#: namespace-changing semantics — boolean, no value. Any OTHER global flag
#: before the subcommand — `-c`/`--config-env` (arbitrary config override,
#: e.g. `url.insteadOf` rewriting the push destination), `--exec-path` (a
#: different git subprogram directory), `--namespace` (a different ref
#: namespace), or any flag this table has not reviewed — is unresolved,
#: never silently skipped: each can change WHICH resource a subsequent
#: `push` actually touches.
_GIT_SAFE_GLOBAL_BOOLEAN_FLAGS = frozenset({"--no-pager"})

#: Closed allowlist of `git push` flags that carry no force/delete/mirror
#: semantics — any push flag NOT in this set is unresolved. `--follow-tags`
#: (pushes annotated tags reachable from the pushed refs — an extra ref
#: this table did not review) and `-u`/`--set-upstream` (writes the local
#: `.git/config`, a file this table does not resolve) are deliberately
#: excluded: both are unresolved rather than silently treated as a no-op
#: push flag.
_GIT_PUSH_ALLOWED_FLAGS = frozenset(
    {
        "-v", "--verbose", "-q", "--quiet", "-n", "--dry-run",
        "--porcelain", "--progress", "--no-progress",
        "--atomic", "--thin", "--no-thin",
    }
)

#: Flags that make a push force-equivalent (rewrite/replace remote history
#: beyond a plain fast-forward) or delete/prune remote refs — refused
#: identically to `-f`/`--force`, never covered by a plain-push approval.
_GIT_PUSH_FORCE_EQUIV_FLAGS = frozenset(
    {"-f", "--force", "--force-if-includes", "--mirror", "--prune", "--all", "--tags"}
)

#: Closed allowlist of `git branch` flags reviewed as read-only -- boolean
#: only, deliberately: a value-taking flag (`--contains`, `--points-at`,
#: `--format`, `--sort`, ...) is excluded rather than given its own value-
#: skip rule, since the simple per-token closed-set check below would
#: otherwise misread that flag's OWN value as an unrecognized flag. This
#: same exclusion also rejects every positional branch-name argument
#: (create/rename/delete all take one), which is the correct, safe
#: outcome: any git-branch invocation not covered here is unresolved.
_GIT_BRANCH_SAFE_FLAGS = frozenset(
    {
        "-l", "--list", "-a", "--all", "-r", "--remotes",
        "-v", "-vv", "--verbose", "--show-current",
        "--color", "--no-color",
    }
)

#: Closed, PER-SUBCOMMAND allowlist of boolean read-only flags (item (c)) --
#: a flag outside its own subcommand's set is unresolved, never silently
#: allowed through (B2: a prior version checked only `--output`/`--output=`
#: for every readonly subcommand, letting everything else -- `--ext-diff`/
#: `--textconv` (each can invoke an external program via `diff.external`/a
#: configured filter driver), `--show-signature` (invokes `gpg`), an
#: abbreviated `--outpu=` (git supports unique-prefix long-option
#: abbreviation) -- resolve to no effect unreviewed). Deliberately
#: boolean-only, same reasoning as `_GIT_BRANCH_SAFE_FLAGS`: a value-taking
#: flag is excluded rather than given its own value-skip rule, since the
#: per-token closed-set check below would otherwise misread that flag's own
#: value as an unrecognized flag. A literal positional (revision, range, or
#: pathspec) is allowed through for every subcommand here -- read-only by
#: each subcommand's own documented contract.
_GIT_READONLY_SAFE_FLAGS: dict[str, frozenset[str]] = {
    "status": frozenset({
        "-s", "--short", "-b", "--branch", "--long",
        "-v", "--verbose", "--porcelain", "--ignored",
    }),
    "log": frozenset({
        "--oneline", "-p", "--patch", "--stat", "--graph", "--all",
        "--color", "--no-color", "--reverse", "--name-only", "--name-status",
    }),
    "diff": frozenset({
        "--stat", "--name-only", "--name-status",
        "--color", "--no-color", "-p", "--patch", "--cached", "--staged",
    }),
    "show": frozenset({
        "--stat", "--name-only", "--name-status",
        "--color", "--no-color", "-p", "--patch",
    }),
    "rev-parse": frozenset({
        "--verify", "--short", "--abbrev-ref",
        "--is-inside-work-tree", "--show-toplevel",
    }),
    "ls-files": frozenset({
        "-c", "--cached", "-o", "--others", "-m", "--modified",
        "-d", "--deleted", "--full-name",
    }),
    "blame": frozenset({"-w", "--porcelain", "--line-porcelain"}),
}


def _find_git_subcommand(
    operands: list[str],
) -> tuple[str | None, list[str], dict[str, str], str | None]:
    """`(subcommand, remaining_operands, global_flag_values, unresolved_reason)`.
    `global_flag_values` records the literal value of each `-C`/`--git-dir`/
    `--work-tree` global flag seen before the subcommand, so `_resolve_git`
    can check each against the venue. `unresolved_reason` is `None` unless
    the global-flag grammar itself falls outside the closed set this table
    resolves: a global flag that is neither a venue flag nor in
    `_GIT_SAFE_GLOBAL_BOOLEAN_FLAGS` is refused, never silently skipped, and
    a repeated venue flag is refused rather than letting a later occurrence
    retarget git past the checked value."""
    i = 0
    n = len(operands)
    global_values: dict[str, str] = {}
    while i < n and operands[i].startswith("-"):
        tok = operands[i]
        key = tok.split("=", 1)[0]
        if key in _GIT_VENUE_VALUE_FLAGS:
            if key in global_values:
                return None, [], {}, f"git global flag {key!r} repeated: refusing a second retarget"
            if "=" in tok:
                global_values[key] = tok.split("=", 1)[1]
                i += 1
            else:
                if i + 1 >= n:
                    return None, [], {}, f"git global flag {key!r} is missing its value"
                global_values[key] = operands[i + 1]
                i += 2
            continue
        if tok in _GIT_SAFE_GLOBAL_BOOLEAN_FLAGS:
            i += 1
            continue
        return None, [], {}, f"git global flag {tok!r} is outside the reviewed closed set"
    if i >= n:
        return None, [], global_values, None
    return operands[i], operands[i + 1 :], global_values, None


def _resolve_git(operands: list[str], venue_real: str) -> Resolution:
    subcommand, rest, global_values, unresolved_reason = _find_git_subcommand(operands)
    if unresolved_reason is not None:
        return Resolution("unresolved", reason_class="contract-unresolved", reason=unresolved_reason)
    if subcommand is None:
        return Resolution("unresolved", reason_class="contract-unresolved", reason="bare `git` with no subcommand")

    for flag_name in _GIT_VENUE_FLAGS:
        value = global_values.get(flag_name)
        if value is None:
            continue
        value_real = _realpath(_abs_join(value, venue_real))
        if value_real != venue_real:
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason=f"git {flag_name} {value!r} targets a different repository than the venue",
            )

    if subcommand in _GIT_READONLY_SUBCOMMANDS:
        safe_flags = _GIT_READONLY_SAFE_FLAGS[subcommand]
        for tok in rest:
            if tok == "--":
                continue
            if tok.startswith("-"):
                key = tok.split("=", 1)[0]
                if key not in safe_flags:
                    return Resolution(
                        "unresolved",
                        reason_class="contract-unresolved",
                        reason=f"git {subcommand} flag {tok!r} is outside the reviewed closed set",
                    )
                continue
            if _is_non_literal(tok):
                return Resolution(
                    "unresolved",
                    reason_class="residual-syntax",
                    reason=f"git {subcommand} positional {tok!r} is non-literal",
                )
        return Resolution("resolved", resources=[])

    if subcommand == "branch":
        for tok in rest:
            key = tok.split("=", 1)[0]
            if key not in _GIT_BRANCH_SAFE_FLAGS:
                return Resolution(
                    "unresolved",
                    reason_class="contract-unresolved",
                    reason=f"git branch flag or argument {tok!r} is outside the reviewed closed set",
                )
        return Resolution("resolved", resources=[])

    if subcommand != "push":
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason=f"git subcommand {subcommand!r} is not resolved by this table",
        )

    is_force = False
    is_delete = False
    positionals: list[str] = []
    for tok in rest:
        key = tok.split("=", 1)[0]
        if key in _GIT_PUSH_FORCE_EQUIV_FLAGS or tok.startswith("--force-with-lease"):
            is_force = True
        elif key in ("-d", "--delete"):
            is_delete = True
        elif tok.startswith("-"):
            if key not in _GIT_PUSH_ALLOWED_FLAGS:
                return Resolution(
                    "unresolved",
                    reason_class="contract-unresolved",
                    reason=f"git push flag {tok!r} is outside the allowed grammar",
                )
        else:
            positionals.append(tok)

    if is_force or is_delete:
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="a force-or-delete-push (or mirror/prune/all/tags equivalent) is never covered by a plain-push approval",
        )

    # Zero-refspec push (bare `git push`, or `git push <remote>` with no
    # refspec): the ref actually pushed depends on the remote's upstream
    # config and `push.default`, neither visible on the command line — this
    # is unresolved, NOT a "no resources" outcome.
    if len(positionals) < 2:
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="git push with no explicit refspec: the pushed ref depends on git config, not decidable from the command line",
        )

    remote, refspecs = positionals[0], positionals[1:]
    if _is_non_literal(remote):
        return Resolution("unresolved", reason_class="residual-syntax", reason="git push remote is non-literal")

    if refspecs and refspecs[0] == "tag" and len(refspecs) > 1:
        # `git push <remote> tag <tagname>` is git's own distinct shorthand
        # for pushing `refs/tags/<tagname>` -- NOT two independent refspecs
        # named "tag" and "<tagname>". Treating it as a flat refspec list
        # (the code below) would misreport a nonsensical first destination
        # literally named "tag"; refuse instead of resolving it wrong.
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="git push <remote> tag <name> is a distinct push shorthand this table does not resolve",
        )

    push_resources: list[resources.Resource] = []
    for refspec in refspecs:
        if _is_non_literal(refspec):
            return Resolution(
                "unresolved", reason_class="residual-syntax", reason="git push refspec is non-literal"
            )
        if refspec.startswith("+"):
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason="a `+`-prefixed refspec is a force-or-delete-push variant, never covered by a plain-push approval",
            )
        if refspec.startswith(":"):
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason="a `:branch` refspec is a force-or-delete-push variant: it deletes the remote ref",
            )
        dst = refspec.split(":", 1)[1] if ":" in refspec else refspec
        if not dst:
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason="an empty destination refspec is a force-or-delete-push variant: it deletes the remote ref",
            )
        push_resources.append(resources.VcsRefResource(remote, dst, "push"))
    return Resolution("resolved", resources=push_resources)


#: pytest's closed flag grammar (item (c)): `-k` is the only value-taking
#: flag reviewed here -- `-p` and `--tb=` each get their own dedicated
#: check below since their acceptable VALUES are themselves a closed set,
#: not merely "any value".
_PYTEST_VALUE_FLAGS = frozenset({"-k"})
_PYTEST_BOOLEAN_FLAGS = frozenset({"-q", "-x", "-v", "-s"})
_PYTEST_ALLOWED_TB_VALUES = frozenset({"short", "line", "no"})


def _resolve_pytest(operands: list[str], venue_real: str) -> Resolution:
    """pytest's own closed flag/path grammar (item (c) of the closed command
    grammar): a fixed set of boolean flags, `-k EXPR`, `-p no:cacheprovider`
    exactly, and `--tb=` limited to its three plain-text values. A test-path
    positional must be literal, relative, and never escape the venue via a
    `..` segment -- an absolute path or a `..` segment could name a file
    this table does not review. Anything else is unresolved; a clean parse
    resolves to a write on the whole venue subtree, since pytest's own
    plugins (cache, coverage, junit-xml via a config file) can write
    anywhere under it without naming a path on the command line."""
    i = 0
    n = len(operands)
    while i < n:
        tok = operands[i]
        if tok in _PYTEST_BOOLEAN_FLAGS:
            i += 1
            continue
        if tok in _PYTEST_VALUE_FLAGS:
            if i + 1 >= n:
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"pytest {tok} is missing its value",
                )
            i += 2
            continue
        if tok == "-p":
            if i + 1 >= n or operands[i + 1] != "no:cacheprovider":
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason="pytest -p is only reviewed for the exact value 'no:cacheprovider'",
                )
            i += 2
            continue
        if tok.startswith("--tb="):
            value = tok.split("=", 1)[1]
            if value not in _PYTEST_ALLOWED_TB_VALUES:
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"pytest --tb={value!r} is outside the reviewed closed set",
                )
            i += 1
            continue
        if tok.startswith("-"):
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason=f"pytest flag {tok!r} is outside the reviewed closed set",
            )
        if _is_non_literal(tok) or os.path.isabs(tok):
            return Resolution(
                "unresolved", reason_class="residual-syntax",
                reason="pytest test path is non-literal or absolute",
            )
        if ".." in tok.replace("\\", "/").split("/"):
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason="pytest test path escapes the venue via '..'",
            )
        i += 1
    return Resolution("resolved", resources=[resources.FileResource(venue_real, "write")])


def _script_and_argv(operands: list[str]) -> tuple[str | None, list[str]]:
    """The interpreter's first non-flag operand is "the script" (mirrors
    `_compute_identity`'s own `script_candidate` convention); everything
    after it is the script's OWN argv, flags included — an interpreter-level
    flag before the script (`python3 -u script.py --branch x`) is skipped,
    not mistaken for one of the script's own arguments. `-c`/`-m` (whose
    VALUE is inline code / a module name, never a script path) are handled
    separately by `_resolve_interpreter` before this is ever called."""
    for i, tok in enumerate(operands):
        if not tok.startswith("-"):
            return tok, operands[i + 1 :]
    return None, []


def _resolve_interpreter(operands: list[str], venue_real: str) -> Resolution:
    """Consult the script-effects registry (`script_effects.py`) for a
    reviewed, digest-pinned entry matching the script being run. `-c`
    (inline code) is always unresolved; `-m pytest` is special-cased to the
    same whole-venue-write verdict as bare `pytest` (finding: `pytest` via
    `python3 -m pytest` resolves the same as `pytest` itself); any other
    `-m MODULE` is unresolved. Any OTHER dash-prefixed token before the
    script -- a glued short form (`-cCODE`, `-mMOD`), a value-taking flag
    (`-W`, `-X`), or anything else -- is refused outright rather than
    skipped: the closed grammar here recognizes exactly `-c`/`--command`/
    `--command=`/`-m`/`-m=` and nothing else, so an unreviewed flag can
    never silently shift which token this resolver treats as "the script"
    (B1: a prior version skipped unrecognized dash tokens, letting
    `-mMOD scripts/land-branch.py` or `-cCODE -m pytest` be mistaken for a
    plain script/pytest invocation). No entry — or no script at all — falls
    back to the original unresolved verdict; the caller (`resolve_command`)
    computes content-bound identity for that case regardless of what this
    function returns for `identity`."""
    from . import script_effects  # deferred: see script_effects.py's own docstring

    for i, tok in enumerate(operands):
        if tok in ("-c", "--command") or tok.startswith("--command="):
            return Resolution(
                "unresolved",
                reason_class="adhoc-undeclared",
                reason="python -c/--command runs inline code passed on the command line, never resolved by this table",
            )
        if tok == "-m" or tok.startswith("-m="):
            has_equals = "=" in tok
            module = tok.split("=", 1)[1] if has_equals else (operands[i + 1] if i + 1 < len(operands) else None)
            if module == "pytest":
                pytest_argv = operands[i + 1 :] if has_equals else operands[i + 2 :]
                return _resolve_pytest(pytest_argv, venue_real)
            return Resolution(
                "unresolved",
                reason_class="adhoc-undeclared",
                reason=f"python -m {module!r} runs a module invocation this table does not resolve",
            )
        if tok.startswith("-"):
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason=f"python flag {tok!r} before the script is outside the reviewed closed set (-c/--command/-m only)",
            )
        break

    script, script_argv = _script_and_argv(operands)
    if script is not None:
        resolved = script_effects.resolve_script(script, script_argv, venue_real)
        if resolved is not None:
            return resolved
    return Resolution("unresolved", reason_class="contract-unresolved", reason="no script-effects registry entry")


_CUSTOM_RESOLVERS = {
    "git": lambda operands, venue_real: _resolve_git(operands, venue_real),
    "pytest": lambda operands, venue_real: _resolve_pytest(operands, venue_real),
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
    whole-subtree residual. Closed-world throughout: `_check_command_shape`
    first rejects anything outside the one reviewed command shape (item
    (a)), then every segment's program must itself be a table entry whose
    effect this function knows how to dispatch -- a command that is 90%
    readable and 10% opaque is not 90% approvable, so the first
    undecidable segment (an unlisted program, an unlisted flag, an
    unresolved resolver verdict) makes the WHOLE command unresolved."""
    venue_real = _realpath(venue)
    table = contract_table if contract_table is not None else load_contract_table()

    stripped_text = shell_tokens.strip_heredoc_bodies(text)
    if _has_nested_execution(stripped_text):
        return _unresolved_with_identity(
            text, venue_real, "residual-syntax", "command substitution, a subshell, or process substitution hides nested work"
        )

    shape_violation = _check_command_shape(stripped_text)
    if shape_violation is not None:
        reason_class, reason = shape_violation
        return _unresolved_with_identity(text, venue_real, reason_class, reason)

    try:
        tokens = shell_tokens.separator_exact_split(_strip_standalone_stderr_merge(stripped_text))
    except Exception:
        return _unresolved_with_identity(text, venue_real, "residual-syntax", "command line failed to tokenize")

    segments = list(bash_write_targets.split_segments(tokens))
    if not segments:
        return Resolution("resolved", resources=[])

    all_resources: list[resources.Resource] = []
    for seg in segments:
        prog = widening_targets.program_name(seg[0])
        operands = seg[1:]

        entry = table.get(prog)
        if entry is None:
            return _unresolved_with_identity(
                text, venue_real, "unknown-program", f"{prog!r} has no tool_contracts.toml entry"
            )

        if entry.effect == "unresolved":
            return _unresolved_with_identity(text, venue_real, "contract-unresolved", entry.reason or "")

        if entry.effect == "none":
            assert entry.safe_flags is not None  # enforced by load_contract_table
            if "*" not in entry.safe_flags:
                for tok in operands:
                    if not tok.startswith("-") or tok == "-":
                        continue
                    key = tok.split("=", 1)[0]
                    if key not in entry.safe_flags:
                        return _unresolved_with_identity(
                            text, venue_real, "contract-unresolved",
                            f"{prog!r} flag {tok!r} is outside the reviewed safe-flag set",
                        )
            continue

        if entry.effect == "resolver":
            resolver = _CUSTOM_RESOLVERS.get(prog)
            if resolver is None:  # pragma: no cover - table/resolver drift guard
                raise ValueError(f"tool_contracts.toml declares {prog!r} as effect='resolver' with no matching resolver")
            seg_resolution = resolver(operands, venue_real)
            if seg_resolution.status == "unresolved":
                return _unresolved_with_identity(
                    text, venue_real, seg_resolution.reason_class or "contract-unresolved", seg_resolution.reason or ""
                )
            all_resources.extend(seg_resolution.resources)
            continue

        # _VALID_EFFECTS admits only "none"/"unresolved"/"resolver", each
        # handled above; this branch guards against a future entry naming an
        # effect this function has no dispatch for, and fails closed rather
        # than silently treating it as a no-op.
        return _unresolved_with_identity(
            text, venue_real, "contract-unresolved",
            f"program {prog!r} declares effect={entry.effect!r}, which this resolver does not dispatch",
        )

    return Resolution("resolved", resources=all_resources)
