import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy import text
from test_capabilities_db import body as connection_body
from test_capabilities_db import service as capabilities
from test_find_expert_db import _project
from test_formal_expert_intake_db import Registry, proposal, web_app
from test_formal_host_calls_db import FORMAL_OPERATION
from test_knowbot_test_runs_db import runner_for, setup_test_computer
from test_workbench_ledger_db import OTHER, OWNER
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal
from test_workbench_task_runtime_db import ScriptedAppServer, close, drive

from app.domain.workbench.tool_operations import OperationCatalog
from app.infrastructure.external.knowbase_data_v2 import KnowbaseDataV2
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.http.web import workbench_expert as routes


async def test_prepare_from_browser_rechecks_version_connection_budget_and_revision(
    db, monkeypatch
):
    registry = Registry()
    registry.methods = SimpleNamespace(operations=OperationCatalog((FORMAL_OPERATION,)))
    fake = ScriptedAppServer([json.dumps(proposal(registry))])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        async with db() as session:
            asset = await capabilities(WorkbenchRepository(session)).create_connection(
                OWNER, connection_body()
            )
        app = web_app(db, registry, monkeypatch)
        pin = {"material_id": str(uuid4()), "version_no": 2, "sha256": "a" * 64}
        calls = []
        held = True

        def material_service(request):
            assert (
                request.headers["authorization"] == "Bearer scoped-test-user"
                and request.method == "GET"
            )
            calls.append(request.url.path)
            if request.url.path.endswith("/materials"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "material": {
                                "material_id": pin["material_id"],
                                "name": "名册",
                                "deleted": False,
                            },
                            "version": pin,
                        }
                    ],
                )
            if request.url.path.endswith("/" + pin["material_id"]):
                if not held:
                    return httpx.Response(404, json={"code": "item_not_found"})
                return httpx.Response(
                    200,
                    json={
                        "material": {
                            "material_id": pin["material_id"],
                            "name": "名册",
                            "deleted": False,
                        },
                        "version": {**pin, "version_no": 3},
                    },
                )
            assert request.url.path.endswith("/" + pin["material_id"] + "/versions")
            return (
                httpx.Response(200, json=[pin])
                if held
                else httpx.Response(404, json={"code": "item_not_found"})
            )

        library = KnowbaseDataV2(
            {"KNOWBASE_INTERNAL_URL": "https://metadata.test"},
            transport=httpx.MockTransport(material_service),
        )
        monkeypatch.setattr(routes, "knowbase_v2", lambda: library)

        async def token():
            return "scoped-test-user"

        app.dependency_overrides[routes.get_web_knowbase_token_provider] = lambda: token
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            start = await client.post(
                f"/api/workbench/projects/{project}/find-expert",
                json={
                    "idempotency_key": "prepare-question",
                    "question": "需要差异表",
                    "match_contract": "knowbench/expert-match/v5",
                },
            )
            tid = start.json()["task_id"]
            path = "/api/workbench/find-expert/" + tid
            prep = path + "/preparation"
            await drive(runner)
            result = (await client.get(path)).json()["formal"]
            assert (await client.get(prep)).status_code == 409
            selected = {
                "reference": result["options"][0]["reference"],
                "review_digest": result["review_digest"],
            }
            assert (
                await client.put(path + "/selection", json=selected)
            ).status_code == 200
            page = await client.get(prep)
            assert (
                page.status_code == 200
                and "no-store" in page.headers["cache-control"]
                and calls == []
            )
            option = page.json()["account_options"]["deliver-notice"][0]
            assert (
                option["pin"]["asset_id"] == asset["id"]
                and "only-server-knows" not in page.text
            )
            assert (await client.get(prep + "?library=true")).json()["materials"] == [
                {**pin, "name": "名册"}
            ]
            body = {
                "review_digest": result["review_digest"],
                "expected_revision": 0,
                "accounts": {"deliver-notice": option["pin"]},
                "materials": {"to": pin},
                "budget_limit": "10",
            }
            wrong = {**pin, "sha256": "b" * 64}
            assert (
                await client.put(prep, json={**body, "materials": {"to": wrong}})
            ).status_code == 409
            assert (
                await client.put(prep, json={**body, "materials": {"unknown": pin}})
            ).status_code == 409
            assert (
                await client.put(prep, json={**body, "budget_limit": "0"})
            ).status_code == 422

            async def save():
                return await client.put(prep, json=body)

            first, retry = await asyncio.gather(save(), save())
            assert (
                first.status_code == retry.status_code == 200
                and first.json() == retry.json()
            )
            saved = (await client.get(prep)).json()
            assert saved["revision"] == 1 and saved["bindings"]["budget"] == {
                "currency": "CNY",
                "limit": "10.00",
            }
            assert (
                await client.put(prep, json={**body, "budget_limit": "20"})
            ).status_code == 409
            url = path + "/execution-preview?revision=1"
            before = (await client.get(url)).json()
            assert before["issues"] == [
                {"code": "material_authority_required", "slot": "to", "path": []},
                {"code": "input_required", "slot": "to", "path": []},
            ]
            assert before["estimated_maximum"] == "0" and before["steps"][0][
                "path"
            ] == ["only"]
            assert (await client.get(url)).json() == before
            assert (
                await client.put(
                    prep, json={**body, "values": {"to": 3}, "expected_revision": 1}
                )
            ).status_code == 409
            assert (
                await client.put(prep, json={**body, "values": {"to": chr(0)}})
            ).status_code == 422
            edited = {
                **body,
                "values": {"to": "test@example.test"},
                "expected_revision": 1,
                "budget_limit": None,
            }
            assert (await client.put(prep, json=edited)).json()["revision"] == 2
            assert (await save()).status_code == 409
            assert (await client.get(url)).status_code == 409
            url = path + "/execution-preview?revision=2"
            complete = (await client.get(url)).json()
            assert (
                complete["issues"]
                == [{"code": "material_authority_required", "slot": "to", "path": []}]
                and complete["bindings"]["values"] == edited["values"]
            )
            assert complete["preview_digest"] != before["preview_digest"]
            deps = registry.dependencies.copy()
            registry.dependencies.clear()
            revoked = (await client.get(url)).json()
            assert (
                any(i["code"] == "dependency_unavailable" for i in revoked["issues"])
                and revoked["estimated_maximum"] is None
            )
            registry.dependencies.update(deps)
            held = False
            async with db() as session:
                await session.execute(
                    text(
                        "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                    ),
                    {"id": asset["id"]},
                )
                await session.commit()
            stale = (await client.get(prep)).json()
            assert stale["invalid_accounts"] == ["deliver-notice"] and stale[
                "invalid_materials"
            ] == ["to"]
            assert (
                await client.put(prep, json={**edited, "expected_revision": 2})
            ).status_code == 409
            clear = {**edited, "expected_revision": 2, "materials": {}, "accounts": {}}
            assert (await client.put(prep, json=clear)).json()["revision"] == 3
            app.dependency_overrides[routes.get_web_current_user] = lambda: principal(
                OTHER
            )
            assert (await client.get(prep + "?library=true")).status_code == 404
            assert (await client.put(prep, json=clear)).status_code == 404
            app.dependency_overrides[routes.get_web_current_user] = lambda: principal(
                OWNER
            )
            entries = registry.entries.copy()
            registry.entries.clear()
            assert (await client.get(prep)).status_code == 404
            assert (await client.put(prep, json=clear)).status_code == 404
            async with db() as s:
                assert (
                    await s.execute(text("select count(*) from workbench_calls"))
                ).scalar_one() == 0
                assert (
                    await s.execute(text("select count(*) from workbench_tasks"))
                ).scalar_one() == 1
            assert len(fake.turn_inputs) == 1
            registry.entries.update(entries)
            revised = await client.post(
                f"/api/workbench/projects/{project}/find-expert",
                json={
                    "idempotency_key": "revised-prepare-question",
                    "question": "需要逐笔差异表",
                    "revises": tid,
                    "reference": selected["reference"],
                    "match_contract": "knowbench/expert-match/v5",
                },
            )
            assert revised.status_code == 201
            assert (await client.get(prep)).status_code == 409
            assert (await client.put(prep, json=clear)).status_code == 409
    finally:
        await close(runner)
        await fake.close()


async def test_edit_boolean_to_number_and_read_back(db, monkeypatch):
    registry = Registry()
    registry.methods = SimpleNamespace(operations=OperationCatalog(()))
    for old, definition in list(registry.definitions.items()):
        doc = definition.document()
        doc["procedure"]["steps"][0]["inputs"]["to"]["slot"]["schema"] = {
            "type": ["boolean", "integer"]
        }
        definition = type(definition).model_validate(doc)
        ref = old.model_copy(update={"digest": definition.digest()})
        del registry.definitions[old], registry.entries[old]
        registry.definitions[ref] = definition
        registry.entries[ref] = registry.public_declaration(ref, definition)
    fake = ScriptedAppServer([json.dumps(proposal(registry))])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        app = web_app(db, registry, monkeypatch)

        async def library_token():
            raise AssertionError("No materials were selected")

        app.dependency_overrides[routes.get_web_knowbase_token_provider] = lambda: (
            library_token
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            started = await client.post(
                f"/api/workbench/projects/{project}/find-expert",
                json={
                    "idempotency_key": "typed-preparation",
                    "question": "需要差异表",
                    "match_contract": "knowbench/expert-match/v5",
                },
            )
            started.raise_for_status()
            path = "/api/workbench/find-expert/" + started.json()["task_id"]
            await drive(runner)
            outcome = (await client.get(path)).json()["formal"]
            fingerprint = outcome["review_digest"]
            selection = await client.put(
                path + "/selection",
                json={
                    "reference": outcome["options"][0]["reference"],
                    "review_digest": fingerprint,
                },
            )
            selection.raise_for_status()
            prep = path + "/preparation"
            body = {
                "review_digest": fingerprint,
                "expected_revision": 0,
                "accounts": {},
                "materials": {},
                "budget_limit": "5",
                "values": {"to": True},
            }
            first = await client.put(prep, json=body)
            first.raise_for_status()
            assert first.json()["revision"] == 1
            second = await client.put(
                prep, json={**body, "expected_revision": 1, "values": {"to": 1}}
            )
            second.raise_for_status()
            assert type(second.json()["bindings"]["values"]["to"]) is int
            assert second.json()["revision"] == 2
            stored = (await client.get(prep)).json()
            assert type(stored["bindings"]["values"]["to"]) is int
            assert stored["bindings"]["values"]["to"] == 1
    finally:
        await close(runner)
        await fake.close()
