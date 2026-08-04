"""Non-regression harness over a committed corpus fixture.

Proves that tightening agentctl's plan-submission validation does not change how
an already-authored plan loads. The domain is FIXTURES_DIR/plan_corpus/, a
versioned copy of the plan corpus, deliberately NOT the author's live
~/.claude-agent/plans/ directory: a domain keyed on machine-local state is red on
every other machine (the directory is absent there) and turns red on this one the
moment an unrelated task writes a new plan into it. The live directory is still
worth watching, so `scan_live_drift` reports drift against it without ever
failing the suite — see `test_live_scan_is_advisory`.

Per plan the baseline records BOTH load modes. `lenient` (strict=False) is the
mode every already-authored plan is read through, and it must not move at all.
`strict` (strict=True) is recorded because it is the mode that MAY legitimately
tighten: new submission requirements belong in a submission-seam validator, not
in `parse_plan`'s `if strict:` branches, and a stage that puts them in the wrong
place shows up here as a strict-column flip. Its guard value depends on the
column being non-constant on arrival, which `test_strict_column_discriminates`
pins.

Regeneration (the ONLY supported way to move the baseline; review the diff — a
changed row is the regression this harness exists to report):

    python3 scripts/tests/test_frozen_plan_compat.py --update

`--update` prints what moved and, if the report is non-empty, refuses to write
until rerun with `--force`: writing is a second, deliberate command. It does not
make anyone READ the report — every genuine regeneration is non-empty, so
`--force` is the normal case, not the exception.

With no argument the same entry point prints the live-vs-fixture drift report
and the current baseline comparison, which is how `scan_live_drift` is consumed
outside the suite.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.plan import load_plan  # noqa: E402
from agentctl.state import Subject  # noqa: E402
from lib import config_root  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CORPUS_DIR = FIXTURES_DIR / "plan_corpus"
BASELINE_PATH = FIXTURES_DIR / "plan_corpus_baseline.json"

LIVE_PLANS_DIR = config_root.plans_dir()

# `scan_live_drift`'s `live_only` count grows by construction (every plan authored
# after the freeze adds one), so a bare count is not itself actionable. Past this
# many live-only plans, the fixture corpus no longer represents current
# plan-authoring patterns closely enough — consider re-freezing it (copy a
# representative sample into CORPUS_DIR and run `--update --force`).
#
# Basis: ~a third of the corpus this is judged against (55 plans at the freeze).
# Below that the fixture is still mostly what people write today; above it, more
# than a quarter of the population the harness claims to represent was authored
# after the sample was taken. A first calibration, not a measured one —
# recalibrate here on first firing.
LIVE_DRIFT_REFREEZE_THRESHOLD = 20

_MODES = (("lenient", False), ("strict", True))
# A sentinel `object()`, not an in-band string: a baseline value that literally
# equals a string sentinel (e.g. a plan whose outcome text IS "<absent>") would
# make a genuine key removal invisible to `compare`'s `!=` checks below.
_ABSENT = object()


def _display(value: object) -> object:
    """JSON-printable form of a comparison value for the printed report —
    `_ABSENT` is swapped for a readable marker only here, never compared as one."""
    return "<absent>" if value is _ABSENT else value


def _outcome_identity(exc: Exception, path: Path) -> str:
    """`<ExceptionClassName>: <first line>` — the failure's identity, so that a
    change in WHICH rule rejects a plan — or a change in WHICH exception class
    escapes `parse_plan` — is a visible baseline diff rather than a silent swap
    under a shared label. `record_plan` calls this for any exception, not only
    `PlanError`: `parse_plan` direct-indexes some strict-mode fields (e.g. a
    stage's `[stage.principle]` subfields) trusting an earlier guard that only
    fires for substantive plans, so a non-substantive plan can make it raise
    `KeyError` instead — an escaped exception class is itself the regression
    class this harness exists to report.

    The plan's own path is substituted out because two error messages embed it,
    and a baseline carrying absolute paths would be red on every checkout but the
    one it was generated in.
    """
    first = str(exc).splitlines()[0] if str(exc) else ""
    for concrete in (str(path), str(path.parent)):
        first = first.replace(concrete, "<plan>")
    return f"{type(exc).__name__}: {first}"


def _flatten(value: object, prefix: str = "") -> dict:
    """Flatten a nested asdict() structure to dotted/indexed leaf keys.

    An EMPTY dict or list is itself a leaf rather than an absent subtree: under
    the subset comparison below (see `compare`) a key the baseline never recorded
    is ignored, so an empty list left unrecorded would let a later change silently
    populate it. Recorded as a leaf, that same change is a changed key.
    """
    if isinstance(value, dict) and value:
        out: dict = {}
        for key, sub in value.items():
            out.update(_flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list) and value:
        out = {}
        for i, sub in enumerate(value):
            out.update(_flatten(sub, f"{prefix}[{i}]"))
        return out
    return {prefix: value}


def fingerprint(doc) -> dict:
    """Canonical projection of a loaded PlanDoc, closing the hole that a plan
    still loads but its content is silently coerced differently."""
    return _flatten(dataclasses.asdict(doc))


def record_plan(path: Path) -> dict:
    """Both load modes for one plan file: outcome per mode, plus the doc
    fingerprint for the lenient mode.

    Only the lenient doc is fingerprinted. Lenient is the mode already-authored
    plans are actually read through and the one that must not move; strict is
    allowed to tighten, so pinning its doc too would only add churn without
    adding a claim.

    Known blind spot: because strict docs are never fingerprinted, a coercion
    change on the strict-only path is invisible here — e.g. `plan.py` builds a
    stage's `Principle` via direct indexing under strict vs `.get()` with
    defaults under lenient, and only the lenient shape is pinned.
    """
    record: dict = {}
    for mode, strict in _MODES:
        try:
            doc = load_plan(path, strict=strict)
        except Exception as exc:  # any escape is itself a reportable outcome
            record[mode] = {"outcome": _outcome_identity(exc, path)}
            continue
        entry: dict = {"outcome": "ok"}
        if not strict:
            entry["doc"] = fingerprint(doc)
        record[mode] = entry
    return record


def records(directory: Path) -> dict:
    """The harness's domain: a GLOB over the corpus, never a hand-written list."""
    return {p.name: record_plan(p) for p in sorted(directory.glob("*.toml"))}


def compare(baseline: dict, actual: dict) -> dict:
    """Report how `actual` regressed against `baseline`; empty dict means green.

    SUBSET semantics on the doc fingerprint: every key PRESENT IN THE BASELINE
    must still carry the same value, and keys that appear later are ignored. This
    is not a weaker equality check, it is the exact non-regression claim —
    "loading an old plan still yields what it used to yield". A whole-doc
    equality (or a single signature hash) would redden all 55 rows the moment any
    stage adds a dataclass field with a default, which changes nothing about how
    old plans load; a suite that reddens on harmless additions trains whoever
    follows to regenerate the baseline without reading it, which destroys the
    guard. Do not "simplify" this back to `actual == baseline`.
    """
    report: dict = {}
    missing = sorted(set(baseline) - set(actual))
    added = sorted(set(actual) - set(baseline))
    if missing:
        report["missing_from_fixture"] = missing
    if added:
        report["new_in_fixture"] = added

    changed_outcome: dict = {}
    changed_doc: dict = {}
    for name in sorted(set(baseline) & set(actual)):
        base_rec, act_rec = baseline[name], actual[name]
        for mode in sorted(base_rec):
            base_mode = base_rec[mode]
            act_mode = act_rec.get(mode, {})
            if base_mode.get("outcome") != act_mode.get("outcome"):
                changed_outcome.setdefault(name, {})[mode] = {
                    "baseline": _display(base_mode.get("outcome", _ABSENT)),
                    "actual": _display(act_mode.get("outcome", _ABSENT)),
                }
            act_doc = act_mode.get("doc", {})
            for key, base_value in (base_mode.get("doc") or {}).items():
                act_value = act_doc.get(key, _ABSENT)
                if act_value != base_value:
                    changed_doc.setdefault(name, {})[key] = {
                        "baseline": base_value,
                        "actual": _display(act_value),
                    }
    if changed_outcome:
        report["changed_outcome"] = changed_outcome
    if changed_doc:
        report["changed_doc"] = changed_doc
    return report


def _update_baseline(current: dict, baseline_path: Path, *, force: bool) -> int:
    """Regenerate `baseline_path` from `current`, printing what moved and
    refusing to write when that report is non-empty unless `force` is set.

    Without this, `--update` rewrites the whole (megabyte-scale) baseline with
    zero feedback about what changed, which is how a snapshot guard degrades
    into a rubber stamp — the "review the diff" instruction in the module
    docstring otherwise means reading an 8800-line JSON diff nobody reads.
    """
    # A bootstrap run has no prior baseline, so `compare` reports all 55 plans as
    # new. Refusing there would demand `--force` to review a report whose content
    # is "everything, because there was nothing" — a gate with nothing behind it.
    bootstrap = not baseline_path.exists()
    old = {} if bootstrap else json.loads(baseline_path.read_text())
    report = compare(old, current)
    if report and not bootstrap:
        print(json.dumps(report, indent=1))
        if not force:
            print(
                "refusing to write: review the report above, then rerun with "
                "--force to write anyway"
            )
            return 1
    baseline_path.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    print(f"wrote {baseline_path} — {len(current)} plans")
    return 0


def scan_live_drift(
    live_dir: Path = LIVE_PLANS_DIR, corpus_dir: Path = CORPUS_DIR
) -> dict:
    """Report, without ever raising or failing anything, how the live plans
    directory has drifted from the versioned fixture.

    `live_only` grows by construction — every plan authored after the freeze
    adds one — so the raw count alone is not actionable; `_live_drift_advisory`
    turns it into one once it crosses `LIVE_DRIFT_REFREEZE_THRESHOLD`.

    A live directory that does not exist reports empty rather than erroring.
    Checked with `is_dir()` rather than a try/except around `glob()`: pathlib's
    `glob()` silently yields nothing for a missing directory instead of raising,
    so the except branch never fires and, uncaught, this function would fall
    through to comparing the real fixture against an empty live set — reporting
    every fixture plan as a spurious 'fixture_only' drift."""
    if not live_dir.is_dir():
        return {"live_only": [], "fixture_only": []}
    live_names = {p.name for p in live_dir.glob("*.toml")}
    fixture_names = {p.name for p in corpus_dir.glob("*.toml")}
    return {
        "live_only": sorted(live_names - fixture_names),
        "fixture_only": sorted(fixture_names - live_names),
    }


def _live_drift_advisory(live_only: list[str]) -> str | None:
    """The actionable form of `scan_live_drift`'s `live_only` count: `None`
    below `LIVE_DRIFT_REFREEZE_THRESHOLD`, else a one-line advisory naming the
    threshold and the action (re-freeze the corpus)."""
    if len(live_only) < LIVE_DRIFT_REFREEZE_THRESHOLD:
        return None
    return (
        f"live-only plans: {len(live_only)} >= LIVE_DRIFT_REFREEZE_THRESHOLD "
        f"({LIVE_DRIFT_REFREEZE_THRESHOLD}) — consider re-freezing the corpus "
        "(copy a representative sample into plan_corpus/ and run "
        "--update --force)"
    )


def test_baseline_matches():
    """Every fixture plan still loads — in both modes — to what it loaded at
    baseline time, and still yields the same doc content."""
    baseline = json.loads(BASELINE_PATH.read_text())
    report = compare(baseline, records(CORPUS_DIR))
    assert not report, report


def test_escape_from_load_is_recorded(monkeypatch):
    """The widened `except` records ANY escape from `load_plan`, not just the
    one live defect that happens to produce one today.

    Stated separately from `test_non_planerror_escape_is_recorded` because that
    test rides a real `plan.py` bug: moving principle-subfield validation to the
    submission seam is in this plan's remaining scope, and when a later stage
    does it, that test's pinned string goes stale and the natural fix is to
    delete it — taking the property with it. This one cannot go stale."""
    def boom(path, strict=False, **kwargs):
        raise RuntimeError("synthetic escape from the loader")

    monkeypatch.setattr("test_frozen_plan_compat.load_plan", boom)
    record = record_plan(CORPUS_DIR / sorted(p.name for p in CORPUS_DIR.glob("*.toml"))[0])

    assert record["lenient"]["outcome"] == "RuntimeError: synthetic escape from the loader"
    assert record["strict"]["outcome"] == record["lenient"]["outcome"]


def test_fingerprint_failure_still_propagates(monkeypatch):
    """The complementary property, and the actual risk the widened `except`
    introduces: `fingerprint` sits OUTSIDE the `try`, so a bug in the harness's
    own projection must still crash rather than be recorded as if it were a
    fact about the plan. A harness that reports its own breakage as a plan
    outcome is a harness that can go green while blind."""
    def boom(doc):
        raise RuntimeError("synthetic harness-side fingerprint bug")

    monkeypatch.setattr("test_frozen_plan_compat.fingerprint", boom)

    with pytest.raises(RuntimeError, match="harness-side"):
        record_plan(CORPUS_DIR / sorted(p.name for p in CORPUS_DIR.glob("*.toml"))[0])


def test_non_planerror_escape_is_recorded(tmp_path):
    """`record_plan` must RECORD a non-`PlanError` escape from `parse_plan`, not
    crash the run: `plan.py` direct-indexes a stage's `[stage.principle]`
    subfields under strict, guarded only when the plan is substantive, so a
    non-substantive plan with a partial principle table raises `KeyError`.

    Doubles as a canary on that `plan.py` direct-indexing bug: if a later stage
    moves the check to the submission seam, this test goes red by design. The
    mode-independent form of its property lives in
    `test_escape_from_load_is_recorded`, so replacing the pinned string here
    does not lose coverage."""
    plan_path = tmp_path / "partial_principle.toml"
    plan_path.write_text(
        '[meta]\n'
        'task_id = "partial-principle"\n'
        'goal = "trigger a non-PlanError escape from parse_plan"\n'
        'done_criterion = "n/a"\n'
        'criterion_type = "measurable"\n'
        'weight_class = "chat"\n'
        '\n'
        '[[stage]]\n'
        'index = 1\n'
        'title = "stage"\n'
        'executor = "in_thread"\n'
        'expected_result_image = "n/a"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "n/a"\n'
        '\n'
        '[stage.principle]\n'
        'statement = "only the statement is present"\n'
    )

    record = record_plan(plan_path)

    assert record["strict"]["outcome"] == "KeyError: 'source'"
    assert record["lenient"]["outcome"] == "ok"


def test_strict_column_discriminates():
    """The strict column's whole guard value is that it is non-constant on
    arrival: a stage that wrongly pushes a new requirement into `parse_plan`
    instead of the submission-seam validator flips rows here.

    Resolution limit: `parse_plan` is fail-fast, so only the FIRST tripped rule
    is ever observed per plan — 28 of the 30 strict failures in the corpus
    collapse onto one rule, all at stage 1. A new strict rule ordered after that
    one is unobservable on those 28 plans; the discrimination that matters lives
    in the 25 "ok" rows."""
    baseline = json.loads(BASELINE_PATH.read_text())
    strict_outcomes = {rec["strict"]["outcome"] for rec in baseline.values()}
    assert "ok" in strict_outcomes
    assert any(outcome.startswith("PlanError:") for outcome in strict_outcomes)


def test_harness_enumerates(tmp_path):
    """The harness's domain is a GLOB over the fixture directory, not a
    hand-written list of names: a plan file added at test time must show up as a
    divergence. Compared against its OWN pre-injection snapshot rather than the
    on-disk baseline, so a stale baseline cannot let this pass without the glob
    ever running."""
    corpus_copy = tmp_path / "plan_corpus"
    shutil.copytree(CORPUS_DIR, corpus_copy)
    before = records(corpus_copy)

    extra = corpus_copy / "zz_injected_extra_plan.toml"
    extra.write_text(
        '[meta]\n'
        'task_id = "zz-injected-extra-plan"\n'
        'goal = "synthetic plan injected by test_harness_enumerates"\n'
        'done_criterion = "n/a"\n'
        'criterion_type = "measurable"\n'
        'weight_class = "chat"\n'
        '\n'
        '[[stage]]\n'
        'index = 1\n'
        'title = "injected"\n'
        'executor = "in_thread"\n'
        'expected_result_image = "the harness reports this plan as new"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "n/a"\n'
    )

    after = records(corpus_copy)
    assert set(after) - set(before) == {"zz_injected_extra_plan.toml"}
    assert compare(before, after) == {"new_in_fixture": ["zz_injected_extra_plan.toml"]}
    assert after["zz_injected_extra_plan.toml"]["lenient"]["outcome"] == "ok"


def test_constant_record_reddens(monkeypatch):
    """The harness must fail on the class of change it guards. With the record
    function degraded to the constant every plan would report under a naive
    implementation, the comparison goes red — on the outcome axis (the strict
    column is not uniformly "ok") and on the doc axis (an empty fingerprint drops
    keys the baseline pinned)."""
    baseline = json.loads(BASELINE_PATH.read_text())
    monkeypatch.setattr(
        sys.modules[__name__],
        "record_plan",
        lambda path: {"lenient": {"outcome": "ok", "doc": {}}, "strict": {"outcome": "ok"}},
    )

    report = compare(baseline, records(CORPUS_DIR))

    assert report["changed_outcome"]
    assert report["changed_doc"]


def test_changed_doc_value_reddens():
    """A plan that still loads but whose content is coerced differently is a
    regression the outcome column alone cannot see."""
    baseline = json.loads(BASELINE_PATH.read_text())
    actual = json.loads(json.dumps(baseline))
    name = sorted(actual)[0]
    key = sorted(actual[name]["lenient"]["doc"])[0]
    actual[name]["lenient"]["doc"][key] = "coerced-differently"

    report = compare(baseline, actual)

    assert list(report["changed_doc"]) == [name]
    assert key in report["changed_doc"][name]


def test_added_field_does_not_redden():
    """Subset semantics, proven with the real machinery: a dataclass field added
    with a default (stage 2 adds `material_refs` to `Subject`) shows up in
    `asdict` as an extra key and must NOT redden the suite."""

    @dataclass
    class _SubjectPlusField(Subject):
        material_refs: list = field(default_factory=list)

    plan_path = sorted(CORPUS_DIR.glob("*.toml"))[0]
    record = record_plan(plan_path)
    baseline = {plan_path.name: record}

    doc = load_plan(plan_path, strict=False)
    stage = doc.stages[0]
    stage.subject = _SubjectPlusField(
        material=stage.subject.material,
        result=stage.subject.result,
        invariants=stage.subject.invariants,
    )
    widened = copy.deepcopy(record)
    widened["lenient"]["doc"] = fingerprint(doc)
    actual = {plan_path.name: widened}

    assert any(".material_refs" in key for key in widened["lenient"]["doc"])
    assert compare(baseline, actual) == {}


def test_live_scan_is_advisory(tmp_path):
    """A live-only plan produces a report, not a failure."""
    live_dir = tmp_path / "live"
    corpus_dir = tmp_path / "corpus"
    live_dir.mkdir()
    corpus_dir.mkdir()
    (live_dir / "only_in_live.toml").write_text("[meta]\n")

    report = scan_live_drift(live_dir=live_dir, corpus_dir=corpus_dir)

    assert report["live_only"] == ["only_in_live.toml"]


def test_live_scan_tolerates_missing_live_dir(tmp_path):
    """On a machine where the live directory was never created, the scan reports
    empty rather than erroring — it must never be the reason this suite fails."""
    report = scan_live_drift(
        live_dir=tmp_path / "does-not-exist", corpus_dir=CORPUS_DIR
    )
    assert report == {"live_only": [], "fixture_only": []}


def test_live_drift_advisory_fires_above_threshold():
    """Below `LIVE_DRIFT_REFREEZE_THRESHOLD` the drift count stays silent;
    at/above it, `_live_drift_advisory` names the threshold and the action —
    the fix for a `live_only` count that is otherwise unactionable by design."""
    below = ["p.toml"] * (LIVE_DRIFT_REFREEZE_THRESHOLD - 1)
    at = ["p.toml"] * LIVE_DRIFT_REFREEZE_THRESHOLD

    assert _live_drift_advisory(below) is None
    advisory = _live_drift_advisory(at)
    assert advisory is not None
    assert str(LIVE_DRIFT_REFREEZE_THRESHOLD) in advisory


def test_update_writes_when_report_is_empty(tmp_path):
    """No pending diff (the baseline already matches `current`) — `--update`
    writes without needing `--force`; only a non-empty report requires it."""
    baseline_path = tmp_path / "baseline.json"
    current = {"a.toml": {"lenient": {"outcome": "ok", "doc": {"meta.task_id": "a"}},
                           "strict": {"outcome": "ok"}}}
    baseline_path.write_text(json.dumps(current))

    exit_code = _update_baseline(current, baseline_path, force=False)

    assert exit_code == 0
    assert json.loads(baseline_path.read_text()) == current


def test_update_bootstraps_without_force(tmp_path):
    """With no baseline on disk, every plan reports as new — a report whose
    content is "everything, because there was nothing". Demanding `--force` to
    review that is a gate with nothing behind it, so the bootstrap run writes."""
    baseline_path = tmp_path / "baseline.json"
    current = {"a.toml": {"lenient": {"outcome": "ok", "doc": {"meta.task_id": "a"}},
                          "strict": {"outcome": "ok"}}}

    exit_code = _update_baseline(current, baseline_path, force=False)

    assert exit_code == 0
    assert json.loads(baseline_path.read_text()) == current


def test_update_refuses_without_force(tmp_path):
    """A non-empty pending report must block the write — `--update` without
    `--force` is a dry run, not a rubber stamp."""
    baseline_path = tmp_path / "baseline.json"
    old = {"a.toml": {"lenient": {"outcome": "ok", "doc": {"meta.task_id": "a"}},
                       "strict": {"outcome": "ok"}}}
    baseline_path.write_text(json.dumps(old))
    changed = {"a.toml": {"lenient": {"outcome": "ok", "doc": {"meta.task_id": "b"}},
                           "strict": {"outcome": "ok"}}}

    exit_code = _update_baseline(changed, baseline_path, force=False)

    assert exit_code == 1
    assert json.loads(baseline_path.read_text()) == old


def test_update_writes_with_force(tmp_path):
    """`--force` writes the reported diff — the escape hatch once the report
    has actually been reviewed."""
    baseline_path = tmp_path / "baseline.json"
    old = {"a.toml": {"lenient": {"outcome": "ok", "doc": {"meta.task_id": "a"}},
                       "strict": {"outcome": "ok"}}}
    baseline_path.write_text(json.dumps(old))
    changed = {"a.toml": {"lenient": {"outcome": "ok", "doc": {"meta.task_id": "b"}},
                           "strict": {"outcome": "ok"}}}

    exit_code = _update_baseline(changed, baseline_path, force=True)

    assert exit_code == 0
    assert json.loads(baseline_path.read_text()) == changed


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate the committed baseline from the current corpus",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --update, write even when the pending report is non-empty",
    )
    args = parser.parse_args(argv)

    current = records(CORPUS_DIR)
    if args.update:
        return _update_baseline(current, BASELINE_PATH, force=args.force)

    drift = scan_live_drift()
    print(json.dumps(drift, indent=1))
    advisory = _live_drift_advisory(drift["live_only"])
    if advisory:
        print(advisory)
    report = compare(json.loads(BASELINE_PATH.read_text()), current)
    if report:
        print(json.dumps(report, indent=1))
        return 1
    print(f"baseline green — {len(current)} plans")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
