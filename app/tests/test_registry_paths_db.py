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
    from test_workbench_task_runtime_db import close

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

        async def planning_window(self, subject, offset, limit):
            assert not session.in_transaction() and subject == "author"
            return list(self.permitted)[offset : offset + limit], False

        async def planning_formal_refs(self, subject, references):
            assert not session.in_transaction()
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


async def test_question_and_declaration_search_use_qualified_versions_and_pinned_steps(
    db,
):
    from uuid import uuid4

    from formal_plan_support import freeze
    from test_formal_execution_db import settle, status

    from app.application.workbench.formal_test_runs import FormalExecutions
    from app.domain.workbench.errors import NotFound
    from app.domain.workbench.formal_method_execution_generated import (
        FormalMethodExecutionRequest,
    )
    from app.domain.workbench.formal_method_generated import MethodRef
    from app.domain.workbench.method_search import PlanningQuery

    ctx = await setup_plan(db, expert_purpose="核对本期报表并汇总差异")
    v1 = ctx.leaf.reference
    updated = ctx.leaf_request.model_dump(mode="json", by_alias=True)
    updated["revision"] += 1
    updated["run_id"] = str(uuid4())
    updated["draft"]["definition"]["capability_declaration"]["title"] = (
        "Updated synthetic notice"
    )
    from app.domain.workbench.formal_method_execution_generated import (
        FormalTestSuiteDraft,
    )

    declaration = updated["draft"]["definition"]["capability_declaration"]
    promise = next(iter(declaration["promises"]))
    declaration["conditions"] = {
        promise: {"when": "Certified ledger reconciliations", "input_slots": ["to"]}
    }
    suite = updated["execution"]["suite"]
    suite.pop("digest")
    suite["cases"][0]["covers_promises"] = [promise]
    updated["execution"]["suite"] = (
        FormalTestSuiteDraft.model_validate(suite).freeze().model_dump(mode="json")
    )
    trial = FormalMethodExecutionRequest.model_validate(updated)

    async with db() as sql:
        repo = WorkbenchRepository(sql)
        admitted = await FormalExecutions(repo, ctx.factory(repo)).accept(
            trial, caller="knowlab"
        )
        assert admitted["status"] == "pending"
    await ctx.conductor.advance(str(trial.run_id))
    await approve(db, trial, ctx.factory, await status(db, trial, ctx.factory))
    await settle(ctx.conductor, trial)
    newer = freeze(trial, await status(db, trial, ctx.factory), version=2)
    v2 = newer.reference
    assert v2.digest != v1.digest

    class Market:
        allowed = {v2}
        checks = []

        async def planning_window(self, subject, offset, limit):
            assert not session.in_transaction() and subject == "author"
            self.checks.append((subject, offset, limit))
            return list(self.allowed)[offset : offset + limit], False

        async def planning_formal_refs(self, subject, refs):
            assert not session.in_transaction()
            assert subject == "author"
            return self.allowed.intersection(MethodRef.model_validate(r) for r in refs)

    market = Market()
    async with db() as session:
        methods = ctx.factory(WorkbenchRepository(session)).formal_sources
        await methods.register(newer, caller="knowlab")
        methods.market = market
        registry = methods.registry
        draft = ctx.request.draft.definition.capability_declaration
        owned = await registry.author_versions("author", 30, 0, declaration=draft)
        assert [registry.reference(r) for r in owned] == [v2.model_dump(mode="json")]
        question = PlanningQuery(stage="expert", question="核对本月报表")
        assert (await registry.planning_versions(OWNER, 30, 0, query=question))[
            "items"
        ] == []
        compose = question.model_copy(update={"stage": "composition"})
        results = await registry.planning_versions(OWNER, 30, 0, query=compose)
        assert [registry.reference(r) for r in results["items"]] == [
            v2.model_dump(mode="json")
        ]
        assert (await registry.planning_declaration(OWNER, v2))[
            "declaration"
        ] == newer.draft.definition.capability_declaration.model_dump(mode="json")
        with pytest.raises(NotFound):
            await registry.planning_declaration(OWNER, v1)
        market.allowed.clear()
        with pytest.raises(NotFound):
            await registry.planning_declaration(OWNER, v2)
    # The expert admitted before v2 continues to execute the v1 references.
    for _ in range(2):
        await approve(db, ctx.request, ctx.factory, await advance(db, ctx))
    expert = freeze(ctx.request, await advance(db, ctx))
    assert set(expert.validation.dependencies) == {v1}
    async with db() as session:
        methods = ctx.factory(WorkbenchRepository(session)).formal_sources
        await methods.register(expert, caller="knowlab")
        methods.market = market
        market.allowed.update({expert.reference, v2})
        results = await methods.registry.planning_versions(OWNER, 30, 0, query=question)
        assert [methods.registry.reference(r) for r in results["items"]] == [
            expert.reference.model_dump(mode="json")
        ]

        author_basis = draft.model_copy(
            update={
                "title": "核对报表",
                "purpose": "逐期汇总差异",
                "applies_when": ("有报表",),
                "promises": {"sent": "当期差异"},
            }
        )
        ranked = await methods.registry.author_versions(
            "author", 1, 0, declaration=author_basis
        )
        assert methods.registry.reference(ranked[0]) == expert.reference.model_dump(
            mode="json"
        )

    def factory(repo):
        svc = ctx.factory(repo)
        svc.formal_sources.market = market
        return svc

    url = "/api/workbench/method-skill-center/planning-methods"
    async with AsyncClient(
        transport=ASGITransport(application(db, factory)), base_url="http://testserver"
    ) as http:
        for offset, ref in enumerate([v2, expert.reference]):
            page = await http.post(
                url,
                params={"limit": 1, "offset": offset},
                json={"stage": "composition", "question": "return one receipt"},
            )
            assert (
                page.status_code == 200 and "no-store" in page.headers["cache-control"]
            )
            assert [methods.registry.reference(r) for r in page.json()["items"]] == [
                ref.model_dump(mode="json")
            ]
            assert page.json()["next"] == (1 if offset == 0 else None)
        english_check = market.checks[-1]
        conditional = await http.post(
            url,
            params={"limit": 1},
            json={"stage": "composition", "question": "certified reconciliations"},
        )
        assert conditional.json()["items"][0]["method_id"] == str(v2.method_id)
        detail = await http.post(url + "/declaration", json=v2.model_dump(mode="json"))
        assert detail.json()["promise_outputs"][promise] == ["receipt"]
        assert detail.json()["declaration"]["conditions"] == declaration["conditions"]
        chinese = await http.post(
            url,
            params={"limit": 1},
            json={"stage": "composition", "question": "核对报表"},
        )
        assert chinese.json()["items"][0]["method_id"] == str(
            expert.reference.method_id
        )
        assert market.checks[-1] == english_check
        assert english_check == ("author", 0, 100)
        assert (await http.post(url, json={"stage": "expert"})).status_code == 422
        market.allowed.remove(v2)
        assert (
            await http.post(url + "/declaration", json=v2.model_dump(mode="json"))
        ).status_code == 404
        async with db() as sql, sql.begin():
            await sql.execute(
                text("""
                UPDATE workbench_formal_methods SET document=jsonb_set(document,
                    '{draft,definition,capability_declaration,title}', '"changed"'::jsonb)
                WHERE method_id=:mid AND version=:version
            """),
                {
                    "mid": expert.reference.method_id,
                    "version": expert.reference.version,
                },
            )
        changed = await http.post(
            url + "/declaration", json=expert.reference.model_dump(mode="json")
        )
        assert changed.status_code == 409 and "declaration" not in changed.json()


async def test_sparse_planning_window_advances_without_scanning_the_entire_market():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import UUID

    from app.application.workbench.registry_catalog import SceneRegistry
    from app.domain.workbench.formal_method_generated import MethodRef
    from app.domain.workbench.method_search import PlanningQuery

    rows = [
        {"method_id": UUID(int=n), "latest_version": 1, "digest": "sha256:" + "a" * 64}
        for n in range(1, 233)
    ]

    async def candidates(owner, limit, offset, *, eligible=None, **filters):
        if eligible is None:
            return rows[offset : offset + limit]
        ids = {pin["method_id"] for pin in eligible}
        return [row for row in rows if str(row["method_id"]) in ids][
            offset : offset + limit
        ]

    allowed = [
        MethodRef.model_validate(SceneRegistry.reference(row)) for row in rows[100:]
    ]
    market = SimpleNamespace(
        planning_window=AsyncMock(
            side_effect=lambda subject, offset, limit: (
                allowed[offset : offset + limit],
                offset + limit < len(allowed),
            )
        )
    )
    repo = SimpleNamespace(
        transaction=lambda **_: nullcontext(),
        subject_of_actor=AsyncMock(return_value="author"),
        registry_method_candidates=candidates,
    )
    registry = SceneRegistry(SimpleNamespace(market=market), repo)
    query = PlanningQuery(stage="expert", question="Review")
    page = await registry.planning_versions(OWNER, 30, 0, query=query)
    ids = []
    while True:
        ids.extend(int(row["method_id"]) for row in page["items"])
        if page["next"] is None:
            break
        page = await registry.planning_versions(OWNER, 30, page["next"], query=query)
    assert ids == list(range(101, 233))
    # Two 90-pin qualification windows are ranked together for each output page.
    assert market.planning_window.await_count == 10
    assert all(call.args[2] == 90 for call in market.planning_window.await_args_list)

    # An entirely filtered block must retain a continuation and respect the
    # same per-request cap, however many more licensed pins exist.
    missing = [
        allowed[0].model_copy(update={"method_id": UUID(int=n)})
        for n in range(1000, 1450)
    ]
    allowed[:0] = missing
    market.planning_window.reset_mock()
    limited = await registry.planning_versions(OWNER, 30, 0, query=query)
    assert limited == {"items": [], "next": 360}
    assert market.planning_window.await_count == 4
    resumed = await registry.planning_versions(OWNER, 30, limited["next"], query=query)
    assert len(resumed["items"]) == 30
