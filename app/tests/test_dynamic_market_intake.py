"""Marketplace HTTP admission, immutable task catalogs and gate withdrawal (real DB)."""

import pytest
from sqlalchemy import text
from test_referenced_method_catalog import manifest
from test_workbench_expert_data_db import FakeKnowbase, FakeSupply, known
from test_workbench_knowbots_db import PROFILE, ask, pin
from test_workbench_knowbots_db import knowmart as knowmart
from test_workbench_ledger_db import db as db
from test_workbench_project_routes import machine, project
from test_workbench_routes import A, B
from test_workbench_routes import make_client as make_client

import app.interfaces.http.web.workbench_expert as routes
from app.application.workbench.expert_data import ExpertData
from app.application.workbench.plan_receipts import catalog_for_task
from app.bootstrap import workbench
from app.domain.workbench.knowbots import manifest_digest
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.http.middleware.auth import get_web_knowbase_token_provider
from core.config import Settings


@pytest.mark.parametrize(
    "setting", ["KNOWBOT_DYNAMIC_TEST_OWNERS", "KNOWBOT_DYNAMIC_MARKET_OWNERS"]
)
@pytest.mark.parametrize("subject", [f"sub-{A}", f"sub-{B}"])
async def test_only_marketplace_owner_setting_admits_new_pins(
    make_client, db, knowmart, monkeypatch, setting, subject
):
    knowmart.give(f"sub-{A}", manifest())
    monkeypatch.setattr(
        workbench, "get_settings", lambda: Settings(**{setting: [subject]})
    )
    allowed = setting == "KNOWBOT_DYNAMIC_MARKET_OWNERS" and subject == f"sub-{A}"
    async with make_client(A) as http:
        response = await pin(http)
        assert response.status_code == (200 if allowed else 422), response.text
        if allowed:
            assert response.json()["pack"]["steps"] == []
            assert "method_text" not in response.text
    async with db() as session:
        count = (
            await session.execute(text("select count(*) from workbench_knowbot_pins"))
        ).scalar_one()
        assert count == int(allowed)


async def test_dynamic_delegation_freezes_catalog_and_survives_admission_closure(
    make_client, db, knowmart, monkeypatch
):
    doc = manifest()
    knowmart.give(f"sub-{A}", doc)
    monkeypatch.setattr(
        workbench,
        "get_settings",
        lambda: Settings(KNOWBOT_DYNAMIC_MARKET_OWNERS=[f"sub-{A}"]),
    )
    base = FakeKnowbase()
    monkeypatch.setattr(
        routes,
        "expert_data",
        lambda: ExpertData(base, FakeSupply(), supply_enabled=False),
    )

    async def token():
        return "user-token"

    make_client.app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
        token
    )
    await known(db)
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        assert (await pin(http)).status_code == 200
        needs = await http.get(f"/api/workbench/packs/{PROFILE}/data-needs")
        assert needs.json()["contract"] == "knowbot-manifest/v3"
        request = ask()
        made = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=request
        )
        assert made.status_code == 201, made.text
        task_id = made.json()["task_id"]
        shown = await http.get(f"/api/workbench/tasks/{task_id}/steps")
        assert shown.status_code == 200, shown.text
        assert shown.json()["steps"] == []  # no invented fixed sequence
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = await repo.get_task(task_id)
            assert row["pack_source"] == "knowmart"
            assert row["pack_digest"] == manifest_digest(doc)
            assert (
                catalog_for_task(row).method_sources.keys()
                == doc["pack"]["method_sources"].keys()
            )
            assert "workflow" not in row["pack_snapshot"]
            assert await repo.get_task_resolution(task_id) is not None

        monkeypatch.setattr(workbench, "get_settings", lambda: Settings())
        replay = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=request
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["task_id"] == task_id and not replay.json()["created"]
        assert (
            await http.get(f"/api/workbench/tasks/{task_id}/steps")
        ).status_code == 200
        refused = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=ask()
        )
        assert refused.status_code == 422, refused.text
        forged = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask() | {"dynamic_method_owners": [f"sub-{A}"]},
        )
        assert forged.status_code == 422
    async with make_client(B) as http:
        assert (await http.get(f"/api/workbench/packs/{PROFILE}")).status_code == 404
        assert (
            await http.get(f"/api/workbench/tasks/{task_id}/steps")
        ).status_code == 404


@pytest.mark.parametrize("owners", [["*"], ["x", "x"], [""], [" x"]])
def test_market_owner_configuration_is_exact(owners):
    with pytest.raises(ValueError):
        Settings(KNOWBOT_DYNAMIC_MARKET_OWNERS=owners)


@pytest.mark.parametrize("review", [False, True])
async def test_marketplace_pin_to_runner_executes_selected_methods_and_data_stage(
    db, review
):
    from knowbot_fixtures import KNOWBOT, FakeSummoner
    from method_plan_fixtures import proposal
    from test_knowbot_test_runs_db import (
        AUTHOR,
        ENV,
        WORKSPACE,
        close,
        drive,
        runner_for,
        setup_test_computer,
    )
    from test_method_advisor_db import ROWS
    from test_workbench_advisor_db import ScriptedAppServer
    from test_workbench_ledger_db import OWNER

    from app.application.workbench.catalog import pin_knowbot
    from app.application.workbench.delegation import DelegationService
    from app.application.workbench.execution_plan import final_results
    from app.application.workbench.project_service import ProjectService

    plan = proposal(review=review)
    # Synthetic document is part of the question, so the data stage needs no new data.
    fake = ScriptedAppServer(
        [plan.model_dump_json(), "The question includes all input.", ROWS]
        + (['{"notes":"checked"}'] if review else [])
        + [ROWS]
    )
    await fake.start()
    runner = runner_for(db, records=True)
    try:
        await setup_test_computer(db, fake)
        summoner = FakeSummoner()
        doc = manifest()
        summoner.give(AUTHOR, doc)

        async def token():
            return "user-token"

        async with db() as session:
            repo = WorkbenchRepository(session)
            await pin_knowbot(
                repo,
                summoner,
                owner_actor_id=OWNER,
                owner_subject=AUTHOR,
                ref=f"{KNOWBOT}@1",
                dynamic_method_owners=frozenset({AUTHOR}),
            )
            place = await ProjectService(repo).create(
                owner_actor_id=OWNER,
                environment_id=ENV,
                workspace_root=WORKSPACE,
                path="synthetic-review",
            )
            made = await DelegationService(
                repo, dynamic_method_owners=frozenset({AUTHOR})
            ).from_expert_entry(
                owner_actor_id=OWNER,
                project_id=place["id"],
                profile_id=PROFILE,
                question="Synthetic document: id=one. "
                + ("Review it." if review else "List it."),
                idempotency_key="market-dynamic-run",
                user_token=token,
                expert_data=ExpertData(
                    FakeKnowbase(), FakeSupply(), supply_enabled=False
                ),
            )
        # No direct task/snapshot writes and no intake gate needed for an accepted run.
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = await repo.get_task(made["task_id"])
            assert row["state"] == "SUCCEEDED", (
                row["failure_reason"] if "failure_reason" in row else row["state"]
            )
            assert row["pack_source"] == "knowmart"
            assert row["pack_digest"] == manifest_digest(doc)
            assert row["accepted_plan_version"] == 1
            result = (await final_results(repo, row))["report"]
            assert result["content"] == {"rows": [{"id": "one"}]}
            attempts = await repo.list_attempts(made["task_id"])
            assert len(attempts) == (5 if review else 4)  # planning + data + methods
            assert (await repo.get_task_resolution(made["task_id"]))[
                "status"
            ] == "ready"
            data_prompt = str(fake.turn_inputs[1])
            # SDD 0051（所有者 11-07）：需求只说要什么，不叫模型先找供应商、不写渠道
            assert "discover_source_tools" not in data_prompt
            assert "metrics (chart-of-accounts codes" in data_prompt
    finally:
        await close(runner)
        await fake.close()
