#!/usr/bin/env bash
# Built-in GitHub-Issue tracker backend (optional).
#
# Implements the tracker half of the provider contract (see registry.sh):
#   tracker_resolve <key>  -> prints 'key<TAB>slug'  for an existing issue
#   tracker_create  <title> -> prints 'key<TAB>slug' for a newly created issue
#
# slug = kebab-case of the issue title. Every `gh` call goes through the GH_BIN
# seam (default `gh`) so the tests can stub it. tracker_create is confirmation-
# gated: it proceeds without a prompt only when CLAUDE_LAUNCH_ASSUME_YES=1, and
# under CLAUDE_DRY_RUN it performs zero external effects.
#
# When `gh` is absent or no issue is requested, the CLI selects tracker name
# `none` (see trackers/none.sh) and the task name passes through unchanged — this
# backend is only sourced when the caller explicitly asked for a GitHub issue.

GH_BIN="${GH_BIN:-gh}"
# scripts/project_entry/trackers/github.sh -> up two levels is scripts/.
_GH_ORGCHECK_PY="${CHECK_ORG_NEUTRAL_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/check-org-neutral.py}"
_VERIFY_PLAN_SYNC_PY="${VERIFY_PLAN_SYNC_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/verify-ticket-plan-sync.py}"

# kebab: lowercase, non-alnum -> '-', squeeze/trim dashes, truncate ~40 chars.
# Uses ERE (sed -E, '+'): BSD sed (macOS) does not honor GNU's BRE '\+'.
_gh_slug() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E -e 's/[^a-z0-9]+/-/g' -e 's/^-+//' -e 's/-+$//' \
    | cut -c1-40 \
    | sed -E -e 's/-+$//'
}

# tracker_resolve <issue-number> -> 'number<TAB>slug'
tracker_resolve() {
  local key="$1" title slug
  title="$("$GH_BIN" issue view "$key" --json title --jq .title)" || return 1
  slug="$(_gh_slug "$title")"
  printf '%s\t%s\n' "$key" "$slug"
}

# tracker_create <title> -> 'number<TAB>slug'  (confirmation-gated)
# Reads CLAUDE_TRACKER_QUEUE for the target GitHub repo (owner/repo); when unset,
# gh uses the current git context (existing behaviour).
tracker_create() {
  local title="$1" slug out key
  local _queue="${CLAUDE_TRACKER_QUEUE:-}"
  slug="$(_gh_slug "$title")"

  if [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'github tracker: [dry-run] would create issue %q%s\n' \
      "$title" "${_queue:+ (queue=$_queue)}" >&2
    printf 'DRYRUN\t%s\n' "$slug"
    return 0
  fi

  if [[ "${CLAUDE_LAUNCH_ASSUME_YES:-}" != "1" ]]; then
    printf 'github tracker: create issue %q%s? set CLAUDE_LAUNCH_ASSUME_YES=1 to proceed.\n' \
      "$title" "${_queue:+ (queue=$_queue)}" >&2
    return 1
  fi

  local -a _gh_args=(--title "$title")
  [[ -n "$_queue" ]] && _gh_args+=(--repo "$_queue")
  out="$("$GH_BIN" issue create "${_gh_args[@]}")" || return 1
  # `gh issue create` prints the new issue URL (…/issues/<n>) on success.
  key="$(printf '%s' "$out" | grep -oE '[0-9]+$' | tail -1)"
  [[ -n "$key" ]] || key="$out"
  printf '%s\t%s\n' "$key" "$slug"
}

# tracker_read <key> -> flat, backend-agnostic rendering of the issue on stdout
# (title / status / author / description / comments). Optional verb: the
# caller probes presence with `declare -F tracker_read`. Single degrade class:
# any failure (gh absent, auth error, malformed JSON, …) normalizes to exit 1
# with the reason on stderr — never leaks gh's own exit code or partial stdout.
tracker_read() {
  local key="$1" json rc

  if [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'github tracker: [dry-run] tracker_read skipped for %q\n' "$key" >&2
    return 1
  fi

  json="$("$GH_BIN" issue view "$key" --json title,state,author,body,comments 2>/dev/null)"
  rc=$?
  if [[ $rc -ne 0 || -z "$json" ]]; then
    printf 'github tracker: tracker_read failed for %q (gh issue view exited %d)\n' "$key" "$rc" >&2
    return 1
  fi

  printf '%s' "$json" | python3 -c '
import json, sys

d = json.load(sys.stdin)
out = []
out.append("title: " + str(d.get("title", "")))
out.append("status: " + str(d.get("state", "")))
out.append("author: " + str((d.get("author") or {}).get("login", "")))
out.append("--- description ---")
out.append(d.get("body") or "")
for i, c in enumerate(d.get("comments") or [], start=1):
    login = (c.get("author") or {}).get("login", "")
    created = c.get("createdAt", "")
    out.append("--- comment " + str(i) + " by " + str(login) + " at " + str(created) + " ---")
    out.append(c.get("body") or "")
print("\n".join(out))
' || {
    printf 'github tracker: tracker_read failed for %q (rendering error)\n' "$key" >&2
    return 1
  }
}

# _gh_orgcheck <file> [<file> ...] -> 0 if every file is clean of org-internal
# markers (or the override is set), 1 otherwise, with the marker report on
# stderr. Shared refusal gate for tracker_comment / tracker_publish_plan.
_gh_orgcheck() {
  local f report
  for f in "$@"; do
    report="$(python3 "$_GH_ORGCHECK_PY" "$f" 2>&1)" && continue
    if [[ "${CLAUDE_PUBLISH_ALLOW_INTERNAL:-}" == "1" ]]; then
      continue
    fi
    printf 'github tracker: refused to publish %q — org-internal markers found (set CLAUDE_PUBLISH_ALLOW_INTERNAL=1 to override):\n%s\n' \
      "$f" "$report" >&2
    return 1
  done
  return 0
}

# tracker_comment <key> <markdown-path> -> posts the file's contents as a NEW
# issue comment; never edits the description. Optional verb: the caller
# probes presence with `declare -F tracker_comment`. Guarded by the
# org-neutral check on <markdown-path>, CLAUDE_DRY_RUN, and the
# CLAUDE_LAUNCH_ASSUME_YES confirmation gate (outward-facing, irreversible).
# Single degrade class: exit 0 = posted, ANY nonzero = not posted, reason on
# stderr.
tracker_comment() {
  local key="$1" md_path="$2" rc

  _gh_orgcheck "$md_path" || return 1

  if [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'github tracker: [dry-run] would comment on %q from %q\n' "$key" "$md_path" >&2
    return 1
  fi

  if [[ "${CLAUDE_LAUNCH_ASSUME_YES:-}" != "1" ]]; then
    printf 'github tracker: post comment on %q from %q? set CLAUDE_LAUNCH_ASSUME_YES=1 to proceed.\n' \
      "$key" "$md_path" >&2
    return 1
  fi

  local -a _gh_args=(issue comment "$key" --body-file "$md_path")
  [[ -n "${CLAUDE_TRACKER_QUEUE:-}" ]] && _gh_args+=(--repo "$CLAUDE_TRACKER_QUEUE")
  "$GH_BIN" "${_gh_args[@]}" >/dev/null
  rc=$?
  if [[ $rc -ne 0 ]]; then
    printf 'github tracker: tracker_comment failed for %q (gh issue comment exited %d)\n' "$key" "$rc" >&2
    return 1
  fi
}

# tracker_publish_plan <key> <toml-path> <markdown-path> -> publishes the
# approved plan snapshot: a SECRET gist (never --public) holding <toml-path>
# verbatim (passed by path — never re-read/re-serialized, so the published
# bytes are byte-identical to the input file), then a NEW issue comment
# containing <markdown-path>'s content plus the gist URL and an
# agent-plan-sync marker (see verify-ticket-plan-sync.py) so a later session
# can detect drift between this comment and the current TOML. Optional
# verb, probed the same way. Guarded by the org-neutral check on BOTH
# files, CLAUDE_DRY_RUN, and CLAUDE_LAUNCH_ASSUME_YES. Single degrade
# class: exit 0 = gist created AND comment posted, ANY nonzero = not fully
# published — including a marker-computation failure, which is a
# precondition of publishing at all and never leaves a half-published
# state. A comment failure AFTER a successful gist creation still surfaces
# as one degrade, but the gist URL is reported on stderr rather than left
# unknown — a half-published state is never left unreported.
tracker_publish_plan() {
  local key="$1" toml_path="$2" md_path="$3" out rc gist_url tmp_comment marker_line

  _gh_orgcheck "$toml_path" "$md_path" || return 1

  if [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'github tracker: [dry-run] would publish plan for %q (toml=%q, comment=%q)\n' \
      "$key" "$toml_path" "$md_path" >&2
    return 1
  fi

  if [[ "${CLAUDE_LAUNCH_ASSUME_YES:-}" != "1" ]]; then
    printf 'github tracker: publish plan for %q (gist + comment)? set CLAUDE_LAUNCH_ASSUME_YES=1 to proceed.\n' \
      "$key" >&2
    return 1
  fi

  marker_line="$(python3 "$_VERIFY_PLAN_SYNC_PY" --emit-marker --plan "$toml_path")" || {
    printf 'github tracker: tracker_publish_plan failed to compute the plan-sync marker for %q — refusing to publish.\n' \
      "$toml_path" >&2
    return 1
  }

  out="$("$GH_BIN" gist create "$toml_path" --desc "approved plan: $key" 2>&1)"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    printf 'github tracker: tracker_publish_plan failed creating the gist for %q (gh gist create exited %d): %s\n' \
      "$key" "$rc" "$out" >&2
    return 1
  fi
  gist_url="$(printf '%s\n' "$out" | grep -oE 'https://gist\.[^[:space:]]+' | tail -1)"
  [[ -n "$gist_url" ]] || gist_url="$out"

  tmp_comment="$(mktemp)" || {
    printf 'github tracker: tracker_publish_plan created gist %s for %q but could not stage the comment (mktemp failed) — the gist is not yet linked from the task.\n' \
      "$gist_url" "$key" >&2
    return 1
  }
  cat "$md_path" >"$tmp_comment"
  printf '\nApproved plan: %s\n' "$gist_url" >>"$tmp_comment"
  printf '\n%s\n' "$marker_line" >>"$tmp_comment"

  local -a _gh_args=(issue comment "$key" --body-file "$tmp_comment")
  [[ -n "${CLAUDE_TRACKER_QUEUE:-}" ]] && _gh_args+=(--repo "$CLAUDE_TRACKER_QUEUE")
  "$GH_BIN" "${_gh_args[@]}" >/dev/null
  rc=$?
  rm -f "$tmp_comment"
  if [[ $rc -ne 0 ]]; then
    printf 'github tracker: tracker_publish_plan created gist %s for %q but failed to post the comment (gh issue comment exited %d) — the gist is not linked from the task; retry the comment manually with that URL.\n' \
      "$gist_url" "$key" "$rc" >&2
    return 1
  fi
}
