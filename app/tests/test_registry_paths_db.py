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
    installed = f"/api/workbench/method-skill-center/installed/{ctx.asset['id']}"
    formal = f"/api/workbench/method-skill-center/formal/{ref.method_id}/{ref.version}"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        inventory = await http.get("/api/workbench/method-skill-center")
        assert inventory.status_code == 200, inventory.text
        listing = inventory.json()
        assert any(
            row["reference"] == ref.model_dump(mode="json") for row in listing["formal"]
        )
        assert any(row["asset_id"] == ctx.asset["id"] for row in listing["installed"])
        assert any(row["name"] == "permissions" for row in listing["native"])
        assert (
            await http.get("/api/workbench/method-skill-center/activity")
        ).json() == []
        asset = (await http.get(installed)).json()
        method = (await http.get(formal, params={"digest": ref.digest})).json()
        assert asset["path"] == "installed" and method["path"] == "formal"
        assert asset["asset_id"] == method["source_asset_id"]
        assert method["reference"] == ref.model_dump(mode="json")
        assert method in listing["formal"]
        assert (method["method_kind"], method["runner_route"]) == ("tool", "formal_mcp")
        assert method["allowed_callers"]
        assert (
            await http.get(formal, params={"digest": "sha256:" + "0" * 64})
        ).status_code == 404
        app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
        assert (await http.get("/api/workbench/method-skill-center")).json()[
            "formal"
        ] == []
        assert (await http.get("/api/workbench/method-skill-center")).json()[
            "installed"
        ] == []
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


async def test_installation_and_two_derived_methods_in_one_turn(db):
    from uuid import uuid4

    from formal_method_use_support import with_listings
    from formal_model_support import ModelServer, setup
    from formal_plan_support import freeze
    from test_formal_model_admission_db import _decide_case
    from test_workbench_advisor_db import close

    from app.application.workbench.formal_session_tools import (
        FormalSessionTools,
        formal_tool_key,
    )
    from app.application.workbench.formal_test_runs import FormalExecutions
    from app.domain.workbench.formal_method_execution_generated import (
        FormalMethodExecutionRequest,
    )

    fake = ModelServer()
    await fake.start()
    runner = None
    try:
        first, remote, factory, conductor, runner = await setup(db, fake)
        doc = first.model_dump(mode="json", by_alias=True)
        doc.update(method_id=str(uuid4()), run_id=str(uuid4()))
        doc["draft"]["definition"]["capability_declaration"]["title"] = "Second notice"
        second = FormalMethodExecutionRequest.model_validate(doc)
        versions = []
        for request in (first, second):
            async with db() as session:
                repo = WorkbenchRepository(session)
                await FormalExecutions(repo, factory(repo)).accept(
                    request, caller="knowlab"
                )
            await _decide_case(
                db,
                fake,
                request,
                request.execution.suite.cases[0],
                runner,
                conductor,
                decision="accept",
            )
            await conductor.advance(str(request.run_id))
            async with db() as session:
                repo = WorkbenchRepository(session)
                service = FormalExecutions(repo, factory(repo))
                result = await service.status(
                    "author", str(request.run_id), caller="knowlab"
                )
                frozen = freeze(request, result)
                await service.formal.register(frozen, caller="knowlab")
                versions.append(frozen)
        _, _, sid = await seed(db, root="/home/u/research/registry-three")
        factory = with_listings(factory, *versions)
        async with db() as session:
            repo = WorkbenchRepository(session)
            svc = factory(repo)
            source = first.draft.source
            raw = tool_key(str(source.asset_id), source.entry)
            refs = [v.reference for v in versions]
            keys = [raw, *(formal_tool_key(r) for r in refs)]
            current = await svc.selection(OWNER, sid)
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=current["revision"],
                    tools=[raw],
                    formal_methods=tuple(refs),
                    purpose="Two registered notices",
                ),
            )
            prepared = await svc.prepare(OWNER, sid)
            token = prepared["config"]["mcp_servers.sunmoon_capabilities"][
                "http_headers"
            ]["Authorization"].split()[1]
            remote.calls.clear()
            calls = []
            for key in keys:
                started = await request_call(
                    svc, sid, token, key, {"to": "test@example.test"}
                )
                calls.append(await repo.capability_call(OWNER, started["call_id"]))
            assert len({c["id"] for c in calls}) == 3
            assert len({str(c["source_asset_id"]) for c in calls}) == 1
            assert not remote.calls
            for call in calls:
                assert (await svc.decide(OWNER, call["id"], approval(call)))[
                    "status"
                ] == "succeeded"
            assert len(remote.calls) == 3
            history = {r["record_id"]: r for r in await repo.registry_activity(OWNER)}
            assert [history[c["id"]]["path"] for c in calls] == [
                "installed",
                "formal",
                "formal",
            ]
            assert [history[c["id"]]["reference"] for c in calls[1:]] == [
                r.model_dump(mode="json") for r in refs
            ]

            svc.formal_market.versions.remove(("author", refs[0]))
            runtime = (await repo.capability_selection(OWNER, sid))["runtime"]
            assert set(await FormalSessionTools(svc).available(OWNER, runtime)) == {
                raw,
                keys[2],
            }
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def test_link_writes_report_and_work_session_delivers_approved_content(
    db, tmp_path
):
    import asyncio
    import json
    import os
    from pathlib import Path

    from test_capabilities_db import OPERATION, URL, Remote, body

    from app.domain.workbench.tool_registry import ToolRegistration

    agent = os.environ.get("REGISTRY_TEST_LINK_AGENT")
    if not agent:
        pytest.skip("Set REGISTRY_TEST_LINK_AGENT to the built local Link agent")
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_name("registry_link_report.mjs")),
        agent,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0
    messages = [json.loads(line) for line in out.splitlines()]
    [report] = [message["body"] for message in messages if "body" in message]
    assert (
        report
        == (tmp_path / "reports" / "report.txt").read_text()
        == "Synthetic monthly report"
    )
    remote = Remote()
    tool = remote.tools[0]
    tool["inputSchema"]["properties"]["body"] = {"type": "string"}
    tool["inputSchema"]["required"].append("body")
    policy = OPERATION.model_copy(
        update={
            "descriptor_digest": ToolRegistration.discover(
                "mcp", URL, tool
            ).descriptor_digest
        }
    )
    _, _, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text("UPDATE workbench_sessions SET kind='work' WHERE id=:sid"),
                {"sid": sid},
            )
        svc = service(repo, remote, operations=(policy,))
        asset = await svc.create_connection(OWNER, body())
        key = tool_key(asset["id"], tool["name"])
        selection = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selection["revision"],
                tools=[key],
                purpose="Send the approved report",
            ),
        )
        turn = await svc.prepare(OWNER, sid)
        token = turn["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
        args = {"to": "recipient@example.test", "body": report}
        requested = await request_call(svc, sid, token, key, args)
        call = await repo.capability_call(OWNER, requested["call_id"])
        assert call["detail"]["arguments"] == args and not remote.calls
        assert (await svc.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "succeeded"
        assert len(remote.calls) == 1 and remote.calls[0][-1] == args
        activity = await repo.registry_activity(OWNER)
        assert any(
            row["record_id"] == call["id"] and row["status"] == "succeeded"
            for row in activity
        )
        assert not await repo.registry_activity(OTHER)


async def test_registry_scenes_share_versions_but_not_grants(db):
    from uuid import uuid4

    from app.application.workbench.formal_methods import FormalMethods
    from app.domain.workbench.capabilities import CapabilityError
    from app.domain.workbench.formal_method_generated import MethodRef
    from app.domain.workbench.registry_sources import SourceDraft

    ctx = await setup_plan(db)
    ref = ctx.leaf.reference

    class Market:
        permitted = set()

        async def planning_formal_refs(self, subject, references):
            assert subject == "author"
            return self.permitted.intersection(
                MethodRef.model_validate(r) for r in references
            )

    market = Market()
    async with db() as session:
        repo = WorkbenchRepository(session)
        methods = FormalMethods(repo, market=market)
        async with repo.transaction():
            direct = await repo.register_direct_revision(
                OWNER,
                uuid4(),
                1,
                SourceDraft(kind="script", name="reconcile.py", content="print(1)"),
            )
        sources = await methods.sources("author")
        item = next(x for x in sources["items"] if x["kind"] == "registered_script")
        assert (item["source"], item["digest"]) == (
            str(direct["source_id"]),
            direct["digest"],
        )
        assert item["asset_id"] is None

        def refs(rows):
            return [methods.registry.reference(r) for r in rows]

        registered = await methods.registry.author_versions("author", 30, 0)
        assert ref.model_dump(mode="json") in refs(registered)
        assert not (await methods.registry.planning_versions(OWNER, 30, 0))["items"]
        market.permitted = {ref}
        assert refs(
            (await methods.registry.planning_versions(OWNER, 30, 0))["items"]
        ) == [ref.model_dump(mode="json")]
        assert not await methods.registry.author_versions("other", 30, 0)
        market.permitted.clear()
        assert not (await methods.registry.planning_versions(OWNER, 30, 0))["items"]
        # A stale snapshot and even a positive market answer cannot substitute
        # for the exact live registration.
        market.permitted = {ref}
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_formal_methods SET digest=:d WHERE method_id=:id"
                ),
                {"id": str(ref.method_id), "d": "sha256:" + "0" * 64},
            )
        assert not (await methods.registry.planning_versions(OWNER, 30, 0))["items"]
        with pytest.raises(CapabilityError):
            await methods.registry.dependency(OWNER, ref)
    before = len(ctx.remote.calls)
    result = await advance(db, ctx)
    assert len(ctx.remote.calls) == before
    assert result["status"] == "failed" or not result["actions"]
