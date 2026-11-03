"""原找专家入口接冻结方法运行；真实数据库/运行器，模型与商城为合成端口。"""

import json

import pytest
from knowbot_fixtures import KNOWBOT, FakeSummoner
from method_plan_fixtures import proposal
from test_find_expert_db import Directory
from test_knowbot_test_runs_db import (
    AUTHOR,
    ENV,
    WORKSPACE,
    runner_for,
    setup_test_computer,
)
from test_method_advisor_db import ROWS
from test_referenced_method_catalog import manifest
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_expert_data_db import FakeKnowbase, FakeSupply
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.catalog import pin_knowbot
from app.application.workbench.delegation import DelegationService
from app.application.workbench.execution_plan import final_results
from app.application.workbench.expert_data import ExpertData
from app.application.workbench.find_expert import FindExpertService, find_expert
from app.application.workbench.plan_receipts import catalog_for_task
from app.application.workbench.project_service import ProjectService
from app.domain.workbench.knowbots import manifest_digest
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("review", [False, True])
async def test_ai_find_to_pinned_expert_to_selected_methods_and_delivery(db, review):
    doc = manifest()
    plan = proposal(review=review)
    question = "Synthetic document: id=one. " + ("Review it." if review else "List it.")
    choice = {
        "outcome": "summon",
        "picks": [
            {"knowbot_id": KNOWBOT, "version": "1", "reason": "Matches the scope"}
        ],
    }
    directory = Directory(
        [
            {
                "knowbot_id": KNOWBOT,
                "version": "1",
                "name": doc["name"],
                "tagline": doc["tagline"],
                "scope": {"does": "Inspect a supplied document"},
                "free": True,
                "price": None,
                # 目录投影不能把作者方法正文传给找专家这一步。
                "pack": doc["pack"],
                "private": "not public",
            }
        ]
    )
    fake = ScriptedAppServer(
        [
            json.dumps(choice),
            plan.model_dump_json(),
            "All inputs are in the question.",
            ROWS,
        ]
        + (['{"notes":"checked"}'] if review else [])
        + [ROWS]
    )
    await fake.start()
    runner = runner_for(db, records=True)
    try:
        await setup_test_computer(db, fake)
        async with db() as session:
            repo = WorkbenchRepository(session)
            place = await ProjectService(repo).create(
                owner_actor_id=OWNER,
                environment_id=ENV,
                workspace_root=WORKSPACE,
                path="find-dynamic",
            )
            svc = FindExpertService(repo, directory=directory)
            found_round = await svc.start(
                owner_actor_id=OWNER,
                project_id=place["id"],
                question=question,
                idempotency_key="synthetic-find-dynamic",
            )
            task = await repo.get_task(found_round["task_id"])
            listed = await find_expert(repo, directory, task)
            assert listed["experts"][0]["knowbot_id"] == KNOWBOT
            assert (
                "pack" not in listed["experts"][0]
                and "private" not in listed["experts"][0]
            )
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            found = await FindExpertService(repo, directory=directory).result(
                OWNER, found_round["task_id"]
            )
            assert found["outcome"] == "summon"
            [pick] = found["picks"]
            summoner = FakeSummoner()
            summoner.give(AUTHOR, doc)
            pinned = await pin_knowbot(
                repo,
                summoner,
                owner_actor_id=OWNER,
                owner_subject=AUTHOR,
                ref=f"{pick['knowbot_id']}@{pick['version']}",
                dynamic_method_owners=frozenset({AUTHOR}),
            )

            async def token():
                return "synthetic-user-token"

            args = dict(
                owner_actor_id=OWNER,
                project_id=place["id"],
                profile_id=pinned.profile_id,
                question=found["question"],
                idempotency_key=f"find-expert:{found_round['task_id']}",
                user_token=token,
                expert_data=ExpertData(
                    FakeKnowbase(), FakeSupply(), supply_enabled=False
                ),
            )
            service = DelegationService(repo, dynamic_method_owners=frozenset({AUTHOR}))
            made = await service.from_expert_entry(**args)
            replay = await service.from_expert_entry(**args)
            assert replay["task_id"] == made["task_id"] and not replay["created"]
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = await repo.get_task(made["task_id"])
            assert row["state"] == "SUCCEEDED"
            assert row["original_input"]["input"]["text"] == question
            assert row["pack_digest"] == manifest_digest(doc)
            catalog = catalog_for_task(row)
            assert catalog.method_sources["report"].reference.version == 2
            attempts = await repo.list_attempts(made["task_id"])
            chosen = [a["step_id"] for a in attempts]
            assert ("review_1" in chosen) == review
            assert len(chosen) == (5 if review else 4)
            assert (await final_results(repo, row))["report"]["content"] == json.loads(
                ROWS
            )
        assert len(fake.turn_inputs) == (6 if review else 5)
        assert all(question in prompt for prompt in fake.turn_inputs[:2])
    finally:
        await close(runner)
        await fake.close()
