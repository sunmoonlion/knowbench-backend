"""One installation, independent native and frozen entries, using synthetic peers."""

import pytest
from formal_plan_support import advance, setup_plan
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import OTHER, OWNER, approval, service
from test_formal_execution_db import approve
from test_formal_method_use_routes_db import application
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_ledger_db import seed
from test_workbench_routes import principal
from tool_invocation_support import request_call

from app.bootstrap.workbench import build_runner
from app.domain.workbench.capabilities import SelectionPut, tool_key
from app.domain.workbench.errors import Forbidden
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.http.middleware.auth import get_web_current_user


@pytest.mark.parametrize("scene", ["chat", "work"])
async def test_installed_send_and_expert_leaf_share_source_and_require_separate_approval(
    db, scene, tmp_path
):
    ctx = await setup_plan(db)
    step = await advance(db, ctx)
    ref = ctx.leaf.reference
    app = application(db, ctx.factory)
    _, sandbox, sid = await seed(db, root="/home/u/research/registry")
    installed = f"/api/workbench/registry/installed/{ctx.asset['id']}"
    formal = f"/api/workbench/registry/formal/{ref.method_id}/{ref.version}"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        asset = (await http.get(installed)).json()
        method = (await http.get(formal, params={"digest": ref.digest})).json()
        assert asset["path"] == "installed" and method["path"] == "formal"
        assert asset["asset_id"] == method["source_asset_id"]
        assert method["reference"] == ref.model_dump(mode="json")
        assert (
            await http.get(formal, params={"digest": "sha256:" + "0" * 64})
        ).status_code == 404
        app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
        assert (await http.get(installed)).status_code == 404
        assert (
            await http.get(formal, params={"digest": ref.digest})
        ).status_code == 404
    key = tool_key(asset["asset_id"], "send")
    assert asset["entries"] == [{"kind": "mcp", "entry": "send", "tool_key": key}]
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text("UPDATE workbench_sessions SET kind=:kind WHERE id=:sid"),
                {"kind": scene, "sid": sid},
            )
            await repo.set_session_thread(sid, "native-thread")
        svc = service(repo, ctx.remote)
        current = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=current["revision"],
                tools=[key],
                purpose="Notify the report recipient",
            ),
        )
        prepared = await svc.prepare(OWNER, sid)
        config = prepared["config"]["mcp_servers.sunmoon_capabilities"]
        token = config["http_headers"]["Authorization"].split()[1]
        if scene == "work":
            runner = build_runner(
                db, publisher=RedisPublisher(None, "test"), runner_id="registry-test"
            )
            link = await runner.link_for(sandbox, repo)
            report = tmp_path / "report.txt"
            report.write_text("Synthetic report")
            await link.on_notification(
                "item/completed",
                {
                    "threadId": "native-thread",
                    "turnId": "edit",
                    "item": {
                        "id": "local-edit",
                        "type": "fileChange",
                        "changes": [{"path": str(report)}],
                        "status": "completed",
                    },
                },
            )
            event = next(
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "item/completed"
            )
            assert event["payload"]["registry_action"] == "file_change"
            assert report.read_text() == "Synthetic report"
        args = {"to": "test@example.test"}
        requested = await request_call(svc, sid, token, key, args)
        native = await repo.capability_call(OWNER, requested["call_id"])
        expert = await repo.capability_call(OWNER, step["actions"][0]["call_id"])
        assert native["registration_id"] == expert["registration_id"]
        assert (
            native["source_asset_id"] == expert["source_asset_id"] == asset["asset_id"]
        )
        assert native["parent_call_id"] is None and expert["parent_call_id"]
        assert expert["detail"]["formal_host"]["reference"] == method["reference"]
        assert not ctx.remote.calls
        with pytest.raises(Forbidden):
            await svc.decide(OWNER, native["id"], approval(expert))
        assert (await svc.decide(OWNER, native["id"], approval(native)))[
            "status"
        ] == "succeeded"
        assert (await repo.capability_call(OWNER, expert["id"]))[
            "status"
        ] == "awaiting_approval"
    assert len(ctx.remote.calls) == 1
    await approve(db, ctx.request, ctx.factory, step)
    assert len(ctx.remote.calls) == 2
