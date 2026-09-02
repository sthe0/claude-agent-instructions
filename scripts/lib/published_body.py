"""Resolve whether a Bash tool call is a publication, and which bytes it posts.

Difficulty removed: the published-text writer gate must decide, from a
PreToolUse payload alone, "is this call about to publish reader-facing text,
and if so what body is it posting" -- and neither question has an existing
answer anywhere in this repo. This module answers both, structurally: it reads
shell-command SHAPE (which flag, which operand, which command substitution)
and never the MEANING of any text, which is what keeps it outside the
regex-not-for-semantic-classification prohibition and is what lets a caller
drive a hard deny off its answer.

A publication is recognized in two ways: a Bash command whose segment invokes
a Core `gh` verb (`gh issue comment`, `gh pr comment`, `gh issue create`
unconditionally; `gh issue edit` / `gh pr edit` / `gh pr create` only when a
`--body*` flag is present), or a Bash command / tool name matching a
machine-local seam entry (`config_root.publication_tools_file()`) for a
deployment-specific verb or a genuine `mcp__*` tool name. The seam is data,
Core's `gh` shapes are mechanism -- on a machine that declares no seam, the
Core shapes alone still apply.

Body resolution returns a typed `Resolution`, never a bare string, because
"the body could not be determined" is a distinct, first-class outcome
(`UNRESOLVED`) from "here are the bytes" (`TEXT`) -- collapsing the two would
let an unmodelled command masquerade as an empty, trivially-matching body.
Six shapes are recognized (file-valued flag, inline literal, heredoc nested in
a command substitution, a same-command shell-variable assignment, an
attachment operand, and an inline path read sharing the assignment shape's
reader) -- everything else is UNRESOLVED, never a fallback scan of the raw
command string: the gate's predicate is a property of the BODY's provenance,
and the command string is not the body, so matching it would answer a
different question.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import bash_write_targets, config_root, shell_tokens

TEXT = "TEXT"
ATTACHMENT = "ATTACHMENT"
UNRESOLVED = "UNRESOLVED"
NOT_A_PUBLICATION = "NOT_A_PUBLICATION"


@dataclass(frozen=True)
class Resolution:
    kind: str
    body: str | None = None
    path: str | None = None
    shape: int | None = None


# Core, host-agnostic `gh` shapes. `True` marks a verb that is a publication
# only when a `--body*` flag is present (an edit/create call with no body text
# does not publish anything); `False` marks a verb that always posts text.
_CORE_GH_NO_BODY_REQUIRED = (("issue", "comment"), ("pr", "comment"), ("issue", "create"))
_CORE_GH_BODY_REQUIRED = (("issue", "edit"), ("pr", "edit"), ("pr", "create"))

_FILE_FLAG_NAMES = ("--body-file", "-F")
_VALUE_FLAG_NAMES = ("--body", "--text")

_INLINE_CAT_RE = re.compile(r"^cat\s+(.+)$", re.DOTALL)
_INLINE_READ_RE = re.compile(r"^<\s*(.+)$", re.DOTALL)

ADVISORY_SINK_NAME = "published-text-gate-advisories.jsonl"


def _strip_quotes(path: str) -> str:
    path = path.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "'\"":
        return path[1:-1]
    return path


def _read_target_bytes(raw_path: str | None, cwd: str) -> str | None:
    """The decoded contents of `raw_path` resolved against `cwd`, or `None`
    when the path is missing, unreadable, or reads as zero bytes -- the rule
    that keeps an empty/unreadable target from resolving as a trivially-
    matching empty TEXT body."""
    if not raw_path:
        return None
    path = _strip_quotes(raw_path)
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _resolve_var_assignment(command: str, varname: str, cwd: str) -> str | None:
    """The bytes of the path a same-command `VAR=$(cat <path>)` assignment
    reads, for a reference `$VAR` found elsewhere in `command` -- or `None`
    when no such assignment exists (the genuinely-unresolved `$VAR` case)."""
    pattern = re.compile(
        r"(?:^|[\s;\n])" + re.escape(varname) + r"=\$\(\s*cat\s+([^)]+?)\s*\)"
    )
    match = pattern.search(command)
    if not match:
        return None
    return _read_target_bytes(match.group(1), cwd)


def _classify_value(value: str, command: str, cwd: str) -> tuple[str | None, int | None]:
    """Dispatch a flag/operand VALUE token (already shell-unquoted) to the
    shape it syntactically is: `@path` (shape 1), a command substitution
    (shape 6's inline path read, or shape 3's nested heredoc, sharing
    `shell_tokens.heredoc_bodies`), a bare `$VAR` reference (shape 4), or an
    inline literal (shape 2, returned verbatim -- no file read)."""
    if value.startswith("@"):
        return _read_target_bytes(value[1:], cwd), 1
    if value.startswith("$(") and value.endswith(")"):
        inner = value[2:-1].strip()
        # A heredoc/here-string operand (`cat <<'EOF' ... EOF`) is tried FIRST:
        # `_INLINE_CAT_RE` is a bare `cat\s+(.+)` match and would otherwise
        # swallow `cat <<'EOF'...` too, misreading the heredoc redirect as a
        # literal path argument.
        bodies = shell_tokens.heredoc_bodies(inner)
        if bodies:
            return (bodies[0] if len(bodies) == 1 and bodies[0] else None), 3
        cat_match = _INLINE_CAT_RE.match(inner)
        if cat_match:
            return _read_target_bytes(cat_match.group(1), cwd), 6
        read_match = _INLINE_READ_RE.match(inner)
        if read_match:
            return _read_target_bytes(read_match.group(1), cwd), 6
        return None, None
    if value.startswith("$"):
        varname = value.lstrip("$").strip("{}")
        return _resolve_var_assignment(command, varname, cwd), 4
    return value, 2


def _resolve_text_body(command: str, tokens: list[str], cwd: str) -> tuple[str | None, int | None]:
    for i, tok in enumerate(tokens):
        if tok in _FILE_FLAG_NAMES and i + 1 < len(tokens):
            return _read_target_bytes(tokens[i + 1], cwd), 1
        for name in _FILE_FLAG_NAMES:
            if tok.startswith(name + "="):
                return _read_target_bytes(tok[len(name) + 1:], cwd), 1
    for i, tok in enumerate(tokens):
        if tok in _VALUE_FLAG_NAMES and i + 1 < len(tokens):
            return _classify_value(tokens[i + 1], command, cwd)
        for name in _VALUE_FLAG_NAMES:
            if tok.startswith(name + "="):
                return _classify_value(tok[len(name) + 1:], command, cwd)
    for tok in tokens:
        if tok.startswith("text:"):
            return _classify_value(tok[len("text:"):], command, cwd)
    return None, None


def _attachment_path(tokens: list[str]) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == "--attach" and i + 1 < len(tokens):
            return tokens[i + 1]
    positionals = [t for t in tokens if not t.startswith("-")]
    return positionals[-1] if positionals else None


@dataclass(frozen=True)
class _Match:
    is_attachment: bool


def _match_gh(tokens: list[str]) -> _Match | None:
    for seg in bash_write_targets.split_segments(tokens):
        if not seg:
            continue
        if os.path.basename(seg[0]) != "gh" or len(seg) < 3:
            continue
        shape2 = (seg[1], seg[2])
        if shape2 in _CORE_GH_NO_BODY_REQUIRED:
            return _Match(is_attachment=False)
        if shape2 in _CORE_GH_BODY_REQUIRED and any(t.startswith("--body") for t in seg):
            return _Match(is_attachment=False)
    return None


def _match_seam_bash(command: str, seam) -> _Match | None:
    for entry in seam or []:
        if not isinstance(entry, dict) or entry.get("kind") != "bash_verb":
            continue
        name = entry.get("name")
        if name and name in command:
            return _Match(is_attachment=entry.get("body_shape") == "attachment")
    return None


def _match_seam_mcp_tool(tool_name: str, seam) -> _Match | None:
    for entry in seam or []:
        if not isinstance(entry, dict) or entry.get("kind") != "mcp_tool":
            continue
        if entry.get("name") == tool_name:
            return _Match(is_attachment=entry.get("body_shape") == "attachment")
    return None


def is_publication(tool_name: str, tool_input, seam=None) -> bool:
    """True iff this tool call is a publication -- text or attachment alike.
    A thin wrapper over `resolve()`'s own verb match, so the trigger and the
    resolver can never disagree about which calls count."""
    return resolve(tool_name, tool_input, cwd=".", seam=seam).kind != NOT_A_PUBLICATION


def resolve(tool_name: str, tool_input, cwd: str, seam=None) -> Resolution:
    """Resolve `tool_name`/`tool_input` to a `Resolution`. `cwd` is the call's
    effective working directory (from the PreToolUse payload), used to
    resolve every relative path candidate. `seam` is the machine-local
    verb/tool list from `config_root.publication_tools_file()` (a list of
    `{"name", "kind", ...}` dicts), or `None` for a machine that declares
    none -- Core's `gh` shapes still apply either way."""
    if tool_name != "Bash":
        match = _match_seam_mcp_tool(tool_name, seam)
        if match is None:
            return Resolution(kind=NOT_A_PUBLICATION)
        record_advisory(UNRESOLVED, None, f"mcp_tool:{tool_name}")
        return Resolution(kind=UNRESOLVED)

    command = (tool_input or {}).get("command") or ""
    if not command.strip():
        return Resolution(kind=NOT_A_PUBLICATION)

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = None

    match = _match_gh(tokens) if tokens else None
    if match is None:
        match = _match_seam_bash(command, seam)
    if match is None:
        return Resolution(kind=NOT_A_PUBLICATION)

    if match.is_attachment:
        path = _attachment_path(tokens or [])
        if not path:
            record_advisory(ATTACHMENT, 5, command)
            return Resolution(kind=UNRESOLVED, shape=5)
        abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        return Resolution(kind=ATTACHMENT, path=abs_path, shape=5)

    text, shape = _resolve_text_body(command, tokens or [], cwd)
    if not text:
        record_advisory(TEXT, shape, command)
        return Resolution(kind=UNRESOLVED, shape=shape)
    return Resolution(kind=TEXT, body=text, shape=shape)


# Non-gating diagnostic only: a hint for a deny message, never a decision.
_TOML_SECTION_RE = re.compile(r"^[ \t]*\[[A-Za-z0-9_.\[\]]+\]\s*$", re.MULTILINE)
_PLAN_FIELD_RE = re.compile(r"^\s*-?\s*\*\*[A-Za-z][A-Za-z0-9 _/-]*:\*\*", re.MULTILINE)
_TOML_KV_RE = re.compile(r'^[ \t]*[A-Za-z_][A-Za-z0-9_]*\s*=\s*("|\[|\d|true|false).*$', re.MULTILINE)


def artifact_syntax_hint(body: str) -> str:
    """A human-readable hint when `body` carries raw artifact syntax (a TOML
    section, plan-render `**Field:**` labels, or several `key = value` TOML
    lines) rather than reader-facing prose -- for use in a deny message only.
    Never gates a decision: a test asserts this never changes an outcome."""
    if not body:
        return ""
    if _TOML_SECTION_RE.search(body):
        return "the body contains a TOML `[section]` header"
    if _PLAN_FIELD_RE.search(body):
        return "the body contains plan-render `**Field:**` labels"
    if len(_TOML_KV_RE.findall(body)) >= 2:
        return "the body contains multiple `key = value` TOML-shaped lines"
    return ""


def record_advisory(kind: str, shape: int | None, command: str) -> None:
    """Append a fail-open advisory JSON line for a resolution that fell
    through to UNRESOLVED. Never raises: a hook that cannot write its
    diagnostics must still allow rather than wedge the turn. Records a
    sha256 of the command rather than the command itself."""
    try:
        sink = config_root.hook_state_dir("published-text-gate") / ADVISORY_SINK_NAME
        sink.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "kind": kind,
            "shape": shape,
            "command_sha256": hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with sink.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass
