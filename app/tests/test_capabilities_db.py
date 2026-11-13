"""Real PostgreSQL: connection ownership, frozen selection, approval and crash receipts."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from test_workbench_ledger_db import OTHER, OWNER, seed
from test_workbench_ledger_db import db as db

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


def service(repo, remote=None):
    return Capabilities(
        repo,
        remote or Remote(),
        CIPHER,
        runtime_url="http://api.test/api/mcp/capabilities",
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
        frozen = await svc.prepare(OWNER, sid, expert=True)
        assert (
            frozen["inputs"] == []
            and frozen["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is False
        )
        await svc.action(OWNER, skill["id"], AssetAction(revision=1, action="disable"))
        with pytest.raises(CapabilityError):
            await svc.prepare(OWNER, sid)
        with pytest.raises(Forbidden):
            await svc.runtime(sid, token)


async def test_approval_exact_once_replay_and_owner(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        requested = await svc.request_call(
            sid, token, key, {"to": "reader@example.test"}
        )
        assert not remote.calls
        same = await svc.request_call(sid, token, key, {"to": "reader@example.test"})
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
        request = await svc.request_call(sid, token, key, {"to": "r@example.test"})
        call = await repo.capability_call(OWNER, request["call_id"])
        await svc.action(OWNER, a["id"], AssetAction(revision=1, action="disconnect"))
        with pytest.raises(CapabilityError):
            await svc.decide(OWNER, call["id"], approval(call))
        with pytest.raises(CapabilityError):
            await svc.request_call(sid, token, key, {"to": "other@example.test"})
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
        req = await svc.request_call(sid, token, key, {"to": "r@example.test"})
        call = await repo.capability_call(OWNER, req["call_id"])
        assert (await svc.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "unknown"
        await svc.decide(OWNER, call["id"], approval(call))
        assert len(remote.calls) == 1
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_capability_calls SET status='dispatching', updated_at=now()-interval '3 minutes' WHERE id=:id"
                ),
                {"id": call["id"]},
            )
        recovered = await svc.calls(OWNER, sid)
        assert recovered[0]["status"] == "unknown" and len(remote.calls) == 1


async def test_dedupe_is_not_limited_to_latest_100_and_expiry(db):
    sid, a, key, token, remote = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        first = await svc.request_call(sid, token, key, {"to": "first@example.test"})
        for i in range(101):
            await svc.request_call(sid, token, key, {"to": f"{i}@example.test"})
        assert (await svc.request_call(sid, token, key, {"to": "first@example.test"}))[
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
        req = await svc.request_call(sid, token, key, {"to": "r@example.test"})
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
        with pytest.raises(CapabilityError):
            await svc.prepare(OWNER, sid)
        assert (await repo.capability_runtime_by_session(sid))["runtime"][
            "status"
        ] == "prepared"
        # The old token still cannot stage a call to a now-disabled connection.
        with pytest.raises(CapabilityError):
            await svc.request_call(sid, token, key, {"to": "reader@example.test"})


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
            await svc.request_call(sid, token, key, {"to": "reader@example.test"})
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


async def test_migration_down_up_preserves_existing_sessions(db):
    import importlib.util
    from pathlib import Path

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    _, _, sid = await seed(db)
    path = (
        Path(__file__).resolve().parents[1]  # noqa: ASYNC240 - test-only migration module
        / "alembic/versions/20261113_0040_capabilities.py"
    )
    spec = importlib.util.spec_from_file_location("capability_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with db() as s:
        conn = await s.connection()

        def cycle(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()
                assert sync.execute(
                    text("SELECT id FROM workbench_sessions WHERE id=:s"), {"s": sid}
                ).scalar()
                module.upgrade()

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
        assert projection["method"]["managed_bindings_supported"] is False
        assert projection["preparation"] == "method_managed"
        assert key not in projection["method"]["declared_tools"]
