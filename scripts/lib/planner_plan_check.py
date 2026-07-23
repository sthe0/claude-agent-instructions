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
# The `Plan:` label tolerates the same emphasis run as the marker line: a specialist
# that bolds its PLAN-READY: marker bolds its `**Plan:**` label too, and a marker-only
# fix would leave the planner path false-BLOCKed. Two emphasis runs are absorbed — the
# leading one (`**Plan`) and the label's closing one right after the colon (`Plan:** `),
# so `**Plan:** **/x.toml**` captures `/x.toml` after the path cleanup below strips the
# path's own wrap. Kept as a widened regex rather than a per-line scan so the module
# keeps its single MULTILINE search over the whole output.
PLAN_PATH_RE = re.compile(r"^\s*[*_`]*\s*Plan\s*:\s*[*_`]*\s*(.+?)\s*$", re.MULTILINE)

_EMPHASIS_CHARS = "*_`"


def strip_marker_emphasis(line: str) -> tuple[str, bool]:
    """Drop a leading run of markdown emphasis / code-span characters (``*``, ``_``,
    `` ` ``) so a correctly-named return marker wrapped in emphasis — ``**COMPLETED:**``,
    `` `COMPLETED:` ``, ``__COMPLETED:__`` — is matched exactly like the bare form.

    Difficulty removed: a specialist whose work SUCCEEDED but who rendered its marker as
    markdown was rejected as MALFORMED on formatting alone, parking the stage and forcing
    a manual recovery. The ``^MARKER:`` anchor still applies — only after this leading run
    is removed — so a marker word mid-prose (``*note* ESCALATE: x`` -> ``note* ESCALATE:
    x``) still does not match. Returns ``(stripped_line, was_wrapped)``; ``was_wrapped`` is
    True iff a leading emphasis run was removed."""
    stripped = line.lstrip(_EMPHASIS_CHARS)
    return stripped, stripped != line


def trim_wrapped_marker_body(body: str) -> str:
    """Trim a trailing emphasis / code-span run from a marker BODY whose marker line was
    wrapped, so ``**COMPLETED:** done`` yields ``done`` rather than ``** done``. Gate the
    call on ``was_wrapped``: an UNWRAPPED body is left byte-identical, so a marker whose
    body legitimately ends in ``*``/``_`` is untouched on the path that works today."""
    return body.strip().strip(_EMPHASIS_CHARS).strip()


def extract_marker(result_text: str) -> str | None:
    """The label of the specialist's message: the marker word on the FIRST line that
    carries a known ``^MARKER:`` — matching ``validate_marker``'s any-line contract, so a
    specialist writing a summary before the marker is read correctly (both for plan
    dispatch and for telemetry). A leading markdown-emphasis run is stripped first so a
    wrapped marker reads like a bare one. ``None`` if no line carries a known marker."""
    for line in result_text.splitlines():
        stripped, _ = strip_marker_emphasis(line.strip())
        m = MARKER_RE.match(stripped)
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


def validate_planner_plan(result_text: str) -> tuple[str, bool]:
    """For planner ``PLAN-READY:`` outputs, extract the ``Plan: <path>`` line and validate
    the declared plan through the engine's own TOML validator (``agentctl.plan.load_plan``).

    The planner's deliverable is the TOML plan the engine tracks, so the path must end in
    ``.toml``, the plan must load, and it must declare ``weight_class = "substantive"``.
    There is NO markdown branch. Engine-import failure fails CLOSED (``ok=False``): the
    plan check must never be silently skipped because an import broke."""
    from lib import config_root  # local import: keep this module importable stand-alone

    plans_hint = config_root.plans_dir()

    m = PLAN_PATH_RE.search(result_text)
    if not m:
        return (
            "MALFORMED: planner PLAN-READY: output is missing a "
            "`Plan: <absolute-path>` line. The planner's deliverable is the TOML plan the "
            f"engine tracks: write it to `{plans_hint}/<slug>.toml` and declare the path on "
            "its own line right after PLAN-READY:.\n\n" + result_text,
            False,
        )
    plan_path = m.group(1).strip().strip("`'\"*_").strip()

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


def check_planner_return(result_text: str, kind: str) -> tuple[str, bool, str | None]:
    """The wrappers' single entry point: validate the return marker, then — for a planner
    ``PLAN-READY:`` — validate the declared TOML plan. Returns
    ``(forwarded_text, ok, marker)`` where ``marker`` is the extracted return-marker label
    (or ``None`` when malformed), so the caller logs the RIGHT marker for telemetry
    regardless of where in the output the marker appeared."""
    forwarded, ok = validate_marker(result_text)
    if not ok:
        return forwarded, False, None
    marker = extract_marker(forwarded)
    if kind == "planner" and marker == "PLAN-READY":
        forwarded, ok = validate_planner_plan(forwarded)
    return forwarded, ok, marker
