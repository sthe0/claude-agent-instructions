"""The sole authority on whether a permission grant may reach a spawned child.

Difficulty removed: a spawned specialist's file/tool access is currently a
prose brief ("work only under X") the child is trusted to honor, with no
structural check on either end — a plan can declare an `Edit` grant onto the
agent's own settings file and nothing refuses it, and a runtime
`resolve-permission --decision granted` has no shape check at all. This
module is the ONE validator both the plan loader (`agentctl/plan.py`, at
load/diff time) and the runtime grant path (`cli.cmd_resolve_permission`, at
grant time) call — a grant that never passed `validate_grants` can never
reach `spawn-specialist.py`'s `--settings`/`--add-dir` materialization,
regardless of which of the two entry points produced it.

Two other functions round out the module's authority: `derive_stage_grants`
is the ONLY function that may propose a grant from a stage's own declared
elements (a verify_command segment, an output_artifact) — never elsewhere,
never by a human copying a rule into a plan by hand — and every grant it
proposes is passed back through `validate_grants` before use, so a derived
grant is never trusted more than a declared one. `grant_covers_call` is the
inverse question — given an EFFECTIVE grant set, was this actual tool call
covered — used to classify a PERMISSION-REQUEST or a transcript denial as a
materialization defect (grants existed but the child's own settings
materialization failed to carry them) versus a genuine planning miss (no
grant covers this call, so asking the user is correct). It deliberately does
NOT reuse `lib.permission_entry_match.covers`: that function's docstring
states its own failure mode is toward COVERING (a permissions UI showing
"this is probably already allowed"), which is the wrong bias for a function
whose answer gates whether a stage is marked passed without ever asking.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lib import shell_tokens, widening_targets

# --- data shapes -------------------------------------------------------------


@dataclass
class AddDirGrant:
    path: str
    mode: str  # "read" | "write"
    provenance: str  # "declared" | "derived:DR-R" | "runtime"

    def to_dict(self) -> dict:
        return {"path": self.path, "mode": self.mode, "provenance": self.provenance}

    @classmethod
    def from_dict(cls, d: dict) -> "AddDirGrant":
        return cls(path=d["path"], mode=d["mode"], provenance=d.get("provenance", "declared"))


@dataclass
class RuleGrant:
    rule: str  # e.g. "Bash(git status:*)", "Edit(//abs/path)"
    provenance: str  # "declared" | "derived:DR-V" | "derived:DR-O" | "derived:DR-E" | "runtime"

    def to_dict(self) -> dict:
        return {"rule": self.rule, "provenance": self.provenance}

    @classmethod
    def from_dict(cls, d: dict) -> "RuleGrant":
        return cls(rule=d["rule"], provenance=d.get("provenance", "declared"))


@dataclass
class StageGrants:
    """The full grant set a plan (or a runtime grant) may attach to one
    stage. `allow` and `add_dirs` are the two shapes `spawn-specialist.py`
    materializes; `permission_mode` is data-only here (the validator refuses
    every value the plan-authoring surface could set — see
    `validate_grants` — so this field exists to make that refusal explicit
    in the type, not to carry a real value through)."""
    allow: list[RuleGrant] = field(default_factory=list)
    add_dirs: list[AddDirGrant] = field(default_factory=list)
    permission_mode: str | None = None

    def is_empty(self) -> bool:
        return not self.allow and not self.add_dirs and self.permission_mode is None

    def effective_tuple(self) -> tuple:
        """A hashable, order-independent projection used to compare two
        grant sets for GROWTH (diff_plans' substantive check) without
        caring about provenance-label churn or declaration order."""
        return (
            frozenset((r.rule) for r in self.allow),
            frozenset((a.path, a.mode) for a in self.add_dirs),
            self.permission_mode,
        )

    def to_dict(self) -> dict:
        return {
            "allow": [r.to_dict() for r in self.allow],
            "add_dirs": [a.to_dict() for a in self.add_dirs],
            "permission_mode": self.permission_mode,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "StageGrants | None":
        if not d:
            return None
        return cls(
            allow=[RuleGrant.from_dict(r) for r in d.get("allow", [])],
            add_dirs=[AddDirGrant.from_dict(a) for a in d.get("add_dirs", [])],
            permission_mode=d.get("permission_mode"),
        )


class GrantValidationError(ValueError):
    """Raised by `validate_rule`/`validate_grants` naming exactly which
    entry was refused and why — the refusal is a plan-authoring or
    runtime-grant error, never a silent drop."""


# Permission-mode values a grant is never allowed to set, regardless of
# spelling case: each one removes the ask entirely rather than widening a
# specific, reviewable surface.
_FORBIDDEN_PERMISSION_MODES = frozenset({"bypasspermissions", "dontask", "auto"})

# Programs whose write-capable form this module refuses unless the rule's
# own lexed destination argument(s) are provably not a G-target. A rule
# naming one of these with NO destination argument at all (e.g. bare
# `Bash(dd:*)`) is refused outright — there is nothing to prove safe.
_WRITE_CAPABLE_PROGRAMS = frozenset({"tee", "cp", "mv", "sed", "dd", "install", "rsync"})


def _rule_program_and_arg(rule: str) -> tuple[str, str] | None:
    """Parse `Tool(arg)` into `(Tool, arg)`; `None` if the shape doesn't
    match (the validator refuses anything that doesn't parse this way)."""
    if "(" not in rule or not rule.endswith(")"):
        return None
    tool, _, rest = rule.partition("(")
    arg = rest[:-1]
    return tool.strip(), arg


def _bash_command_from_rule_arg(arg: str) -> str:
    """A `Bash(<command>[:*])` rule's argument, with a trailing `:*`
    wildcard suffix stripped — the wildcard means "this command and any
    arguments", not a literal character to tokenize."""
    return arg[:-2] if arg.endswith(":*") else arg


def validate_rule(rule: str) -> None:
    """Refuse `rule` (a Claude-settings-style permission string, e.g.
    `Bash(git status:*)`, `Edit(//abs/path)`) if it is any of the shapes
    Stage 1's spec names. Raises `GrantValidationError` naming the reason;
    returns `None` on a rule the validator accepts. Accepting is NOT a
    claim the rule is useful — only that it is not one of the refused
    shapes."""
    if not isinstance(rule, str) or not rule.strip():
        raise GrantValidationError(f"empty or non-string rule: {rule!r}")

    parsed = _rule_program_and_arg(rule)
    if parsed is None:
        raise GrantValidationError(
            f"rule {rule!r} is not of the form Tool(arg) — refused for lack of a "
            f"parseable destination"
        )
    tool, arg = parsed

    if tool != "Bash":
        # Edit/Write/Read-style rules: the argument is a path-shaped target.
        _validate_non_bash_rule(rule, tool, arg)
        return

    command = _bash_command_from_rule_arg(arg)
    if not command.strip():
        raise GrantValidationError(f"rule {rule!r} names no command")

    try:
        tokens = shell_tokens.separator_exact_split(command)
    except ValueError as exc:
        raise GrantValidationError(f"rule {rule!r} does not lex as a shell command: {exc}") from exc
    tokens = [t for t in tokens if t.strip()]
    if not tokens:
        raise GrantValidationError(f"rule {rule!r} names no command")

    if widening_targets.is_claude_program(tokens):
        raise GrantValidationError(f"rule {rule!r} invokes the `claude` program — refused")

    verb = widening_targets.agentctl_user_authority_call(tokens)
    if verb is not None:
        raise GrantValidationError(
            f"rule {rule!r} invokes agentctl user-authority verb {verb!r} — refused"
        )

    if widening_targets.is_settings_channel_program(tokens):
        raise GrantValidationError(f"rule {rule!r} invokes a settings-channel program — refused")

    if widening_targets.is_crontab_target(command):
        raise GrantValidationError(f"rule {rule!r} invokes crontab — a launch surface — refused")

    stripped = widening_targets._strip_wrappers(tokens)
    if not stripped:
        # A leading wrapper/launcher run (env, timeout, npx, exec, nohup,
        # command, ...) that consumes the entire token list leaves no program
        # to validate against — e.g. bare `env` or `timeout 30` with nothing
        # after it. Refuse outright: `_segment_covered`'s wildcard prefix
        # match would otherwise let `Bash(env:*)` cover any `env <anything>`.
        raise GrantValidationError(
            f"rule {rule!r} is a bare wrapper/launcher with no operand — refused"
        )
    prog = widening_targets._program_name(stripped[0])
    operand_tokens = stripped[1:]

    # Bare interpreter/launcher without a script (or `-m module`) operand:
    # `python3` alone, `bash` alone, `sh` alone — refused, since such a rule
    # would grant an unbounded interactive-equivalent invocation surface.
    _INTERPRETERS = frozenset({"bash", "sh", "zsh", "python", "python3", "node", "ruby", "perl"})
    if (prog in _INTERPRETERS or widening_targets._INTERPRETER_RE.match(prog)) and not operand_tokens:
        raise GrantValidationError(
            f"rule {rule!r} is a bare interpreter/launcher with no script operand — refused"
        )

    if prog in _WRITE_CAPABLE_PROGRAMS:
        _validate_write_capable_bash(rule, prog, operand_tokens)


def _validate_write_capable_bash(rule: str, prog: str, operand_tokens: list[str]) -> None:
    if not operand_tokens:
        raise GrantValidationError(
            f"rule {rule!r} names write-capable program {prog!r} with no destination "
            f"argument to prove safe — refused"
        )
    for tok in operand_tokens:
        if tok.startswith("-"):
            continue
        if _looks_like_path(tok) and _is_g_target_path(tok):
            raise GrantValidationError(
                f"rule {rule!r} names write-capable program {prog!r} whose argument "
                f"{tok!r} is a protected G-target — refused"
            )


def _looks_like_path(token: str) -> bool:
    return "/" in token or token.startswith("~")


def _is_g_target_path(path: str) -> bool:
    return (
        widening_targets.is_live_settings(path)
        or widening_targets.is_agentctl_state_path(path)
        or widening_targets.is_launch_surface(path)
        or widening_targets.add_dir_is_or_contains_protected_root(path)
        or widening_targets.add_dir_under_protected_root(path)
    )


def _validate_non_bash_rule(rule: str, tool: str, arg: str) -> None:
    path = arg[2:] if arg.startswith("//") else arg
    if not path:
        return
    if _is_g_target_path(path):
        raise GrantValidationError(
            f"rule {rule!r} ({tool} onto {path!r}) is a protected G-target — refused"
        )


def validate_add_dir(path: str, mode: str) -> None:
    """Refuse an add_dir grant that is-or-contains a protected root (any
    mode) or is under `~/.claude`/the agentctl state dir (any mode)."""
    if not isinstance(path, str) or not path.strip():
        raise GrantValidationError(f"empty add_dir path: {path!r}")
    if mode not in ("read", "write"):
        raise GrantValidationError(f"add_dir {path!r} has unknown mode {mode!r}")
    if widening_targets.add_dir_is_or_contains_protected_root(path):
        raise GrantValidationError(
            f"add_dir {path!r} is-or-contains a protected root — refused"
        )
    if widening_targets.add_dir_under_protected_root(path):
        raise GrantValidationError(
            f"add_dir {path!r} is under a protected ~/.claude or agentctl-state root — refused"
        )


def validate_grants(grants: StageGrants) -> None:
    """Validate every entry of `grants`. Raises `GrantValidationError` on
    the first refused entry (declared-plan parsing surfaces this at load
    time; derived-grant callers catch it per-entry and route to
    `dropped` instead — see `derive_stage_grants`)."""
    if grants.permission_mode is not None and grants.permission_mode.lower() in _FORBIDDEN_PERMISSION_MODES:
        raise GrantValidationError(
            f"permission_mode {grants.permission_mode!r} removes the ask entirely — refused"
        )
    for r in grants.allow:
        validate_rule(r.rule)
    for a in grants.add_dirs:
        validate_add_dir(a.path, a.mode)


# --- derivation ---------------------------------------------------------------

_SEGMENT_SPLIT_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def _is_unresolvable_segment(seg: str) -> bool:
    """True iff `seg`'s actual target cannot be read off its text alone —
    a `$`/backtick expansion, a redirection, or a subshell paren."""
    return any(ch in seg for ch in ("$", "`", "<", ">")) or "(" in seg or ")" in seg


def _raw_top_level_segments(command: str) -> list[str] | None:
    """Split `command` into its top-level segments on `&&`/`||`/`;`/`|`,
    stripping a leading `!` from each and dropping empty ones. `None` on a
    lex failure (unbalanced quote). Every segment is returned, resolvable
    or not — the two callers below disagree on what an unresolvable
    segment MEANS, so neither policy lives here."""
    try:
        tokens = shell_tokens.separator_exact_split(command)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEGMENT_SPLIT_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    out = []
    for seg_tokens in segments:
        seg = " ".join(seg_tokens).strip()
        if seg.startswith("!"):
            seg = seg[1:].strip()
        if seg:
            out.append(seg)
    return out


def _verify_command_segments(verify_command: str) -> list[str]:
    """Split a stage's `verify_command` into its top-level segments on
    `&&`/`||`/`;`/`|`, stripping a leading `!`. A segment containing `$`,
    a backtick, a redirection operator, a heredoc marker, or a subshell
    paren is DROPPED — derivation never proposes a grant for a command
    whose actual target cannot be read off the text. Used only for
    PROPOSING grants (narrowing what is offered); `_bash_call_covered`
    below needs the opposite bias and does not use this function."""
    raw = _raw_top_level_segments(verify_command)
    if raw is None:
        return []
    return [seg for seg in raw if not _is_unresolvable_segment(seg)]


def _in_venue(path: str, venue: str) -> bool:
    return not path.startswith("/")  # a relative path is venue-local by construction


def derive_stage_grants(stage, *, venue: str) -> tuple[StageGrants, list[dict]]:
    """The pure function from a stage's declared elements to a derived
    grant set: DR-V (one `Bash(<segment>:*)` per top-level verify_command
    segment), DR-O (an in-venue `.py` output_artifact not under `tests/`
    becomes `Bash(python3 <rel>:*)`; a `.sh` one becomes `Bash(<rel>:*)`),
    DR-E (spawn:developer/spawn:tech-writer only: each in-venue
    output_artifact/material_ref becomes `Edit(//<abs>)`), DR-R (each
    outside-venue absolute path in material_refs/knowledge_refs/
    output_artifacts becomes a READ add_dir on its directory).

    Every proposed entry passes through the same validator entries pass
    through when declared; a refused entry is dropped, not raised — this
    function never fails a plan load, it only ever narrows what it offers.
    Returns `(grants, dropped)` where `dropped` is a list of
    `{"entry": ..., "reason": ...}` dicts for whatever the validator
    refused.
    """
    allow: list[RuleGrant] = []
    add_dirs: list[AddDirGrant] = []
    dropped: list[dict] = []

    def _try_rule(rule: str, provenance: str) -> None:
        try:
            validate_rule(rule)
        except GrantValidationError as exc:
            dropped.append({"entry": rule, "reason": str(exc)})
            return
        allow.append(RuleGrant(rule=rule, provenance=provenance))

    def _try_add_dir(path: str, mode: str, provenance: str) -> None:
        try:
            validate_add_dir(path, mode)
        except GrantValidationError as exc:
            dropped.append({"entry": path, "reason": str(exc)})
            return
        add_dirs.append(AddDirGrant(path=path, mode=mode, provenance=provenance))

    verify_command = getattr(stage.criterion, "verify_command", None)
    if verify_command:
        for segment in _verify_command_segments(verify_command):
            _try_rule(f"Bash({segment}:*)", "derived:DR-V")

    is_dev_or_writer = stage.is_spawn() and stage.spawn_kind() in ("developer", "tech-writer")

    for artifact in stage.output_artifacts:
        if not _in_venue(artifact, venue):
            continue
        if artifact.startswith("tests/"):
            continue
        if artifact.endswith(".py"):
            _try_rule(f"Bash(python3 {artifact}:*)", "derived:DR-O")
        elif artifact.endswith(".sh"):
            _try_rule(f"Bash({artifact}:*)", "derived:DR-O")

    if is_dev_or_writer:
        for artifact in list(stage.output_artifacts) + list(stage.subject.material_refs):
            if not _in_venue(artifact, venue):
                continue
            abs_path = f"{venue.rstrip('/')}/{artifact}"
            _try_rule(f"Edit(//{abs_path})", "derived:DR-E")

    all_refs = (
        list(stage.subject.material_refs)
        + list(stage.subject.knowledge_refs)
        + list(stage.output_artifacts)
    )
    seen_dirs: set[str] = set()
    for ref in all_refs:
        if not ref.startswith("/"):
            continue  # only OUTSIDE-venue absolute paths derive a read add_dir
        directory = ref.rsplit("/", 1)[0] or "/"
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        _try_add_dir(directory, "read", "derived:DR-R")

    return StageGrants(allow=allow, add_dirs=add_dirs), dropped


# --- coverage -------------------------------------------------------------


def grant_covers_call(grants: StageGrants, tool_name: str, tool_input: dict) -> bool:
    """Whether the harness-materialized `grants` would have covered an
    actual `tool_name`/`tool_input` call. Fails toward NOT-covered on
    anything unresolvable — deliberately the opposite bias from
    `lib.permission_entry_match.covers`, since this function's answer
    decides whether a denial is a `materialization_defect` (routes the
    stage to FAILED/DIAGNOSING with no re-ask) or a genuine planning miss
    (correctly asks the user); a false "covered" here would silently
    swallow a legitimate ask."""
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str) or not command.strip():
            return False
        return _bash_call_covered(grants, command)
    if tool_name in ("Edit", "Write", "Read", "NotebookEdit"):
        path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        if not isinstance(path, str) or not path.strip():
            return False
        return _path_call_covered(grants, tool_name, path)
    return False


def _resolve_for_match(path: str) -> str | None:
    """Normalize `..`/`.` segments and resolve symlinks before matching a
    file-tool path against a glob/add_dir. An unresolvable path (one whose
    components cannot be stat'd) is treated as unmatched by the caller."""
    from pathlib import Path

    try:
        p = Path(path).expanduser()
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    resolved_str = str(resolved)
    parts = resolved_str.split("/")
    if ".git" in parts or ".claude" in parts:
        return None
    return resolved_str


def _path_call_covered(grants: StageGrants, tool_name: str, path: str) -> bool:
    resolved = _resolve_for_match(path)
    if resolved is None:
        return False
    for r in grants.allow:
        parsed = _rule_program_and_arg(r.rule)
        if parsed is None:
            continue
        tool, arg = parsed
        if tool != tool_name:
            continue
        rule_path = arg[2:] if arg.startswith("//") else arg
        rule_resolved = _resolve_for_match(rule_path)
        if rule_resolved is not None and rule_resolved == resolved:
            return True
    for a in grants.add_dirs:
        dir_resolved = _resolve_for_match(a.path)
        if dir_resolved is None:
            continue
        if resolved == dir_resolved or resolved.startswith(dir_resolved + "/"):
            if tool_name in ("Edit", "Write", "NotebookEdit") and a.mode != "write":
                continue
            return True
    return False


def _bash_call_covered(grants: StageGrants, command: str) -> bool:
    segments = _raw_top_level_segments(command)
    # A compound/multi-line command is covered only when EVERY top-level
    # segment is individually covered by some Bash rule — a partially
    # covered compound command is not fully materializable from the grant
    # set and must not be reported as covered. An unresolvable segment (a
    # `$`/backtick expansion, a redirect, a subshell paren) is never
    # silently dropped here the way derivation drops it: unlike
    # `_verify_command_segments`, which only NARROWS what grants are
    # offered, this function's answer decides whether a real denial is
    # reported as a materialization defect — a segment whose real target
    # cannot be read off the text must count as NOT covered, not as
    # absent from the count.
    if segments is None or not segments:
        return False
    for segment in segments:
        if _is_unresolvable_segment(segment) or not _segment_covered(grants, segment):
            return False
    return True


def _segment_covered(grants: StageGrants, segment: str) -> bool:
    for r in grants.allow:
        parsed = _rule_program_and_arg(r.rule)
        if parsed is None or parsed[0] != "Bash":
            continue
        rule_command = _bash_command_from_rule_arg(parsed[1])
        wildcard = parsed[1].endswith(":*")
        if wildcard:
            if segment == rule_command or segment.startswith(rule_command + " "):
                return True
        elif segment == rule_command:
            return True
    return False
