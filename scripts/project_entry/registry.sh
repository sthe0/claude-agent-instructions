#!/usr/bin/env bash
# Backend registry for the modular task-entry subsystem.
#
# Resolves a backend NAME -> the shell file that implements it, searching the
# built-in directory first and a machine-local plugin directory second. The CLI
# (enter-task.sh) then `source`s the resolved file and calls the contract
# functions below. This file is org-neutral: it knows nothing about any specific
# workspace (git, …) or tracker (github, …) — only how to find one by name.
#
# ── Provider contract ───────────────────────────────────────────────────────
# A WORKSPACE backend file (backends/<name>.sh) defines:
#   backend_detect                       -> exit 0 if usable on this machine
#   backend_ensure_workspace <name> <branch>
#                                        -> ensure an isolated working copy exists
#                                           (create if absent, reuse if present);
#                                           print the project_dir as the FINAL
#                                           stdout line.
#   backend_compose <project_dir>        -> wire `.claude` into project_dir
#                                           (no-op when the working copy already
#                                           carries .claude, as a git worktree does).
#
# Optional init-only extensions (declared only by backends that support --init):
#   backend_init_workspace <name> <target>
#                                        -> create a fresh repo at <target>;
#                                           print <target> as the final stdout line;
#                                           honour CLAUDE_DRY_RUN (log+print, no mkdir/vcs).
#   backend_seal_workspace <dir> <msg>   -> stage all + initial commit in the new repo;
#                                           no-op under CLAUDE_DRY_RUN.
#
# A TRACKER backend file (trackers/<name>.sh) defines:
#   tracker_resolve <key>                -> print 'key<TAB>slug' for an existing task
#   tracker_create  <title>             -> print 'key<TAB>slug' for a newly created task;
#                                          target queue from $CLAUDE_TRACKER_QUEUE (set by
#                                          enter-task.sh from the resolved project record)
#
# Optional read verb (declared only by trackers that support it):
#   tracker_read <key>                  -> print a flat, backend-agnostic rendering
#                                          of the task (title / status / author /
#                                          description / comments) on stdout;
#                                          read-only, no side effects. The caller
#                                          probes presence with `declare -F tracker_read`
#                                          before calling and never invokes it on a
#                                          backend that omits it. Exactly ONE degrade
#                                          class: exit 0 = rendered ok, ANY nonzero =
#                                          unavailable, with the human-readable reason
#                                          on stderr. An implementation MUST normalize
#                                          every downstream tool's exit code into that
#                                          single class rather than letting it leak
#                                          through as tracker_read's own status.
#   tracker_plan_marker <key>           -> print every posted comment's body
#                                          on <key>, in CHRONOLOGICAL
#                                          (creation) order, oldest first —
#                                          LOAD-BEARING: verify-ticket-plan-
#                                          sync.py's extract_marker() picks
#                                          the LAST marker match in the
#                                          concatenated text as authoritative,
#                                          so a backend implementing this verb
#                                          MUST preserve that order (matching
#                                          `gh issue view --json comments`'s
#                                          own default) — a backend that
#                                          reorders or reverses comments
#                                          before printing them would
#                                          silently make a stale marker win
#                                          over the current one. Newline-
#                                          joined, no header decoration;
#                                          performs NO marker-parsing itself
#                                          — that stays exclusively in
#                                          verify-ticket-plan-sync.py. Exactly
#                                          ONE degrade class, same shape as
#                                          tracker_read: exit 0 = rendered ok
#                                          INCLUDING zero comments (empty
#                                          stdout is success, not failure —
#                                          the absence is reported by
#                                          verify-ticket-plan-sync.py's own
#                                          NO-PLAN status instead), ANY
#                                          nonzero = unavailable, reason on
#                                          stderr. Intended consumer:
#                                          `<backend-call> tracker_plan_marker
#                                          <key> | python3
#                                          verify-ticket-plan-sync.py --plan
#                                          <toml> --comment-file -`.
#
# Optional write verbs (declared only by trackers that support them):
#   tracker_comment <key> <markdown-path>
#                                        -> post <markdown-path>'s content as a NEW
#                                          comment on the task (never edits the
#                                          description). Exactly ONE degrade class,
#                                          same shape as tracker_read: exit 0 = posted,
#                                          ANY nonzero = not posted, reason on stderr.
#   tracker_publish_plan <key> <toml-path> <markdown-path>
#                                        -> publish an approved plan snapshot: an
#                                          implementation-defined durable artifact
#                                          holding <toml-path> BYTE-IDENTICAL to the
#                                          input file (never re-serialized), followed
#                                          by a NEW comment on the task containing
#                                          <markdown-path>'s content plus a reference
#                                          to that artifact. Exactly ONE degrade class:
#                                          exit 0 = fully published, ANY nonzero = not
#                                          fully published. If the artifact step
#                                          succeeds but the comment step fails, the
#                                          artifact's reference (e.g. its URL) MUST
#                                          still appear in the stderr reason — a
#                                          half-published state is never left
#                                          unreported.
#
# Both write verbs are probed the same way as tracker_read (`declare -F <verb>`)
# and follow the same confirmation-gating as tracker_create: CLAUDE_DRY_RUN performs
# zero external effects, and CLAUDE_LAUNCH_ASSUME_YES=1 is required to proceed
# without a prompt.
#
# THE ORG-NEUTRAL GUARD IS PART OF THE VERB, not of the repo lint: before ANY
# external call, tracker_comment and tracker_publish_plan MUST run
# scripts/check-org-neutral.py over every text input they are about to publish
# (tracker_comment: the markdown; tracker_publish_plan: BOTH the toml and the
# markdown) and refuse (nonzero, reason on stderr, zero external calls made) on
# any marker hit. The one escape hatch is CLAUDE_PUBLISH_ALLOW_INTERNAL=1, which
# only a non-Core plugin publishing to an org-internal venue may set — Core's own
# github backend never sets it itself, since GitHub Issues is a PUBLIC venue.
#
# Every external tool a backend calls MUST go through a *_BIN env seam (GIT_BIN,
# GH_BIN, …) so the hermetic tests can stub it. Backends must honour CLAUDE_DRY_RUN
# (exported by the CLI): when set to a non-empty value they perform zero external
# side effects.
#
# ── Resolution order ────────────────────────────────────────────────────────
#   1. built-in:      scripts/project_entry/backends/<name>.sh  (resp. trackers/)
#   2. machine-local: ${CLAUDE_PROJECT_PLUGIN_DIR:-<agent-home>/project-entry-plugins}/backends/<name>.sh
#      (<agent-home> is $CLAUDE_AGENT_HOME when isolated, else the legacy
#      ~/.claude/project-entry-plugins — see _plugin_dir below)
# A built-in name is stable (checked first); a plugin ADDS a new name. A fresh
# plugin name not shipped in Core is discovered from the machine-local dir — the
# coupling-free way a specialized backend (arc, …) attaches without editing Core.

# Directory of this registry file -> the built-in backends/trackers live beside it.
_REGISTRY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Machine-local plugin root (overridable for tests / per-machine installs).
# Read-time resolution (same order as lib/config_root.py's agent_home()): an
# explicit override wins; else prefer an existing isolated root; else fall
# back to the legacy ~/.claude location for a not-yet-migrated machine; else
# default to the isolated root (so a fresh machine's first lookup still names
# the isolated path, not the dead one).
_plugin_dir() {
  if [[ -n "${CLAUDE_PROJECT_PLUGIN_DIR:-}" ]]; then
    printf '%s' "$CLAUDE_PROJECT_PLUGIN_DIR"
    return
  fi
  local isolated="${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}/project-entry-plugins"
  local legacy="$HOME/.claude/project-entry-plugins"
  if [[ -d "$isolated" ]]; then
    printf '%s' "$isolated"
  elif [[ -d "$legacy" ]]; then
    printf '%s' "$legacy"
  else
    printf '%s' "$isolated"
  fi
}

# _registry_resolve <kind> <name>  where <kind> is 'backends' or 'trackers'.
# Prints the resolved file path on stdout and returns 0, or prints an error to
# stderr and returns 1.
_registry_resolve() {
  local kind="$1" name="$2" builtin plugin
  builtin="$_REGISTRY_DIR/$kind/$name.sh"
  if [[ -f "$builtin" ]]; then
    printf '%s\n' "$builtin"
    return 0
  fi
  plugin="$(_plugin_dir)/$kind/$name.sh"
  if [[ -f "$plugin" ]]; then
    printf '%s\n' "$plugin"
    return 0
  fi
  printf 'registry: no %s backend named %q (looked in %s and %s)\n' \
    "$kind" "$name" "$builtin" "$plugin" >&2
  return 1
}

registry_resolve_workspace() { _registry_resolve backends "$1"; }
registry_resolve_tracker()   { _registry_resolve trackers "$1"; }
