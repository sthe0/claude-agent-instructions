"""Every plan-dashboard mutation turns its tests red; with none installed they stay green.

Each case runs a test file in a subprocess with one mutation installed, so a mutation that no
test notices fails here: ``MUTATIONS`` against the model tests, the standard-library rows of
``TUI_MUTATIONS`` against ``test_plan_dashboard_tui.py``, ``WEB_MUTATIONS`` against
``test_plan_dashboard_web.py``, ``WEB_PAGE_MUTATIONS`` against ``test_plan_dashboard_web_dom.py``
(node is required as that file requires it: ``PLAN_DASHBOARD_REQUIRE_NODE=1`` fails without it, otherwise
the cases skip), and the rows that target the textual layer against the pilot file, when textual can be
imported (see ``pilot_interpreter_has_textual``).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import plan_dashboard_mutations as mutations
from test_plan_dashboard_web_dom import find_node

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
MODEL_TESTS = TESTS / "test_plan_dashboard_model.py"
TUI_TESTS = TESTS / "test_plan_dashboard_tui.py"
PILOT_TESTS = TESTS / "test_plan_dashboard_tui_app.py"
WEB_TESTS = TESTS / "test_plan_dashboard_web.py"
DOM_TESTS = TESTS / "test_plan_dashboard_web_dom.py"
WORKERS = 4
RUN_TIMEOUT_S = 1800
HUNG = -9
PILOT_MUTATIONS = sorted(name for name, (module, _, _) in mutations.TUI_MUTATIONS.items()
                         if module == mutations.TUI_APP)
STDLIB_TUI_MUTATIONS = sorted(set(mutations.TUI_MUTATIONS) - set(PILOT_MUTATIONS))
CYCLE_REPORT_NAMES = ("cycle-cpu-from-wall", "reuse-census-off")
SNAPSHOT_TESTS = TESTS / "test_plan_dashboard_snapshot.py"
SNAPSHOT_NAMES = (
    "snapshot-checksum-ignored", "cache-same-size-rewrite-missed", "unread-cost-zero", "old-marking-dropped",
    "jump-uses-snapshot-window", "mark-confirmed-from-snapshot", "overlay-waits-on-order-read",
    "binding-scan-uncached", "binding-cache-never-invalidated", "source-state-cached-forever",
    "source-binding-cached-forever", "source-transcript-cached-forever", "source-ledger-cached-forever",
    "code-version-identity-ignored", "code-version-new-module-missed", "code-version-always-parsed",
    "snapshot-records-every-source", "snapshot-directory-marker-ignored", "same-named-transcript-dropped",
    "unrecorded-source-stat-skipped", "order-read-piles-up", "child-warm-piles-up", "skipped-order-read-not-made-up",
    "row-confirmed-skipped", "startup-applied-last-read-only", "startup-applied-at-check-end",
    "startup-applied-needs-old-rows", "stat-cycle-memo-off", "stat-dir-time-key-off", "stat-dir-key-after-listing",
    "stat-dir-unsettled-key-stored", "binding-index-reuse-off", "binding-pass-per-reader",
    "binding-key-ignores-times", "binding-reuse-ignores-presumption", "state-listing-reuse-off",
    "state-key-ignores-times", "state-window-kept", "child-index-reuse-off", "child-index-ignores-floor",
    "reuse-skips-observed", "transcript-pass-per-reader", "transcript-index-reuse-off",
    "transcript-key-ignores-subdirs", "transcript-unsettled-key-stored")
SNAPSHOT_PILOT_NAMES = ("mark-confirmed-from-snapshot", "overlay-waits-on-order-read", "order-read-piles-up",
                        "child-warm-piles-up", "skipped-order-read-not-made-up", "row-confirmed-skipped",
                        "startup-applied-last-read-only", "startup-applied-at-check-end",
                        "startup-applied-needs-old-rows")
SNAPSHOT_STDLIB_NAMES = tuple(name for name in SNAPSHOT_NAMES if name not in SNAPSHOT_PILOT_NAMES)
WATCH_TESTS = TESTS / "test_plan_dashboard_watch.py"
WATCH_NAMES = (
    "overflow-not-swept", "new-dir-not-watched", "pattern-dirs-not-watched", "reconcile-sweep-dropped",
    "redraw-at-idle", "watch-failure-not-fallen-back", "event-cycle-reads-all", "redraw-not-spaced",
    "light-tick-reads-sources", "light-tick-skips-tmux", "light-tick-owns-redraw", "light-tick-ignores-pending-event",
    "sweep-not-sliced", "skipped-slice-share-lost", "held-redraw-waits-for-next-tick", "owed-header-repaint-dropped",
    "owed-tmux-poll-dropped", "cycle-start-after-read", "detection-at-path-choice")
WATCH_PILOT_NAMES = tuple(name for name, (module, _, _) in mutations.WATCH_MUTATIONS.items()
                          if module == mutations.TUI_APP)
WATCH_STDLIB_NAMES = tuple(name for name in WATCH_NAMES if name not in WATCH_PILOT_NAMES)
HARNESS_TESTS = TESTS / "test_plan_dashboard_harness.py"
HARNESS_MUTATIONS = (
    "harness-save-records-app-sources",
    "campaign-r3-k1-only-when-bands-blocked",
    "r1-old-marking-always-required",
    "r1-old-marking-per-run",
    "r1-r2-time-judged-per-run",
    "r2-startup-applied-unchecked",
    "r2-applied-judged-at-end-of-check",
    "r2-census-after-launch",
    "r3-sweep-at-cycle-start",
    "r3-due-by-sweep-order",
    "r3-uncovered-cycle-tolerated",
    "r3-latency-ignores-interval",
    "bands-old-reading",
    "r3-overlap-cycle-timed",
    "r3-watch-path-not-forced",
    "r3-fallback-run-counted",
    "r3-watch-latency-ignored",
    "r3-latency-judged-by-median",
    "r3-no-due-source-run-judged",
    "redraw-spacing-unjudged",
    "redraw-counts-header-repaint",
    "r3-coverage-only-in-assigned-cycle",
    "r6-internal-event-moment-compared",
    "r2-no-marked-rows-fallback-dropped",
    "k1-light-tick-child-counted",
    "r3-cycle-end-fallback-dropped",
    "redraw-spacing-from-first-paint",
    "r3-cell-counts-pooled",
    "r3-latency-from-last-write",
    "save-exit-wait-short",
    "stat-sweep-every-cycle-end",
    "stat-due-by-sweep-start",
    "stat-no-closing-sweep",
    "stat-closing-sweep-beside-running",
    "smoke-void-counted",
    "report-only-ignored",
    "r1-from-wall",
    "cpu-walk-no-ppid-fallback",
    "next-measurement-counts-void-k1",
)
SCRIPT_MODULE_TESTS = {**dict.fromkeys(HARNESS_MUTATIONS, HARNESS_TESTS),
                       "launcher-imports-every-subcommand": TUI_TESTS}
REQUIRED = os.environ.get("PLAN_DASHBOARD_REQUIRE_TEXTUAL") == "1"


def pilot_interpreter_has_textual() -> bool:
    return importlib.util.find_spec("textual") is not None


PILOT_RED_TEST = {
    "child-warm-piles-up": "test_a_running_child_index_warm_up_is_not_restarted_and_is_made_up_once",
    "cycle-cpu-from-wall": "test_cycle_events_carry_process_cpu_time",
    "held-redraw-waits-for-next-tick": "test_two_redraws_are_never_less_than_half_a_second_apart",
    "light-tick-ignores-pending-event": "test_the_light_tick_yields_to_a_pending_event_cycle",
    "light-tick-owns-redraw": "test_a_tmux_change_found_by_the_light_tick_is_applied_by_the_next_ticks_cycle",
    "light-tick-reads-sources": "test_the_light_tick_reads_no_source_file",
    "light-tick-skips-tmux": "test_the_light_tick_reads_no_source_file",
    "mark-confirmed-from-snapshot": "test_a_start_from_a_snapshot_shows_old_rows_and_confirms_each_task_after_its_sources_were_read",
    "order-read-piles-up": "test_a_running_order_read_is_not_restarted_and_is_made_up_once",
    "overlay-waits-on-order-read": "test_a_failed_marks_read_stays_named_when_the_order_read_lands_after_it",
    "owed-header-repaint-dropped": "test_the_light_tick_yields_to_a_pending_event_cycle",
    "owed-tmux-poll-dropped": "test_the_light_tick_yields_to_a_pending_event_cycle",
    "redraw-at-idle": "test_an_idle_dashboard_starts_no_cycle_but_the_reconcile_mode_one_and_does_not_redraw",
    "redraw-not-spaced": "test_two_redraws_are_never_less_than_half_a_second_apart",
    "reuse-census-off": "test_a_reused_stat_cycle_reports_its_reused_readers",
    "row-confirmed-skipped": "test_each_row_confirmed_by_the_live_check_is_named_once_in_row_confirmed",
    "skipped-order-read-not-made-up": "test_a_running_order_read_is_not_restarted_and_is_made_up_once",
    "skipped-slice-share-lost": "test_a_sweep_never_delays_an_event_cycle_or_its_redraw",
    "startup-applied-at-check-end": "test_startup_applied_carries_every_source_the_start_up_check_applied",
    "startup-applied-last-read-only": "test_startup_applied_carries_every_source_the_start_up_check_applied",
    "startup-applied-needs-old-rows": "test_startup_applied_carries_every_source_the_start_up_check_applied",
    "sweep-not-sliced": "test_a_sweep_never_delays_an_event_cycle_or_its_redraw",
    "tui-app-all-counter-partial": "test_rows_carry_the_waiting_mark_style_and_the_header_count",
    "tui-app-all-no-feedback": "test_a_toggle_names_the_load_at_once_and_then_the_mode_and_count",
    "tui-app-all-overlay-eager": "test_all_mode_reads_the_marks_of_the_rows_in_view_only_and_each_once_per_change",
    "tui-app-all-overlay-no-cache": "test_all_mode_reads_the_marks_of_the_rows_in_view_only_and_each_once_per_change",
    "tui-app-all-overlay-no-rescan": "test_all_mode_reads_the_marks_of_the_rows_in_view_only_and_each_once_per_change",
    "tui-app-attention-error-blanks": "test_a_failing_attention_or_chat_read_keeps_the_task_table_and_names_the_error",
    "tui-app-callback-after-exit": "test_an_outcome_that_arrives_after_the_loop_closed_raises_nothing",
    "tui-app-chat-absent": "test_the_mark_and_the_chat_follow_a_transcript_that_grows_after_a_tick",
    "tui-app-chat-keys-swallowed": "test_j_k_up_and_down_scroll_the_chat_and_leave_the_task_cursor",
    "tui-app-chat-late-apply": "test_a_chat_answer_for_another_task_is_not_shown_on_the_open_chat_tab",
    "tui-app-chat-out-of-order": "test_a_chat_read_that_ends_after_a_newer_one_is_dropped",
    "tui-app-chat-read-always": "test_opening_another_task_shows_no_chat_of_the_previous_one_while_its_own_loads",
    "tui-app-chat-stale": "test_opening_another_task_shows_no_chat_of_the_previous_one_while_its_own_loads",
    "tui-app-chat-waits-reload": "test_the_mark_and_the_chat_follow_a_transcript_that_grows_after_a_tick",
    "tui-app-detail-keys-swallowed": "test_arrow_keys_move_the_tree_cursor_and_enter_and_space_toggle_a_node",
    "tui-app-detail-out-of-order": "test_a_detail_read_that_ends_after_a_newer_one_is_dropped",
    "tui-app-detail-unfocused": "test_the_detail_tree_opens_collapsed_with_focus_on_the_tree_by_enter",
    "tui-app-expand-global": "test_the_tree_state_of_one_task_is_not_carried_to_another",
    "tui-app-jump-unbound": "test_g_on_the_tasks_tab_jumps_to_the_task_under_the_cursor_and_names_the_way_back",
    "tui-app-loading-title-overwritten": "test_the_loading_title_survives_a_reload_issued_before_the_toggle",
    "tui-app-mark-unread-blank": "test_all_mode_reads_the_marks_of_the_rows_in_view_only_and_each_once_per_change",
    "tui-app-mode-unshown": "test_tasks_tab_lists_the_live_tasks",
    "tui-app-no-refresh": "test_the_view_reads_again_once_the_interval_has_passed",
    "tui-app-quit-waits-load": "test_q_returns_to_the_shell_within_two_seconds_while_a_reload_is_still_running",
    "tui-app-rows-unstyled": "test_rows_carry_the_waiting_mark_style_and_the_header_count",
    "tui-app-screen-scrolls": "test_no_tab_scrolls_the_screen_and_the_tabs_row_sits_below_the_header",
    "tui-app-table-waits-overlay": "test_tasks_tab_lists_the_live_tasks",
    "tui-app-tick-cancels-read": "test_ticks_leave_the_reads_in_flight_alone_and_each_lands_once_released",
    "tui-app-tree-expanded": "test_the_detail_tree_opens_collapsed_with_focus_on_the_tree_by_enter",
    "tui-app-tree-no-placeholder": "test_another_task_shows_loading_in_the_tree_at_once_and_none_of_the_previous_one",
    "tui-app-tree-state-forgotten": "test_a_refresh_keeps_what_the_user_opened_and_the_cursor_though_the_label_changes",
    "tui-app-tree-waits-reload": "test_the_detail_tree_fills_while_the_reload_is_still_running",
    "tui-app-writes-state": "test_the_app_writes_nothing",
    "tui-jump-writes-state": "test_g_on_the_tasks_tab_jumps_to_the_task_under_the_cursor_and_names_the_way_back",
}
_green_pilot_run: list = []


def run_tests(test_file: Path, mutation: str, only: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, mutations.ENV: mutation, "PYTHONPATH": f"{TESTS}{os.pathsep}{SCRIPTS}"}
    target = f"{test_file}::{only}" if only else str(test_file)
    command = [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", "-p", "plan_dashboard_mutations",
               *(["-x"] if mutation else []), target]
    try:
        return subprocess.run(command, cwd=SCRIPTS.parent, env=env, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as hung:
        out = hung.output.decode() if isinstance(hung.output, bytes) else (hung.output or "")
        return subprocess.CompletedProcess(command, HUNG, f"{out}\nTIMEOUT after {RUN_TIMEOUT_S}s, mutation: {mutation or 'none'}", "")


def run_all(test_file: Path, names: list[str]) -> dict[str, subprocess.CompletedProcess]:
    names = ["", *names]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(zip(names, pool.map(lambda name: run_tests(test_file, name), names)))


def run_pilot(names: list[str]) -> dict[str, subprocess.CompletedProcess]:
    """The unmutated pilot run is made once for the three pilot fixtures; each mutated run executes the one test
    that goes red first for it, so a renamed test ends in pytest's rc 4, which `assert_red` rejects."""
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        green = pool.submit(lambda: _green_pilot_run[0] if _green_pilot_run else run_tests(PILOT_TESTS, ""))
        mutated = list(pool.map(lambda name: run_tests(PILOT_TESTS, name, PILOT_RED_TEST[name]), names))
        if not _green_pilot_run:
            _green_pilot_run.append(green.result())
    return dict(zip(["", *names], [_green_pilot_run[0], *mutated]))


@pytest.fixture(scope="module")
def results():
    return run_all(MODEL_TESTS, sorted(mutations.MUTATIONS))


@pytest.fixture(scope="module")
def tui_results():
    return run_all(TUI_TESTS, STDLIB_TUI_MUTATIONS)


@pytest.fixture(scope="module")
def web_results():
    return run_all(WEB_TESTS, sorted(mutations.WEB_MUTATIONS))


@pytest.fixture(scope="module")
def dom_results():
    if find_node() is None:
        if os.environ.get("PLAN_DASHBOARD_REQUIRE_NODE") == "1":
            pytest.fail("PLAN_DASHBOARD_REQUIRE_NODE=1 but node was not found (PLAN_DASHBOARD_NODE or PATH)")
        pytest.skip("node not found")
    return run_all(DOM_TESTS, sorted(mutations.WEB_PAGE_MUTATIONS))


@pytest.fixture(scope="module")
def pilot_results():
    if not (REQUIRED or pilot_interpreter_has_textual()):
        pytest.skip("textual is not importable here; run this file under the uv command of docs/components/plan-dashboard.md")
    return run_pilot(PILOT_MUTATIONS)


@pytest.fixture(scope="module")
def snapshot_results():
    return run_all(SNAPSHOT_TESTS, list(SNAPSHOT_STDLIB_NAMES))


@pytest.fixture(scope="module")
def snapshot_pilot_results():
    if not (REQUIRED or pilot_interpreter_has_textual()):
        pytest.skip("textual is not importable here; run this file under the uv command of docs/components/plan-dashboard.md")
    return run_pilot(list(SNAPSHOT_PILOT_NAMES))


def assert_not_hung(result):
    """A run that hit the hang guard says nothing about the tests: it is neither the red nor the green the
    catalogue looks for, and fails as its own outcome."""
    if result.returncode == HUNG:
        pytest.fail(f"no verdict, the run hung (hang guard {RUN_TIMEOUT_S}s):\n{result.stdout[-1000:]}", pytrace=False)


def assert_green(result):
    assert_not_hung(result)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "plan-dashboard mutation: none" in result.stdout


def assert_red(name, result, what):
    assert_not_hung(result)
    assert f"plan-dashboard mutation: {name} applied" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 1, f"{name} left the {what} green:\n{result.stdout[-2000:]}"
    assert "FAILED" in result.stdout


@pytest.mark.parametrize("judge", [lambda r: assert_green(r), lambda r: assert_red("m", r, "tests")], ids=["green", "red"])
def test_a_run_that_hit_the_hang_guard_is_its_own_outcome_neither_red_nor_green(judge):
    hung = subprocess.CompletedProcess([], HUNG, "plan-dashboard mutation: m applied\nFAILED x\nTIMEOUT", "")
    with pytest.raises(pytest.fail.Exception, match="the run hung"):
        judge(hung)


def test_the_model_tests_are_green_with_no_mutation(results):
    assert_green(results[""])


@pytest.mark.parametrize("name", sorted(mutations.MUTATIONS))
def test_a_mutation_turns_a_model_test_red(name, results):
    assert_red(name, results[name], "model tests")


def test_the_console_tests_are_green_with_no_mutation(tui_results):
    assert_green(tui_results[""])


@pytest.mark.parametrize("name", STDLIB_TUI_MUTATIONS)
def test_a_console_mutation_turns_a_console_test_red(name, tui_results):
    assert_red(name, tui_results[name], "console tests")


def test_the_web_tests_are_green_with_no_mutation(web_results):
    assert_green(web_results[""])


@pytest.mark.parametrize("name", sorted(mutations.WEB_MUTATIONS))
def test_a_web_mutation_turns_a_web_test_red(name, web_results):
    assert_red(name, web_results[name], "web tests")


def test_the_page_tests_are_green_with_no_mutation(dom_results):
    assert_green(dom_results[""])


@pytest.mark.parametrize("name", sorted(mutations.WEB_PAGE_MUTATIONS))
def test_a_page_mutation_turns_a_page_test_red(name, dom_results):
    assert_red(name, dom_results[name], "page tests")


def test_the_pilot_tests_are_green_with_no_mutation(pilot_results):
    assert_green(pilot_results[""])


@pytest.mark.parametrize("name", PILOT_MUTATIONS)
def test_a_console_mutation_turns_a_pilot_test_red(name, pilot_results):
    assert_red(name, pilot_results[name], "pilot tests")


def test_the_cycle_report_mutations_are_registered_console_mutations():
    registered = {name for name, (module, _, _) in mutations.TUI_MUTATIONS.items()
                  if module == mutations.TUI_APP and name in CYCLE_REPORT_NAMES}
    assert registered == set(CYCLE_REPORT_NAMES) and set(CYCLE_REPORT_NAMES) <= set(PILOT_MUTATIONS)


def test_the_snapshot_family_is_the_registered_one():
    assert set(SNAPSHOT_NAMES) == set(mutations.SNAPSHOT_MUTATIONS) and len(set(SNAPSHOT_NAMES)) == len(SNAPSHOT_NAMES)
    assert set(SNAPSHOT_PILOT_NAMES) == {name for name, (module, _, _) in mutations.SNAPSHOT_MUTATIONS.items()
                                         if module == mutations.TUI_APP}


def test_the_snapshot_tests_are_green_with_no_mutation(snapshot_results):
    assert_green(snapshot_results[""])


@pytest.mark.parametrize("name", SNAPSHOT_STDLIB_NAMES)
def test_a_snapshot_mutation_turns_a_snapshot_test_red(name, snapshot_results):
    assert_red(name, snapshot_results[name], "snapshot tests")


@pytest.mark.parametrize("name", SNAPSHOT_PILOT_NAMES)
def test_a_snapshot_mutation_turns_a_pilot_test_red(name, snapshot_pilot_results):
    assert_red(name, snapshot_pilot_results[name], "pilot tests")


@pytest.fixture(scope="module")
def watch_results():
    return run_all(WATCH_TESTS, list(WATCH_STDLIB_NAMES))


@pytest.fixture(scope="module")
def watch_pilot_results():
    if not (REQUIRED or pilot_interpreter_has_textual()):
        pytest.skip("textual is not importable here; run this file under the uv command of docs/components/plan-dashboard.md")
    return run_pilot(list(WATCH_PILOT_NAMES))


def test_the_watch_family_is_the_registered_one():
    assert set(WATCH_NAMES) == set(mutations.WATCH_MUTATIONS) and len(set(WATCH_NAMES)) == len(WATCH_NAMES)


def test_the_watch_tests_are_green_with_no_mutation(watch_results):
    assert_green(watch_results[""])


@pytest.mark.parametrize("name", WATCH_STDLIB_NAMES)
def test_a_watch_mutation_turns_a_watch_test_red(name, watch_results):
    assert_red(name, watch_results[name], "watch tests")


@pytest.mark.parametrize("name", WATCH_PILOT_NAMES)
def test_a_watch_mutation_turns_a_pilot_test_red(name, watch_pilot_results):
    assert_red(name, watch_pilot_results[name], "pilot tests")


@pytest.fixture(scope="module")
def script_module_results():
    runs = {"": HARNESS_TESTS, **SCRIPT_MODULE_TESTS}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(zip(runs, pool.map(lambda name: run_tests(runs[name], name), runs)))


def test_the_script_module_family_is_the_registered_one():
    assert set(SCRIPT_MODULE_TESTS) == set(mutations.SCRIPT_MODULE_MUTATIONS)


def test_the_harness_tests_are_green_with_no_mutation(script_module_results):
    assert_green(script_module_results[""])


@pytest.mark.parametrize("name", sorted(SCRIPT_MODULE_TESTS))
def test_a_script_module_mutation_turns_its_test_red(name, script_module_results):
    assert_red(name, script_module_results[name], SCRIPT_MODULE_TESTS[name].name)
