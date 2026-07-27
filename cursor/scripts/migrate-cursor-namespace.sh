#!/usr/bin/env bash
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
_realpath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

_identity_file="${CLAUDE_AGENT_IDENTITY:-${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}/agent-identity.local}"
_id_get() { [[ -r "$_identity_file" ]] && sed -n "s/^$1=//p" "$_identity_file" | head -1; }

# Core ships no built-in roots — where a machine keeps its project checkouts is
# per-machine data, held in agent-identity.local's `cursor_project_roots=` key
# (comma/space-separated glob patterns).
discover_configured_project_roots() {
  local raw pattern candidate
  local -a patterns
  raw="$(_id_get cursor_project_roots || true)"
  [[ -z "$raw" ]] && return 0
  IFS=', ' read -r -a patterns <<<"$raw"
  for pattern in "${patterns[@]+"${patterns[@]}"}"; do
    [[ -z "$pattern" ]] && continue
    pattern="${pattern/#\~/$HOME}"
    # Unquoted on purpose: the configured entry is a glob to expand here.
    for candidate in $pattern; do
      [[ -d "$candidate" ]] && printf '%s\n' "$candidate"
    done
  done
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--all-configured-roots] [<project_root> ...]

  Global: always runs scripts/setup-symlinks.sh (includes ~/.cursor/* links).

  Project: for each <project_root>, runs .claude/scripts/setup-local.sh when present.
  --all-configured-roots  also runs setup-local on every project root matching
                          the machine's cursor_project_roots= identity key.

Typical whole-machine migration:
  $(basename "$0") --all-configured-roots
EOF
}

ALL_CONFIGURED=0
PROJECT_ROOTS=()

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --all-configured-roots)
      ALL_CONFIGURED=1
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

if [[ "$ALL_CONFIGURED" -eq 1 ]]; then
  while IFS= read -r discovered; do
    [[ -z "$discovered" ]] && continue
    PROJECT_ROOTS+=("$discovered")
  done < <(discover_configured_project_roots)
fi

echo "== Global links =="
"$REPO/scripts/setup-symlinks.sh"

if [[ "${#PROJECT_ROOTS[@]}" -eq 0 ]]; then
  cat <<'EOF'
No project roots were passed (and --all-configured-roots found none).

Run setup-local inside each project checkout, for example:
  cd <project_root> && .claude/scripts/setup-local.sh

Or pass roots explicitly, or list them as glob patterns in the
cursor_project_roots= key of the machine's agent-identity.local and rerun
with --all-configured-roots.
EOF
  exit 0
fi

echo "== Project links =="
# Deduplicate roots (bash 3.2-compatible while-read dedup)
_deduped=()
while IFS= read -r r; do _deduped+=("$r"); done < <(printf '%s\n' "${PROJECT_ROOTS[@]}" | sort -u)
PROJECT_ROOTS=("${_deduped[@]+"${_deduped[@]}"}")

for project_root in "${PROJECT_ROOTS[@]}"; do
  setup_local="$project_root/.claude/scripts/setup-local.sh"
  if [[ -x "$setup_local" ]]; then
    # Invoke via the real storage path, not the mount's .claude symlink:
    # setup-local.sh derives STORAGE from "$(dirname "$0")/.." with a logical
    # pwd, so calling it through .claude/scripts/ resolves STORAGE back to the
    # .claude symlink and makes step 1 relink .claude onto itself (ELOOP).
    real_setup="$(_realpath "$setup_local")"
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
