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
#
# When CLAUDE_WORKSPACE_ROOT is set (populated from a project record's workspace_path),
# it is used as the git repo root instead of git rev-parse --show-toplevel. This lets
# a worktree be created off a registered checkout even when the caller's cwd is outside
# that repo (e.g. entering from $HOME via --project <key>).
backend_ensure_workspace() {
  local name="$1" branch="$2" toplevel repo parent wt
  if [[ -n "${CLAUDE_WORKSPACE_ROOT:-}" ]]; then
    toplevel="$CLAUDE_WORKSPACE_ROOT"
  else
    toplevel="$("$GIT_BIN" rev-parse --show-toplevel)" || {
      printf 'git backend: not inside a git repository\n' >&2
      return 1
    }
  fi
  repo="$(basename "$toplevel")"
  parent="$(dirname "$toplevel")"
  wt="$parent/$repo-$name"

  # Reuse an existing worktree at this path; never recreate it.
  # Use -C "$toplevel" so git operates in the right repo regardless of cwd.
  if "$GIT_BIN" -C "$toplevel" worktree list --porcelain 2>/dev/null | grep -qxF "worktree $wt"; then
    printf 'git backend: reusing existing worktree %s\n' "$wt" >&2
  elif [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'git backend: [dry-run] would create worktree %s on branch %s\n' "$wt" "$branch" >&2
  else
    "$GIT_BIN" -C "$toplevel" worktree add "$wt" -b "$branch" >&2 || return 1
  fi

  printf '%s\n' "$wt"
}

# backend_compose <project_dir> -> no-op for git (worktree already carries .claude).
backend_compose() { :; }
