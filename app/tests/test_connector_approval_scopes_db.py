"""C4 against migrated PostgreSQL and the ordinary request/approval/dispatch path."""

import asyncio
import base64
import hashlib
from copy import deepcopy

import pytest
from sqlalchemy import text
from test_capabilities_db import (
    OPERATION,
    OTHER,
    OWNER,
    URL,
    Remote,
    approval,
    body,
    service,
)
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import seed
from tool_invocation_support import request_call

from app.domain.workbench.capabilities import (
    AssetAction,
    CapabilityError,
    SelectionPut,
    tool_key,
)
from app.domain.workbench.errors import Forbidden, NotFound
from app.domain.workbench.tool_operations import McpOperation, ResourceBinding
from app.domain.workbench.tool_registry import ToolRegistration
from app.infrastructure.workbench.repository import WorkbenchRepository

READ = {
    "name": "read_record",
    "description": "Read one record",
    "inputSchema": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "view": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}
SEND = {
    "name": "send",
    "description": "Send a message",
    "inputSchema": {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "body": {"type": "string"},
            "attachments": {"type": "array"},
        },
        "required": ["to", "body", "attachments"],
        "additionalProperties": False,
    },
}


class Records(Remote):
    def __init__(self):
        super().__init__()
        self.tools = [READ, SEND]
        self.before_dispatch = None
        self.wrong_result = False

    async def call_checked(self, url, token, name, arguments, *, before_send):
        if self.before_dispatch:
            await self.before_dispatch()
        if before_send:
            await before_send()
        self.calls.append((name, deepcopy(arguments)))
        if name == "send":
            return {"content": []}
        return {
            "structuredContent": {
                "id": "other" if self.wrong_result else arguments["id"],
                "value": 10,
                "other_account": "unapproved",
            },
            "content": [{"type": "text", "text": "untagged private data"}],
            "extra": "unapproved",
        }


def policy(tool, **overrides):
    common = OPERATION.model_dump()
    common.update(
        tool_name=tool["name"],
        descriptor_digest=ToolRegistration.discover("mcp", URL, tool).descriptor_digest,
    )
    return McpOperation.model_validate(common | overrides)


READ_POLICY = policy(
    READ,
    operation="read",
    effect="read",
    resources=[ResourceBinding(resource_role="record", argument_path=("id",))],
    read_result_id_field="id",
    read_result_fields=("value",),
)
SEND_POLICY = policy(SEND, inline_attachments_field="attachments")


async def setup(
    db,
    remote,
    tool,
    *,
    policies=(READ_POLICY, SEND_POLICY),
    purpose="Read the quarterly report",
):
    _, _, sid = await seed(db)
    async with db() as session:
        svc = service(WorkbenchRepository(session), remote, operations=policies)
        a = await svc.create_connection(OWNER, body())
        key = tool_key(a["id"], tool["name"])
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=(await svc.selection(OWNER, sid))["revision"],
                tools=[key],
                purpose=purpose,
            ),
        )
        run = await svc.prepare(OWNER, sid)
        token = run["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
    return sid, a, key, token


async def prompt(svc, sid, token, key, args, identifier):
    result = await request_call(svc, sid, token, key, args, call_id=identifier)
    assert result["status"] == "awaiting_approval", result
    return await svc.repo.capability_call(OWNER, result["call_id"])


async def test_reuse_is_per_exact_arguments_owner_and_selection_and_is_revocable(db):
    remote = Records()
    sid, asset, key, token = await setup(db, remote, READ)
    async with db() as session:
        svc = service(WorkbenchRepository(session), remote, operations=(READ_POLICY,))
        args = {"id": "report", "view": "summary"}
        first = await prompt(svc, sid, token, key, args, "first")
        granted = await svc.decide(
            OWNER,
            first["id"],
            approval(first).model_copy(update={"remember_read": True}),
        )
        assert granted["detail"]["approval"]["granted"]
        assert first["task_id"] is None and len(remote.calls) == 1
        repeat = await request_call(svc, sid, token, key, args, call_id="repeat")
        assert (
            repeat["status"] == "succeeded"
            and "untagged" not in str(repeat)
            and "unapproved" not in str(repeat)
        )
        new = await svc.repo.capability_call(OWNER, repeat["call_id"])
        assert (
            new["detail"]["read_approval_id"] == first["id"]
            and new["interaction_id"] is None
            and new["task_id"] is None
        )
        assert (await request_call(svc, sid, token, key, args, call_id="repeat"))[
            "call_id"
        ] == repeat["call_id"]
        assert len(remote.calls) == 2
        await prompt(
            svc,
            sid,
            token,
            key,
            {"id": "else", "view": "summary"},
            "different-resource",
        )
        await prompt(
            svc, sid, token, key, {"id": "report", "view": "full"}, "different-outbound"
        )
        with pytest.raises(NotFound):
            await svc.revoke_read_approval(OTHER, first["id"])
        revoked = await svc.revoke_read_approval(OWNER, first["id"])
        assert revoked["detail"]["approval"]["revoked"]
        await prompt(svc, sid, token, key, args, "after-revocation")
        assert len(remote.calls) == 2


@pytest.mark.parametrize(
    "change", ["expiry", "selection", "connection", "wrong_result", "test_once"]
)
async def test_read_scope_cannot_outlive_or_expand_its_authority(db, change):
    remote = Records()
    sid, asset, key, token = await setup(db, remote, READ)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(READ_POLICY,))
        args = {"id": "a"}
        first = await prompt(svc, sid, token, key, args, "first")
        await svc.decide(
            OWNER,
            first["id"],
            approval(first).model_copy(update={"remember_read": change != "test_once"}),
        )
        if change == "expiry":
            async with svc.repo.transaction():
                await s.execute(
                    text(
                        "UPDATE workbench_calls SET read_scope_until=now() WHERE id=:id"
                    ),
                    {"id": first["id"]},
                )
        if change == "connection":
            await svc.action(
                OWNER, asset["id"], AssetAction(revision=1, action="disable")
            )
            with pytest.raises(CapabilityError):
                await request_call(svc, sid, token, key, args, call_id="after-change")
            return
        if change == "selection":
            sel = await svc.selection(OWNER, sid)
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=sel["revision"], tools=[key], purpose="Another purpose"
                ),
            )
            run = await svc.prepare(OWNER, sid)
            token = run["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
                "Authorization"
            ].split()[1]
        if change == "wrong_result":
            remote.wrong_result = True
            result = await request_call(
                svc, sid, token, key, args, call_id="bad-provider"
            )
            assert (
                result["status"] == "failed"
                and "untagged" not in str(result)
                and "unapproved" not in str(result)
            )
            return
        await prompt(svc, sid, token, key, args, "next")
        assert len(remote.calls) == 1


async def test_revocation_during_adapter_handshake_prevents_read(db):
    remote = Records()
    sid, _, key, token = await setup(db, remote, READ)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(READ_POLICY,))
        args = {"id": "a"}
        first = await prompt(svc, sid, token, key, args, "first")
        await svc.decide(
            OWNER,
            first["id"],
            approval(first).model_copy(update={"remember_read": True}),
        )

        async def revoke():
            async with db() as other:
                await service(
                    WorkbenchRepository(other), remote, operations=(READ_POLICY,)
                ).revoke_read_approval(OWNER, first["id"])

        remote.before_dispatch = revoke
        result = await request_call(
            svc, sid, token, key, args, call_id="revoked-before-send"
        )
        assert result["status"] == "failed" and len(remote.calls) == 1


async def test_write_approval_is_once_only_and_binds_recipient_body_and_bytes(db):
    remote = Records()
    sid, asset, key, token = await setup(db, remote, SEND)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(SEND_POLICY,))
        args = {
            "to": "buyer",
            "body": "draft",
            "attachments": [
                {"name": "report", "content_base64": base64.b64encode(b"v1").decode()}
            ],
        }
        first = await prompt(svc, sid, token, key, args, "write-1")
        view = (await svc.calls(OWNER, sid))[0]
        assert view["detail"]["approval"]["attachments"] == [
            {"name": "report", "size": 2, "sha256": hashlib.sha256(b"v1").hexdigest()}
        ]
        with pytest.raises(CapabilityError):
            await svc.decide(
                OWNER,
                first["id"],
                approval(first).model_copy(update={"remember_read": True}),
            )
        for index, modified in enumerate(
            [
                args | {"to": "another"},
                args | {"body": "final"},
                args | {"attachments": [{"name": "report", "content_base64": "djI="}]},
            ]
        ):
            changed = await prompt(svc, sid, token, key, modified, f"change-{index}")
            with pytest.raises(Forbidden):
                await svc.decide(OWNER, changed["id"], approval(first))
        with pytest.raises(CapabilityError):
            await request_call(
                svc,
                sid,
                token,
                key,
                args
                | {
                    "attachments": [
                        {"name": "report", "url": "https://example.test/latest"}
                    ]
                },
                call_id="mutable",
            )
        assert not remote.calls

        async def accept():
            async with db() as other:
                return await service(
                    WorkbenchRepository(other), remote, operations=(SEND_POLICY,)
                ).decide(OWNER, first["id"], approval(first))

        await asyncio.gather(accept(), accept())
        assert remote.calls == [("send", args)]
        pending = await prompt(svc, sid, token, key, args, "another-action")
        await svc.action(OWNER, asset["id"], AssetAction(revision=1, action="disable"))
        with pytest.raises(CapabilityError):
            await svc.decide(OWNER, pending["id"], approval(pending))
        assert len(remote.calls) == 1


async def test_method_awaits_owner_once_and_reuses_only_its_live_step_grant(db):
    from method_access_support import ACCESS
    from test_method_capabilities_db import setup_method

    from app.application.workbench.expert_desk import ExpertDesk
    from app.domain.workbench.capabilities import MethodPermissionRevoke

    access = deepcopy(ACCESS)
    access.update(
        operations=["read"],
        resources=[
            {"resource_kind": "connector", "resource_role": "record", "values": ["a"]}
        ],
    )
    access["data_transfer"].update(
        argument_fields=["id"], purpose="Read the selected record"
    )
    remote = Records()
    sid, task, _, _, _, _, key, token, _ = await setup_method(
        db, access=access, remote=remote, operation=READ_POLICY
    )
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(READ_POLICY,))
        args = {"id": "a"}
        first = await prompt(svc, sid, token, key, args, "task-read")
        assert (
            first["task_id"] == task
            and (await svc.repo.get_task(task))["state"] == "WAITING"
        )
        review = await ExpertDesk(svc.repo).review(
            first["interaction_id"], owner_actor_id=OWNER
        )
        assert review["subject"]["connector_call_id"] == first["id"]
        assert [o["id"] for o in review["pending"]["options"]] == [
            "accept",
            "accept_read_scope",
            "decline",
        ]
        await svc.decide(
            OWNER,
            first["id"],
            approval(first).model_copy(update={"remember_read": True}),
        )
        assert (await svc.repo.get_task(task))["state"] == "RUNNING"
        again = await request_call(
            svc, sid, token, key, args, call_id="another-task-read"
        )
        raw = await svc.repo.capability_call(OWNER, again["call_id"])
        assert (
            again["status"] == "succeeded"
            and raw["task_id"] == task
            and raw["interaction_id"] is None
        )
        assert raw["parent_call_id"] == first["parent_call_id"]
        with pytest.raises(CapabilityError):
            await request_call(svc, sid, token, key, {"id": "b"}, call_id="outside")
        view = await svc.selection(OWNER, sid)
        await svc.revoke_method_permissions(
            OWNER,
            sid,
            MethodPermissionRevoke(
                revision=view["revision"], method_context=view["method"]["context"]
            ),
        )
        with pytest.raises((CapabilityError, Forbidden)):
            await request_call(svc, sid, token, key, args, call_id="revoked-method")
        assert len(remote.calls) == 2


async def test_a2a_account_switch_cannot_inherit_another_accounts_read_approval(
    db, monkeypatch
):
    from test_connection_oauth_db import AGENT_OP, Provider

    from app.domain.workbench.tool_operations import OperationCatalog

    p = Provider(db, monkeypatch)
    _, _, sid = await seed(db)
    policy = McpOperation.model_validate(
        AGENT_OP.model_dump()
        | {
            "resources": [ResourceBinding(resource_role="task", argument_path=("id",))],
            "read_result_id_field": "id",
            "read_result_fields": ["state"],
        }
    )
    args = {"id": "external-task"}
    async with db() as s:
        svc = p.service(s)
        svc.operations = OperationCatalog((policy,))
        for index in (1, 2):
            p.account = f"external-{index}"
            asset = await p.connect()
            key = tool_key(asset["id"], policy.tool_name)
            current = await svc.selection(OWNER, sid)
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=current["revision"], tools=[key], purpose="Read one task"
                ),
            )
            run = await svc.prepare(OWNER, sid)
            token = run["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
                "Authorization"
            ].split()[1]
            call = await prompt(svc, sid, token, key, args, f"account-{index}")
            if index == 1:
                approved = approval(call)
                await svc.decide(
                    OWNER,
                    call["id"],
                    approved.model_copy(update={"remember_read": True}),
                )
                repeat = await request_call(
                    svc, sid, token, key, args, call_id="same-account"
                )
                assert repeat["result"]["structuredContent"] == {
                    "id": "external-task",
                    "state": "TASK_STATE_COMPLETED",
                }
            else:
                with pytest.raises(Forbidden):
                    await svc.decide(OWNER, call["id"], approved)
        assert p.tasks == ["external-task"] * 2


async def test_set_scope_covers_subsets_without_combining_approvals_and_filters_outputs(
    db,
):
    tool = deepcopy(READ)
    tool["inputSchema"] = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "period": {"type": "string"},
                },
                "required": ["ids", "period"],
                "additionalProperties": False,
            }
        },
        "required": ["filter"],
    }
    reviewed = policy(
        tool,
        operation="read",
        effect="read",
        resources=[
            ResourceBinding(
                resource_role="record",
                argument_path=("filter", "ids"),
                cardinality="many",
            )
        ],
        read_result_id_field="id",
        read_result_items_field="records",
        read_result_fields=["value"],
    )

    class Batch(Records):
        async def call_checked(self, url, token, name, arguments, *, before_send):
            await before_send()
            self.calls.append(arguments)
            ids = list(arguments["filter"]["ids"])
            if self.wrong_result:
                ids[-1] = "external-record"
            return {
                "structuredContent": {
                    "records": [{"id": i, "value": 3, "extra": "hidden"} for i in ids],
                    "extra": "hidden",
                },
                "content": [{"type": "text", "text": "hidden"}],
            }

    remote = Batch()
    remote.tools = [tool]
    sid, _, key, token = await setup(db, remote, tool, policies=(reviewed,))

    def args(ids, period="2026"):
        return {"filter": {"ids": ids, "period": period}}

    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(reviewed,))
        for ids, identifier in [(["a", "b"], "first"), (["c"], "second")]:
            initial = await prompt(svc, sid, token, key, args(ids), identifier)
            await svc.decide(
                OWNER,
                initial["id"],
                approval(initial).model_copy(update={"remember_read": True}),
            )
        sub = await request_call(svc, sid, token, key, args(["b"]), call_id="subset")
        assert sub["result"]["structuredContent"] == {
            "records": [{"id": "b", "value": 3}]
        } and "hidden" not in str(sub)
        await prompt(svc, sid, token, key, args(["b", "c"]), "two-grants")
        await prompt(svc, sid, token, key, args(["a"], "2025"), "new-filter")
        remote.wrong_result = True
        bad = await request_call(
            svc, sid, token, key, args(["a", "b"]), call_id="bad-result"
        )
        assert (
            bad["status"] == "failed"
            and "hidden" not in str(bad)
            and "external-record" not in str(bad)
        )
        await svc.revoke_read_approval(OWNER, initial["id"])
        await prompt(svc, sid, token, key, args(["c"]), "revoked-subset")
        remote.wrong_result = False
        assert (
            await request_call(svc, sid, token, key, args(["a"]), call_id="other-grant")
        )["status"] == "succeeded"


@pytest.mark.parametrize("surface", ["card", "interaction"])
async def test_http_approval_and_revoke_use_authenticated_owner_receipt(
    db, monkeypatch, surface
):
    import httpx
    from test_capability_routes_db import application

    from app.bootstrap import workbench
    from app.interfaces.endpoints import capability_routes, workbench_routes

    remote = Records()
    sid, _, key, token = await setup(db, remote, READ)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(READ_POLICY,))
        pending = await prompt(svc, sid, token, key, {"id": "a"}, "api-call")
    app = application(db, remote)

    async def scoped_service():
        async with db() as s:
            yield service(WorkbenchRepository(s), remote, operations=(READ_POLICY,))

    async def db_session():
        async with db() as s:
            yield s

    monkeypatch.setattr(
        workbench,
        "capability_service",
        lambda repo: service(repo, remote, operations=(READ_POLICY,)),
    )
    app.dependency_overrides[workbench_routes.get_db_session] = db_session
    app.dependency_overrides[workbench_routes.require_workbench_enabled] = lambda: None
    app.dependency_overrides[capability_routes.service] = scoped_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as http:
        path = "/api/workbench/capability-calls/" + pending["id"]
        body = approval(pending).model_dump() | {"remember_read": True}
        if surface == "card":
            receipt = await http.post(path + "/decision", json=body)
        else:
            receipt = await http.post(
                "/api/workbench/interactions/" + pending["interaction_id"] + "/respond",
                json={
                    "token": body["token"],
                    "subject_digest": body["subject_digest"],
                    "decision": "accept_read_scope",
                },
            )
        assert receipt.status_code == 200, receipt.text
        listed = await http.get(
            f"/api/workbench/sessions/{sid}/capability-calls",
            params={"call_id": pending["id"]},
        )
        assert listed.status_code == 200, listed.text
        calls = listed.json()["calls"]
        assert len(calls) == 1 and calls[0]["detail"]["approval"]["granted"]
        missing = await http.get(
            f"/api/workbench/sessions/{sid}/capability-calls", params={"call_id": sid}
        )
        assert missing.json() == {"calls": []}
        revoked = await http.post(path + "/revoke-read", json={})
        assert (
            revoked.status_code == 200
            and revoked.json()["detail"]["approval"]["revoked"]
        )
        assert "read_offer_key" not in revoked.text


@pytest.mark.parametrize("reason", ["unreviewed", "missing-purpose"])
async def test_read_hint_or_empty_purpose_cannot_create_scope(db, reason):
    remote = Records()
    reviewed = (
        READ_POLICY
        if reason == "missing-purpose"
        else policy(
            READ, operation="read", effect="read", resources=READ_POLICY.resources
        )
    )
    sid, _, key, token = await setup(
        db,
        remote,
        READ,
        policies=(reviewed,),
        purpose="" if reason == "missing-purpose" else "Review",
    )
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote, operations=(reviewed,))
        first = await prompt(svc, sid, token, key, {"id": "a"}, "no-scope")
        assert not first["detail"].get("read_offer_key")
        with pytest.raises(CapabilityError):
            await svc.decide(
                OWNER,
                first["id"],
                approval(first).model_copy(update={"remember_read": True}),
            )
        assert not remote.calls
