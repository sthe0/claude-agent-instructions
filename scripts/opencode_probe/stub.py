"""A scripted OpenAI-compatible chat-completions server: the only model the probe suite uses.

The scenario file is JSON: ``{"models": {"<model id>": [turn, ...]}}``. Each request
that carries tools consumes the next turn of its model (an exhausted script answers
``"done"``). A turn is ``{"text": "..."}`` or ``{"tool_calls": [{"name": n, "arguments": {...}}]}``
(both may be present); ``"error": <http status>`` answers that status instead and ``"hang": <seconds>``
delays the answer. Requests without tools (title generation) get a fixed reply and
consume nothing. Every request is appended to ``requests.jsonl`` beside the scenario file.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLAIN_REPLY = "stub title"
EXHAUSTED_REPLY = "done"


class StubState:
    def __init__(self, scenario_path: Path):
        self.scenario_path = Path(scenario_path)
        self.requests_path = self.scenario_path.parent / "requests.jsonl"
        self.lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        """Read the scenario file again (the harness writes it once the port is known)."""
        with self.lock:
            self.scenario = json.loads(self.scenario_path.read_text(encoding="utf-8"))
            self.cursor: dict[str, int] = {}

    def next_turn(self, model: str, has_tools: bool) -> tuple[dict, int]:
        if not has_tools:
            return {"text": PLAIN_REPLY}, -1
        with self.lock:
            index = self.cursor.get(model, 0)
            self.cursor[model] = index + 1
        script = self.scenario.get("models", {}).get(model, [])
        return (script[index] if index < len(script) else {"text": EXHAUSTED_REPLY}), index

    def log(self, entry: dict) -> None:
        with self.lock, self.requests_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def _chunk(model: str, delta: dict, finish: str | None, usage: dict | None = None) -> dict:
    out = {"id": "stub-1", "object": "chat.completion.chunk", "created": 0, "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


def _stream_events(model: str, turn: dict, turn_index: int) -> list[dict]:
    events = [_chunk(model, {"role": "assistant", "content": ""}, None)]
    if turn.get("text"):
        events.append(_chunk(model, {"content": turn["text"]}, None))
    calls = turn.get("tool_calls") or []
    for i, call in enumerate(calls):
        events.append(_chunk(model, {"tool_calls": [{
            "index": i, "id": f"call_{model}_{turn_index}_{i}", "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))}}]}, None))
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    events.append(_chunk(model, {}, "tool_calls" if calls else "stop", usage))
    return events


def _make_handler(state: StubState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.log({"get": self.path})
            self._send_json({"object": "list", "data": [
                {"id": m, "object": "model"} for m in state.scenario.get("models", {})]})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            model = body.get("model", "")
            tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
            turn, index = state.next_turn(model, bool(tools))
            state.log({"path": self.path, "model": model, "tools": tools, "turn": index,
                       "tool_specs": {t["function"]["name"]: t["function"].get("parameters")
                                      for t in body.get("tools") or []},
                       "tool_descriptions": {t["function"]["name"]: t["function"].get("description")
                                             for t in body.get("tools") or []},
                       "messages": body.get("messages"), "stream": bool(body.get("stream"))})
            if turn.get("hang"):
                time.sleep(float(turn["hang"]))
            if turn.get("error"):
                self._send_json({"error": {"message": "stub model error", "type": "stub_error"}},
                                int(turn["error"]))
                return
            events = _stream_events(model, turn, index)
            if not body.get("stream"):
                final = events[-1]
                message = {"role": "assistant", "content": turn.get("text") or None}
                calls = [e["choices"][0]["delta"]["tool_calls"][0] for e in events
                         if e["choices"][0]["delta"].get("tool_calls")]
                if calls:
                    message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
                self._send_json({"id": "stub-1", "object": "chat.completion", "created": 0,
                                 "model": model, "usage": final["usage"],
                                 "choices": [{"index": 0, "message": message,
                                              "finish_reason": final["choices"][0]["finish_reason"]}]})
                return
            payload = b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def start_stub(scenario_path: Path) -> tuple[ThreadingHTTPServer, int]:
    """Serve ``scenario_path`` on an ephemeral loopback port in a daemon thread."""
    state = StubState(scenario_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    server.daemon_threads = True
    server.stub_state = state
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def serve_forever(scenario_path: Path, port_file: Path) -> int:
    server, port = start_stub(scenario_path)
    Path(port_file).write_text(str(port), encoding="utf-8")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0
