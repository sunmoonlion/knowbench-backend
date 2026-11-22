"""Exact independent business review for a host-executed, frozen expert."""

import pytest
from formal_plan_support import freeze, setup_plan
from formal_recursive_support import (
    accept_request,
    assert_passed,
    complete,
    parent_request,
)
from sqlalchemy import text
from test_workbench_ledger_db import db as db

from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.source_licenses import BusinessCoverageReview
from app.infrastructure.workbench.repository import WorkbenchRepository


async def reviewed_plan(db, *, public_leaf=True):
    ctx = await setup_plan(db, accept=False, public_leaf=public_leaf)
    expert, review = await review_trial(db, ctx)
    return ctx, expert, review


async def review_trial(db, ctx):
    req = ctx.request
    review = BusinessCoverageReview(
        subject=req.owner_subject,
        method_id=req.method_id,
        definition_digest=req.draft.definition.digest(),
        source_digest=None,
        suite_digest=req.execution.suite.digest,
        evidence={"reference": "test:expert-coverage", "digest": "sha256:" + "e" * 64},
        expires_at="2100-01-01T00:00:00Z",
    )
    original = ctx.factory

    def factory(repo):
        caps = original(repo)
        caps.formal_sources.business_reviews += (review,)
        return caps

    ctx.factory = factory
    ctx.conductor = KnowbotTestConductor(
        workbench_stores(db), formal_capabilities=factory
    )
    await accept_request(db, ctx)
    result = await complete(db, ctx)
    await assert_passed(db, ctx, result)
    expert = freeze(req, result).model_copy(update={"visibility": "public"})
    assert "business" in expert.validation.checks and not expert.validation.uncovered
    return expert, review


async def test_review_is_sealed_to_the_exact_expert_trial(db):
    ctx, expert, review = await reviewed_plan(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        service = ctx.factory(repo).formal_sources
        async with repo.transaction():
            await service.check_delivery_source(ctx.owner, expert)
            for reviews in (
                (),
                (review, review),
                (review.model_copy(update={"source_digest": "sha256:" + "a" * 64}),),
                (review.model_copy(update={"suite_digest": "sha256:" + "a" * 64}),),
                (
                    review.model_copy(
                        update={"definition_digest": "sha256:" + "a" * 64}
                    ),
                ),
                (review.model_copy(update={"expires_at": service.now()}),),
                (
                    review.model_copy(
                        update={
                            "evidence": review.evidence.model_copy(
                                update={"reference": "test:later-review"}
                            )
                        }
                    ),
                ),
            ):
                service.business_reviews = reviews
                with pytest.raises(Forbidden):
                    await service.check_delivery_source(ctx.owner, expert)


@pytest.mark.parametrize("public_leaf", [True, False])
async def test_public_expert_registration_qualifies_the_entire_closure(db, public_leaf):
    ctx, expert, _ = await reviewed_plan(db, public_leaf=public_leaf)
    async with db() as session:
        repo = WorkbenchRepository(session)
        service = ctx.factory(repo).formal_sources
        if not public_leaf:
            with pytest.raises(Forbidden):
                await service.register(expert, caller="knowlab")
            assert (
                await repo.formal_method(ctx.owner, str(expert.reference.method_id), 1)
                is None
            )
            return
        await service.register(expert, caller="knowlab")
        delivered = await service.delivery("author", expert.reference, market=True)
        assert delivered["reference"] == expert.reference.model_dump(mode="json")
        service.business_reviews = tuple(
            r
            for r in service.business_reviews
            if r.method_id != ctx.leaf.reference.method_id
        )
        with pytest.raises(Forbidden):
            await service.delivery("author", expert.reference, market=True)


async def test_public_expert_can_reuse_a_reviewed_public_subexpert(db):
    ctx, child, _ = await reviewed_plan(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await ctx.factory(repo).formal_sources.register(child, caller="knowlab")
        async with repo.transaction():
            await repo.purge_test_run(str(ctx.request.run_id))
    ctx.request = parent_request(
        ctx.request, [child, child], depth=3, expansions=7, steps=6
    )
    root, _ = await review_trial(db, ctx)
    async with db() as session:
        repo = WorkbenchRepository(session)
        service = ctx.factory(repo).formal_sources
        await service.register(root, caller="knowlab")
        assert (await service.delivery("author", root.reference, market=True))[
            "reference"
        ] == root.reference.model_dump(mode="json")
        service.business_reviews = tuple(
            r
            for r in service.business_reviews
            if r.method_id != child.reference.method_id
        )
        with pytest.raises(Forbidden):
            await service.delivery("author", root.reference, market=True)


async def test_invalid_root_trial_rolls_back_public_registration(db):
    ctx, expert, _ = await reviewed_plan(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text("UPDATE workbench_test_runs SET digest=:wrong WHERE run_id=:run"),
                {"wrong": "0" * 64, "run": str(ctx.request.run_id)},
            )
        with pytest.raises(CapabilityError, match="execution request"):
            await ctx.factory(repo).formal_sources.register(expert, caller="knowlab")
        assert (
            await repo.formal_method(ctx.owner, str(expert.reference.method_id), 1)
            is None
        )
