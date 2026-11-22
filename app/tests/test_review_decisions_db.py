"""Owner-only adjudication of a sealed review, through the browser API and real ledger."""

import asyncio
from functools import partial
from uuid import uuid4

import httpx
from branch_review_support import review_case
from test_formal_method_use_routes_db import application
from test_instruction_runner_db import drive, stack
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal

from app.application.workbench.project_service import ProjectService
from app.interfaces.endpoints import workbench_routes as routes


async def test_owner_adjudication_is_pinned_replayable_and_preserves_history(
    db, monkeypatch
):
    s = await stack(
        db,
        setup_case=partial(review_case, monkeypatch=monkeypatch),
        result={"result": "Missing evidence. Totals agree."},
    )
    app = application(db, lambda repo: None)

    async def session_dep():
        async with db() as sql:
            yield sql

    app.dependency_overrides[routes.require_workbench_enabled] = lambda: None
    app.dependency_overrides[routes.get_db_session] = session_dep
    url = f"/api/workbench/tasks/{s.task}/review-decisions"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver"
    ) as web:
        assert (await web.get(url)).status_code == 409
        s.units.ready = True
        await drive(s)
        async with s.host() as svc, svc.repo.transaction():
            task = await svc.repo.get_task(s.task)
            project = await ProjectService(svc.repo).view(
                str(task["project_id"]), owner_actor_id=str(task["owner_actor_id"])
            )
            assert [
                row["id"] for row in project["tasks"] if row["has_review_report"]
            ] == [s.task]
            await svc.repo.put_artifact(
                task_id=s.task,
                attempt_id=None,
                name="formal-step:direct",
                kind="step",
                content={"result": "Newer report."},
            )
        response = await web.get(url)
        assert response.status_code == 200, response.text
        page = response.json()
        assert (
            page["revision"] == 0
            and page["reports"][0]["text"] == "Missing evidence. Totals agree."
        )
        item = {
            "artifact_id": page["reports"][0]["id"],
            "quote": "Missing evidence.",
            "decision": "deferred",
            "reason": "Await signed appendix.",
        }
        body = {
            "review_digest": page["review_digest"],
            "previous_revision": 0,
            "items": [item],
        }
        for change in ({"quote": "Newer report."}, {"artifact_id": str(uuid4())}):
            assert (
                await web.put(url, json={**body, "items": [{**item, **change}]})
            ).status_code == 409
        assert (
            await web.put(url, json={**body, "items": [item, item]})
        ).status_code == 422
        both = await asyncio.gather(*(web.put(url, json=body) for _ in range(2)))
        assert both[0].json() == both[1].json() and both[0].json()["revision"] == 1
        updated = {**item, "decision": "accepted", "reason": "Appendix now available."}
        assert (
            await web.put(url, json={**body, "items": [updated]})
        ).status_code == 409
        assert (
            await web.put(
                url, json={**body, "items": [updated], "previous_revision": 1}
            )
        ).json()["revision"] == 2
        assert (await web.get(url)).json()["items"] == [updated]
        assert (await web.get(url, params={"revision": 1})).json()["items"] == [item]
        assert (await web.get(url, params={"revision": 3})).status_code == 404
        app.dependency_overrides[routes.get_web_current_user] = lambda: principal(OTHER)
        assert (
            (await web.get(url)).status_code
            == (await web.put(url, json=body)).status_code
            == 404
        )
    async with s.host() as svc:
        assert (
            len(await svc.repo.list_attempts(s.task)) == len(s.provider_requests) == 1
        )


async def test_owner_can_keep_first_findings_after_cancelling_comparison(
    db, monkeypatch
):
    from test_workbench_ledger_db import OWNER

    from app.application.workbench.ledger import Ledger
    from app.application.workbench.review_decisions import OwnerDecision, decision_page
    from app.infrastructure.workbench.repository import WorkbenchRepository

    s = await stack(
        db,
        setup_case=partial(review_case, monkeypatch=monkeypatch, strict=True),
        result={"result": "Check the signature."},
    )
    s.units.ready = True
    await drive(s)
    async with db() as sql:
        repo = WorkbenchRepository(sql)
        await Ledger(repo).request_cancel(s.task, owner_actor_id=OWNER)
        page = await decision_page(repo, OWNER, s.task)
        assert page["task_state"] == "CANCELLED" and page["missing_stages"] == [
            "review-comparison"
        ]
        (first,) = page["reports"]
        command = OwnerDecision.model_validate(
            {
                "review_digest": page["review_digest"],
                "previous_revision": 0,
                "items": [
                    {
                        "artifact_id": first["id"],
                        "quote": first["text"],
                        "decision": "declined",
                        "reason": "The signed copy is already on record.",
                    }
                ],
            }
        )
        saved = await decision_page(repo, OWNER, s.task, command=command)
        assert saved["revision"] == 1 and saved["task_state"] == "CANCELLED"
    assert len(s.provider_requests) == 1
