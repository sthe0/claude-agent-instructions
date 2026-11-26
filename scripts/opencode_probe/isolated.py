"""The isolated-run rule: every OpenCode run's environment lives under one fresh root R.

R is created directly under ``<state>/runs/`` with a ``probe-run`` marker; a runs/ entry
without ``record.json`` is no record, so ``check-record`` never reads R. HOME, the agent
home (``CLAUDE_AGENT_HOME`` / ``CLAUDE_CONFIG_DIR``, which ``config_root.agent_home()``
reads before HOME), every XDG directory and the OpenCode config locations point into R.
Before a case runs, a child process with that environment asserts that the agent home
and each path the run uses lie under R; otherwise the case is ``unobserved``. The runner
removes the roots of a passing case; the roots of any other case stay as evidence (without
the binary copy, which every world drops on exit).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import core

MARKER = "probe-run"
AGENT_HOME_FILES = ("CLAUDE.md", "config.md", "memory-global/MEMORY.md")
AGENT_HOME_DIRS = ("skills", "agents")
PATH_KEYS = ("HOME", "CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
             "XDG_STATE_HOME", "XDG_CACHE_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR")
CREATED_ROOTS: list[Path] = []
KEPT_FROM_CALLER = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "AGENT_OPENCODE_PROBE_STATE")


class ScratchNotInEffect(Exception):
    """The isolated environment did not take effect; the case is ``unobserved``."""


def make_root(state: Path | None = None) -> Path:
    """A fresh run root directly under ``<state>/runs`` (``mktemp -d -p``) with its marker."""
    runs = (state or core.state_dir()) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="root.", dir=runs)).resolve()
    (root / MARKER).write_text("opencode probe run root\n", encoding="utf-8")
    CREATED_ROOTS.append(root)
    return root


def fill_agent_home(root: Path, repo_root: Path | None = None) -> Path:
    """Byte copies of the agent-home sources of the repo the suite runs from, under
    R/home/.claude-agent, so ``skills_dir()`` and the view generator find them under R."""
    repo_root = repo_root or core.REPO_ROOT
    agent_home = root / "home" / ".claude-agent"
    agent_home.mkdir(parents=True, exist_ok=True)
    for rel in AGENT_HOME_FILES:
        source = repo_root / rel
        if source.is_file():
            (agent_home / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, agent_home / rel)
    for rel in AGENT_HOME_DIRS:
        source = repo_root / rel
        if source.is_dir():
            shutil.copytree(source, agent_home / rel, symlinks=False, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".git"))
    return agent_home


def isolated_env(root: Path, caller: dict | None = None) -> dict[str, str]:
    """The environment of every OpenCode run of the suite (see the module docstring)."""
    root = Path(root).resolve()
    caller = dict(os.environ if caller is None else caller)
    env = {key: caller[key] for key in KEPT_FROM_CALLER if key in caller}
    home = root / "home"
    agent_home = home / ".claude-agent"
    for path in (home, agent_home, root / "xdg" / "config", root / "xdg" / "data",
                 root / "xdg" / "state", root / "xdg" / "cache", root / "opencode-config"):
        path.mkdir(parents=True, exist_ok=True)
    fill_agent_home(root)
    env.update({
        "HOME": str(home),
        "CLAUDE_AGENT_HOME": str(agent_home),
        "CLAUDE_CONFIG_DIR": str(agent_home),
        "XDG_CONFIG_HOME": str(root / "xdg" / "config"),
        "XDG_DATA_HOME": str(root / "xdg" / "data"),
        "XDG_STATE_HOME": str(root / "xdg" / "state"),
        "XDG_CACHE_HOME": str(root / "xdg" / "cache"),
        "OPENCODE_CONFIG": str(root / "opencode.json"),
        "OPENCODE_CONFIG_DIR": str(root / "opencode-config"),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    })
    return env


def place_binary(root: Path, source: str | None = None) -> str:
    """A copy of the binary under test at ``R/bin/opencode``. OpenCode lists the directory above
    the binary's own ``bin`` as a config directory, so running the installed
    ``~/.opencode/bin/opencode`` would load the user's ``~/.opencode`` into an isolated run; a
    copy, not a link, so nothing written through R can reach the installed file."""
    source = source or core.opencode_bin()
    target = root / "bin" / "opencode"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(source, target)
    return str(target)


def drop_binary(root: Path) -> None:
    shutil.rmtree(root / "bin", ignore_errors=True)


def remove_root(root: Path) -> None:
    """Delete a run root made by ``make_root``; anything else is left alone."""
    root = Path(root)
    if root.parent.name == "runs" and root.name.startswith("root.") and (root / MARKER).is_file():
        shutil.rmtree(root)


HOME_STAND_INS = {
    ".claude/CLAUDE.md": "stand-in global instructions\n",
    ".claude/skills/stand-in/SKILL.md": "---\nname: stand-in\ndescription: stand-in\n---\nstand-in\n",
    ".agents/skills/stand-in-agents/SKILL.md":
        "---\nname: stand-in-agents\ndescription: stand-in\n---\nstand-in\n",
    ".config/opencode/opencode.json": "{}\n",
    ".opencode/stand-in.txt": "stand-in\n",
}


def seed_home_stand_ins(root: Path) -> dict[str, str]:
    """Stand-ins of the HOME-level OpenCode and Claude locations; {relative path: sha256}
    so a case can show a write through HOME left them byte-identical."""
    home = root / "home"
    for rel, text in HOME_STAND_INS.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return home_stand_in_digests(root)


def home_stand_in_digests(root: Path) -> dict[str, str]:
    home = root / "home"
    return {rel: hashlib.sha256((home / rel).read_bytes()).hexdigest() if (home / rel).is_file()
            else "missing" for rel in HOME_STAND_INS}


_ASSERT_SNIPPET = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from lib import config_root
print(json.dumps({"agent_home": os.path.realpath(str(config_root.agent_home())),
                  "paths": {k: os.path.realpath(os.environ.get(k, "")) for k in sys.argv[2:]}}))
"""


def assert_scratch_in_effect(root: Path, env: dict[str, str]) -> None:
    """Raise ScratchNotInEffect unless, in a child process with ``env``, the agent home and
    every path of PATH_KEYS lie under R."""
    root_real = os.path.realpath(root)
    scripts_dir = str(core.REPO_ROOT / "scripts")
    result = subprocess.run([sys.executable, "-c", _ASSERT_SNIPPET, scripts_dir, *PATH_KEYS],
                            env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ScratchNotInEffect(f"assertion child failed: {result.stderr[-300:]}")
    seen = json.loads(result.stdout)
    outside = {k: v for k, v in {"agent_home": seen["agent_home"], **seen["paths"]}.items()
               if not (v == root_real or v.startswith(root_real + os.sep))}
    if outside:
        raise ScratchNotInEffect(f"paths outside the run root: {sorted(outside.items())}")
