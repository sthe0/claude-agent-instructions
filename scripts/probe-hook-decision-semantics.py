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
WORKTREE_ROOT = SCRIPTS_DIR.parent
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
    "choose the text style", "let's get started", "select login method",
    "paste code here", "oauth/authorize",
    # Observed live only after switching the interactive cwd to WORKTREE_ROOT
    # (see run_interactive_cell): this repo's own CLAUDE.md carries external
    # `@`-imports (`~/.claude-agent/config.md`, `~/.claude-agent/memory-global/
    # MEMORY.md`), which the TUI gates behind a one-time security dialog.
    # Its pre-selected default, "No, disable external imports", is safe to
    # accept via Enter -- unlike the folder-trust dialog's default, it does
    # not exit the session or grant anything; it degrades the child to
    # running without those imports, which this probe does not depend on.
    "accessing untrusted files", "external imports",
)
ONBOARDING_DISMISS_ATTEMPTS = 4
ONBOARDING_DISMISS_WAIT_S = 3

# Substrings observed in the folder-trust ("quick safety check") dialog --
# kept SEPARATE from ONBOARDING_INDICATORS on purpose: `_dismiss_onboarding`
# sends a blind Enter for the latter, and Enter on this dialog accepts its
# pre-selected default, which was observed live to be "No, exit" -- exiting
# the session outright. A cell that hits this dialog must never be
# auto-dismissed; it is recorded not-observed instead (see item 3 of the
# stage-6 fix-round-2 brief).
TRUST_DIALOG_INDICATORS = (
    "quick safety check", "trust this folder", "trust the files",
    "is this a project you created",
)


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


def _decision_hook_script(tmpdir: Path, decision: str, witness_path: Path) -> Path:
    """Write a scratch PreToolUse hook that unconditionally returns `decision`
    (ask/deny) for any Bash tool call, and allow for everything else. Every
    invocation appends one JSON line to `witness_path` before printing its
    decision -- the only proof a cell has that the hook actually ran for the
    sentinel call, as opposed to the child never reaching a tool-use attempt
    at all (e.g. stuck at a pre-task dialog). See `_hook_fired_for_bash`."""
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
        f"with open({str(witness_path)!r}, 'a', encoding='utf-8') as _w:\n"
        "    _w.write(json.dumps({'tool_name': tool_name, 'decision': decision}) + chr(10))\n"
        "print(json.dumps({'hookSpecificOutput': {\n"
        "    'hookEventName': 'PreToolUse',\n"
        "    'permissionDecision': decision,\n"
        "    'permissionDecisionReason': 'probe-hook-decision-semantics scratch hook',\n"
        "}}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _hook_fired_for_bash(witness_path: Path) -> bool:
    """True iff the scratch hook's witness file shows at least one invocation
    for a Bash tool call -- i.e. the child actually reached a Bash tool-use
    attempt and the hook ran, regardless of what decision it returned."""
    if not witness_path.exists():
        return False
    for line in witness_path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("tool_name") == "Bash":
            return True
    return False


def _tail_evidence(text: str, max_chars: int = 200) -> str:
    """A short, single-line tail of `text` for a not-observed cell's notes --
    enough to see what the child was actually doing, without dumping a whole
    transcript/pane capture into the docs table."""
    text = text.strip()
    if not text:
        return "(empty)"
    return text[-max_chars:].replace("\n", " | ")


def _not_observed_result(cell_id: str, requested_mode: str, decision: str, timed_out: bool, notes: str) -> CellResult:
    return CellResult(
        cell_id=cell_id, requested_mode=requested_mode, decision=decision,
        effective_mode=None, sentinel_ran=None, prompt_shown=None,
        timed_out=timed_out, notes=notes,
    )


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


def detect_trust_dialog(pane_text: str) -> bool:
    lowered = pane_text.lower()
    return any(indicator in lowered for indicator in TRUST_DIALOG_INDICATORS)


# Substrings observed (or, for the auto/bypass cases, documented) in the
# TUI's bottom mode-status footer line. Not exhaustive by construction (this
# script does not control the UI's wording) -- a pane whose footer matches
# none of these is recorded as effective_mode=None, the conservative (no
# false positive) reading, same policy as PROMPT_INDICATORS.
MODE_FOOTER_INDICATORS = ("mode on", "accept edits on", "bypassing permissions")


def parse_interactive_footer(pane_text: str) -> str | None:
    """Return the TUI's mode-status footer line, stripped, KEPT LITERAL (not
    normalized to a bool) -- observed live: the deny cell's footer read
    "⏸ manual mode on", meaning the requested `auto` mode was NOT in effect,
    which a hard-coded "auto" would have hidden. Scans bottom-up for the
    last non-empty line matching MODE_FOOTER_INDICATORS; returns None if no
    such line is present (e.g. the ask dialog replaces the footer entirely)."""
    for line in reversed(pane_text.splitlines()):
        stripped = line.strip()
        if stripped and any(ind in stripped.lower() for ind in MODE_FOOTER_INDICATORS):
            return stripped
    return None


def _dismiss_onboarding(session_name: str) -> str | None:
    """Accept whatever default onboarding dialog (theme picker, login screen)
    is pre-selected by sending Enter, re-checking the pane each time, until
    none of ONBOARDING_INDICATORS remain or the attempt budget is spent.
    Returns None on success. Never sends Enter into a folder-trust dialog
    (its default was observed live to be "No, exit", which would kill the
    session) -- returns "trust_dialog" immediately instead, without
    dismissing anything, so the caller can record the cell not-observed.
    Returns "onboarding_budget_exhausted" if neither resolves within the
    attempt budget."""
    for _ in range(ONBOARDING_DISMISS_ATTEMPTS):
        capture = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-200"],
            timeout=10, capture_output=True, text=True, check=False,
        )
        pane = capture.stdout
        if detect_trust_dialog(pane):
            return "trust_dialog"
        if not detect_onboarding(pane):
            return None
        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], timeout=10, check=False)
        time.sleep(ONBOARDING_DISMISS_WAIT_S)
    return "onboarding_budget_exhausted"


def run_headless_cell(mode: str, decision: str, tmpdir: Path, raw_dump_dir: Path | None = None) -> CellResult:
    marker = _marker("PERM_PROBE_OK")
    witness_path = tmpdir / f"witness_{_marker('w')}.log"
    hook_path = _decision_hook_script(tmpdir, decision, witness_path)
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
    cell_id = f"headless:{mode}:{decision}"
    if not _hook_fired_for_bash(witness_path):
        notes = f"not observed: hook never fired (last stream evidence: {_tail_evidence(raw)})"
        return _not_observed_result(cell_id, mode, decision, timed_out, notes)
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
        cell_id=cell_id,
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


def build_tmux_new_session_argv(session_name: str, cwd: str, inner_cmd: str, env: dict) -> list[str]:
    """`tmux new-session` argv, forwarding exactly `CLAUDE_CONFIG_DIR` from
    `env` (via `-e`) when present, and nothing else -- observed live: a tmux
    session inherits the tmux SERVER's environment, not this process's, so
    without an explicit `-e` the child loses `CLAUDE_CONFIG_DIR` and reports
    "Login expired" instead of running on this fleet's own login chain."""
    argv = ["tmux", "new-session", "-d", "-s", session_name]
    value = env.get("CLAUDE_CONFIG_DIR")
    if value:
        argv += ["-e", f"CLAUDE_CONFIG_DIR={value}"]
    argv += ["-x", "220", "-y", "50", inner_cmd]
    return argv


def _build_interactive_cell_result(
    decision: str, pane_text: str, footer_text: str | None, sentinel_ran: bool,
    hook_fired: bool, trust_blocked: bool, timed_out: bool,
) -> CellResult:
    """Pure decision logic for the interactive cell, factored out of
    `run_interactive_cell` so it is unit-testable without tmux/subprocess.
    A cell counts as observed only if the hook actually fired for the
    sentinel call (`hook_fired`, from the witness file) and the session
    never got stuck at the folder-trust dialog (`trust_blocked`).
    `sentinel_ran` is supplied by the caller from a FILE the sentinel command
    itself creates -- never derived from matching the marker text in
    `pane_text`, which also matches the marker sitting unsubmitted in an
    `ask` dialog's still-pending prompt box (round-4 bug this replaces).
    `effective_mode` is the raw, literal footer text (see
    `parse_interactive_footer`), never a hard-coded "auto"."""
    cell_id = f"interactive:auto:{decision}"
    if trust_blocked:
        notes = (
            "not observed: hit the folder-trust dialog despite launching in "
            f"the trusted worktree root; last pane: {_tail_evidence(pane_text)}"
        )
        return _not_observed_result(cell_id, "auto", decision, timed_out, notes)
    if not hook_fired:
        notes = f"not observed: hook never fired (last pane: {_tail_evidence(pane_text)})"
        return _not_observed_result(cell_id, "auto", decision, timed_out, notes)
    notes = f"hook fired; footer: {footer_text!r}" if footer_text else "hook fired; no footer captured"
    return CellResult(
        cell_id=cell_id,
        requested_mode="auto",
        decision=decision,
        effective_mode=footer_text,
        sentinel_ran=sentinel_ran,
        prompt_shown=detect_prompt(pane_text) if pane_text else None,
        timed_out=timed_out,
        notes=notes,
    )


def run_interactive_cell(decision: str, tmpdir: Path, raw_dump_dir: Path | None = None) -> CellResult:
    """Runs ambient (see module docstring's 'Two isolation choices'): the
    interactive root always runs on this fleet's live settings/login chain,
    not an isolated one, so only the ambient chain can observe what it
    actually sees. The tmux pane's cwd is the WORKTREE_ROOT (not a scratch
    mktemp dir) -- spawns already run there, so it is already trusted and
    the folder-trust ("quick safety check") dialog is not expected to
    appear; `_dismiss_onboarding` still checks for it defensively on every
    poll and, if seen, returns "trust_dialog" without ever sending Enter
    into it (its default was observed live to be "No, exit", which exits
    the session). The scratch hook/settings/witness files still live in
    `tmpdir` (a mktemp dir), referenced by absolute path -- a decision hook
    comes only from a scratch `--settings` file this script writes and
    tears down, so no live settings file is read or modified, and the tmux
    session is created and killed entirely by this script.

    Round-4 fixes (see the plan's stage-6 brief): the task prompt is sent as
    a literal paste (`send-keys -l`) followed by a SEPARATE `Enter` send --
    one `send-keys <text> "Enter"` call lands the whole thing as a paste
    with the Enter swallowed, so the prompt never submits. `CLAUDE_CONFIG_DIR`
    is forwarded into the tmux server via `-e` (a tmux session inherits the
    tmux SERVER's environment, not this process's -- without it the child
    reports "Login expired"). The sentinel is a FILE the child's own Bash
    command creates, not a marker matched in the pane text (which also
    matches the marker sitting unsubmitted in an `ask` dialog's pending
    prompt box). The footer (mode status line) is captured once before the
    prompt is sent -- the `ask` dialog can hide it -- and kept as-is if the
    post-response capture shows none."""
    marker = _marker("PERM_PROBE_OK")
    witness_path = tmpdir / f"witness_{_marker('w')}.log"
    sentinel_path = tmpdir / f"sentinel_{marker}"
    hook_path = _decision_hook_script(tmpdir, decision, witness_path)
    settings = _settings_with_hook(hook_path)
    session_name = f"probe-{uuid.uuid4().hex[:10]}"
    inner_cmd = build_interactive_inner_cmd(str(WORKTREE_ROOT), settings)
    pane_text = ""
    footer_text: str | None = None
    timed_out = False
    trust_blocked = False
    try:
        subprocess.run(
            build_tmux_new_session_argv(session_name, str(WORKTREE_ROOT), inner_cmd, os.environ),
            check=True, timeout=10,
        )
        time.sleep(TMUX_BOOT_WAIT_S)
        dismiss_result = _dismiss_onboarding(session_name)
        if dismiss_result == "trust_dialog":
            trust_blocked = True
        else:
            pre_capture = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-200"],
                timeout=10, capture_output=True, text=True, check=False,
            )
            footer_text = parse_interactive_footer(pre_capture.stdout)
            prompt_text = (
                f"Use the Bash tool to run exactly this command and nothing else: "
                f"echo {marker} > {sentinel_path}"
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", prompt_text],
                check=True, timeout=10,
            )
            time.sleep(2)
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                check=True, timeout=10,
            )
            time.sleep(TMUX_RESPONSE_WAIT_S)
        capture = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-200"],
            check=True, timeout=10, capture_output=True, text=True,
        )
        pane_text = capture.stdout
        if footer_text is None:
            footer_text = parse_interactive_footer(pane_text)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], timeout=10, check=False,
                        capture_output=True)

    _dump_raw(raw_dump_dir, f"interactive-auto-{decision}", pane_text)
    hook_fired = _hook_fired_for_bash(witness_path)
    sentinel_ran = sentinel_path.exists()
    return _build_interactive_cell_result(
        decision, pane_text, footer_text, sentinel_ran, hook_fired, trust_blocked, timed_out
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
