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
#   project/         only with --project-mount: the project's .claude/ and CLAUDE.md,
#                    composed by a project-supplied composer (see below)
#   sandbox.env      ISB_SOURCE / ISB_CORE_SHA, and with --project-mount also
#                    ISB_PROJECT_MOUNT / ISB_COMPOSER / ISB_PROTECTED
#
# Usage: instruction-sandbox.sh [--source <repo>] [--core-ref <ref>]
#                               [--root <dir>] [--project-mount <dir>]
#                               [--project-composer <name>] [--dry-run] [--help]
#   --source    repository holding the candidate (default: toplevel of the cwd)
#   --core-ref  committed ref to sandbox (default: HEAD); never committed for you
#   --root      sandbox directory; must be absent or empty
#               (default: a fresh mktemp dir under /tmp)
#   --project-mount     a candidate mount of a project to compose into <root>/project
#   --project-composer  composer name; default: the one composer that accepts the mount
#   --dry-run   validate and print the plan; write nothing (also CLAUDE_DRY_RUN=1)
# The final stdout line is the sandbox root.
#
# Project composers: Core ships none. A project installs <name>.sh into
#   ${CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR:-<agent home>/instruction-sandbox-plugins}/composers/
# (resolved in the caller's real environment). Each file defines five functions:
#   composer_detect <mount>     exit 0 when this composer handles the mount
#   composer_protected_paths    print paths the mount must never be or sit under,
#                               and composed links must never resolve under
#   composer_validate <mount>   extra refusals: message on stderr, nonzero = refuse
#   composer_compose <mount>    build the project under $ISB_PROJECT_ROOT (and any
#                               other sandbox-owned dir under $ISB_ROOT); runs with
#                               the sandbox HOME and scrubbed env, ISB_REAL_HOME
#                               names the caller's real home
#   composer_snapshot           print the project's own canon snapshot lines
# Every refusal happens before <root> is created; the mount is never written.
#
# Isolation is by construction: a fake HOME plus a private clone redirect every
# path the install chain resolves ($HOME, the agent root, the repo and its
# .git/config), so no install script is patched. The clone reads the source's
# objects through alternates and registers nothing in the source.
set -euo pipefail

usage() { sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "instruction-sandbox: $*" >&2; exit 2; }

source_repo=""
core_ref="HEAD"
root=""
project_mount=""
composer_name=""
dry_run="${CLAUDE_DRY_RUN:-}"
[[ "$dry_run" == "0" ]] && dry_run=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)   [[ $# -ge 2 ]] || die "--source needs a value"; source_repo="$2"; shift 2 ;;
    --core-ref) [[ $# -ge 2 ]] || die "--core-ref needs a value"; core_ref="$2"; shift 2 ;;
    --root)     [[ $# -ge 2 ]] || die "--root needs a value"; root="$2"; shift 2 ;;
    --project-mount)    [[ $# -ge 2 ]] || die "--project-mount needs a value"; project_mount="$2"; shift 2 ;;
    --project-composer) [[ $# -ge 2 ]] || die "--project-composer needs a value"; composer_name="$2"; shift 2 ;;
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

inside() { [[ "$1" == "$2" || "$1" == "$2"/* ]]; }

composer_fns=(composer_detect composer_protected_paths composer_validate composer_compose composer_snapshot)
# Every call sources the plugin in its own bash, never in this shell.
composer_run() { local plugin="$1"; shift; bash -c 'source "$1" || exit 1; shift; "$@"' _ "$plugin" "$@"; }
composer_complete() {
  bash -c 'source "$1" || exit 1; shift; for f in "$@"; do declare -F "$f" >/dev/null || exit 1; done' \
    _ "$1" "${composer_fns[@]}" >/dev/null 2>&1
}

composer=""
mount_real=""
protected=()
if [[ -n "$project_mount" ]]; then
  plugin_dir="${CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR:-${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}/instruction-sandbox-plugins}"
  mount_real="$(readlink -f "$project_mount")" || die "--project-mount does not resolve: $project_mount"
  [[ -d "$mount_real" ]] || die "--project-mount is not a directory: $project_mount"
  if [[ -n "$composer_name" ]]; then
    [[ "$composer_name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "refusing composer name: $composer_name"
    composer="$plugin_dir/composers/$composer_name.sh"
    [[ -f "$composer" ]] || die "refusing --project-mount: no composer named $composer_name in $plugin_dir/composers"
    composer_complete "$composer" || die "refusing --project-mount: $composer does not define the five-function contract"
  else
    candidates=()
    shopt -s nullglob
    for f in "$plugin_dir"/composers/*.sh; do
      if ! composer_complete "$f"; then
        echo "instruction-sandbox: warning: skipping $f: does not define the five-function composer contract" >&2
        continue
      fi
      if composer_run "$f" composer_detect "$mount_real" >/dev/null 2>&1; then
        candidates+=("$f")
      fi
    done
    shopt -u nullglob
    case "${#candidates[@]}" in
      0) die "refusing --project-mount: no composer in $plugin_dir/composers accepts $mount_real (Core ships none; the project installs its own)" ;;
      1) composer="${candidates[0]}" ;;
      *) die "refusing --project-mount: several composers accept $mount_real: ${candidates[*]}" ;;
    esac
  fi
  protected_out="$(composer_run "$composer" composer_protected_paths)" \
    || die "refusing --project-mount: composer_protected_paths failed"
  while IFS= read -r p; do
    [[ -n "$p" ]] && protected+=("$(readlink -m "$p")")
  done <<< "$protected_out"
  for p in ${protected[@]+"${protected[@]}"}; do
    inside "$mount_real" "$p" && die "refusing --project-mount $mount_real: inside protected path $p"
  done
  composer_run "$composer" composer_validate "$mount_real" >/dev/null \
    || die "refusing --project-mount $mount_real: composer validation failed"
  if [[ -n "$root" ]]; then
    if inside "$mount_real" "$(readlink -m "$root")"; then
      die "refusing --project-mount $mount_real: inside the sandbox root"
    fi
  fi
fi

created_root=""
if [[ -z "$root" ]]; then
  if [[ -n "$dry_run" ]]; then
    # -u: report a name only, no write, no cleanup obligation for the dry-run path.
    root="$(mktemp -u /tmp/instruction-sandbox.XXXXXX)"
  else
    root="$(mktemp -d /tmp/instruction-sandbox.XXXXXX)"
    created_root=1
  fi
fi
root_real="$(readlink -m "$root")"
canon="$(readlink -m "${CLAUDE_INSTRUCTIONS_CANON:-$HOME/claude-agent-instructions}")"
home_real="$(readlink -m "$HOME")"

[[ "$root_real" != "/" && "$root_real" != "$home_real" ]] \
  || die "refusing root $root_real"
for guarded in "$home_real/.claude-agent" "$home_real/.claude" "$home_real/.cursor" \
               "$source_real" "$canon" \
               ${mount_real:+"$mount_real"} ${protected[@]+"${protected[@]}"}; do
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
  [[ -z "$composer" ]] || echo "dry-run: would compose $mount_real into $root_real/project with $composer"
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

if [[ -n "$composer" ]]; then
  mkdir -p "$root_real/project"
  env "${unset_args[@]}" \
    HOME="$root_real/home" \
    CLAUDE_AGENT_HOME="$root_real/home/.claude-agent" \
    CLAUDE_CONFIG_DIR="$root_real/home/.claude-agent" \
    CLAUDE_INSTRUCTIONS_REPO="$root_real/core" \
    ISB_ROOT="$root_real" \
    ISB_PROJECT_ROOT="$root_real/project" \
    ISB_REAL_HOME="$home_real" \
    bash -c 'source "$1" || exit 1; composer_compose "$2"' _ "$composer" "$mount_real" \
    >"$root_real/compose.log" 2>&1 \
    || { tail -n 10 "$root_real/compose.log" >&2; die "composer_compose failed in the sandbox"; }
  [[ -d "$root_real/project/.claude" && ! -L "$root_real/project/.claude" ]] \
    || die "composer left no real directory at project/.claude"
  [[ -f "$root_real/project/CLAUDE.md" && ! -L "$root_real/project/CLAUDE.md" ]] \
    || die "composer left no regular file at project/CLAUDE.md"
fi

{
  printf 'ISB_SOURCE=%s\n' "$source_real"
  printf 'ISB_CORE_SHA=%s\n' "$sha"
  if [[ -n "$composer" ]]; then
    printf 'ISB_PROJECT_MOUNT=%s\n' "$mount_real"
    printf 'ISB_COMPOSER=%s\n' "$composer"
    printf 'ISB_PROTECTED=%s\n' "$(IFS=:; echo "${protected[*]-}")"
  fi
} > "$root_real/sandbox.env"

built=1
echo "$root_real"
