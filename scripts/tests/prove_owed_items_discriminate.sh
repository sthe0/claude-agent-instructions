#!/usr/bin/env bash
# Proves test_owed_items.py discriminates via two named mutations against the
# CURRENT (post-D8) source tree, not a pre-fix revision — there is no useful
# "pre-fix commit" here (D8 built this contract from nothing), so each mutation
# targets exactly one property this stage introduced, applied with sed against a
# scratch copy of the two real source files.
#
#   disarm                 revert config.md's armed value back to the pre-D8 "0"
#                           -> must turn RED exactly:
#                              test_armed_value_matches_derivation_record
#
#   record_only_to_blocker make effort.record_crossing() delegate to
#                           effort.record_fire() — a WELL-FORMED call (valid
#                           Divergence, no exception), so the mutation's failure
#                           surfaces as an AssertionError in the engine probe, not
#                           as a crash the probe never got to evaluate
#                           -> must turn RED exactly:
#                              test_engine_probe_record_only_never_blocks_and_other_scales_stay_live
#
# Each mutation is verified to apply (sed reports a changed line count) before
# its variant is tested — an unapplied substitution would turn a red run into
# evidence of nothing. Works in a temp copy of the repo; safe to run inside the
# canonical checkout.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
TESTS="$HERE/test_owed_items.py"

WORK="$(mktemp -d)"
trap '[[ -n "${WORK:-}" ]] && rm -rf "$WORK"' EXIT

# Full working copy (tracked files only) so PYTHONPATH-relative imports and the
# venue-relative config.md read resolve exactly as they do for a real run.
# --cached + --others (not just --cached): this stage's own new files
# (test_owed_items.py, this script itself, the JSON derivation record) are not yet
# committed when this proof first runs against them.
git -C "$REPO_ROOT" ls-files -z --cached --others --exclude-standard -- scripts config.md docs/operations/effort-interactions-arming.json \
  | (cd "$REPO_ROOT" && xargs -0 tar cf -) \
  | (mkdir -p "$WORK/tree" && cd "$WORK/tree" && tar xf -)

run_variant() {
  local label="$1" tree="$2"
  echo "=== variant $label"
  (
    cd "$tree" \
      && PYTHONPATH="$tree/scripts:${PYTHONPATH:-}" \
         python3 -m pytest "scripts/tests/$(basename "$TESTS")" -q --no-header --tb=line -p no:cacheprovider
  )
}

TEST_NAMES="$(grep -oE '^def test_[A-Za-z0-9_]+' "$TESTS" | sed 's/^def //')"
TOTAL="$(printf '%s\n' "$TEST_NAMES" | grep -c .)"

overall=0

# --- baseline: unmodified post-D8 tree — every test must be green ------------
echo "=== variant baseline (unmodified post-D8 tree)"
base_out="$(run_variant baseline "$WORK/tree" 2>&1)"
base_rc=$?
echo "$base_out"
if [[ "$base_rc" -ne 0 ]]; then
  echo "  VERDICT: FAIL — baseline must be fully green, rc=$base_rc"
  overall=1
else
  echo "  VERDICT: ok — baseline green"
fi

# --- mutation: disarm ----------------------------------------------------------
cp -r "$WORK/tree" "$WORK/tree_disarm"
before="$(grep -c '`effort-absolute-interactions` | `43`' "$WORK/tree_disarm/config.md")"
sed -i 's/`effort-absolute-interactions` | `43`/`effort-absolute-interactions` | `0`/' "$WORK/tree_disarm/config.md"
after="$(grep -c '`effort-absolute-interactions` | `0`' "$WORK/tree_disarm/config.md")"
if [[ "$before" -ne 1 || "$after" -ne 1 ]]; then
  echo "FATAL: disarm mutation did not apply (before=$before after=$after)" >&2
  exit 2
fi
disarm_out="$(run_variant disarm "$WORK/tree_disarm" 2>&1)"
echo "$disarm_out"
disarm_red="$(grep -oE '^FAILED scripts/tests/test_owed_items\.py::test_[A-Za-z0-9_]+' <<< "$disarm_out" | sed 's#.*::##' | sort -u)"
if [[ "$disarm_red" == "test_armed_value_matches_derivation_record" ]]; then
  echo "PASS disarm — exactly test_armed_value_matches_derivation_record red"
else
  echo "FAIL disarm — expected exactly test_armed_value_matches_derivation_record red, got: $disarm_red"
  overall=1
fi

# --- mutation: record_only_to_blocker ------------------------------------------
# Replace record_crossing's BODY with a well-formed delegation to record_fire — a
# valid Divergence, no exception — so a broken delivery fails the engine probe's
# own assertions rather than crashing before the probe evaluates anything.
cp -r "$WORK/tree" "$WORK/tree_blocker"
EFFORT_PY="$WORK/tree_blocker/scripts/agentctl/effort.py"
before="$(grep -c 'state.effort_crossings.append(record)' "$EFFORT_PY")"
python3 - "$EFFORT_PY" <<'PY'
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
needle = (
    "    record = {\n"
    '        "scale": scale,\n'
    '        "actual": actual,\n'
    '        "estimate": estimate,\n'
    '        "baseline": baseline,\n'
    '        "ts": now,\n'
    "    }\n"
    "    state.effort_crossings.append(record)\n"
    "    return record\n"
)
replacement = (
    "    return record_fire(\n"
    "        state,\n"
    "        Divergence(scale=scale, kind=\"absolute\", actual=actual, estimate=estimate,\n"
    "                   multiple=1.0, framing=\"mutated: record_crossing delegates to record_fire\"),\n"
    "        now=now,\n"
    "    )\n"
)
assert text.count(needle) == 1, f"needle not found exactly once (count={text.count(needle)})"
open(path, "w", encoding="utf-8").write(text.replace(needle, replacement))
PY
mutate_rc=$?
after="$(grep -c 'state.effort_crossings.append(record)' "$EFFORT_PY")"
if [[ "$mutate_rc" -ne 0 || "$before" -ne 1 || "$after" -ne 0 ]]; then
  echo "FATAL: record_only_to_blocker mutation did not apply (rc=$mutate_rc before=$before after=$after)" >&2
  exit 2
fi
blocker_out="$(run_variant record_only_to_blocker "$WORK/tree_blocker" 2>&1)"
echo "$blocker_out"
blocker_red="$(grep -oE '^FAILED scripts/tests/test_owed_items\.py::test_[A-Za-z0-9_]+' <<< "$blocker_out" | sed 's#.*::##' | sort -u)"
if [[ "$blocker_red" == "test_engine_probe_record_only_never_blocks_and_other_scales_stay_live" ]]; then
  echo "PASS record_only_to_blocker — exactly test_engine_probe_record_only_never_blocks_and_other_scales_stay_live red"
else
  echo "FAIL record_only_to_blocker — expected exactly test_engine_probe_record_only_never_blocks_and_other_scales_stay_live red, got: $blocker_red"
  overall=1
fi

echo "=== summary ($TOTAL tests total in $TESTS)"
if [[ "$overall" -ne 0 ]]; then
  echo "DISCRIMINATION NOT PROVEN"
  exit 1
fi
echo "DISCRIMINATION PROVEN (baseline green; disarm -> armed-value test red only; record_only_to_blocker -> divergence test red only)"
