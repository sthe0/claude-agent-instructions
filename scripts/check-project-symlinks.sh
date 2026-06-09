#!/usr/bin/env bash
# check-project-symlinks.sh — SessionStart hook for verifying that Claude Code
# project symlinks are wired up correctly for the current cwd.
#
# Safe by design: only creates symlinks where nothing exists; never overwrites,
# deletes, or moves anything. Anything ambiguous is reported with a suggested
# fix for the user / agent to act on.
#
# Always exits 0. The hook must never break session startup.
#
# Emits hook JSON (https://json.schemastore.org/claude-code-settings.json):
#   - `hookSpecificOutput.additionalContext` carries the full report (visible
#     to the model so it can react).
#   - `systemMessage` is set only when there are issues, so the user sees a
#     terminal warning.
#
# Checks (run unconditionally where the relevant artefact exists; skipped if
# the artefact is absent so the hook is useful on any project):
#   1. If `<cwd>/.claude` is a symlink pointing into `…/arcadia_claude_local/…`,
#      verify that mount is mounted.
#   2. If `<cwd>/.claude` is a symlink, verify its target exists.
#   3. Wire `~/.claude/projects/<cwd-hash>/memory` → `<cwd>/.claude/agent-memory`
#      (when missing or an empty dir; otherwise report).
#   4. If `<cwd>/.arcignore` exists, require a literal `.claude` (or
#      `**.claude**`) line.
#   5. If `<storage>/scripts/link-skills.sh` exists and `<storage>/skills/` is
#      empty, run the script (idempotent regeneration only).
#   6. If `<storage>/rules/*.mdc` exists, mirror missing rules into
#      `<cwd>/.cursor/rules/` and the root `CLAUDE.md`.

set -u
# stdin may contain the hook payload JSON — drain harmlessly.
read -r _ 2>/dev/null || true

CWD="${PWD:-$(pwd)}"
ACTIONS=()
ISSUES=()
SUGGESTIONS=()

note_action()  { ACTIONS+=("$1"); }
note_issue()   { ISSUES+=("$1"); }
note_suggest() { SUGGESTIONS+=("$1"); }

claude_dir="$CWD/.claude"
storage=""
if [[ -L "$claude_dir" ]]; then
  storage="$(readlink -f "$claude_dir" 2>/dev/null || true)"
fi

# --- 1. arcadia_claude_local mount present (only relevant if storage lives there).
if [[ -n "$storage" && "$storage" == */arcadia_claude_local/junk/* ]]; then
  mount_root="${storage%%/junk/*}"
  if ! mountpoint -q "$mount_root" 2>/dev/null; then
    note_issue "storage mount $mount_root is not mounted"
    note_suggest "cd ~ && arc mount -m $mount_root --object-store \$HOME/.arc/store/.arc/objects --override-object-store --allow-other > /tmp/arc-mount-claude_local.log 2>&1 &"
  fi
fi

# --- 2. <cwd>/.claude symlink target reachable.
if [[ -L "$claude_dir" ]]; then
  if [[ ! -e "$storage" ]]; then
    note_issue ".claude symlink at $claude_dir points to non-existent $(readlink "$claude_dir" 2>/dev/null)"
    note_suggest "verify storage exists, or recreate the symlink: ln -sfn <correct-target> $claude_dir"
  fi
fi

# --- 3. Auto-memory symlink ~/.claude/projects/<hash>/memory → <cwd>/.claude/agent-memory.
agent_memory="$claude_dir/agent-memory"
if [[ -d "$agent_memory" ]]; then  # resolves through symlinks
  # Claude Code's per-cwd hash: every non-alphanumeric char → "-" (the harness
  # sanitizes "/" AND "_" — and any other non-alnum). Leading "/" already becomes
  # "-", so no extra prefix. /home/arcadia_X → -home-arcadia-X (underscore → dash).
  hash="$(printf '%s' "$CWD" | sed 's/[^A-Za-z0-9]/-/g')"
  projects_dir="$HOME/.claude/projects/$hash"
  mem_link="$projects_dir/memory"
  expected_target="$(readlink -f "$agent_memory" 2>/dev/null || echo "$agent_memory")"

  if [[ -L "$mem_link" ]]; then
    actual_target="$(readlink -f "$mem_link" 2>/dev/null || true)"
    if [[ "$actual_target" != "$expected_target" ]]; then
      note_issue "auto-memory link $mem_link → $(readlink "$mem_link") (expected → $agent_memory)"
      note_suggest "rm $mem_link && ln -s $agent_memory $mem_link  (after confirming current target is not needed)"
    fi
  elif [[ -d "$mem_link" ]]; then
    if [[ -z "$(ls -A "$mem_link" 2>/dev/null)" ]]; then
      if rmdir "$mem_link" 2>/dev/null && ln -s "$agent_memory" "$mem_link" 2>/dev/null; then
        note_action "linked empty auto-memory dir: $mem_link → $agent_memory"
      else
        note_issue "failed to symlink auto-memory $mem_link → $agent_memory"
      fi
    else
      note_issue "auto-memory $mem_link is a non-empty real directory; not linked to $agent_memory"
      note_suggest "$HOME/claude-agent-instructions/scripts/setup-project-memory.sh $CWD  (moves existing to .bak)"
    fi
  elif [[ -e "$mem_link" ]]; then
    note_issue "$mem_link exists but is not a directory or symlink"
  else
    mkdir -p "$projects_dir" 2>/dev/null
    if ln -s "$agent_memory" "$mem_link" 2>/dev/null; then
      note_action "linked auto-memory: $mem_link → $agent_memory"
    else
      note_issue "failed to create $mem_link → $agent_memory"
    fi
  fi
fi

# --- 4. .arcignore contains a .claude entry.
if [[ -f "$CWD/.arcignore" ]]; then
  if ! grep -qxE '\.claude/?' "$CWD/.arcignore" \
     && ! grep -qxE '\*\*\.claude\*\*' "$CWD/.arcignore" \
     && ! grep -qxE '/\.claude/?' "$CWD/.arcignore" \
     && ! grep -qxE '\*\*/\.claude/?' "$CWD/.arcignore"; then
    note_issue "$CWD/.arcignore does not contain a .claude (or **.claude**) line"
    note_suggest "review then add: echo '.claude' >> $CWD/.arcignore"
  fi
fi

# --- 5. Storage skills/ populated (idempotent regen via link-skills.sh).
if [[ -n "$storage" && -d "$storage" ]]; then
  link_script="$storage/scripts/link-skills.sh"
  skills_dir="$storage/skills"
  if [[ -x "$link_script" && -d "$skills_dir" ]]; then
    if [[ -z "$(ls -A "$skills_dir" 2>/dev/null)" ]]; then
      if bash "$link_script" >/dev/null 2>&1; then
        note_action "regenerated skill symlinks in $skills_dir"
      else
        note_issue "link-skills.sh failed; $skills_dir is empty"
        note_suggest "run manually: bash $link_script"
      fi
    fi
  fi
fi

# --- 6. Cursor rule symlinks under <cwd>/.cursor/rules/ and root CLAUDE.md.
rules_src="$claude_dir/rules"
if [[ -d "$rules_src" ]]; then
  cursor_rules_dir="$CWD/.cursor/rules"
  mkdir -p "$cursor_rules_dir" 2>/dev/null
  for src in "$rules_src"/*.mdc; do
    [[ -f "$src" ]] || continue
    name="$(basename "$src")"
    dst="$cursor_rules_dir/$name"
    if [[ -L "$dst" ]]; then
      actual="$(readlink -f "$dst" 2>/dev/null || true)"
      expected="$(readlink -f "$src" 2>/dev/null || true)"
      if [[ "$actual" != "$expected" ]]; then
        note_issue "cursor rule $dst → $(readlink "$dst") (expected → $src)"
        note_suggest "rm $dst && ln -s $src $dst"
      fi
    elif [[ -e "$dst" ]]; then
      note_issue "$dst exists as a regular file/dir; not a symlink to $src"
    else
      if ln -s "$src" "$dst" 2>/dev/null; then
        note_action "linked cursor rule: $dst → $src"
      fi
    fi
  done
fi
if [[ -f "$claude_dir/CLAUDE.md" ]]; then
  root_claude="$CWD/CLAUDE.md"
  if [[ -L "$root_claude" ]]; then
    actual="$(readlink -f "$root_claude" 2>/dev/null || true)"
    expected="$(readlink -f "$claude_dir/CLAUDE.md" 2>/dev/null || true)"
    if [[ "$actual" != "$expected" ]]; then
      note_issue "root CLAUDE.md → $(readlink "$root_claude") (expected → $claude_dir/CLAUDE.md)"
    fi
  elif [[ -e "$root_claude" ]]; then
    : # real CLAUDE.md present — leave alone; many projects own one
  else
    if ln -s "$claude_dir/CLAUDE.md" "$root_claude" 2>/dev/null; then
      note_action "linked root CLAUDE.md → $claude_dir/CLAUDE.md"
    fi
  fi
fi

# --- Compose report.
report=""
if (( ${#ACTIONS[@]} > 0 )); then
  report+=$'actions taken:\n'
  for a in "${ACTIONS[@]}"; do report+="  + $a"$'\n'; done
fi
if (( ${#ISSUES[@]} > 0 )); then
  [[ -n "$report" ]] && report+=$'\n'
  report+=$'issues found:\n'
  for i in "${ISSUES[@]}"; do report+="  ! $i"$'\n'; done
  if (( ${#SUGGESTIONS[@]} > 0 )); then
    report+=$'suggested fixes:\n'
    for s in "${SUGGESTIONS[@]}"; do report+="  → $s"$'\n'; done
  fi
fi

if (( ${#ISSUES[@]} > 0 )); then
  jq -nc \
    --arg ctx "claude-symlinks-check (cwd: $CWD)"$'\n'"$report" \
    --arg msg "claude-symlinks-check: ${#ISSUES[@]} issue(s) — see additionalContext" \
    '{
      systemMessage: $msg,
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: $ctx
      }
    }'
elif (( ${#ACTIONS[@]} > 0 )); then
  jq -nc \
    --arg ctx "claude-symlinks-check (cwd: $CWD)"$'\n'"$report" \
    '{
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: $ctx
      }
    }'
fi

exit 0
