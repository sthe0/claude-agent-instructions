"""Language-agnostic, model-backed semantic judge for the Stop-hook perception layer.

Mirrors scripts/agentctl/advisor.py: a cheap `claude -p` call with a HARD subprocess
timeout, fail-open to a NEUTRAL verdict (never a false positive/negative), plus a
config-mode + per-kind kill-switch + env-override gating layer. It replaces the
per-language natural-language cue regexes that formerly supplied the three end-of-turn
perception booleans (`binary_ask` / `si_feedback` / `outage_escalation`) with ONE judge
that classifies MEANING in any language, so a Russian prose binary-confirm question is
caught the same as an English one.

Fail-open contract: judge(kind, text) returns None (NOT False) on a disabled kind, a
None runner, a non-zero exit, empty/unparseable output, or ANY exception. None means
"no signal — keep the caller's deterministic default"; the caller never blocks on None.
So control flow with judge()==None is byte-identical to judge-absent.

Recursion safety: the live runner launches `claude -p` with AGENT_RECURSION_DEPTH>=1 in
the child env, so every depth-gated hook (including hook-turn-end-gate.py's own
early-return at AGENT_RECURSION_DEPTH>=1) short-circuits — no hook->claude->hook
recursion is possible.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl.config import Thresholds  # noqa: E402
from agentctl.dispatch import RunResult  # noqa: E402

KINDS = ("binary_ask", "si_feedback", "outage_escalation")

_ENV_OVERRIDE = "SEMANTIC_JUDGE"

# Fallbacks for a missing config key. The live values live in config.md and are
# referenced by key; these only keep the judge robust if a key is absent. The timeout
# is shorter than the advisor's 20 s because this sits on the hot Stop path.
_DEFAULT_MODEL = "sonnet"
_DEFAULT_TIMEOUT_S = 8
_DEFAULT_KINDS = "binary_ask,outage_escalation,si_feedback"
_DEFAULT_SI_MAXLEN = 400

# Every prompt classifies MEANING in any language and ends with a strict YES/NO +
# one-line-reason protocol, mirroring advisor.acceptance_judge so the caller has a
# machine-decidable verdict.
_PROMPTS: dict[str, str] = {
    "binary_ask": (
        "You are classifying an AI assistant's final message to a user. "
        "Does this assistant text END by asking the user to CONFIRM a binary action "
        "(yes/no, go/stop, apply/skip, proceed/cancel) or to PICK from a defined set of "
        "named options — as opposed to an open-ended free-text question? "
        "Judge the MEANING; the message may be in any language.\n\n"
        "Assistant text:\n{payload}"
    ),
    "si_feedback": (
        "You are classifying a user's message to an AI coding assistant. "
        "Is this message agent-behavior FEEDBACK — a correction, rejection, or a stated "
        "rule about HOW the assistant should act (for example 'don't do that', 'always "
        "ask first', 'you got the scope wrong', 'answer in my language') — as opposed to "
        "a neutral task instruction or a plain confirmation? "
        "Judge the MEANING; the message may be in any language.\n\n"
        "User message:\n{payload}"
    ),
    "outage_escalation": (
        "You are classifying an AI assistant's final message to a user. "
        "Does this text surface an EXTERNAL-SERVICE FAILURE (an outage, timeout, or error "
        "from a service the assistant called) to the user and ask how to proceed, WITHOUT "
        "first showing a diagnosis of that failure? "
        "Judge the MEANING; the message may be in any language.\n\n"
        "Assistant text:\n{payload}"
    ),
}

_PROTOCOL = (
    "\n\nAnswer on the FIRST line with exactly YES or NO. On the SECOND line give a "
    "one-line reason."
)


def _thr(thresholds: Thresholds | None) -> Thresholds:
    return thresholds if thresholds is not None else Thresholds()


def _enabled_kinds(thr: Thresholds) -> set[str]:
    try:
        raw = thr.semantic_judge_kinds
    except KeyError:
        raw = _DEFAULT_KINDS
    return {k.strip() for k in raw.split(",") if k.strip()}


def _model(thr: Thresholds) -> str:
    try:
        return thr.semantic_judge_model
    except KeyError:
        return _DEFAULT_MODEL


def _timeout_s(thr: Thresholds) -> int:
    try:
        return thr.semantic_judge_timeout_s
    except KeyError:
        return _DEFAULT_TIMEOUT_S


def si_maxlen(thresholds: Thresholds | None = None) -> int:
    """Char-length gate for the si_feedback precondition (read by hook-turn-end-gate.py)."""
    try:
        return _thr(thresholds).semantic_judge_si_maxlen
    except KeyError:
        return _DEFAULT_SI_MAXLEN


def resolve_enabled(kind: str, *, thresholds: Thresholds | None = None) -> bool:
    """Resolve whether the judge should run for `kind`.

    SEMANTIC_JUDGE overrides both ways ("1" forces on, "0" forces off, regardless of
    config or kind). Absent the override, the judge is on iff config semantic-judge-mode
    == "on" AND `kind` is in semantic-judge-kinds (the per-kind kill-switch). A missing
    mode key resolves to off (fail-open default-off, mirroring advisor.resolve_enabled);
    a missing kinds key resolves to all-kinds-enabled (the documented defaults).
    """
    env = os.environ.get(_ENV_OVERRIDE)
    if env == "1":
        return True
    if env == "0":
        return False
    thr = _thr(thresholds)
    try:
        mode = thr.semantic_judge_mode
    except KeyError:
        return False
    if mode != "on":
        return False
    return kind in _enabled_kinds(thr)


def judge(
    kind: str,
    text: str,
    *,
    runner=None,
    thresholds: Thresholds | None = None,
    enabled: bool | None = None,
) -> bool | None:
    """Classify the MEANING of `text` for `kind`; return True/False, or None (fail-open).

    `kind` is one of KINDS. None means "no signal": an unknown kind, a disabled kind, a
    None runner, a non-zero exit, empty or unparseable output, or ANY exception. The
    caller keeps its deterministic default on None, so control flow is byte-identical to
    judge-absent. A disabled kind returns None WITHOUT spawning a subprocess.
    """
    if kind not in _PROMPTS:
        return None
    if enabled is None:
        enabled = resolve_enabled(kind, thresholds=thresholds)
    if not enabled or runner is None:
        return None
    try:
        model = _model(_thr(thresholds))
        prompt = _PROMPTS[kind].format(payload=text) + _PROTOCOL
        result = runner(["claude", "-p", "--model", model, prompt])
        if result.returncode != 0:
            return None
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return None
        head = lines[0].upper()
        if head.startswith("YES"):
            return True
        if head.startswith("NO"):
            return False
        return None
    except Exception:
        return None


def subprocess_runner(argv: list[str], *, timeout: int | None = None) -> RunResult:
    """Live `claude -p` runner with a hard timeout and a recursion-guarded child env.

    The child env carries AGENT_RECURSION_DEPTH>=1 (incremented off the current value)
    so every depth-gated hook in the nested `claude -p` short-circuits — no
    hook->claude->hook recursion. The env is merged onto os.environ, never cleared. On
    timeout it returns a non-zero RunResult (judge() maps that to None: do-not-fire), so
    a judge outage can never wedge the Stop gate.
    """
    if timeout is None:
        timeout = _timeout_s(Thresholds())
    env = dict(os.environ)
    try:
        depth = int(env.get("AGENT_RECURSION_DEPTH", "0"))
    except ValueError:
        depth = 0
    env["AGENT_RECURSION_DEPTH"] = str(max(depth + 1, 1))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return RunResult(1, "", f"semantic judge timed out after {timeout}s")
