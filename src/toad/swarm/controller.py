"""Glue between a `Conversation` (the coordinator) and its swarm.

The conversation calls in at four points: before its agent starts (to name the
swarm MCP server on `session/new`), on `/swarm` slash commands, when an agent
turn ends (to send queued worker reports), and on unmount.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

import toad
from toad.acp.protocol import McpServer
from toad.swarm.client import ACPClient
from toad.swarm.manager import SwarmError, SwarmManager
from toad.swarm.tools import SwarmToolServer

if TYPE_CHECKING:
    from toad.widgets.conversation import Conversation

# Agents known to accept stdio servers in session/new mcpServers.
MCP_AGENTS = frozenset({"towa.gfx-labs.com"})

USAGE = """\
**/swarm** runs worker agents in their own sessions.

- `/swarm spawn NAME TASK` start a worker
- `/swarm list` workers with status and plan progress
- `/swarm msg NAME TEXT` send a worker a follow-up
- `/swarm stop NAME` stop a worker (`all` stops every worker)
- `/swarm view` open the swarm screen

Worker reports are sent to this conversation's agent when they finish.
"""


class SwarmController:
    def __init__(self, conversation: Conversation, command: str, identity: str) -> None:
        self.conversation = conversation
        self.identity = identity
        self.client = ACPClient(command, Path(conversation.project_path))
        self.manager = SwarmManager(self.client, deliver=self._deliver)
        self.tools = SwarmToolServer(self.manager)
        self.pending: list[str] = []
        self._tools_started = False

    @classmethod
    def for_conversation(cls, conversation: Conversation) -> SwarmController | None:
        agent_data = conversation._agent_data
        if agent_data is None or agent_data.get("protocol") != "acp":
            return None
        command = toad.get_os_matrix(agent_data["run_command"])
        if not command:
            return None
        return cls(conversation, command, agent_data["identity"])

    async def mcp_servers(self) -> list[McpServer]:
        """Servers for the coordinator's session/new (empty if unsupported)."""
        if self.identity not in MCP_AGENTS:
            return []
        if not self._tools_started:
            await self.tools.start()
            self._tools_started = True
        return [self.tools.mcp_server()]

    async def close(self) -> None:
        await self.manager.stop_all()
        await self.client.close()
        await self.tools.close()

    # Delivery -----------------------------------------------------------

    def _deliver(self, report: str) -> None:
        # The report itself is rebuilt at flush time: a swarm_await call made
        # before then may have claimed it, and must not get it twice.
        self.conversation.call_later(self.flush)

    def flush(self) -> None:
        """Send queued reports as one prompt when the coordinator is free."""
        conversation = self.conversation
        if conversation.turn == "agent" or conversation.agent is None:
            return
        reports = self.manager.take_unreported()
        if not reports:
            return
        conversation.call_later(conversation.prompt_agent, "\n\n---\n\n".join(reports))

    # Slash command --------------------------------------------------------

    async def slash(self, parameters: str) -> None:
        from toad.widgets.markdown_note import MarkdownNote

        try:
            note = await self._slash(parameters)
        except SwarmError as error:
            self.conversation.notify(str(error), title="/swarm", severity="error")
            return
        if note:
            await self.conversation.post(MarkdownNote(note))

    async def _slash(self, parameters: str) -> str | None:
        verb, _, rest = parameters.strip().partition(" ")
        rest = rest.strip()
        match verb:
            case "spawn":
                name, _, task = rest.partition(" ")
                worker = await self.manager.spawn(name, task.strip())
                return f"Spawned worker **{worker.name}**."
            case "msg" | "message":
                name, _, text = rest.partition(" ")
                if not text.strip():
                    raise SwarmError("usage: /swarm msg NAME TEXT")
                self.manager.get(name)
                self.conversation.run_worker(self.manager.message(name, text.strip()))
                return f"Sent message to **{name}**."
            case "stop":
                names = list(self.manager.workers) if rest == "all" else shlex.split(rest)
                if not names:
                    raise SwarmError("usage: /swarm stop NAME")
                for name in names:
                    await self.manager.stop(name)
                return "Stopped " + ", ".join(f"**{n}**" for n in names) + "."
            case "list" | "status":
                return list_markdown(self.manager)
            case "view" | "":
                from toad.screens.swarm import SwarmScreen

                self.conversation.app.push_screen(SwarmScreen(self.manager))
                return None
        return USAGE


def list_markdown(manager: SwarmManager) -> str:
    if not manager.workers:
        return "No swarm workers. Start one with `/swarm spawn NAME TASK`."
    rows = ["| worker | status | plan | latest |", "|---|---|---|---|"]
    for s in manager.status():
        latest = s["latest"].replace("|", "\\|")[:80]
        rows.append(f"| {s['name']} | {s['status']} | {s['plan']} | {latest} |")
    return "\n".join(rows)
