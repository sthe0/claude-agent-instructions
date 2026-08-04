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

With no argument the same entry point prints the live-vs-fixture drift report
and the current baseline comparison, which is how `scan_live_drift` is consumed
outside the suite.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.plan import PlanError, load_plan  # noqa: E402
from agentctl.state import Subject  # noqa: E402
from lib import config_root  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CORPUS_DIR = FIXTURES_DIR / "plan_corpus"
BASELINE_PATH = FIXTURES_DIR / "plan_corpus_baseline.json"

LIVE_PLANS_DIR = config_root.plans_dir()

_MODES = (("lenient", False), ("strict", True))
_ABSENT = "<absent>"


def _outcome_identity(exc: PlanError, path: Path) -> str:
    """`PlanError: <first line>` — the failure's identity, so that a change in
    WHICH rule rejects a plan is a visible baseline diff rather than a silent
    swap under a shared "PlanError" label.

    The plan's own path is substituted out because two error messages embed it,
    and a baseline carrying absolute paths would be red on every checkout but the
    one it was generated in.
    """
    first = str(exc).splitlines()[0] if str(exc) else ""
    for concrete in (str(path), str(path.parent)):
        first = first.replace(concrete, "<plan>")
    return f"PlanError: {first}"


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
    """
    record: dict = {}
    for mode, strict in _MODES:
        try:
            doc = load_plan(path, strict=strict)
        except PlanError as exc:
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
                    "baseline": base_mode.get("outcome", _ABSENT),
                    "actual": act_mode.get("outcome", _ABSENT),
                }
            act_doc = act_mode.get("doc", {})
            for key, base_value in (base_mode.get("doc") or {}).items():
                act_value = act_doc.get(key, _ABSENT)
                if act_value != base_value:
                    changed_doc.setdefault(name, {})[key] = {
                        "baseline": base_value,
                        "actual": act_value,
                    }
    if changed_outcome:
        report["changed_outcome"] = changed_outcome
    if changed_doc:
        report["changed_doc"] = changed_doc
    return report


def scan_live_drift(
    live_dir: Path = LIVE_PLANS_DIR, corpus_dir: Path = CORPUS_DIR
) -> dict:
    """Report, without ever raising or failing anything, how the live plans
    directory has drifted from the versioned fixture.

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


def test_baseline_matches():
    """Every fixture plan still loads — in both modes — to what it loaded at
    baseline time, and still yields the same doc content."""
    baseline = json.loads(BASELINE_PATH.read_text())
    report = compare(baseline, records(CORPUS_DIR))
    assert not report, report


def test_strict_column_discriminates():
    """The strict column's whole guard value is that it is non-constant on
    arrival: a stage that wrongly pushes a new requirement into `parse_plan`
    instead of the submission-seam validator flips rows here."""
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
    baseline = {plan_path.name: record_plan(plan_path)}

    doc = load_plan(plan_path, strict=False)
    stage = doc.stages[0]
    stage.subject = _SubjectPlusField(
        material=stage.subject.material,
        result=stage.subject.result,
        invariants=stage.subject.invariants,
    )
    widened = record_plan(plan_path)
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


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate the committed baseline from the current corpus",
    )
    args = parser.parse_args(argv)

    current = records(CORPUS_DIR)
    if args.update:
        BASELINE_PATH.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        print(f"wrote {BASELINE_PATH} — {len(current)} plans")
        return 0

    print(json.dumps(scan_live_drift(), indent=1))
    report = compare(json.loads(BASELINE_PATH.read_text()), current)
    if report:
        print(json.dumps(report, indent=1))
        return 1
    print(f"baseline green — {len(current)} plans")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
