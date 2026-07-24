"""Shared planner-return validation for the two spawn wrappers.

Difficulty removed: spawn-specialist.py and spawn-cursor-specialist.py each carried
their own copy of the return-marker + planner-plan validation, and the copies drifted.
One had relaxed ``validate_marker`` to the any-line contract (the 2026-06-24 fix: a
specialist writing a summary before its marker is normal); the other still carried the
stale first-non-empty-line form. Worse, BOTH callers extracted the marker from line 0
only (``forwarded.splitlines()[0]``) while ``validate_marker`` accepts a marker on any
line — so a planner writing any preamble before its ``PLAN-READY:`` marker skipped the
plan check entirely (fail-open) AND logged the wrong ``return_marker`` telemetry. This
module is the single home both wrappers import, so the contract cannot drift and the
plan check cannot be bypassed.

The planner's deliverable is the TOML plan the engine tracks. ``validate_planner_plan``
loads the declared plan through ``agentctl.plan.load_plan`` — the same validator
``cmd_submit_plan`` trusts — and requires a ``.toml`` path with
``weight_class = "substantive"``. Plans are TOML-only; there is no markdown plan class.
Engine-import failure fails CLOSED — the plan check must never be
silently skipped because an import broke.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.marker_extract import Extraction

RETURN_MARKERS = (
    "COMPLETED",
    "PLAN-READY",
    "INCOMPLETE",
    "CLARIFY",
    "REPLAN",
    "PERMISSION-REQUEST",
    "ESCALATE",
    "REVIEW",
)
MARKER_RE = re.compile(rf"^({'|'.join(RETURN_MARKERS)}):")
PLAN_PATH_RE = re.compile(r"^\s*Plan\s*:\s*(.+?)\s*$", re.MULTILINE)


def extract_marker(result_text: str) -> str | None:
    """The label of the specialist's message: the marker word on the FIRST line that
    carries a known ``^MARKER:`` — matching ``validate_marker``'s any-line contract, so a
    specialist writing a summary before the marker is read correctly (both for plan
    dispatch and for telemetry). ``None`` if no line carries a known marker."""
    for line in result_text.splitlines():
        m = MARKER_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def validate_marker(result_text: str) -> tuple[str, bool]:
    """Return ``(text, ok)``. A return marker is the label of the message; accept it on
    ANY line (the ``^MARKER:`` anchor keeps prose from matching by accident), not only the
    first non-empty one — specialists routinely write a short summary before the marker,
    and rejecting that as MALFORMED false-BLOCKs an otherwise-passing stage. If no line
    carries a known marker, prepend ``MALFORMED:`` and ``ok=False``."""
    if extract_marker(result_text) is not None:
        return result_text, True
    return (
        "MALFORMED: specialist output contained no known return marker line.\n\n"
        + result_text,
        False,
    )


def validate_planner_plan(plan_path: str | None, result_text: str) -> tuple[str, bool]:
    """For planner ``PLAN-READY:`` outputs, validate the declared plan through the
    engine's own TOML validator (``agentctl.plan.load_plan``).

    ``plan_path`` is the path the second-pass extraction read out of the planner's
    output; ``None`` means no extraction supplied one, and the path is recovered from
    the ``Plan: <path>`` line by regex — a purely STRUCTURAL match (a labelled path,
    not free-text meaning), so the regex is in its proper place here.

    The planner's deliverable is the TOML plan the engine tracks, so the path must end in
    ``.toml``, the plan must load, and it must declare ``weight_class = "substantive"``.
    There is NO markdown branch. Engine-import failure fails CLOSED (``ok=False``): the
    plan check must never be silently skipped because an import broke. A path the
    extraction invented rather than read fails closed here too — it is validated by
    construction, not trusted."""
    from lib import config_root  # local import: keep this module importable stand-alone

    plans_hint = config_root.plans_dir()

    if plan_path is None:
        m = PLAN_PATH_RE.search(result_text)
        plan_path = m.group(1).strip().strip("`'\"") if m else None
    if not plan_path:
        return (
            "MALFORMED: planner PLAN-READY: output is missing a "
            "`Plan: <absolute-path>` line. The planner's deliverable is the TOML plan the "
            f"engine tracks: write it to `{plans_hint}/<slug>.toml` and declare the path on "
            "its own line right after PLAN-READY:.\n\n" + result_text,
            False,
        )

    if not plan_path.endswith(".toml"):
        return (
            f"MALFORMED: planner PLAN-READY: declared plan at `{plan_path}` is not a "
            ".toml file. The planner's deliverable is the TOML plan the engine tracks "
            f"(the real plans directory is `{plans_hint}`); plans are TOML-only.\n\n" + result_text,
            False,
        )

    try:
        from agentctl.plan import PlanError, load_plan
    except Exception as exc:  # fail CLOSED — never skip the check on a broken engine import
        return (
            "MALFORMED: planner PLAN-READY: could not load the engine plan validator "
            f"(agentctl.plan) to check `{plan_path}`: {exc}. Refusing to pass the plan "
            "unchecked.\n\n" + result_text,
            False,
        )

    try:
        doc = load_plan(plan_path)
    except PlanError as exc:
        return (
            f"MALFORMED: planner PLAN-READY: declared plan at `{plan_path}` failed engine "
            f"validation (agentctl.plan.load_plan): {exc}\n\n" + result_text,
            False,
        )

    weight = (doc.meta.weight_class or "").lower()
    if weight != "substantive":
        return (
            f"MALFORMED: planner PLAN-READY: declared plan at `{plan_path}` has "
            f"weight_class={doc.meta.weight_class!r}, but a planner deliverable that reaches "
            "the plan-approval gate must be substantive (set "
            '[meta] weight_class = "substantive").\n\n' + result_text,
            False,
        )
    return result_text, True


_DIGEST_MAX = 300


def canonicalize(
    marker: str, digest: str, plan_path: str | None, original: str
) -> str:
    """Assemble the canonical envelope a passing second-pass extraction produces::

        <MARKER>:                    # BARE — nothing after the colon
        Digest: <one-line digest>    # only when the digest is non-empty
        Plan: <absolute path>        # planner PLAN-READY only
        <blank line>
        <the specialist's full original output, byte-for-byte>

    This is the ONE site that turns a perception verdict into text. Every
    downstream router (``agentctl.dispatch.parse_marker``) then does a trivial
    deterministic parse of what this function produced, instead of re-scanning
    raw prose for a line-start marker word — so site B reads what site A emitted
    and the two cannot drift.

    The marker line is BARE by design, and that is load-bearing rather than
    cosmetic: ``parse_marker`` returns the text AFTER the marker's colon as the
    router body, and ``agentctl.cli.cmd_dispatch`` feeds that body to
    deterministic consumers — most sharply ``permissions.check_permission`` for
    a PERMISSION-REQUEST, which SUBSTRING-matches it against the user's granted
    patterns. Model-authored text on that line would therefore be untrusted
    input to a security gate: a digest that happened to contain a granted
    pattern would auto-skip the user's permission ask (fail-OPEN). With the
    marker line bare, the router body is ``""`` for every canonicalised marker,
    so the permission path defaults to always-ask — the exact legacy polarity.

    The digest instead lands on its own ``Digest:`` line, where no rule-half
    consumer parses it and a human or a later grep can still read it. It is
    sanitised even there: whitespace collapsed, newlines dropped (a multi-line
    digest would otherwise forge envelope structure) and truncated, so it cannot
    displace the original output that follows. ``plan_path`` is model-authored
    too and gets the same newline treatment — a value that still spans lines is
    dropped from the envelope rather than allowed to forge one. (A fabricated
    path stays caught downstream by ``validate_planner_plan``'s .toml + load_plan
    + weight_class checks; this clause only prevents an injected LINE.)"""
    lines = [f"{marker}:"]
    body = " ".join((digest or "").split())[:_DIGEST_MAX]
    if body:
        lines.append(f"Digest: {body}")
    path = (plan_path or "").strip()
    if path and not any(ch in path for ch in "\r\n"):
        lines.append(f"Plan: {path}")
    return "\n".join(lines) + f"\n\n{original}"


def check_planner_return(
    result_text: str, kind: str, *, extraction: Extraction | None = None
) -> tuple[str, bool, str | None]:
    """The wrappers' single entry point: validate the return marker, then — for a planner
    ``PLAN-READY:`` — validate the declared TOML plan. Returns
    ``(forwarded_text, ok, marker)`` where ``marker`` is the extracted return-marker label
    (or ``None`` when malformed), so the caller logs the RIGHT marker for telemetry
    regardless of where in the output the marker appeared.

    ``extraction`` is the optional second-pass verdict (``lib.marker_extract.Extraction``).
    When ``None`` (the default), behaviour is BYTE-IDENTICAL to the legacy path — the
    kill switch and every pre-existing caller that has not been wired to the extractor.
    When provided and not degraded, the extraction is AUTHORITATIVE: its marker (even one
    the legacy any-line regex scan would miss, e.g. under markdown emphasis) is accepted
    and canonicalised, and a ``marker=None`` verdict is MALFORMED rather than an excuse to
    re-ask the legacy scan — falling back there would resurrect exactly the classifier this
    pass replaces. Only ``degraded`` (``claude`` absent — an observable condition, never a
    judgement) falls back to the legacy scan unchanged."""
    if extraction is not None and not extraction.degraded:
        if extraction.marker is None:
            return (
                "MALFORMED: specialist output contained no known return marker "
                f"(second-pass extraction: {extraction.reason or 'no marker found'}). "
                "Set AGENTCTL_MARKER_EXTRACTOR=0 to fall back to the legacy "
                "line-start marker scan.\n\n" + result_text,
                False,
                None,
            )
        marker = extraction.marker
        plan_path = extraction.plan_path
        forwarded = canonicalize(marker, extraction.digest, plan_path, result_text)
    else:
        forwarded, ok = validate_marker(result_text)
        if not ok:
            return forwarded, False, None
        marker = extract_marker(forwarded)
        plan_path = None

    if kind == "planner" and marker == "PLAN-READY":
        forwarded, ok = validate_planner_plan(plan_path, forwarded)
        if not ok:
            return forwarded, False, marker
    return forwarded, True, marker
