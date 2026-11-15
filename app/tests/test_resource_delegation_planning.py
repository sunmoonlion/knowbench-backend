"""Planning can render direct-only capabilities without interpreting an execution plan."""

from unittest.mock import AsyncMock

import pytest

from app.application.workbench import execution_plan
from app.application.workbench.method_authorizations import delegation_binding
from app.domain.workbench.errors import MethodPlanInvalid


@pytest.mark.parametrize("accepted,special", [(None, True), (None, False), (1, True)])
async def test_planning_has_no_delegation_options_without_reading_execution_plan(
    monkeypatch, accepted, special
):
    read = AsyncMock(side_effect=AssertionError("planning has no executable child"))
    monkeypatch.setattr(execution_plan, "execution_for_task", read)
    assert (
        await delegation_binding(
            object(),
            {
                "task": {"accepted_plan_version": accepted},
                "special": special,
            },
        )
        is None
    )
    read.assert_not_awaited()


async def test_invalid_accepted_plan_remains_an_execution_error(monkeypatch):
    read = AsyncMock(side_effect=MethodPlanInvalid("accepted plan changed"))
    monkeypatch.setattr(execution_plan, "execution_for_task", read)
    with pytest.raises(MethodPlanInvalid, match="accepted plan changed"):
        await delegation_binding(
            object(),
            {
                "task": {"accepted_plan_version": 1},
                "special": False,
            },
        )
    read.assert_awaited_once()
