"""Real model qualification gates explicit per-conversation method exposure."""

import pytest
from sqlalchemy import text
from test_capabilities_db import OWNER
from test_formal_method_catalog_db import admitted
from test_formal_model_catalog_db import model_admitted
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_ledger_db import seed

from app.application.workbench.formal_session_tools import (
    FormalSessionTools,
    formal_tool_key,
)
from app.domain.workbench.capabilities import CapabilityError, SelectionPut
from app.domain.workbench.projects import ExecutionScene
from app.domain.workbench.tool_operations import OperationCatalog
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_only_explicit_qualified_reference_is_mounted_and_experts_do_not_inherit(
    db,
):
    frozen, _, factory, remote = await model_admitted(db)
    _, _, sid = await seed(db, root="/home/u/research/formal-tools")
    before = len(remote.calls)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        initial = await svc.selection(OWNER, sid)
        assert initial["formal_methods"] == []
        empty = await svc.prepare(OWNER, sid)
        assert not empty["config"]["mcp_servers.sunmoon_capabilities"]["enabled"]
        chosen = await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=initial["revision"], formal_methods=(frozen.reference,)
            ),
        )
        assert chosen["formal_methods"] == [frozen.reference.model_dump(mode="json")]
        prepared = await svc.prepare(OWNER, sid)
        key = formal_tool_key(frozen.reference)
        assert prepared["config"]["mcp_servers.sunmoon_capabilities"][
            "enabled_tools"
        ] == [key, "action_result"]
        runtime = (await repo.capability_selection(OWNER, sid))["runtime"]
        assert set(runtime["tools"]) == {key}
        spec = runtime["tools"][key]
        assert spec["execution_kind"] == "formal_method_model"
        assert spec["tool"]["inputSchema"]["properties"] == {"to": {"type": "string"}}
        assert spec["formal_method"]["frozen"] == frozen.model_dump(
            mode="json", by_alias=True
        )
        assert (
            await FormalSessionTools(svc).available(OWNER, runtime) == runtime["tools"]
        )
        isolated = await svc.prepare(OWNER, sid, scene=ExecutionScene.METHOD)
        assert not isolated["config"]["mcp_servers.sunmoon_capabilities"]["enabled"]
        assert (await svc.selection(OWNER, sid))["formal_methods"] == chosen[
            "formal_methods"
        ]
    assert len(remote.calls) == before


async def test_prepared_formal_method_is_removed_after_current_source_or_policy_changes(
    db,
):
    frozen, _, factory, _ = await model_admitted(db)
    _, _, sid = await seed(db, root="/home/u/research/formal-tools")
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        current = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=current["revision"], formal_methods=(frozen.reference,)
            ),
        )
        await svc.prepare(OWNER, sid)
        runtime = (await repo.capability_selection(OWNER, sid))["runtime"]
        svc.operations = OperationCatalog(())
        assert svc.runtime_tools(runtime) == {}
        svc = factory(repo)
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                ),
                {"id": str(frozen.draft.source.asset_id)},
            )
        assert await FormalSessionTools(svc).available(OWNER, runtime) == {}
        current = await svc.selection(OWNER, sid)
        assert current["formal_methods"] == [frozen.reference.model_dump(mode="json")]
        assert {
            "ref": formal_tool_key(frozen.reference),
            "code": "formal_method_unavailable",
        } in current["issues"]
        prepared = await svc.prepare(OWNER, sid)
        assert not prepared["config"]["mcp_servers.sunmoon_capabilities"]["enabled"]
        assert (await repo.capability_selection(OWNER, sid))["runtime"]["tools"] == {}


async def test_host_only_qualification_cannot_be_selected_for_a_model(db):
    frozen, _, _, factory, _ = await admitted(db)
    _, _, sid = await seed(db, root="/home/u/research/formal-tools")
    async with db() as session:
        svc = factory(WorkbenchRepository(session))
        current = await svc.selection(OWNER, sid)
        with pytest.raises(CapabilityError):
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=current["revision"], formal_methods=(frozen.reference,)
                ),
            )
        assert (await svc.selection(OWNER, sid))["formal_methods"] == []
