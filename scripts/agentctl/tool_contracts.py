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
import re
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


def _real_program(
    seg: list[str], *, assignment_names: list[str] | None = None
) -> tuple[str, list[str]] | None:
    """`(casefolded_basename, operand_tokens)` of the real (wrapper-stripped)
    program a segment invokes, or `None` for an empty/assignment-only
    segment. When `assignment_names` is passed, every environment-variable
    NAME stripped away to reach that program (a leading bare `KEY=VALUE`, or
    one passed to an `env` wrapper) is appended to it -- see
    `widening_targets.strip_wrappers`."""
    stripped = widening_targets.strip_wrappers(seg, assignment_names=assignment_names)
    if not stripped:
        return None
    return widening_targets.program_name(stripped[0]).casefold(), stripped[1:]


#: Environment-variable NAMEs a leading assignment prefix (`NAME=value ...`,
#: or `env NAME=value ...`) may set without collapsing the whole command to
#: unresolved: closed-world by construction -- an env assignment can change
#: ANY program's behavior in ways this module cannot see (a different
#: config file, a different PATH, an injected interpreter flag via a
#: `*_OPTS`-style variable), so the default is refusal, and a name is added
#: here only after a specific reviewed case needs it. Empty today: no case
#: has been reviewed yet.
_REVIEWED_BENIGN_ENV_ASSIGNMENTS: frozenset[str] = frozenset()

_BARE_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _leading_bare_assignments(seg: list[str]) -> tuple[list[str], list[str]]:
    """Consume a leading run of bare `NAME=value` tokens (no dash) from
    `seg`, returning `(remaining_tokens, assignment_names)` -- mirrors the
    prefix `widening_targets.strip_wrappers` also strips, kept as an
    independent, minimal local re-implementation here since the closed-
    world wrapper walk below (`_strip_closed_wrappers`) must not reuse that
    module's permissive wrapper handling for the rest of the token
    stream."""
    names: list[str] = []
    i = 0
    while i < len(seg) and _BARE_ASSIGNMENT_RE.match(seg[i]):
        names.append(seg[i].split("=", 1)[0])
        i += 1
    return seg[i:], names


#: Value-taking flag reviewed as safe for `nice`; the legacy bare `-N`
#: adjustment shorthand (`nice -10 cmd`) is indistinguishable from an
#: unreviewed flag without a much larger grammar, so it is refused rather
#: than guessed.
_NICE_VALUE_FLAGS = frozenset({"-n", "--adjustment"})

#: Flags reviewed as safe for `timeout`; `-k`/`--kill-after` and
#: `-s`/`--signal` take a value, the rest are boolean.
_TIMEOUT_VALUE_FLAGS = frozenset({"-k", "--kill-after", "-s", "--signal"})
_TIMEOUT_BOOLEAN_FLAGS = frozenset({"--preserve-status", "--foreground", "-v", "--verbose"})


def _strip_closed_wrappers(
    seg: list[str], venue_real: str
) -> tuple[list[str], list[resources.Resource]] | Resolution:
    """Closed-world wrapper stripping for EFFECT resolution.

    Unlike `widening_targets.strip_wrappers` (permissive -- its only job is
    locating the real program name for identity, see `_real_program`), this
    walk only steps past a wrapper token when its own effect on THIS
    invocation has been reviewed and is provably neutral. It steps past
    `nice`/`timeout`/`nohup`/`env` one at a time, each with its own closed
    flag grammar; anything else -- an unrecognized wrapper token, an
    unreviewed flag on a reviewed one -- stops the walk WITHOUT consuming
    the token, so the unrecognized token itself becomes the dispatched
    program name and refuses via "no tool_contracts.toml entry" (none of
    these four wrapper names has a table entry of its own, except `env` run
    bare, which is deliberately still reviewed below). `eval`, `xargs`,
    `time`, `sudo`, `doas`, `flock`, and every other wrapper token this
    function does not name therefore ALWAYS falls through to that same
    unknown-program refusal -- there is no safe subset for them."""
    head = list(seg)
    extra: list[resources.Resource] = []
    while head:
        name = widening_targets.program_name(head[0]).casefold()
        rest = head[1:]

        if name == "nice":
            i = 0
            while i < len(rest) and rest[i].startswith("-") and rest[i] != "-":
                tok = rest[i]
                key = tok.split("=", 1)[0]
                if key in _NICE_VALUE_FLAGS:
                    i += 1 if "=" in tok else 2
                    continue
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"nice flag {tok!r} is outside the reviewed closed set",
                )
            head = rest[i:]
            continue

        if name == "timeout":
            i = 0
            while i < len(rest) and rest[i].startswith("-") and rest[i] != "-":
                tok = rest[i]
                key = tok.split("=", 1)[0]
                if key in _TIMEOUT_VALUE_FLAGS:
                    i += 1 if "=" in tok else 2
                    continue
                if tok in _TIMEOUT_BOOLEAN_FLAGS:
                    i += 1
                    continue
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"timeout flag {tok!r} is outside the reviewed closed set",
                )
            if i >= len(rest):
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason="timeout with no DURATION/command operand",
                )
            head = rest[i + 1 :]  # rest[i] is DURATION, consumed unexamined
            continue

        if name == "nohup":
            if rest and rest[0].startswith("-") and rest[0] != "-":
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"nohup flag {rest[0]!r} is outside the reviewed closed set",
                )
            if not rest:
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason="nohup with no command operand",
                )
            extra.append(resources.FileResource(os.path.join(venue_real, "nohup.out"), "write"))
            head = rest
            continue

        if name == "env":
            stripped_rest, names = _leading_bare_assignments(rest)
            unreviewed = [n for n in names if n not in _REVIEWED_BENIGN_ENV_ASSIGNMENTS]
            if unreviewed:
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"unreviewed environment assignment(s) {unreviewed!r} passed to env: not provably behavior-neutral",
                )
            if stripped_rest and stripped_rest[0].startswith("-") and stripped_rest[0] != "-":
                return Resolution(
                    "unresolved", reason_class="contract-unresolved",
                    reason=f"env flag {stripped_rest[0]!r} is outside the reviewed closed set -- env with any option flag is always unresolved",
                )
            if not stripped_rest:
                # A bare `env` (or `env NAME=value ...` with nothing left to
                # run) is not a wrapper at all here -- it IS the program;
                # fall through to its own contract-table entry.
                break
            head = stripped_rest
            continue

        break

    return head, extra


def _resolve_effect_head(
    seg: list[str], venue_real: str
) -> tuple[list[str], list[resources.Resource]] | None | Resolution:
    """The closed-world equivalent of `_real_program`, used ONLY for effect
    resolution (never for identity, which keeps using the permissive
    `_real_program`/`widening_targets.strip_wrappers` unchanged -- identity
    capture is not a security-relevant gating decision). Strips a leading
    bare `NAME=value` assignment run, then a closed-world wrapper prefix
    (`_strip_closed_wrappers`); returns `None` for an empty/assignment-only
    segment, a `Resolution` to refuse the whole command, or the dispatched
    `(stripped_head_tokens, extra_resources)`."""
    rest, names = _leading_bare_assignments(seg)
    unreviewed = [n for n in names if n not in _REVIEWED_BENIGN_ENV_ASSIGNMENTS]
    if unreviewed:
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason=f"unreviewed environment assignment(s) {unreviewed!r}: not provably behavior-neutral",
        )
    if not rest:
        return None

    strip_result = _strip_closed_wrappers(rest, venue_real)
    if isinstance(strip_result, Resolution):
        return strip_result
    head, extra = strip_result
    if not head:
        return None
    return head, extra


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
        if any(tok == "--output" or tok.startswith("--output=") for tok in rest):
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason=f"git {subcommand} --output writes to a file, not resolved by this table",
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


_FIND_EXEC_FLAGS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)


def _resolve_find(operands: list[str]) -> Resolution:
    if any(tok in _FIND_EXEC_FLAGS for tok in operands):
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="find -exec/-execdir/-ok/-okdir runs an arbitrary command per matched file, and -delete/-fprint*/-fls write, per matched file",
        )
    return Resolution("resolved", resources=[])


def _resolve_awk(operands: list[str]) -> Resolution:
    return Resolution(
        "unresolved",
        reason_class="contract-unresolved",
        reason="an awk program's own `print > \"file\"` can write anywhere the script names",
    )


def _resolve_pytest(venue_real: str) -> Resolution:
    return Resolution("resolved", resources=[resources.FileResource(venue_real, "write")])


def _resolve_rg(operands: list[str]) -> Resolution:
    if any(tok == "--pre" or tok.startswith("--pre=") for tok in operands):
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="rg --pre runs an arbitrary preprocessor command on every searched file",
        )
    return Resolution("resolved", resources=[])


def _resolve_date(operands: list[str]) -> Resolution:
    if any(tok in ("-s", "--set") or tok.startswith("--set=") for tok in operands):
        return Resolution(
            "unresolved",
            reason_class="contract-unresolved",
            reason="date -s/--set changes the system clock, not a resource this table types",
        )
    return Resolution("resolved", resources=[])


#: `sed` options that take the script text as their VALUE (`-e`/
#: `--expression`) or point at a script FILE (`-f`/`--file`) rather than
#: giving the script inline.
_SED_INLINE_SCRIPT_OPTS = frozenset({"-e", "--expression"})
_SED_SCRIPT_FILE_OPTS = frozenset({"-f", "--file"})

#: One sed address (a line number, `$`, or a `/regex/`) -- what a `w`/`W`/`e`
#: detector has to look PAST before it can see the actual command letter, or
#: an address like `3w` would be misread as the command `3`.
_SED_ADDR = r"(?:\d+|\$|/(?:\\.|[^/\\])*/)"
_SED_ADDR_PREFIX_RE = re.compile(rf"^\s*{_SED_ADDR}(?:\s*,\s*{_SED_ADDR})?\s*!?\s*")

#: Commands that neither write to an arbitrary path nor execute a command --
#: enumerated so the fail-closed default below only applies to a command
#: letter this function does not actually recognize, not to every ordinary
#: sed command that happens to not be `w`/`W`/`e`. Deliberately excludes
#: `{`/`}`: a `{...}` block's BODY can itself contain a `w`/`W`/`e` command,
#: and this function does not recursively parse block contents, so a
#: segment opening with `{` falls through to the catch-all `True` below
#: rather than being silently treated as safe.
_SED_SAFE_COMMANDS = frozenset("pdnNgGhHxlqQ=btT:#yzFDPrR")

#: Closed allowlist of sed flags this resolver recognizes as behavior-
#: neutral for the w/W/e determination -- boolean, no value. Any OTHER
#: flag -- including a GNU long-option ABBREVIATION of `--expression`/
#: `--file` (e.g. `--expr=...`/`--fil=...`, which getopt would accept but
#: this table deliberately does not recognize) or a bundled short-option
#: form (`-ne` for `-n -e`) -- is refused rather than silently skipped: an
#: unrecognized flag could itself smuggle a script value this resolver
#: never inspects.
_SED_SAFE_BOOLEAN_FLAGS = frozenset(
    {
        "-n", "--quiet", "--silent",
        "-E", "-r", "--regexp-extended",
        "-s", "--separate",
        "-u", "--unbuffered",
        "-z", "--null-data",
        "--posix", "--sandbox",
    }
)

#: A token that looks like a sed `-i` backup-suffix argument (`.bak`, `.`,
#: `.orig-1`) rather than the start of a script -- used only to decide
#: whether a bare `-i` followed by this token is GNU/BSD-ambiguous (see
#: `_resolve_sed`), never to accept or reject a script on its own.
_SED_SUFFIX_LIKE_RE = re.compile(r"^\.[A-Za-z0-9_.-]*$")


def _sed_segment_writes_or_execs(segment: str) -> bool:
    """`True` iff `segment` -- one `;`/newline-delimited sed command --
    is a `w`/`W` (write) or `e` (execute) command, or an `s///` substitute
    carrying a trailing `w` (write-to-file) flag. Strips a leading address
    (`3w file`, `/re/,$e cmd`, ...) first, so a `w`/`W`/`e` occurring inside
    ordinary substitute TEXT (e.g. the letter `e` in a replacement `bye`) is
    never mistaken for the COMMAND. Fail-closed on anything not positively
    cleared: an address form this does not recognize (a custom `\\cREGEXc`
    delimiter, a GNU step address `first~step`) leaves the segment
    unstripped, and a command letter outside `_SED_SAFE_COMMANDS` falls
    through to the catch-all `True` at the end."""
    rest = _SED_ADDR_PREFIX_RE.sub("", segment, count=1).strip()
    if not rest:
        return False
    if rest[0] in ("w", "W"):
        return True
    if rest[0] == "e" and (len(rest) == 1 or not rest[1].isalnum()):
        return True
    if rest[0] == "s" and len(rest) > 1:
        delim = rest[1]
        parts = re.split(rf"(?<!\\){re.escape(delim)}", rest[2:], maxsplit=2)
        if len(parts) != 3:
            # Malformed or ambiguous (e.g. this ';'-split cut a delimiter
            # mid-command) -- not provably free of a `w` flag.
            return True
        flags = re.split(r"[\s;]", parts[2], maxsplit=1)[0]
        # `e` on a substitute (`s/.../.../e`) executes the resulting line
        # as a shell command -- an exec effect exactly like a bare `e`
        # command, not merely a write.
        return "w" in flags or "e" in flags
    if rest[0] in _SED_SAFE_COMMANDS:
        return False
    return True


def _sed_script_writes_or_execs(combined: str) -> bool:
    """`True` iff any `;`/newline-delimited segment of `combined` is a
    write/exec command per `_sed_segment_writes_or_execs`."""
    return any(
        _sed_segment_writes_or_execs(segment)
        for line in combined.split("\n")
        for segment in line.split(";")
    )


def _resolve_sed(operands: list[str], venue_real: str) -> Resolution:
    """A `w`/`W` (write) or `e` (execute) command — or an `s///e` exec
    flag — embedded IN the script writes/executes regardless of `-i`, so
    this resolver ALWAYS parses and checks the script, `-i` or not; `-i`/
    `--in-place`'s own file-rewrite effect is captured separately and
    unconditionally by `bash_write_targets.segment_write_target` (see
    `resolve_command`). Fail-closed: unresolved unless the script is
    PROVABLY free of `w`/`W`/`e` — and a `-f`/`--file` script (read from a
    file this resolver does not inspect) can never be proven free, so it
    is always unresolved.

    Flag parsing is closed-world too: only the exact forms `-e`/
    `--expression`, `-f`/`--file` (each with an optional `=value`), `-i`/
    `-i<suffix>`, `--in-place`/`--in-place=<suffix>`, and the boolean
    flags in `_SED_SAFE_BOOLEAN_FLAGS` are recognized — a GNU long-option
    ABBREVIATION (`--expr=`, `--fil=`) or a bundled short form (`-ne` for
    `-n -e`) is a flag-shaped token this table does not recognize, and is
    refused rather than silently skipped, since either could smuggle a
    script value this resolver never inspects."""
    scripts: list[str] = []
    positionals: list[str] = []
    used_script_file = False
    used_inline = False
    i = 0
    n = len(operands)
    while i < n:
        tok = operands[i]
        if "=" in tok and tok.split("=", 1)[0] in (_SED_INLINE_SCRIPT_OPTS | _SED_SCRIPT_FILE_OPTS):
            key, val = tok.split("=", 1)
            if key in _SED_SCRIPT_FILE_OPTS:
                used_script_file = True
            else:
                used_inline = True
                scripts.append(val)
            i += 1
            continue
        if tok in _SED_SCRIPT_FILE_OPTS:
            used_script_file = True
            i += 2
            continue
        if tok in _SED_INLINE_SCRIPT_OPTS:
            used_inline = True
            if i + 1 < n:
                scripts.append(operands[i + 1])
            i += 2
            continue
        if tok == "--in-place" or tok.startswith("--in-place="):
            i += 1
            continue
        if tok == "-i":
            # A bare `-i` parses differently under GNU sed (no separate
            # argument -- an optional suffix, if any, must be glued on:
            # `-i.bak`) and BSD/macOS sed (a MANDATORY separate suffix
            # argument, which may be an empty string: `-i ''`). Whether the
            # next token is "the BSD suffix" or "the first positional (the
            # script, under GNU semantics)" is therefore not decidable from
            # the command line alone whenever that next token is empty or
            # looks like a suffix rather than script text -- guessing either
            # reading risks locating the wrong text as the script and
            # silently missing an embedded w/W/e command.
            nxt = operands[i + 1] if i + 1 < n else None
            if nxt is not None and (nxt == "" or _SED_SUFFIX_LIKE_RE.match(nxt)):
                return Resolution(
                    "unresolved",
                    reason_class="contract-unresolved",
                    reason="sed -i followed by an empty or suffix-like token is ambiguous between GNU and BSD sed -- not provably locating the real script",
                )
            i += 1
            continue
        if tok.startswith("-i") and not tok.startswith("--"):
            # `-i<suffix>` — GNU sed attaches an optional suffix directly
            # with no separator, so this token IS the whole flag.
            i += 1
            continue
        if tok in _SED_SAFE_BOOLEAN_FLAGS:
            i += 1
            continue
        if tok.startswith("-"):
            return Resolution(
                "unresolved",
                reason_class="contract-unresolved",
                reason=f"sed flag {tok!r} is outside the reviewed closed set",
            )
        positionals.append(tok)
        i += 1

    if used_script_file:
        return Resolution(
            "unresolved",
            reason_class="adhoc-undeclared",
            reason="sed -f/--file reads its script from a file this resolver does not inspect -- not provably free of a w/W/e command",
        )
    if not used_inline:
        # POSIX/GNU convention: absent -e/-f, the first positional operand
        # IS the script (everything after it is a file operand).
        if positionals:
            scripts.append(positionals[0])

    combined = "\n".join(scripts)
    if _sed_script_writes_or_execs(combined):
        return Resolution(
            "unresolved",
            reason_class="adhoc-undeclared",
            reason="sed script may contain a w/W (write) or e (execute) command and is not provably free of one",
        )
    return Resolution("resolved", resources=[])


#: Closed grammar for `mv` -- a small reviewed boolean set plus
#: `-t`/`--target-directory`; a backup flag (`-b`/`--backup`,
#: `-S`/`--suffix`) is refused outright since it can write an EXTRA file
#: (the backup copy) this table does not model.
_MV_BOOLEAN_FLAGS = frozenset({"-f", "--force", "-i", "--interactive", "-n", "--no-clobber", "-v", "--verbose"})
_MV_TARGET_DIR_FLAGS = frozenset({"-t", "--target-directory"})


def _resolve_mv(operands: list[str], venue_real: str) -> Resolution:
    """POSIX mv REMOVES its source(s) in addition to writing the
    destination; the destination side is already correctly resolved by
    `lib.bash_write_targets.segment_write_target` (reviewed for the
    canon-readonly guard, and still consulted unconditionally by
    `resolve_command` on the wrapper-stripped segment). This resolver's own
    job is (a) refuse any flag outside the small reviewed set below, since
    an unrecognized one could itself retarget or add a write this table
    does not see, and (b) add each SOURCE as its own write resource -- the
    piece the destination-only computation misses entirely."""
    target_dir: str | None = None
    positionals: list[str] = []
    i = 0
    n = len(operands)
    while i < n:
        tok = operands[i]
        key = tok.split("=", 1)[0]
        if key in _MV_TARGET_DIR_FLAGS:
            if "=" in tok:
                target_dir = tok.split("=", 1)[1]
                i += 1
            else:
                if i + 1 >= n:
                    return Resolution(
                        "unresolved", reason_class="contract-unresolved",
                        reason="mv -t/--target-directory is missing its value",
                    )
                target_dir = operands[i + 1]
                i += 2
            continue
        if tok in _MV_BOOLEAN_FLAGS:
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason=f"mv flag {tok!r} is outside the reviewed closed set",
            )
        positionals.append(tok)
        i += 1

    sources = positionals if target_dir is not None else positionals[:-1]
    if not sources or (target_dir is None and len(positionals) < 2):
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason="mv requires at least a source and a destination",
        )
    if any(_is_non_literal(tok) for tok in sources):
        return Resolution("unresolved", reason_class="residual-syntax", reason="mv source operand is non-literal")

    return Resolution(
        "resolved",
        resources=[resources.FileResource(_abs_join(src, venue_real), "write") for src in sources],
    )


#: Closed grammar for `install`; `-d`/`--directory` toggles directory-
#: creation mode, handled specially below since it is not an ordinary copy.
_INSTALL_BOOLEAN_FLAGS = frozenset({"-v", "--verbose", "-p", "--preserve-timestamps", "-C", "--compare"})
_INSTALL_VALUE_FLAGS = frozenset({"-m", "--mode", "-o", "--owner", "-g", "--group"})
_INSTALL_TARGET_DIR_FLAGS = frozenset({"-t", "--target-directory"})
_INSTALL_DIR_FLAGS = frozenset({"-d", "--directory"})


def _resolve_install(operands: list[str], venue_real: str) -> Resolution:
    """GNU install's `-d`/`--directory` mode treats EVERY positional as its
    own target directory to create -- a shape `lib.bash_write_targets`'
    shared cp/mv/install dispatch does not special-case (it reads the last
    positional as a destination and the rest as sources, exactly as wrong
    for `-d` as it would be for an ordinary two-file copy), so this
    resolver computes dir-mode's targets itself rather than relying on the
    destination-only computation `resolve_command` also consults. Outside
    `-d` mode, install's own contract is a plain copy with no source
    removal and no case `bash_write_targets` does not already cover -- this
    resolver only enforces the closed flag grammar and defers the
    destination target to that shared computation."""
    dir_mode = False
    target_dir: str | None = None
    positionals: list[str] = []
    i = 0
    n = len(operands)
    while i < n:
        tok = operands[i]
        key = tok.split("=", 1)[0]
        if key in _INSTALL_DIR_FLAGS:
            dir_mode = True
            i += 1
            continue
        if key in _INSTALL_TARGET_DIR_FLAGS:
            if "=" in tok:
                target_dir = tok.split("=", 1)[1]
                i += 1
            else:
                if i + 1 >= n:
                    return Resolution(
                        "unresolved", reason_class="contract-unresolved",
                        reason="install -t/--target-directory is missing its value",
                    )
                target_dir = operands[i + 1]
                i += 2
            continue
        if key in _INSTALL_VALUE_FLAGS:
            if "=" in tok:
                i += 1
            else:
                if i + 1 >= n:
                    return Resolution(
                        "unresolved", reason_class="contract-unresolved",
                        reason=f"install {key} is missing its value",
                    )
                i += 2
            continue
        if tok in _INSTALL_BOOLEAN_FLAGS:
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason=f"install flag {tok!r} is outside the reviewed closed set",
            )
        positionals.append(tok)
        i += 1

    non_literal_check = positionals + ([target_dir] if target_dir else [])
    if any(_is_non_literal(tok) for tok in non_literal_check):
        return Resolution("unresolved", reason_class="residual-syntax", reason="install operand is non-literal")

    if dir_mode:
        if not positionals:
            return Resolution(
                "unresolved", reason_class="contract-unresolved",
                reason="install -d/--directory with no directory operand",
            )
        return Resolution(
            "resolved",
            resources=[resources.FileResource(_abs_join(p, venue_real), "write") for p in positionals],
        )

    if target_dir is not None and not positionals:
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason="install -t/--target-directory with no source operand",
        )
    if target_dir is None and len(positionals) < 2:
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason="install requires at least a source and a destination",
        )
    return Resolution("resolved", resources=[])


def _resolve_cp(operands: list[str], venue_real: str) -> Resolution:
    """`--parents` preserves each source's own directory structure under
    the destination, so the actual leaf path cp writes is not the plain
    basename-under-dest join `lib.bash_write_targets.segment_write_target`
    computes for an ordinary copy -- refused rather than reported wrong.
    Absent `--parents`, cp's own contract is exactly what that shared,
    already-reviewed dispatch resolves; this resolver adds nothing to it."""
    if any(tok == "--parents" for tok in operands):
        return Resolution(
            "unresolved", reason_class="contract-unresolved",
            reason="cp --parents preserves each source's own directory structure under the destination -- not the plain basename-under-dest join this table otherwise resolves",
        )
    return Resolution("resolved", resources=[])


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
    `-m MODULE` is unresolved. No entry — or no script at all — falls back
    to the original unresolved verdict; the caller (`resolve_command`)
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
            module = tok.split("=", 1)[1] if "=" in tok else (operands[i + 1] if i + 1 < len(operands) else None)
            if module == "pytest":
                return _resolve_pytest(venue_real)
            return Resolution(
                "unresolved",
                reason_class="adhoc-undeclared",
                reason=f"python -m {module!r} runs a module invocation this table does not resolve",
            )
        if not tok.startswith("-"):
            break

    script, script_argv = _script_and_argv(operands)
    if script is not None:
        resolved = script_effects.resolve_script(script, script_argv, venue_real)
        if resolved is not None:
            return resolved
    return Resolution("unresolved", reason_class="contract-unresolved", reason="no script-effects registry entry")


_CUSTOM_RESOLVERS = {
    "git": lambda operands, venue_real: _resolve_git(operands, venue_real),
    "find": lambda operands, venue_real: _resolve_find(operands),
    "awk": lambda operands, venue_real: _resolve_awk(operands),
    "gawk": lambda operands, venue_real: _resolve_awk(operands),
    "nawk": lambda operands, venue_real: _resolve_awk(operands),
    "mawk": lambda operands, venue_real: _resolve_awk(operands),
    "pytest": lambda operands, venue_real: _resolve_pytest(venue_real),
    "python3": lambda operands, venue_real: _resolve_interpreter(operands, venue_real),
    "python": lambda operands, venue_real: _resolve_interpreter(operands, venue_real),
    "sed": lambda operands, venue_real: _resolve_sed(operands, venue_real),
    "rg": lambda operands, venue_real: _resolve_rg(operands),
    "date": lambda operands, venue_real: _resolve_date(operands),
    "mv": lambda operands, venue_real: _resolve_mv(operands, venue_real),
    "install": lambda operands, venue_real: _resolve_install(operands, venue_real),
    "cp": lambda operands, venue_real: _resolve_cp(operands, venue_real),
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
            text, venue_real, "residual-syntax", "command substitution, a subshell, or process substitution hides nested work"
        )

    try:
        tokens = shell_tokens.separator_exact_split(stripped_text)
    except Exception:
        return _unresolved_with_identity(text, venue_real, "residual-syntax", "command line failed to tokenize")

    segments = list(bash_write_targets.split_segments(tokens))
    if not segments:
        return Resolution("resolved", resources=[])

    all_resources: list[resources.Resource] = []
    for seg in segments:
        effect_head = _resolve_effect_head(seg, venue_real)
        if isinstance(effect_head, Resolution):
            return _unresolved_with_identity(
                text, venue_real, effect_head.reason_class or "contract-unresolved", effect_head.reason or ""
            )
        if effect_head is None:
            # Assignment-only segment (e.g. `FOO=bar`), or a wrapper prefix
            # that consumed the whole segment with nothing left to run: no
            # program, no effect of its own.
            continue
        head, extra_resources = effect_head
        all_resources.extend(extra_resources)

        # Write candidates (a generic `>`/`>>` redirect, or a reviewed
        # writes-operands verb) are computed on the STRIPPED head, not the
        # raw segment -- a wrapper prefix `resolve_command` has already
        # reviewed as neutral (nice, timeout, ...) must not also hide the
        # wrapped command's own write target from this generic scan.
        write_candidates = bash_write_targets.segment_write_target(head, venue_real)

        prog = widening_targets.program_name(head[0]).casefold()
        operands = head[1:]

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
            # A resolver-dispatched program may STILL redirect its stdout
            # (`git status > f`); the generic redirect candidates below are
            # collected regardless of the resolver's own verdict.

        # entry.effect in ("none", "writes-operands"): the program's OWN
        # write targets (if any) are exactly what bash_write_targets already
        # found in `write_candidates` — no further table dispatch needed.
        for candidate in write_candidates:
            all_resources.append(resources.FileResource(candidate, "write"))

    return Resolution("resolved", resources=all_resources)
