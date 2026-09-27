#!/usr/bin/env python3
"""Measure, don't infer: PreToolUse `permissionDecision` ask/deny semantics
across permission modes, in headless (`claude -p`) children and an
interactive session, plus two `--add-dir` grant-materialization checks.

Difficulty removed: the Claude Code hooks reference documents `ask`/`deny`
as PreToolUse outcomes but is silent on how `ask` behaves in `-p` (headless,
no TTY) sessions, and on whether a requested `--permission-mode` even takes
effect under `auto`/`bypassPermissions` (a managed
`disableBypassPermissionsMode` has been observed to silently drop a
requested mode — see memory-global/leaves/spawning-specialists.md,
2026-08-04). Guard design (PR-C, a later stage of this plan) needs a real
decision table, not a guess, for what a `deny` vs an `ask` hook response
actually does per mode.

Method: every cell in the table is one real, bounded run of `claude` (via
Python `subprocess`/`tmux`, never a bare `claude` Bash invocation — the
program is reviewed as code, not grantable ad hoc), with a scratch
PreToolUse hook that unconditionally returns a fixed decision for any Bash
tool call. Nothing is inferred from documentation. A cell that cannot be
observed is recorded as such, never guessed.

Two isolation choices, made deliberately, not by omission:
  - The 8-cell {default, acceptEdits, auto, bypassPermissions} x {ask, deny}
    headless matrix runs inside `lib.host_llm.isolated_run_kwargs()`
    (CLAUDE_CONFIG_DIR pointed at a fresh empty home, borrowed auth) so the
    result documents the CLI's own behaviour, not this fleet's
    `settings/base.json` overlay (which pins `defaultMode: auto` and wires
    this fleet's own hooks). This is the "Hook decision semantics" table in
    docs/components/settings-and-permissions.md.
  - The 2 interactive cells and the 2 `--add-dir` cells run in the AMBIENT
    environment on purpose, for two separate reasons. The interactive
    cells stand for the interactive root, which always runs on this
    fleet's live settings chain (not an isolated one) -- an isolated,
    freshly-onboarded `CLAUDE_CONFIG_DIR` was tried first and routes the
    TUI into a browser-based OAuth login screen that cannot complete
    non-interactively, so only the ambient chain can observe what the
    interactive root actually sees; a live `deny`/`ask` decision still
    comes only from a scratch `--settings` file this script writes and
    tears down, so no live settings file is read or modified. The
    `--add-dir` cells run ambient for their own reason: cell 11
    (`accept_edits_read_add_dir`) checks that a scratch `Edit` deny holds
    under an explicit `--permission-mode acceptEdits`, and cell 12
    (`default_write_add_dir`) specifically measures what a spawned
    non-developer kind (no explicit `--permission-mode`, so it inherits
    whatever this fleet's own settings chain resolves to) can and cannot
    write into an added directory — that is the exact scenario
    spawn-specialist.py's write-add_dir materialization depends on, so it
    must be measured against the live chain, not an isolated one.

Every child is launched via `proc_tree.launch_supervised` + `kill_tree` (same
process-group teardown spawn-specialist.py itself relies on) and bounded by a
wall-clock timeout and a small `--max-budget-usd`, so an `ask` that never
resolves in a headless session degrades to a recorded timeout, never a hang.
The tmux interactive session is created and torn down by this script itself.

Usage:
  python3 scripts/probe-hook-decision-semantics.py --dry-run
  python3 scripts/probe-hook-decision-semantics.py --only headless
  python3 scripts/probe-hook-decision-semantics.py --only interactive
  python3 scripts/probe-hook-decision-semantics.py --only add_dir
  python3 scripts/probe-hook-decision-semantics.py           # everything

`--dry-run` prints the planned cells and the exact command for each, without
spawning anything. Otherwise, on completion, prints the markdown table (the
same content this script's caller pastes into
docs/components/settings-and-permissions.md) to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

def _dump_raw(raw_dump_dir: Path | None, name: str, raw: str) -> None:
    """Persist a child's raw stdout as evidence for the recorded cell -- so a
    surprising decision (e.g. a `deny` cell whose sentinel still ran) can be
    read back from the actual transcript, not re-inferred. Off by default
    (`raw_dump_dir is None`): no machine-specific path is baked into
    committed code; pass `--raw-dump-dir DIR` to opt in."""
    if raw_dump_dir is None:
        return
    raw_dump_dir.mkdir(parents=True, exist_ok=True)
    (raw_dump_dir / f"{name}.raw.txt").write_text(raw, encoding="utf-8")

import proc_tree  # noqa: E402  (launch_supervised / kill_tree — process-group teardown)
from lib import host_llm  # noqa: E402  (isolated_run_kwargs — CLAUDE_CONFIG_DIR sandbox)

HEADLESS_MODES = ("default", "acceptEdits", "auto", "bypassPermissions")
DECISIONS = ("ask", "deny")

CHILD_MODEL = "haiku"
CHILD_EFFORT = "low"
CHILD_BUDGET_USD = "0.15"
HEADLESS_TIMEOUT_S = 45
TMUX_BOOT_WAIT_S = 6
TMUX_RESPONSE_WAIT_S = 18
TMUX_TIMEOUT_S = 60

# Substrings observed in an interactive permission dialog. Not exhaustive by
# construction (the UI text is not a contract this script controls) — a
# session whose pane text matches none of these is recorded as
# prompt_shown=False, which is the conservative (no false positive) reading.
PROMPT_INDICATORS = (
    "do you want to proceed",
    "do you want to allow",
    "1. yes",
    "2. yes, and don't ask again",
    "bash command",
    "permission to",
)

DENIAL_INDICATORS = ("permission denied", "permission to use", "hook blocked", "denied by hook")

# A fresh, isolated CLAUDE_CONFIG_DIR (host_llm.isolated_run_kwargs) triggers
# first-run onboarding (theme picker, folder-trust dialog) before the actual
# session starts -- observed live: the first `send-keys "Enter"` dismissed
# the theme picker instead of submitting the task prompt, so the real prompt
# was never sent and the cell recorded a false "nothing happened". Detected
# and dismissed (accepting whatever default is pre-selected) before sending
# the real task.
ONBOARDING_INDICATORS = (
    "choose the text style", "trust the files", "let's get started", "select login method",
    "paste code here", "oauth/authorize",
)
ONBOARDING_DISMISS_ATTEMPTS = 4
ONBOARDING_DISMISS_WAIT_S = 3


@dataclass
class CellResult:
    cell_id: str
    requested_mode: str
    decision: str  # the hook's configured response, or "n/a" for add_dir cells
    effective_mode: str | None  # None = not observed
    sentinel_ran: bool | None  # None = not observed
    prompt_shown: bool | None  # None = not applicable / not observed
    timed_out: bool = False
    notes: str = ""


def _marker(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _decision_hook_script(tmpdir: Path, decision: str) -> Path:
    """Write a scratch PreToolUse hook that unconditionally returns `decision`
    (ask/deny) for any Bash tool call, and allow for everything else."""
    path = tmpdir / f"hook-{decision}.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "try:\n"
        "    payload = json.load(sys.stdin)\n"
        "except Exception:\n"
        "    payload = {}\n"
        "tool_name = payload.get('tool_name')\n"
        f"decision = {decision!r} if tool_name == 'Bash' else 'allow'\n"
        "print(json.dumps({'hookSpecificOutput': {\n"
        "    'hookEventName': 'PreToolUse',\n"
        "    'permissionDecision': decision,\n"
        "    'permissionDecisionReason': 'probe-hook-decision-semantics scratch hook',\n"
        "}}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _settings_with_hook(hook_path: Path, extra_perms: dict | None = None) -> dict:
    settings: dict = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": str(hook_path)}]}
            ]
        }
    }
    if extra_perms:
        settings["permissions"] = extra_perms
    return settings


def _run_child(cmd: list[str], prompt: str, *, env: dict, cwd: str, timeout_s: int) -> tuple[str, bool]:
    """Launch `cmd` (a full `claude ...` argv), feed `prompt` on stdin, and
    return (combined_stdout, timed_out). Never raises on timeout or on a
    non-zero exit — both are data, not a script failure."""
    proc = proc_tree.launch_supervised(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        cwd=cwd,
    )
    try:
        stdout, _ = proc.communicate(input=prompt, timeout=timeout_s)
        return stdout or "", False
    except subprocess.TimeoutExpired as exc:
        proc_tree.kill_tree(proc)
        partial = ""
        if exc.stdout:
            partial = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", "replace")
        return partial, True
    finally:
        if proc.poll() is None:
            proc_tree.kill_tree(proc)


def find_key(obj, names: set[str]):
    """Recursively search a decoded-JSON value for the first key (matched
    case-insensitively against `names`) and return its value, else None. Used
    to pull an effective-mode-shaped field out of a stream-json event without
    committing to one exact envelope shape (measured, not assumed)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in names:
                return v
        for v in obj.values():
            found = find_key(v, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_key(item, names)
            if found is not None:
                return found
    return None


_MODE_KEY_NAMES = {"permissionmode", "permission_mode", "mode"}


def parse_stream_json_effective_mode(raw_stdout: str) -> str | None:
    """Scan every JSON-parseable line of `raw_stdout` for a mode-shaped key.
    Returns the first hit, or None if no line parsed or none carried one."""
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        found = find_key(obj, _MODE_KEY_NAMES)
        if isinstance(found, str):
            return found
    return None


def sentinel_present(text: str, marker: str) -> bool:
    return marker in text


def parse_tool_results(raw_stdout: str) -> list[dict]:
    """Extract every `tool_result` content block (`{'content': str|Any,
    'is_error': bool}`) from a stream-json transcript. A denied Bash call's
    tool_use_id still echoes the marker in the ASSISTANT's proposed
    `tool_use.input.command` -- only a `tool_result` shows what the tool
    actually returned."""
    results: list[dict] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "user":
            continue
        content = obj.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results.append({"content": block.get("content"), "is_error": bool(block.get("is_error"))})
    return results


def command_executed(raw_stdout: str, marker: str) -> bool:
    """True iff `marker` appears in a non-error `tool_result` -- i.e. Bash
    actually ran and echoed it back. NOT `sentinel_present(raw_stdout, ...)`:
    that also matches a DENIED call, because the marker sits verbatim in the
    assistant's proposed `tool_use.input.command` regardless of outcome."""
    for result in parse_tool_results(raw_stdout):
        content = result["content"]
        text = content if isinstance(content, str) else json.dumps(content)
        if marker in text and not result["is_error"]:
            return True
    return False


def permission_denied(raw_stdout: str) -> bool:
    """True iff the CLI's own final `result` event recorded a permission
    denial (`permission_denials`) for this turn -- the authoritative signal,
    unlike pattern-matching hook/dialog text (`detect_denial`), whose
    observed wording ("hook error", "blocked by a PreToolUse hook") does not
    even match `DENIAL_INDICATORS`)."""
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") == "result" and obj.get("permission_denials"):
            return True
    return False


def detect_prompt(pane_text: str) -> bool:
    lowered = pane_text.lower()
    return any(indicator in lowered for indicator in PROMPT_INDICATORS)


def detect_denial(text: str) -> bool:
    lowered = text.lower()
    return any(indicator in lowered for indicator in DENIAL_INDICATORS)


def detect_onboarding(pane_text: str) -> bool:
    lowered = pane_text.lower()
    return any(indicator in lowered for indicator in ONBOARDING_INDICATORS)


def _dismiss_onboarding(session_name: str) -> None:
    """Accept whatever default onboarding dialog (theme picker, folder-trust)
    is pre-selected by sending Enter, re-checking the pane each time, until
    none of ONBOARDING_INDICATORS remain or the attempt budget is spent."""
    for _ in range(ONBOARDING_DISMISS_ATTEMPTS):
        capture = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-200"],
            timeout=10, capture_output=True, text=True, check=False,
        )
        if not detect_onboarding(capture.stdout):
            return
        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], timeout=10, check=False)
        time.sleep(ONBOARDING_DISMISS_WAIT_S)


def run_headless_cell(mode: str, decision: str, tmpdir: Path, raw_dump_dir: Path | None = None) -> CellResult:
    marker = _marker("PERM_PROBE_OK")
    hook_path = _decision_hook_script(tmpdir, decision)
    settings = _settings_with_hook(hook_path)
    kwargs = host_llm.isolated_run_kwargs()
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--max-budget-usd", CHILD_BUDGET_USD,
        "--model", CHILD_MODEL,
        "--effort", CHILD_EFFORT,
        "--settings", json.dumps(settings),
    ]
    if mode != "default":
        cmd += ["--permission-mode", mode]
    prompt = f"Use the Bash tool to run exactly this command and nothing else: echo {marker}"
    raw, timed_out = _run_child(cmd, prompt, env=kwargs["env"], cwd=kwargs["cwd"], timeout_s=HEADLESS_TIMEOUT_S)
    _dump_raw(raw_dump_dir, f"headless-{mode}-{decision}", raw)
    effective_mode = parse_stream_json_effective_mode(raw)
    ran = command_executed(raw, marker)
    denied = permission_denied(raw)
    notes = (
        "timed out waiting for the child" if timed_out
        else "permission denied (result.permission_denials)" if denied
        else "denial text observed" if detect_denial(raw)
        else ""
    )
    return CellResult(
        cell_id=f"headless:{mode}:{decision}",
        requested_mode=mode,
        decision=decision,
        effective_mode=effective_mode,
        sentinel_ran=ran,
        prompt_shown=False,  # -p is non-interactive: structurally no prompt UI is possible
        timed_out=timed_out,
        notes=notes,
    )


def build_interactive_inner_cmd(cwd: str, settings: dict) -> str:
    """The exact shell command line launched inside the tmux pane: ambient
    environment (no CLAUDE_CONFIG_DIR override, no isolated-home forwarding)
    so the session runs on this fleet's own live settings/login chain, the
    same chain the interactive root always runs on -- only a scratch
    `--settings` file supplies the decision hook."""
    return (
        f"cd {shlex.quote(cwd)} && "
        f"claude --permission-mode auto --model {CHILD_MODEL} --effort {CHILD_EFFORT} "
        f"--settings {shlex.quote(json.dumps(settings))}"
    )


def run_interactive_cell(decision: str, tmpdir: Path, raw_dump_dir: Path | None = None) -> CellResult:
    """Runs ambient (see module docstring's 'Two isolation choices'): the
    interactive root always runs on this fleet's live settings/login chain,
    not an isolated one, so only the ambient chain can observe what it
    actually sees. An isolated, freshly-onboarded CLAUDE_CONFIG_DIR was
    tried first and routes the TUI into a browser-based OAuth login screen
    that ignores a borrowed CLAUDE_CODE_OAUTH_TOKEN and cannot complete
    non-interactively (unlike `-p` headless mode) -- switching to ambient
    removes that gate entirely, since the ambient chain is already logged
    in. `_dismiss_onboarding` is kept as a defensive backstop in case a
    trust/theme dialog still appears; a decision hook still comes only from
    a scratch `--settings` file this script writes and tears down, so no
    live settings file is read or modified, and the tmux session is created
    and killed entirely by this script."""
    marker = _marker("PERM_PROBE_OK")
    hook_path = _decision_hook_script(tmpdir, decision)
    settings = _settings_with_hook(hook_path)
    session_name = f"probe-{uuid.uuid4().hex[:10]}"
    inner_cmd = build_interactive_inner_cmd(str(tmpdir), settings)
    pane_text = ""
    timed_out = False
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", inner_cmd],
            check=True, timeout=10,
        )
        time.sleep(TMUX_BOOT_WAIT_S)
        _dismiss_onboarding(session_name)
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name,
             f"Use the Bash tool to run exactly this command and nothing else: echo {marker}", "Enter"],
            check=True, timeout=10,
        )
        time.sleep(TMUX_RESPONSE_WAIT_S)
        capture = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-200"],
            check=True, timeout=10, capture_output=True, text=True,
        )
        pane_text = capture.stdout
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], timeout=10, check=False,
                        capture_output=True)

    _dump_raw(raw_dump_dir, f"interactive-auto-{decision}", pane_text)
    blocked_on_login = bool(pane_text) and not sentinel_present(pane_text, marker) and detect_onboarding(pane_text)
    if blocked_on_login:
        notes = (
            "not observed: ambient interactive TUI still routed to an "
            "onboarding/login screen (unexpected on an already-logged-in "
            "fleet chain); see raw dump if --raw-dump-dir was given"
        )
        return CellResult(
            cell_id=f"interactive:auto:{decision}",
            requested_mode="auto",
            decision=decision,
            effective_mode=None,
            sentinel_ran=None,
            prompt_shown=None,
            timed_out=timed_out,
            notes=notes,
        )
    return CellResult(
        cell_id=f"interactive:auto:{decision}",
        requested_mode="auto",
        decision=decision,
        effective_mode="auto" if pane_text else None,
        sentinel_ran=sentinel_present(pane_text, marker) if pane_text else None,
        prompt_shown=detect_prompt(pane_text) if pane_text else None,
        timed_out=timed_out,
        notes="captured tmux pane text below ceiling; see live.md for the raw capture" if pane_text else "no pane text captured",
    )


def run_add_dir_read_cell(tmpdir: Path, raw_dump_dir: Path | None = None) -> CellResult:
    """acceptEdits + --add-dir D + Edit(//D/**) deny: attempted write into D
    must be refused. Isolated (own scratch hooks/settings only) since we care
    about our own deny rule, not fleet guard interference."""
    d = Path(tempfile.mkdtemp(prefix="probe-readdir-"))
    target = d / "probe-write-test.txt"
    kwargs = host_llm.isolated_run_kwargs()
    settings = {"permissions": {"deny": [f"Edit(//{d}/**)"]}}
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json", "--verbose",
        "--max-budget-usd", CHILD_BUDGET_USD,
        "--model", CHILD_MODEL, "--effort", CHILD_EFFORT,
        "--permission-mode", "acceptEdits",
        "--add-dir", str(d),
        "--settings", json.dumps(settings),
    ]
    prompt = f"Use the Write tool to create the file {target} with the exact content OK. Do nothing else."
    raw, timed_out = _run_child(cmd, prompt, env=kwargs["env"], cwd=kwargs["cwd"], timeout_s=HEADLESS_TIMEOUT_S)
    _dump_raw(raw_dump_dir, "add_dir-accept_edits_read_add_dir", raw)
    wrote = target.exists()
    shutil.rmtree(d, ignore_errors=True)
    return CellResult(
        cell_id="add_dir:accept_edits_read_add_dir",
        requested_mode="acceptEdits",
        decision="n/a (Edit deny via permissions, no hook)",
        effective_mode=parse_stream_json_effective_mode(raw),
        sentinel_ran=wrote,
        prompt_shown=False,
        timed_out=timed_out,
        notes="sentinel_ran=True here means the deny FAILED to hold (write succeeded) -- expected False",
    )


def run_add_dir_write_cell(raw_dump_dir: Path | None = None) -> CellResult:
    """default mode (no --permission-mode; ambient/fleet chain applies on
    purpose -- see module docstring) + --add-dir D2 + Edit(//D2/**) allow +
    Edit(//D2/**/settings*.json) deny: write to D2/x must succeed, write to
    D2/settings.json must be refused. Two child runs, one cell."""
    d2 = Path(tempfile.mkdtemp(prefix="probe-writedir-"))
    settings = {
        "permissions": {
            "allow": [f"Edit(//{d2}/**)"],
            "deny": [f"Edit(//{d2}/**/settings*.json)"],
        }
    }
    common = [
        "claude", "-p",
        "--output-format", "stream-json", "--verbose",
        "--max-budget-usd", CHILD_BUDGET_USD,
        "--model", CHILD_MODEL, "--effort", CHILD_EFFORT,
        "--add-dir", str(d2),
        "--settings", json.dumps(settings),
    ]
    # AMBIENT env deliberately (os.environ, unmodified) -- see module docstring.
    ambient_env = dict(os.environ)
    ambient_cwd = str(d2)

    ok_target = d2 / "x"
    raw_ok, timed_out_ok = _run_child(
        common, f"Use the Write tool to create the file {ok_target} with the exact content OK. Do nothing else.",
        env=ambient_env, cwd=ambient_cwd, timeout_s=HEADLESS_TIMEOUT_S,
    )
    _dump_raw(raw_dump_dir, "add_dir-default_write_add_dir-ok", raw_ok)
    ok_written = ok_target.exists()

    blocked_target = d2 / "settings.json"
    raw_blocked, timed_out_blocked = _run_child(
        common, f"Use the Write tool to create the file {blocked_target} with the exact content {{}}. Do nothing else.",
        env=ambient_env, cwd=ambient_cwd, timeout_s=HEADLESS_TIMEOUT_S,
    )
    _dump_raw(raw_dump_dir, "add_dir-default_write_add_dir-blocked", raw_blocked)
    blocked_written = blocked_target.exists()

    shutil.rmtree(d2, ignore_errors=True)
    notes = (
        f"D2/x written={ok_written} (expected True); "
        f"D2/settings.json written={blocked_written} (expected False)"
    )
    return CellResult(
        cell_id="add_dir:default_write_add_dir",
        requested_mode="default (ambient fleet chain)",
        decision="n/a (Edit allow+deny via permissions, no hook)",
        effective_mode=parse_stream_json_effective_mode(raw_ok) or parse_stream_json_effective_mode(raw_blocked),
        sentinel_ran=ok_written and not blocked_written,
        prompt_shown=False,
        timed_out=timed_out_ok or timed_out_blocked,
        notes=notes,
    )


def render_table(results: list[CellResult]) -> str:
    header = (
        "| Cell | Requested mode | Decision | Effective mode | Sentinel ran | Prompt shown | Timed out | Notes |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for r in results:
        def fmt(v):
            if v is None:
                return "not observed"
            if isinstance(v, bool):
                return "yes" if v else "no"
            return str(v)
        rows.append(
            f"| `{r.cell_id}` | {r.requested_mode} | {r.decision} | {fmt(r.effective_mode)} | "
            f"{fmt(r.sentinel_ran)} | {fmt(r.prompt_shown)} | {fmt(r.timed_out)} | {r.notes} |"
        )
    return header + "\n".join(rows) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=("headless", "interactive", "add_dir"), default=None,
                   help="run only one group of cells; default: all groups (12 cells)")
    p.add_argument("--dry-run", action="store_true", help="print planned cells/commands; spawn nothing")
    p.add_argument("--raw-dump-dir", default=None,
                   help="optional dir to write each child's raw stdout/pane text for debugging "
                        "a surprising cell; default: no dump written")
    return p


def _plan() -> list[str]:
    plan = [f"headless:{m}:{d}" for m in HEADLESS_MODES for d in DECISIONS]
    plan += [f"interactive:auto:{d}" for d in DECISIONS]
    plan += ["add_dir:accept_edits_read_add_dir", "add_dir:default_write_add_dir"]
    return plan


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.dry_run:
        print("Planned cells (12 total):")
        for cell_id in _plan():
            if args.only and not cell_id.startswith(args.only):
                continue
            print(f"  {cell_id}")
        return 0

    raw_dump_dir = Path(args.raw_dump_dir) if args.raw_dump_dir else None

    results: list[CellResult] = []
    with tempfile.TemporaryDirectory(prefix="probe-hooks-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        if args.only in (None, "headless"):
            for mode in HEADLESS_MODES:
                for decision in DECISIONS:
                    print(f"running headless:{mode}:{decision} ...", file=sys.stderr)
                    results.append(run_headless_cell(mode, decision, tmpdir, raw_dump_dir))
        if args.only in (None, "interactive"):
            for decision in DECISIONS:
                print(f"running interactive:auto:{decision} ...", file=sys.stderr)
                results.append(run_interactive_cell(decision, tmpdir, raw_dump_dir))
        if args.only in (None, "add_dir"):
            print("running add_dir:accept_edits_read_add_dir ...", file=sys.stderr)
            results.append(run_add_dir_read_cell(tmpdir, raw_dump_dir))
            print("running add_dir:default_write_add_dir ...", file=sys.stderr)
            results.append(run_add_dir_write_cell(raw_dump_dir))

    print(render_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
