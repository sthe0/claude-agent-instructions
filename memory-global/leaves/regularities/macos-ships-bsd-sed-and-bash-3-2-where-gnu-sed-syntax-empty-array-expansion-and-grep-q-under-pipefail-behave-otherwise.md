---
name: macos-ships-bsd-sed-and-bash-3-2-where-gnu-sed-syntax-empty-array-expansion-and-grep-q-under-pipefail-behave-otherwise
description: macOS ships BSD sed and bash 3.2, where GNU sed syntax, empty-array expansion and grep -q under pipefail behave otherwise than on Linux
type: reference
schema: regularity/v1
generality: 1
induced_from: [macos-shell-portability-gotchas]
rule_derived: "lifted from macos-shell-portability-gotchas.md; the leaf cites commits in sthe0/claude-agent-instructions but no experience leaf stem, so no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# macos-ships-bsd-sed-and-bash-3-2-where-gnu-sed-syntax-empty-array-expansion-and-grep-q-under-pipefail-behave-otherwise

## Regularity

macOS ships BSD userland and bash 3.2.57 (frozen before GPLv3), where a script verified on Linux silently misbehaves. BSD sed has no GNU BRE one-or-more: in sed -e 's/[^a-z0-9]\+/-/g' the \+ is read as a literal +, so the substitution does nothing, and the same holds for \? and \|; sed -E honours + on both BSD and GNU. In bash 3.2, expanding "${arr[@]}" on an empty array under set -u aborts with "unbound variable" (fixed in bash 4.4); the form ${arr[@]+"${arr[@]}"} expands to nothing when empty on every bash. In cmd | grep -q PATTERN under set -o pipefail, grep -q closes the pipe on the first match, a producer that keeps writing takes SIGPIPE (exit 141), and pipefail reports that as the pipeline status although the match succeeded. Under zsh, the macOS default shell, ${BASH_SOURCE[0]} is empty in a sourced file, while $0 holds the sourced file's path. These showed up as product bugs in the project_entry launcher subsystem on 2026-06-30 (commits c32ad73, 6007ff9, 5346c7a).

## Generality

Level 1 — ranges over shell scripts and their tests that were developed and verified on Linux or Homebrew bash 5 and then run on macOS with its BSD userland, bash 3.2 or zsh. It is deliberately not claimed for scripts that are pinned to a GNU userland and a newer bash on the machine where they run.

## Induced from

- [[macos-shell-portability-gotchas]] — contributes the four portability traps found as product bugs in the project_entry launcher subsystem on 2026-06-30: BSD sed `\+`, empty-array expansion under set -u, grep -q under pipefail, and `${BASH_SOURCE[0]}` under zsh.

## Refutation

Running the cited constructs under macOS /bin/bash 3.2 and BSD sed shows the substitution applied, the empty-array expansion not aborting under set -u, the grep -q pipeline not failing under pipefail, or ${BASH_SOURCE[0]} non-empty in a zsh-sourced file.
