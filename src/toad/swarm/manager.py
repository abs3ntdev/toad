"""Swarm workers: named ACP sessions that run a task and report back.

A `SwarmManager` belongs to one coordinator conversation. Each worker is its
own ACP session on a shared `ACPClient` (one towa process, many sessions).
When a worker's turn ends its final message is reported to the coordinator,
either to a `swarm_await` call that is waiting for it or, when nothing waits,
through the `deliver` callback, which the UI turns into a coordinator prompt.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from toad.swarm.client import ACPClient

STATUSES = ("starting", "running", "idle", "failed", "stopped")
ACTIVE = ("starting", "running")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")

WORKER_PREAMBLE = (
    "You are swarm worker '{name}', one of several agents working in parallel "
    "under a coordinator. Do the task below directly, keep to its scope, and "
    "end your turn with a short report of what you did and what you observed.\n\n"
    "Task:\n{task}"
)


@dataclass
class TranscriptEntry:
    role: str  # "user", "agent", "thought", "tool", "system"
    text: str


@dataclass
class Worker:
    name: str
    task: str
    status: str = "starting"
    session_id: str | None = None
    transcript: list[TranscriptEntry] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    result: str = ""
    error: str = ""
    turns: int = 0
    reported_turns: int = 0
    stop_reason: str | None = None
    started: float = field(default_factory=time.monotonic)
    _current: list[str] = field(default_factory=list)
    _turn_task: asyncio.Task | None = None
    _done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def latest(self) -> str:
        for entry in reversed(self.transcript):
            if entry.role in ("agent", "system"):
                return entry.text.strip().splitlines()[-1] if entry.text.strip() else ""
        return ""

    @property
    def plan_progress(self) -> tuple[int, int]:
        done = sum(1 for e in self.plan if e.get("status") == "completed")
        return done, len(self.plan)

    def _append(self, role: str, text: str) -> None:
        if self.transcript and self.transcript[-1].role == role and role in (
            "agent",
            "thought",
        ):
            self.transcript[-1].text += text
        else:
            self.transcript.append(TranscriptEntry(role, text))

    def on_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        content = update.get("content")
        text = content.get("text", "") if isinstance(content, dict) else ""
        if kind == "agent_message_chunk":
            self._current.append(text)
            self._append("agent", text)
        elif kind == "agent_thought_chunk":
            self._append("thought", text)
        elif kind == "tool_call":
            self._append("tool", str(update.get("title") or "tool call"))
        elif kind == "plan":
            self.plan = list(update.get("entries") or [])

    def summary(self) -> dict[str, Any]:
        done, total = self.plan_progress
        return {
            "name": self.name,
            "status": self.status,
            "task": self.task,
            "latest": self.latest,
            "plan": f"{done}/{total}" if total else "",
            "turns": self.turns,
            "error": self.error,
        }


Deliver = Callable[[str], Awaitable[None] | None]


class SwarmError(Exception):
    pass


class SwarmManager:
    def __init__(
        self,
        client: ACPClient,
        *,
        deliver: Deliver | None = None,
        on_change: Callable[[], None] | None = None,
        max_workers: int = 8,
    ) -> None:
        self.client = client
        self.workers: dict[str, Worker] = {}
        self.deliver = deliver
        self.on_change = on_change
        self.max_workers = max_workers
        self._awaited: dict[str, int] = {}

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    def get(self, name: str) -> Worker:
        try:
            return self.workers[name]
        except KeyError:
            raise SwarmError(f"no worker named {name!r}") from None

    async def spawn(self, name: str, task: str) -> Worker:
        if not NAME_RE.match(name):
            raise SwarmError(
                "worker names are 1-40 letters, digits, '.', '_' or '-'"
            )
        if not task.strip():
            raise SwarmError("a worker needs a task")
        if (old := self.workers.get(name)) is not None and old.status in ACTIVE:
            raise SwarmError(f"worker {name!r} is already running")
        if sum(w.status in ACTIVE for w in self.workers.values()) >= self.max_workers:
            raise SwarmError(f"at most {self.max_workers} workers may run at once")
        worker = Worker(name, task)
        self.workers[name] = worker
        self._changed()
        try:
            worker.session_id = await self.client.new_session(worker)
        except Exception as error:
            worker.status, worker.error = "failed", str(error)
            worker._done.set()
            self._changed()
            raise SwarmError(f"failed to start {name!r}: {error}") from error
        self._start_turn(worker, WORKER_PREAMBLE.format(name=name, task=task))
        return worker

    async def message(self, name: str, text: str) -> Worker:
        worker = self.get(name)
        if worker.session_id is None or worker.status in ("stopped", "failed"):
            raise SwarmError(f"worker {name!r} is {worker.status}")
        if worker.status in ACTIVE:
            # towa rejects a second prompt while a turn runs; queue after it.
            assert worker._turn_task is not None
            await asyncio.shield(worker._turn_task)
        self._start_turn(worker, text)
        return worker

    async def stop(self, name: str) -> Worker:
        worker = self.get(name)
        if worker.session_id is not None:
            self.client.cancel(worker.session_id)
        if worker._turn_task is not None and not worker._turn_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker._turn_task), 10)
            except (asyncio.TimeoutError, Exception):
                worker._turn_task.cancel()
        worker.status = "stopped"
        worker.reported_turns = worker.turns
        if worker.session_id is not None:
            self.client.forget(worker.session_id)
        worker._done.set()
        self._changed()
        return worker

    async def stop_all(self) -> None:
        for name, worker in list(self.workers.items()):
            if worker.status in ACTIVE:
                await self.stop(name)

    def status(self) -> list[dict[str, Any]]:
        return [w.summary() for w in self.workers.values()]

    async def wait(
        self, names: list[str] | None = None, timeout: float | None = None
    ) -> list[Worker]:
        """Wait until the named workers (default: all active) finish their turn.

        Results returned here count as reported, so they are not delivered
        again as a coordinator prompt.
        """
        targets = [self.get(n) for n in names] if names else [
            w for w in self.workers.values() if w.status in ACTIVE
        ]
        for worker in targets:
            self._awaited[worker.name] = self._awaited.get(worker.name, 0) + 1
        try:
            waits = [asyncio.create_task(w._done.wait()) for w in targets]
            if waits:
                _, pending = await asyncio.wait(waits, timeout=timeout)
                for task in pending:
                    task.cancel()
        finally:
            for worker in targets:
                self._awaited[worker.name] -= 1
        for worker in targets:
            if worker._done.is_set():
                worker.reported_turns = worker.turns
        return targets

    def take_unreported(self) -> list[str]:
        """Reports for finished turns nobody has claimed, marking them claimed."""
        reports = []
        for worker in self.workers.values():
            if (
                worker.reported_turns < worker.turns
                and worker.status not in ACTIVE
                and worker.status != "stopped"
            ):
                worker.reported_turns = worker.turns
                reports.append(format_report(worker, worker.stop_reason))
        return reports

    def _start_turn(self, worker: Worker, text: str) -> None:
        worker.status = "running"
        worker._done.clear()
        worker._current = []
        worker.transcript.append(TranscriptEntry("user", text))
        worker._turn_task = asyncio.create_task(self._run_turn(worker, text))
        self._changed()

    async def _run_turn(self, worker: Worker, text: str) -> None:
        assert worker.session_id is not None
        stop_reason: str | None = None
        try:
            stop_reason = await self.client.prompt(worker.session_id, text)
        except asyncio.CancelledError:
            stop_reason = "cancelled"
        except Exception as error:
            worker.status, worker.error = "failed", str(error)
            worker.transcript.append(TranscriptEntry("system", f"error: {error}"))
        else:
            if worker.status != "stopped":
                worker.status = "idle"
        worker.result = "".join(worker._current).strip()
        worker.turns += 1
        if self._awaited.get(worker.name, 0) > 0:
            # A swarm_await call returns this result; do not deliver it twice.
            worker.reported_turns = worker.turns
        worker.stop_reason = stop_reason
        worker._done.set()
        self._changed()
        if (
            worker.reported_turns < worker.turns
            and worker.status != "stopped"
            and self.deliver is not None
        ):
            # Not marked reported yet: a later swarm_await may still claim it.
            # Deferred consumers call take_unreported() when they can deliver.
            report = format_report(worker, stop_reason)
            outcome = self.deliver(report)
            if asyncio.iscoroutine(outcome):
                await outcome


def format_report(worker: Worker, stop_reason: str | None = None) -> str:
    if worker.status == "failed":
        body = f"failed: {worker.error}"
    else:
        body = worker.result or "(no final message)"
    extra = f" (stop: {stop_reason})" if stop_reason and stop_reason != "end_turn" else ""
    return f"[swarm] worker '{worker.name}' finished{extra}:\n\n{body}"
