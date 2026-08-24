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

import os
import shutil
import tempfile
from pathlib import Path

from .runtime_models import HOST_CLAUDE, HOST_CURSOR, HOSTS

DEFAULT_CURSOR_API_KEY_FILE = Path.home() / ".cursor_api_key"

# A bare `claude -p ...` argv is a FULL Claude Code session: absent isolation it
# inherits the ambient CLAUDE_CONFIG_DIR and cwd, so it loads the fleet's own
# settings.json and registers the same hooks the coordinating session runs under.
# Measured on this machine: a judge prompt containing feedback-shaped example text
# (the self-improvement judge's own template) re-entered hook-self-improvement-
# reminder.py on itself, recursing 126 levels deep to a 111 364-char prompt, and
# 1583 such calls exhausted a 5-hour quota window in 48 minutes. Pointing
# CLAUDE_CONFIG_DIR at an empty, agent-owned directory and running from an empty
# cwd (no CLAUDE.md chain to discover) leaves no settings.json for any hook to
# register from, so the recursion cannot start regardless of prompt content. A
# live A/B on the same 989-byte prompt: ambient cache_creation_input_tokens
# 40 707 vs. isolated 8 499.
_SANDBOX_ROOT = Path(tempfile.gettempdir()) / "claude-judge-sandbox"


def isolated_run_kwargs() -> dict:
    """kwargs for subprocess.run() that isolate a single-turn `claude -p ...` call
    from the fleet's own hook registrations (see _SANDBOX_ROOT's comment above).

    `env` is os.environ COPIED, never replaced, with only CLAUDE_CONFIG_DIR
    overridden: auth is env-carried (ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY), so
    replacing the environment would break every judge's ability to authenticate.
    `cwd` is a separate empty directory so no project-level CLAUDE.md chain is
    discovered from the working directory either. Both sandbox directories are
    created on demand (mkdir is idempotent and safe under concurrent callers).
    """
    home = _SANDBOX_ROOT / "home"
    cwd = _SANDBOX_ROOT / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return {"cwd": str(cwd), "env": env}


_HOST_BINARY_FAMILY = {
    HOST_CLAUDE: frozenset({"claude"}),
    HOST_CURSOR: frozenset({"agent", "cursor-agent"}),
}


class CrossHostError(Exception):
    """Refused: the assembled command would invoke a binary outside the bound
    runtime_host's family."""


def binary_for(host: str) -> str:
    if host == HOST_CLAUDE:
        return "claude"
    if host == HOST_CURSOR:
        return shutil.which("agent") or shutil.which("cursor-agent") or "agent"
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
    raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")


def build_prompt_argv(
    host: str, model: str | None, prompt: str, *, workspace: "Path | str | None" = None
) -> list[str]:
    binary = binary_for(host)
    if host == HOST_CLAUDE:
        if model is None:
            raise ValueError("model is required for HOST_CLAUDE")
        argv = [binary, "-p", "--model", model, prompt]
    elif host == HOST_CURSOR:
        argv = [
            binary, "-p", "--trust", "--force", "--approve-mcps",
            "--output-format", "text",
        ]
        if model is not None:
            argv += ["--model", model]
        if workspace is not None:
            argv += ["--workspace", str(workspace)]
        argv.append(prompt)
    else:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")
    assert_same_family(binary, host)
    return argv
