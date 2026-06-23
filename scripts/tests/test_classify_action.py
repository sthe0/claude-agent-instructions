from agentctl.classify import classify_action


def test_read_is_side_effect_free():
    assert classify_action("Read") == "side-effect-free"


def test_grep_is_side_effect_free():
    assert classify_action("Grep") == "side-effect-free"


def test_glob_is_side_effect_free():
    assert classify_action("Glob") == "side-effect-free"


def test_websearch_is_side_effect_free():
    assert classify_action("WebSearch") == "side-effect-free"


def test_bash_ls_is_side_effect_free():
    assert classify_action("Bash", "ls") == "side-effect-free"


def test_bash_cat_is_side_effect_free():
    assert classify_action("Bash", "cat") == "side-effect-free"


def test_bash_grep_is_side_effect_free():
    assert classify_action("Bash", "grep") == "side-effect-free"


def test_bash_git_log_is_side_effect_free():
    assert classify_action("Bash", "git", "log") == "side-effect-free"


def test_bash_arc_status_is_side_effect_free():
    assert classify_action("Bash", "arc", "status") == "side-effect-free"


def test_mcp_get_is_side_effect_free():
    assert classify_action("mcp__tracker__GetIssue") == "side-effect-free"


def test_mcp_search_in_name_is_side_effect_free():
    assert classify_action("mcp__x__searchFoo") == "side-effect-free"


def test_edit_needs_approval():
    assert classify_action("Edit") == "needs-approval"


def test_write_needs_approval():
    assert classify_action("Write") == "needs-approval"


def test_bash_rm_needs_approval():
    assert classify_action("Bash", "rm") == "needs-approval"


def test_bash_git_push_needs_approval():
    assert classify_action("Bash", "git", "push") == "needs-approval"


def test_bash_arc_commit_needs_approval():
    assert classify_action("Bash", "arc", "commit") == "needs-approval"


def test_bash_mv_needs_approval():
    assert classify_action("Bash", "mv") == "needs-approval"


def test_bash_python3_is_unknown():
    assert classify_action("Bash", "python3") == "unknown"


def test_bash_unknown_verb_is_unknown():
    assert classify_action("Bash", "frobnicate") == "unknown"


def test_unknown_tool_is_unknown():
    assert classify_action("SomeUnknownTool") == "unknown"


def test_mcp_opaque_method_is_unknown():
    assert classify_action("mcp__x__doStuff") == "unknown"


def test_bash_git_bare_needs_approval():
    assert classify_action("Bash", "git", None) == "needs-approval"


def test_bash_arc_bare_needs_approval():
    assert classify_action("Bash", "arc", None) == "needs-approval"
