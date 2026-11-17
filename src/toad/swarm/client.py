"""A headless ACP client that serves many sessions over one agent process.

The UI's `toad.acp.agent.Agent` binds one process to one conversation. A swarm
needs many sessions without widgets, so this client keeps its own JSON-RPC
plumbing and routes `session/update` by `sessionId` to a per-session handler.
towa serves many sessions from one process, so every worker shares it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import toad

PROTOCOL_VERSION = 1

JSON = dict[str, Any]


class SessionHandler(Protocol):
    def on_update(self, update: JSON) -> None: ...


class ACPError(Exception):
    def __init__(self, error: JSON) -> None:
        self.error = error
        message = str(error.get("message", "agent error"))
        data = error.get("data")
        if isinstance(data, dict) and data.get("details"):
            message = f"{message}: {data['details']}"
        super().__init__(message)


def choose_permission(options: list[JSON]) -> str | None:
    """Pick the option an unattended worker answers a permission request with."""
    for kind in ("allow_once", "allow_always"):
        for option in options:
            if option.get("kind") == kind:
                return option.get("optionId")
    return options[0].get("optionId") if options else None


class ACPClient:
    """One ACP agent process, many sessions."""

    def __init__(
        self,
        command: str,
        cwd: Path,
        *,
        stream_factory: Callable[[], Awaitable[tuple[Any, Any]]] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self._stream_factory = stream_factory
        self._process: asyncio.subprocess.Process | None = None
        self._reader: Any = None
        self._writer: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handlers: dict[str, SessionHandler] = {}
        self._read_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._started = asyncio.Lock()
        self.ready = False
        self.agent_capabilities: JSON = {}

    async def start(self) -> None:
        async with self._started:
            if self.ready:
                return
            if self._stream_factory is not None:
                self._reader, self._writer = await self._stream_factory()
            else:
                env = os.environ.copy()
                env["TOAD_CWD"] = str(self.cwd)
                self._process = await asyncio.create_subprocess_shell(
                    self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(self.cwd),
                    env=env,
                    limit=10 * 1024 * 1024,
                )
                self._reader, self._writer = self._process.stdout, self._process.stdin
            self._read_task = asyncio.create_task(self._read_loop())
            response = await self.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": False,
                    },
                    "clientInfo": {
                        "name": toad.NAME,
                        "title": f"{toad.TITLE} swarm",
                        "version": toad.get_version(),
                    },
                },
            )
            self.agent_capabilities = response.get("agentCapabilities") or {}
            self.ready = True

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), 5)
            except (OSError, asyncio.TimeoutError):
                pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent closed"))
        self._pending.clear()
        self.ready = False

    def _write(self, message: JSON) -> None:
        if self._writer is None:
            raise ConnectionError("agent is not running")
        self._writer.write(json.dumps(message).encode("utf-8") + b"\n")

    async def call(self, method: str, params: JSON) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def notify(self, method: str, params: JSON) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def new_session(
        self, handler: SessionHandler, mcp_servers: list[JSON] | None = None
    ) -> str:
        await self.start()
        response = await self.call(
            "session/new", {"cwd": str(self.cwd), "mcpServers": mcp_servers or []}
        )
        session_id = response["sessionId"]
        self._handlers[session_id] = handler
        return session_id

    async def prompt(self, session_id: str, text: str) -> str | None:
        response = await self.call(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )
        return (response or {}).get("stopReason")

    def cancel(self, session_id: str) -> None:
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except ConnectionError:
            pass

    def forget(self, session_id: str) -> None:
        self._handlers.pop(session_id, None)

    async def _read_loop(self) -> None:
        try:
            while line := await self._reader.readline():
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" not in message:
                    self._resolve(message)
                else:
                    task = asyncio.create_task(self._handle_request(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("agent exited"))
            self.ready = False

    def _resolve(self, message: JSON) -> None:
        future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(ACPError(message["error"] or {}))
        else:
            future.set_result(message.get("result"))

    async def _handle_request(self, message: JSON) -> None:
        method = message["method"]
        params = message.get("params") or {}
        request_id = message.get("id")
        try:
            result = self._dispatch(method, params)
        except Exception as error:  # reply with an error, never crash the loop
            if request_id is not None:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": str(error)},
                    }
                )
            return
        if request_id is not None:
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _path(self, path: str) -> Path:
        return self.cwd / path

    def _dispatch(self, method: str, params: JSON) -> Any:
        match method:
            case "session/update":
                handler = self._handlers.get(params.get("sessionId", ""))
                if handler is not None:
                    handler.on_update(params.get("update") or {})
                return None
            case "session/request_permission":
                option_id = choose_permission(params.get("options") or [])
                if option_id is None:
                    return {"outcome": {"outcome": "cancelled"}}
                return {"outcome": {"outcome": "selected", "optionId": option_id}}
            case "fs/read_text_file":
                try:
                    text = self._path(params["path"]).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                except OSError:
                    text = ""
                line, limit = params.get("line"), params.get("limit")
                if line is not None:
                    lines = text.splitlines()[max(0, line - 1) :]
                    text = "\n".join(lines[:limit] if limit is not None else lines)
                return {"content": text}
            case "fs/write_text_file":
                self._path(params["path"]).write_text(params["content"], "utf-8")
                return None
        raise LookupError(f"method not found: {method}")
