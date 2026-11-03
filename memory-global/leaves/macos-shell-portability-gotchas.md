---
name: macos-shell-portability-gotchas
description: Three macOS-specific shell-portability traps that pass on Linux but break on macOS — BSD sed lacks GNU '\+', bash 3.2 errors on empty "${arr[@]}" under set -u, and 'cmd | grep -q' under pipefail spuriously fails via SIGPIPE.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:macos-ships-bsd-sed-and-bash-3-2-where-gnu-sed-syntax-empty-array-expansion-and-grep-q-under-pipefail-behave-otherwise, regularity:a-tool-relies-on-facilities-its-authors-host-supplies-unasked]
grounding_judged: 924e13aeb350fc963941311aa08f970f337f5425d4d2f8d18af6be502ab0d0ab
created: 2026-06-30
last_verified: 2026-06-30
---

## Difficulty

A shell script (or its tests) written and verified on Linux silently misbehaves on macOS because macOS ships **BSD userland** and an ancient **bash 3.2.57** (`/usr/bin/env bash` → `/bin/bash`; Apple froze it pre-GPLv3). The symptoms look like product bugs but are portability traps; each cost real rediscovery time on the `project_entry` launcher subsystem (a past era's task-entry subsystem on this machine, 2026-06-30).

## Principle

To keep a shell script and its tests working on macOS as on Linux, write sed with -E and + instead of GNU \+, expand possibly-empty arrays as ${arr[@]+"${arr[@]}"}, capture a producer's output and then grep it instead of piping into grep -q under pipefail, self-locate a sourced file with ${BASH_SOURCE[0]:-$0}, and verify shell changes under /bin/bash 3.2 with set -uo pipefail and under zsh, not only the Linux or Homebrew bash 5 they were developed on.

## Goal class

shell scripts that run on macOS and Linux alike

## Grounded in

- [[macos-ships-bsd-sed-and-bash-3-2-where-gnu-sed-syntax-empty-array-expansion-and-grep-q-under-pipefail-behave-otherwise]]
- [[a-tool-relies-on-facilities-its-authors-host-supplies-unasked]]

## Guidance

Three concrete traps and their portable fixes:

- **BSD sed has no `\+` (GNU BRE one-or-more).** `sed -e 's/[^a-z0-9]\+/-/g'` on macOS treats `\+` as a *literal* `+`, so the substitution silently does nothing (e.g. spaces in a slug survive: `Add the widget` → `add the widget` instead of `add-the-widget`). **Fix:** use ERE — `sed -E -e 's/[^a-z0-9]+/-/g'` — `-E` and `+` are honored by both BSD and GNU sed. Same applies to `\?` and `\|`; prefer `sed -E`. (Other GNU-isms to avoid: `grep -P`, `readlink -f`, `date -d`.)

- **bash 3.2: `"${arr[@]}"` on an EMPTY array under `set -u` aborts the shell** ("unbound variable"; fixed in bash 4.4). A function that does `_cargs=("$@")` then `cmd "${_cargs[@]}"` dies when called with no extra args inside a `set -u` script. **Fix:** the empty-safe idiom `${arr[@]+"${arr[@]}"}` — expands to nothing when empty, to all elements otherwise, on every bash incl. 3.2. (Harmless in a normal interactive shell, which has no `set -u` — so this only bites scripts/tests, not real use.)

- **`cmd | grep -q PATTERN` under `set -o pipefail` can spuriously fail (exit 141).** `grep -q` closes the pipe on the first match; if the left-hand producer keeps writing afterward it takes **SIGPIPE** (128+13 = 141), and `pipefail` surfaces that as the pipeline's exit status — so the `if` fails *even though the match succeeded*. **Fix:** capture then grep — `out="$(cmd 2>/dev/null)"; grep -q PATTERN <<<"$out"` (or `printf '%s\n' "$out" | grep -q …`).

**Self-location in sourced files** is a related macOS/zsh trap (separate leaf-worthy fact, recorded inline here): `${BASH_SOURCE[0]}` is empty when a file is `source`d from **zsh** (the macOS default shell), so `dirname` resolves wrong. Use `${BASH_SOURCE[0]:-$0}` — zsh sets `$0` to the sourced file path; no-op under bash.

Verify shell changes under **`/bin/bash` (3.2) with `set -uo pipefail`** and under **zsh**, not just the Homebrew bash 5 / Linux bash you develop on.

> verified by: project_entry launcher fixes 2026-06-30 — commits c32ad73 (BSD sed), 6007ff9 (empty-array + SIGPIPE), 5346c7a (zsh self-location) in sthe0/claude-agent-instructions.

## See also

- [[remote-sudo-access-paths]] — another shell/OS-environment gotcha set (TTY-less `!` shell, BSD vs Linux behavior).
