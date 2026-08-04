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
from pathlib import Path

from .runtime_models import HOST_CLAUDE, HOST_CURSOR, HOSTS

DEFAULT_CURSOR_API_KEY_FILE = Path.home() / ".cursor_api_key"

_HOST_BINARY_FAMILY = {
    HOST_CLAUDE: frozenset({"claude"}),
    HOST_CURSOR: frozenset({"agent", "cursor-agent"}),
}


class CrossHostError(Exception):
    """Refused: the assembled command would invoke a binary outside the bound
    runtime_host's family (e.g. runtime_host=cursor building an argv around
    `claude`, or runtime_host=claude building one around `agent`)."""


def binary_for(host: str) -> str:
    """The CLI binary `host` launches: the canonical literal for HOST_CLAUDE
    (there is only ever one name, and `subprocess.run` resolves it against PATH
    itself, so pre-resolving would only make every judge/extractor test depend
    on whether `claude` happens to be installed — a hermeticity regression for
    no behavioural gain). HOST_CURSOR genuinely has two possible binary names
    (`agent` / `cursor-agent`), so THAT choice does need to check what is
    actually on PATH — falling back to `agent` so a --dry-run preview stays
    legible even with neither installed."""
    if host == HOST_CLAUDE:
        return "claude"
    if host == HOST_CURSOR:
        return shutil.which("agent") or shutil.which("cursor-agent") or "agent"
    raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")


def assert_same_family(binary: str, host: str) -> None:
    """Refuse a cross-host invocation before it ever reaches a subprocess — the
    one deterministic guard behind the "cross-host refuse with an explicit
    error" done criterion: runtime_host=cursor must never shell out to
    `claude`; runtime_host=claude must never shell out to `agent`/`cursor-agent`."""
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
    """Whether `host`'s CLI is actually runnable right now. Never raises;
    returns (ok, reason) so a caller can refuse a REAL spawn with an explicit
    message while a --dry-run argv preview (which never calls this) proceeds
    regardless of binary/key availability."""
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
    host: str, model: str, prompt: str, *, workspace: "Path | str | None" = None
) -> list[str]:
    """Assemble the argv for a single bare `<binary> -p ... <prompt>` call.

    Claude: `["claude", "-p", "--model", <model>, <prompt>]` — the exact shape
    agentctl.advisor's judges have always built inline, kept byte-identical so
    every existing default-host caller (and its pinned tests) sees the same
    argv. Cursor: mirrors spawn-cursor-specialist.py's build_agent_cmd (`-p
    --trust --force --approve-mcps --output-format text --model <model>`,
    optionally `--workspace <path>`), with the prompt POSITIONAL —
    cursor-agent reads `-p` as a boolean switch and takes the prompt as its
    own argument, unlike claude's flag-then-prompt shape. Both shapes put the
    prompt LAST, so a caller that only cares "is the prompt in argv" (e.g.
    `argv[-1]`) needs no host branch.
    """
    binary = binary_for(host)
    if host == HOST_CLAUDE:
        argv = [binary, "-p", "--model", model, prompt]
    elif host == HOST_CURSOR:
        argv = [
            binary, "-p", "--trust", "--force", "--approve-mcps",
            "--output-format", "text", "--model", model,
        ]
        if workspace is not None:
            argv += ["--workspace", str(workspace)]
        argv.append(prompt)
    else:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}")
    assert_same_family(binary, host)
    return argv
