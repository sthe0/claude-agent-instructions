"""Pricing the effort of understanding an order from the record of its type, and writing the actual back.

The understanding segment opens at classify priced at the configured default. An order's type entry names
a record; when that record carries an estimate (`lib.understanding_effort`), `price` re-prices the segment
per scale from it, and a scale the record does not carry keeps the configured default. A record that
carries no estimate, a malformed block, or no record at all prices at the default and says why: the
order's submission is never refused for it. At resolve `write_back` records the session's whole
understanding actual on the record that priced it, through `record-experience.py understanding-effort`
(so the worktree route and the edit-ledger stamp apply), and makes that actual the record's estimate:
the next order of the type is priced from the latest actual.
With `CLAUDE_DRY_RUN` set it writes nothing, as the repo's other dry-run seams do.

One mapping joins an `Estimate` (`confirmer == ""` while nobody agreed) and the block's estimate row
(no `confirmer` key while nobody agreed): `to_estimate` / `to_row`.

Callers reach every seam as `understanding_pricing.<name>` so a control can replace one at its module attribute.
"""
from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from lib import understanding_effort
from . import effort, effort_segments
from .dispatch import Runner, subprocess_runner
from .sub_plan import Estimate

RECORD_EXPERIENCE = Path(__file__).resolve().parents[1] / "record-experience.py"
DRY_RUN_ENV = "CLAUDE_DRY_RUN"
SOURCE_DEFAULT = effort_segments.SOURCE_CONFIGURED_DEFAULT
SOURCE_RECORD = effort_segments.SOURCE_TYPE_RECORD


@dataclass
class Pricing:
    """The price of understanding one order: the budget per scale, where each scale's figure came from,
    who authored it, the type record that priced it (None when the entry names none) and why a
    default stood where a record's figure might have."""
    budget: dict
    source: str
    author: str
    scale_sources: dict
    type: dict | None
    reason: str
    confirmer: str = ""

    def as_record(self) -> dict:
        return {"type": self.type, "reason": self.reason, "scale_sources": dict(self.scale_sources),
                "confirmer": self.confirmer}


def to_estimate(row: dict) -> Estimate:
    return Estimate(minutes=row.get("minutes"), usd=row.get("usd"), author=row.get("author", ""),
                    confirmer=row.get("confirmer", ""))


def to_row(estimate: Estimate) -> dict:
    fields = (("minutes", estimate.minutes), ("usd", estimate.usd), ("author", estimate.author),
              ("confirmer", estimate.confirmer))
    return {key: value for key, value in fields if value is not None and value != ""}


def estimate_from_actual(actual: dict, session: str) -> Estimate:
    """The estimate an actual stands for when its record holds none: authored by the session that
    recorded the actual, confirmed by nobody."""
    return Estimate(minutes=actual[effort_segments.SCALE_MINUTES] or None,
                    usd=actual[effort_segments.SCALE_USD] or None, author=session, confirmer="")


def _default(default: dict, type_ref: dict | None, reason: str) -> Pricing:
    return Pricing(
        budget=dict(default), source=SOURCE_DEFAULT, author=effort_segments.AUTHOR_CONFIGURED_DEFAULT,
        scale_sources={scale: SOURCE_DEFAULT for scale in default}, type=type_ref, reason=reason)


def price(default: dict, entry: dict | None, path: Path | None) -> Pricing:
    """`entry` is `{kind, ref}` of the order's type entry (None when it names none), `path` the leaf it
    resolves to (None when it resolves to none)."""
    if entry is None or path is None:
        return _default(default, None, "the order's type entry names no record, so the configured default prices it")
    type_ref = {"kind": entry["kind"], "ref": entry["ref"], "path": str(path)}
    try:
        text = path.read_text(encoding="utf-8")
        held = understanding_effort.problems(text)
        row = None if held else understanding_effort.estimate_of(text)
    except (OSError, ValueError) as exc:
        return _default(default, type_ref, f"the type record cannot be read for an estimate ({exc}); "
                                           "the configured default prices it")
    if held:
        return _default(default, type_ref, "the type record's Understanding effort block is malformed ("
                                           + "; ".join(held) + "); the configured default prices it")
    if row is None:
        return _default(default, type_ref, "the type record carries no understanding estimate, so the configured "
                                           "default prices it")
    estimate = to_estimate(row)
    carried = effort.agreed_figures(estimate.minutes, estimate.usd)
    if not carried:
        return _default(default, type_ref, "the type record's estimate carries no positive figure, so the "
                                           "configured default prices it")
    budget = {**default, **{scale: figure for scale, figure in carried.items() if scale in default}}
    return Pricing(
        budget=budget, source=SOURCE_RECORD, author=estimate.author,
        scale_sources={scale: SOURCE_RECORD if scale in carried else SOURCE_DEFAULT for scale in default},
        type=type_ref, reason="priced from the type record's estimate", confirmer=estimate.confirmer)


def _run(runner: Runner, args: list[str], session: str | None) -> tuple[bool, str]:
    argv = [sys.executable, str(RECORD_EXPERIENCE), "understanding-effort", *args]
    if session:
        argv += ["--session", session]
    result = runner(argv)
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {(result.stderr or '').strip()[:300]}"
    return True, (result.stdout or "").strip()


def _figure_args(figures: dict) -> list[str]:
    args = []
    if figures.get(effort_segments.SCALE_MINUTES):
        args += ["--minutes", repr(float(figures[effort_segments.SCALE_MINUTES]))]
    if figures.get(effort_segments.SCALE_USD):
        args += ["--usd", repr(float(figures[effort_segments.SCALE_USD]))]
    return args


def session_actual(armings: list[dict]) -> dict:
    """What the session spent understanding its order: every understanding arming it held, the ones a
    user act renewed included. The carried counts are summed with the booked ones: an understanding
    arming never carries across a link today (`effort_segments.conserve` sums booked only), so the
    carried part is zero here and counted for the day one does."""
    total = {scale: 0.0 for scale in effort_segments.SCALES}
    for arming in armings:
        for scale, amount in effort_segments.arming_actual(arming).items():
            total[scale] += amount
    return total


def _outcome(written: list, skipped: str | None, retry: list[list[str]] | None = None, session: str | None = None,
             *, already_there: bool = False, replaced_confirmed: dict | None = None) -> dict:
    """`retry` holds the argv tails of every `record-experience.py understanding-effort` call still owed;
    they are printed as the exact command that repeats them (the actual call is safe to repeat: a session
    already recorded adds no second row). `replaced_confirmed` names the confirmed estimate the
    write-back replaced, `already_there` that the session's actual row was on the record already."""
    command = None
    if retry:
        tail = ["--session", session] if session else []
        command = " && ".join(shlex.join(["python3", str(RECORD_EXPERIENCE), "understanding-effort", *args, *tail])
                              for args in retry)
    return {"written": written, "skipped": skipped, "retry": command, "already_there": already_there,
            "replaced_confirmed": replaced_confirmed}


def _held_estimate(leaf: str, session: str) -> tuple[dict | None, bool]:
    """The estimate the record holds and whether it holds the session's actual row already; (None, False)
    for a block the verb will refuse anyway."""
    try:
        text = Path(leaf).read_text(encoding="utf-8")
        if understanding_effort.problems(text):
            return None, False
        return understanding_effort.estimate_of(text), understanding_effort.has_actual(text, session)
    except (OSError, ValueError):
        return None, False


def write_back(armings: list[dict], *, session: str, runner: Runner | None = None) -> dict:
    """Record the session's understanding actual on the record that priced the understanding as it stood
    last, and make that actual the record's estimate: the next order of the type is priced from the latest
    actual, and a scale the actual did not measure is left to the configured default. Returns what was
    done: `{"written": [...], "skipped": reason, "retry": the command that repeats what is owed,
    "already_there": ..., "replaced_confirmed": ...}`; a failed write is reported, never raised, since
    the order is already agreed and the record can be written by hand. An arming still open (the order
    never reached agreement) is no final actual: nothing is written for it. A session whose last arming
    names no record writes nothing."""
    if any(arming["closed_at"] is None for arming in armings):
        return _outcome([], "an understanding arming is still open, so no final actual exists to write; "
                            "it is written at the resolve of a session whose order was agreed")
    if os.environ.get(DRY_RUN_ENV):
        return _outcome([], f"{DRY_RUN_ENV} is set: a dry run writes nothing to a type record")
    run = runner or subprocess_runner
    type_ref = (armings[-1].get("pricing") or {}).get("type") if armings else None
    if not type_ref:
        return _outcome([], "the order's type entry names no record to write the actual to")
    actual = session_actual(armings)
    figures = _figure_args(actual)
    if not figures:
        return _outcome([], "nothing was measured for understanding the order")
    leaf = type_ref["path"]
    try:
        Path(leaf).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return _outcome([], f"the type record cannot be read: {exc}")
    held, already_there = _held_estimate(leaf, session)
    actual_args = ["--leaf", leaf, "--kind", "actual", "--actual-session", session, *figures]
    stood_in = estimate_from_actual(actual, session)
    row = to_row(stood_in)
    estimate_args = ["--leaf", leaf, "--kind", "estimate", "--author", stood_in.author, "--replace", *_figure_args(
        {effort_segments.SCALE_MINUTES: row.get("minutes"), effort_segments.SCALE_USD: row.get("usd")})]
    owed = [actual_args, estimate_args]
    written = []
    ok, said = _run(run, actual_args, session)
    if not ok:
        return _outcome(written, f"the actual was not written: {said}", owed, session, already_there=already_there)
    written.append("actual")
    ok, said = _run(run, estimate_args, session)
    if not ok:
        return _outcome(written, f"the estimate from the actual was not written: {said}", owed, session,
                        already_there=already_there)
    written.append("estimate")
    replaced = None
    if held and held.get("confirmer"):
        replaced = {"confirmer": held["confirmer"], "minutes": held.get("minutes"), "usd": held.get("usd")}
    return _outcome(written, None, already_there=already_there, replaced_confirmed=replaced)
