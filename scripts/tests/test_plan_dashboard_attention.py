"""Attention overlay, chat tails and the session-locator seam of the plan dashboard:
``plan_dashboard.attention`` and ``plan_dashboard.locate``, their routes in ``plan_dashboard.web`` and
the ``locate`` / ``snapshot --attention`` / ``snapshot --chat`` commands.

Plain pytest, standard library only. Every transcript, binding and fake locator plugin is written
before the read-only guard over the fixture root is entered; the plugins use a placeholder argv and
no test starts a program. The mutation plugin's attention family
(``plan_dashboard_mutations.ATTENTION_MUTATIONS``) breaks these tests one by one.
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import config_root
from plan_dashboard import attention, locate, model, web
from plan_dashboard_fixtures import build_fixture_root, read_only_guard, set_mtime
from plan_dashboard_support import ALPHA, BETA, NOW, at, event, iso, isolated_home, make_state  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
DOC = REPO / "docs" / "components" / "plan-dashboard.md"
_spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

INTERVAL_S = 0.05
QUIET_S = 0.5
FIXED_NOW = NOW.timestamp()
PROJECT = "-attention-project"
H_A, H_B, H_FREE = "harness-a", "harness-b", "harness-free"
QUESTION = {"questions": [{"header": "HEADER-TEXT", "question": "Which way?", "multiSelect": False, "options": [
    {"label": "Left", "description": "DESC-LEFT"}, {"label": "Right", "description": "DESC-RIGHT"}]}]}


@pytest.fixture(autouse=True)
def fresh_cache():
    attention._CACHE.clear()
    yield
    attention._CACHE.clear()


# --- transcript builders ---------------------------------------------------------------------

def stamp(minutes: int) -> str:
    return iso(at(minutes=minutes))


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str, tool_input: dict | None = None) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input or {}}


def user(minutes: int, content) -> dict:
    return {"type": "user", "timestamp": stamp(minutes), "message": {"role": "user", "content": content}}


def tool_result(minutes: int, tool_id: str, content="result") -> dict:
    return user(minutes, [{"type": "tool_result", "tool_use_id": tool_id, "content": content}])


def assistant(minutes: int, *blocks: dict, stop: str | None = None) -> dict:
    message = {"role": "assistant", "content": list(blocks)}
    if stop:
        message["stop_reason"] = stop
    return {"type": "assistant", "timestamp": stamp(minutes), "message": message}


def put(root, harness: str, entries, *, age_minutes: int = 0, project: str = PROJECT) -> Path:
    path = root.transcripts_root / project / f"{harness}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    set_mtime(path, at(minutes=age_minutes))
    return path


def make_root(tmp_path, *, bindings=None):
    alpha = make_state(ALPHA, stages=("ACTIVE",), history=[event("classify", at(minutes=30)),
                                                           event("next_stage", at(minutes=20), stage=1)])
    beta = make_state(BETA, stages=("ACTIVE",), history=[event("classify", at(minutes=25))])
    return build_fixture_root(tmp_path, [alpha, beta], now=NOW, bindings=bindings)


def overlay(root, locator=None, engine=ALPHA) -> dict:
    return attention.task_overlay(engine, locator, root.transcripts_root)


def fake(**hooks) -> SimpleNamespace:
    return SimpleNamespace(NAME="fake", **hooks)


WORKING = [user(10, "go on"), assistant(9, text("looking"), tool_use("t1", "Bash", {"command": "ls"}), stop="tool_use")]
IDLE = [user(10, "go on"), assistant(9, text("done"), stop="end_turn")]
ASKING = [user(10, "go on"), assistant(9, tool_use("q1", "AskUserQuestion", QUESTION), stop="tool_use")]


# --- the waiting value -----------------------------------------------------------------------

@pytest.mark.parametrize("entries, waiting, since", [
    (ASKING, "choice", stamp(9)),
    ([user(10, "plan?"), assistant(9, tool_use("p1", "ExitPlanMode"), stop="tool_use")], "choice", stamp(9)),
    (IDLE, "idle", stamp(9)),
    (WORKING, "none", stamp(9)),
    (WORKING + [tool_result(8, "t1")], "none", stamp(8)),
    (ASKING + [tool_result(8, "q1", "Left")], "none", stamp(8)),
    (IDLE + [user(3, "and then?")], "none", stamp(3)),
], ids=["ask-question", "exit-plan-mode", "idle", "working", "tool-answered", "choice-answered", "user-spoke-last"])
def test_waiting_value_from_the_transcript(tmp_path, entries, waiting, since):
    root = make_root(tmp_path)
    put(root, ALPHA, entries)
    assert overlay(root) == {"waiting": waiting, "harness_session": ALPHA, "since": since}


@pytest.mark.parametrize("entries, permission, waiting", [
    (WORKING, True, "permission"),
    (WORKING, False, "none"),
    (WORKING, None, "none"),
    (IDLE, True, "permission"),
    (ASKING, True, "choice"),
], ids=["working+permission", "working", "unknown", "permission-over-idle", "choice-over-permission"])
def test_permission_ranks_below_choice_and_above_idle(tmp_path, entries, permission, waiting):
    root = make_root(tmp_path)
    put(root, ALPHA, entries)
    assert overlay(root, fake(permission_wait=lambda h: permission))["waiting"] == waiting


def test_no_transcript_means_no_waiting_value_whatever_the_locator_says(tmp_path):
    root = make_root(tmp_path)
    asked = []
    locator = fake(permission_wait=lambda h: asked.append(h) or True)
    assert overlay(root, locator) == {"waiting": None, "harness_session": None, "since": None}
    assert asked == []


def test_a_bound_session_without_a_transcript_is_named_and_waits_for_nothing(tmp_path):
    root = make_root(tmp_path, bindings={H_A: [ALPHA], H_B: [ALPHA]})
    assert overlay(root) == {"waiting": None, "harness_session": H_B, "since": None}


@pytest.mark.parametrize("age_a, age_b, winner", [(5, 1, H_B), (1, 5, H_A), (3, 3, H_B)],
                         ids=["b-newer", "a-newer", "tie-goes-to-the-greater-id"])
def test_the_newest_transcript_of_the_candidate_sessions_is_the_one_bound(tmp_path, age_a, age_b, winner):
    root = make_root(tmp_path, bindings={H_A: [ALPHA], H_B: [ALPHA]})
    put(root, H_A, IDLE, age_minutes=age_a)
    put(root, H_B, ASKING, age_minutes=age_b)
    result = overlay(root)
    assert result["harness_session"] == winner
    assert result["waiting"] == ("idle" if winner == H_A else "choice")


def test_the_engine_session_id_is_a_candidate_too(tmp_path):
    root = make_root(tmp_path, bindings={H_A: [ALPHA]})
    put(root, H_A, IDLE, age_minutes=9)
    put(root, ALPHA, ASKING, age_minutes=1)
    assert overlay(root)["harness_session"] == ALPHA


def test_never_asks_the_model_for_detail(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    put(root, ALPHA, ASKING)

    def refuse(*args, **kwargs):
        raise AssertionError("model.detail must not be called")

    monkeypatch.setattr(model, "detail", refuse)
    attention.event_payload(*attention.collect([ALPHA, BETA], None, root.transcripts_root))
    attention.task_chat(ALPHA, None, root.transcripts_root)


# --- finding the transcript ------------------------------------------------------------------

def test_the_locators_transcript_comes_first_then_the_newest_file_under_the_root(tmp_path):
    root = make_root(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "any-name.jsonl"
    elsewhere.parent.mkdir()
    elsewhere.write_text("".join(json.dumps(e) + "\n" for e in IDLE), encoding="utf-8")
    put(root, ALPHA, ASKING, age_minutes=50, project="-old")
    new = put(root, ALPHA, WORKING, age_minutes=2, project="-new")
    assert attention.find_transcript(ALPHA, None, root.transcripts_root) == new
    assert attention.find_transcript(ALPHA, fake(transcript_path=lambda h: str(elsewhere)), root.transcripts_root) == elsewhere
    for unusable in (str(tmp_path / "missing.jsonl"), None, 7, ""):
        assert attention.find_transcript(ALPHA, fake(transcript_path=lambda h: unusable), root.transcripts_root) == new
    assert attention.find_transcript(ALPHA, fake(transcript_path=lambda h: 1 / 0), root.transcripts_root) == new


def test_with_a_root_given_nothing_outside_it_is_looked_up_or_opened(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    outside = tmp_path / "outside" / f"{ALPHA}.jsonl"
    outside.parent.mkdir()
    outside.write_text(json.dumps(user(1, "hi")) + "\n", encoding="utf-8")
    opened = []
    real_open = open
    monkeypatch.setattr(attention, "open", lambda file, *a, **k: opened.append(str(file)) or real_open(file, *a, **k), raising=False)
    monkeypatch.setattr(config_root, "iter_transcripts", lambda pattern: [outside])
    assert attention.find_transcript(ALPHA, None, root.transcripts_root) is None
    assert overlay(root) == {"waiting": None, "harness_session": None, "since": None}
    assert opened == []
    assert attention.find_transcript(ALPHA, None, None) == outside


# --- the chat tail ---------------------------------------------------------------------------

def test_chat_tail_is_bounded_and_keeps_only_what_a_reader_may_see(tmp_path):
    root = make_root(tmp_path)
    entries = [user(100 - n, f"msg-{n:02d}") for n in range(60)]
    entries += [assistant(30, {"type": "thinking", "thinking": "SECRET-THOUGHT"}, text("x" * 5000)),
                assistant(29, tool_use("b1", "Bash", {"command": "SECRET-COMMAND"}), stop="tool_use"),
                tool_result(28, "b1", "SECRET-RESULT"),
                assistant(27, tool_use("q1", "AskUserQuestion", QUESTION), stop="tool_use")]
    put(root, ALPHA, entries)
    chat = attention.task_chat(ALPHA, None, root.transcripts_root)
    shown = chat["entries"]
    assert chat["harness_session"] == ALPHA
    assert len(shown) == attention.CHAT_MAX_ENTRIES
    assert shown[0] == {"role": "user", "text": "msg-23", "tool": None, "cut": False}
    long_text, bash, ask = shown[-3:]
    assert long_text["cut"] is True and len(long_text["text"]) == attention.CHAT_TEXT_MAX
    assert long_text["text"].endswith(attention.CUT_MARK)
    assert bash == {"role": "assistant", "text": "", "tool": "Bash", "cut": False}
    assert ask["tool"] == "AskUserQuestion"
    dumped = json.dumps(chat)
    assert not any(word in dumped for word in ("SECRET", "DESC-", "HEADER-TEXT"))


def test_an_ask_question_entry_carries_the_question_and_the_option_labels(tmp_path):
    root = make_root(tmp_path)
    put(root, ALPHA, ASKING)
    ask = attention.task_chat(ALPHA, None, root.transcripts_root)["entries"][-1]
    assert ask["text"] == "Which way?\n- Left\n- Right"


def test_question_text_skips_malformed_blocks():
    assert attention.question_text(None) == ""
    assert attention.question_text({"questions": [7, {"question": 3}, {"question": "Ok?", "options": [{"label": "A"}, 5]}]}) == "Ok?\n- A"


def test_the_chat_of_an_unknown_session_is_empty(tmp_path):
    root = make_root(tmp_path)
    assert attention.chat_tail("nobody", None, root.transcripts_root) == {"harness_session": "nobody", "entries": []}


# --- incremental reads -----------------------------------------------------------------------

def spy_consume(monkeypatch):
    seen = []
    real = attention._consume
    monkeypatch.setattr(attention, "_consume", lambda tail, data: (seen.append(data), real(tail, data))[1])
    return seen


def lines_of(entries) -> bytes:
    return b"".join(json.dumps(e).encode("utf-8") + b"\n" for e in entries)


def test_the_first_read_is_bounded_and_starts_on_a_line(tmp_path, monkeypatch):
    path = tmp_path / "big.jsonl"
    path.write_bytes(lines_of(user(1, f"line-{n:05d}-" + "p" * 900) for n in range(1500)))
    seen = spy_consume(monkeypatch)
    tail = attention.read_tail(path)
    size = path.stat().st_size
    assert size > attention.FIRST_READ_BYTES
    assert len(seen) == 1 and attention.FIRST_READ_BYTES - 2000 < len(seen[0]) <= attention.FIRST_READ_BYTES
    assert seen[0].startswith(b'{"type"') and seen[0].endswith(b"\n")
    assert tail.offset == size and tail.chat[-1]["text"].startswith("line-01499-")


def test_later_reads_take_only_the_appended_bytes_and_keep_a_partial_line(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    path.write_bytes(lines_of(IDLE))
    attention.read_tail(path)
    seen = spy_consume(monkeypatch)
    extra = lines_of([user(2, "second")])
    with open(path, "ab") as handle:
        handle.write(extra[:20])
    attention.read_tail(path)
    with open(path, "ab") as handle:
        handle.write(extra[20:])
    tail = attention.read_tail(path)
    assert seen == [extra[:20], extra[20:]]
    assert [e["text"] for e in tail.chat].count("second") == 1


def test_an_unchanged_transcript_is_not_opened_again(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    path.write_bytes(lines_of(IDLE))
    first = attention.read_tail(path)
    opened = []
    real_open = open
    monkeypatch.setattr(attention, "open", lambda file, *a, **k: opened.append(file) or real_open(file, *a, **k), raising=False)
    assert attention.read_tail(path) is first and opened == []


def test_a_shrunk_or_replaced_transcript_is_read_again_from_the_start(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(lines_of([user(5, "one"), user(4, "two"), user(3, "three")]))
    assert [e["text"] for e in attention.read_tail(path).chat] == ["one", "two", "three"]
    path.write_bytes(lines_of([user(2, "x")]))
    assert [e["text"] for e in attention.read_tail(path).chat] == ["x"]
    replacement = tmp_path / "new.jsonl"
    replacement.write_bytes(lines_of([user(2, "y" * 40), user(1, "z" * 40)]))
    os.replace(replacement, path)
    assert [e["text"][0] for e in attention.read_tail(path).chat] == ["y", "z"]


def test_an_unreadable_path_has_no_tail(tmp_path):
    assert attention.read_tail(tmp_path / "missing.jsonl") is None


# --- session rows and the overlay payload ----------------------------------------------------

def sessions_locator(entries, **hooks):
    return fake(sessions=lambda: entries, **hooks)


def test_unbound_sessions_get_a_row_and_bound_ones_do_not(tmp_path):
    root = make_root(tmp_path, bindings={H_A: [ALPHA]})
    put(root, H_A, IDLE)
    put(root, H_FREE, ASKING)
    put(root, "other", WORKING)
    locator = sessions_locator([
        {"harness_session": H_A, "cwd": "/w/a", "name": "bound"},
        {"harness_session": ALPHA, "cwd": None, "name": "the engine id itself"},
        {"harness_session": H_FREE, "cwd": "/w/free", "name": "free"},
        {"harness_session": "other"},
        {"harness_session": H_FREE, "cwd": "/dup", "name": "dup"},
        "junk", {"cwd": "/no-id"}, {"harness_session": 5}, {"harness_session": "x", "cwd": 3},
    ])
    overlays, rows = attention.collect([ALPHA, BETA], locator, root.transcripts_root)
    assert overlays[ALPHA]["harness_session"] == H_A
    assert rows == [
        {"harness_session": H_FREE, "cwd": "/w/free", "name": "free", "waiting": "choice", "since": stamp(9)},
        {"harness_session": "other", "cwd": None, "name": None, "waiting": "none", "since": stamp(9)}]


@pytest.mark.parametrize("locator", [None, fake(), fake(sessions=lambda: 1 / 0), fake(sessions=lambda: "nope")],
                         ids=["no-locator", "no-hook", "raising-hook", "malformed-result"])
def test_without_a_usable_sessions_hook_there_are_no_rows(tmp_path, locator):
    root = make_root(tmp_path)
    put(root, H_FREE, ASKING)
    assert attention.collect([ALPHA], locator, root.transcripts_root)[1] == []


def test_the_digest_follows_the_overlay_and_nothing_else(tmp_path):
    root = make_root(tmp_path)
    path = put(root, ALPHA, WORKING)
    first = attention.event_payload(*attention.collect([ALPHA], None, root.transcripts_root))
    again = attention.event_payload(*attention.collect([ALPHA], None, root.transcripts_root))
    assert first == again and set(first) == {"digest", "tasks", "sessions"}
    with open(path, "ab") as handle:
        handle.write(lines_of([assistant(1, text("finished"), stop="end_turn")]))
    changed = attention.event_payload(*attention.collect([ALPHA], None, root.transcripts_root))
    assert changed["digest"] != first["digest"] and changed["tasks"][ALPHA]["waiting"] == "idle"


# --- the web routes --------------------------------------------------------------------------

@contextlib.contextmanager
def serving(root, *, guarded=True, **options):
    with contextlib.ExitStack() as stack:
        if guarded:
            stack.enter_context(read_only_guard(root.home))
        options = {"cost_log": root.cost_log, "transcripts_root": root.transcripts_root, "interval": INTERVAL_S,
                   "clock": lambda: FIXED_NOW, **options}
        server = web.serve_in_thread(root.state_root, **options)
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        yield server


def get(server, path):
    conn = http.client.HTTPConnection(web.LOOPBACK, server.port, timeout=5)
    conn.request("GET", path, headers={"Host": f"{web.LOOPBACK}:{server.port}"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, body


def open_stream(server):
    conn = http.client.HTTPConnection(web.LOOPBACK, server.port, timeout=5)
    conn.request("GET", "/api/events", headers={"Host": f"{web.LOOPBACK}:{server.port}"})
    return conn, conn.getresponse()


def next_frame(response):
    lines = []
    while True:
        raw = response.fp.readline()
        assert raw, "the stream closed before the next frame"
        line = raw.decode("utf-8").rstrip("\n")
        if line:
            lines.append(line)
        elif lines:
            if not lines[0].startswith("retry:"):
                return lines
            lines = []


def expect_silence(response):
    response.fp.raw._sock.settimeout(QUIET_S)
    with pytest.raises((socket.timeout, TimeoutError)):
        response.fp.readline()


def event_data(frame) -> dict:
    assert frame[1].startswith("data: ")
    return json.loads(frame[1][len("data: "):])


def routes_root(tmp_path):
    root = make_root(tmp_path, bindings={H_A: [ALPHA]})
    put(root, H_A, ASKING)
    put(root, H_FREE, IDLE)
    return root


ROUTE_LOCATOR = sessions_locator([{"harness_session": H_FREE, "cwd": "/w", "name": "free"}])


def test_attention_route_equals_the_payload_and_the_event_data(tmp_path):
    root = routes_root(tmp_path)
    expected = attention.event_payload(*attention.collect([ALPHA, BETA], ROUTE_LOCATOR, root.transcripts_root))
    assert expected["tasks"][ALPHA]["waiting"] == "choice" and [r["harness_session"] for r in expected["sessions"]] == [H_FREE]
    with serving(root, locator=ROUTE_LOCATOR) as server:
        status, body = get(server, "/api/attention")
        conn, response = open_stream(server)
        assert next_frame(response)[0] == "event: snapshot"
        frame = next_frame(response)
        conn.close()
    assert status == 200 and json.loads(body) == expected
    assert frame[0] == "event: attention" and event_data(frame) == expected


def test_the_overlay_leaves_the_pinned_snapshot_alone_and_does_not_move_with_the_clock(tmp_path):
    root = routes_root(tmp_path)
    with serving(root) as bare:
        plain_snapshot, plain_attention = get(bare, "/api/snapshot")[1], get(bare, "/api/attention")[1]
    with serving(root, locator=ROUTE_LOCATOR) as located:
        assert get(located, "/api/snapshot")[1] == plain_snapshot
        assert get(located, "/api/attention")[1] != plain_attention
        located_digest = json.loads(get(located, "/api/attention")[1])["digest"]
    with serving(root, locator=ROUTE_LOCATOR, clock=lambda: FIXED_NOW + 3600) as later:
        assert json.loads(get(later, "/api/attention")[1])["digest"] == located_digest


def test_chat_routes_answer_for_tasks_and_listed_sessions_only(tmp_path):
    root = routes_root(tmp_path)
    with serving(root, locator=ROUTE_LOCATOR) as server:
        status, body = get(server, f"/api/task/{ALPHA}/chat")
        assert status == 200 and json.loads(body)["harness_session"] == H_A
        assert json.loads(body)["entries"][-1]["tool"] == "AskUserQuestion"
        status, body = get(server, f"/api/session/{H_FREE}/chat")
        assert status == 200 and json.loads(body)["entries"][-1]["text"] == "done"
        for path in (f"/api/session/{H_A}/chat", "/api/session/nobody/chat", "/api/session/..%2Fx/chat",
                     "/api/task/nobody/chat", "/api/task/..%2Fx/chat", f"/api/task/{ALPHA}x/chat"):
            assert get(server, path)[0] == 404, path


def test_the_stream_sends_one_attention_event_per_change(tmp_path):
    root = routes_root(tmp_path)
    path = put(root, ALPHA, WORKING)
    with serving(root, guarded=False, locator=ROUTE_LOCATOR) as server:
        conn, response = open_stream(server)
        assert next_frame(response)[0] == "event: snapshot"
        assert event_data(next_frame(response))["tasks"][ALPHA]["waiting"] == "choice"
        with open(path, "ab") as handle:
            handle.write(lines_of([assistant(1, text("finished"), stop="end_turn")]))
        os.remove(root.transcripts_root / PROJECT / f"{H_A}.jsonl")
        frame = next_frame(response)
        assert frame[0] == "event: attention" and event_data(frame)["tasks"][ALPHA]["waiting"] == "idle"
        expect_silence(response)
        conn.close()


def test_the_snapshot_event_does_not_wait_for_a_slow_overlay(tmp_path, monkeypatch):
    root = routes_root(tmp_path)
    release = threading.Event()
    collect = attention.collect
    monkeypatch.setattr(attention, "collect", lambda *args: release.wait(10) and collect(*args))
    try:
        with serving(root, guarded=False, locator=ROUTE_LOCATOR) as server:
            conn, response = open_stream(server)
            assert next_frame(response)[0] == "event: snapshot"
            release.set()
            assert next_frame(response)[0] == "event: attention"
            conn.close()
    finally:
        release.set()


def test_a_quiet_stream_stays_quiet_after_the_first_two_events(tmp_path):
    root = routes_root(tmp_path)
    with serving(root, locator=ROUTE_LOCATOR) as server:
        conn, response = open_stream(server)
        assert [next_frame(response)[0], next_frame(response)[0]] == ["event: snapshot", "event: attention"]
        expect_silence(response)
        conn.close()


# --- the locator seam ------------------------------------------------------------------------

def write_locator(directory: Path, name: str = "fake", *, body: str | None = None, **data) -> None:
    target = directory / locate.LOCATOR_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    source = body if body is not None else textwrap.dedent(f"""
        NAME = {name!r}
        DATA = {data!r}
        def health(): return True, "fine"
        def transcript_path(h): return DATA.get("paths", {{}}).get(h)
        def permission_wait(h): return h in DATA.get("permission", [])
        def jump_target(h):
            if h in DATA.get("jump", []):
                return {{"argv": ["placeholder-no-op", h], "label": "go to " + h, "return_hint": "return by hand"}}
        def sessions(): return DATA.get("sessions", [])
    """)
    (target / f"{name}.py").write_text(source, encoding="utf-8")


def test_exactly_one_locator_is_used_and_a_broken_one_is_an_error(tmp_path):
    with pytest.raises(locate.LocatorError, match="no locator"):
        locate.select(tmp_path / "empty")
    assert locate.select_or_none(tmp_path / "empty") is None
    write_locator(tmp_path, "a")
    assert locate.name_of(locate.select(tmp_path)) == "a"
    write_locator(tmp_path, "b")
    with pytest.raises(locate.LocatorError, match="several"):
        locate.select(tmp_path)
    assert locate.name_of(locate.select(tmp_path, "b")) == "b"
    with pytest.raises(locate.LocatorError, match="named zzz"):
        locate.select(tmp_path, "zzz")
    write_locator(tmp_path, "c", body="raise ImportError('nope')")
    with pytest.raises(locate.LocatorError, match="does not load"):
        locate.select(tmp_path, "a")
    write_locator(tmp_path, "c", body="NAME = 'c'")
    with pytest.raises(locate.LocatorError, match=r"lacks health\(\)"):
        locate.select(tmp_path, "a")


def test_malformed_hook_answers_count_as_absent():
    locator = fake(
        transcript_path=lambda h: 3, permission_wait=lambda h: "yes",
        jump_target=lambda h: {"argv": [], "label": "x", "return_hint": "y"})
    assert locate.transcript_path(locator, "h") is None
    assert locate.permission_wait(locator, "h") is None
    assert locate.jump_target(locator, "h") is None
    for bad in ({"argv": ["a", 1], "label": "x", "return_hint": "y"}, {"argv": ["a"], "label": 1, "return_hint": "y"}, "a", None):
        assert locate.jump_target(fake(jump_target=lambda h, bad=bad: bad), "h") is None
    assert locate.jump_target(fake(jump_target=lambda h: {"argv": ["a"], "label": "x", "return_hint": "y"}), "h") \
        == {"argv": ["a"], "label": "x", "return_hint": "y"}
    assert locate.jump_target(fake(jump_target=lambda h: 1 / 0), "h") is None
    assert locate.jump_target(None, "h") is None and locate.sessions(None) == []


def test_health_runs_with_the_network_and_process_spawning_stubbed():
    def reaches_out():
        with contextlib.suppress(OSError):
            socket.socket().connect(("127.0.0.1", 9))
        return True

    def spawns():
        with contextlib.suppress(OSError):
            subprocess.Popen(["placeholder-no-op"])
        return True

    assert locate.check_health(fake(health=lambda: (True, "fine"))) == (True, "fine")
    assert locate.check_health(fake(health=lambda: False)) == (False, "")
    for probe in (reaches_out, spawns):
        ok, detail = locate.check_health(fake(health=probe))
        assert not ok and "network or spawned" in detail
    ok, detail = locate.check_health(fake(health=lambda: 1 / 0))
    assert not ok and "ZeroDivisionError" in detail
    socket.socket().close()
    assert subprocess.Popen.__module__ == "subprocess"


class Recorder:
    def __init__(self, outcome=None):
        self.calls, self.outcome = [], outcome

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome or subprocess.CompletedProcess(args[0], 0, "", "")


TARGET = {"argv": ["placeholder-no-op", "one"], "label": "l", "return_hint": "r"}


def test_jump_runs_the_argv_without_a_shell_under_a_timeout(monkeypatch):
    run = Recorder()
    monkeypatch.setattr(locate.subprocess, "run", run)
    assert locate.jump(TARGET).returncode == 0
    assert run.calls == [((["placeholder-no-op", "one"],),
                          {"shell": False, "timeout": 5, "capture_output": True, "text": True})]


@pytest.mark.parametrize("failure, code", [
    (subprocess.TimeoutExpired(["x"], 5), 124), (FileNotFoundError("no such program"), 127), (PermissionError("denied"), 127)])
def test_jump_never_raises(monkeypatch, failure, code):
    monkeypatch.setattr(locate.subprocess, "run", Recorder(failure))
    result = locate.jump(TARGET)
    assert result.returncode == code and result.args == TARGET["argv"]


def test_jump_accepts_a_replacement_runner():
    runner = Recorder(subprocess.CompletedProcess(["x"], 3, "", ""))
    assert locate.jump(TARGET, runner=runner).returncode == 3 and runner.calls == [((TARGET["argv"],), {})]


# --- the commands ----------------------------------------------------------------------------

def command(root, *args):
    return [*args, "--state-root", str(root.state_root), "--cost-log", str(root.cost_log),
            "--transcripts-root", str(root.transcripts_root), "--all"]


def test_snapshot_attention_lists_the_overlay_and_the_unbound_sessions(tmp_path, capsys):
    root = routes_root(tmp_path)
    write_locator(tmp_path / "plugins", sessions=[{"harness_session": H_FREE, "cwd": "/w", "name": "free"}])
    with read_only_guard(root.home):
        assert cli.main(command(root, "snapshot", "--attention", "--plugin-dir", str(tmp_path / "plugins"))) == 0
    snapshot = json.loads(capsys.readouterr().out)
    waiting = {t["session_id"]: t["attention"]["waiting"] for t in snapshot["tasks"]}
    assert waiting[ALPHA] == "choice" and waiting[BETA] is None
    assert [r["harness_session"] for r in snapshot["sessions"]] == [H_FREE]


def test_snapshot_attention_goes_on_without_a_locator(tmp_path, capsys):
    root = routes_root(tmp_path)
    with read_only_guard(root.home):
        assert cli.main(command(root, "snapshot", "--attention", "--plugin-dir", str(tmp_path / "none"))) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["sessions"] == [] and all("attention" in t for t in snapshot["tasks"])


def test_the_locator_changes_what_the_overlay_says(tmp_path, capsys):
    root = make_root(tmp_path)
    put(root, ALPHA, WORKING)
    write_locator(tmp_path / "plugins", permission=[ALPHA])
    with read_only_guard(root.home):
        assert cli.main(command(root, "snapshot", "--attention", "--plugin-dir", str(tmp_path / "plugins"))) == 0
    tasks = {t["session_id"]: t for t in json.loads(capsys.readouterr().out)["tasks"]}
    assert tasks[ALPHA]["attention"]["waiting"] == "permission"


def test_snapshot_chat_prints_the_tail_of_a_task_or_a_listed_session(tmp_path, capsys):
    root = routes_root(tmp_path)
    write_locator(tmp_path / "plugins", sessions=[{"harness_session": H_FREE, "cwd": None, "name": None}])
    plugins = ["--plugin-dir", str(tmp_path / "plugins")]
    with read_only_guard(root.home):
        assert cli.main(command(root, "snapshot", "--chat", ALPHA, *plugins)) == 0
        assert json.loads(capsys.readouterr().out)["entries"][-1]["tool"] == "AskUserQuestion"
        assert cli.main(command(root, "snapshot", "--chat", H_FREE, *plugins)) == 0
        assert json.loads(capsys.readouterr().out)["entries"][-1]["text"] == "done"
        assert cli.main(command(root, "snapshot", "--chat", "nobody", *plugins)) == cli.EXIT_NO_SUCH_SESSION
    assert "no such session: nobody" in capsys.readouterr().err


def test_locate_health_reports_ok_unhealthy_and_a_missing_plugin(tmp_path, capsys):
    root = make_root(tmp_path)
    write_locator(tmp_path / "good")
    assert cli.main(command(root, "locate", "--health", "--plugin-dir", str(tmp_path / "good"))) == 0
    assert "locator fake: ok: fine" in capsys.readouterr().out
    write_locator(tmp_path / "bad", body=textwrap.dedent("""
        import contextlib, socket
        NAME = "reaches"
        def health():
            with contextlib.suppress(OSError):
                socket.socket().connect(("127.0.0.1", 9))
            return True
    """))
    assert cli.main(command(root, "locate", "--health", "--plugin-dir", str(tmp_path / "bad"))) == 1
    assert "locator reaches: unhealthy" in capsys.readouterr().out
    assert cli.main(command(root, "locate", "--health", "--plugin-dir", str(tmp_path / "none"))) == cli.EXIT_NO_LOCATOR
    assert "no locator plugin" in capsys.readouterr().err


def test_locate_names_the_hooks_and_what_it_finds_for_a_session(tmp_path, capsys):
    root = make_root(tmp_path, bindings={H_A: [ALPHA]})
    put(root, H_A, IDLE)
    write_locator(tmp_path / "plugins", jump=[H_A])
    with read_only_guard(root.home):
        assert cli.main(command(root, "locate", "--session", ALPHA, "--session", BETA,
                                "--plugin-dir", str(tmp_path / "plugins"), "--locator", "fake")) == 0
    out = capsys.readouterr().out
    assert "locator: fake" in out and "hooks: transcript_path, permission_wait, jump_target, sessions" in out
    alpha_block = out.split(f"session {ALPHA}\n", 1)[1].split("session ", 1)[0]
    assert f"harness session: {H_A}" in alpha_block and "transcript: found" in alpha_block
    assert "waiting: idle" in alpha_block and f"jump: go to {H_A} (return: return by hand)" in alpha_block
    beta_block = out.split(f"session {BETA}\n", 1)[1]
    assert "harness session: none" in beta_block and "transcript: not found" in beta_block
    assert "waiting: unknown" in beta_block and "jump: no jump target" in beta_block


def test_locate_without_a_locator_exits_with_the_locator_code(tmp_path, capsys):
    root = make_root(tmp_path)
    assert cli.main(command(root, "locate", "--plugin-dir", str(tmp_path / "none"))) == cli.EXIT_NO_LOCATOR
    assert "no locator plugin" in capsys.readouterr().err


# --- the documentation and the Core boundary -------------------------------------------------

def section() -> str:
    return DOC.read_text(encoding="utf-8").split("## Attention and chat", 1)[1].split("\n## ", 1)[0]


def test_the_doc_section_names_the_values_the_bounds_and_the_seam():
    text = section()
    for needle in ("choice", "permission", "idle", "none", "AskUserQuestion", "40", "2000", "sessions()",
                   "locators/", "--plugin-dir", "--locator", "locate --health", "/api/attention", "chat"):
        assert needle in text, needle


def test_core_files_and_the_doc_name_no_messenger_multiplexer_or_terminal():
    names = ["".join(parts) for parts in (("tm", "ux"), ("i", "term"), ("tele", "gram"), ("cc", "gram"))]
    pattern = re.compile("|".join(names), re.IGNORECASE)
    for path in (SCRIPTS / "plan_dashboard" / "locate.py", SCRIPTS / "plan_dashboard" / "attention.py", DOC):
        assert not pattern.search(path.read_text(encoding="utf-8")), path
