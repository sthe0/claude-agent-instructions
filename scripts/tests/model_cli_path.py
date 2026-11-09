"""PATH values for a test child that must reach no real model CLI.

The stubs directory holds a guard under every model CLI name (`claude`, `agent`,
`cursor-agent`); conftest puts it first on the session PATH. A test that builds a child's PATH
by hand either puts the stubs directory ahead of every system directory
(`path_with_arc_stub`) or simulates a host with no model CLI at all (`path_without_model_cli`).
Stdlib only, so a helper script run outside pytest can import it.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

STUBS_DIR = str(Path(__file__).resolve().parent / "stubs")
MODEL_CLI_NAMES = ("claude", "agent", "cursor-agent")
LAUNCH_LOG_ENV = "CORE_TESTS_LAUNCH_LOG"
LAUNCH_MARKER = "real model launch blocked"
# Where a guard records a launch from a child whose environment has no LAUNCH_LOG_ENV; the
# guards derive the same name from their own directory.
UNATTRIBUTED_LAUNCH_LOG = Path("/tmp") / f"core-tests-model-cli-launches{STUBS_DIR.replace('/', '_')}.log"

_no_model_cli_dirs: dict[bool, str] = {}


def path_with_arc_stub(*leading: str, system: str = "/usr/bin:/bin") -> str:
    """A PATH for a test that builds its own: the test's `leading` stub directories, then the
    stubs directory (the stub arc, and the guards standing in for every model CLI), then
    `system`. A literal PATH that reaches the real launcher in a fresh HOME makes it download
    a release; one that reaches a real model CLI starts a model."""
    return os.pathsep.join(p for p in (*leading, STUBS_DIR, system) if p)


def path_without_model_cli(*leading: str, real_arc: bool = False) -> str:
    """The test's `leading` directories, then one directory linking every program of /usr/bin
    and /bin except the model CLIs. `arc` links to the stub arc unless `real_arc`: a literal
    PATH that reaches the real launcher in a fresh HOME makes it download a release."""
    if real_arc not in _no_model_cli_dirs:
        links = Path(tempfile.mkdtemp(prefix="core-tests-no-model-cli-"))
        atexit.register(shutil.rmtree, links, True)
        if not real_arc:
            (links / "arc").symlink_to(Path(STUBS_DIR) / "arc")
        for system in ("/usr/bin", "/bin"):
            for entry in os.scandir(system):
                if entry.name not in MODEL_CLI_NAMES and not os.path.lexists(links / entry.name):
                    (links / entry.name).symlink_to(entry.path)
        _no_model_cli_dirs[real_arc] = str(links)
    return os.pathsep.join((*leading, _no_model_cli_dirs[real_arc]))
