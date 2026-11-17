from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from textual.app import ComposeResult
from textual.content import Content
from textual.reactive import reactive
from textual import containers
from textual.widgets import Static

from toad.pill import pill
from toad.widgets.strike_text import StrikeText


class NonSelectableStatic(Static):
    ALLOW_SELECT = False


class Plan(containers.Grid):
    # BORDER_TITLE = "Plan"
    DEFAULT_CLASSES = "block"
    DEFAULT_CSS = """

    Plan {        
        # border: panel $secondary;
        border-top: ascii $secondary;            
        border-bottom: ascii $secondary;
        background: black 10%;        
        margin: 1 0 1 0 !important;   # Special case because the tall border reduces the apparent width           
        padding: 0 1 0 1;        
        height: auto;                        
        grid-size: 2;
        grid-columns: auto 1fr;
        grid-rows: auto;
        border-title-align: center;

        .-no-plan {
            text-style: dim italic;
        }

        .plan {
            color: $text-secondary;
        }
        .status {
            padding: 0 0 0 0;
            color: $text-secondary;
        }
        .priority {
            padding: 0 0 0 0;
        }
        .status.status-completed {
            color: $text-success;            
        }
        .plan-header {
            column-span: 2;
            text-style: bold;
            color: $text-primary;
        }
        .plan-intention, .plan-goals {
            column-span: 2;
            color: $text-muted;
            text-style: italic;
        }
        .plan-phase {
            column-span: 2;
            text-style: bold;
            color: $text-accent;
        }
        .plan-lane {
            column-span: 2;
            color: $text-secondary;
            padding-left: 1;
        }
        .status-pending {
            opacity: 0.7;
        }          
    }

    """

    @dataclass(frozen=True)
    class Entry:
        """Information about an entry in the Plan."""

        content: Content
        priority: str
        status: str
        phase: str | None = None
        lane: str | None = None

        @classmethod
        def from_acp(cls, entry: dict[str, Any]) -> Plan.Entry:
            """Build an Entry from an ACP plan entry, reading optional `_meta`
            grouping (`phase`, `lane`) as sent by jcode-style todo tools."""
            meta = entry.get("_meta") or {}
            phase = meta.get("phase")
            lane = meta.get("lane")
            return cls(
                Content(entry["content"]),
                entry.get("priority", "medium"),
                entry.get("status", "pending"),
                None if phase is None else str(phase),
                None if lane is None else str(lane),
            )

        def update_status(self, status: str) -> Plan.Entry:
            """Get a new Entry with updated status.

            Args:
                status: New status

            Returns:
                New Entry instance.
            """
            return replace(self, status=status)

    entries: reactive[list[Entry] | None] = reactive(None, recompose=True)
    all_complete: reactive[bool] = reactive(False, toggle_class="-all-complete")

    PRIORITIES = {
        "high": pill("H", "$error-muted", "$text-error"),
        "medium": pill("M", "$warning-muted", "$text-warning"),
        "low": pill("L", "$primary-muted", "$text-primary"),
    }

    def __init__(
        self,
        entries: list[Entry],
        title: str | None = None,
        intention: str | None = None,
        goals: list[str] | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ):
        self.newly_completed: set[Plan.Entry] = set()
        super().__init__(name=name, id=id, classes=classes)
        self.title = title
        self.intention = intention
        self.goals = list(goals or [])
        self.set_reactive(Plan.entries, entries)

    @classmethod
    def meta_kwargs(cls, meta: dict[str, Any] | None) -> dict[str, Any]:
        """Extract title / intention / goals from a plan update's `_meta`."""
        meta = meta or {}
        plan = meta.get("plan") or {}
        goals = meta.get("goals") or []
        return {
            "title": meta.get("title") or plan.get("title"),
            "intention": meta.get("user_intention") or plan.get("user_intention"),
            "goals": [
                g if isinstance(g, str) else str(g.get("group") or g.get("feedback_loop") or g)
                for g in goals
            ],
        }

    def watch_entries(self, old_entries: list[Entry], new_entries: list[Entry]) -> None:
        entry_map = {entry.content: entry for entry in old_entries}
        newly_completed: set[Plan.Entry] = set()
        for entry in new_entries:
            old_entry = entry_map.get(entry.content, None)
            if (
                old_entry is not None
                and entry.status == "completed"
                and entry.status != old_entry.status
            ):
                newly_completed.add(entry)
        self.newly_completed = newly_completed
        if new_entries:
            self.all_complete = all(
                entry.status == "completed" for entry in self.entries
            )
        else:
            self.all_complete = False

    def compose(self) -> ComposeResult:
        if not self.entries:
            yield Static("No plan yet", classes="-no-plan")
            return
        if self.title:
            yield NonSelectableStatic(Content(self.title), classes="plan-header")
        if self.intention:
            yield NonSelectableStatic(Content(self.intention), classes="plan-intention")
        if self.goals:
            yield NonSelectableStatic(
                Content("Goals: " + "; ".join(self.goals)), classes="plan-goals"
            )
        grouped = any(e.phase is not None or e.lane is not None for e in self.entries)
        current_phase: str | None = None
        current_lane: str | None = None
        for entry in self.entries:
            if grouped:
                if entry.phase != current_phase:
                    current_phase = entry.phase
                    current_lane = None
                    if entry.phase is not None:
                        yield NonSelectableStatic(
                            Content(f"Phase {entry.phase}"), classes="plan-phase"
                        )
                if entry.lane != current_lane:
                    current_lane = entry.lane
                    if entry.lane is not None:
                        yield NonSelectableStatic(
                            Content(f"▸ {entry.lane}"), classes="plan-lane"
                        )
            classes = f"priority-{entry.priority} status-{entry.status}"
            yield NonSelectableStatic(
                self.render_status(entry.status),
                classes=f"status {classes}",
            )

            yield (
                strike_text := StrikeText(
                    entry.content,
                    classes=f"plan {classes}",
                )
            )
            if entry in self.newly_completed:
                self.call_after_refresh(strike_text.strike)
            elif entry.status == "completed":
                strike_text.add_class("-complete")
        self.all_complete = all(entry.status == "completed" for entry in self.entries)

    def render_status(self, status: str) -> Content:
        if status == "completed":
            return Content(" ✔ ")
        elif status == "pending":
            return Content(" • ")
        elif status == "in_progress":
            return Content("👉 ")
        return Content()


if __name__ == "__main__":
    from textual.app import App

    entries = [
        Plan.Entry(
            Content("Build the best damn UI for agentic coding in the terminal"),
            "hide",
            "in_progress",
        ),
        Plan.Entry(
            Content(
                "Embarass big tech by being the only agent CLI that can render Markdown tables"
            ),
            "high",
            "pending",
        ),
        Plan.Entry(
            Content.from_markup("Catch flight to Wuhan"),
            "low",
            "pending",
        ),
        Plan.Entry(
            Content.from_markup("Eat 热干面 for breakfast"),
            "low",
            "pending",
        ),
        Plan.Entry(
            Content.from_markup("Pack sunscreen, catch flight to Thailand"),
            "low",
            "pending",
        ),
        Plan.Entry(
            Content.from_markup(
                "Work as a digital nomad in Asia, eat well, don't get sun-stroke"
            ),
            "low",
            "pending",
        ),
    ]

    class PlanApp(App):
        BINDINGS = [("space", "strike")]

        CSS = """
        Screen {
            align: center middle;
            Plan {
                margin: 1;
            }
        }
        """

        def __init__(self) -> None:
            super().__init__()
            self.working = 0

        def compose(self) -> ComposeResult:
            yield Plan(entries)

        def action_strike(self) -> None:
            new_entries = entries.copy()
            new_entries[self.working] = entries[self.working].update_status("completed")
            self.working += 1
            try:
                new_entries[self.working] = entries[self.working].update_status(
                    "in_progress"
                )
            except IndexError:
                pass

            self.query_one(Plan).entries = new_entries
            entries[:] = new_entries

    app = PlanApp()
    app.run()
