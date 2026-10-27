"""Two real opaque-code judge calls, printed: the one manual check the stubbed tests cannot make.

Difficulty removed: `test_code_effects.py` stubs the judge runner, so it cannot show that the real
`claude -p` judge (isolated, low tier, quoted-data prompt) answers in the closed schema, how long it
takes against `resource-grant-judge-timeout-s`, or that it refuses code it cannot see. Run this after a
change to the judge prompt or the timeouts, and when measuring the live latency the hook budget rests on.

    python3 scripts/code_effects_live_probe.py

It makes two calls (one `claude -p` each, low tier) and prints for each the parsed verdict and the
wall-clock latency:
  1. `python3 -c` that writes a file in a scratch git repository -> expected: one file write effect;
  2. code importing a local module whose text is not shown -> expected: UNDETERMINABLE.
Nothing is written outside a temporary directory; the cache is not used.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import advisor, code_effects, tool_contracts  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve() / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        command = "python3 -c \"open('probe-out.txt', 'w').write('x')\""
        segment = next(s for s in tool_contracts.resolve_segments(command, str(repo)).segments if s.status == "opaque")
        collected = code_effects.collect(segment)
        print(f"1. collect: {collected}")
        started = time.monotonic()
        verdict = advisor.judge_code_effects(collected.code, str(repo))
        print(f"   judge ({time.monotonic() - started:.2f}s, timeout {advisor.code_effects_judge_timeout_s()}s): {verdict}")

        hidden = "import unseen_local_module\nunseen_local_module.go()\n"
        started = time.monotonic()
        verdict = advisor.judge_code_effects(hidden, str(repo))
        print(f"2. code importing an unseen local module\n   judge ({time.monotonic() - started:.2f}s): {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
