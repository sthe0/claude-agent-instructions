#!/usr/bin/env bash
# Reader for the extracted-skills manifest: <agent-home>/extracted-skills.local.
#
# A skill specific to one machine or one organization does not ship in this public
# repo — it lives in <agent-home>/skills-local/ and reaches the Claude Code catalog
# through the symlink setup-symlinks.sh creates. Its NAME is machine-local data
# too, so the manifest carries the names and this repo carries none.
#
# Format: one skill name per line, '#' starts a comment, blank lines ignored. A
# missing manifest is a valid state (no extracted skills) yielding no names — which
# is what keeps the controls green on a clone that extracted nothing.
#
# The format lives here, in one place, because two controls must agree on it:
# verify-layout-contract.sh (each name gone from the repo, present in the overlay)
# and verify-extracted-skills-resolve.sh (each name resolves in the catalog).
#
# Usage:
#   source "$SCRIPTS_DIR/lib/extracted-skills.sh"
#   while IFS= read -r name; do ...; done < <(extracted_skill_names)

extracted_skill_names() {
  local manifest="${1:-${CLAUDE_AGENT_HOME:-}/extracted-skills.local}"
  [[ -f "$manifest" ]] || return 0
  local line name
  while IFS= read -r line || [[ -n "$line" ]]; do
    name="${line%%#*}"
    name="${name//[[:space:]]/}"
    [[ -n "$name" ]] && printf '%s\n' "$name"
  done < "$manifest"
  return 0
}
