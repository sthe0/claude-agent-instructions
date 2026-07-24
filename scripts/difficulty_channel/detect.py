"""Auto-detect the appropriate difficulty_channel for this machine.

detect_channel() is a pure function — every host-access operation is injected via the
four probe parameters so tests run offline with no real host inspection.

Core carries only the probe interface and the ORG-NEUTRAL precedence:
  1. an optional machine-local detect hook decides first (contract below)
  2. any GitHub credential (token file, GITHUB_TOKEN env, gh CLI) -> github
  3. default (no credential at all)                              -> github + a warning

Which signals identify an *organization* is org-specific DATA, not Core mechanism, so
it lives in the machine-local plugin dir and never in the public repo.

Plugin contract. ``<plugin dir>/detect.py`` — plugin dir: ``$CLAUDE_DIFFICULTY_PLUGIN_DIR``
else ``<config root>/difficulty-channel-plugins``, the same dir that holds ``adapters/`` —
exposes::

    detect(hostname, has_command, path_exists, getenv) -> DetectResult | None

called with the four probes as KEYWORD arguments. Returning a DetectResult decides the
channel (the hook owns its own evidence/warnings); returning None defers to the neutral
rules above, and the hook's evidence is dropped with it — evidence explains the decision,
and a signal that did not drive the outcome is noise. The hook module must use ABSOLUTE
imports (see ``lib.plugin_dir.load_plugin_module``).

``detect_channel`` never loads the hook itself — that would make it impure. Impure callers
resolve it once via ``load_detect_hook()`` and pass it in; ``detect_this_machine()`` below
is the reference wiring.
"""
from __future__ import annotations

import os
import socket
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:  # scripts/ for lib.plugin_dir
    sys.path.insert(0, _SCRIPTS_DIR)
from lib.plugin_dir import load_plugin_module, resolve_plugin_dir  # noqa: E402

DEFAULT_CHANNEL = "github"

PLUGIN_DIR_ENV = "CLAUDE_DIFFICULTY_PLUGIN_DIR"
PLUGIN_DIR_NAME = "difficulty-channel-plugins"
_PLUGIN_NAMESPACE = "difficulty_channel._plugin_detect"


@dataclass
class DetectResult:
    channel: str
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def detect_channel(
    hostname: Callable[[], str],
    has_command: Callable[[str], bool],
    path_exists: Callable[[str], bool],
    getenv: Callable[[str], str | None],
    hook: Callable[..., DetectResult | None] | None = None,
) -> DetectResult:
    """Pure detection function — all host I/O via the four injected probes.

    `hostname` is unused by the neutral rules (no hostname pattern is org-neutral); it stays
    in the signature because the hook needs it and because the probe set is the contract.
    """
    if hook is not None:
        decided = hook(
            hostname=hostname,
            has_command=has_command,
            path_exists=path_exists,
            getenv=getenv,
        )
        if decided is not None:
            return decided

    evidence: list[str] = []
    warnings: list[str] = []

    if path_exists("~/.github-token"):
        evidence.append("github-token-file")
    if getenv("GITHUB_TOKEN"):
        evidence.append("github-token-env")
    if has_command("gh"):
        evidence.append("gh-cli")

    if not evidence:
        warnings.append("no credentials found; defaulting to github")

    return DetectResult(channel=DEFAULT_CHANNEL, evidence=evidence, warnings=warnings)


def load_detect_hook() -> Callable[..., DetectResult | None] | None:
    """Resolve this machine's detect hook, or None when no plugin dir / hook is installed.

    Impure by construction (reads the filesystem) — kept out of `detect_channel` so that
    stays a pure function over its probes.
    """
    module = load_plugin_module(
        resolve_plugin_dir(PLUGIN_DIR_ENV, PLUGIN_DIR_NAME), "detect.py", _PLUGIN_NAMESPACE
    )
    return getattr(module, "detect", None) if module is not None else None


def _real_path_exists(p: str) -> bool:
    return os.path.exists(os.path.expanduser(p))


def detect_this_machine() -> DetectResult:
    """Reference wiring: the real probes plus this machine's detect hook."""
    return detect_channel(
        hostname=socket.getfqdn,
        has_command=lambda cmd: shutil.which(cmd) is not None,
        path_exists=_real_path_exists,
        getenv=os.environ.get,
        hook=load_detect_hook(),
    )


if __name__ == "__main__":
    result = detect_this_machine()
    print(result.channel)
    if result.evidence:
        print(f"evidence: {', '.join(result.evidence)}", file=sys.stderr)
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
