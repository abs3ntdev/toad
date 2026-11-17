"""Subagent tree, driven by updates shaped like towa's `subagent` tool."""

import asyncio
import json

from textual.app import App, ComposeResult

from toad.widgets.subagents import SubagentTracker, SubagentTree, is_subagent_call

ID = "call_1"
ANNOUNCE = {"sessionUpdate": "tool_call", "toolCallId": ID, "name": "subagent",
            "title": "subagent", "kind": "other", "status": "pending"}
WHOLE = {"sessionUpdate": "tool_call", "toolCallId": ID, "name": "subagent",
         "title": "Find TODOs", "kind": "other", "status": "pending",
         "rawInput": {"prompt": "Grep for TODO in src", "model": "fast", "description": "Find TODOs"}}
RUNNING = {"sessionUpdate": "tool_call_update", "toolCallId": ID, "status": "in_progress"}
PROGRESS = {"sessionUpdate": "tool_call_update", "toolCallId": ID, "content": [
    {"type": "content", "content": {"type": "text", "text": "Looking."}},
    {"type": "content", "content": {"type": "text", "text": "grep TODO src"}}]}
DONE = {"sessionUpdate": "tool_call_update", "toolCallId": ID, "status": "completed", "content": [
    {"type": "content", "content": {"type": "text", "text": json.dumps(
        {"final": "3 TODOs found", "session_id": "abc", "charged": True})}}]}
FAILED = {"sessionUpdate": "tool_call_update", "toolCallId": ID, "status": "failed", "content": [
    {"type": "content", "content": {"type": "text", "text": json.dumps(
        {"charged": False, "error": {"kind": "too_deep", "message": "depth 2 over 1"}})}}]}


def test_detection():
    assert is_subagent_call(ANNOUNCE)
    assert is_subagent_call({"toolCallId": "x", "rawInput": {"prompt": "p"}})
    assert not is_subagent_call({"toolCallId": "x", "name": "read", "rawInput": {"path": "a"}})
    assert not is_subagent_call({"toolCallId": "x", "kind": "execute", "rawInput": {"prompt": "p"}})


def test_lifecycle():
    t = SubagentTracker()
    assert t.update({"toolCallId": "other", "name": "bash", "status": "pending"}) is None
    for u in (ANNOUNCE, WHOLE, RUNNING, PROGRESS):
        a = t.update(u)
    assert (a.title, a.task, a.model, a.status) == ("Find TODOs", "Grep for TODO in src", "fast", "in_progress")
    assert a.latest == "grep TODO src"
    a = t.update(DONE)
    assert (a.status, a.final, a.session_id) == ("completed", "3 TODOs found", "abc")


def test_failure():
    t = SubagentTracker()
    t.update(WHOLE)
    a = t.update(FAILED)
    assert a.status == "failed" and a.latest == "error: depth 2 over 1"


def test_tree_widget():
    class TreeApp(App):
        def compose(self) -> ComposeResult:
            yield SubagentTree()

    async def run():
        app = TreeApp()
        async with app.run_test() as pilot:
            tree = app.query_one(SubagentTree)
            assert not tree.update_tool_call({"toolCallId": "z", "name": "bash"})
            for u in (WHOLE, RUNNING, PROGRESS):
                assert tree.update_tool_call(u)
            await pilot.pause()
            node = tree.root.children[0]
            labels = [str(c.label) for c in node.children]
            assert "◐ Find TODOs [fast] · in_progress" == str(node.label)
            assert "latest: grep TODO src" in labels
            assert "Subagents (1 running / 1)" == tree.border_title
            tree.update_tool_call(DONE)
            assert not node.is_expanded
            assert "latest: 3 TODOs found" in [str(c.label) for c in node.children]

    asyncio.run(run())
