"""Admit and register through real execution before testing ordinary session use."""

from sqlalchemy import text
from test_formal_execution_db import approve, settle, status
from test_formal_execution_db import setup as execution_setup
from test_workbench_ledger_db import seed

from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.infrastructure.workbench.repository import WorkbenchRepository


async def admitted(db, *, remote=None, kind="chat", purge=True):
    request, asset, remote, factory, conductor = await execution_setup(
        db, remote=remote
    )
    await conductor.advance(str(request.run_id))
    await approve(db, request, factory, await status(db, request, factory))
    await settle(conductor, request)
    result = await status(db, request, factory)
    assert result["status"] == "passed"
    frozen = FrozenFormalMethod.model_validate(
        {
            "contract": "knowlab/frozen-formal-method/v1",
            "owner_subject": "author",
            "reference": {
                "method_id": request.method_id,
                "version": 1,
                "digest": request.draft.definition.digest(),
            },
            "draft": request.draft,
            "validation": result["admission"]["receipt"],
            "visibility": "private",
        }
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        await factory(repo).formal_sources.register(frozen, caller="knowlab")
        if purge:
            async with repo.transaction():
                await repo.purge_test_run(str(request.run_id))
    _, _, sid = await seed(db, root="/home/u/research/formal-use")
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_sessions SET kind=:kind,thread_id=NULL WHERE id=:id"
                ),
                {"id": sid, "kind": kind},
            )
    remote.calls.clear()
    return frozen, asset, remote, factory, sid
