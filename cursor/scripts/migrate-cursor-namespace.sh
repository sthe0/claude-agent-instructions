#!/usr/bin/env bash
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"

discover_deepagent_project_roots() {
  local candidate
  for candidate in "$HOME/arcadia/robot/deepagent" "$HOME"/arcadia_*/robot/deepagent; do
    [[ -d "$candidate" ]] && printf '%s\n' "$candidate"
  done
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--all-deepagent-mounts] [<project_root> ...]

  Global: always runs scripts/setup-symlinks.sh (includes ~/.cursor/* links).

  Project: for each <project_root>, runs .claude/scripts/setup-local.sh when present.
  --all-deepagent-mounts  also runs setup-local on every ~/arcadia/robot/deepagent
                          and ~/arcadia_*/robot/deepagent found on this machine.

Typical deepagent-only migration:
  $(basename "$0") --all-deepagent-mounts
EOF
}

ALL_DEEPAGENT=0
PROJECT_ROOTS=()

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --all-deepagent-mounts)
      ALL_DEEPAGENT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      PROJECT_ROOTS+=("$1")
      shift
      ;;
  esac
done

if [[ "$ALL_DEEPAGENT" -eq 1 ]]; then
  while IFS= read -r discovered; do
    [[ -z "$discovered" ]] && continue
    PROJECT_ROOTS+=("$discovered")
  done < <(discover_deepagent_project_roots)
fi

echo "== Global links =="
"$REPO/scripts/setup-symlinks.sh"

if [[ "${#PROJECT_ROOTS[@]}" -eq 0 ]]; then
  cat <<'EOF'
No project roots were passed (and --all-deepagent-mounts found none).

Run setup-local on each mount that contains robot/deepagent, for example:
  cd ~/arcadia/robot/deepagent && .claude/scripts/setup-local.sh
  cd ~/arcadia_<ticket>/robot/deepagent && .claude/scripts/setup-local.sh

Or pass roots explicitly / use --all-deepagent-mounts.
EOF
  exit 0
fi

echo "== Project links =="
# Deduplicate roots (bash 4+ associative array or sort -u)
mapfile -t PROJECT_ROOTS < <(printf '%s\n' "${PROJECT_ROOTS[@]}" | sort -u)

for project_root in "${PROJECT_ROOTS[@]}"; do
  setup_local="$project_root/.claude/scripts/setup-local.sh"
  if [[ -x "$setup_local" ]]; then
    # Invoke via the real storage path, not the mount's .claude symlink:
    # setup-local.sh derives STORAGE from "$(dirname "$0")/.." with a logical
    # pwd, so calling it through .claude/scripts/ resolves STORAGE back to the
    # .claude symlink and makes step 1 relink .claude onto itself (ELOOP).
    real_setup="$(readlink -f "$setup_local")"
    echo "project: $project_root (setup-local: $real_setup)"
    (cd "$project_root" && "$real_setup")
  else
    echo "skip: $project_root (missing executable $setup_local)"
    if [[ -x "$REPO/cursor/scripts/link-project-cursor-agents.sh" ]]; then
      echo "  fallback: link-project-cursor-agents.sh only"
      "$REPO/cursor/scripts/link-project-cursor-agents.sh" "$project_root"
    fi
  fi
done
