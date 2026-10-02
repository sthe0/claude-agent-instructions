#!/usr/bin/env bash
# Grant-surface sweep: runs every test file that exercises the permission
# payload spawn-specialist.py hands a spawned child, in ONE pytest
# invocation with no -k filter, and accepts the run only when pytest's
# summary line reports passes and nothing else. A skip, an xfail, an error
# or an empty run is no evidence that the payload still holds, so each one
# fails the sweep.
#
# Which files belong (pinned against the tree by test_sweep_grant_surface.py):
# every scripts/tests/test_*.py naming build_child_settings, plus
# test_stage_grants.py and test_grant_derivation.py, which test the grant
# producers feeding it, plus test_sweep_grant_surface.py itself.
#
# Usage: scripts/tests/sweep_grant_surface.sh    (exit 0 iff the sweep is green)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"

SWEEP_FILES=(
    scripts/tests/test_authoring_grant_check.py
    scripts/tests/test_grant_derivation.py
    scripts/tests/test_grant_snapshot_refresh.py
    scripts/tests/test_pra_review_catalogue.py
    scripts/tests/test_review_attestation_capability.py
    scripts/tests/test_spawn_autocompact_window.py
    scripts/tests/test_spawn_grants_integration.py
    scripts/tests/test_spawn_merge_verb_grant.py
    scripts/tests/test_spawn_plans_reachability.py
    scripts/tests/test_spawn_project_settings.py
    scripts/tests/test_spawn_pytest_allowlist_hygiene.py
    scripts/tests/test_spawn_specialist_grants.py
    scripts/tests/test_spawn_stage2_image.py
    scripts/tests/test_stage_grants.py
    scripts/tests/test_sweep_grant_surface.py
)

# <file>:<test function> pairs the sweep is worthless without: a green run
# that silently lost one of them proves less than it claims.
REQUIRED_DEFS=(
    "scripts/tests/test_spawn_specialist_grants.py:test_mutation_control_is_collected_and_discriminates"
)

summary_line_is_green() {
    local green='^[0-9]+ passed(, [0-9]+ warnings?)? in [0-9]+\.[0-9]+s( \([0-9]+:[0-9]{2}:[0-9]{2}\))?$'
    [[ "$1" =~ $green ]]
}

main() {
    local pair file name output status last_line
    for pair in "${REQUIRED_DEFS[@]}"; do
        file="${pair%%:*}"
        name="${pair#*:}"
        if ! grep -Eq "^def ${name}\(" "$REPO_ROOT/$file" 2>/dev/null; then
            echo "sweep_grant_surface: required test ${file}::${name} is missing" >&2
            return 1
        fi
    done

    output="$(cd "$REPO_ROOT" && python3 -m pytest -q "${SWEEP_FILES[@]}" 2>&1)"
    status=$?
    last_line="$(printf '%s\n' "$output" | awk 'NF { line = $0 } END { print line }')"
    if [[ $status -eq 0 ]] && summary_line_is_green "$last_line"; then
        printf '%s\n' "$last_line"
        return 0
    fi
    printf '%s\n' "$output" | tail -n 10 >&2
    return 1
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    main "$@"
    exit $?
fi
