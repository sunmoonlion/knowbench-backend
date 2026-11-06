import pytest

from app.domain.workbench.step_progress import StepProgressGuard


@pytest.fixture(autouse=True)
def finite_limits_for_legacy_guard_tests(monkeypatch):
    monkeypatch.setattr("app.domain.workbench.step_progress.STEP_IDLE_SECONDS", 180.0)
    monkeypatch.setattr("app.domain.workbench.step_progress.STEP_TOOL_SECONDS", 300.0)


def tool(identity="one", result=None, **fields):
    return {
        "id": identity,
        "type": "mcpToolCall",
        "server": "data",
        "tool": "query_facts",
        "arguments": {"code": "A"},
        "status": "completed",
        "result": {"values": [1]} if result is None else result,
        **fields,
    }


def test_changing_prose_is_activity_not_progress():
    g = StepProgressGuard(0)
    for i in range(1, 180):
        g.observe(
            "item/completed",
            {"id": str(i), "type": "agentMessage", "text": f"working {i}"},
            i,
        )
    assert g.last_activity == 179 and g.failure is None
    g.tick(180)
    assert g.failure == "no_progress"


def test_started_completed_dedup_and_tool_clock():
    g = StepProgressGuard(0)
    g.observe("item/started", tool(), 100)
    g.observe("item/started", tool(), 200)
    g.tick(300)
    assert g.failure is None and g.idle_seconds == 100
    g.observe("item/completed", tool(), 301)
    assert not g.active and g.idle_seconds == 0
    g.observe("item/completed", tool(), 400)
    assert g.idle_seconds == 99 and not g.failure
    g.observe("item/started", tool(), 401)
    assert not g.active
    g.tick(481)
    assert g.failure == "no_progress"


def test_long_tool_has_its_own_limit():
    g = StepProgressGuard(0)
    g.observe("item/started", tool(), 1)
    g.tick(300)
    assert g.failure is None
    g.tick(301)
    assert g.failure == "tool_time_limit"


def test_identical_results_do_not_refresh_progress():
    g = StepProgressGuard(0)
    g.observe("item/completed", tool("a"), 10)
    g.observe("item/completed", tool("b"), 50)
    assert g.idle_seconds == 40
    g.observe("item/completed", tool("c"), 70)
    assert g.failure == "repeated_result"


def test_distinct_failures_do_not_refresh_but_new_success_does():
    g = StepProgressGuard(0)
    for i in range(1, 4):
        g.observe(
            "item/completed", tool(str(i), {"isError": True, "error": str(i)}), i * 30
        )
    assert g.idle_seconds == 90 and g.failure is None
    g.observe("item/completed", tool("ok"), 100)
    assert g.idle_seconds == 0
