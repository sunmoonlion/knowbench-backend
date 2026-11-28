"""The run sheet follows current attempt activity and unsettled costs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.application.workbench.expert_desk import ExpertDesk
from app.domain.workbench.states import TaskState
from app.domain.workbench.step_view import budget_view


async def test_running_charge_persists_during_the_same_attempts_question():
    repo = SimpleNamespace(
        list_record_events=AsyncMock(
            return_value=[
                {"type": "attempt/opened", "attempt_id": "a"},
                {
                    "type": "thread/tokenUsage/updated",
                    "payload": {"cost": {"turn": "0.50"}},
                },
            ]
        )
    )
    task = {
        "id": "t",
        "session_id": "s",
        "active_attempt_id": "a",
        "state": TaskState.RUNNING,
    }
    desk = ExpertDesk(repo)

    def projection(running):
        return budget_view({"used": "1.00", "currency": "CNY"}, running=running)[
            "spent"
        ]

    assert projection(await desk._running_cost(task)) == "1.50"
    task["state"] = TaskState.WAITING
    assert projection(await desk._running_cost(task)) == "1.50"
