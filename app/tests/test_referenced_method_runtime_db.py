"""The author's actual compiled reference fixture runs through the existing engine.

Only disposable test tasks bypass closed v3 intake; no author registry or model
service is called, and the scripted plan is not evidence of real model quality.
"""

import json

import pytest
from method_plan_fixtures import proposal
from sqlalchemy import text
from test_method_advisor_db import ROWS, Driver
from test_method_plans_db import task
from test_referenced_method_catalog import manifest
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.advisor import Advisor
from app.application.workbench.execution_plan import final_results
from app.application.workbench.expert_desk import ExpertDesk
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

pytestmark = pytest.mark.asyncio


async def test_one_frozen_reference_catalog_runs_distinct_task_plans_and_keeps_versions(
    db,
):
    pinned = manifest()["pack"]
    seen = []
    for review in (False, True):
        tid, _ = await task(db)
        async with db() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE workbench_tasks SET pack_snapshot=cast(:snapshot as jsonb) WHERE id=:id"
                    ),
                    {"snapshot": json.dumps(pinned), "id": tid},
                )
        plan = proposal(review=review)
        driver = Driver(
            [plan.model_dump_json(), ROWS]
            + (['{"notes":"checked"}'] if review else [])
            + [ROWS]
        )
        assert (
            await Advisor(SqlWorkbenchStores(db), driver).drive(tid)
            == TaskState.SUCCEEDED
        )
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = await repo.get_task(tid)
            assert row["pack_snapshot"] == pinned
            assert row["accepted_plan_version"] == 1
            attempts = await repo.list_attempts(tid)
            report = next(a for a in attempts if a["step_id"] == "report_1")
            assert report["step_version"] == "2"
            assert report["refs"]["method_version"] == "2"
            assert report["refs"]["pack_digest"] == row["pack_digest"]
            result = (await final_results(repo, row))["report"]
            assert str(result["attempt_id"]) == str(report["id"])
            assert result["content"] == json.loads(ROWS)
            view = await ExpertDesk(repo).steps(tid, owner_actor_id=OWNER)
            assert [s["method_instance"] for s in view["steps"]] == [
                s.instance_id for s in plan.steps
            ]
            assert all(s["current_result"] is not None for s in view["steps"])
            seen.append((tid, str(result["id"]), len(view["steps"])))
    assert seen[0][0] != seen[1][0] and seen[0][1] != seen[1][1]
    assert [s[2] for s in seen] == [2, 3]
