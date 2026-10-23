"""Web view of the plan dashboard: the read-only loopback server in ``plan_dashboard.web``, its
static page and the ``web`` subcommand.

Plain pytest, standard library only. Each test names the property it pins; the mutation plugin's
web family (``plan_dashboard_mutations.WEB_MUTATIONS``) breaks them one by one. The server is
started inside each test body, under ``read_only_guard`` over the fixture root, so a server that
wrote there fails the test that started it.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import json
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lib import transcript_cost
from plan_dashboard import model, web
from plan_dashboard_fixtures import _snapshot, _transcript_line, read_only_guard, set_mtime
from plan_dashboard_support import (  # noqa: F401
    ALPHA, BETA, NOW, STALE, at, console_root, isolated_home, usage_row)

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
DOC = REPO / "docs" / "components" / "plan-dashboard.md"
PAGE = SCRIPTS / "plan_dashboard" / "static" / "index.html"
INTERVAL_S = 0.05
QUIET_S = 0.5
FIXED_NOW = NOW.timestamp()


@contextlib.contextmanager
def running(tmp_path, *, guarded=True, prepare=None, **options):
    """The fixture root with a server over it; ``(server, root)``. ``prepare(root)`` runs before
    the guard and the server, for a fixture change the test needs in place first."""
    root = console_root(tmp_path)
    if prepare:
        prepare(root)
    with contextlib.ExitStack() as stack:
        if guarded:
            stack.enter_context(read_only_guard(root.home))
        options = {"cost_log": root.cost_log, "transcripts_root": root.transcripts_root, "interval": INTERVAL_S,
                   "clock": lambda: FIXED_NOW, **options}
        server = web.serve_in_thread(root.state_root, **options)
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        yield server, root


def request(server, method, path, host=None, *, send_host=True):
    conn = http.client.HTTPConnection(web.LOOPBACK, server.port, timeout=5)
    conn.putrequest(method, path, skip_host=True)
    if send_host:
        conn.putheader("Host", host or f"{web.LOOPBACK}:{server.port}")
    conn.endheaders()
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), body


def open_stream(server, timeout=5):
    conn = http.client.HTTPConnection(web.LOOPBACK, server.port, timeout=timeout)
    conn.putrequest("GET", "/api/events", skip_host=True)
    conn.putheader("Host", f"{web.LOOPBACK}:{server.port}")
    conn.endheaders()
    return conn, conn.getresponse()


def next_frame(response):
    """The next event or comment frame of a stream, as its lines (the ``retry`` preamble skipped)."""
    lines = []
    while True:
        line = response.fp.readline().decode("utf-8").rstrip("\n")
        if line:
            lines.append(line)
        elif lines:
            if not lines[0].startswith("retry:"):
                return lines
            lines = []
        elif not line and response.fp.closed:
            return lines


def expect_silence(response, conn):
    response.fp.raw._sock.settimeout(QUIET_S)
    with pytest.raises((socket.timeout, TimeoutError)):
        response.fp.readline()


def collected(root, **options):
    options = {"window_hours": 24, "include_all": False, "sessions": (), **options}
    return model.collect(root.state_root, now=dt.datetime.fromtimestamp(FIXED_NOW, dt.timezone.utc),
                         cost_log=root.cost_log, **options)


def test_page_is_served_and_the_server_is_bound_to_loopback_only(tmp_path):
    with running(tmp_path) as (server, _):
        status, headers, body = request(server, "GET", "/")
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert body == PAGE.read_bytes()
        assert server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", ""])
def test_check_bind_refuses_a_non_loopback_address(host):
    with pytest.raises(ValueError):
        web.check_bind(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_check_bind_accepts_loopback(host):
    web.check_bind(host)


def test_serve_refuses_to_bind_a_non_loopback_address(tmp_path):
    root = console_root(tmp_path)
    started = []
    with pytest.raises(ValueError):
        web.serve(root.state_root, "0.0.0.0", 0,
                  ready=lambda server: (started.append(server), threading.Thread(target=server.shutdown).start()))
    assert started == []


def test_snapshot_bytes_equal_the_models_json_and_list_the_unreadable_task(tmp_path):
    with running(tmp_path) as (server, root):
        status, headers, body = request(server, "GET", "/api/snapshot")
        assert status == 200 and headers["Content-Type"].startswith("application/json")
        assert body == model.to_json(collected(root)).encode("utf-8")
        snapshot = json.loads(body)
        assert {ALPHA, BETA, "broken"} <= {t["session_id"] for t in snapshot["tasks"]}
        assert STALE not in {t["session_id"] for t in snapshot["tasks"]}
        assert all("totals" in t for t in snapshot["tasks"] if t["readable"])


def test_task_detail_bytes_equal_the_models_json_once_the_transcript_was_read(tmp_path):
    with running(tmp_path) as (server, root):
        status, _, body = request(server, "GET", f"/api/task/{ALPHA}")
        detail = json.loads(body)
        assert status == 200
        assert detail["totals"]["root_counted"] is True and detail["totals"]["root_cost_usd"] is not None
        assert detail["phases"] and all("estimate" in phase for phase in detail["phases"])
        expected = model.detail(root.state_root, ALPHA, cost_log=root.cost_log, transcripts_root=root.transcripts_root)
        assert body == model.to_json(expected).encode("utf-8")


@pytest.mark.parametrize("path", ["/api/task/..%2Fx", "/api/task/../x", "/api/task/no-such-session",
                                  "/api/task/", "/nowhere"])
def test_a_malformed_or_unknown_path_is_404(tmp_path, path):
    with running(tmp_path) as (server, _):
        assert request(server, "GET", path)[0] == 404


def test_concurrent_requests_never_double_count_the_root_cost(tmp_path):
    baseline = []

    def append_row(root):
        baseline.append(model.detail(root.state_root, ALPHA, cost_log=root.cost_log,
                                     transcripts_root=root.transcripts_root)["totals"]["root_cost_usd"])
        model.clear_cache()
        transcript = next(root.transcripts_root.rglob("*.jsonl"))
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_transcript_line(usage_row("m2", at(minutes=15), 2)) + "\n")

    with running(tmp_path, prepare=append_row) as (server, root):
        before = baseline[0]
        streams = [open_stream(server) for _ in range(2)]
        seen, failures = [], []

        def worker():
            try:
                seen.append(json.loads(request(server, "GET", f"/api/task/{ALPHA}")[2])["totals"]["root_cost_usd"])
                request(server, "GET", "/api/snapshot")
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        for conn, _ in streams:
            conn.close()
        model.clear_cache()
        expected = model.detail(root.state_root, ALPHA, cost_log=root.cost_log,
                                transcripts_root=root.transcripts_root)["totals"]["root_cost_usd"]
        assert not failures and len(seen) == 8
        assert expected > before
        row = usage_row("m2", at(minutes=15), 2)
        assert expected == pytest.approx(before + transcript_cost.token_cost(row["usage"], row["model"]))
        assert set(seen) == {expected}


@pytest.mark.parametrize("host", ["evil.example", "evil.example:%d", "localhost.evil.example", "127.0.0.1.evil.example",
                                  None])
def test_a_foreign_or_missing_host_is_refused_on_every_route(tmp_path, host):
    with running(tmp_path) as (server, _):
        host = host % server.port if host and "%d" in host else host
        for path in ("/", "/api/snapshot", f"/api/task/{ALPHA}", "/api/events"):
            status = request(server, "GET", path, host, send_host=host is not None)[0]
            assert status == 421, (host, path)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:%d", "localhost", "localhost:%d", "[::1]", "[::1]:%d"])
def test_every_loopback_name_is_accepted_with_or_without_a_port(tmp_path, host):
    with running(tmp_path) as (server, _):
        host = host % server.port if "%d" in host else host
        assert request(server, "GET", "/api/snapshot", host)[0] == 200


def test_host_allowed_refuses_text_after_the_bracketed_name():
    assert not web.host_allowed("[::1]anything") and not web.host_allowed("[::1]:x")
    assert web.host_allowed("[::1]") and web.host_allowed("[::1]:80")


def test_a_refused_write_closes_the_connection_so_its_body_is_not_parsed_as_a_request(tmp_path):
    with running(tmp_path) as (server, _):
        body = b"GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        with socket.create_connection((web.LOOPBACK, server.port), timeout=5) as conn:
            conn.sendall((f"POST /x HTTP/1.1\r\nHost: {web.LOOPBACK}:{server.port}\r\n"
                          f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
            data = b""
            while chunk := conn.recv(4096):
                data += chunk
        assert data.startswith(b"HTTP/1.1 405") and data.count(b"HTTP/1.1 ") == 1


def test_host_allowed_decides_on_the_name_not_a_prefix():
    assert web.host_allowed("localhost:8765") and web.host_allowed("[::1]:1")
    assert not web.host_allowed(None) and not web.host_allowed("")
    assert not web.host_allowed("localhost.evil.example") and not web.host_allowed("127.0.0.1.evil.example:80")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_write_methods_get_405_and_change_nothing(tmp_path, method):
    with running(tmp_path) as (server, _):
        status, headers, _ = request(server, method, "/api/snapshot")
        assert status == 405 and headers["Allow"] == "GET, HEAD"


def test_an_unlisted_method_is_not_served(tmp_path):
    with running(tmp_path) as (server, _):
        assert request(server, "TRACE", "/api/snapshot")[0] == 501


def test_head_returns_the_headers_without_a_body_and_never_streams(tmp_path):
    with running(tmp_path) as (server, _):
        status, headers, body = request(server, "HEAD", "/api/snapshot")
        assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0
        status, headers, body = request(server, "HEAD", "/api/events")
        assert status == 200 and body == b"" and headers["Content-Type"].startswith("text/event-stream")
        assert web.head_events_end_with_the_headers(server.port)


def test_the_first_event_arrives_on_connect_and_an_unchanged_state_sends_no_second_one(tmp_path):
    with running(tmp_path) as (server, root):
        conn, response = open_stream(server)
        frame = next_frame(response)
        assert frame[0] == "event: snapshot" and frame[1].startswith("data: ")
        assert json.loads(frame[1][len("data: "):]) == json.loads(model.to_json(collected(root)))
        expect_silence(response, conn)
        conn.close()


def test_a_change_gives_exactly_one_more_event_and_touches_only_the_rewritten_file(tmp_path):
    with running(tmp_path, guarded=False) as (server, root):
        conn, response = open_stream(server)
        first = json.loads(next_frame(response)[1][len("data: "):])
        before = _snapshot(root.home)
        path = root.state_root / f"{ALPHA}.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["stages"][1].setdefault("outcome", {})["status"] = "PASSED"
        path.write_text(json.dumps(state), encoding="utf-8")
        set_mtime(path, at(minutes=1))
        second = json.loads(next_frame(response)[1][len("data: "):])
        done = {t["session_id"]: t["stages_done"] for t in first["tasks"]}[ALPHA]
        assert {t["session_id"]: t["stages_done"] for t in second["tasks"]}[ALPHA] == done + 1
        expect_silence(response, conn)
        conn.close()
        after = _snapshot(root.home)
        assert [name for name in before if before[name] != after[name]] == [str(path)]


def test_window_hours_and_sessions_change_what_the_snapshot_lists(tmp_path):
    with running(tmp_path, window_hours=0.1) as (server, _):
        ids = {t["session_id"] for t in json.loads(request(server, "GET", "/api/snapshot")[2])["tasks"]}
        assert ALPHA not in ids and BETA in ids
    with running(tmp_path / "pinned", sessions=[STALE]) as (server, _):
        ids = {t["session_id"] for t in json.loads(request(server, "GET", "/api/snapshot")[2])["tasks"]}
        assert STALE in ids


def test_the_page_builds_its_dom_without_html_injection_and_marks_estimates():
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", text)
    for token in ("estimateMark", "not counted", "function openWindowText(", '"open ("', '" so far)"', "clockOffsetMs", "≥"):
        assert token in text, token


def test_the_page_loads_nothing_from_outside():
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"https?://|\bsrc\s*=|\bhref\s*=|@import|url\(|<link\b", text)


def test_subcommand_self_test_passes_and_a_taken_port_exits_5(tmp_path):
    script = str(SCRIPTS / "plan-dashboard.py")
    done = subprocess.run([sys.executable, script, "web", "--self-test", "--state-root", str(tmp_path)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "web self-test: ok" in done.stdout
    with socket.socket() as taken:
        taken.bind((web.LOOPBACK, 0))
        taken.listen()
        port = taken.getsockname()[1]
        done = subprocess.run([sys.executable, script, "web", "--state-root", str(tmp_path), "--port", str(port)],
                              capture_output=True, text=True, timeout=60)
    assert done.returncode == 5 and "cannot listen" in done.stderr


def test_the_doc_has_a_web_page_section_with_the_tunnel_and_the_port_flag():
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Web page", 1)[1].split("\n## ", 1)[0]
    assert "ssh -L" in section and "--port" in section
