#!/usr/bin/env bash
# Hermetic test for slugify.py — the org-neutral title -> slug derivation.
#
# Asserts: ascii titles produce the SAME slug as the old `[a-z0-9]`+dash rule
# (no regression for the common case), accented Latin folds, Cyrillic
# transliterates, symbol/emoji-only collapses to EMPTY, and length is capped at
# 40 with no trailing dash. Drives the CLI exactly as enter-task.sh does.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLUGIFY="$HERE/../slugify.py"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  [ OK ] %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }

# eq <label> <input> <expected>
eq() {
  local got; got="$(python3 "$SLUGIFY" "$2")"
  if [[ "$got" == "$3" ]]; then ok "$1 ('$2' -> '$got')"
  else bad "$1 — got '$got', expected '$3'"; fi
}

# 1. ascii passthrough: same as the old slug rule.
eq "ascii passthrough"        "Add the widget"     "add-the-widget"
eq "ascii: symbols squeezed"  "Fix: A/B  test!!"   "fix-a-b-test"
eq "ascii: trims edges"       "  -hello-  "        "hello"

# 2. accented Latin folds via NFKD.
eq "accent fold (Cafe)"       "Café"               "cafe"
eq "accent fold (resume)"     "Résumé déjà"        "resume-deja"

# 3. Cyrillic transliterates instead of vanishing.
eq "cyrillic basic"           "Привет мир"         "privet-mir"
eq "cyrillic multi-char"      "Очередь задач"      "ochered-zadach"
eq "mixed latin+cyrillic"     "Fix баг now"        "fix-bag-now"

# 4. symbol / emoji-only collapses to EMPTY (caller handles the fallback).
eq "symbols only -> empty"    "★ ☆"                ""
eq "emoji only -> empty"      "🚀🔥"               ""

# 5. stdin path (no argv) behaves identically.
got_stdin="$(printf '%s' "Привет мир" | python3 "$SLUGIFY")"
if [[ "$got_stdin" == "privet-mir" ]]; then ok "stdin path matches argv"
else bad "stdin path — got '$got_stdin'"; fi

# 6. truncation to 40 chars + no trailing dash.
long_in="aaaaaaaaaa-bbbbbbbbbb-cccccccccc-dddddddddd-eeeeeeeeee"
got_long="$(python3 "$SLUGIFY" "$long_in")"
if [[ "${#got_long}" -le 40 && "$got_long" != *- ]]; then
  ok "truncates to <=40, no trailing dash (len=${#got_long})"
else
  bad "truncation — got '$got_long' (len=${#got_long})"
fi
# A title whose 40th char lands on a dash must drop that dash.
got_trim="$(python3 "$SLUGIFY" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa b")"
if [[ "$got_trim" != *- ]]; then ok "no trailing dash after truncation cut"
else bad "trailing dash survived truncation — got '$got_trim'"; fi

echo
printf 'slugify tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
