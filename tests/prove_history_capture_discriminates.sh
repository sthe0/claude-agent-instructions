#!/usr/bin/env bash
# D9 mutation catalogue for tests/test_history_capture.py.
#
# A passing test suite proves the six capture points work TODAY; it says
# nothing about whether the assertions actually pin the fields they claim to
# (a test that never fails on a missing field is not evidence for anything).
# This proves discrimination directly: for each of the six D9 capture points,
# remove its field from its state.log(...) call site and confirm the one test
# written for it goes RED. replan has three call sites (no_change, refinement,
# substantive), each stripped by its own mutation against its own test, so a
# red earned by one site is never read as certifying the other two. A ninth
# mutation injects an actual refusal on an absent new argument, proving
# test_seven_command_directives_pinned_under_pre_d9_argument_shapes would
# catch a D9 regression that breaks the "no new refusal path" invariant,
# which a mere gates.py function-count check cannot see (a refusal added
# INSIDE an existing function changes no function count).
#
# Fields actually proven, one per capture point: declare -> expected;
# critique -> functional_ground; each replan site -> cause_source;
# present_plan -> rejection_text; reviewer_token; reviewer_raw. The tests
# also assert actual/mismatch, the other four critique fields and the replan
# cause's functional_ground/replanning_task/reason, but no mutation removes
# those, so this catalogue does not prove them.
#
# "Goes RED" is not enough: a syntax error, an import failure or an unrelated
# exception left behind by a mutation also makes pytest exit non-zero. A label
# is PASS only if its target test fails BECAUSE the mutation removed what it
# pins — pytest exits 1 AND the failure line it reports inside
# test_history_capture.py carries that label's expected signature. Every other
# red run is a typed "wrong reason" FAIL.
#
# Each label's target test must also be collected and GREEN against the
# unmutated pristine copy, so a stale target test name is reported as such
# rather than as a mutation result.
#
# Runs entirely inside a scratch copy of the worktree root's scripts/, tests/
# and config.md (preserving their relative layout, since
# test_history_capture.py resolves scripts/tests/ via `../scripts/tests`
# relative to its own path) — the real worktree is never mutated. Safe to run
# inside the working checkout.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
ROOT="$(cd "$HERE/.." && pwd -P)"

WORK="$(mktemp -d)"
trap '[[ -n "${WORK:-}" ]] && rm -rf "$WORK"' EXIT

mkdir -p "$WORK/pristine"
cp -R "$ROOT/scripts" "$WORK/pristine/scripts"
cp -R "$ROOT/tests" "$WORK/pristine/tests"
# agentctl/config.py reads config.md from the repo root, three levels above
# itself; without it every test crashes before any mutation matters.
cp "$ROOT/config.md" "$WORK/pristine/config.md"
find "$WORK/pristine" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# label -> "<the one test this mutation must turn RED><TAB><expected failure
# signature>". The signature is the exception the target test raises when
# exactly this label's field is gone; a label missing either half is a FAIL.
EXPECT() {
  case "$1" in
    declare)        printf '%s\t%s\n' test_declare_event_captures_declaration "KeyError: 'expected'" ;;
    critique)       printf '%s\t%s\n' test_critique_event_captures_critique "KeyError: 'functional_ground'" ;;
    replan_no_change)   printf '%s\t%s\n' test_replan_event_captures_cause_from_difficulty "KeyError: 'cause_source'" ;;
    replan_refinement)  printf '%s\t%s\n' test_replan_event_captures_cause_from_explicit_reason "KeyError: 'cause_source'" ;;
    replan_substantive) printf '%s\t%s\n' test_substantive_replan_event_captures_cause_from_difficulty "KeyError: 'cause_source'" ;;
    present_plan)   printf '%s\t%s\n' test_present_plan_event_captures_rejection_text "KeyError: 'rejection_text'" ;;
    reviewer_token) printf '%s\t%s\n' test_plan_review_event_captures_reviewer_token_and_raw "KeyError: 'reviewer_token'" ;;
    reviewer_raw)   printf '%s\t%s\n' test_plan_review_event_captures_reviewer_token_and_raw "KeyError: 'reviewer_raw'" ;;
    refusal)        printf '%s\t%s\n' test_seven_command_directives_pinned_under_pre_d9_argument_shapes "replan mismatch" ;;
  esac
}

# Runs one test of tests/test_history_capture.py inside a scratch copy, echoing
# pytest's combined output and returning its exit code. The verdict below
# depends on --tb=line and on colour being off, so both live only here.
run_target() {
  # $1 = scratch copy root, $2 = test name
  # PY_COLORS=0 rather than --color=no: with PY_COLORS=1 in the caller's
  # environment, --color=no still left no matchable crash line and every
  # correct mutation was rejected; PY_COLORS=0 overrides both PY_COLORS=1 and
  # FORCE_COLOR=1 from the caller.
  (cd "$1" && PY_COLORS=0 python3 -m pytest "tests/test_history_capture.py::$2" -q --no-header --tb=line -p no:cacheprovider 2>&1)
}

# Returns 0 if the named test function's own source contains what raises the
# signature: for "KeyError: '<k>'" the subscript ["<k>"] / ['<k>'], for any
# other signature the literal text (an assertion message).
signature_in_source() {
  # $1 = test file, $2 = test name, $3 = signature
  python3 - "$1" "$2" "$3" <<'PY'
import ast
import re
import sys

path, test_name, signature = sys.argv[1:4]
src = open(path, encoding="utf-8").read()
fn = next((n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.FunctionDef) and n.name == test_name), None)
if fn is None:
    sys.exit(1)
body = ast.get_source_segment(src, fn)
key = re.fullmatch(r"KeyError: '(.+)'", signature)
needles = [f'["{key[1]}"]', f"['{key[1]}']"] if key else [signature]
sys.exit(0 if any(n in body for n in needles) else 1)
PY
}

# Runs one target test against the UNMUTATED pristine copy, per label, just
# before that label's mutation. Returns 0 if it is collected and green and its
# source can still raise the label's signature; otherwise echoes why and
# returns 1 (not collected-and-green) or 2 (stale signature table).
pristine_check() {
  local test_name="$1" signature="$2" out rc
  out="$(run_target "$WORK/pristine" "$test_name")"
  rc=$?
  if [[ "$rc" -eq 0 ]]; then
    if ! signature_in_source "$WORK/pristine/tests/test_history_capture.py" "$test_name" "$signature"; then
      echo "\"$signature\" cannot be raised by $test_name's own source"
      return 2
    fi
    return 0
  fi
  case "$rc" in
    2|4|5) echo "collection/import error (rc=$rc) — target test not found, not collected, or its module fails to import: $(tail -3 <<< "$out" | tr '\n' ' ')" ;;
    *)     echo "pre-existing failure against the pristine copy (rc=$rc): $(tail -3 <<< "$out" | tr '\n' ' ')" ;;
  esac
  return 1
}

# Applies exactly one named D9 mutation to a scratch cli.py in place. Every
# substitution asserts its expected occurrence count first, so a mutation that
# silently fails to apply (source drifted, whitespace changed) raises loudly
# instead of leaving cli.py unmutated.
mutate() {
  # $1 = path to the scratch cli.py, $2 = mutation label
  python3 - "$1" "$2" <<'PY'
import sys


def replace_exact(text: str, old: str, new: str, expect: int, what: str) -> str:
    n = text.count(old)
    assert n == expect, f"{what}: expected {expect} occurrence(s) of {old!r}, found {n}"
    return text.replace(old, new)


path, label = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()

if label == "declare":
    text = replace_exact(
        text,
        'state.log("declare", expected=args.expected, actual=args.actual, mismatch=args.mismatch)',
        'state.log("declare")',
        1, "declare",
    )
elif label == "critique":
    text = replace_exact(
        text,
        '''    state.log(
        "critique",
        functional_ground=state.difficulty.critique.functional_ground,
        replanning_task=state.difficulty.critique.replanning_task,
        invariants_to_preserve=state.difficulty.critique.invariants_to_preserve,
        differences_to_remove=state.difficulty.critique.differences_to_remove,
        failure_address=state.difficulty.critique.failure_address,
    )''',
        '    state.log("critique")',
        1, "critique",
    )
elif label == "replan_no_change":
    text = replace_exact(
        text,
        'state.log("replan", kind="no_change", exited_diagnosing=True, **replan_cause)',
        'state.log("replan", kind="no_change", exited_diagnosing=True)',
        1, "replan_no_change",
    )
elif label == "replan_refinement":
    text = replace_exact(
        text,
        'state.log("replan", kind="refinement", exited_diagnosing=diagnosing, **replan_cause)',
        'state.log("replan", kind="refinement", exited_diagnosing=diagnosing)',
        1, "replan_refinement",
    )
elif label == "replan_substantive":
    text = replace_exact(
        text,
        'state.log("replan", kind="substantive", **replan_cause)',
        'state.log("replan", kind="substantive")',
        1, "replan_substantive",
    )
elif label == "present_plan":
    text = replace_exact(
        text,
        ''',
              rejection_text=getattr(args, "rejection_text", None))''',
        ")",
        1, "present_plan",
    )
elif label == "reviewer_token":
    text = replace_exact(
        text,
        "              reviewer_token=_normalize_reviewer_token(review.reviewer),\n",
        "",
        1, "reviewer_token",
    )
elif label == "reviewer_raw":
    text = replace_exact(
        text,
        "              reviewer_raw=review.reviewer,\n",
        "",
        1, "reviewer_raw",
    )
elif label == "refusal":
    text = replace_exact(
        text,
        '    replan_cause = _replan_cause(state, getattr(args, "reason", None))',
        '    replan_cause = _replan_cause(state, getattr(args, "reason", None))\n'
        '    if not hasattr(args, "reason"):\n'
        '        return Directive(False, state.node, "replan", "reason argument now required")',
        1, "refusal",
    )
else:
    raise SystemExit(f"unknown mutation label: {label}")

open(path, "w", encoding="utf-8").write(text)
PY
}

LABELS="declare critique replan_no_change replan_refinement replan_substantive present_plan reviewer_token reviewer_raw refusal"
TOTAL=0
FAILCOUNT=0
for label in $LABELS; do
  TOTAL=$((TOTAL+1))

  IFS=$'\t' read -r test_name signature <<< "$(EXPECT "$label")"
  if [[ -z "$test_name" || -z "$signature" ]]; then
    echo "FAIL $label: no target test and expected failure signature registered in EXPECT"
    FAILCOUNT=$((FAILCOUNT+1))
    continue
  fi

  why="$(pristine_check "$test_name" "$signature")"
  case $? in
    0) ;;
    2)
      echo "FAIL $label: stale signature table, not a discrimination result — $why; mutation not run"
      FAILCOUNT=$((FAILCOUNT+1))
      continue
      ;;
    *)
      echo "FAIL $label: target test $test_name is not collected-and-green on the pristine copy ($why)"
      FAILCOUNT=$((FAILCOUNT+1))
      continue
      ;;
  esac

  mut_dir="$WORK/mut_$label"
  rm -rf "$mut_dir"
  cp -R "$WORK/pristine" "$mut_dir"

  if ! mutate "$mut_dir/scripts/agentctl/cli.py" "$label"; then
    echo "FAIL $label: could not apply mutation (substitution did not match; see stderr above)"
    FAILCOUNT=$((FAILCOUNT+1))
    continue
  fi

  out="$(run_target "$mut_dir" "$test_name")"
  rc=$?
  # --tb=line reports the innermost frame, so this line exists only when the
  # exception was raised by the test's own code, not somewhere inside cli.py.
  crash_line="$(grep -m1 -E 'test_history_capture\.py:[0-9]+: ' <<< "$out" || true)"
  reason="${crash_line:-$(tail -3 <<< "$out" | tr '\n' ' ')}"
  if [[ "$rc" -eq 0 ]]; then
    echo "FAIL $label: $test_name stayed green against the mutated cli.py"
    echo "$out" | tail -5
    FAILCOUNT=$((FAILCOUNT+1))
  elif [[ "$rc" -ne 1 ]]; then
    echo "FAIL $label: $test_name went RED for the wrong reason (rc=$rc, not a test failure — expected rc=1 with \"$signature\"): $reason"
    FAILCOUNT=$((FAILCOUNT+1))
  elif [[ -z "$crash_line" || "$crash_line" != *"$signature"* ]]; then
    echo "FAIL $label: $test_name went RED for the wrong reason (rc=1, but its failure line lacks \"$signature\"): $reason"
    FAILCOUNT=$((FAILCOUNT+1))
  else
    echo "PASS $label: $test_name went RED because of the mutation ($crash_line)"
  fi
done

echo "=== summary: $((TOTAL-FAILCOUNT))/$TOTAL mutations proven"
if [[ "$FAILCOUNT" -ne 0 ]]; then
  echo "catalogue verdict: FAIL"
  exit 1
fi
echo "catalogue verdict: PASS (all $TOTAL mutations discriminate)"
