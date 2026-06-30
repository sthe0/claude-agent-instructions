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
