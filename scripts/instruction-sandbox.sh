#!/usr/bin/env bash
# Build a self-contained sandbox of a candidate (committed, not yet landed)
# instruction revision, so edited instructions and hooks can be exercised
# without touching the real agent config root, personal settings, editor links
# or the canonical checkout.
#
# Layout of <root>:
#   core/            private clone of the candidate revision (detached, no remote)
#   home/            fake $HOME; its .claude-agent and .cursor are composed by the
#                    candidate's own, unmodified setup-symlinks.sh
#   sandbox.env      ISB_SOURCE / ISB_CORE_SHA for later stages
#
# Usage: instruction-sandbox.sh [--source <repo>] [--core-ref <ref>]
#                               [--root <dir>] [--dry-run] [--help]
#   --source    repository holding the candidate (default: toplevel of the cwd)
#   --core-ref  committed ref to sandbox (default: HEAD); never committed for you
#   --root      sandbox directory; must be absent or empty
#               (default: a fresh mktemp dir under /tmp)
#   --dry-run   validate and print the plan; write nothing (also CLAUDE_DRY_RUN=1)
# The final stdout line is the sandbox root.
#
# Isolation is by construction: a fake HOME plus a private clone redirect every
# path the install chain resolves ($HOME, the agent root, the repo and its
# .git/config), so no install script is patched. The clone reads the source's
# objects through alternates and registers nothing in the source.
set -euo pipefail

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "instruction-sandbox: $*" >&2; exit 2; }

source_repo=""
core_ref="HEAD"
root=""
dry_run="${CLAUDE_DRY_RUN:-}"
[[ "$dry_run" == "0" ]] && dry_run=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)   [[ $# -ge 2 ]] || die "--source needs a value"; source_repo="$2"; shift 2 ;;
    --core-ref) [[ $# -ge 2 ]] || die "--core-ref needs a value"; core_ref="$2"; shift 2 ;;
    --root)     [[ $# -ge 2 ]] || die "--root needs a value"; root="$2"; shift 2 ;;
    --dry-run)  dry_run=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ -z "$source_repo" ]]; then
  source_repo="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || die "not inside a git repository; pass --source"
fi
source_real="$(readlink -f "$source_repo")"
[[ -d "$source_real" ]] || die "--source is not a directory: $source_repo"
git -C "$source_real" rev-parse --git-dir >/dev/null 2>&1 \
  || die "--source is not a git repository: $source_repo"

sha="$(git -C "$source_real" rev-parse --verify --quiet "${core_ref}^{commit}")" \
  || die "--core-ref does not resolve to a commit in $source_real: $core_ref"
common_dir="$(git -C "$source_real" rev-parse --path-format=absolute --git-common-dir)"

created_root=""
if [[ -z "$root" ]]; then
  root="$(mktemp -d /tmp/instruction-sandbox.XXXXXX)"
  created_root=1
fi
root_real="$(readlink -m "$root")"
canon="$(readlink -m "${CLAUDE_INSTRUCTIONS_CANON:-$HOME/claude-agent-instructions}")"
home_real="$(readlink -m "$HOME")"

inside() { [[ "$1" == "$2" || "$1" == "$2"/* ]]; }

[[ "$root_real" != "/" && "$root_real" != "$home_real" ]] \
  || die "refusing root $root_real"
for guarded in "$home_real/.claude-agent" "$home_real/.claude" "$home_real/.cursor" \
               "$source_real" "$canon"; do
  if inside "$root_real" "$guarded"; then
    die "refusing root $root_real: inside protected path $guarded"
  fi
done
if [[ -e "$root_real" ]]; then
  [[ -d "$root_real" && -z "$(ls -A "$root_real")" ]] \
    || die "root exists and is not an empty directory: $root_real"
fi

if [[ -n "$dry_run" ]]; then
  echo "dry-run: would clone $common_dir at $sha into $root_real/core"
  echo "dry-run: would compose $root_real/home with setup-symlinks.sh"
  echo "$root_real"
  exit 0
fi

mkdir -p "$root_real"
built=""
cleanup_on_failure() {
  if [[ -z "$built" && -n "$created_root" && -n "$root_real" && "$root_real" == /tmp/instruction-sandbox.* ]]; then
    rm -rf -- "$root_real"
  fi
}
# EXIT, not ERR: `die` calls `exit 2` directly, which an ERR trap never sees.
trap cleanup_on_failure EXIT

# Clone from the common git dir so a linked-worktree --source works the same as
# a plain checkout; the SHA was already resolved in --source.
git clone --quiet --shared --no-checkout "$common_dir" "$root_real/core"
git -C "$root_real/core" checkout --quiet --detach "$sha"
git -C "$root_real/core" remote remove origin

mkdir -p "$root_real/home/.claude-agent"
ln -sfn "$root_real/core" "$root_real/home/claude-agent-instructions"

# Caller-set overrides of these must not reach the install chain.
scrub=(CLAUDE_CODE_SESSION_ID AGENT_LINEAGE_IDS CLAUDE_CODE_MESSAGING_SOCKET
       CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_POLICY_LEDGER CLAUDE_PROJECT_PLUGIN_DIR
       CLAUDE_DIFFICULTY_PLUGIN_DIR CLAUDE_AUTH_PROFILE_DIR
       CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR CLAUDE_INSTRUCTIONS_CANON)
unset_args=()
for v in "${scrub[@]}"; do unset_args+=(-u "$v"); done
while IFS= read -r v; do
  [[ "$v" == AGENTCTL_* ]] && unset_args+=(-u "$v")
done < <(compgen -e)

env "${unset_args[@]}" \
  HOME="$root_real/home" \
  CLAUDE_AGENT_HOME="$root_real/home/.claude-agent" \
  CLAUDE_CONFIG_DIR="$root_real/home/.claude-agent" \
  CLAUDE_INSTRUCTIONS_REPO="$root_real/core" \
  "$root_real/core/scripts/setup-symlinks.sh" >"$root_real/setup.log" 2>&1 \
  || { tail -n 10 "$root_real/setup.log" >&2; die "setup-symlinks.sh failed in the sandbox"; }

{
  printf 'ISB_SOURCE=%s\n' "$source_real"
  printf 'ISB_CORE_SHA=%s\n' "$sha"
} > "$root_real/sandbox.env"

built=1
echo "$root_real"
