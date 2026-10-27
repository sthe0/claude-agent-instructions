"""Three real opaque-code judge calls, printed: the one manual check the stubbed tests cannot make.

Difficulty removed: `test_code_effects.py` stubs the judge runner, so it cannot show that the real
`claude -p` judge (isolated, low tier, quoted-data prompt) answers in the closed schema, how long it
takes against `resource-grant-judge-timeout-s`, or that it refuses code it cannot name the effects of.
Run this after a change to the judge prompt or the timeouts, and when measuring the live latency the hook
budget rests on.

    python3 scripts/code_effects_live_probe.py

Each case makes one `claude -p` call (low tier) and prints the verdict and its wall-clock latency:
  1. `python3 -c` that writes a file in a scratch git repository -> expected: one file write effect;
  2. code calling a module whose text is not shown (the judge alone; `collect` would refuse earlier)
     -> expected: UNDETERMINABLE;
  3. code that runs `gh pr merge` through subprocess -- readable arguments, an effect the schema cannot
     express -> expected: UNDETERMINABLE, never an empty effect list.
Nothing is written outside a temporary directory; the verdict cache lives there too.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import advisor, code_effects, tool_contracts  # noqa: E402


def _opaque(command: str, repo: Path):
    return next(s for s in tool_contracts.resolve_segments(command, str(repo)).segments if s.status == "opaque")


def _timed(label: str, call):
    started = time.monotonic()
    result = call()
    print(f"{label}\n   ({time.monotonic() - started:.2f}s, judge timeout {advisor.code_effects_judge_timeout_s()}s): {result}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve() / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        cache = Path(tmp).resolve() / "cache"

        write = _opaque("python3 -c \"open('probe-out.txt', 'w').write('x')\"", repo)
        _timed("1. a file write", lambda: code_effects.effects_of(write, [write], cache_dir=cache))

        _timed(
            "2. a module whose text is not shown",
            lambda: advisor.judge_code_effects("import unseen_local_module\nunseen_local_module.go()\n", str(repo)),
        )

        merge = _opaque("python3 -c \"import subprocess; subprocess.run(['gh', 'pr', 'merge', '1'])\"", repo)
        _timed("3. gh pr merge through subprocess", lambda: code_effects.effects_of(merge, [merge], cache_dir=cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
