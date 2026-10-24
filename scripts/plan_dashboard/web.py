"""Read-only web view of the live agentctl tasks.

A stdlib HTTP server bound to a loopback address only, serving the static page, a JSON
snapshot, one task's detail, the attention overlay and chat tails, and a Server-Sent Events stream
that sends a snapshot when its content changed and an ``attention`` event when the overlay's digest
changed. GET and HEAD only; a request whose ``Host`` header is not a loopback name is
refused (DNS rebinding), so a page on another origin cannot read the data through the browser.
Every read goes through ``model``, which never writes the state directory.
"""
from __future__ import annotations

import datetime as dt
import http.client
import ipaddress
import json
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentctl import store
from plan_dashboard import attention, model

LOOPBACK = "127.0.0.1"
HOST_NAMES = frozenset({LOOPBACK, "localhost", "[::1]"})
READ_METHODS = ("GET", "HEAD")
DEFAULT_INTERVAL_S = 2.0
HEARTBEAT_S = 15.0
INDEX = Path(__file__).resolve().parent / "static" / "index.html"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
}
TEXT = "text/plain; charset=utf-8"


def page_bytes() -> bytes:
    return INDEX.read_bytes()


def check_bind(host: str) -> None:
    """Refuse any address that is not loopback; ``serve`` calls it, so no caller can widen it."""
    if host == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError(f"plan dashboard binds loopback addresses only, not {host!r}")


def host_allowed(header: str | None) -> bool:
    """The Host header names a loopback name, with or without a port; a missing one is refused."""
    if not header:
        return False
    name = header.lower()
    if name.startswith("["):
        name, closer, rest = name.partition("]")
        name += closer
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
    elif ":" in name:
        name = name.rsplit(":", 1)[0]
    return name in HOST_NAMES


def event_due(last_digest: str | None, digest: str) -> bool:
    return digest != last_digest


def sse_frame(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def make_handler(state_root, *, window_hours=24, include_all=False, sessions=(), cost_log=None,
                 transcripts_root=None, locator=None, clock=time.time):
    """The request handler class bound to one set of read options. The server it runs under
    supplies ``stopping`` (an Event) and ``interval`` (seconds)."""
    state_root = Path(state_root)
    sessions = list(sessions)

    def snapshot() -> dict:
        return model.collect(state_root, now=dt.datetime.fromtimestamp(clock(), dt.timezone.utc),
                             window_hours=window_hours, include_all=include_all, sessions=sessions,
                             cost_log=cost_log)

    def overlay(current: dict) -> tuple[dict, list]:
        return attention.collect([t["session_id"] for t in current["tasks"]], locator, transcripts_root)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def _read(self):
            if not host_allowed(self.headers.get("Host")):
                return self._send(HTTPStatus.MISDIRECTED_REQUEST, TEXT, b"bad host\n")
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, "text/html; charset=utf-8", page_bytes())
            if path == "/api/snapshot":
                return self._send_json(snapshot())
            if path == "/api/attention":
                return self._send_json(attention.event_payload(*overlay(snapshot())))
            if path.startswith("/api/task/"):
                rest = urllib.parse.unquote(path[len("/api/task/"):])
                if rest.endswith("/chat"):
                    return self._task_chat(rest[: -len("/chat")])
                return self._task(rest)
            if path.startswith("/api/session/") and path.endswith("/chat"):
                return self._session_chat(urllib.parse.unquote(path[len("/api/session/"): -len("/chat")]))
            if path == "/api/events":
                return self._events()
            return self._send(HTTPStatus.NOT_FOUND, TEXT, b"not found\n")

        def _refuse(self):
            self.close_connection = True
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, TEXT, b"read-only\n", {"Allow": ", ".join(READ_METHODS)})

        do_GET = do_HEAD = _read
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def _task(self, session_id: str):
            if store.safe_session_id(session_id) != session_id:
                return self._send(HTTPStatus.NOT_FOUND, TEXT, b"no such session\n")
            try:
                payload = model.detail(state_root, session_id, cost_log=cost_log, transcripts_root=transcripts_root)
            except model.UnknownSession:
                return self._send(HTTPStatus.NOT_FOUND, TEXT, b"no such session\n")
            self._send_json(payload)

        def _task_chat(self, session_id: str):
            if store.safe_session_id(session_id) != session_id or not (state_root / f"{session_id}.json").is_file():
                return self._send(HTTPStatus.NOT_FOUND, TEXT, b"no such session\n")
            self._send_json(attention.task_chat(session_id, locator, transcripts_root))

        def _session_chat(self, harness_session: str):
            rows = overlay(snapshot())[1] if store.safe_session_id(harness_session) == harness_session else []
            if not any(row["harness_session"] == harness_session for row in rows):
                return self._send(HTTPStatus.NOT_FOUND, TEXT, b"no such session\n")
            self._send_json(attention.chat_tail(harness_session, locator, transcripts_root))

        def _events(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            last, last_attention, last_write = None, None, time.monotonic()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while not self.server.stopping.is_set():
                    current = snapshot()
                    digest = model.content_digest(current)
                    payload = attention.event_payload(*overlay(current))
                    sent = False
                    if event_due(last, digest):
                        self.wfile.write(sse_frame("snapshot", model.to_json(current)))
                        last, sent = digest, True
                    if event_due(last_attention, payload["digest"]):
                        self.wfile.write(sse_frame("attention", model.to_json(payload)))
                        last_attention, sent = payload["digest"], True
                    if sent:
                        last_write = time.monotonic()
                    elif time.monotonic() - last_write >= HEARTBEAT_S:
                        self.wfile.write(b": keepalive\n\n")
                        last_write = time.monotonic()
                    self.wfile.flush()
                    self.server.stopping.wait(self.server.interval)
            except OSError:
                return

        def _send_json(self, payload):
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", model.to_json(payload).encode("utf-8"))

        def _send(self, status, content_type: str, body: bytes, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, interval: float):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.stopping = threading.Event()
        self.interval = interval
        super().__init__(address, handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


def serve(state_root, host=LOOPBACK, port=0, *, interval=DEFAULT_INTERVAL_S, window_hours=24,
          include_all=False, sessions=(), cost_log=None, transcripts_root=None, locator=None, clock=time.time,
          ready=None) -> None:
    """Serve until shut down. ``ready(server)`` is called once the socket is listening."""
    check_bind(host)
    handler = make_handler(state_root, window_hours=window_hours, include_all=include_all, sessions=sessions,
                           cost_log=cost_log, transcripts_root=transcripts_root, locator=locator, clock=clock)
    server = Server((host, port), handler, interval)
    try:
        if ready:
            ready(server)
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


def serve_in_thread(state_root, host=LOOPBACK, port=0, **options) -> Server:
    """``serve`` on a daemon thread; returns the listening server, or raises what ``serve`` raised."""
    ready, failure, holder = threading.Event(), [], []

    def run():
        try:
            serve(state_root, host, port, ready=lambda server: (holder.append(server), ready.set()), **options)
        except BaseException as exc:
            failure.append(exc)
            ready.set()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    if failure:
        raise failure[0]
    if not holder:
        raise TimeoutError("the web server did not start")
    return holder[0]


def head_events_end_with_the_headers(port: int, timeout: float = 5.0) -> bool:
    """HEAD /api/events on a raw socket: the server must close right after the blank line ending
    the headers, which ``http.client`` would hide because it never reads a HEAD body."""
    with socket.create_connection((LOOPBACK, port), timeout=timeout) as conn:
        conn.sendall(f"HEAD /api/events HTTP/1.1\r\nHost: {LOOPBACK}:{port}\r\n\r\n".encode("ascii"))
        data = b""
        try:
            while chunk := conn.recv(4096):
                data += chunk
        except OSError:
            return False
    head, separator, rest = data.partition(b"\r\n\r\n")
    return bool(separator) and head.startswith(b"HTTP/1.1 200") and rest == b""


def self_test(state_root, **options) -> list[str]:
    """Serve ``state_root`` read-only on a free port and probe it; returns the failures. An
    empty or missing state directory is fine: the probes do not need a particular task."""
    failures = []

    def check(label, ok):
        if not ok:
            failures.append(label)

    server = serve_in_thread(state_root, interval=0.05, **options)
    try:
        def fetch(method, path, host=None, *, send_host=True, first_event=False):
            conn = http.client.HTTPConnection(LOOPBACK, server.port, timeout=5)
            conn.putrequest(method, path, skip_host=True)
            if send_host:
                conn.putheader("Host", host or f"{LOOPBACK}:{server.port}")
            conn.endheaders()
            response = conn.getresponse()
            if first_event:
                body = b"".join(response.fp.readline() for _ in range(4))
            else:
                body = response.read()
            conn.close()
            return response.status, body

        status, body = fetch("GET", "/")
        check("page", status == 200 and b"<html" in body)
        status, body = fetch("GET", "/api/snapshot")
        check("snapshot", status == 200 and b'"tasks"' in body)
        check("head events", head_events_end_with_the_headers(server.port))
        check("first event", b"event: snapshot" in fetch("GET", "/api/events", first_event=True)[1])
        check("bad host", fetch("GET", "/", host="evil.example")[0] == 421)
        check("missing host", fetch("GET", "/", send_host=False)[0] == 421)
        check("post", fetch("POST", "/api/snapshot")[0] == 405)
    finally:
        server.shutdown()
        server.server_close()
    return failures
