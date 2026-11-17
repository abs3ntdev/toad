"""Stdio MCP server that forwards swarm tool calls to toad.

Run by the agent: `python -m toad.swarm.mcp_bridge <unix socket path>`.
Implements the small part of MCP a tool server needs (initialize, tools/list,
tools/call, ping) with no dependency beyond the standard library.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


def _tools() -> list[dict[str, Any]]:
    from toad.swarm.tools import TOOLS

    return TOOLS


class Bridge:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._sock: socket.socket | None = None
        self._file: Any = None

    def _connect(self) -> None:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self.socket_path)
            self._file = self._sock.makefile("rwb")

    def forward(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self._connect()
            self._file.write(json.dumps({"name": name, "arguments": arguments}).encode() + b"\n")
            self._file.flush()
            line = self._file.readline()
            if not line:
                raise ConnectionError("toad closed the swarm socket")
            reply = json.loads(line)
        except (OSError, ValueError) as error:
            self._sock = None
            reply = {"text": f"swarm unavailable: {error}", "isError": True}
        return {
            "content": [{"type": "text", "text": reply.get("text", "")}],
            "isError": bool(reply.get("isError")),
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            return None  # notification
        match method:
            case "initialize":
                result: Any = {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "toad-swarm", "version": "1"},
                }
            case "tools/list":
                result = {"tools": _tools()}
            case "tools/call":
                result = self.forward(params.get("name", ""), params.get("arguments") or {})
            case "ping":
                result = {}
            case _:
                return {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m toad.swarm.mcp_bridge SOCKET", file=sys.stderr)
        return 2
    bridge = Bridge(argv[0])
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if (reply := bridge.handle(message)) is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
