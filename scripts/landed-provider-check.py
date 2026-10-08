#!/usr/bin/env python3
"""Run one non-git landed-check provider: the command agentctl renders for a landed
spec whose ``provider`` is not ``git`` (see ``scripts/agentctl/landed_providers.py``).

Exit 0 when the provider reports the token landed on the target, 1 when it reports
not yet, and 97 (``LANDED_GIT_ERROR_EXIT``, the shared "could not answer" code) when
the provider cannot decide, its plugin is missing or broken, or the call raises. One
stderr line names the reason.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl.landed_providers import load_provider  # noqa: E402
from agentctl.state import LANDED_GIT_ERROR_EXIT  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--target", required=True)
    args = ap.parse_args(argv)
    try:
        verdict = load_provider(args.provider).is_landed(args.token, args.target)
    except Exception as exc:
        print(f"landed provider {args.provider!r}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return LANDED_GIT_ERROR_EXIT
    if verdict is True:
        return 0
    if verdict is False:
        return 1
    print(f"landed provider {args.provider!r} cannot decide for token {args.token!r}",
          file=sys.stderr)
    return LANDED_GIT_ERROR_EXIT


if __name__ == "__main__":
    sys.exit(main())
