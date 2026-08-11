#!/usr/bin/env bash
# Proves that test_sync_stash_pop_safety.py discriminates: a test authored next
# to its fix is satisfied by that fix by construction, so the only evidence it
# pins anything is an observed failure against code that lacks the fix.
#
# Three variants, because a base-only proof is not enough — a test set can go
# fully green against a HALF-fixed script:
#   A  the pre-fix script at $SYNC_BASE_REV                    -> ALL must fail
#   B  A with only the has_uncommitted predicate made honest   -> >=1 must fail
#   C  B plus ownership decided by "did an entry appear", but  -> >=1 must fail
#      retrieval still positional (`git stash pop`, no ref)
#
# C must ALSO neuter cmd_pull's own `local did_stash=false; if has_uncommitted;
# then stash_if_dirty; did_stash=true; fi` gate. Without that the surrogate is
# inert: cmd_pull re-decides whether to pop by calling has_uncommitted a SECOND
# time, so swapping stash_if_dirty / pop_stash_if_any alone cannot change one
# observable, and B and C go red on an identical set — a third variant proving
# nothing. (Measured; it is what an earlier revision of this file did.)
#
# The separation enforced below is `red(B) != red(C)`, NOT "C red where B is
# green". The latter cannot exist, and the asymmetry is structural rather than
# incidental: C's fixes are a strict superset of B's, and the ownership check C
# adds can only SUPPRESS a pop that B would have performed. Suppressing a pop
# turns a red green; it can never turn a green red. Measured separator: the
# relapsed-predicate test, red on B and green on C.
#
# What C's own red set is worth: ownership by "an entry appeared" plus positional
# retrieval STILL hands a concurrent writer's entry to the caller — tests (b) and
# (b'), where a hook lands a foreign stash on top mid-pull. That is the machine
# evidence that identity BY SHA, not by appearance, is the load-bearing half of
# the fix, and it is why those two hook fixtures are not decorative.
#
# Reads the repository (git show) and works in temp dirs only, so it is safe to
# run inside the canonical checkout.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
# Pinned by sha, never by `main`: after landing, main carries the fix and a
# main-relative baseline would invert this whole check.
BASE_REV="${SYNC_BASE_REV:-f087002}"
TESTS="$HERE/test_sync_stash_pop_safety.py"

WORK="$(mktemp -d)"
trap '[[ -n "${WORK:-}" ]] && rm -rf "$WORK"' EXIT

# Own HOME, no inherited repo pointer: a test that forgot to override either
# must still be unable to reach a real repository's stash stack or the real log.
export HOME="$WORK/home"
mkdir -p "$HOME"
unset CLAUDE_INSTRUCTIONS_REPO

if ! git -C "$REPO_ROOT" show "$BASE_REV:scripts/sync-instructions-repo.sh" > "$WORK/variant_a.sh"; then
  echo "FATAL: cannot read $BASE_REV:scripts/sync-instructions-repo.sh" >&2
  exit 2
fi

python3 - "$WORK" <<'PY' || exit 2
import pathlib
import re
import sys

work = pathlib.Path(sys.argv[1])
base = (work / "variant_a.sh").read_text()

HONEST = 'has_uncommitted() {\n  [[ -n "$(git status --porcelain)" ]]\n}'
OWNED_STASH = '''stash_if_dirty() {
  if has_uncommitted; then
    local before after
    before="$(git rev-parse --verify --quiet 'stash@{0}' 2>/dev/null || true)"
    log "stash uncommitted changes"
    git stash push -u -m "sync-instructions-repo $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    after="$(git rev-parse --verify --quiet 'stash@{0}' 2>/dev/null || true)"
    if [[ -n "$after" && "$after" != "$before" ]]; then SYNC_DID_STASH=1; fi
  fi
}'''
POSITIONAL_POP = '''pop_stash_if_any() {
  if [[ "${SYNC_DID_STASH:-0}" == 1 ]]; then
    log "stash pop"
    if ! git stash pop; then
      log "WARN: stash pop conflict — resolve manually in $REPO"
      return 1
    fi
  fi
  return 0
}'''

# cmd_pull decides for itself whether the pop runs, by calling has_uncommitted a
# second time. Leave this in place and the surrogate above is unreachable.
OUTER_GATE_OLD = '''  local did_stash=false
  if has_uncommitted; then
    stash_if_dirty
    did_stash=true
  fi'''
OUTER_GATE_NEW = '''  local did_stash=true
  stash_if_dirty'''


def sub(text, func, replacement, what):
    # An unapplied patch would turn a red run into evidence of nothing.
    out, n = re.subn(func + r"\(\) \{.*?\n\}", lambda m: replacement, text, flags=re.S)
    assert n == 1, f"{what}: {func} substitution did not apply (n={n})"
    return out


def sub_literal(text, old, new, what):
    assert text.count(old) == 1, f"{what}: outer-gate substitution did not apply"
    return text.replace(old, new)


variant_b = sub(base, "has_uncommitted", HONEST, "variant B")
(work / "variant_b.sh").write_text(variant_b)

variant_c = sub(variant_b, "stash_if_dirty", OWNED_STASH, "variant C")
variant_c = sub(variant_c, "pop_stash_if_any", POSITIONAL_POP, "variant C")
variant_c = sub_literal(variant_c, OUTER_GATE_OLD, OUTER_GATE_NEW, "variant C")
(work / "variant_c.sh").write_text(variant_c)
PY

# Enumerated from the file, not hardcoded, so a test added later cannot be
# silently left out of the proof.
TEST_NAMES="$(grep -oE '^def test_[A-Za-z0-9_]+' "$TESTS" | sed 's/^def //')"
TOTAL="$(printf '%s\n' "$TEST_NAMES" | grep -c .)"
if [[ "$TOTAL" -eq 0 ]]; then
  echo "FATAL: no tests found in $TESTS" >&2
  exit 2
fi

describe() {
  case "$1" in
    a) echo "pre-fix script at $BASE_REV" ;;
    b) echo "only has_uncommitted made honest" ;;
    c) echo "honest predicate + entry-appeared ownership (outer gate neutered) + positional pop" ;;
  esac
}

# Membership in a newline-separated list without a subprocess — `printf | grep -q`
# can die as SIGPIPE 141 under `set -o pipefail` on bash 3.2 (macOS).
in_list() {
  local nl=$'\n'
  case "$nl$2" in
    *"$nl$1$nl"*) return 0 ;;
  esac
  return 1
}

overall=0
RED_a=""
RED_b=""
RED_c=""
for variant in a b c; do
  script="$WORK/variant_$variant.sh"
  echo "=== variant $variant — $(describe "$variant")"
  failed=0
  red_names=""
  while IFS= read -r t; do
    [[ -z "$t" ]] && continue
    out="$(cd "$REPO_ROOT" && SYNC_SCRIPT="$script" python3 -m pytest "$TESTS::$t" \
             -q --no-header --tb=line -p no:cacheprovider 2>&1)"
    rc=$?
    if [[ "$rc" -ne 0 ]]; then
      failed=$((failed+1))
      red_names="$red_names$t"$'\n'
      # --tb=line renders each failure as "<file>:<line>: <exception first line>",
      # which is the assertion that discriminated. Anchor on the test file's own
      # name — unrelated site-packages warnings share that shape.
      reason="$(grep -m1 -E "$(basename "$TESTS"):[0-9]+: " <<< "$out" || true)"
      [[ -z "$reason" ]] && reason="$(tail -3 <<< "$out" | tr '\n' ' ')"
      echo "  RED   $t"
      echo "        $reason"
    else
      echo "  green $t"
    fi
  done <<< "$TEST_NAMES"

  case "$variant" in
    a) RED_a="$red_names" ;;
    b) RED_b="$red_names" ;;
    c) RED_c="$red_names" ;;
  esac

  if [[ "$variant" == a ]]; then
    if [[ "$failed" -ne "$TOTAL" ]]; then
      echo "  VERDICT: FAIL — $failed/$TOTAL red, all $TOTAL must be red on the pre-fix script"
      overall=1
    else
      echo "  VERDICT: ok — $failed/$TOTAL red"
    fi
  elif [[ "$failed" -lt 1 ]]; then
    echo "  VERDICT: FAIL — 0/$TOTAL red, at least one must be red on this surrogate"
    overall=1
  else
    echo "  VERDICT: ok — $failed/$TOTAL red"
  fi
done

echo "=== summary (R = red against that variant)"
printf '  %-62s %s  %s  %s\n' "test" "A" "B" "C"
while IFS= read -r t; do
  [[ -z "$t" ]] && continue
  ca="."; cb="."; cc="."
  in_list "$t" "$RED_a" && ca="R"
  in_list "$t" "$RED_b" && cb="R"
  in_list "$t" "$RED_c" && cc="R"
  printf '  %-62s %s  %s  %s\n' "$t" "$ca" "$cb" "$cc"
done <<< "$TEST_NAMES"

# A variant whose red set matches its predecessor's is a variant that proves
# nothing the predecessor did not already prove — the reason this check exists.
separators=""
backwards=""
while IFS= read -r t; do
  [[ -z "$t" ]] && continue
  if in_list "$t" "$RED_b" && ! in_list "$t" "$RED_c"; then
    separators="$separators $t(red on B, green on C)"
  elif in_list "$t" "$RED_c" && ! in_list "$t" "$RED_b"; then
    backwards="$backwards $t"
  fi
done <<< "$TEST_NAMES"

# C's fixes are a strict superset of B's and its ownership check can only
# SUPPRESS a pop B would have performed, so red(C) must be a proper subset of
# red(B). A test red on C but green on B therefore does not mean "C proved
# more" — it means C stopped being that superset, i.e. the surrogate is
# malformed. Accepting it as a separator would report a broken variant as proof.
if [[ -n "$backwards" ]]; then
  echo "  VERDICT: FAIL — red on C but green on B:$backwards"
  echo "           C is built as B plus an ownership check, which can only turn red"
  echo "           green. This direction means the surrogate no longer contains B."
  overall=1
elif [[ -z "$separators" ]]; then
  echo "  VERDICT: FAIL — variants B and C are red on an identical set, so C adds no"
  echo "           evidence B does not already supply. Repair the surrogate so its"
  echo "           ownership check is reachable, or delete variant C."
  overall=1
else
  echo "  B/C separator:$separators"
fi

if [[ "$overall" -ne 0 ]]; then
  echo "DISCRIMINATION NOT PROVEN"
  exit 1
fi
echo "DISCRIMINATION PROVEN (A all red; B and C red where it counts; B != C)"
