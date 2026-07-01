#!/usr/bin/env bash
# Built-in arc-mount workspace backend — the internal-toolchain counterpart to
# backends/git.sh.
#
# Implements the SAME workspace half of the provider contract (see registry.sh):
#   backend_detect / backend_ensure_workspace / backend_compose
# so the router (session-isolate.sh) and detector stay backend-blind: they only
# resolve a name through registry.sh and call these three functions.
#
# arc has no worktree command; its equivalent is a second `arc mount` sharing the
# main mount's --object-store (the using-arc-multiple-mounts skill). An isolated
# working copy is therefore a new mount at <main-mount>_<name>. A mount already
# carries the repo's .claude tree, so compose is a no-op — exactly as for a git
# worktree. Every arc call goes through the ARC_BIN seam (default `arc`) so the
# tests can stub it; under CLAUDE_DRY_RUN no mount-creating command is run.

ARC_BIN="${ARC_BIN:-arc}"

# _arc_main_mount_fields -> prints "<main_mount><TAB><object_store>" for the
# mounted arc mount that is an ancestor of the anchor directory (CLAUDE_WORKSPACE_ROOT
# when set — a registered checkout resolved from --project — else $PWD). Emits
# nothing and returns non-zero when the anchor is not inside any mounted arc mount.
# Parsing is delegated to python3 (already a hard dependency of this subsystem);
# the deepest matching mount wins, so a nested mount is preferred over its parent.
_arc_main_mount_fields() {
  local anchor="${CLAUDE_WORKSPACE_ROOT:-$PWD}"
  "$ARC_BIN" mount --list --json 2>/dev/null | ARC_ANCHOR="$anchor" python3 -c '
import json, os, sys
anchor = os.path.realpath(os.environ["ARC_ANCHOR"])
try:
    mounts = json.load(sys.stdin)
except Exception:
    sys.exit(1)
best = None
for m in mounts if isinstance(mounts, list) else []:
    if m.get("status") != "mounted":
        continue
    mp = m.get("mount")
    if not mp:
        continue
    mp_real = os.path.realpath(mp)
    if anchor == mp_real or anchor.startswith(mp_real + os.sep):
        if best is None or len(mp_real) > len(best[0]):
            best = (mp_real, m.get("object-store") or "")
if best is None:
    sys.exit(1)
sys.stdout.write(best[0] + "\t" + best[1] + "\n")
'
}

# _arc_mount_exists <mount_path> -> exit 0 iff an arc mount is already registered
# at that path (any status), so an existing isolated mount is reused, never recreated.
_arc_mount_exists() {
  "$ARC_BIN" mount --list --json 2>/dev/null | MP="$1" python3 -c '
import json, os, sys
mp = os.path.realpath(os.environ["MP"])
try:
    mounts = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for m in mounts if isinstance(mounts, list) else []:
    if m.get("mount") and os.path.realpath(m["mount"]) == mp:
        sys.exit(0)
sys.exit(1)
'
}

# arc is usable on this machine only when the anchor is inside a mounted arc mount.
backend_detect() { _arc_main_mount_fields >/dev/null 2>&1; }

# backend_ensure_workspace <name> <branch> -> prints project_dir (final line).
#
# Creates (or reuses) an isolated mount at <main-mount>_<name> sharing the main
# mount's object-store, then checks out <branch> in it. Under CLAUDE_DRY_RUN no
# mount is created and no directory is made; the would-be mount path is still
# reported so the detector can be shown the isolated root.
backend_ensure_workspace() {
  local name="$1" branch="$2" fields main_mount object_store mount_path
  if ! fields="$(_arc_main_mount_fields)"; then
    printf 'arc backend: not inside a mounted arc mount\n' >&2
    return 1
  fi
  main_mount="${fields%%$'\t'*}"
  object_store="${fields#*$'\t'}"
  mount_path="${main_mount}_${name}"

  if _arc_mount_exists "$mount_path"; then
    printf 'arc backend: reusing existing mount %s\n' "$mount_path" >&2
  elif [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'arc backend: [dry-run] would create mount %s on branch %s (object-store %s)\n' \
      "$mount_path" "$branch" "$object_store" >&2
  else
    mkdir -p "$mount_path" || return 1
    "$ARC_BIN" mount -m "$mount_path" --object-store "$object_store" --override-object-store >&2 || return 1
    ( cd "$mount_path" && "$ARC_BIN" checkout -b "$branch" ) >&2 || return 1
  fi

  printf '%s\n' "$mount_path"
}

# backend_compose <project_dir> -> no-op for arc (the mount already carries .claude).
backend_compose() { :; }
