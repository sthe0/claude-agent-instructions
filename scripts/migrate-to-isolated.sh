#!/usr/bin/env bash
# migrate-to-isolated.sh — relocate system-owned entries from ~/.claude (the old
# in-place install location) to the isolated agent config root ($CLAUDE_AGENT_HOME,
# default ~/.claude-agent).
#
# An old install wrote all system symlinks and managed files directly into ~/.claude,
# which means the agent system could clobber or intermix with the user's personal
# Claude config.  This script detects and moves only the system-owned pieces to the
# isolated root, leaving personal files in ~/.claude untouched.
#
# System-owned detection:
#   - a symlink in ~/.claude whose resolved target is under the
#     claude-agent-instructions repo (CLAUDE.md, config.md, memory-global, and the
#     per-file agents/*, per-dir skills/* symlinks), and
#   - the purely system-managed plain file agent-identity.local.
#
# NOT moved (deliberately):
#   - settings.json — an ADDITIVE MERGE of the user's personal settings + system
#     settings (apply-settings.sh). Moving it would strip the user's personal
#     settings from their ~/.claude. The isolated root gets its own system-only
#     settings.json from setup-symlinks.sh, so migration never needs to move it.
#   - any personal content: projects/, sessions/, backups/, .claude.json, user files.
#
# Usage:
#   scripts/migrate-to-isolated.sh [--apply]
#
#   (no flags) / --dry-run   Preview only — list what WOULD move; make no changes.
#   --apply                  Actually back up and move the system-owned entries.
#
# Preview is the DEFAULT: nothing is moved unless --apply is given explicitly.
# Safe to rerun (idempotent).  Backs up relocated entries before moving.
# Guards every interpolated path — can never collapse to $HOME or the root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
source "$REPO/scripts/lib/config-root.sh"

APPLY=0
for _arg in "$@"; do
  case "$_arg" in
    --apply)   APPLY=1 ;;
    --dry-run) APPLY=0 ;;  # explicit preview; same as the default
    -h|--help)
      sed -n '2,32p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "error: unknown argument '$_arg' (use --apply or --dry-run)" >&2
      exit 2 ;;
  esac
done

SRC="$HOME/.claude"
DEST="$CLAUDE_AGENT_HOME"

# Paranoid guards: non-empty + not dangerous roots
[[ -n "${SRC:-}" ]]  || { echo "error: SRC is empty" >&2; exit 1; }
[[ -n "${DEST:-}" ]] || { echo "error: DEST (CLAUDE_AGENT_HOME) is empty" >&2; exit 1; }
[[ "$SRC"  != "$HOME" ]] || { echo "refuse: SRC collapsed to \$HOME" >&2; exit 1; }
[[ "$SRC"  != "/" ]]     || { echo "refuse: SRC is /" >&2; exit 1; }
[[ "$DEST" != "$HOME" ]] || { echo "refuse: DEST collapsed to \$HOME" >&2; exit 1; }
[[ "$DEST" != "/" ]]     || { echo "refuse: DEST is /" >&2; exit 1; }
[[ "$DEST" != "$SRC" ]]  || { echo "refuse: DEST and SRC are the same path" >&2; exit 1; }

if [[ ! -d "$SRC" ]]; then
  echo "No $SRC directory — nothing to migrate."
  exit 0
fi

_realpath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }
REPO_REAL="$(_realpath "$REPO")"
[[ -n "$REPO_REAL" ]] || { echo "error: could not resolve REPO real path" >&2; exit 1; }

# Returns 0 if the path is a symlink whose resolved target is under REPO_REAL
_is_repo_symlink() {
  local _p="$1"
  [[ -n "$_p" ]] || return 1
  [[ -L "$_p" ]] || return 1
  local _tgt
  _tgt="$(_realpath "$_p" 2>/dev/null)" || return 1
  [[ -n "$_tgt" && "$_tgt" == "$REPO_REAL"* ]]
}

# ── Collect system-owned entries ────────────────────────────────────────────────

TOP_SYMLINKS=()
AGENT_SYMLINKS=()
SKILL_SYMLINKS=()
PLAIN_FILES=()

for _name in CLAUDE.md config.md memory-global; do
  _p="$SRC/$_name"
  _is_repo_symlink "$_p" && TOP_SYMLINKS+=("$_name")
done

for _dir_name in agents skills; do
  _dir="$SRC/$_dir_name"
  if [[ -d "$_dir" && ! -L "$_dir" ]]; then
    while IFS= read -r -d '' _entry; do
      [[ -n "$_entry" ]] || continue
      _base="$(basename "$_entry")"
      [[ -n "$_base" ]] || continue
      if _is_repo_symlink "$_entry"; then
        if [[ "$_dir_name" == "agents" ]]; then
          AGENT_SYMLINKS+=("$_base")
        else
          SKILL_SYMLINKS+=("$_base")
        fi
      fi
    done < <(find "$_dir" -maxdepth 1 -mindepth 1 -print0 2>/dev/null)
  fi
done

# agent-identity.local is purely system-managed (written by configure-identity.sh).
# settings.json is DELIBERATELY excluded — it is a personal+system merge (see header).
for _name in agent-identity.local; do
  _p="$SRC/$_name"
  [[ -f "$_p" && ! -L "$_p" ]] && PLAIN_FILES+=("$_name")
done

# ── Early exit if nothing found ─────────────────────────────────────────────────

_count_top=${#TOP_SYMLINKS[@]}
_count_agent=${#AGENT_SYMLINKS[@]}
_count_skill=${#SKILL_SYMLINKS[@]}
_count_plain=${#PLAIN_FILES[@]}
_total=$(( _count_top + _count_agent + _count_skill + _count_plain ))

if [[ "$_total" -eq 0 ]]; then
  echo "Nothing system-owned found in $SRC — already migrated or never in-place."
  exit 0
fi

# ── Report ──────────────────────────────────────────────────────────────────────

echo "System-owned entries found in $SRC (to be moved to $DEST):"
for _name in "${TOP_SYMLINKS[@]+"${TOP_SYMLINKS[@]}"}"; do
  printf '  symlink  %s/%s\n' "$SRC" "$_name"
done
for _name in "${AGENT_SYMLINKS[@]+"${AGENT_SYMLINKS[@]}"}"; do
  printf '  symlink  %s/agents/%s\n' "$SRC" "$_name"
done
for _name in "${SKILL_SYMLINKS[@]+"${SKILL_SYMLINKS[@]}"}"; do
  printf '  symlink  %s/skills/%s\n' "$SRC" "$_name"
done
for _name in "${PLAIN_FILES[@]+"${PLAIN_FILES[@]}"}"; do
  printf '  file     %s/%s\n' "$SRC" "$_name"
done
echo

# Advisory: a legacy merged settings.json is LEFT in place (it now holds the user's
# personal settings). Surface it so the user knows it was intentionally not moved.
if [[ -f "$SRC/settings.json" && ! -L "$SRC/settings.json" ]]; then
  echo "note: $SRC/settings.json is left in place (personal+system merge — never moved)."
  if [[ -f "$SRC/settings.json.bak" ]]; then
    echo "      a pre-system original may exist at $SRC/settings.json.bak (not restored)."
  fi
  echo
fi

if [[ "$APPLY" -eq 0 ]]; then
  printf 'Destination: %s\n' "$DEST"
  echo "(preview — no changes made; rerun with --apply to move)"
  exit 0
fi

# ── Backup ──────────────────────────────────────────────────────────────────────

_TS="$(date +%Y%m%d%H%M%S)"
BAK="$SRC.premigrate.bak.$_TS"
[[ -n "$BAK" ]]     || { echo "error: BAK path is empty" >&2; exit 1; }
[[ "$BAK" != "$HOME" ]] || { echo "error: BAK collapsed to \$HOME" >&2; exit 1; }
[[ "$BAK" != "/" ]]     || { echo "error: BAK is /" >&2; exit 1; }

echo "Backing up entries to $BAK ..."
mkdir -p "$BAK"

for _name in "${TOP_SYMLINKS[@]+"${TOP_SYMLINKS[@]}"}"; do
  [[ -n "$_name" ]] || continue
  cp -a "$SRC/$_name" "$BAK/$_name" 2>/dev/null || true
done
if [[ "$_count_agent" -gt 0 ]]; then
  mkdir -p "$BAK/agents"
  for _name in "${AGENT_SYMLINKS[@]+"${AGENT_SYMLINKS[@]}"}"; do
    [[ -n "$_name" ]] || continue
    cp -a "$SRC/agents/$_name" "$BAK/agents/$_name" 2>/dev/null || true
  done
fi
if [[ "$_count_skill" -gt 0 ]]; then
  mkdir -p "$BAK/skills"
  for _name in "${SKILL_SYMLINKS[@]+"${SKILL_SYMLINKS[@]}"}"; do
    [[ -n "$_name" ]] || continue
    cp -a "$SRC/skills/$_name" "$BAK/skills/$_name" 2>/dev/null || true
  done
fi
for _name in "${PLAIN_FILES[@]+"${PLAIN_FILES[@]}"}"; do
  [[ -n "$_name" ]] || continue
  cp -a "$SRC/$_name" "$BAK/$_name" 2>/dev/null || true
done

# ── Move ────────────────────────────────────────────────────────────────────────

mkdir -p "$DEST"
MOVED=0

# Move a single entry from a source to a dest path.
# If dest already exists (previous run placed it there), skip the move and
# remove the stale source entry — idempotent.
_move_entry() {
  local _s="$1" _d="$2"
  [[ -n "$_s" ]] || return 0
  [[ -n "$_d" ]] || return 0
  # Protect against dangerous path collapses
  [[ "$_s" != "$HOME" && "$_s" != "/" ]] \
    || { printf '  guard: refusing dangerous src %s\n' "$_s" >&2; return 1; }
  [[ "$_d" != "$HOME" && "$_d" != "/" ]] \
    || { printf '  guard: refusing dangerous dest %s\n' "$_d" >&2; return 1; }

  if [[ -L "$_d" || -e "$_d" ]]; then
    printf '  skip (already in dest): %s\n' "$(basename "$_s")"
    rm -f "$_s"
  else
    mv "$_s" "$_d"
    printf '  moved: %s -> %s\n' "$_s" "$_d"
    MOVED=$(( MOVED + 1 ))
  fi
}

echo "Moving entries to $DEST ..."

for _name in "${TOP_SYMLINKS[@]+"${TOP_SYMLINKS[@]}"}"; do
  [[ -n "$_name" ]] || continue
  _move_entry "$SRC/$_name" "$DEST/$_name"
done

if [[ "$_count_agent" -gt 0 ]]; then
  mkdir -p "$DEST/agents"
  for _name in "${AGENT_SYMLINKS[@]+"${AGENT_SYMLINKS[@]}"}"; do
    [[ -n "$_name" ]] || continue
    _move_entry "$SRC/agents/$_name" "$DEST/agents/$_name"
  done
fi

if [[ "$_count_skill" -gt 0 ]]; then
  mkdir -p "$DEST/skills"
  for _name in "${SKILL_SYMLINKS[@]+"${SKILL_SYMLINKS[@]}"}"; do
    [[ -n "$_name" ]] || continue
    _move_entry "$SRC/skills/$_name" "$DEST/skills/$_name"
  done
fi

for _name in "${PLAIN_FILES[@]+"${PLAIN_FILES[@]}"}"; do
  [[ -n "$_name" ]] || continue
  _move_entry "$SRC/$_name" "$DEST/$_name"
done

echo
if [[ "$MOVED" -gt 0 ]]; then
  printf 'Migrated %d entries from %s to %s.\n' "$MOVED" "$SRC" "$DEST"
  printf 'Backup at: %s\n' "$BAK"
  echo "Next: run scripts/setup-symlinks.sh to complete the isolated setup."
else
  echo "All system entries already present in $DEST — nothing moved (idempotent)."
  printf '(Backup at %s is empty; safe to remove: rm -rf '"'"'%s'"'"')\n' "$BAK" "$BAK"
fi
