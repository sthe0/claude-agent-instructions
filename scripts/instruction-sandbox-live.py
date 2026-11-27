#!/usr/bin/env python3
"""Launch one tool-less `claude -p` inside an instruction-sandbox root and check
that the instructions it LOADED carry the expected marker tokens.

Usage: instruction-sandbox-live.py --root <root> --cwd <dir> --expect <token> [--expect <token> ...]
                                   [--timeout <seconds>] [--model <alias>]
       instruction-sandbox-live.py --root <root> --cwd <dir> --probe hook-gate-deny
                                   [--timeout <seconds>] [--model <alias>] [--stream-out <file>]

The verdict comes from the CLI's own load record and nothing else: before the
launch an `InstructionsLoaded` hook (registered in the sandbox settings) appends
one JSON line per loaded instruction file to <root>/isb-load-record.jsonl, and
that record decides whatever the reply or the CLI exit say:

  some loaded file whose content carries an expected token  -> PASS
  >= 1 instruction file loaded, no loaded file carries it   -> FAIL marker-missing
  no record + the CLI completed a session                   -> FAIL load-record-missing
  no record + no completed session                          -> UNAVAILABLE, naming it

A record that exists but names no file counts as no record. The reply, read
through structured output (`--output-format json --json-schema`, one string
field `marker`), is a diagnostic only: its class (match / wrapped / no-token /
no-field) is recorded on every run and never changes the verdict.

`--probe hook-gate-deny` runs one turn with the Edit tool enabled, asked to edit a
file in a private /dev/shm directory while the session sits before an execution node; it passes
when the stream carries the state gate's deny text and the file is unchanged (output
`pass denied`, or `fail <no-tool-call|no-gate-deny|file-changed|deny-and-changed> result=<excerpt>`; exit 0 / 1 / 3 as below).

The child runs under the sandbox HOME / agent root, with every built-in tool
disabled, so it can only answer from the instructions the client loaded through
the redirected paths — it cannot read the marker file itself. Credentials are
lent through the environment (never copied into the sandbox).

Output, one line each: `reply <token> <class>` per expected token, only when
the launch completed a session (a launch that did not prints its single
`UNAVAILABLE <reason>` line and nothing else); on FAIL
`reason <code> [missing=<tokens>]`, `load-record <files>` and `reply_excerpt
<redacted bounded text>`.
Every run also writes <root>/live-marker-run.json (verdict, reason_code, argv,
exit, session_completed, load_record, reply_match, reply_excerpt).
A failed launch's reason is `launch-exit=<n>` followed by an excerpt of its output
(the stream's final result text, else the last non-empty line), token-shaped strings redacted.
Exit:  0  PASS: the load record shows a loaded file carrying every token
       1  FAIL marker-missing (a load record exists, no loaded file carries a
          token) or FAIL load-record-missing (a completed session left no record)
          — never mapped to UNAVAILABLE
       3  UNAVAILABLE: `auth`, `timeout`, `launch-exit=<n>`, `launch-error`,
          `no-probe-dir` or `probe-dir-exempt`;
          a launch that did not complete says nothing about the instructions
       2  usage
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import exempt_paths  # noqa: E402
from lib import host_llm  # noqa: E402
from lib.runtime_models import HOST_CLAUDE  # noqa: E402

# The fleet-calibrated `claude -p` bound (docs/operations/advisor-timeout-calibration.md):
# measured on inputs up to ~200k chars, above the ~80k-char always-loaded surface
# a full-CLAUDE.md launch carries.
DEFAULT_TIMEOUT_S = 480
DEFAULT_MODEL = "haiku"

EXIT_PASS, EXIT_MISSING, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3

LAUNCH_EXCERPT_LIMIT = 200
REPLY_EXCERPT_LIMIT = 400
LOAD_RECORD_NAME = "isb-load-record.jsonl"
RUN_RECORD_NAME = "live-marker-run.json"
LOAD_RECORD_MAX_FILES = 32
LOAD_RECORD_PATH_LIMIT = 200
# Defence in depth: a long unbroken run of token characters is never a diagnosis worth keeping.
TOKEN_SHAPED = re.compile(r"sk-[\w-]{8,}|[A-Za-z0-9_.\-]{32,}")
MARKER_PLACEHOLDER = "<MARKER>"
# The structured-output contract the marker probe asks the CLI for: the whole
# reply is one string field (any wrapping text around the token value stays
# inside it, and its class is recorded, never gating).
MARKER_JSON_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"marker": {"type": "string"}},
    "required": ["marker"],
    "additionalProperties": False,
})

# Caller-session variables that would redirect the child's state, identity or
# plugin lookups out of the sandbox, where the canon snapshot cannot see them.
SCRUB_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "AGENT_LINEAGE_IDS",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_POLICY_LEDGER",
    "CLAUDE_PROJECT_PLUGIN_DIR",
    "CLAUDE_DIFFICULTY_PLUGIN_DIR",
    "CLAUDE_AUTH_PROFILE_DIR",
    "CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR",
    "CLAUDE_INSTRUCTIONS_CANON",
)

PROMPT = (
    "Reply with exactly the value that follows 'SANDBOX-MARKER: ' in your "
    "instructions, one value per line, and nothing else."
)
# The label PROMPT names; a model sometimes repeats it before the value.
MARKER_LABEL = "SANDBOX-MARKER:"
QUOTE_CHARS = "`'\""


def reply_line_value(line: str) -> str:
    """The value one reply line carries, with an optional repeated marker label dropped.

    A label with nothing after it leaves an empty value, which matches no token.
    """
    value = line.strip().strip(QUOTE_CHARS).strip()
    if value.startswith(MARKER_LABEL):
        value = value[len(MARKER_LABEL):]
    return value.strip().strip(QUOTE_CHARS)

# ── the marker verdict (R3): decided by the load record alone ───────────────

@dataclass(frozen=True)
class MarkerOutcome:
    verdict: str            # PASS | FAIL | UNAVAILABLE
    reason_code: str        # "" | marker-missing | load-record-missing | <launch reason>
    session_completed: bool
    reply_match: dict       # token -> match | wrapped | no-token | no-field
    load_record: list       # loaded instruction file paths, bounded
    reply_excerpt: str      # bounded, redacted raw CLI result text
    missing: tuple          # expected tokens no loaded file carries


def register_load_hook(root: Path, record_file: Path) -> None:
    """Idempotently register the InstructionsLoaded hook in the sandbox settings.

    The hook appends each loaded file's payload (one JSON line naming `file_path`)
    to the record file; the paths the CLI enumerates are the evidence the verdict
    rests on, so the child itself never learns the record path through its
    environment — only the hook command holds it."""
    settings_path = root / "home" / ".claude-agent" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError:
            settings = {}
    command = f"cat >> {record_file}"
    entries = settings.setdefault("hooks", {}).setdefault("InstructionsLoaded", [])
    if not any(h.get("command") == command
               for entry in entries for h in entry.get("hooks", [])
               if isinstance(h, dict)):
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def parse_load_record(text: str) -> list[str]:
    """The loaded instruction file paths the record names; a record that exists
    but names no file counts as no record."""
    paths: list[str] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict)
                and event.get("hook_event_name") == "InstructionsLoaded"
                and isinstance(event.get("file_path"), str)):
            paths.append(event["file_path"][:LOAD_RECORD_PATH_LIMIT])
    return paths[:LOAD_RECORD_MAX_FILES]


def parse_session(stdout: str) -> "tuple[bool, object]":
    """(session_completed, result): the CLI completed a session iff its stdout is
    the JSON envelope of a non-error result. Error results, non-JSON output and
    killed launches all read as no completed session."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return False, None
    if not isinstance(envelope, dict) or envelope.get("type") != "result" or envelope.get("is_error"):
        return False, None
    return True, envelope.get("result")


def reply_class(result: object, token: str) -> str:
    """The reply's diagnostic class; never a verdict. The field is stripped and
    compared to the token exactly — as the one value on the one field it is, or
    as one value per line of a multi-token reply."""
    if isinstance(result, dict) and isinstance(result.get("marker"), str):
        value = result["marker"].strip()
        if value == token or token in value.splitlines():
            return "match"
        if token in value:
            return "wrapped"
        return "no-token"
    return "no-field"


def redacted_reply(result: object, expect: "tuple[str, ...]",
                   limit: int = REPLY_EXCERPT_LIMIT) -> str:
    """The raw CLI result text, bounded and redacted: each expected token becomes
    `<MARKER>`, token-shaped strings become `[redacted]`, and nothing longer than
    the byte cap is kept."""
    text = result if isinstance(result, str) else json.dumps(result)
    for token in expect:
        text = text.replace(token, MARKER_PLACEHOLDER)
    text = TOKEN_SHAPED.sub("[redacted]", text)
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _read_text_or_none(path: str) -> "str | None":
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def token_in_file(read_file, path: str, token: str) -> bool:
    """A loaded file carries the token iff one of its lines holds it as the
    marker value (label and quoting tolerated, as in the reply)."""
    content = read_file(path)
    return content is not None and any(
        reply_line_value(line) == token for line in content.splitlines())


def marker_verdict(record_paths: list[str], expect: "tuple[str, ...]",
                   session_completed: bool, result: object,
                   unavailable_reason: str, read_file) -> MarkerOutcome:
    """The total verdict mapping. A present load record decides whatever the
    reply or the CLI exit say; marker-missing and load-record-missing are never
    UNAVAILABLE; the reply never turns a FAIL into a PASS."""
    reply_match = {token: reply_class(result, token) for token in expect}
    excerpt = redacted_reply(result, expect)
    load_record = list(record_paths)[:LOAD_RECORD_MAX_FILES]
    if load_record:
        missing = tuple(token for token in expect
                        if not any(token_in_file(read_file, path, token)
                                    for path in load_record))
        if missing:
            return MarkerOutcome("FAIL", "marker-missing", session_completed,
                                 reply_match, load_record, excerpt, missing)
        return MarkerOutcome("PASS", "", session_completed, reply_match, load_record, excerpt, ())
    if session_completed:
        return MarkerOutcome("FAIL", "load-record-missing", session_completed,
                             reply_match, [], excerpt, tuple(expect))
    reason = unavailable_reason or "unknown"
    return MarkerOutcome("UNAVAILABLE", reason.split()[0] if reason.split() else reason,
                         session_completed, reply_match, [], excerpt, tuple(expect))


HOOK_PROBE_PROMPT = (
    "Read the file {path} with the Read tool, then use the Edit tool once to replace the text "
    "'original' with 'changed' in it. Do not retry the edit, and report the Edit result in one line."
)
PROBE_ORIGINAL = "original\n"
# The state gate's deny prefix (scripts/hook-state-gate.py deny_with).
HOOK_DENY_TEXT = "agentctl session is in node="
PROBE_DIR_PARENT = "/dev/shm"


def build_child_env(caller_env: dict, root: Path) -> tuple[dict, str]:
    """Return (child env, credential status). `caller_env` must be the caller's
    UNMODIFIED environment: `_lend_auth` finds the credentials through
    `harness_config_root()`, i.e. the real CLAUDE_CONFIG_DIR, before any
    sandbox override is applied."""
    env = dict(caller_env)
    # Private seam: the one place that already owns borrow-the-token-into-the-env.
    status = host_llm._lend_auth(env)
    for name in list(env):
        if name in SCRUB_VARS or name.startswith("AGENTCTL_"):
            del env[name]
    home = root / "home"
    agent_root = home / ".claude-agent"
    env.update(
        AGENT_TELEMETRY_TEST="1",
        HOME=str(home),
        CLAUDE_AGENT_HOME=str(agent_root),
        CLAUDE_CONFIG_DIR=str(agent_root),
        CLAUDE_INSTRUCTIONS_REPO=str(root / "core"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
    )
    return env, status


def build_argv(model: str) -> list[str]:
    # `--tools ""` is variadic, so the prompt goes over stdin, not as a trailing arg.
    argv = host_llm.build_launch_argv(HOST_CLAUDE, model, lean=True, structured_output=True)
    return argv + ["--json-schema", MARKER_JSON_SCHEMA, "--tools", ""]


def build_hook_probe_argv(model: str) -> list[str]:
    """One turn with Read and Edit enabled (Edit refuses an unread file before any hook runs),
    so the PreToolUse state gate can fire. The
    stream carries the tool result, where the gate's deny text is observable; every
    permission is pre-granted, so only a hook can refuse the edit."""
    argv = host_llm.build_launch_argv(HOST_CLAUDE, model, lean=True)
    return argv + ["--output-format", "stream-json", "--verbose",
                   "--permission-mode", "bypassPermissions", "--tools", "Read,Edit"]


def _tool_result_texts(stream: str) -> list[str]:
    texts = []
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if event.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                texts.append(str(body))
    return texts


def _tool_use_count(stream: str) -> int:
    count = 0
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    return count


def hook_probe_diagnosis(stream: str, before: str, after: str) -> tuple[str, str]:
    """Return (reason, excerpt of the tool results): `pass`, `no-tool-call`, `no-gate-deny`,
    `file-changed` or `deny-and-changed`. PASS iff a tool result carries the state gate's
    deny text and the file is unchanged."""
    results = _tool_result_texts(stream)
    excerpt = " | ".join(" ".join(text.split()) for text in results)[:400]
    denied = any(HOOK_DENY_TEXT in text for text in results)
    changed = before != after
    if denied and changed:
        return "deny-and-changed", excerpt
    if denied:
        return "pass", excerpt
    if changed:
        return "file-changed", excerpt
    if not results and not _tool_use_count(stream):
        return "no-tool-call", excerpt
    return "no-gate-deny", excerpt


def hook_probe_verdict(stream: str, before: str, after: str) -> bool:
    return hook_probe_diagnosis(stream, before, after)[0] == "pass"


@contextlib.contextmanager
def _child_scratch_environment(env: dict):
    """Apply the child's scratch-root settings to this process, restoring the parent's after."""
    saved = {k: os.environ.get(k) for k in ("TMPDIR", "HOME", exempt_paths.SCRATCH_ROOTS_ENV)}
    for key in saved:
        if key in env:
            os.environ[key] = env[key]
        else:
            os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def probe_target_is_gated(target: Path, env: dict) -> bool:
    """The state gate's own verdict on the target, under the child's scratch-root settings."""
    with _child_scratch_environment(env):
        return exempt_paths.is_gated_path(str(target))


def containing_scratch_root(target: Path, env: dict) -> str | None:
    """The most specific scratch root, under the child's settings, that contains the target."""
    with _child_scratch_environment(env):
        resolved = os.path.realpath(str(target))
        containing = [root for root in exempt_paths.scratch_roots()
                      if resolved.startswith(os.path.realpath(root) + os.sep)]
    return max(containing, key=lambda root: len(os.path.realpath(root)), default=None)


def probe_dir_exempt_detail(probe_dir: Path, env: dict) -> str:
    """The UNAVAILABLE reason when the gate exempts the probe dir: which scratch root holds it
    and, when the child's TMPDIR is what makes that root a scratch root, why."""
    root = containing_scratch_root(probe_dir / "probe-target.py", env)
    if root is None:
        return f"probe-dir-exempt: the state gate exempts the probe dir {probe_dir}, though no scratch root contains it"
    detail = f"probe-dir-exempt: the probe dir {probe_dir} is inside the scratch root {root}, which the state gate exempts"
    if env.get("TMPDIR"):
        detail += f"; TMPDIR={env['TMPDIR']} (the temp dir choice) puts it there, so point TMPDIR outside every scratch root"
    return detail


def run_hook_probe(model: str, env: dict, cwd: Path, timeout: float,
                   stream_out: Path | None = None) -> tuple[str, str]:
    """Return ('pass'|'fail', detail) or ('unavailable', reason).

    The sandbox lives under /tmp, which the state gate exempts by path, so the target is a
    real file in a private directory outside /tmp, removed afterwards."""
    try:
        probe_dir = Path(tempfile.mkdtemp(prefix="isb-hook-probe.", dir=PROBE_DIR_PARENT))
    except OSError:
        return "unavailable", "no-probe-dir"
    try:
        target = probe_dir / "probe-target.py"
        target.write_text(PROBE_ORIGINAL)
        if not probe_target_is_gated(target, env):
            return "unavailable", probe_dir_exempt_detail(probe_dir, env)
        kind, payload, _ = run_launch(build_hook_probe_argv(model), env, cwd, timeout,
                                      HOOK_PROBE_PROMPT.format(path=target))
        if kind == "unavailable":
            return kind, payload
        if stream_out is not None:
            stream_out.parent.mkdir(parents=True, exist_ok=True)
            stream_out.write_text(payload)
        reason, excerpt = hook_probe_diagnosis(payload, PROBE_ORIGINAL, target.read_text())
        if reason == "pass":
            return "pass", "denied"
        return "fail", f"{reason} result={excerpt!r}"
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _clean_line(text: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = TOKEN_SHAPED.sub("[redacted]", " ".join(text.split()))
    return text[:LAUNCH_EXCERPT_LIMIT]


def _last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _stream_cause(out: str) -> str:
    """The final result event's text, else the last stdout line that is not a stream event."""
    result = ""
    last = ""
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            last = line
            continue
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("result"), str):
            result = event["result"]
    return _last_line(result) or last


def launch_failure_excerpt(argv: list[str], out: str, err: str) -> str:
    if "stream-json" in argv:
        cause = _stream_cause(out) or _last_line(err)
    else:
        cause = _last_line(out) or _last_line(err)
    return _clean_line(cause)


def run_launch(argv: list[str], env: dict, cwd: Path, timeout: float,
               prompt: str = PROMPT) -> "tuple[str, str, int | None]":
    """Return ('ok', stdout, 0) or ('unavailable', reason, exit-code-or-None)."""
    try:
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
    except OSError:
        return "unavailable", "launch-error", None
    try:
        out, err = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return "unavailable", "timeout", None
    if proc.returncode != 0:
        excerpt = launch_failure_excerpt(argv, out or "", err or "")
        return ("unavailable",
                f"launch-exit={proc.returncode}" + (f" {excerpt}" if excerpt else ""),
                proc.returncode)
    return "ok", out, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--expect", action="append", default=[], metavar="TOKEN")
    parser.add_argument("--probe", choices=("marker", "hook-gate-deny"), default="marker")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--stream-out", type=Path, default=None,
                        help="save the hook probe's raw stream-json here")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_PASS
    root = args.root.resolve()
    if args.probe == "marker" and not args.expect:
        print("--expect is required for the marker probe", file=sys.stderr)
        return EXIT_USAGE
    if not (root / "home").is_dir() or not args.cwd.is_dir():
        print(f"no sandbox home under {root} or no such cwd {args.cwd}", file=sys.stderr)
        return EXIT_USAGE

    env, status = build_child_env(dict(os.environ), root)
    if status not in host_llm.AUTHENTICATED_TOKEN_STATUSES:
        print(f"credential status: {status}", file=sys.stderr)
        print("UNAVAILABLE auth")
        return EXIT_UNAVAILABLE

    if args.probe == "hook-gate-deny":
        # A recursion depth >= 1 is the gate's executor bypass, which would hide the deny.
        env.pop("AGENT_RECURSION_DEPTH", None)
        kind, detail = run_hook_probe(args.model, env, args.cwd, args.timeout,
                                     args.stream_out)
        if kind == "unavailable":
            print(f"UNAVAILABLE {detail}")
            return EXIT_UNAVAILABLE
        print(f"{kind} {detail}")
        return EXIT_PASS if kind == "pass" else EXIT_MISSING

    # The record file is inside the throwaway sandbox root; the hook command (and
    # nothing in the child's environment) is the only thing that names it.
    # A record an earlier launch left behind must not decide this one.
    record_file = root / LOAD_RECORD_NAME
    record_file.unlink(missing_ok=True)
    register_load_hook(root, record_file)
    argv = build_argv(args.model)
    kind, payload, exit_code = run_launch(argv, env, args.cwd, args.timeout)
    session_completed, result = parse_session(payload) if kind == "ok" else (False, None)
    record_paths = []
    if record_file.exists():
        record_paths = parse_load_record(
            record_file.read_text(encoding="utf-8", errors="replace"))
    outcome = marker_verdict(record_paths, tuple(args.expect), session_completed, result,
                             payload if kind == "unavailable" else "", _read_text_or_none)
    (root / RUN_RECORD_NAME).write_text(
        json.dumps({
            "verdict": outcome.verdict,
            "reason_code": outcome.reason_code,
            "argv": argv,
            "exit": exit_code,
            "session_completed": session_completed,
            "load_record": outcome.load_record,
            "reply_match": outcome.reply_match,
            "reply_excerpt": outcome.reply_excerpt,
        }, indent=2) + "\n", encoding="utf-8")

    # A launch that never completed has no reply to classify; the one
    # UNAVAILABLE line below is the whole output for it.
    if session_completed:
        for token in args.expect:
            print(f"reply {token} {outcome.reply_match[token]}")
    if outcome.verdict == "PASS":
        print("verdict PASS")
        return EXIT_PASS
    if outcome.verdict == "FAIL":
        extra = f" missing={','.join(outcome.missing)}" if outcome.missing else ""
        print(f"reason {outcome.reason_code}{extra}")
        print(f"load-record {' '.join(outcome.load_record)}".rstrip())
        print(f"reply_excerpt {outcome.reply_excerpt}")
        return EXIT_MISSING
    print(f"UNAVAILABLE {payload if kind == 'unavailable' else 'unknown'}")
    return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
