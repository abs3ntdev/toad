"""The swarm tools the coordinator agent calls, served to it over MCP.

towa connects to MCP servers a client names in `session/new` `mcpServers`
and puts their tools in the prompt directly. Only the stdio transport is
certain to be supported there, so toad names a small stdio bridge
(`python -m toad.swarm.mcp_bridge <socket>`). The bridge speaks MCP to the
agent and forwards each `tools/call` as one JSON line to a unix socket that
`SwarmToolServer` listens on inside toad, where the `SwarmManager` lives.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from toad.acp.protocol import McpServer
from toad.swarm.manager import SwarmError, SwarmManager, format_report

TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_spawn",
        "description": (
            "Start a named worker agent in its own session with a task. It runs "
            "in parallel; its final report is delivered to you when it finishes "
            "(or returned by swarm_await)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique worker name"},
                "task": {"type": "string", "description": "Self-contained task"},
            },
            "required": ["name", "task"],
        },
    },
    {
        "name": "swarm_message",
        "description": "Send a follow-up message to a worker, starting a new turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["name", "message"],
        },
    },
    {
        "name": "swarm_status",
        "description": "List workers with status, latest message and plan progress.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_await",
        "description": (
            "Wait for workers to finish their current turn and return their "
            "reports. Defaults to all running workers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "number"},
            },
        },
    },
    {
        "name": "swarm_stop",
        "description": "Stop a worker.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
]


async def call_tool(manager: SwarmManager, name: str, args: dict[str, Any]) -> str:
    """Run one swarm tool and return its text result. Raises SwarmError."""
    match name:
        case "swarm_spawn":
            worker = await manager.spawn(str(args.get("name", "")), str(args.get("task", "")))
            return f"spawned worker '{worker.name}' (session {worker.session_id})"
        case "swarm_message":
            worker = await manager.message(
                str(args.get("name", "")), str(args.get("message", ""))
            )
            return f"sent message to '{worker.name}'"
        case "swarm_status":
            return json.dumps(manager.status(), indent=1) if manager.workers else "no workers"
        case "swarm_await":
            names = args.get("names") or None
            timeout = args.get("timeout_seconds")
            workers = await manager.wait(names, float(timeout) if timeout else None)
            if not workers:
                return "no running workers"
            parts = []
            for worker in workers:
                if worker._done.is_set():
                    parts.append(format_report(worker))
                else:
                    parts.append(f"[swarm] worker '{worker.name}' still {worker.status}")
            return "\n\n".join(parts)
        case "swarm_stop":
            worker = await manager.stop(str(args.get("name", "")))
            return f"stopped '{worker.name}'"
    raise SwarmError(f"unknown tool {name!r}")


class SwarmToolServer:
    """Unix socket the MCP bridge forwards tool calls to."""

    def __init__(self, manager: SwarmManager) -> None:
        self.manager = manager
        self.path: Path | None = None
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="toad-swarm-"))
        self.path = directory / "tools.sock"
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        os.chmod(self.path, 0o600)
        return self.path

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        if self.path is not None:
            for p in (self.path, self.path.parent):
                try:
                    p.unlink() if p.is_socket() else p.rmdir()
                except OSError:
                    pass

    def mcp_server(self, name: str = "toad_swarm") -> McpServer:
        """The `mcpServers` entry for session/new."""
        assert self.path is not None, "start() first"
        return {
            "name": name,
            "command": sys.executable,
            "args": ["-m", "toad.swarm.mcp_bridge", str(self.path)],
            "env": [],
        }

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                    text = await call_tool(
                        self.manager, request["name"], request.get("arguments") or {}
                    )
                    reply = {"text": text, "isError": False}
                except (SwarmError, KeyError, ValueError) as error:
                    reply = {"text": str(error), "isError": True}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
