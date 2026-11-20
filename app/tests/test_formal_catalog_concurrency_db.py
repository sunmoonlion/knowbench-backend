"""Directory and preparation serialize source locks across two conversations."""

import asyncio
from uuid import uuid4

from formal_method_use_support import with_listings
from sqlalchemy import text
from test_capabilities_db import OWNER, body
from test_formal_execution_db import approve, settle, setup, status
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_ledger_db import seed

from app.application.workbench.formal_method_calls import FormalMethodCalls
from app.application.workbench.formal_test_runs import FormalExecutions
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_directory_reverse_source_order_cannot_deadlock_preparation(db):
    template, first, remote, factory, conductor = await setup(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        second = await factory(repo).create_connection(OWNER, body("second-source"))
    assets = sorted([first, second], key=lambda asset: asset["id"])
    versions = []
    # Real test executions and registrations: later-created method uses the
    # higher-ID source, so the directory order is opposite to asset lock order.
    for asset in assets:
        doc = template.model_dump(mode="json", by_alias=True)
        for field in ("run_id", "request_id", "workdraft_id", "method_id"):
            doc[field] = str(uuid4())
        doc["draft"]["source"].update(
            asset_id=asset["id"], asset_revision=asset["revision"]
        )
        suite = doc["execution"]["suite"]
        suite.pop("digest")
        suite["suite_id"] = str(uuid4())
        doc["execution"]["suite"] = (
            FormalTestSuiteDraft.model_validate(suite).freeze().model_dump(mode="json")
        )
        request = FormalMethodExecutionRequest.model_validate(doc)
        async with db() as session:
            repo = WorkbenchRepository(session)
            await FormalExecutions(repo, factory(repo)).accept(
                request, caller="knowlab"
            )
        await conductor.advance(str(request.run_id))
        await approve(db, request, factory, await status(db, request, factory))
        await settle(conductor, request)
        report = await status(db, request, factory)
        assert report["status"] == "passed", report
        frozen = FrozenFormalMethod.model_validate(
            {
                "contract": "knowlab/frozen-formal-method/v1",
                "owner_subject": request.owner_subject,
                "reference": {
                    "method_id": request.method_id,
                    "version": 1,
                    "digest": request.draft.definition.digest(),
                },
                "draft": request.draft,
                "validation": report["admission"]["receipt"],
                "visibility": "private",
            }
        )
        async with db() as session:
            repo = WorkbenchRepository(session)
            await factory(repo).formal_sources.register(frozen, caller="knowlab")
        versions.append(frozen)
    factory = with_listings(factory, *versions)
    _, _, directory_session = await seed(db, root="/home/u/research/catalog-locks")
    _, _, prepare_session = await seed(db, root="/home/u/research/prepare-locks")
    async with db() as session:
        rows = await WorkbenchRepository(session).list_formal_methods(OWNER)
        assert [row["document"]["draft"]["source"]["asset_id"] for row in rows] == [
            asset["id"] for asset in reversed(assets)
        ]
    prepare_has_first, directory_entered = asyncio.Event(), asyncio.Event()

    class PreparingRepository(WorkbenchRepository):
        async def capability_assets(self, owner, *, lock=False):
            if lock and not prepare_has_first.is_set():
                await self.session.execute(text("SET LOCAL lock_timeout = '3s'"))
                await self.capability_asset(owner, assets[0]["id"], lock=True)
                prepare_has_first.set()
                await asyncio.wait_for(directory_entered.wait(), 5)
            return await super().capability_assets(owner, lock=lock)

    class DirectoryRepository(WorkbenchRepository):
        async def capability_assets(self, owner, *, lock=False):
            if lock:
                directory_entered.set()
            return await super().capability_assets(owner, lock=lock)

        async def capability_asset(self, owner, asset_id, *, lock=False):
            value = await super().capability_asset(owner, asset_id, lock=lock)
            # On the old path there was no ordered prelock. Let preparation
            # contend only after the directory owns the higher source lock;
            # PostgreSQL then detects the actual A/B versus B/A deadlock.
            if lock and asset_id == assets[1]["id"]:
                directory_entered.set()
            return value

    async def prepare():
        async with db() as session:
            return await factory(PreparingRepository(session)).prepare(
                OWNER, prepare_session
            )

    async def directory():
        await asyncio.wait_for(prepare_has_first.wait(), 5)
        async with db() as session:
            return await FormalMethodCalls(
                factory(DirectoryRepository(session))
            ).catalog(OWNER, directory_session)

    prepared, listed = await asyncio.wait_for(
        asyncio.gather(prepare(), directory()), 10
    )
    assert prepared["scope_id"]
    assert len(listed["items"]) == 2 and all(
        item["available"] for item in listed["items"]
    )
    assert {item["reference"]["method_id"] for item in listed["items"]} == {
        str(version.reference.method_id) for version in versions
    }
    assert (
        len(remote.calls) == 2
    )  # Discovery/preparation neither dispatches nor spends.
