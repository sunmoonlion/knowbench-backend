from decimal import Decimal

import pytest
from knowbot_fixtures import FakeSummoner, manifest
from test_workbench_expert_data_db import (
    PROPOSED,
    A,
    StepZero,
    asked,
    expert_routes,
    known,
)  # noqa: F401
from test_workbench_expert_data_db import db as db
from test_workbench_expert_data_db import make_client as make_client
from test_workbench_expert_data_db import world as world

from app.application.workbench.advisor import Advisor, TurnResult
from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.ledger import Ledger
from app.bootstrap.workbench import workbench_store, workbench_stores


@pytest.mark.parametrize(
    "failure", [None, "repeated_result", "time_limit", "stop_unconfirmed"]
)
async def test_unseeded_data_phase_never_looks_like_scope_or_retries_itself(
    make_client,
    db,
    world,
    monkeypatch,
    failure,
):
    summoner = FakeSummoner()
    summoner.give(
        f"sub-{A}", {**manifest(), "schema": "knowbot-manifest/v2", "bindings": []}
    )
    monkeypatch.setattr(expert_routes, "knowmart_summoner", lambda: summoner)
    original = StepZero.run_turn
    seen = []

    async def turn(self, thread_id, text_, *, timeout=0):  # noqa: ASYNC109
        task_id = text_.split('task="', 1)[1].split('"', 1)[0]
        async with db() as s:
            repo = workbench_store(s)
            assert await repo.list_task_data(task_id) == []
            view = await ExpertDesk(repo).steps(task_id, owner_actor_id=A)
            assert view["position"] is None
            assert view["now"]["step"]["title"] == "定数据"
            assert all(step["status"] == "pending" for step in view["steps"])
        seen.append(text_)
        assert "empty inputs list is normal" in text_
        if failure:
            return TurnResult(
                turn_id="guard-turn",
                final_text="正在查数据",
                tokens={},
                cost=Decimal("0.05"),
                error=f"data_planning_{failure}",
                planning_failure=None if failure == "stop_unconfirmed" else failure,
                planning_stop_unconfirmed=failure == "stop_unconfirmed",
            )
        return await original(self, thread_id, text_, timeout=timeout)

    monkeypatch.setattr(StepZero, "run_turn", turn)
    await known(db)
    async with make_client(A) as http:
        task_id, driver, state = await asked(
            http, db, world, "unseeded-data", PROPOSED[:1]
        )
        assert state == ("FAILED" if failure == "stop_unconfirmed" else "WAITING")
        run = (await http.get(f"/api/workbench/tasks/{task_id}/steps")).json()
        assert run["position"] is None
        assert all(
            s["status"] == ("not_reached" if state == "FAILED" else "pending")
            for s in run["steps"]
        )
        assert not world["supply"].submitted
        async with db() as s:
            repo = workbench_store(s)
            task = await repo.get_task(task_id)
            assert task["current_step"] == 0
            assert len(await repo.list_attempts(task_id)) == 1
            if failure:
                assert Decimal(str(task["budget"]["used"])) == Decimal("0.05")
                if failure == "stop_unconfirmed":
                    assert task["original_input"]["client_context"]["turn_open"]
                else:
                    assert task["waiting_reason"] == "INPUT"
                    review = await ExpertDesk(repo).review(
                        str(task["active_interaction_id"]), owner_actor_id=A
                    )
                    assert review["where"]["step"] is None
                    assert review["pending"]["title"] == "数据准备已暂停"
            else:
                assert task["waiting_reason"] == "DATA"
        if not failure:
            response = await http.post(
                f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
            )
            assert response.status_code == 200, response.text
            async with db() as s:
                repo = workbench_store(s)
                assert len(await repo.list_task_data(task_id)) == 1
                task = await repo.get_task(task_id)
                assert task["state"] == "VALIDATING"
                assert (await repo.get_task_resolution(task_id))["status"] == "ready"
        else:
            # Scheduler polling does not launch a paid retry or accept partial requirements.
            await Advisor(workbench_stores(db), driver, records=True).drive(task_id)
            assert len(seen) == 1
            if failure in ("repeated_result", "time_limit"):
                async with db() as s:
                    repo = workbench_store(s)
                    events = await repo.list_task_events(
                        task_id, types=("interaction/opened",)
                    )
                    opened = events[-1]["payload"]
                    interaction = await repo.get_interaction(opened["interaction_id"])
                    await Ledger(repo).respond_interaction(
                        opened["interaction_id"],
                        token=opened["token"],
                        owner_actor_id=A,
                        subject_digest=interaction.get("subject_digest"),
                        response={
                            "decision": "rework"
                            if failure == "repeated_result"
                            else "stop"
                        },
                    )
                await Advisor(workbench_stores(db), driver, records=True).drive(task_id)
                assert len(seen) == (2 if failure == "repeated_result" else 1)
