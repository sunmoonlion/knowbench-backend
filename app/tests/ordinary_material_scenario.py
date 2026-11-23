"""Ordinary user flow using frozen read methods and the provider's material consent."""

import hashlib
from uuid import uuid4

from test_capabilities_db import approval
from test_knowbase_method_read_db import owner_token
from test_ordinary_expert_execution_db import selected_match
from test_workbench_ledger_db import OWNER
from test_workbench_task_runtime_db import drive

from app.infrastructure.workbench.repository import WorkbenchRepository


async def ordinary_read(db, monkeypatch, factory, chain, library, *, changed_receipt):
    material_slot = "source_document" if len(chain) == 3 else "material_id"
    expert = chain[-1]
    versions = {method.reference for method in chain}
    async with selected_match(
        db,
        monkeypatch,
        expert,
        versions,
        OWNER,
        factory,
        token=owner_token,
        library=library,
    ) as match:
        client, path, selected, runner, _, _ = match
        pin = {
            "material_id": library.material_id,
            "version_no": 1,
            "sha256": hashlib.sha256(b"verified").hexdigest(),
        }
        body = {
            "expected_revision": 0,
            "review_digest": selected["review_digest"],
            "budget_limit": "1",
            "values": {},
            "materials": {material_slot: pin},
            "accounts": {},
        }
        prep = path + "/preparation"

        async def preview(n):
            response = await client.get(path + f"/execution-preview?revision={n}")
            assert response.status_code == 200, response.text
            return response.json()

        assert (
            await client.put(prep, json={**body, "values": {"version_no": 2}})
        ).status_code == 200
        bad = await preview(1)
        assert bad["dispatch_gap"] and any(
            i["code"] == "material_authority_required" for i in bad["issues"]
        )
        assert (
            await client.put(
                path + "/execution",
                json={"revision": 1, "preview_digest": bad["preview_digest"]},
            )
        ).status_code == 409
        assert (
            await client.put(prep, json={**body, "expected_revision": 1})
        ).status_code == 200
        view = await preview(2)
        assert not view["dispatch_gap"] and not view["issues"], view
        assert view["steps"][0]["material"] == pin
        assert view["bindings"]["values"] == {
            material_slot: pin["material_id"],
            "version_no": 1,
        }
        submit = {"revision": 2, "preview_digest": view["preview_digest"]}
        response = await client.put(path + "/execution", json=submit)
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            attempts = await repo.list_attempts(task_id)
            call = await repo.capability_call(
                OWNER, attempts[-1]["refs"]["capability_call_id"]
            )
        assert call["detail"]["material_sha256"] == pin["sha256"]
        assert call["status"] == "awaiting_approval"
        action = "/api/workbench/capability-calls/" + str(call["id"])
        consent = action + "/knowbase-read"
        request = (await client.get(consent)).json()
        assert request["read"]["path"][0]["version"] == expert.reference.version
        assert [node["method_id"] for node in request["read"]["path"]] == [
            str(method.reference.method_id) for method in reversed(chain)
        ]
        grant = {
            "subject_digest": request["subject_digest"],
            "expires_at": request["expires_at"],
        }
        for mismatch in ({"sha256": "0" * 64}, {"grant_id": str(uuid4())}):
            assert (
                await client.post(consent, json={**grant, **mismatch})
            ).status_code == 403
        result = await client.post(consent, json={**grant, "sha256": pin["sha256"]})
        assert result.status_code == 200, result.text
        response = await client.post(
            action + "/decision", json=approval(call).model_dump(mode="json")
        )
        assert response.status_code == 200, response.text
        await drive(runner)
        completed = (await client.get(path)).json()
        if changed_receipt:
            assert response.json()["status"] == "unknown", response.text
            assert not completed.get("delivery")
            await drive(runner)
            return
        assert completed["execution_state"] == "SUCCEEDED", completed
        assert completed["delivery"]["text"] == "verified"
        assert completed["delivery"]["source"]["sha256"] == pin["sha256"]
        assert (await client.put(path + "/execution", json=submit)).json()[
            "task_id"
        ] == task_id
