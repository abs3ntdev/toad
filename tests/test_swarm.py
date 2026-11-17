import asyncio
import json
import sys
from pathlib import Path

import pytest

from toad.swarm.client import ACPClient, choose_permission
from toad.swarm.manager import SwarmError, SwarmManager
from toad.swarm.mcp_bridge import Bridge
from toad.swarm.tools import SwarmToolServer, call_tool

FAKE = Path(__file__).parent / "fake_acp_agent.py"


def make_client(tmp_path):
    return ACPClient(f"{sys.executable} {FAKE}", tmp_path)


def test_choose_permission():
    opts = [{"kind": "reject_once", "optionId": "r"}, {"kind": "allow_once", "optionId": "a"}]
    assert choose_permission(opts) == "a"
    assert choose_permission([]) is None


@pytest.mark.asyncio
async def test_two_workers_deliver_reports(tmp_path):
    client = make_client(tmp_path)
    delivered: list[str] = []
    manager = SwarmManager(client, deliver=delivered.append)
    try:
        await manager.spawn("alpha", "say alpha")
        await manager.spawn("beta", "say beta")
        assert {w.session_id for w in manager.workers.values()} == {"s1", "s2"}
        for _ in range(100):
            if len(delivered) == 2:
                break
            await asyncio.sleep(0.02)
        assert sorted(delivered) == [
            "[swarm] worker 'alpha' finished:\n\ndone: say alpha",
            "[swarm] worker 'beta' finished:\n\ndone: say beta",
        ]
        status = {s["name"]: s for s in manager.status()}
        assert status["alpha"]["status"] == "idle"
        assert status["alpha"]["plan"] == "1/2"
        assert status["beta"]["latest"] == "done: say beta"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_await_suppresses_delivery_and_message(tmp_path):
    client = make_client(tmp_path)
    delivered: list[str] = []
    manager = SwarmManager(client, deliver=delivered.append)
    try:
        await call_tool(manager, "swarm_spawn", {"name": "w", "task": "slow job"})
        text = await call_tool(manager, "swarm_await", {})
        assert "done: slow job" in text
        await asyncio.sleep(0.05)
        assert delivered == []
        await call_tool(manager, "swarm_message", {"name": "w", "message": "again"})
        await manager.workers["w"]._turn_task
        assert delivered == ["[swarm] worker 'w' finished:\n\ndone: again"]
        assert manager.workers["w"].turns == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_errors_and_stop(tmp_path):
    client = make_client(tmp_path)
    delivered: list[str] = []
    manager = SwarmManager(client, deliver=delivered.append)
    try:
        with pytest.raises(SwarmError):
            await manager.spawn("bad name!", "x")
        await manager.spawn("f", "please fail")
        await manager.workers["f"]._turn_task
        assert manager.workers["f"].status == "failed"
        assert "boom" in delivered[0]
        await manager.spawn("s", "slow")
        await manager.stop("s")
        assert manager.workers["s"].status == "stopped"
        with pytest.raises(SwarmError):
            await manager.message("s", "hi")
        with pytest.raises(SwarmError):
            await manager.message("nobody", "hi")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_bridge_round_trip(tmp_path):
    client = make_client(tmp_path)
    manager = SwarmManager(client)
    server = SwarmToolServer(manager)
    await server.start()
    try:
        entry = server.mcp_server()
        proc = await asyncio.create_subprocess_exec(
            entry["command"], *entry["args"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )

        async def rpc(id, method, params=None):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id, "method": method,
                                         "params": params or {}}).encode() + b"\n")
            await proc.stdin.drain()
            return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))

        init = await rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
        assert init["result"]["serverInfo"]["name"] == "toad-swarm"
        tools = await rpc(2, "tools/list")
        assert {t["name"] for t in tools["result"]["tools"]} >= {
            "swarm_spawn", "swarm_message", "swarm_status", "swarm_await"}
        spawn = await rpc(3, "tools/call", {"name": "swarm_spawn",
                                            "arguments": {"name": "m", "task": "hello"}})
        assert not spawn["result"]["isError"]
        awaited = await rpc(4, "tools/call", {"name": "swarm_await", "arguments": {}})
        assert "done: hello" in awaited["result"]["content"][0]["text"]
        bad = await rpc(5, "tools/call", {"name": "swarm_stop", "arguments": {"name": "zz"}})
        assert bad["result"]["isError"]
        proc.stdin.close()
        await proc.wait()
    finally:
        await server.close()
        await client.close()


def test_bridge_unavailable():
    reply = Bridge("/nonexistent/sock").forward("swarm_status", {})
    assert reply["isError"]


@pytest.mark.asyncio
async def test_swarm_screen_lists_and_switches(tmp_path):
    from textual.app import App
    from textual.widgets import DataTable, Static

    from toad.screens.swarm import SwarmScreen
    from toad.swarm.controller import list_markdown

    client = make_client(tmp_path)
    manager = SwarmManager(client)
    try:
        assert "No swarm workers" in list_markdown(manager)
        await manager.spawn("one", "task one")
        await manager.spawn("two", "task two")
        await manager.wait()
        assert "| two | idle | 1/2 | done: task two |" in list_markdown(manager)

        class A(App):
            def on_mount(self):
                self.push_screen(SwarmScreen(manager))

        async with A().run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one(DataTable)
            assert table.row_count == 2
            body = str(screen.query_one("#swarm-transcript-body", Static).render())
            assert "done: task one" in body
            table.move_cursor(row=1)
            await pilot.pause()
            body = str(screen.query_one("#swarm-transcript-body", Static).render())
            assert "done: task two" in body and "Plan" in body
    finally:
        await client.close()
