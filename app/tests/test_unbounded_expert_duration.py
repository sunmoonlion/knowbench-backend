"""Owner removed execution duration caps; cancellation/loop protection remain."""

import inspect

from app.application.workbench.runner import SandboxLink
from app.application.workbench.turn import STEP_TIMEOUT_SECONDS, TurnDriver
from app.domain.workbench.data_planning import DATA_PLAN_SECONDS
from app.domain.workbench.expert_lookup import LOOKUP_SECONDS
from app.domain.workbench.step_progress import (
    STEP_IDLE_SECONDS,
    STEP_TOOL_SECONDS,
    StepProgressGuard,
)


def test_default_execution_has_no_time_caps():
    assert STEP_TIMEOUT_SECONDS is DATA_PLAN_SECONDS is LOOKUP_SECONDS is None
    assert STEP_IDLE_SECONDS is STEP_TOOL_SECONDS is None
    for driver in (TurnDriver, SandboxLink):
        assert inspect.signature(driver.run_turn).parameters["timeout"].default is None


def test_long_silent_reasoning_and_long_tool_are_not_interrupted():
    guard = StepProgressGuard(0)
    guard.tick(24 * 3600)
    assert guard.failure is None
    guard.observe("item/started", {"id": "slow", "type": "mcpToolCall"}, 24 * 3600)
    guard.tick(48 * 3600)
    assert guard.failure is None
    guard.observe(
        "item/completed",
        {
            "id": "slow",
            "type": "mcpToolCall",
            "status": "completed",
            "result": {"rows": [1]},
        },
        48 * 3600 + 1,
    )
    assert guard.failure is None and not guard.active


def test_removing_time_caps_does_not_remove_repeated_result_protection():
    guard = StepProgressGuard(0)
    for n in range(3):
        guard.observe(
            "item/completed",
            {
                "id": str(n),
                "type": "mcpToolCall",
                "tool": "query_facts",
                "result": {"rows": []},
            },
            (n + 1) * 3600,
        )
    assert guard.failure == "repeated_result"
