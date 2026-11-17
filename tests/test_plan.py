import pytest
from textual.app import App
from textual.content import Content

from toad.widgets.plan import Plan

TOWA_ENTRIES = [
    {"content": "Write toml", "status": "completed", "priority": "high",
     "_meta": {"phase": 1, "lane": "plan"}},
    {"content": "Render lanes", "status": "in_progress",
     "_meta": {"phase": 1, "lane": "plan"}},
    {"content": "Ship", "status": "pending", "priority": "low",
     "_meta": {"phase": 2, "lane": "release"}},
]


def test_from_acp_flat():
    e = Plan.Entry.from_acp({"content": "x"})
    assert (e.priority, e.status, e.phase, e.lane) == ("medium", "pending", None, None)


def test_update_status_keeps_grouping():
    e = Plan.Entry.from_acp(TOWA_ENTRIES[1]).update_status("completed")
    assert (e.status, e.phase, e.lane) == ("completed", "1", "plan")


def test_meta_kwargs():
    kw = Plan.meta_kwargs({"plan": {"title": "T", "user_intention": "I"},
                           "goals": [{"group": "g1"}, "g2"]})
    assert kw == {"title": "T", "intention": "I", "goals": ["g1", "g2"]}


@pytest.mark.asyncio
async def test_grouped_render():
    entries = [Plan.Entry.from_acp(e) for e in TOWA_ENTRIES]

    class A(App):
        def compose(self):
            yield Plan(entries, title="Towa plan", intention="do it", goals=["tests"])

    async with A().run_test() as pilot:
        plan = pilot.app.query_one(Plan)
        assert [str(w.render()) for w in plan.query(".plan-phase")] == ["Phase 1", "Phase 2"]
        assert len(plan.query(".plan-lane")) == 2
        assert len(plan.query(".plan-header")) == 1
        assert len(plan.query(".plan-goals")) == 1


@pytest.mark.asyncio
async def test_flat_render_unchanged():
    class A(App):
        def compose(self):
            yield Plan([Plan.Entry(Content("a"), "high", "pending")])

    async with A().run_test() as pilot:
        plan = pilot.app.query_one(Plan)
        assert not plan.query(".plan-phase") and not plan.query(".plan-header")
        assert len(plan.query(".status")) == 1
