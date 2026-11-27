"""Question -> public match -> verified delegation -> chosen methods -> delivery.

Real database and runner; synthetic app-server and providers, no paid model calls.
"""

import json
from pathlib import Path

from knowbot_fixtures import KNOWBOT, FakeSummoner
from test_knowbot_test_runs_db import (
    AUTHOR,
    ENV,
    WORKSPACE,
    setup_test_computer,
)
from test_workbench_expert_data_db import FakeKnowbase, FakeSupply
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_task_runtime_db import ScriptedAppServer

from app.application.workbench.catalog import pin_knowbot
from app.application.workbench.delegation import DelegationService
from app.application.workbench.expert_data import ExpertData
from app.application.workbench.project_service import ProjectService
from app.infrastructure.workbench.repository import WorkbenchRepository

QUESTION = "核对文件并复核，交付报告。"


async def test_manual_expert_enters_delegation_without_directory_or_model_matching(db):
    doc = json.loads(
        (
            Path(__file__).parent / "fixtures/capability-methods-manifest.json"
        ).read_text()
    )
    doc["rule_sources"] = [
        {"rule": "合成核对", "source": "user_confirmed", "ref": "synthetic:test"}
    ]
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        async with db() as session:
            repo = WorkbenchRepository(session)
            project = await ProjectService(repo).create(
                owner_actor_id=OWNER,
                environment_id=ENV,
                workspace_root=WORKSPACE,
                path="manual-declaration",
            )
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

            svc = DelegationService(repo, dynamic_method_owners=frozenset({AUTHOR}))
            assert (
                await svc.data_needs(owner_actor_id=OWNER, profile_id=pinned.profile_id)
            )["scope_check_required"] is False
            args = dict(
                owner_actor_id=OWNER,
                project_id=project["id"],
                profile_id=pinned.profile_id,
                question=QUESTION,
                idempotency_key="manual-without-ai",
                user_token=token,
                expert_data=ExpertData(
                    FakeKnowbase(), FakeSupply(), supply_enabled=False
                ),
            )
            made = await svc.from_expert_entry(**args)
            assert (await svc.from_expert_entry(**args))["task_id"] == made["task_id"]
            task = await repo.get_task(made["task_id"])
            brief = task["original_input"]["task_requirements"]
            assert brief["selection_basis"] == "manual"
            assert brief["match_task_id"] is None
            assert brief["requirements"][0]["statement"] == QUESTION
            assert not fake.turn_inputs
    finally:
        await fake.close()
