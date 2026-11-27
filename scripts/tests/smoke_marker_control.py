#!/usr/bin/env python3
"""Live smoke control for the live:core-marker check (R3, core-gate-machine-independent).

Runs the check the way scripts/lib/instruction_smoke_gate.py runs it — a sandbox
built by instruction-sandbox.sh, verified through instruction-sandbox-verify.sh —
N times on fresh intact sandboxes and M times on a deliberately broken load (the
sandbox CLAUDE.md's import of the marker file removed, so CLAUDE.md and every
other instruction file still load but the marker file never does), recording per
run the verdict, the reason code (the first word of the check's detail), the CLI
argv actually launched, the CLI exit, the InstructionsLoaded load record, the
structured-reply diagnostic and a bounded, redacted reply excerpt.

Usage (probe mode — one run, raw evidence, no verdict assertions):
  smoke_marker_control.py --probe --out <file.json> [--model haiku]
Usage (control mode):
  smoke_marker_control.py --runs <n> --broken-runs <m> --out <file.json> [--model haiku]

Exit: 0 every run completed a CLI session and the verdicts came out as required
      (intact runs PASS, broken runs FAIL marker-missing with a non-empty load
      record), 1 otherwise, 3 when the launches were unavailable, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
LIVE = SCRIPTS / "instruction-sandbox-live.py"
VERIFY = SCRIPTS / "instruction-sandbox-verify.sh"

BUILD_TIMEOUT_S = 600
LAUNCH_TIMEOUT_S = 480
STATIC_TIMEOUT_S = 900
VERIFY_TIMEOUT_S = LAUNCH_TIMEOUT_S + STATIC_TIMEOUT_S
MODEL = "haiku"

EXIT_OK, EXIT_BAD, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3

RUN_RECORD = "live-marker-run.json"


def _run(argv: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s,
        env={**dict(os.environ), "AGENT_TELEMETRY_TEST": "1"},
    )


def git_head(repo: Path = REPO) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def build_sandbox(repo: Path, sha: str, root: Path) -> None:
    proc = _run([str(SCRIPTS / "instruction-sandbox.sh"),
                 "--source", str(repo), "--core-ref", sha, "--root", str(root)],
                BUILD_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(f"sandbox build failed (exit {proc.returncode}): {proc.stderr[-400:]}")


def register_load_hook(root: Path, record_file: Path) -> dict:
    """Append one InstructionsLoaded hook to the sandbox settings; return the hooks key."""
    settings_path = root / "home" / ".claude-agent" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("InstructionsLoaded", []).append({
        "hooks": [{"type": "command", "command": f"cat >> {record_file}", "timeout": 10}]
    })
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings["hooks"]


def write_marker(file: Path, token: str) -> None:
    """Replace any earlier marker, then append one plain-text marker line (verify.sh's write_marker)."""
    lines = [ln for ln in file.read_text(encoding="utf-8").splitlines()
             if not ln.startswith("SANDBOX-MARKER: ")]
    lines += ["", f"SANDBOX-MARKER: {token}"]
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_tree(parent: Path) -> None:
    """Remove a throwaway sandbox tree. The sandboxed CLI session flushes
    telemetry into its HOME after its launch returns; that late write races a
    single rm -rf (ENOTEMPTY), so the removal retries until the tree is gone."""
    for attempt in range(4):
        subprocess.run(["rm", "-rf", "--", str(parent)], check=False)
        if not parent.exists():
            return
        time.sleep(0.5 * (attempt + 1))


# ── probe mode ─────────────────────────────────────────────────────────────

def probe(out_path: Path, model: str) -> int:
    sha = git_head()
    parent = Path(tempfile.mkdtemp(prefix="isb-probe-sbx.", dir="/tmp"))
    root = parent / "sbx"
    record_file = root / "home" / "isb-load-record.jsonl"
    token = "probe-token"
    evidence: dict = {"repo_sha": sha, "model": model}
    try:
        build_sandbox(REPO, sha, root)
        evidence["settings_hooks"] = register_load_hook(root, record_file)
        write_marker(root / "core" / "config.md", token)
        proc = _run(["python3", str(LIVE), "--root", str(root), "--cwd", str(root),
                     "--expect", token, "--model", model],
                    LAUNCH_TIMEOUT_S)
        evidence["live_exit"] = proc.returncode
        evidence["live_stdout"] = proc.stdout[-2000:]
        evidence["live_stderr"] = proc.stderr[-500:]
        evidence["load_record_raw"] = (record_file.read_text(encoding="utf-8", errors="replace")
                                       if record_file.exists() else None)
    except subprocess.TimeoutExpired as exc:
        evidence["probe_error"] = f"timeout: {exc}"
    except Exception as exc:  # probe only: raw evidence, never retried
        evidence["probe_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        remove_tree(parent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"probe evidence written to {out_path}")
    return EXIT_OK


# ── control mode ────────────────────────────────────────────────────────────

def break_marker_import(root: Path) -> None:
    """The deliberately broken load: remove the sandbox CLAUDE.md's import of the
    file carrying the marker (config.md), so CLAUDE.md and every other
    instruction file still load but the marker file never does."""
    claude_md = (root / "home" / ".claude-agent" / "CLAUDE.md").resolve()
    lines = claude_md.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines
            if not (ln.lstrip().startswith("@") and ln.rstrip().endswith("config.md"))]
    claude_md.write_text("\n".join(kept) + "\n", encoding="utf-8")


def core_marker_line(verify_stdout: str) -> "tuple[str, str]":
    """(status, reason-code-or-reason) of the live:core-marker CHECK line."""
    for raw in verify_stdout.splitlines():
        if not raw.startswith("CHECK live:core-marker "):
            continue
        fields = raw.split(None, 3)
        detail = fields[3].split()[0] if len(fields) > 3 and fields[3].split() else ""
        return fields[2], detail
    raise RuntimeError("verify output carries no live:core-marker CHECK line")


def one_run(sha: str, model: str, broken: bool) -> dict:
    parent = Path(tempfile.mkdtemp(prefix="isb-smoke-sbx.", dir="/tmp"))
    root = parent / "sbx"
    try:
        build_sandbox(REPO, sha, root)
        if broken:
            break_marker_import(root)
        try:
            proc = _run([str(VERIFY), "--timeout", str(LAUNCH_TIMEOUT_S),
                         "--model", model, str(root)], VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"verdict": "UNAVAILABLE", "reason_code": "verify-timeout",
                    "argv": [], "exit": None, "session_completed": False,
                    "load_record": [], "reply_match": {}, "reply_excerpt": ""}
        status, detail = core_marker_line(proc.stdout)
        run: dict = {"verdict": status, "reason_code": detail}
        run_path = root / RUN_RECORD
        if run_path.exists():
            recorded = json.loads(run_path.read_text(encoding="utf-8"))
            run.update({key: recorded.get(key) for key in
                        ("argv", "exit", "session_completed", "load_record",
                         "reply_match", "reply_excerpt")})
        return run
    finally:
        remove_tree(parent)


def control(runs: int, broken_runs: int, out_path: Path, model: str) -> int:
    sha = git_head()
    intact = [one_run(sha, model, broken=False) for _ in range(runs)]
    broken_records = [one_run(sha, model, broken=True) for _ in range(broken_runs)]
    out = {"repo_sha": sha, "runs": intact, "broken": {"runs": broken_records}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    every = intact + broken_records
    if any(run["verdict"] == "UNAVAILABLE" for run in every):
        print("launches unavailable:", [run["reason_code"] for run in every
                                        if run["verdict"] == "UNAVAILABLE"])
        return EXIT_UNAVAILABLE
    problems = []
    if not all(run["verdict"] == "PASS" for run in intact):
        problems.append(f"intact verdicts {[run['verdict'] for run in intact]}")
    if not all(run["verdict"] == "FAIL" and run["reason_code"] == "marker-missing"
               for run in broken_records):
        problems.append(f"broken verdicts {[(r['verdict'], r['reason_code']) for r in broken_records]}")
    if not all(run.get("session_completed") is True for run in every):
        problems.append("a run did not complete a CLI session")
    if not all(run.get("load_record") for run in broken_records):
        problems.append("a broken run kept no load record")
    if not all(run.get("reply_excerpt") for run in broken_records):
        problems.append("a non-PASS run kept no reply excerpt")
    if problems:
        print("; ".join(problems))
        return EXIT_BAD
    print(f"control PASS: {len(intact)} intact PASS, "
          f"{len(broken_records)} broken FAIL marker-missing")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true",
                      help="one run, raw evidence, no verdict assertions")
    mode.add_argument("--runs", type=int, metavar="N",
                      help="intact-sandbox runs (control mode)")
    parser.add_argument("--broken-runs", type=int, metavar="M", default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    if args.runs is not None:
        if args.runs < 0 or args.broken_runs < 0:
            parser.error("--runs and --broken-runs must be non-negative")
        return control(args.runs, args.broken_runs, args.out, args.model)
    if args.broken_runs:
        parser.error("--broken-runs needs --runs")
    return probe(args.out, args.model)


if __name__ == "__main__":
    sys.exit(main())