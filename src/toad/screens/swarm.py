"""Swarm screen: workers with status, latest message and plan progress.

Select a worker in the table to read its conversation on the right.
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from toad.swarm.manager import SwarmManager, Worker

STATUS_ICON = {
    "starting": "◌",
    "running": "●",
    "idle": "✓",
    "failed": "✗",
    "stopped": "■",
}


def worker_row(worker: Worker) -> tuple[str, str, str, str]:
    done, total = worker.plan_progress
    plan = f"{done}/{total}" if total else ""
    latest = worker.latest.replace("\n", " ")
    if len(latest) > 60:
        latest = latest[:59] + "…"
    return (
        worker.name,
        f"{STATUS_ICON.get(worker.status, '?')} {worker.status}",
        plan,
        latest,
    )


def transcript_markup(worker: Worker) -> Text:

    lines = [f"[b]{escape(worker.name)}[/b]  {worker.status}", ""]
    for entry in worker.transcript:
        text = escape(entry.text.strip())
        if entry.role == "tool":
            lines.append(f"[green]⚙ {text}[/]")
        elif entry.role == "user":
            lines.append(f"[b cyan]▶ {text}[/]")
        elif entry.role == "thought":
            lines.append(f"[dim i]{text}[/]")
        elif entry.role == "system":
            lines.append(f"[red]{text}[/]")
        else:
            lines.append(text)
        lines.append("")
    if worker.plan:
        lines.append("[b]Plan[/b]")
        for item in worker.plan:
            mark = {"completed": "✓", "in_progress": "▸"}.get(item.get("status", ""), "·")
            lines.append(f" {mark} {escape(str(item.get('content', '')))}")
    return Text.from_markup("\n".join(lines))


class SwarmScreen(ModalScreen):
    DEFAULT_CSS = """
    SwarmScreen {
        align: center middle;
    }
    SwarmScreen > Horizontal {
        width: 95%;
        height: 90%;
        border: round $primary;
        background: $surface;
    }
    SwarmScreen DataTable {
        width: 1fr;
        height: 1fr;
    }
    SwarmScreen #swarm-transcript {
        width: 1fr;
        height: 1fr;
        border-left: solid $primary;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("s", "stop_worker", "Stop worker"),
    ]

    def __init__(self, manager: SwarmManager) -> None:
        super().__init__()
        self.manager = manager
        self.selected: str | None = next(iter(manager.workers), None)

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield DataTable(cursor_type="row", id="swarm-workers")
            with VerticalScroll(id="swarm-transcript"):
                yield Static(id="swarm-transcript-body")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Worker", "Status", "Plan", "Latest")
        self.refresh_workers()
        self.set_interval(0.5, self.refresh_workers)

    def refresh_workers(self) -> None:
        table = self.query_one(DataTable)
        for worker in self.manager.workers.values():
            row = worker_row(worker)
            if worker.name in table.rows:
                for column, value in zip(table.columns, row):
                    table.update_cell(worker.name, column, value)
            else:
                table.add_row(*row, key=worker.name)
        if self.selected is None and self.manager.workers:
            self.selected = next(iter(self.manager.workers))
        self.refresh_transcript()

    def refresh_transcript(self) -> None:
        body = self.query_one("#swarm-transcript-body", Static)
        if self.selected is None or self.selected not in self.manager.workers:
            body.update("No workers. Use /swarm spawn NAME TASK.")
            return
        body.update(transcript_markup(self.manager.workers[self.selected]))

    @on(DataTable.RowHighlighted)
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.selected = event.row_key.value
            self.refresh_transcript()

    async def action_stop_worker(self) -> None:
        if self.selected is not None:
            await self.manager.stop(self.selected)
            self.refresh_workers()
