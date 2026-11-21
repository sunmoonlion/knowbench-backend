"""A public tool runs in another author's real expert validation on their own connection."""

from uuid import UUID, uuid4

import pytest
from formal_plan_support import advance, freeze, setup_plan
from formal_recursive_support import accept_request, parent_request, register_completed
from test_capabilities_db import body, service
from test_formal_execution_db import approve
from test_source_licenses import license_record
from test_workbench_ledger_db import OTHER, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
)
from app.domain.workbench.incubation_tools import IncubationGrant, IncubationGrants
from app.domain.workbench.source_licenses import SourceLicenseCatalog
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("revoke", [False, True])
async def test_cross_author_reference_uses_consumers_account(db, revoke):
    ctx = await setup_plan(db, accept=False, public_leaf=True)
    pub = ctx.leaf
    grant = IncubationGrant(
        owner_subject=pub.owner_subject,
        listing_id=uuid4(),
        reference=pub.reference,
        scope="acquired",
    )

    class Market:
        active = True
        author_session = None

        async def incubation_tools(
            self, subject, *, reference=None, after=None, limit=30
        ):
            if self.author_session is not None:
                assert not self.author_session.in_transaction()
            allowed = (
                self.active
                and subject == "other"
                and (reference is None or reference == pub.reference)
            )
            return IncubationGrants(
                subject=subject, items=(grant,) if allowed else (), next=None
            )

    market = Market()
    env, _, _ = await seed(db, owner=OTHER)
    source = pub.draft.source.model_dump(mode="json")
    licenses = SourceLicenseCatalog(
        (
            license_record(source, rights={"execute": True, "deliver": True}),
            license_record(source, subject="other", record_id="consumer"),
        )
    )
    original = ctx.factory

    def factory(repo):
        caps = original(repo)
        caps.formal_sources.source_licenses = licenses
        caps.formal_sources.market = market
        return caps

    ctx.factory = factory
    ctx.conductor = KnowbotTestConductor(
        workbench_stores(db), formal_capabilities=factory
    )
    raw = ctx.request.model_dump(mode="json", by_alias=True)
    raw["owner_subject"] = "other"
    raw["execution"]["runner"]["environment_id"] = env
    ctx.request = FormalMethodExecutionRequest.model_validate(raw)
    async with db() as sql:
        repo = WorkbenchRepository(sql)
        market.author_session = sql
        svc = factory(repo).formal_sources
        assert not (await svc.shared_tools("other"))["items"]
        buyer_asset = await service(repo).create_connection(
            OTHER, body().model_copy(update={"token": "only-consumer-token"})
        )
        assert buyer_asset["id"] != str(pub.draft.source.asset_id)
        tools = await svc.shared_tools("other", reference=pub.reference)
        assert tools["items"][0]["definition"] == pub.draft.definition.model_dump(
            mode="json", by_alias=True
        )
        assert tools["items"][0]["selected"] == buyer_asset["id"]
        second = await service(repo).create_connection(
            OTHER,
            body("second-account").model_copy(
                update={"token": "second-token", "account_label": "second@example.test"}
            ),
        )
        detail = (await svc.shared_tools("other", reference=pub.reference))["items"][0]
        assert detail["selected"] is None
        assert {i["asset_id"] for i in detail["installations"]} == {
            buyer_asset["id"],
            second["id"],
        }
        rev = next(
            i["revision"]
            for i in detail["installations"]
            if i["asset_id"] == buyer_asset["id"]
        )
        for invalid, revision in [
            (pub.draft.source.asset_id, rev),
            (UUID(buyer_asset["id"]), rev + 1),
        ]:
            with pytest.raises(CapabilityError):
                await svc.choose_incubation_asset(
                    "other", pub.reference, invalid, revision
                )
        receipt = await svc.choose_incubation_asset(
            "other", pub.reference, UUID(buyer_asset["id"]), rev
        )
        assert receipt["asset_id"] == buyer_asset["id"]
        assert (await svc.shared_tools("other", reference=pub.reference))["items"][0][
            "selected"
        ] == buyer_asset["id"]
        market.author_session = None
        assert (
            await FormalExecutions(repo, factory(repo)).accept(
                ctx.request, caller="knowlab"
            )
        )["status"] == "pending"
    for i in range(2):
        pending = await advance(db, ctx)
        assert pending["actions"], pending
        if revoke and i == 1:
            market.active = False
            with pytest.raises(CapabilityError):
                await approve(db, ctx.request, ctx.factory, pending)
            assert len(ctx.remote.calls) == 1
            return
        await approve(db, ctx.request, ctx.factory, pending)
        assert len(ctx.remote.calls) == i + 1
        assert ctx.remote.calls[-1][1] == "only-consumer-token"
    result = await advance(db, ctx)
    assert result["status"] == "passed", result
    async with db() as sql:
        repo = WorkbenchRepository(sql)
        frozen = freeze(ctx.request, result)
        assert (await factory(repo).formal_sources.register(frozen, caller="knowlab"))[
            "status"
        ] == "registered"
        rows = await repo.list_task_calls(OTHER, result["cases"][0]["task_id"])
        assert {
            str(row["source_asset_id"]) for row in rows if row["kind"] == "tool"
        } == {buyer_asset["id"]}

        with pytest.raises(CapabilityError):
            await factory(repo).formal_sources.delivery(
                "other", frozen.reference, market=True
            )
    ctx.owner = OTHER
    ctx.request = parent_request(ctx.request, [frozen], depth=3, expansions=4, steps=3)
    await accept_request(db, ctx)
    nested = await register_completed(db, ctx)
    assert nested.owner_subject == "other"
    assert len(ctx.remote.calls) == 4
    assert all(c[1] == "only-consumer-token" for c in ctx.remote.calls)


async def test_shared_script_uses_consumers_verified_package_and_meter(db):
    from types import SimpleNamespace

    from formal_plan_support import plan_request
    from test_formal_execution_db import settle, status
    from test_formal_script_execution_db import SOURCE, collect_script, script_setup
    from test_skill_package import archive
    from test_skill_package_db import package

    from app.domain.workbench.formal_method_admission_generated import (
        FrozenFormalMethod,
    )

    req, original, conductor, executor = await script_setup(db, public_review=True)
    await conductor.advance(str(req.run_id))
    await approve(db, req, original, await status(db, req, original))
    await collect_script(db, original)
    await settle(conductor, req)
    leaf = freeze(req, await status(db, req, original))
    pub = FrozenFormalMethod.model_validate(
        {**leaf.model_dump(mode="json", by_alias=True), "visibility": "public"}
    )
    grant = IncubationGrant(
        owner_subject="author",
        listing_id=uuid4(),
        reference=pub.reference,
        scope="platform",
    )

    class Platform:
        async def incubation_tools(self, subject, **kw):
            assert subject == "other"
            return IncubationGrants(subject=subject, items=(grant,), next=None)

    async with db() as sql:
        repo = WorkbenchRepository(sql)
        await original(repo).formal_sources.register(pub, caller="knowlab")
        asset = await service(repo).create_skill(
            OTHER,
            package().model_copy(
                update={
                    "archive_base64": archive(
                        {"calculate.py": SOURCE, "SKILL.md": "Run"}
                    )
                }
            ),
            package=True,
        )
        local = next(
            x
            for x in (await original(repo).formal_sources.sources("other"))["items"]
            if x["kind"] == "skill_script"
        )
        assert (
            local["source"] != pub.draft.source.source
            and local["digest"] == pub.draft.source.digest
        )
    licenses = SourceLicenseCatalog(
        (
            license_record(
                pub.draft.source.model_dump(mode="json"),
                rights={"execute": True, "deliver": True},
                delivery_terms={"origin": "test/skill", "notices": ["Test package"]},
            ),
            license_record(local, subject="other", record_id="consumer-skill"),
        )
    )

    def factory(repo):
        svc = original(repo)
        svc.formal_sources.source_licenses = licenses
        svc.formal_sources.market = Platform()
        return svc

    env, _, _ = await seed(db, owner=OTHER)
    case = req.execution.suite.cases[0].model_dump(mode="json")
    raw = plan_request(req, pub, case).model_dump(mode="json", by_alias=True)
    raw["owner_subject"] = "other"
    raw["execution"]["runner"]["environment_id"] = env
    request = FormalMethodExecutionRequest.model_validate(raw)
    ctx = SimpleNamespace(
        request=request,
        factory=factory,
        conductor=KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=factory
        ),
    )
    async with db() as sql:
        repo = WorkbenchRepository(sql)
        assert (await factory(repo).formal_sources.shared_tools("other"))["items"][0][
            "reference"
        ] == pub.reference.model_dump(mode="json")
        assert (
            await FormalExecutions(repo, factory(repo)).accept(
                request, caller="knowlab"
            )
        )["status"] == "pending"
    for _ in range(2):
        pending = await advance(db, ctx)
        assert pending["actions"], pending
        await approve(db, request, factory, pending)
        await collect_script(db, factory)
    result = await advance(db, ctx)
    assert result["status"] == "passed", result
    async with db() as sql:
        repo = WorkbenchRepository(sql)
        registered = await factory(repo).formal_sources.register(
            freeze(request, result), caller="knowlab"
        )
        assert registered["status"] == "registered"
        task = result["cases"][0]["task_id"]
        calls = await repo.list_task_calls(OTHER, task)
        assert {str(c["source_asset_id"]) for c in calls if c["kind"] == "tool"} == {
            asset["id"]
        }
        from decimal import Decimal

        from app.application.workbench.script_budget import task_script_cost

        assert await task_script_cost(repo, task) == Decimal("0.08")
