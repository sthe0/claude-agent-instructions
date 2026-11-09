"""Consumer-side check of apply-mcp-local.sh against the real `claude` CLI.

    python3 scripts/tests/manual_real_cli_mcp.py

Starts the real `claude` (`claude mcp list`, no model call), so the test suite never runs it:
run it by hand after a Claude Code or Cursor upgrade, or after an edit to apply-mcp-local.sh.
In a throwaway config root it registers one personal and one org-layer server with the script,
then asks the CLI which servers it sees and requires both. Exits 0 when the CLI lists both,
1 when it does not, 2 when no `claude` is on PATH.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from test_apply_mcp_local import Env, _stdio


def main() -> int:
    if shutil.which("claude") is None:
        print("no `claude` on PATH", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="manual-real-cli-mcp-") as tmp:
        env = Env(Path(tmp))
        suffix = uuid.uuid4().hex[:10]
        names = [f"corelocal-{suffix}", f"coreplugin-{suffix}"]
        exiting = _stdio(sys.executable, args=["-c", "import sys; sys.exit(0)"])
        env.login()
        env.define("local", names[0], exiting)
        env.define("plugin", names[1], exiting)
        applied = env.run()
        if applied.returncode != 0:
            print(f"apply-mcp-local.sh failed:\n{applied.stderr}", file=sys.stderr)
            return 1

        cwd = Path(tmp) / "empty-cwd"
        cwd.mkdir()
        cli_env = env.env()
        cli_env["CLAUDE_CONFIG_DIR"] = str(env.root)
        proc = subprocess.run(["claude", "mcp", "list"], env=cli_env, cwd=cwd,
                              capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        missing = [name for name in names if name not in out]
        if missing:
            print(f"`claude mcp list` does not list {missing}:\n{out}", file=sys.stderr)
            return 1
    print(f"ok: `claude mcp list` lists {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
