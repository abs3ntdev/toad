"""A tree of the child agents (subagents) an agent has started.

towa runs a child agent through its `subagent` tool. The child's activity
streams as updates to that one tool call: `rawInput` holds the task
(`prompt`, `model`, `description`), `content` is replaced with a growing list
of text blocks (the child's text and one line per tool call it starts), and
the final content is a JSON result with `final`, `session_id` and `error`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from rich.text import Text
from textual.widgets import Tree

SUBAGENT_TOOL_NAMES = frozenset({"subagent", "task", "spawn_agent"})


def is_subagent_call(tool_call: Mapping[str, Any]) -> bool:
    """Is the tool call (or update) a subagent spawn?"""
    name = tool_call.get("name")
    if isinstance(name, str) and name in SUBAGENT_TOOL_NAMES:
        return True
    meta = tool_call.get("_meta") or {}
    if isinstance(meta, dict) and meta.get("toolName") in SUBAGENT_TOOL_NAMES:
        return True
    raw_input = tool_call.get("rawInput")
    # Fallback for agents that send no name: the subagent tool's input shape.
    return (
        isinstance(raw_input, dict)
        and "prompt" in raw_input
        and tool_call.get("kind") in (None, "other")
        and set(raw_input) <= {"prompt", "model", "description", "intent"}
    )


def _text_blocks(content: Any) -> list[str]:
    texts: list[str] = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        block = item.get("content", item)
        if isinstance(block, dict) and block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                texts.append(text)
    return texts


@dataclass
class Subagent:
    """State of one child agent."""

    tool_call_id: str
    title: str = "subagent"
    task: str = ""
    model: str = ""
    status: str = "pending"
    activity: list[str] = field(default_factory=list)
    final: str = ""
    error: str = ""
    session_id: str = ""

    @property
    def latest(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if self.final:
            return self.final
        return self.activity[-1] if self.activity else ""

    def apply(self, tool_call: Mapping[str, Any]) -> None:
        """Merge a tool_call or tool_call_update into this state."""
        raw_input = tool_call.get("rawInput")
        if isinstance(raw_input, dict):
            self.task = raw_input.get("prompt") or self.task
            self.model = raw_input.get("model") or self.model
            description = raw_input.get("description") or raw_input.get("intent")
            if description:
                self.title = description
        if title := tool_call.get("title"):
            if title not in SUBAGENT_TOOL_NAMES or self.title == "subagent":
                self.title = title
        if status := tool_call.get("status"):
            self.status = status
        content = tool_call.get("content")
        if content is None:
            return
        texts = _text_blocks(content)
        if self.status in ("completed", "failed") and texts:
            if self._read_result(texts[-1]):
                return
        self.activity = texts

    def _read_result(self, text: str) -> bool:
        try:
            result = json.loads(text)
        except (ValueError, TypeError):
            return False
        if not isinstance(result, dict):
            return False
        self.final = result.get("final") or ""
        self.session_id = result.get("session_id") or ""
        error = result.get("error")
        if isinstance(error, dict):
            self.error = error.get("message") or error.get("kind") or "failed"
            self.status = "failed"
        return True


class SubagentTracker:
    """Pure state for the tree, keyed by tool call id."""

    def __init__(self) -> None:
        self.agents: dict[str, Subagent] = {}

    def update(self, tool_call: Mapping[str, Any]) -> Subagent | None:
        """Apply an update. Returns the agent it touched, or None if unrelated."""
        tool_call_id = tool_call.get("toolCallId")
        if not tool_call_id:
            return None
        agent = self.agents.get(tool_call_id)
        if agent is None:
            if not is_subagent_call(tool_call):
                return None
            agent = self.agents[tool_call_id] = Subagent(tool_call_id)
        agent.apply(tool_call)
        return agent


STATUS_ICONS = {
    "pending": "○",
    "in_progress": "◐",
    "completed": "●",
    "failed": "✗",
}


def _one_line(text: str, width: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


class SubagentTree(Tree[str]):
    """Collapsible tree of child agents with task, model, status and activity."""

    DEFAULT_CLASSES = "block"
    DEFAULT_CSS = """
    SubagentTree {
        height: auto;
        max-height: 20;
        border-top: ascii $secondary;
        border-bottom: ascii $secondary;
        background: black 10%;
        margin: 1 0 1 0 !important;
        padding: 0 1;
        border-title-align: center;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("Subagents", **kwargs)
        self.tracker = SubagentTracker()
        self.show_root = False
        self.border_title = "Subagents"

    def update_tool_call(self, tool_call: Mapping[str, Any]) -> bool:
        """Apply a tool call update. Returns True if it was a subagent."""
        agent = self.tracker.update(tool_call)
        if agent is None:
            return False
        self._render_agent(agent)
        return True

    def label_for(self, agent: Subagent) -> Text:
        icon = STATUS_ICONS.get(agent.status, "?")
        model = f" [{agent.model}]" if agent.model else ""
        return Text(f"{icon} {_one_line(agent.title, 60)}{model} · {agent.status}")

    def _render_agent(self, agent: Subagent) -> None:
        node = next(
            (child for child in self.root.children if child.data == agent.tool_call_id),
            None,
        )
        label = self.label_for(agent)
        if node is None:
            node = self.root.add(label, data=agent.tool_call_id, expand=True)
        else:
            node.set_label(label)
            node.remove_children()
        if agent.task:
            node.add_leaf(Text(f"task: {_one_line(agent.task)}"))
        node.add_leaf(Text(f"model: {agent.model or 'inherited'}"))
        if latest := agent.latest:
            node.add_leaf(Text(f"latest: {_one_line(latest)}"))
        if len(agent.activity) > 1 and not agent.final:
            history = node.add(f"activity ({len(agent.activity)})", expand=False)
            for line in agent.activity[-20:]:
                history.add_leaf(Text(_one_line(line)))
        if agent.session_id:
            node.add_leaf(Text(f"session: {agent.session_id}"))
        if agent.status in ("completed", "failed"):
            node.collapse()
        self.border_title = self._summary()

    def _summary(self) -> str:
        agents = self.tracker.agents.values()
        running = sum(a.status in ("pending", "in_progress") for a in agents)
        return f"Subagents ({running} running / {len(self.tracker.agents)})"
