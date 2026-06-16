#!/bin/bash
# Pre-Bash hook: forbid recursive scans (find/grep -r/rg/ag/tree/du) that touch
# an Arcadia mount root. Arcadia is mounted via FUSE — a recursive traversal
# from a mount root or any large subtree hangs the filesystem.
#
# Allowed mount roots: /home/<user>/arcadia and /home/<user>/arcadia_<slug>.
# Blocking rules:
#   1. The command names a mount root as a path argument to find/rg/ag/tree/du
#      or as the target of `grep -r`/`-R`/`-rn` etc.
#   2. The current working directory IS a mount root and the command runs
#      one of the heavy tools without narrowing to a subdirectory.
# `ya tool <name>` invocations are an indexed remote search and are not blocked.

set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
[ -z "$cwd" ] && cwd="${PWD:-}"

allow() {
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
    exit 0
}

deny() {
    jq -n --arg reason "$1" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }'
    exit 0
}

[ -z "$cmd" ] && allow

cwd="${cwd%/}"

cwd_is_mount_root=0
if [[ "$cwd" =~ ^/home/[^/]+/arcadia(_[^/]+)?$ ]]; then
    cwd_is_mount_root=1
fi

# Mask "ya tool <name>" prefixes so the heavy-tool detector doesn't see them.
clean_cmd=$(printf '%s' "$cmd" | sed -E 's/(^|[[:space:];&|`(])ya[[:space:]]+tool[[:space:]]+[a-zA-Z0-9_.-]+/\1__YATOOL__/g')

mount_root_path='(\$HOME|~|/home/[^/[:space:]"'\'']+)/arcadia(_[^/[:space:]"'\'']+)?/?'

# After the tool name, optionally allow other args before the mount-root path.
heavy_path_re="(^|[[:space:];&|\`(])(find|rg|ag|tree|du)[[:space:]]+([^|;&]*[[:space:]])?${mount_root_path}([[:space:]]|$|[;&|\`])"
grep_path_re="(^|[[:space:];&|\`(])grep[[:space:]]+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)([[:space:]]+[^|;&]*[[:space:]])?${mount_root_path}([[:space:]]|$|[;&|\`])"

# An inline `cd <mount-root>` re-roots the rest of the command at the FUSE mount even when
# the session cwd is elsewhere (e.g. `cd ~/arcadia; grep -rn pat .`). The cwd-based checks
# below only see the session cwd, so detect the inline cd explicitly here. Single-quoted so
# the literal backtick in the separator class needs no escaping.
cd_to_mount_re='(^|[[:space:];&|`(])cd[[:space:]]+(\$HOME|~|/home/[^/[:space:]]+)/arcadia(_[^/[:space:]]+)?([[:space:];&|`)]|$)'
if printf '%s' "$clean_cmd" | grep -qE "$cd_to_mount_re"; then
    if printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])(find|rg|ag|tree|du)([[:space:]]|$)' \
       || printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])grep[[:space:]]+(-[a-zA-Z]*[rR]|--recursive)'; then
        deny "Inline 'cd' into an Arcadia mount root followed by a recursive scan (find/grep -r/rg/ag/tree/du) re-roots the scan at the FUSE mount. Use 'ya tool cs' / semantic code search, or cd into a specific narrow subdirectory first."
    fi
fi

if printf '%s' "$clean_cmd" | grep -qE "$heavy_path_re"; then
    deny "Heavy recursive scan (find/rg/ag/tree/du) targets an Arcadia mount root. Narrow to a specific subdirectory, or use 'ya tool cs' / semantic code search instead."
fi

if printf '%s' "$clean_cmd" | grep -qE "$grep_path_re"; then
    deny "Recursive grep targets an Arcadia mount root. Use 'ya tool cs' / semantic code search, or narrow to a specific subdirectory."
fi

# $HOME / ~ / /home/<user> is NOT a mount root but CONTAINS every FUSE Arcadia mount as a
# subdirectory — a recursive scan rooted at the home dir descends into all of them and hangs
# the FS. Block scans whose path IS the home root (optional trailing slash, no deeper
# component); narrow subdirs like ~/.claude stay allowed.
home_root='(\$HOME|~|/home/[^/[:space:]"'\'']+)/?'
home_heavy_re="(^|[[:space:];&|\`(])(find|rg|ag|tree|du)[[:space:]]+([^|;&]*[[:space:]])?${home_root}([[:space:]]|$|[;&|\`])"
home_grep_re="(^|[[:space:];&|\`(])grep[[:space:]]+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)([[:space:]]+[^|;&]*[[:space:]])?${home_root}([[:space:]]|$|[;&|\`])"
if printf '%s' "$clean_cmd" | grep -qE "$home_heavy_re" || printf '%s' "$clean_cmd" | grep -qE "$home_grep_re"; then
    deny "Recursive scan rooted at the home dir (\$HOME/~//home/<user>) is forbidden — home contains FUSE Arcadia mounts; the traversal hangs the FS. Find a skill CLI via 'ls <project>/.claude/skills/<name>/scripts/'; for code use 'ya tool cs'; or narrow to a specific subdirectory."
fi

# The filesystem root `/` is the worst case: it contains every FUSE Arcadia mount plus
# the whole OS tree, so a recursive scan from `/` traverses all of it and hangs. Block
# scans whose path argument is exactly `/` (a bare slash token, not `/place`, `/home/x`,
# etc., which are matched as deeper paths and left to the narrower rules above).
fs_root_heavy_re="(^|[[:space:];&|\`(])(find|rg|ag|tree|du)[[:space:]]+([^|;&]*[[:space:]])?/([[:space:]]|$|[;&|\`])"
fs_root_grep_re="(^|[[:space:];&|\`(])grep[[:space:]]+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)([[:space:]]+[^|;&]*[[:space:]])?/([[:space:]]|$|[;&|\`])"
if printf '%s' "$clean_cmd" | grep -qE "$fs_root_heavy_re" || printf '%s' "$clean_cmd" | grep -qE "$fs_root_grep_re"; then
    deny "Recursive scan rooted at the filesystem root '/' is forbidden — it traverses the entire OS and every FUSE Arcadia mount and hangs. Narrow to a specific directory, list a known path with 'ls <dir>', or use 'ya tool cs' / semantic code search."
fi

if [ "$cwd_is_mount_root" -eq 1 ]; then
    if printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])find([[:space:]]+\.([[:space:]]|$)|[[:space:]]+-[a-zA-Z]|[[:space:]]*$|[[:space:]]*[;&|])'; then
        deny "Running 'find' from the Arcadia mount root ($cwd) is forbidden. cd into a specific subdirectory first or use 'ya tool cs'."
    fi
    if printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])grep[[:space:]]+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)[^|;&]*([[:space:]]\.([[:space:]]|$)|[[:space:]]*$|[[:space:]]*[;&|])'; then
        deny "Recursive grep from the Arcadia mount root ($cwd) is forbidden. cd into a specific subdirectory first or use 'ya tool cs'."
    fi
    if printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])(rg|ag)([[:space:]]+\.([[:space:]]|$)|[[:space:]]+[^-][^|;&]*$|[[:space:]]*$|[[:space:]]*[;&|])'; then
        deny "Running rg/ag from the Arcadia mount root ($cwd) is forbidden. cd into a specific subdirectory first or use 'ya tool cs'."
    fi
    if printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])tree([[:space:]]|$|[;&|])'; then
        deny "Running 'tree' from the Arcadia mount root ($cwd) is forbidden."
    fi
    if printf '%s' "$clean_cmd" | grep -qE '(^|[[:space:];&|`(])du[[:space:]]+[^|;&]*(\.([[:space:]]|$)|$)'; then
        deny "Running 'du' from the Arcadia mount root ($cwd) is forbidden."
    fi
fi

allow
