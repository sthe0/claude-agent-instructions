"""A minimal stdio MCP server for the probe suite: one tool, ``touch``, that writes the marker
file named by argv[1]; the marker's existence is how a case knows the tool really ran."""
import json
import sys
from pathlib import Path

MARKER = Path(sys.argv[1])


def reply(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        reply(request_id, {"protocolVersion": message["params"].get("protocolVersion", "2024-11-05"),
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "probe-mcp", "version": "0"}})
    elif method == "tools/list":
        reply(request_id, {"tools": [{"name": "touch", "description": "write the probe marker",
                                      "inputSchema": {"type": "object", "properties": {}}}]})
    elif method == "tools/call":
        MARKER.write_text("ran", encoding="utf-8")
        reply(request_id, {"content": [{"type": "text", "text": "touched"}]})
    elif request_id is not None:
        reply(request_id, {})
