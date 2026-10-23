#!/usr/bin/env python3
"""Shared long-job launch-pattern scan.

Difficulty removed: two consumers must apply the SAME "does this Bash command
launch a long-running external job?" predicate and never drift apart —

  - the PreToolUse advisory `hook-long-job-arm.py` nudges at launch time;
  - the turn-end guardian in `hook-turn-end-gate.py` blocks a turn that launched
    such a job without arming a harness-tracked auto-wake.

Keeping detect() (and its regexes / orchestrator-list resolution) in ONE
importable module — mirroring timer_arm_detect.py — makes that divergence
impossible. `hook-long-job-arm.py` has a hyphenated name and cannot be imported,
so the shared logic lives here and the hook re-exports it.

The orchestrator name list is operator-configurable so this works in any org:
set `long_job_orchestrators=name1,name2` (comma/space-separated) in the system
config root's `agent-identity.local`. Core ships no built-in names — with the
key absent, an unconfigured machine detects only `nohup` and arms on no
orchestrator launches until the operator configures its own list.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NOHUP_RE = re.compile(r"\bnohup\b")
_SINGLE_QUOTED_RE = re.compile(r"'[^']*'")
_DOUBLE_QUOTED_RE = re.compile(r'"[^"]*"')

# Separators after which the next unquoted word starts a new simple command.
_NOHUP_CMD_SEPS = frozenset({"|", "||", "&&", "|&", ";", "&", "("})
# Reserved words after which the next word is still a command word.
_NOHUP_CMD_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "{", "!"})
# Shell variable assignment prefix: VAR=value is not the command word.
_NOHUP_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
# Command wrappers that run their remaining arguments as a command, mapped to
# the options that consume the next token as a value.
_WRAPPER_VALUE_OPTS: dict[str, frozenset[str]] = {
    "setsid": frozenset(),
    "exec": frozenset({"-a"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "time": frozenset({"-f", "-o"}),
    "command": frozenset(),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}),
    "timeout": frozenset({"-s", "-k"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
}
_WRAPPER_POSITIONALS = {"timeout": 1}


def _strip_quoted(cmd: str) -> str:
    """Drop quoted spans so a keyword inside a search pattern is not read as a launch."""
    return _DOUBLE_QUOTED_RE.sub("", _SINGLE_QUOTED_RE.sub("", cmd))


def _nohup_is_command_word(cmd: str) -> bool:
    """True iff `nohup` appears as the command word of a simple command in `cmd`.

    Blanks heredoc bodies with `shell_tokens.neutralize_heredoc_constructs`
    and tokenizes with `shell_tokens.separator_exact_split` (posix, quote-aware),
    then checks whether `nohup` is the first non-assignment word after a command
    separator or at the start.  Fail-open: falls back to a plain NOHUP_RE text
    search on any tokenizer error so a real nohup launch is never silently missed.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lib import shell_tokens
        neutralized = shell_tokens.neutralize_heredoc_constructs(cmd)
        tokens = shell_tokens.separator_exact_split(neutralized)
    except Exception:
        return bool(NOHUP_RE.search(cmd))

    in_cmd = True  # start of command is always a command position
    wrapper: str | None = None  # command wrapper whose own arguments are being skipped
    skip_value = False  # previous token was a wrapper option that takes a value
    positionals = 0  # wrapper positional arguments still to skip (timeout's duration)
    for token in tokens:
        if token in _NOHUP_CMD_SEPS:
            in_cmd, wrapper, skip_value, positionals = True, None, False, 0
        elif token == ")":
            in_cmd, wrapper = False, None
        elif in_cmd:
            if _NOHUP_ASSIGN_RE.fullmatch(token):
                continue  # variable assignment; command word not yet seen
            if wrapper is not None:
                if skip_value:
                    skip_value = False
                    continue
                if token.startswith("-") and token != "-":
                    skip_value = token in _WRAPPER_VALUE_OPTS[wrapper]
                    continue
                if positionals:
                    positionals -= 1
                    continue
            if token == "nohup":
                return True
            if token in _NOHUP_CMD_KEYWORDS:
                wrapper = None
                continue
            if token in _WRAPPER_VALUE_OPTS:
                wrapper = token
                positionals = _WRAPPER_POSITIONALS.get(token, 0)
                continue
            in_cmd = False
    return False

# Core carries no org-specific orchestrator names; the list is entirely
# operator-supplied via agent-identity.local's `long_job_orchestrators=` key.
DEFAULT_ORCHESTRATORS: tuple[str, ...] = ()


def _orchestrator_names(identity_path=None) -> tuple[str, ...]:
    """Resolve the orchestrator name list. Reads `long_job_orchestrators=`
    (comma/space-separated) from the resolved agent-identity.local, falling back to
    DEFAULT_ORCHESTRATORS. Fail-open: any error yields the default (a hook must
    never crash the Bash call it advises on)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from difficulty_channel.authority import read_local_identity, LOCAL_IDENTITY_PATH

        raw = read_local_identity(identity_path or LOCAL_IDENTITY_PATH).get(
            "long_job_orchestrators", ""
        )
        names = tuple(n for n in re.split(r"[,\s]+", raw.strip()) if n)
        return names or DEFAULT_ORCHESTRATORS
    except Exception:
        return DEFAULT_ORCHESTRATORS


def _build_tool_re(names=None) -> "re.Pattern[str]":
    """Compile the orchestrator-name alternation from the resolved (or given) list.
    An empty list (no names configured anywhere) compiles to a pattern that never
    matches — an empty alternation would otherwise match at every word boundary."""
    resolved = names if names is not None else _orchestrator_names()
    if not resolved:
        return re.compile(r"(?!)")
    alts = "|".join(re.escape(n) for n in resolved)
    return re.compile(rf"\b({alts})\b", re.IGNORECASE)


# orchestration tool + a launch verb, in either order, within the command
TOOL_RE = _build_tool_re()
VERB_RE = re.compile(
    r"\b(start[-_]?op|start|launch|submit|create|run|exec|operation|vanilla)\b",
    re.IGNORECASE,
)


def detect(cmd: str) -> str | None:
    """Return a short reason string if the command looks like a long-job launch."""
    if _nohup_is_command_word(cmd):
        return "detached process (nohup)"
    scan = _strip_quoted(cmd)
    if TOOL_RE.search(scan) and VERB_RE.search(scan):
        tool = TOOL_RE.search(scan).group(1).lower()
        return f"orchestrator launch ({tool})"
    return None
