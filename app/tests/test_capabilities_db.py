"""Real PostgreSQL: connection ownership, frozen selection, approval and crash receipts."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from test_workbench_ledger_db import OTHER, OWNER, seed
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.application.workbench.capabilities import Capabilities
from app.domain.workbench.capabilities import (
    ActionDecision,
    AssetAction,
    CapabilityError,
    ConnectionCreate,
    SelectionPut,
    SkillCreate,
    tool_key,
)
from app.domain.workbench.errors import (
    Forbidden,
    NotFound,
    StaleStateVersion,
)
from app.domain.workbench.tool_operations import McpOperation, ResourceBinding
from app.domain.workbench.tool_registry import ToolRegistration
from app.infrastructure.workbench.repository import WorkbenchRepository

URL = "https://tools.example.test/mcp"
SCHEMA = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
    "additionalProperties": False,
}
TOOLS = [{"name": "send", "description": "Send a notice", "inputSchema": SCHEMA}]
CIPHER = Fernet(Fernet.generate_key())
OPERATION = McpOperation(
    service_url=URL,
    tool_name="send",
    descriptor_digest=ToolRegistration.discover("mcp", URL, TOOLS[0]).descriptor_digest,
    capability_ref="synthetic-mail",
    version="1",
    operation="send",
    effect="send",
    resources=(ResourceBinding(resource_role="recipient", argument_path=("to",)),),
)


class Remote:
    allowed_urls = (URL,)

    def __init__(self):
        self.calls = []
        self.tools = deepcopy(TOOLS)
        self.fail = False

    async def discover(self, url, token):
        return deepcopy(self.tools)

    async def call(self, url, token, name, arguments):
        self.calls.append((url, token, name, arguments))
        if self.fail:
            raise TimeoutError()
        return {"content": [{"type": "text", "text": "sent"}], "isError": False}


def service(repo, remote=None, *, operations=(OPERATION,)):
    return Capabilities(
        repo,
        remote or Remote(),
        CIPHER,
        runtime_url="http://api.test/api/mcp/capabilities",
        operations=operations,
    )


def body(key="create-connection-1", **changes):
    return ConnectionCreate(
        request_id=key,
        name="Office mail",
        account_label="office@example.test",
        url=URL,
        token="only-server-knows",
        **changes,
    )


async def prepared(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        remote = Remote()
        svc = service(repo, remote)
        a = await svc.create_connection(OWNER, body())
        key = tool_key(a["id"], "send")
        selection = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selection["revision"],
                tools=[key],
                purpose="Notify one recipient",
            ),
        )
        runtime = await svc.prepare(OWNER, sid)
        assert runtime["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is True
        token = runtime["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
    return sid, a, key, token, remote


def approval(call, decision="accept"):
    return ActionDecision(
        decision=decision,
        token=call["detail"]["approval_token"],
        subject_digest=call["digest"],
    )


async def test_connection_ownership_encryption_idempotence_and_defaults(db):
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo)
        a = await svc.create_connection(OWNER, body())
        assert "secret" not in a and "only-server-knows" not in str(a)
        assert a["id"] == (await svc.create_connection(OWNER, body()))["id"]
        raw = await repo.capability_asset(OWNER, a["id"])
        assert (
            raw["secret"] != "only-server-knows"
            and svc.secret(raw) == "only-server-knows"
        )
        with pytest.raises(NotFound):
            await repo.capability_asset(OTHER, a["id"])
        assert await svc.assets(OTHER) == []
        await svc.action(OWNER, a["id"], AssetAction(revision=1, action="default_on"))
    _, _, sid = await seed(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        selected = await svc.selection(OWNER, sid)
        assert (
            selected["tools"] == [tool_key(a["id"], "send")]
            and selected["issues"] == []
        )
        changed = await svc.action(
            OWNER, a["id"], AssetAction(revision=1, action="default_off")
        )
        assert changed["revision"] == 1
        assert (await svc.selection(OWNER, sid))["tools"] == selected["tools"]
        assert not (await svc.selection(OWNER, sid))["issues"]
    _, _, later = await seed(db)
    async with db() as s:
        assert (await service(WorkbenchRepository(s)).selection(OWNER, later))[
            "tools"
        ] == []


async def test_selection_conflicts_dependencies_disabled_revision_and_method_boundary(
    db,
):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote)
        skill = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="skill-request-1",
                name="notice",
                description="Notice",
                version="1",
                content="SKILL_SENTINEL",
                dependencies=[key],
            ),
        )
        selection = await svc.selection(OWNER, sid)
        with pytest.raises(CapabilityError):
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(revision=selection["revision"], skills=[skill["id"]]),
            )
        selection = await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selection["revision"], tools=[key], skills=[skill["id"]]
            ),
        )
        result = await svc.prepare(OWNER, sid)
        assert "SKILL_SENTINEL" not in str(
            result["inputs"]
        ) and "only-server-knows" not in str(result)
        assert "Notice" in str(result["inputs"])
        with pytest.raises(StaleStateVersion):
            await svc.save_selection(
                OWNER, sid, SelectionPut(revision=selection["revision"] - 1)
            )
        frozen = await svc.prepare(OWNER, sid, scene="method")
        assert (
            frozen["inputs"] == []
            and frozen["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is False
        )
        await svc.action(OWNER, skill["id"], AssetAction(revision=1, action="disable"))
        degraded = await svc.prepare(OWNER, sid)
        assert skill["id"] not in str(degraded["config"])
        assert "excluded from this turn" in str(degraded["inputs"])
        with pytest.raises(Forbidden):
            await svc.runtime(sid, token)


async def test_approval_exact_once_replay_and_owner(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        requested = await request_call(
            svc, sid, token, key, {"to": "reader@example.test"}
        )
        assert not remote.calls
        same = await request_call(svc, sid, token, key, {"to": "reader@example.test"})
        assert same["call_id"] == requested["call_id"]
        call = await repo.capability_call(OWNER, requested["call_id"])
        assert call["detail"]["account_label"] == "office@example.test"
        with pytest.raises(NotFound):
            await svc.decide(OTHER, call["id"], approval(call))
        with pytest.raises(Forbidden):
            await svc.decide(
                OWNER,
                call["id"],
                approval(call).model_copy(update={"subject_digest": "0" * 64}),
            )
        done = await svc.decide(OWNER, call["id"], approval(call))
        assert done["status"] == "succeeded" and len(remote.calls) == 1
        assert remote.calls[0][1] == "only-server-knows"
        await svc.decide(OWNER, call["id"], approval(call))
        assert len(remote.calls) == 1


async def test_revocation_after_prompt_rejects_call_and_does_not_resend(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        request = await request_call(svc, sid, token, key, {"to": "r@example.test"})
        call = await repo.capability_call(OWNER, request["call_id"])
        await svc.action(OWNER, a["id"], AssetAction(revision=1, action="disconnect"))
        with pytest.raises(CapabilityError):
            await svc.decide(OWNER, call["id"], approval(call))
        with pytest.raises(CapabilityError):
            await request_call(svc, sid, token, key, {"to": "other@example.test"})
        assert (
            not remote.calls
            and (await repo.capability_asset(OWNER, a["id"]))["secret"] is None
        )
        assert (await svc.decide(OWNER, call["id"], approval(call, "decline")))[
            "status"
        ] == "declined"


async def test_timeout_and_crash_are_unknown_never_retried(db):
    sid, a, key, token, remote = await prepared(db)
    remote.fail = True
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        req = await request_call(svc, sid, token, key, {"to": "r@example.test"})
        call = await repo.capability_call(OWNER, req["call_id"])
        assert (await svc.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "unknown"
        await svc.decide(OWNER, call["id"], approval(call))
        assert len(remote.calls) == 1
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_calls SET status='dispatching', updated_at=now()-interval '3 minutes' WHERE id=:id"
                ),
                {"id": call["id"]},
            )
        recovered = await svc.calls(OWNER, sid)
        assert recovered[0]["status"] == "unknown" and len(remote.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "status", "dispatches"),
    [
        ("handshake_timeout", "failed", 0),
        ("connect_timeout", "failed", 1),
        ("read_timeout", "unknown", 1),
        ("connection_lost", "unknown", 1),
        ("write_error", "unknown", 1),
        ("http_error", "unknown", 1),
        ("oversize", "unknown", 1),
        ("malformed", "unknown", 1),
        ("wrong_id", "unknown", 1),
        ("protocol_error", "failed", 1),
        ("tool_error", "failed", 1),
        ("success", "succeeded", 1),
    ],
)
async def test_real_mcp_adapter_outcomes_are_recorded_without_retry(
    db, monkeypatch, outcome, status, dispatches
):
    from capability_transport_support import transport_remote

    sid, _, key, token, _ = await prepared(db)
    remote, calls = transport_remote(monkeypatch, URL, outcome)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        requested = await request_call(
            svc, sid, token, key, {"to": "reader@example.test"}
        )
        call = await repo.capability_call(OWNER, requested["call_id"])
        result = await svc.decide(OWNER, call["id"], approval(call))
        assert result["status"] == status
        assert (await repo.capability_call(OWNER, call["id"]))["status"] == status
        assert (await svc.decide(OWNER, call["id"], approval(call)))["status"] == status
        assert len(calls) == dispatches


@pytest.mark.parametrize("change", ["refresh", "renew", "disable_enable"])
async def test_changed_connection_does_not_block_chat_or_reuse_old_approval(db, change):
    from app.domain.workbench.capabilities import ConnectionRenew

    sid, asset, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        requested = await request_call(
            svc, sid, token, key, {"to": "reader@example.test"}
        )
        call = await repo.capability_call(OWNER, requested["call_id"])
        if change == "renew":
            await svc.renew(
                OWNER, asset["id"], ConnectionRenew(revision=1, token="replacement")
            )
        elif change == "disable_enable":
            await svc.action(
                OWNER, asset["id"], AssetAction(revision=1, action="disable")
            )
            await svc.action(
                OWNER, asset["id"], AssetAction(revision=2, action="enable")
            )
        else:
            await svc.action(
                OWNER, asset["id"], AssetAction(revision=1, action="refresh")
            )
        with pytest.raises(CapabilityError):
            await svc.decide(OWNER, call["id"], approval(call))
        result = await svc.prepare(OWNER, sid)
        assert result["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is False
        assert "excluded from this turn" in str(result["inputs"])
        selected = await svc.selection(OWNER, sid)
        assert selected["tools"] == [key]  # User's intent remains visible for review.
        assert any(issue["code"] == "revision_changed" for issue in selected["issues"])
        assert not remote.calls


async def test_dedupe_is_not_limited_to_latest_100_and_expiry(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        first = await request_call(svc, sid, token, key, {"to": "first@example.test"})
        for i in range(101):
            await request_call(svc, sid, token, key, {"to": f"{i}@example.test"})
        assert (await request_call(svc, sid, token, key, {"to": "first@example.test"}))[
            "call_id"
        ] == first["call_id"]
        assert (
            await s.execute(
                text("SELECT count(*) FROM workbench_interactions WHERE session_id=:s"),
                {"s": sid},
            )
        ).scalar() == 102
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_interactions SET expires_at=now()-interval '1 minute' WHERE session_id=:s"
                ),
                {"s": sid},
            )
        assert all(c["status"] == "expired" for c in await svc.calls(OWNER, sid))
        assert not remote.calls


async def test_parallel_approval_dispatches_only_once_and_session_cleanup(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote)
        req = await request_call(svc, sid, token, key, {"to": "r@example.test"})
        call = await svc.repo.capability_call(OWNER, req["call_id"])

    async def decide():
        async with db() as s:
            return await service(WorkbenchRepository(s), remote).decide(
                OWNER, call["id"], approval(call)
            )

    await asyncio.gather(decide(), decide())
    assert len(remote.calls) == 1
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.delete_session_tree(sid)
        assert not await repo.capability_runtime_by_session(sid)


async def test_prepare_rechecks_asset_after_preflight_and_blocks_schema_refresh(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        original = svc.selection

        async def race(owner, session_id):
            view = await original(owner, session_id)
            await svc.action(owner, a["id"], AssetAction(revision=1, action="disable"))
            return view

        svc.selection = race
        result = await svc.prepare(OWNER, sid)
        assert result["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is False
        assert (await repo.capability_runtime_by_session(sid))["runtime"][
            "status"
        ] == "prepared"
        # The old token still cannot stage a call to a now-disabled connection.
        with pytest.raises(Forbidden):
            await request_call(svc, sid, token, key, {"to": "reader@example.test"})


async def test_renew_requires_reconfirmation_and_completed_scope_cannot_stage_calls(db):
    from app.domain.workbench.capabilities import ConnectionRenew

    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        renewed = await svc.renew(
            OWNER, a["id"], ConnectionRenew(revision=1, token="replacement-only-server")
        )
        assert renewed["revision"] == 2 and "replacement-only-server" not in str(
            renewed
        )
        assert (await svc.selection(OWNER, sid))["issues"][0][
            "code"
        ] == "revision_changed"
        with pytest.raises(CapabilityError):
            await request_call(svc, sid, token, key, {"to": "reader@example.test"})
        async with repo.transaction():
            selected = await repo.capability_runtime_by_session(sid)
            selected["runtime"]["status"] = "completed"
            await repo.capability_runtime(OWNER, sid, selected["runtime"])
        with pytest.raises(Forbidden):
            await svc.runtime(sid, token)


async def test_late_turn_events_cannot_overwrite_new_scope(db):
    sid, _, _, _, _ = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        old = (await repo.capability_runtime_by_session(sid))["runtime"]
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.capability_runtime_event(
                sid, old["scope_id"], "turn-old", started=True
            )
    async with db() as s:
        repo = WorkbenchRepository(s)
        await service(repo).prepare(OWNER, sid)
        current = (await repo.capability_runtime_by_session(sid))["runtime"]
        assert current["scope_id"] != old["scope_id"]
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.capability_runtime_event(
                sid, old["scope_id"], "turn-old", started=False
            )
            await repo.capability_runtime_event(
                sid, old["scope_id"], "turn-old", started=True
            )
        assert (await repo.capability_runtime_by_session(sid))["runtime"] == current


@pytest.mark.parametrize("event", ["completed", "takeover", "reconnected"])
@pytest.mark.parametrize("already_completed", [False, True])
async def test_new_runner_closes_persisted_scope_without_in_memory_scope(
    db, event, already_completed
):
    from app.application.workbench.runner import SandboxLink
    from app.bootstrap.workbench import build_runner
    from app.infrastructure.workbench.publisher import RedisPublisher

    sid, _, key, _, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        skill = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="restart-skill",
                name="restart",
                description="Restart fixture",
                version="1",
                content="BODY_MUST_NOT_BE_READ_AFTER_COMPLETION",
            ),
        )
        selected = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selected["revision"],
                tools=[key],
                skills=[skill["id"]],
            ),
        )
        prepared_scope = await svc.prepare(OWNER, sid)
        token = prepared_scope["config"]["mcp_servers.sunmoon_capabilities"][
            "http_headers"
        ]["Authorization"].split()[1]
        async with repo.transaction():
            await repo.set_session_thread(sid, "restart-thread")
            await repo.capability_runtime_event(
                sid, prepared_scope["scope_id"], "old-turn", started=True
            )
        request = await request_call(
            svc, sid, token, key, {"to": "reader@example.test"}
        )
        call = await repo.capability_call(OWNER, request["call_id"])
        sandbox_id = str((await repo.get_session(sid))["sandbox_id"])
        assert await svc.skill_resource(
            sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
        )
        if already_completed:
            async with repo.transaction():
                await repo.capability_runtime_event(
                    sid, prepared_scope["scope_id"], "old-turn", started=False
                )

    runner = build_runner(
        db, publisher=RedisPublisher(None, "test"), runner_id="restarted-runner"
    )
    link = SandboxLink(runner, {"id": sandbox_id})
    assert link.capability_scopes == {}
    if event == "completed":
        await link.on_notification(
            "turn/completed",
            {
                "threadId": "restart-thread",
                "turn": {"id": "old-turn", "status": "completed"},
            },
        )
    elif event == "takeover":
        async with runner.stores() as repo, repo.transaction():
            await runner._take_over(repo, sandbox_id)
    else:
        await runner.expire_approvals(sandbox_id, "connection re-established")
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        assert (await repo.capability_runtime_by_session(sid))["runtime"][
            "status"
        ] == "completed"
        with pytest.raises(Forbidden):
            await svc.skill_resource(
                sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
            )
        with pytest.raises(Forbidden):
            await request_call(svc, sid, token, key, {"to": "another@example.test"})
        if event != "completed":
            with pytest.raises(CapabilityError):
                await svc.decide(OWNER, call["id"], approval(call))
            assert not remote.calls
        else:
            # Normal completion permits the user to approve the already-staged
            # exact operation until its original deadline, without a reconnect.
            assert (await svc.decide(OWNER, call["id"], approval(call)))[
                "status"
            ] == "succeeded"
            assert len(remote.calls) == 1


async def test_migration_down_up_preserves_existing_sessions(db):
    import importlib.util
    from pathlib import Path

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    _, _, sid = await seed(db)
    path = (
        Path(__file__).resolve().parents[1]  # noqa: ASYNC240 - test-only migration module
        / "alembic/versions/20261114_0041_calls.py"
    )
    spec = importlib.util.spec_from_file_location("capability_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    authority_path = path.with_name("20261114_0042_authorizations.py")
    authority_spec = importlib.util.spec_from_file_location(
        "authority_migration", authority_path
    )
    authority_module = importlib.util.module_from_spec(authority_spec)
    authority_spec.loader.exec_module(authority_module)
    async with db() as s:
        conn = await s.connection()

        def cycle(sync):
            with Operations.context(MigrationContext.configure(sync)):
                authority_module.downgrade()
                module.downgrade()
                assert sync.execute(
                    text("SELECT id FROM workbench_sessions WHERE id=:s"), {"s": sid}
                ).scalar()
                module.upgrade()
                authority_module.upgrade()

        await conn.run_sync(cycle)
        await s.commit()
        selected = await service(WorkbenchRepository(s)).selection(OWNER, sid)
        assert selected["tools"] == [] and selected["skills"] == []


async def test_method_panel_projects_frozen_tools_without_chat_authority(db):
    from test_workbench_ledger_db import handover

    from app.application.workbench.ledger import Ledger

    sid, _, key, _, _ = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER)
        projection = await service(repo).selection(OWNER, sid)
        assert not projection["editable"] and projection["method"] is not None
        assert projection["tools"] == [] and projection["skills"] == []
        assert projection["method"]["managed_bindings_supported"] is True
        assert projection["method"]["requirements"] == {"tools": [], "skills": []}
        assert projection["preparation"] == "method_managed"
        assert key not in projection["method"]["declared_tools"]
