#!/usr/bin/env bash
# Smoke-check that every skill EXTRACTED from this repo into the machine-local
# skills overlay is still invocable on this machine.
#
# Why it exists: the repo ships the skills MECHANISM plus the skills useful to any
# reader; a skill that is specific to one machine or one organization lives in
# <agent-home>/skills-local/ instead and reaches the Claude Code catalog only
# through the symlink setup-symlinks.sh creates. Nothing inside the repo can see
# that artifact, so a missing or dangling link makes the skill silently
# unavailable — the failure no in-repo check can catch.
#
# When to run it: after scripts/setup-symlinks.sh, and whenever a skill has
# disappeared from the catalog. scripts/verify-layout-contract.sh covers the other
# direction (extracted skills must not reappear in the repo, and must be present in
# the overlay); this script covers resolution — the link actually leads to a skill.
#
# How to run it:  bash scripts/verify-extracted-skills-resolve.sh
# It also runs as part of scripts/verify-instructions-sync.sh, next to the layout
# contract, so the installer path covers it too.
#
# Input: the extracted-skills manifest — format and location in
# lib/extracted-skills.sh. No manifest is a valid state: the script reports zero
# extracted skills and exits 0.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/config-root.sh
source "$SCRIPTS_DIR/lib/config-root.sh"  # exports CLAUDE_AGENT_HOME
# shellcheck source=scripts/lib/extracted-skills.sh
source "$SCRIPTS_DIR/lib/extracted-skills.sh"

MANIFEST="$CLAUDE_AGENT_HOME/extracted-skills.local"

if [[ ! -f "$MANIFEST" ]]; then
  echo "OK: no extracted-skills manifest at $MANIFEST — nothing to resolve."
  exit 0
fi

FAIL=0
COUNT=0

while IFS= read -r name; do
  COUNT=$((COUNT + 1))
  skill="$CLAUDE_AGENT_HOME/skills/$name/SKILL.md"
  if [[ -f "$skill" ]]; then
    echo "OK: $name resolves ($skill)"
  else
    echo "FAIL: extracted skill '$name' does not resolve at $skill — run scripts/setup-symlinks.sh"
    FAIL=1
  fi
done < <(extracted_skill_names "$MANIFEST")

if [[ "$FAIL" -ne 0 ]]; then
  echo "Extracted-skill resolution failed. Manifest: $MANIFEST"
  exit 1
fi
echo "Extracted-skill resolution ok ($COUNT skill(s) checked)."
