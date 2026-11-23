import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_find_expert_db import _project
from test_formal_orchestration_admission import shape
from test_knowbot_test_runs_db import runner_for, setup_test_computer
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_task_runtime_db import ScriptedAppServer, close, drive

from app.application.workbench.find_expert import FindExpertService
from app.application.workbench.formal_expert_intake import confirm
from app.application.workbench.registry_catalog import SceneRegistry
from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion
from app.domain.workbench.formal_expert_match import verify_options
from app.domain.workbench.formal_method_generated import MethodRef
from app.infrastructure.workbench.repository import WorkbenchRepository


class Registry:
    def __init__(self):
        self.entries = {}
        self.definitions = {}
        root, _, _, deps = shape()
        self.dependencies = {d.reference: d.draft.definition for d in deps.values()}
        for label in ("逐笔", "汇总"):
            data = root.draft.definition.document()
            data["capability_declaration"].update(
                title=label,
                purpose="核对交易",
                applies_when=["交易明细"],
                excludes=["现金盘点"],
                promises={"result": label + "差异表"},
            )
            data["how_to_validate"][0]["promise_id"] = "result"
            definition = type(root.draft.definition).model_validate(data)
            ref = MethodRef(method_id=uuid4(), version=2, digest=definition.digest())
            self.definitions[ref] = definition
            self.entries[ref] = self.public_declaration(ref, definition)

    public_declaration = staticmethod(SceneRegistry.public_declaration)

    async def planning_definitions(self, owner, references):
        assert owner == OWNER
        return {r: self.dependencies[r] for r in references if r in self.dependencies}

    async def planning_definition(self, owner, reference, *, experts_only):
        await self.planning_declaration(owner, reference, experts_only=experts_only)
        return self.definitions[reference]

    async def planning_versions(self, owner, limit, offset, *, query):
        assert owner == OWNER and query.stage == "expert"
        return {"items": [v["reference"] for v in self.entries.values()], "next": None}

    def reference(self, row):
        return row

    async def planning_declaration(self, owner, reference, *, experts_only):
        assert owner == OWNER and experts_only
        if reference not in self.entries:
            raise NotFound("access revoked")
        return self.entries[reference]


def proposal(registry):
    return {
        "contract": "knowbench/expert-match/v5",
        "requirements": [
            {
                "id": "r",
                "kind": "result",
                "statement": "交付差异表",
                "question_quote": "差异表",
                "required": True,
            }
        ],
        "outcome": "candidates",
        "picks": [
            {
                "reference": v["reference"],
                "reason": v["declaration"]["title"],
                "coverage": [
                    {"requirement": "r", "promise": "result", "quote": "差异表"}
                ],
                "limitations": [],
            }
            for v in registry.entries.values()
        ],
        "explanation": "交付粒度不同",
        "clarification": None,
    }


def web_app(db, registry, monkeypatch):
    from test_formal_method_use_routes_db import application

    from app.interfaces.http.web import workbench_expert as routes

    app = application(db, lambda repo: None)

    async def session():
        async with db() as s:
            yield s

    async def wake():
        pass

    app.dependency_overrides[routes.get_db_session] = session
    monkeypatch.setattr(
        routes,
        "find_expert_service",
        lambda s: FindExpertService(WorkbenchRepository(s), formal_registry=registry),
    )
    monkeypatch.setattr(routes, "publish_commands_wakeup", wake)
    return app


async def test_project_match_compares_pins_and_confirms_once(db, monkeypatch):
    registry = Registry()
    result = proposal(registry)
    bad = deepcopy(result)
    bad["picks"][0]["coverage"][0]["quote"] = "捏造承诺"
    fake = ScriptedAppServer([json.dumps(bad), json.dumps(result)])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)

        async def start():
            async with db() as session:
                finder = FindExpertService(
                    WorkbenchRepository(session), formal_registry=registry
                )
                return await finder.start(
                    owner_actor_id=OWNER,
                    project_id=project,
                    question="需要差异表",
                    idempotency_key="formal-project-round",
                    match_contract="knowbench/expert-match/v5",
                )

        made, retry = await asyncio.gather(start(), start())
        assert made == retry
        await drive(runner)
        assert len(fake.turn_inputs) == 2
        assert (
            "逐笔差异表" in fake.turn_inputs[0]
            and "捏造承诺" not in fake.turn_inputs[0]
        )
        tid = made["task_id"]
        async with db() as session:
            finder = FindExpertService(
                WorkbenchRepository(session), formal_registry=registry
            )
            outcome = await finder.result(OWNER, tid)
            assert outcome["state"] == "SUCCEEDED", outcome
            review = outcome["formal"]
            assert len(review["options"]) == 2 and review["selected"] is None
        pins = list(registry.entries)

        async def select(pin, fingerprint=review["review_digest"]):
            async with db() as session:
                return await confirm(
                    WorkbenchRepository(session),
                    registry,
                    OWNER,
                    tid,
                    SimpleNamespace(reference=pin, review_digest=fingerprint),
                )

        with pytest.raises(StaleStateVersion):
            await select(pins[0], "sha256:" + "0" * 64)
        selected, replay = await asyncio.gather(select(pins[0]), select(pins[0]))
        assert selected == replay
        async with db() as session:
            finder = FindExpertService(
                WorkbenchRepository(session), formal_registry=registry
            )
            counts = text(
                "SELECT (SELECT count(*) FROM workbench_tasks), "
                "(SELECT count(*) FROM workbench_calls), "
                "(SELECT count(*) FROM workbench_artifacts)"
            )
            before = (await session.execute(counts)).one()
            other = registry.entries.pop(pins[1])
            result = (await finder.result(OWNER, tid))["formal"]
            assert (
                result["selected"] == selected["reference"]
                and len(result["options"]) == 1
            )
            preview = result["scope_preview"]
            assert [s["id"] for s in preview["steps"]] == ["only"]
            assert preview["inputs"][0]["required_by"] == ["only"]
            assert preview["requirements"][0]["resource_roles"] == ["recipient"]
            assert preview["outputs"][0]["step"] == "only"
            assert "test@example.test" not in json.dumps(preview)
            assert (
                preview == (await finder.result(OWNER, tid))["formal"]["scope_preview"]
            )
            assert len(fake.turn_inputs) == 2
            assert (await session.execute(counts)).one() == before
            registry.entries[pins[1]] = other
        app = web_app(db, registry, monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            page = await client.get(f"/api/workbench/find-expert/{tid}")
            assert (
                page.status_code == 200 and "no-store" in page.headers["cache-control"]
            )
            assert page.json()["formal"]["scope_preview"]["steps"][0]["id"] == "only"
            refused = await client.put(
                f"/api/workbench/find-expert/{tid}/end",
                json={"review_digest": review["review_digest"]},
            )
            assert refused.status_code == 409
            assert len(fake.turn_inputs) == 2
        with pytest.raises(IdempotencyConflict):
            await select(pins[1])
        async with db() as session:
            repo = WorkbenchRepository(session)
            assert (
                len(
                    await repo.list_task_events(
                        tid, types=("task/formal_expert_selected",)
                    )
                )
                == 1
            )
            with pytest.raises(NotFound):
                await FindExpertService(repo, formal_registry=registry).result(
                    str(uuid4()), tid
                )
        removed = registry.entries.pop(pins[0])
        async with db() as session:
            with pytest.raises(NotFound):
                await FindExpertService(
                    WorkbenchRepository(session), formal_registry=registry
                ).result(OWNER, tid)
        with pytest.raises(NotFound):
            await select(pins[0])
        assert await start() == made
        registry.entries[pins[0]] = removed
        async with db() as session:
            finder = FindExpertService(
                WorkbenchRepository(session), formal_registry=registry
            )
            revision = await finder.start(
                owner_actor_id=OWNER,
                project_id=project,
                question="仅核对逐笔差异表",
                reference=pins[0],
                revises=tid,
                idempotency_key="formal-revision-2",
                match_contract="knowbench/expert-match/v5",
            )
            assert (await finder.result(OWNER, tid))["superseded_by"] == revision[
                "task_id"
            ]
            new_snapshot = await finder.repo.list_task_events(
                revision["task_id"], types=("task/formal_expert_directory",)
            )
            assert [
                entry["reference"] for entry in new_snapshot[0]["payload"]["items"]
            ] == [removed["reference"]]
            with pytest.raises(StaleStateVersion, match="newer revision"):
                await finder.start(
                    owner_actor_id=OWNER,
                    project_id=project,
                    question="另一版本",
                    revises=tid,
                    idempotency_key="concurrent-revision",
                    match_contract="knowbench/expert-match/v5",
                )
        with pytest.raises(StaleStateVersion, match="revised"):
            await select(pins[0])

    finally:
        await close(runner)
        await fake.close()


def test_every_mandatory_delivery_needs_a_real_promise_and_output():
    registry = Registry()
    result = proposal(registry)
    result["requirements"].append({**result["requirements"][0], "id": "second"})
    with pytest.raises(ValueError, match="required delivery"):
        verify_options("差异表", {"items": list(registry.entries.values())}, result)


async def test_web_intake_empty_directory_starts_no_model_and_named_match_reopens(
    db, monkeypatch
):
    registry = Registry()
    entries = registry.entries.copy()
    registry.entries.clear()
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        app = web_app(db, registry, monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            url = f"/api/workbench/projects/{project}/find-expert"
            body = {
                "question": "差异表",
                "budget_limit": "2",
                "idempotency_key": "formal-route-1",
                "match_contract": "knowbench/expert-match/v5",
            }
            empty = await client.post(url, json=body)
            assert empty.status_code == 201 and empty.json()["empty"] is True
            assert empty.json()["next"] is None
            async with db() as s:
                assert await s.scalar(text("SELECT count(*) FROM workbench_tasks")) == 0
            # One window contains only tools. The next window contains experts.
            # Each request scans just the requested window and rechecks current access.
            calls = []

            async def windows(owner, limit, offset, *, query):
                assert owner == OWNER and limit == 8 and query.stage == "expert"
                calls.append(offset)
                if offset == 0:
                    return {"items": [], "next": 96}
                assert offset == 96
                return {
                    "items": [v["reference"] for v in registry.entries.values()],
                    "next": None,
                }

            monkeypatch.setattr(registry, "planning_versions", windows)
            first = await client.post(url, json=body)
            assert first.json()["empty"] is True and first.json()["next"] == 96
            continuation = {**body, "directory_offset": first.json()["next"]}
            exhausted = await client.post(url, json=continuation)
            assert (
                exhausted.json()["empty"] is True and exhausted.json()["next"] is None
            )
            async with db() as s:
                assert await s.scalar(text("SELECT count(*) FROM workbench_tasks")) == 0
            registry.entries.update(entries)
            found = await client.post(url, json=continuation)
            assert found.status_code == 201 and found.json()["task_id"]
            assert (await client.post(url, json=continuation)).json() == found.json()
            assert (await client.post(url, json=body)).status_code == 409
            assert calls == [0, 96, 96]
            named = next(iter(entries))
            assert (
                await client.post(
                    url,
                    json={**continuation, "reference": named.model_dump(mode="json")},
                )
            ).status_code == 409
            registry.entries.clear()
            body["idempotency_key"] = "formal-named-1"
            body["reference"] = named.model_dump(mode="json")
            assert (await client.post(url, json=body)).status_code == 404
            registry.entries.update(entries)
            made = await client.post(url, json=body)
            assert made.status_code == 201, made.text
            tid = made.json()["task_id"]
            assert (await client.post(url, json=body)).json() == made.json()
            recovered = await client.get(f"/api/workbench/find-expert/{tid}")
            assert (
                recovered.status_code == 200
                and recovered.json()["question"] == body["question"]
            )
            assert "no-store" in recovered.headers["cache-control"]
            pending = await client.put(
                f"/api/workbench/find-expert/{tid}/selection",
                json={"reference": body["reference"], "review_digest": named.digest},
            )
            assert pending.status_code == 409
    finally:
        await fake.close()


async def test_end_verified_no_match_is_durable_and_fences_revisions(db, monkeypatch):
    registry = Registry()
    answer = {
        **proposal(registry),
        "outcome": "no_match",
        "picks": [],
        "explanation": "缺少逐行凭证",
    }
    fake = ScriptedAppServer([json.dumps(answer)])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        base = f"/api/workbench/projects/{project}/find-expert"
        start = {
            "question": "需要差异表",
            "idempotency_key": "no-match-history",
            "match_contract": "knowbench/expert-match/v5",
        }
        async with AsyncClient(
            transport=ASGITransport(app=web_app(db, registry, monkeypatch)),
            base_url="http://testserver",
        ) as client:
            opened = await client.post(base, json=start)
            tid = opened.json()["task_id"]
            path = f"/api/workbench/find-expert/{tid}"
            invalid = {"review_digest": "sha256:" + "0" * 64}
            assert (await client.put(path + "/end", json=invalid)).status_code == 409
            await drive(runner)
            original = (await client.get(path)).json()["formal"]
            assert original["match"]["outcome"] == "no_match" and not original["ended"]
            assert (await client.put(path + "/end", json=invalid)).status_code == 409
            receipt = {"review_digest": original["review_digest"]}
            from app.application.workbench.formal_expert_intake import end_unmatched

            async with db() as session:
                with pytest.raises(NotFound):
                    await end_unmatched(
                        WorkbenchRepository(session),
                        str(uuid4()),
                        tid,
                        receipt["review_digest"],
                    )
            results = await asyncio.gather(
                *(client.put(path + "/end", json=receipt) for _ in range(2))
            )
            assert all(r.status_code == 200 and r.json() == receipt for r in results)
            assert (await client.get(path)).json()["formal"] == {
                **original,
                "ended": True,
            }
            assert (await client.post(base, json=start)).json()["task_id"] == tid
            registry.entries.clear()
            refused = await client.post(
                base,
                json={**start, "revises": tid, "idempotency_key": "stale-revision"},
            )
            assert refused.status_code == 409, refused.text
            async with db() as session:
                repo = WorkbenchRepository(session)
                assert (
                    len(
                        await repo.list_task_events(
                            tid, types=("task/formal_expert_ended",)
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(text("SELECT count(*) FROM workbench_tasks"))
                    == 1
                )
            assert len(fake.turn_inputs) == 1
    finally:
        await close(runner)
        await fake.close()
