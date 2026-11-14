"""Actual persistence/HTTP: reviewed operations gate exposure, approval and dispatch."""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import OPERATION, OWNER, approval, prepared, service
from test_capability_routes_db import application
from test_method_capabilities_db import setup_method
from test_workbench_ledger_db import db as db
from tool_invocation_support import ObservedCapabilityTransport, request_call

from app.domain.workbench.capabilities import CapabilityError, SelectionPut
from app.domain.workbench.tool_operations import McpOperation
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import capability_routes as routes


def restricted(*values):
    data = OPERATION.model_dump(mode="json")
    data["resources"][0]["allowed_values"] = list(values)
    return McpOperation.model_validate(data)


async def use_policy(db, sid, remote, policy):
    async with db() as session:
        svc = service(WorkbenchRepository(session), remote, operations=(policy,))
        runtime = await svc.prepare(OWNER, sid)
        return runtime["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]


async def test_unreviewed_tool_is_retained_but_cannot_be_exposed_or_selected(db):
    sid, asset, key, token, remote = await prepared(db)
    async with db() as session:
        svc = service(WorkbenchRepository(session), remote, operations=())
        selection = await svc.selection(OWNER, sid)
        assert selection["tools"] == [key]
        assert {"ref": key, "code": "operation_unavailable"} in selection["issues"]
        assert selection["available_tools"][0]["status"] == "operation_unavailable"
        assert (await svc.assets(OWNER))[0]["id"] == asset["id"]
        with pytest.raises(CapabilityError, match="reviewed"):
            await svc.save_selection(
                OWNER, sid, SelectionPut(revision=selection["revision"], tools=[key])
            )
        # Even a previously prepared scope cannot bypass a removed operator policy.
        with pytest.raises(CapabilityError, match="reviewed"):
            await request_call(svc, sid, token, key, {"to": "a@example.test"})
        assert await svc.calls(OWNER, sid) == []
        runtime = await svc.prepare(OWNER, sid)
        assert runtime["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is False
        assert "operation_unavailable" in str(runtime["inputs"])
        assert (await svc.selection(OWNER, sid))["tools"] == [key]
        assert not remote.calls


async def test_policy_removal_is_honored_by_live_mcp_listing_and_skill_dependencies(db):
    sid, _, _, _, skill, _, key, token, remote = await setup_method(db)
    app = application(db, remote)

    async def dependency():
        async with db() as session:
            yield service(WorkbenchRepository(session), remote, operations=())

    app.dependency_overrides[routes.service] = dependency
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listed = (
            await client.post(
                f"/api/mcp/capabilities/{sid}",
                headers={"Authorization": "Bearer " + token},
                json={"id": 1, "method": "tools/list"},
            )
        ).json()["result"]["tools"]
        assert key not in {t["name"] for t in listed}
        assert {t["name"] for t in listed} == {"skill_read", "skill_files"}
    async with db() as session:
        svc = service(WorkbenchRepository(session), remote, operations=())
        read = await svc.skill_resource(
            sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
        )
        assert read["connection_dependencies"][0]["selected_tools"] == []
        # Required method capabilities are not silently omitted as in ordinary chat.
        with pytest.raises(CapabilityError, match="need attention"):
            await svc.prepare(OWNER, sid, scene="method")
        assert not remote.calls


async def test_http_resource_ceiling_is_checked_before_approval_and_dispatch(db):
    sid, _, key, _, remote = await prepared(db)
    policy = restricted("approved@example.test")
    token = await use_policy(db, sid, remote, policy)
    app = application(db, remote)

    async def dependency():
        async with db() as session:
            yield service(WorkbenchRepository(session), remote, operations=(policy,))

    app.dependency_overrides[routes.service] = dependency
    async with AsyncClient(
        transport=ObservedCapabilityTransport(app, db), base_url="http://testserver"
    ) as client:

        async def invoke(recipient):
            return (
                await client.post(
                    f"/api/mcp/capabilities/{sid}",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": key, "arguments": {"to": recipient}},
                    },
                )
            ).json()

        rejected = await invoke("outsider@example.test")
        assert "error" in rejected and "outside" in str(rejected)
        assert (
            await client.get(f"/api/workbench/sessions/{sid}/capability-calls")
        ).json()["calls"] == []
        result = await invoke("approved@example.test")
        call_id = json.loads(result["result"]["content"][0]["text"])["call_id"]
        calls = (
            await client.get(f"/api/workbench/sessions/{sid}/capability-calls")
        ).json()["calls"]
        assert len(calls) == 1 and not remote.calls
        assert "operation" not in calls[0]["detail"]  # public contract unchanged
        done = await client.post(
            f"/api/workbench/capability-calls/{call_id}/decision",
            json=approval(calls[0]).model_dump(),
        )
        assert done.status_code == 200 and done.json()["status"] == "succeeded"
        assert remote.calls[0][3] == {"to": "approved@example.test"}
    async with db() as session:
        call = await WorkbenchRepository(session).capability_call(OWNER, call_id)
        assert call["detail"]["operation"] == policy.resolve(
            {"to": "approved@example.test"}
        )


@pytest.mark.parametrize("change", ["remove", "narrow", "broaden", "effect", "version"])
async def test_changed_policy_invalidates_prepared_scope_and_pending_approval(
    db, change
):
    sid, _, key, token, remote = await prepared(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "a@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        if change == "remove":
            policies = ()
        elif change in ("narrow", "broaden"):
            policies = (
                restricted(
                    *(
                        ["b@example.test"]
                        if change == "narrow"
                        else ["a@example.test", "b@example.test"]
                    )
                ),
            )
        else:
            data = OPERATION.model_dump(mode="json")
            data[change] = "read" if change == "effect" else "2"
            policies = (McpOperation.model_validate(data),)
        changed = service(repo, remote, operations=policies)
        with pytest.raises(CapabilityError):
            await request_call(changed, sid, token, key, {"to": "a@example.test"})
        with pytest.raises(CapabilityError):
            await changed.decide(OWNER, call["id"], approval(call))
        assert not remote.calls
        assert (await repo.capability_call(OWNER, call["id"]))[
            "status"
        ] == "awaiting_approval"
        # Policy withdrawal cannot prevent refusing an old request.
        assert (await changed.decide(OWNER, call["id"], approval(call, "decline")))[
            "status"
        ] == "declined"


async def test_missing_policy_evidence_is_not_reconstructed_for_old_approval(db):
    sid, _, key, token, remote = await prepared(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "a@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_calls SET detail=detail-'operation' WHERE id=:id"
                ),
                {"id": call["id"]},
            )
        with pytest.raises(CapabilityError, match="policy changed"):
            await svc.decide(OWNER, call["id"], approval(call))
        assert not remote.calls


async def test_reviewed_read_still_requires_user_approval_and_records_outbound_data(db):
    sid, _, key, _, remote = await prepared(db)
    data = OPERATION.model_dump(mode="json")
    data["effect"] = "read"
    policy = McpOperation.model_validate(data)
    token = await use_policy(db, sid, remote, policy)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo, remote, operations=(policy,))
        staged = await request_call(svc, sid, token, key, {"to": "query@example.test"})
        assert staged["status"] == "awaiting_approval" and not remote.calls
        call = await repo.capability_call(OWNER, staged["call_id"])
        assert call["detail"]["operation"]["data_transfer"]["arguments_digest"]
