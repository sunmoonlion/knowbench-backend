"""Shared call identities, real method context and one-time historical migration."""

import importlib.util
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_capabilities_db import approval, prepared, service
from test_method_authorization_db import setup as setup_authorized_method
from test_method_capabilities_db import setup_method
from test_skill_execution_db import setup as setup_script
from test_workbench_ledger_db import OTHER, OWNER, seed
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.application.workbench.calls import CallLedger
from app.application.workbench.tool_invocation import ToolInvocation
from app.domain.workbench.errors import NotFound, RecordsRefused
from app.domain.workbench.native_functions import (
    find_expert_revision,
    native_registration,
)
from app.infrastructure.workbench.repository import WorkbenchRepository

FIND_EXPERT_FUNCTION = find_expert_revision()
FIND_EXPERT_REGISTRATION = native_registration(FIND_EXPERT_FUNCTION)


async def test_method_parent_is_stable_across_calls_and_tracks_task_state(db):
    sid, task_id, attempt_id, _, _, _, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        first = await request_call(
            svc, sid, token, key, {"to": "test@example.test"}, call_id="first"
        )
        stored = await repo.capability_call(OWNER, first["call_id"])
        root = await repo.call_by_key(OWNER, sid, "task:" + task_id)
        assert root and root["kind"] == "method" and root["status"] == "waiting"
        assert stored["parent_call_id"] == root["id"]
        assert stored["task_id"] == task_id and stored["attempt_id"] == attempt_id
        assert stored["registration_id"] and stored["registration_digest"]
        assert stored["source_asset_id"] == stored["connection_id"]
        await svc.decide(OWNER, stored["id"], approval(stored))
        assert (await repo.call_by_key(OWNER, sid, "task:" + task_id))[
            "status"
        ] == "running"
        second = await request_call(
            svc, sid, token, key, {"to": "test@example.test"}, call_id="second"
        )
        assert (await repo.capability_call(OWNER, second["call_id"]))[
            "parent_call_id"
        ] == root["id"]
        # Managed approval endpoints cannot reinterpret a method/platform row.
        with pytest.raises(NotFound):
            await repo.capability_call(OWNER, root["id"])
        with pytest.raises(NotFound):
            await repo.capability_call(OTHER, first["call_id"])
        assert len(await svc.calls(OWNER, sid)) == 2
        assert all("parent_call_id" not in c for c in await svc.calls(OWNER, sid))
        assert (
            await s.scalar(
                text("SELECT count(*) FROM workbench_calls WHERE kind='method'")
            )
        ) == 1


async def test_shared_idempotency_has_no_replay_and_preserves_transport_independent_reply(
    db,
):
    _, _, sid = await seed(db)
    invocation = ToolInvocation(sid, "thread", "turn", "call", "sunmoon_workbench")
    async with db() as s:
        repo = WorkbenchRepository(s)
        ledger = CallLedger(repo, OWNER)
        first, fresh = await ledger.begin(
            invocation,
            FIND_EXPERT_REGISTRATION,
            {"task": "task"},
            function=FIND_EXPERT_FUNCTION,
        )
        assert (
            fresh
            and first["parent_call_id"] is None
            and first["source_asset_id"] is None
        )
        same, fresh = await ledger.begin(
            invocation,
            FIND_EXPERT_REGISTRATION,
            {"task": "task"},
            function=FIND_EXPERT_FUNCTION,
        )
        assert (
            not fresh and same["id"] == first["id"] and same["status"] == "dispatching"
        )
        with pytest.raises(RecordsRefused, match="different operation"):
            await ledger.begin(
                invocation,
                FIND_EXPERT_REGISTRATION,
                {"task": "changed"},
                function=FIND_EXPERT_FUNCTION,
            )
        await ledger.finish(
            first,
            {"jsonrpc": "2.0", "id": 7, "result": {"isError": False}},
            "succeeded",
        )
        same, fresh = await ledger.begin(
            invocation,
            FIND_EXPERT_REGISTRATION,
            {"task": "task"},
            function=FIND_EXPERT_FUNCTION,
        )
        assert not fresh and same["detail"]["response"] == {
            "result": {"isError": False}
        }
        assert not await repo.capability_calls(OWNER, sid)
        with pytest.raises(NotFound):
            await repo.capability_call(OWNER, first["id"])


def migration(filename="20261114_0041_calls.py"):
    path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location("call_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_populated_migration_preserves_evidence_and_revokes_old_execution(db):
    sid, asset, key, token, remote = await prepared(db)
    script_sid, executor, script_asset, script_key, script_token = await setup_script(
        db
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        external = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        svc.script_executor = executor
        script = await request_call(
            svc,
            script_sid,
            script_token,
            script_key,
            {"path": "scripts/calculate.py", "args": [], "stdin": ""},
        )
        await s.commit()
        before = {
            r["id"]: r
            for r in await repo.capability_calls(OWNER, sid)
            + await repo.capability_calls(OWNER, script_sid)
        }
        conn = await s.connection()
        module = migration()

        def migrate_existing(sync):
            with Operations.context(MigrationContext.configure(sync)):
                versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
                later = [
                    migration(path.name)
                    for path in sorted(versions.glob("20*.py"))
                    if path.name > "20261114_0041_calls.py"
                ]
                for step in reversed(later):
                    step.downgrade()
                module.downgrade()
                # Reproduce the former overloaded storage and active states.
                sync.execute(
                    text(
                        "UPDATE workbench_capability_calls SET status='dispatching' WHERE id=:i"
                    ),
                    {"i": script["call_id"]},
                )
                sync.execute(
                    text(
                        "UPDATE workbench_capability_calls SET status='awaiting_approval' WHERE id=:i"
                    ),
                    {"i": external["call_id"]},
                )
                module.upgrade()
                for step in later:
                    step.upgrade()
                assert (
                    sync.execute(
                        text("SELECT to_regclass('workbench_capability_calls')")
                    ).scalar()
                    is None
                )

        await conn.run_sync(migrate_existing)
        await s.commit()
        for call_id, original in before.items():
            after = await repo.capability_call(OWNER, call_id)
            assert (
                after["detail"] == original["detail"]
                and after["digest"] == original["digest"]
            )
            assert after["call_key"] == "historical:" + call_id
        external_after = await repo.capability_call(OWNER, external["call_id"])
        script_after = await repo.capability_call(OWNER, script["call_id"])
        assert (
            external_after["status"] == "expired"
            and external_after["connection_id"] == asset["id"]
        )
        assert (
            script_after["status"] == "unknown"
            and script_after["connection_id"] is None
        )
        assert script_after["source_asset_id"] == script_asset["id"]
        assert (await repo.capability_selection(OWNER, sid))["runtime"][
            "aborted"
        ] is True
        assert (await svc.decide(OWNER, external["call_id"], approval(external_after)))[
            "status"
        ] == "expired"
        assert not remote.calls and not executor.started


async def test_method_identity_survives_new_attempt(db):
    from app.application.workbench.ledger import Ledger
    from app.domain.workbench.states import AttemptState

    sid, task_id, attempt_id, _, _, _, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        stored = await repo.capability_call(OWNER, staged["call_id"])
        await svc.decide(OWNER, stored["id"], approval(stored, decision="decline"))
        led = Ledger(repo)
        attempt = await repo.get_attempt(attempt_id)
        await led.attempt_transition(attempt_id, AttemptState.FAILED)
        next_attempt = await led.open_attempt(
            task_id,
            step_id=attempt["step_id"],
            step_version=attempt["step_version"],
            role=attempt["role"],
        )
        async with repo.transaction():
            await repo.update_attempt(
                next_attempt["attempt_id"], refs={"runtime_turn_id": "retry-turn"}
            )
        call, fresh = await CallLedger(repo, OWNER).begin(
            ToolInvocation(
                sid, "thread", "retry-turn", "retry-tool", "sunmoon_workbench"
            ),
            FIND_EXPERT_REGISTRATION,
            {"task": task_id},
            function=FIND_EXPERT_FUNCTION,
            task_id=task_id,
        )
        assert fresh and call["parent_call_id"] == stored["parent_call_id"]
        assert call["attempt_id"] == next_attempt["attempt_id"] != stored["attempt_id"]
        assert (
            await s.scalar(
                text("SELECT count(*) FROM workbench_calls WHERE kind='method'")
            )
            == 1
        )


async def test_scope_change_after_authorization_cannot_create_a_false_parent(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        ledger = CallLedger(repo, OWNER)
        with pytest.raises(RecordsRefused, match="context changed"):
            await ledger.begin(
                ToolInvocation(sid, "thread", "turn", "call", "sunmoon_workbench"),
                FIND_EXPERT_REGISTRATION,
                {"task": "task"},
                function=FIND_EXPERT_FUNCTION,
                task_id="other-task",
            )
        assert await s.scalar(text("SELECT count(*) FROM workbench_calls")) == 0


async def test_unsettled_platform_call_becomes_unknown_and_cannot_be_dispatched_again(
    db,
):
    _, _, sid = await seed(db)
    invocation = ToolInvocation(sid, "thread", "turn", "call", "sunmoon_workbench")
    async with db() as s:
        repo = WorkbenchRepository(s)
        ledger = CallLedger(repo, OWNER)
        call, fresh = await ledger.begin(
            invocation,
            FIND_EXPERT_REGISTRATION,
            {"task": "task"},
            function=FIND_EXPERT_FUNCTION,
        )
        assert fresh
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_calls SET updated_at=now()-interval '3 minutes' WHERE id=:id"
                ),
                {"id": call["id"]},
            )
        same, fresh = await ledger.begin(
            invocation,
            FIND_EXPERT_REGISTRATION,
            {"task": "task"},
            function=FIND_EXPERT_FUNCTION,
        )
        assert not fresh and same["id"] == call["id"] and same["status"] == "unknown"
        assert "response" not in same["detail"]


@pytest.mark.parametrize("origin", ["platform", "managed_step"])
async def test_session_cleanup_removes_call_tree_and_authority_only_for_that_session(
    db, origin
):
    # A separate session with live authority must survive the expiration cleanup.
    _, other_sid, _, _, _, _ = await setup_authorized_method(db)
    if origin == "managed_step":
        tid, sid, key, token, remote, _ = await setup_authorized_method(db)
    else:
        sid, tid, _, _, _, _, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        if origin == "managed_step":
            await request_call(
                service(repo, remote), sid, token, key, {"to": "test@example.test"}
            )
        else:
            await CallLedger(repo, OWNER).begin(
                ToolInvocation(sid, "thread", "turn", "call", "sunmoon_workbench"),
                FIND_EXPERT_REGISTRATION,
                {"task": tid},
                function=FIND_EXPERT_FUNCTION,
                task_id=tid,
            )
        for table in ("workbench_calls", "workbench_method_authorizations"):
            assert await s.scalar(
                text(f"SELECT count(*) FROM {table} WHERE session_id=:s"), {"s": sid}
            )
        other_calls = (
            await s.execute(
                text("SELECT * FROM workbench_calls WHERE session_id=:s ORDER BY id"),
                {"s": other_sid},
            )
        ).all()
        other_authority = (
            await s.execute(
                text(
                    "SELECT * FROM workbench_method_authorizations WHERE session_id=:s ORDER BY call_id"
                ),
                {"s": other_sid},
            )
        ).all()
        async with repo.transaction():
            await repo.delete_session_tree(sid)
        for table in (
            "workbench_method_authorizations",
            "workbench_calls",
            "workbench_tasks",
        ):
            assert not await s.scalar(
                text(f"SELECT count(*) FROM {table} WHERE session_id=:s"), {"s": sid}
            )
        assert not await s.scalar(
            text("SELECT count(*) FROM workbench_attempts WHERE task_id=:t"), {"t": tid}
        )
        assert not await s.scalar(
            text("SELECT count(*) FROM workbench_sessions WHERE id=:s"), {"s": sid}
        )
        assert (
            await s.execute(
                text("SELECT * FROM workbench_calls WHERE session_id=:s ORDER BY id"),
                {"s": other_sid},
            )
        ).all() == other_calls
        assert (
            await s.execute(
                text(
                    "SELECT * FROM workbench_method_authorizations WHERE session_id=:s ORDER BY call_id"
                ),
                {"s": other_sid},
            )
        ).all() == other_authority
        assert await repo.get_session(other_sid)


async def test_call_migration_refuses_lossy_downgrade_and_preserves_history(db):
    sid, tid, _, _, _, _, _, _, _ = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        await CallLedger(repo, OWNER).begin(
            ToolInvocation(sid, "thread", "turn", "call", "sunmoon_workbench"),
            FIND_EXPERT_REGISTRATION,
            {"task": tid},
            function=FIND_EXPERT_FUNCTION,
            task_id=tid,
        )
        before = (
            await s.execute(text("SELECT * FROM workbench_calls ORDER BY id"))
        ).all()
        assert {row.origin for row in before} >= {"platform", "method"}

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration().downgrade()

        with pytest.raises(
            DBAPIError, match="shared call history requires forward repair"
        ):
            async with s.begin_nested():
                await (await s.connection()).run_sync(downgrade)
        assert (
            await s.execute(text("SELECT * FROM workbench_calls ORDER BY id"))
        ).all() == before
        assert (
            await s.scalar(text("SELECT to_regclass('workbench_capability_calls')"))
            is None
        )


async def test_skill_file_migration_reconciles_old_rows_and_aborts_on_corruption(db):
    import base64
    import hashlib
    import json

    module = migration("20261119_0046_skill_files.py")
    body = "中文 instructions\n"
    binary = bytes(range(256))
    data = {"SKILL.md": body.encode(), "assets/raw.bin": binary}
    files = [
        {
            "path": path,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "application/octet-stream"
            if path.endswith(".bin")
            else "text/plain",
        }
        for path, raw in data.items()
    ]
    old = {"content": body, "description": "legacy", "version": "1", "dependencies": []}
    plain = {**old, "format": "instruction-markdown", "digest": "plain-original"}
    package = {
        **old,
        "format": "skill-package-zip",
        "digest": "archive-original",
        "files": files,
        "resources": {k: base64.b64encode(v).decode() for k, v in data.items()},
    }
    async with db() as session:
        conn = await session.connection()

        def cycle(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()
                for num, detail in enumerate([plain, package]):
                    sync.execute(
                        text("""INSERT INTO workbench_capability_assets
                        (id,owner_actor_id,kind,name,status,revision,detail,request_id,request_digest)
                        VALUES (gen_random_uuid(),:owner,'skill',:name,'disabled',3,
                                cast(:detail as jsonb),:name,:name)"""),
                        {
                            "owner": OWNER,
                            "name": str(num),
                            "detail": json.dumps(detail),
                        },
                    )
                get_assets = text(
                    "SELECT * FROM workbench_capability_assets ORDER BY name"
                )
                before = sync.execute(get_assets).mappings().all()
                module.upgrade()
                after = sync.execute(get_assets).mappings().all()
                for a, b in zip(before, after, strict=True):
                    assert {**b, "detail": a["detail"]} == a
                    assert b["detail"]["digest"] == a["detail"]["digest"]
                    assert {"content", "resources"}.isdisjoint(b["detail"])
                saved = sync.execute(
                    text("""SELECT a.name, f.path, f.data FROM workbench_skill_files f
                            JOIN workbench_capability_assets a ON a.id=f.asset_id""")
                ).all()
                assert {(name, path): bytes(buf) for name, path, buf in saved} == {
                    ("0", "SKILL.md"): body.encode(),
                    **{("1", k): v for k, v in data.items()},
                }
                with pytest.raises(DBAPIError, match="immutable"), sync.begin_nested():
                    sync.execute(
                        text(
                            "UPDATE workbench_skill_files SET data='' WHERE path='SKILL.md'"
                        )
                    )
                module.downgrade()
                assert sync.execute(get_assets).mappings().all() == before
                sync.execute(
                    text("""UPDATE workbench_capability_assets
                    SET detail=jsonb_set(detail,'{files,1,sha256}','"bad"') WHERE name='1'""")
                )
                with (
                    pytest.raises(DBAPIError, match="reconciliation failed"),
                    sync.begin_nested(),
                ):
                    module.upgrade()
                assert (
                    sync.execute(
                        text("SELECT to_regclass('workbench_skill_files')")
                    ).scalar()
                    is None
                )
                assert sync.execute(get_assets).mappings().first() == before[0]

        await conn.run_sync(cycle)
