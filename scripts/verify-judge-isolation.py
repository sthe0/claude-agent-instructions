#!/usr/bin/env python3
"""Witness that a single-turn judge call really is isolated, authenticated, and
non-recursive — by driving the SHIPPED `host_llm.isolated_run_kwargs()`.

    python3 scripts/verify-judge-isolation.py             # the live probe
    python3 scripts/verify-judge-isolation.py --self-test # RED arm, no API call
    python3 scripts/verify-judge-isolation.py --r5-audit  # enumeration, no API call

SPENDS LIVE API QUOTA. Two `claude -p` calls per run. It must never be added to
pytest, to a pre-commit hook, or to any suite that runs unattended; it is a
final_check, invoked deliberately. `--self-test` and `--r5-audit` are free.

It replaces two controls that could not fail. The first asserted that no
descendant process was named `claude` — constant-true, since the installed
`claude` is a symlink to the same binary the client self-forks as. The second
asserted a "3x smaller" context against a constant fitted to one measurement.
What stands here instead is measured in the same run, relative rather than
frozen, and durable rather than sampled.

The four arms and why each is the arm it is:

  RATIO   isolated total input context < 0.5 * ambient, both measured in THIS
          run. A frozen constant decays the moment the instruction surface
          changes; a ratio measures the thing the isolation is for.
  FLOOR   isolated total input context > 1000. Deliberately an order of
          magnitude below the ~23k an isolated call actually loads and an order
          of magnitude above the 0 an UNAUTHENTICATED one reports. Its only job
          is to separate "a call happened" from "no call happened" — the exact
          hole through which stage 1 shipped a judge that answered nothing while
          the ratio arm read 0 and called it a pass. A tighter floor would turn
          ordinary prompt drift into a false failure.
  ANSWER  the judge's actual verdict against a known-verdict fixture, not merely
          is_error=false. An authenticated call that returns an error string
          still has a non-zero context.
  WITNESS exactly ONE session transcript under the probe's own slot,
          `<slot>/home/projects/**/*.jsonl`. This IS the no-recursion clause in
          measurable form.

Why the own-slot scope is the right one for detecting recursion — the obvious
objection being that a nested call would get its own slot and never touch the
parent's: the recursion this closes is not a nested call through the seam. It is
the CLIENT's hook machinery invoking `claude` from inside the child, inheriting
the child's CLAUDE_CONFIG_DIR — which IS this slot's home — so a recursing child
deposits its extra transcripts in exactly the tree being counted. Scoped to
`projects/` because `sessions/` and `backups/` live under the same home.
Verified at review: a session that dies at auth (bogus token, is_error=true,
zero tokens) still writes exactly one transcript, so the witness is not blind in
the failure regime.

The witness's honest limit, stated rather than glossed: it sees a nested session
only if that session shares this slot's config root. One that does not —
spawn-specialist.py, which writes to the ambient root — is invisible here.

NO slot count is asserted. The root legitimately holds other live sessions'
slots plus swept residue, and pruning runs at the START of the next
isolated_run_kwargs() call, so it can erase a nested child's slot before a count
could read it.

The AMBIENT baseline is live but SIDE-EFFECT-FREE: it runs against a snapshot
root built from a FILE allowlist, never a wholesale copy. A wholesale copy would
duplicate .credentials.json onto disk and violate the very requirement this
probe exists to enforce. The real ambient root registers dozens of hooks against
stage 2's three guards, so an unstripped baseline arm would, on every run,
auto-start an agentctl session, register a session scope able to deny the real
one, post into the user's Telegram groups and advance the *-due sentinel stamps.
settings.json is REBUILT from an allowlist of keys rather than by deleting
`hooks`: a denylist only removes what someone remembered, and the side-effecting
surface also includes mcpServers, statusLine, apiKeyHelper and whatever a future
client version adds. The snapshot keeps exactly the surface that produces the
context cost, which is the whole point of the baseline. Both arms authenticate
through the same borrowed CLAUDE_CODE_OAUTH_TOKEN, so the snapshot needs no
credential file at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import host_llm  # noqa: E402
from lib.config_root import harness_config_root  # noqa: E402
from lib.runtime_models import HOST_CLAUDE  # noqa: E402

MODEL = "sonnet"
CALL_TIMEOUT_S = 180

MAX_RATIO = 0.5
TOKEN_FLOOR = 1000

# A fixture with one defensible answer, phrased the way a judge prompt is, so a
# model that is authenticated but confused fails the ANSWER arm rather than
# sliding through on is_error=false.
FIXTURE_PROMPT = (
    "You are a strict binary classifier. Reply with exactly one word, uppercase, "
    "no punctuation and no explanation.\n\n"
    "Question: is 7 greater than 3?\n"
    "Answer YES or NO."
)
FIXTURE_ANSWER = "YES"

# The ambient surface that produces the context cost. An allowlist, so a newly
# added side-effecting file is excluded by default rather than by amendment.
_SNAPSHOT_PATHS = ("CLAUDE.md", "config.md", "memory-global", "skills")
# Keys kept when settings.json is rebuilt. `env` shapes the child's environment;
# everything else in the live file either registers a hook, starts a server or
# runs a command.
_SNAPSHOT_SETTINGS_KEYS = ("env",)


# --- pure comparisons -------------------------------------------------------
#
# Each returns None on GREEN and a failure message on RED, and each is unit
# tested against both directions in scripts/tests/test_host_llm.py. --self-test
# gives RED to only the WITNESS arm, and this stage's principle forbids shipping
# a control whose RED has never been observed.

def check_ratio(isolated: int, ambient: int, max_ratio: float = MAX_RATIO) -> str | None:
    if ambient <= 0:
        return f"ambient baseline loaded {ambient} tokens — no baseline to compare against"
    if isolated >= max_ratio * ambient:
        return (
            f"isolated context {isolated} is not below {max_ratio:g}x the ambient "
            f"baseline {ambient} (limit {max_ratio * ambient:.0f})"
        )
    return None


def check_floor(isolated: int, floor: int = TOKEN_FLOOR) -> str | None:
    if isolated <= floor:
        return (
            f"isolated context {isolated} is at or below the floor {floor} — "
            "the call loaded nothing, which is what an unauthenticated judge reports"
        )
    return None


def check_answer(text: str, expected: str = FIXTURE_ANSWER) -> str | None:
    got = (text or "").strip().strip(".").upper()
    if got != expected.upper():
        return f"judge answered {got!r}, expected {expected!r}"
    return None


def check_transcript_count(paths: list[Path]) -> str | None:
    if len(paths) != 1:
        return (
            f"expected exactly 1 session transcript under the probe's own slot, "
            f"found {len(paths)}: {[str(p) for p in paths]}"
        )
    return None


def total_input_tokens(usage: dict) -> int:
    """Every input-side token the call loaded, cached or not.

    `input_tokens` alone undercounts by an order of magnitude once the ambient
    surface is cached, which would make the ratio arm compare noise.
    """
    return sum(
        int(usage.get(k, 0) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


# --- the live arms ----------------------------------------------------------

def _argv_with_json(prompt: str) -> list[str]:
    argv = host_llm.build_prompt_argv(HOST_CLAUDE, MODEL, prompt)
    return argv[:-1] + ["--output-format", "json"] + argv[-1:]


def _run(env: dict, cwd: str, label: str) -> tuple[int, str]:
    """(total input tokens, result text) for one live call."""
    proc = subprocess.run(
        _argv_with_json(FIXTURE_PROMPT),
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_S,
        env=env,
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"{label} arm: `claude -p` exited {proc.returncode}\n{proc.stderr[:2000]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"{label} arm: non-JSON output\n{proc.stdout[:2000]}")
    if payload.get("is_error"):
        raise SystemExit(f"{label} arm: is_error=true — {payload.get('result')!r}")
    return total_input_tokens(payload.get("usage") or {}), str(payload.get("result", ""))


def build_ambient_snapshot(dest: Path) -> Path:
    """A hooks-free stand-in for the ambient config root, from an allowlist."""
    ambient = harness_config_root()
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for name in _SNAPSHOT_PATHS:
        src = ambient / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dest / name, symlinks=True, ignore_dangling_symlinks=True)
        else:
            shutil.copy2(src, dest / name)
    try:
        live = json.loads((ambient / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        live = {}
    kept = {k: live[k] for k in _SNAPSHOT_SETTINGS_KEYS if k in live}
    (dest / "settings.json").write_text(json.dumps(kept, indent=2), encoding="utf-8")
    return dest


def transcripts_in(slot_home: Path) -> list[Path]:
    return sorted((slot_home / "projects").rglob("*.jsonl"))


def credential_fingerprint() -> tuple[str, int] | None:
    """(sha256, mode) of the fleet's live credential file, or None if absent.

    Reads bytes only to hash them; the value is never held, printed or written.
    """
    path = harness_config_root() / host_llm._CREDENTIALS_FILENAME
    try:
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return digest, stat.S_IMODE(st.st_mode)


def assert_no_ancestor_project_context(cwd: Path, failures: list[str]) -> None:
    """The client discovers project context by walking cwd's ancestors, so a
    neutral-looking directory under a shared /var/tmp is only neutral by
    accident — which is why this is an assertion and not an assumption."""
    for parent in [cwd, *cwd.parents]:
        for name in ("CLAUDE.md", "AGENTS.md", ".claude"):
            if (parent / name).exists():
                failures.append(f"ancestor project context found at {parent / name}")


# --- entry points -----------------------------------------------------------

def run_self_test() -> int:
    """Demonstrate the WITNESS arm's RED against a mutated WORLD, not a mutated
    check: plant a second transcript and run the shipped predicate unchanged."""
    kwargs = host_llm.isolated_run_kwargs()
    slot_home = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
    planted = slot_home / "projects" / "self-test"
    planted.mkdir(parents=True, exist_ok=True)
    try:
        for name in ("real.jsonl", "recursed.jsonl"):
            (planted / name).write_text("{}\n", encoding="utf-8")
        failure = check_transcript_count(transcripts_in(slot_home))
    finally:
        shutil.rmtree(planted, ignore_errors=True)
    if failure is None:
        print("SELF-TEST BROKEN: the witness accepted a planted recursion", file=sys.stderr)
        return 0  # green here means the control is dead — the caller expects non-zero
    print(f"self-test RED as designed: {failure}")
    return 1


def run_r5_audit() -> int:
    """Report ambient-root entries and settings keys carrying no disposition."""
    entries, keys = host_llm.undispositioned_ambient_items()
    for name in entries:
        print(f"undispositioned entry: {name}")
    for key in keys:
        print(f"undispositioned settings key: {key}")
    if entries or keys:
        return 1
    print("r5: every ambient entry and settings key carries a disposition")
    return 0


def run_probe() -> int:
    kwargs = host_llm.isolated_run_kwargs()
    slot_home = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
    slot_cwd = Path(kwargs["cwd"])
    status = kwargs["env"][host_llm.JUDGE_TOKEN_STATUS_ENV_VAR]
    print(f"sandbox root : {host_llm._SANDBOX_ROOT}")
    print(f"slot         : {slot_cwd.parent}")
    print(f"token status : {status}")
    if status not in host_llm.AUTHENTICATED_TOKEN_STATUSES:
        print(f"FAIL: the child would run with no credential ({status})", file=sys.stderr)
        return 1

    before = credential_fingerprint()

    isolated_tokens, answer = _run(kwargs["env"], kwargs["cwd"], "isolated")
    witness = transcripts_in(slot_home)

    ambient_env = dict(kwargs["env"])
    ambient_env["CLAUDE_CONFIG_DIR"] = str(
        build_ambient_snapshot(host_llm._SANDBOX_ROOT / f"{os.getpid()}-ambient-snapshot")
    )
    ambient_tokens, _ = _run(ambient_env, kwargs["cwd"], "ambient")

    after = credential_fingerprint()

    print(f"isolated ctx : {isolated_tokens}")
    print(f"ambient ctx  : {ambient_tokens}")
    print(f"answer       : {answer.strip()!r}")
    print(f"transcripts  : {[p.name for p in witness]}")

    failures = [
        f for f in (
            check_ratio(isolated_tokens, ambient_tokens),
            check_floor(isolated_tokens),
            check_answer(answer),
            check_transcript_count(witness),
        ) if f
    ]
    assert_no_ancestor_project_context(slot_cwd, failures)

    # r6's write clause is carried STRUCTURALLY — the child never receives a path
    # to the live file — and controlled hard by final_check 4. This arm only
    # reports, because the ambient session may legitimately rotate its own token
    # inside the probe's window and a hard equality would be flaky. It fails only
    # on effects a refresh does not produce: removal, or a permission change.
    if before is None and after is None:
        print("credential   : absent before and after (nothing to protect)")
    elif before is not None and after is None:
        failures.append("the fleet's live credential file was REMOVED during the probe")
    elif before is None and after is not None:
        print("credential   : appeared during the probe (inconclusive, not ours)")
    elif before[1] != after[1]:
        failures.append(
            f"credential file permissions changed {oct(before[1])} -> {oct(after[1])}"
        )
    elif before[0] != after[0]:
        print("credential   : contents changed (inconclusive — a legitimate refresh)")
    else:
        print("credential   : unchanged by hash")

    for name in ("home", "cwd", host_llm._CREDENTIALS_FILENAME):
        if (host_llm._SANDBOX_ROOT / name).exists():
            failures.append(f"unexpected root-level entry {name} under the sandbox root")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("OK: isolated, authenticated, under budget, and non-recursive")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--self-test", action="store_true",
        help="plant a second transcript and show the witness goes RED (no API call)",
    )
    parser.add_argument(
        "--r5-audit", action="store_true",
        help="report ambient entries/settings keys with no disposition (no API call)",
    )
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    if args.r5_audit:
        return run_r5_audit()
    return run_probe()


if __name__ == "__main__":
    sys.exit(main())
