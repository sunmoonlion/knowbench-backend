"""Question -> public match -> verified delegation -> chosen methods -> delivery.

Real database and runner; synthetic app-server and providers, no paid model calls.
"""

import json
from pathlib import Path

import pytest
from knowbot_fixtures import KNOWBOT, FakeSummoner
from test_capability_method_plan import proposal
from test_expert_match import QUESTION, directory, result
from test_knowbot_test_runs_db import (
    AUTHOR,
    ENV,
    WORKSPACE,
    runner_for,
    setup_test_computer,
)
from test_method_advisor_db import ROWS
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_expert_data_db import FakeKnowbase, FakeSupply
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.catalog import pin_knowbot
from app.application.workbench.delegation import DelegationService
from app.application.workbench.execution_plan import final_results
from app.application.workbench.expert_data import ExpertData
from app.application.workbench.find_expert import FindExpertService, find_expert
from app.application.workbench.project_service import ProjectService
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import IdempotencyConflict
from app.domain.workbench.expert_match import DirectorySnapshot, MatchSelection
from app.domain.workbench.knowbots import manifest_digest
from app.domain.workbench.models import HandoverRequest
from app.infrastructure.workbench.repository import WorkbenchRepository


class Directory:
    def __init__(self, document):
        self.document = document

    def configured(self):
        return True

    async def snapshot(self, owner):
        return DirectorySnapshot.model_validate(self.document)


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("existing_conversation", [False, True])
async def test_new_matching_receipt_is_used_by_the_real_dynamic_runner(
    db, review, existing_conversation
):
    doc = json.loads(
        (
            Path(__file__).parent / "fixtures/capability-methods-manifest.json"
        ).read_text()
    )
    doc["rule_sources"] = [
        {"rule": "合成核对", "source": "user_confirmed", "ref": "synthetic:test"}
    ]
    entries = directory()
    entry = entries["experts"][0]
    entry.update(
        knowbot_id=KNOWBOT,
        digest=manifest_digest(doc),
        public_capabilities=doc["pack"]["public_capabilities"],
    )
    port = Directory(entries)
    choice = result()
    cap = "double_check" if review else "check"
    choice["picks"][0]["knowbot_id"] = KNOWBOT
    for row in choice["picks"][0]["coverage"]:
        row.update(capability_id=cap, declaration_quote=cap)
    question = QUESTION if review else "核对文件，交付报告。"
    if not review:
        choice["requirements"][0].update(
            statement="核对文件", question_quote="核对文件"
        )
    plan = proposal(review=review)
    plan["requirement_coverage"] = {"r1": cap, "r2": cap}
    report = plan["steps"][-1]
    report["inputs"][0]["slot"] = "inspect"
    if review:
        report["inputs"][-1]["slot"] = "review"
    else:
        report["omitted_inputs"] = {"review": "not_selected"}
    final = {**json.loads(ROWS), "review_summary": "checked" if review else "本题未选"}
    fake = ScriptedAppServer(
        [json.dumps(choice), json.dumps(plan), "All inputs are in the question.", ROWS]
        + (['{"notes":"checked"}'] if review else [])
        + [json.dumps(final)]
    )
    await fake.start()
    runner = runner_for(db, records=True)
    try:
        await setup_test_computer(db, fake)
        async with db() as session:
            repo = WorkbenchRepository(session)
            project = await ProjectService(repo).create(
                owner_actor_id=OWNER,
                environment_id=ENV,
                workspace_root=WORKSPACE,
                path="capability-match",
            )
            svc = FindExpertService(repo, directory=port)
            found = await svc.start(
                owner_actor_id=OWNER,
                project_id=project["id"],
                question=question,
                idempotency_key="synthetic-capability-match",
                match_contract="knowbench/expert-match/v2",
                expert=f"knowbot:{KNOWBOT}@1" if existing_conversation else None,
            )
            task = await repo.get_task(found["task_id"])
            assert task["profile_version"] == "2"
            first = await find_expert(repo, port, task)
            assert first["complete"] and "method_sources" not in json.dumps(first)
            assert await find_expert(repo, port, task) == first
            assert (
                len(
                    await repo.list_task_events(
                        found["task_id"], types=("task/expert_directory",)
                    )
                )
                == 1
            )
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            matched = await FindExpertService(repo, directory=port).result(
                OWNER, found["task_id"]
            )
            assert matched["outcome"] == "summon"
            summoner = FakeSummoner()
            summoner.give(AUTHOR, doc)
            pinned = await pin_knowbot(
                repo,
                summoner,
                owner_actor_id=OWNER,
                owner_subject=AUTHOR,
                ref=f"{KNOWBOT}@1",
                dynamic_method_owners=frozenset({AUTHOR}),
            )

            async def token():
                return "synthetic-token"

            selection = MatchSelection(
                match_task_id=found["task_id"],
                match_artifact_version=matched["match_artifact_version"],
                question_revision=1,
                knowbot_id=KNOWBOT,
                version="1",
            )
            service = DelegationService(
                repo, dynamic_method_owners=frozenset({AUTHOR}), directory=port
            )
            args = dict(
                owner_actor_id=OWNER,
                project_id=project["id"],
                profile_id=pinned.profile_id,
                question=question,
                idempotency_key="synthetic-capability-delegation",
                user_token=token,
                expert_data=ExpertData(
                    FakeKnowbase(), FakeSupply(), supply_enabled=False
                ),
                match=selection,
            )
            if existing_conversation:
                conversation = await SessionService(repo).start(
                    owner_actor_id=OWNER, kind="work", project_id=project["id"]
                )
                request = HandoverRequest(
                    session_id=conversation["session_id"],
                    profile_id=pinned.profile_id,
                    original_input={"text": question},
                    match=selection,
                    idempotency_key=args["idempotency_key"],
                )
                kwargs = {
                    k: args[k] for k in ("owner_actor_id", "user_token", "expert_data")
                }
                made = await service.handover(request, **kwargs)
                assert (await service.handover(request, **kwargs))["task_id"] == made[
                    "task_id"
                ]
                with pytest.raises(IdempotencyConflict):
                    await service.handover(
                        request.model_copy(
                            update={
                                "match": selection.model_copy(
                                    update={"question_revision": 2}
                                )
                            }
                        ),
                        **kwargs,
                    )
                # Existing conversation endpoint normally schedules this command.
                row = await repo.get_session(conversation["session_id"])
                async with repo.transaction():
                    await repo.enqueue_command(
                        session_id=conversation["session_id"],
                        sandbox_id=str(row["sandbox_id"]),
                        kind="task.drive",
                        payload={"task_id": made["task_id"]},
                    )
            else:
                made = await service.from_expert_entry(**args)
                assert (await service.from_expert_entry(**args))["task_id"] == made[
                    "task_id"
                ]
                with pytest.raises(IdempotencyConflict):
                    await service.from_expert_entry(
                        **{
                            **args,
                            "match": selection.model_copy(
                                update={"question_revision": 2}
                            ),
                        }
                    )
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(made["task_id"])
            assert task["state"] == "SUCCEEDED", task
            assert (
                task["original_input"]["task_requirements"]["match_task_id"]
                == found["task_id"]
            )
            assert (await final_results(repo, task))["report"]["content"] == final
            attempts = await repo.list_attempts(made["task_id"])
            assert (
                "review_1" in {attempt["step_id"] for attempt in attempts}
            ) == review
        assert len(fake.turn_inputs) == (6 if review else 5)
    finally:
        await close(runner)
        await fake.close()
