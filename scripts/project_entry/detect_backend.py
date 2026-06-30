"""Auto-detect the (workspace, tracker) backend pair for this machine.

detect_backends() is a pure function — every host-access operation is injected via
the three probe parameters, so tests run offline with no real host inspection. This
mirrors scripts/difficulty_channel/detect.py exactly (same injected-probe idiom).

Precedence (first match wins):
  1. `ya` + `arc` both on PATH (strong internal toolchain) -> ('arc', 'startrek')
  2. `gh` on PATH (GitHub CLI present)                      -> ('git', 'github')
  3. otherwise                                              -> ('git', 'none')

The else-branch IS the org-neutral default, so an unconfigured public machine
resolves deterministically to git/none. The strings 'arc'/'startrek' appear here
only as output NAMES — this module carries no arc/startrek implementation.
"""
from __future__ import annotations

import os
import shutil
from typing import Callable


def detect_backends(
    has_command: Callable[[str], bool],
    path_exists: Callable[[str], bool],
    getenv: Callable[[str], "str | None"],
) -> "tuple[str, str]":
    """Pure detection — all host I/O via the three injected probes.

    Returns (workspace, tracker) backend NAMES. path_exists / getenv are accepted
    for probe-signature parity with difficulty_channel.detect (and future signals);
    today's reachability rule needs only has_command.
    """
    if has_command("ya") and has_command("arc"):
        return ("arc", "startrek")
    if has_command("gh"):
        return ("git", "github")
    return ("git", "none")


def _real_path_exists(p: str) -> bool:
    return os.path.exists(os.path.expanduser(p))


if __name__ == "__main__":
    ws, tr = detect_backends(
        has_command=lambda cmd: shutil.which(cmd) is not None,
        path_exists=_real_path_exists,
        getenv=os.environ.get,
    )
    print(f"{ws} {tr}")
