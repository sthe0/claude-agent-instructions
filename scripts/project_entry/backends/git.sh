#!/usr/bin/env bash
# Built-in git-worktree workspace backend — the org-neutral default.
#
# Implements the workspace half of the provider contract (see registry.sh):
#   backend_detect / backend_ensure_workspace / backend_compose
#
# An isolated working copy is a `git worktree` at <repo-parent>/<repo>-<name> on a
# new branch. A worktree already carries the repo's .claude tree, so compose is a
# no-op. Every git call goes through the GIT_BIN seam (default `git`) so the tests
# can stub it; under CLAUDE_DRY_RUN no mutating git command is run.

GIT_BIN="${GIT_BIN:-git}"

backend_detect() { return 0; }  # git is the universal default.

# backend_ensure_workspace <name> <branch> -> prints project_dir (final line).
backend_ensure_workspace() {
  local name="$1" branch="$2" toplevel repo parent wt
  toplevel="$("$GIT_BIN" rev-parse --show-toplevel)" || {
    printf 'git backend: not inside a git repository\n' >&2
    return 1
  }
  repo="$(basename "$toplevel")"
  parent="$(dirname "$toplevel")"
  wt="$parent/$repo-$name"

  # Reuse an existing worktree at this path; never recreate it.
  if "$GIT_BIN" worktree list --porcelain 2>/dev/null | grep -qxF "worktree $wt"; then
    printf 'git backend: reusing existing worktree %s\n' "$wt" >&2
  elif [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'git backend: [dry-run] would create worktree %s on branch %s\n' "$wt" "$branch" >&2
  else
    "$GIT_BIN" worktree add "$wt" -b "$branch" >&2 || return 1
  fi

  printf '%s\n' "$wt"
}

# backend_compose <project_dir> -> no-op for git (worktree already carries .claude).
backend_compose() { :; }
