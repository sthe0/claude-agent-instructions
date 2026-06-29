"""Auto-detect the appropriate difficulty_channel for this machine.

detect_channel() is a pure function — every host-access operation is injected via the
four probe parameters so tests run offline with no real host inspection.

Precedence (first match wins):
  1. STRONG internal signal (corp hostname, arcadia toolchain, skotty, /etc/yandex) -> startrek
  2. Any GitHub credential (token file, GITHUB_TOKEN env, gh CLI)                  -> github
  3. Weak internal signal only (tracker-token file) with no GitHub cred             -> startrek
  4. Default                                                                         -> github

Warnings are emitted when the chosen channel lacks its write credential, or when the
machine has no recognizable credentials at all.
"""
from __future__ import annotations

import os
import socket
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable


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
) -> DetectResult:
    """Pure detection function — all host I/O via the four injected probes."""
    evidence: list[str] = []
    warnings: list[str] = []

    fqdn = hostname()

    # --- Strong internal signals ---
    strong_internal: list[str] = []
    if fqdn.endswith(".yandex.net") or fqdn.endswith(".yandex-team.ru"):
        strong_internal.append("corp-hostname")
    if has_command("ya") and has_command("arc"):
        strong_internal.append("arcadia-toolchain")
    if path_exists("~/.skotty") or has_command("skotty"):
        strong_internal.append("skotty")
    if path_exists("/etc/yandex"):
        strong_internal.append("etc-yandex")

    # --- GitHub credentials ---
    github_creds: list[str] = []
    if path_exists("~/.github-token"):
        github_creds.append("github-token-file")
    if getenv("GITHUB_TOKEN"):
        github_creds.append("github-token-env")
    if has_command("gh"):
        github_creds.append("gh-cli")

    # --- Weak internal signals ---
    weak_internal: list[str] = []
    if path_exists("~/.tracker-token"):
        weak_internal.append("tracker-token")

    # --- Apply precedence ---
    if strong_internal:
        evidence.extend(strong_internal)
        channel = "startrek"
        if not weak_internal:
            warnings.append("startrek chosen but ~/.tracker-token not found; writes may fail")
    elif github_creds:
        evidence.extend(github_creds)
        channel = "github"
    elif weak_internal:
        evidence.extend(weak_internal)
        channel = "startrek"
    else:
        channel = "github"
        warnings.append("no credentials found; defaulting to github")

    return DetectResult(channel=channel, evidence=evidence, warnings=warnings)


def _real_path_exists(p: str) -> bool:
    return os.path.exists(os.path.expanduser(p))


if __name__ == "__main__":
    result = detect_channel(
        hostname=socket.getfqdn,
        has_command=lambda cmd: shutil.which(cmd) is not None,
        path_exists=_real_path_exists,
        getenv=os.environ.get,
    )
    print(result.channel)
    if result.evidence:
        print(f"evidence: {', '.join(result.evidence)}", file=sys.stderr)
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
