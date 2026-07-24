"""Second-pass marker extraction: perception (a bounded model call) feeding a
deterministic, fail-closed validator.

Difficulty removed: recovering a specialist's terminal return marker by regex-
matching a literal English word at line start breaks under markdown emphasis
(``**COMPLETED:**``) and risks semantic collision with a marker word quoted or
discussed in prose (see CLAUDE.md's "separate rule from perception" principle
and ``memory-global/leaves/regex-not-for-semantic-classification.md``). This
module performs the ONE perception call — a closed-set classification of which
marker, if any, actually LABELS a specialist's message — so every downstream
consumer (``lib.planner_plan_check``, ``agentctl.dispatch``) can stay a trivial
deterministic parse of the canonical envelope this pass produces, instead of
re-guessing marker meaning from raw prose.

Fail-CLOSED, not fail-open: this extraction feeds a ROUTING decision that could
silently advance unverified work, so any ambiguity or extractor error yields
``Extraction(marker=None, ...)``, never a fabricated marker — the opposite
polarity from ``agentctl.advisor``'s fail-open judges, which feed BLOCKING
guardians where a false positive would wedge the user instead of silently
mis-routing one.

Role hint, not role restriction: a caller's ``kind`` only shapes the prompt
(sharpens the closed-set question); marker validation always checks the FULL
shared vocabulary (``RETURN_MARKERS``), never a role-narrowed subset — a role's
own SKILL.md is a demonstrably incomplete source of its own marker set (e.g.
``thinker/SKILL.md`` omits REVIEW from its explicit "Applicable markers" line;
``planner/SKILL.md``'s "Other applicable markers" line omits PLAN-READY, which
is documented two lines above as the role's preferred terminal marker instead).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from lib.planner_plan_check import RETURN_MARKERS

_EXTRACT_MODEL = "haiku"
# 30s, not advisor.py's 20s: that ceiling is sized for a short question, while
# this pass reads up to _WINDOW_MAX chars of specialist output.
_EXTRACT_TIMEOUT_S = 30

# Whole output below this; above it, head + elision + tail (see build_prompt).
_WINDOW_MAX = 12000
_WINDOW_HALF = 6000

ENV_KILL_SWITCH = "AGENTCTL_MARKER_EXTRACTOR"
ENV_MODEL = "AGENTCTL_MARKER_MODEL"

# Union, per role, of that role's own SKILL.md "Applicable markers" enumeration
# and skills/specializations/_shared/marker-protocol.md's role-tagged entries
# (PLAN-READY tagged "(planner)"; REVIEW tagged "(thinker)"). This is a HINT
# only — it sharpens the prompt's closed-set question, it never narrows what
# `extract()` accepts as a valid verdict (always the full RETURN_MARKERS).
HINTS_BY_KIND: dict[str, tuple[str, ...]] = {
    "planner": (
        "PLAN-READY", "COMPLETED", "INCOMPLETE", "CLARIFY", "REPLAN",
        "PERMISSION-REQUEST", "ESCALATE",
    ),
    "developer": (
        "COMPLETED", "INCOMPLETE", "CLARIFY", "REPLAN",
        "PERMISSION-REQUEST", "ESCALATE",
    ),
    "code-reviewer": ("COMPLETED", "INCOMPLETE", "CLARIFY", "ESCALATE"),
    "thinker": (
        "COMPLETED", "INCOMPLETE", "CLARIFY", "REPLAN",
        "PERMISSION-REQUEST", "ESCALATE", "REVIEW",
    ),
    "tech-writer": ("COMPLETED", "INCOMPLETE", "CLARIFY", "ESCALATE"),
    "yandex-cloud-expert": (
        "COMPLETED", "INCOMPLETE", "REPLAN", "PERMISSION-REQUEST", "ESCALATE",
    ),
}


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class Extraction:
    """The extraction pass's verdict.

    ``marker`` is a member of the caller's ``allowed`` vocabulary, or ``None``
    when the pass found no terminal marker or could not complete. ``digest`` is
    the model's one-line human-facing summary and ``plan_path`` the path a
    planner declared — both are proposals the deterministic half disposes of
    (``plan_path`` is validated by construction downstream, so a fabricated one
    fails closed).

    ``degraded`` marks the one case the caller must treat as "the pass did not
    run": the ``claude`` binary is absent. That condition is OBSERVABLE and
    binary, so it can never mask an inconclusive classification — every
    judgement-bearing failure (NONE, off-vocabulary, empty, unparseable,
    non-zero exit, timeout, exception) instead yields ``marker=None`` with
    ``degraded=False``, i.e. MALFORMED rather than a silent fallback.
    """

    marker: str | None
    digest: str = ""
    plan_path: str | None = None
    reason: str = ""
    degraded: bool = False


Runner = Callable[..., RunResult]


def subprocess_runner(argv: list[str], *, timeout: int = _EXTRACT_TIMEOUT_S) -> RunResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return RunResult(1, "", f"marker extractor timed out after {timeout}s")


def enabled() -> bool:
    """The kill switch: ``AGENTCTL_MARKER_EXTRACTOR=0`` restores byte-identical
    legacy behaviour — the extraction pass never runs, every caller falls back
    to its pre-existing any-line regex scan."""
    return os.environ.get(ENV_KILL_SWITCH, "1") != "0"


def extractor_available() -> bool:
    """Portability guard: site C (``spawn-cursor-escape.py``) may run under a
    Cursor ``agent`` CLI environment where ``claude`` is not on PATH."""
    return shutil.which("claude") is not None


def model() -> str:
    return os.environ.get(ENV_MODEL) or _EXTRACT_MODEL


def hint_markers_for(kind: str | None) -> tuple[str, ...]:
    """The markers ``kind`` TYPICALLY returns — a prompt hint only. An unlisted
    kind yields the full vocabulary, so an incomplete table degrades to a flat
    closed-set question rather than to a rejection."""
    return HINTS_BY_KIND.get(kind or "", RETURN_MARKERS)


def _window(result_text: str) -> str:
    """Bound the prompt. Head AND tail, never tail-only: the protocol tolerates
    the marker at the TOP with a summary after it, so a tail-only window would
    systematically miss the commonest correct shape."""
    if len(result_text) <= _WINDOW_MAX:
        return result_text
    elided = len(result_text) - 2 * _WINDOW_HALF
    return (
        result_text[:_WINDOW_HALF]
        + f"\n\n[... {elided} characters elided ...]\n\n"
        + result_text[-_WINDOW_HALF:]
    )


def build_prompt(result_text: str, allowed: tuple[str, ...], hint: tuple[str, ...] = ()) -> str:
    hint_line = (
        f"These are the usual choices for this role: {', '.join(hint)}. But ANY "
        "token in the list above is a valid answer if that is what the output "
        "signals — this is a hint, not a restriction.\n\n"
    ) if hint else ""
    return (
        "You are extracting a single terminal control marker from a specialist's "
        "final output. The marker is the LABEL of the message — its declared "
        "disposition — not any occurrence of a marker word inside prose, a quote, "
        "or a discussion of markers in general. Markdown emphasis around the "
        "marker (e.g. `**COMPLETED:**`) does not change its identity.\n\n"
        f"Allowed markers: {', '.join(allowed)}\n\n"
        f"{hint_line}"
        "Answer NONE if the output signals nothing terminal, signals two markers "
        "with no clear terminal one, or is truncated mid-thought. Do not guess a "
        "plausible marker.\n\n"
        "Reply with EXACTLY these three lines and nothing else:\n"
        "MARKER: <one allowed token, or NONE>\n"
        "DIGEST: <one line summarising the outcome, for a human reader>\n"
        "PLAN: <the absolute plan path the output declares, or NONE>\n\n"
        "--- specialist output ---\n"
        f"{_window(result_text)}\n"
        "--- end specialist output ---\n"
    )


def _labelled(lines: list[str], label: str) -> str | None:
    prefix = label + ":"
    for line in lines:
        bare = line.strip().strip("`*# ").strip()
        if bare.upper().startswith(prefix):
            return bare[len(prefix):].strip().strip("`*'\"").strip()
    return None


def extract(
    result_text: str,
    *,
    allowed: tuple[str, ...] = RETURN_MARKERS,
    hint: tuple[str, ...] = (),
    runner: Runner | None = None,
    timeout: int = _EXTRACT_TIMEOUT_S,
) -> Extraction:
    """Run the perception pass over ``result_text`` and return its verdict.

    Fail CLOSED. A process error, timeout, exception, empty output, unparseable
    reply, NONE verdict, or a token outside ``allowed`` all return
    ``marker=None`` — never a guessed marker, and never ``degraded`` (the caller
    degrades only on the observable claude-absent condition, so no judgement
    failure can be mistaken for "the pass did not run"). Never raises."""
    run = runner or subprocess_runner
    prompt = build_prompt(result_text, allowed, hint)
    try:
        res = run(["claude", "-p", "--model", model(), prompt], timeout=timeout)
    except Exception as exc:  # a broken runner must never crash a finished spawn
        return Extraction(None, reason=f"extractor raised: {type(exc).__name__}: {exc}"[:200])

    if res.returncode != 0:
        stderr = (res.stderr or "").strip()[:200]
        return Extraction(None, reason=f"extractor process failed (exit {res.returncode}): {stderr}")

    lines = [line for line in (res.stdout or "").splitlines() if line.strip()]
    if not lines:
        return Extraction(None, reason="extractor returned no output")

    verdict = _labelled(lines, "MARKER")
    if verdict is None:
        return Extraction(None, reason="extractor reply carried no MARKER: line")
    verdict = verdict.upper()
    if verdict == "NONE":
        return Extraction(None, reason="extractor found no marker")
    if verdict not in allowed:
        return Extraction(None, reason=f"extractor returned an unrecognised token: {verdict!r}")

    plan_path = _labelled(lines, "PLAN")
    if plan_path and plan_path.upper() == "NONE":
        plan_path = None
    return Extraction(
        verdict,
        digest=_labelled(lines, "DIGEST") or "",
        plan_path=plan_path or None,
        reason="ok",
    )


def build_extraction(
    result_text: str,
    *,
    kind: str | None = None,
    allowed: tuple[str, ...] = RETURN_MARKERS,
    runner: Runner | None = None,
) -> Extraction | None:
    """The shared call-site guard behind each wrapper's ``_build_extraction``.

    The pass runs UNCONDITIONALLY whenever it can: the model is the PRIMARY
    marker classifier, not a rescue invoked only after the legacy any-line
    regex scan has already failed. A rescue-only wiring would keep that
    unreliable scan primary and could never catch it picking the WRONG marker
    out of a prose body, because the scan reports no failure in that case.

    ``None`` means the kill switch is off, so the caller takes the legacy
    word-scan path BYTE-IDENTICALLY. A ``degraded`` verdict means the pass
    could not run because ``claude`` is off PATH — same legacy fallback, but
    recorded in telemetry so an unexpectedly-absent extractor is visible."""
    if not enabled():
        return None
    if not extractor_available():
        return Extraction(
            None, reason="claude not on PATH; extractor unavailable", degraded=True
        )
    return extract(
        result_text,
        allowed=allowed,
        hint=hint_markers_for(kind) if kind else (),
        runner=runner or subprocess_runner,
    )


# The three shapes the legacy line-start scan gets wrong: emphasis defeats the
# anchor, a preamble pushes the marker off line 1, and a marker word quoted in
# prose collides semantically with the real one.
_SMOKE_CASES: tuple[tuple[str, str, str | None], ...] = (
    ("emphasis", "**COMPLETED:** shipped the fix, tests green.\n", "COMPLETED"),
    (
        "summary_first",
        "I rewrote the parser and added four tests; the suite is green.\n\n"
        "COMPLETED: parser rewritten, 4 tests added, suite green.\n",
        "COMPLETED",
    ),
    (
        "quoted_marker",
        "I considered whether the stage criterion was wrong and I should return "
        "REPLAN: with a proposal, but the criterion holds — the failure was mine.\n\n"
        "**COMPLETED:** criterion held, fix landed.\n",
        "COMPLETED",
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Second-pass marker extractor (scripts/lib/marker_extract.py)."
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="run 3 fixed cases against the REAL model (a live `claude -p` call "
        "each) and print each verdict; exit non-zero if any verdict is wrong",
    )
    args = parser.parse_args(argv)

    if not args.smoke:
        parser.print_help()
        return 1

    if not enabled():
        print(f"error: extractor disabled via {ENV_KILL_SWITCH}=0", file=sys.stderr)
        return 1
    if not extractor_available():
        print("error: claude not on PATH; cannot run --smoke", file=sys.stderr)
        return 1

    all_ok = True
    for label, text, expected in _SMOKE_CASES:
        result = extract(text, hint=hint_markers_for("developer"))
        ok = result.marker == expected
        all_ok = all_ok and ok
        print(
            f"{label}: marker={result.marker} degraded={result.degraded} "
            f"reason={result.reason!r} expected={expected} "
            f"{'OK' if ok else 'MISMATCH'}"
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
