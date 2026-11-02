"""Controlled author intake through the real ledger, runner and result collector.

The author catalog and model replies are synthetic. No task snapshots are patched,
no external model/data calls are made, and production intake stays closed.
"""

import json

import pytest
from method_plan_fixtures import proposal
from test_knowbot_test_runs_db import (
    KnowbotTestConductor,
    KnowbotTestService,
    ScriptedAppServer,
    close,
    drive,
    one_task,
    outputs,
    request,
    runner_for,
    setup_test_computer,
    status,
)
from test_method_advisor_db import ROWS
from test_referenced_method_catalog import manifest
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.bootstrap.workbench import workbench_stores
from app.domain.workbench import knowbots
from app.domain.workbench.errors import KnowbotInvalid
from app.domain.workbench.test_runs import RunRequest
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


def dynamic_request(run_id="dynamic-author-integration"):
    raw = request(run_id).model_dump(mode="json")
    raw["manifest"] = manifest()
    raw["cases"][0]["data"] = {}  # This synthetic document example needs no grant.
    raw["cases"][0]["expectations"] = [
        {"artifact": "report", "path": "rows.0.id", "kind": "equals", "value": "one"}
    ]
    return RunRequest.model_validate(raw)


async def admit(db, req, *, enabled=True):
    async with db() as session:
        return await KnowbotTestService(
            WorkbenchRepository(session),
            dynamic_method_owners=frozenset({req.owner_subject})
            if enabled
            else frozenset(),
        ).accept(req, caller="knowlab")


async def test_default_author_intake_rejects_dynamic_without_persisting_a_run(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        req = dynamic_request()
        with pytest.raises(KnowbotInvalid):
            await admit(db, req, enabled=False)
        async with db() as session:
            assert await WorkbenchRepository(session).get_test_run(req.run_id) is None
        # The ordinary marketplace path is unchanged too.
        with pytest.raises(KnowbotInvalid):
            knowbots.pack_from_manifest(req.manifest, knowbot_id="test", version="1")
    finally:
        await fake.close()


@pytest.mark.parametrize("review", [False, True])
async def test_author_catalog_runs_distinct_plans_through_real_intake_and_runner(
    db, review
):
    plan = proposal(review=review)
    replies = [plan.model_dump_json(), ROWS]
    if review:
        replies.append('{"notes":"checked"}')
    fake = ScriptedAppServer([*replies, ROWS])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        req = dynamic_request()
        accepted = await admit(db, req)
        assert accepted["status"] == "accepted"
        # Closing intake must not break idempotent reads of an already accepted run.
        assert await admit(db, req, enabled=False) == accepted
        conductor = KnowbotTestConductor(workbench_stores(db))
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        done = await status(db, req.run_id)
        assert done["status"] == "passed", done
        assert done["cases"][0]["expectations"] == [True]
        assert len(done["cases"][0]["steps"]) == (3 if review else 2)
        row = await one_task(db)
        assert row["pack_source"] == "knowlab-test"
        assert str(row["owner_actor_id"]) == OWNER
        assert row["pack_digest"] == knowbots.manifest_digest(req.manifest)
        assert row["accepted_plan_version"] == 1
        saved = await outputs(db, req.run_id)
        assert saved["plan_version"] == 1
        [final] = saved["final_outputs"]
        assert final["method_id"] == "report"
        assert final["instance_id"] == "report_1"
        assert final["artifact"] == json.loads(ROWS)
        async with db() as session:
            attempts = await WorkbenchRepository(session).list_attempts(str(row["id"]))
            report = next(a for a in attempts if a["step_id"] == "report_1")
            assert report["step_version"] == "2"
            assert report["refs"]["method_version"] == "2"
            assert report["refs"]["pack_digest"] == row["pack_digest"]
    finally:
        await close(runner)
        await fake.close()


async def test_cancelled_dynamic_author_run_never_starts_a_task(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        req = dynamic_request()
        await admit(db, req)
        async with db() as session:
            service = KnowbotTestService(WorkbenchRepository(session))
            assert (await service.cancel(req.run_id))["status"] == "cancelled"
            assert (await service.cancel(req.run_id))["status"] == "cancelled"
        await KnowbotTestConductor(workbench_stores(db)).tick()
        async with db() as session:
            cases = await WorkbenchRepository(session).list_test_cases(req.run_id)
            assert all(case["task_id"] is None for case in cases)
    finally:
        await fake.close()


@pytest.mark.parametrize("change", ["digest", "definition", "compiler"])
async def test_controlled_intake_still_rejects_tampered_catalog_before_persistence(
    db, change
):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        req = dynamic_request()
        pack = req.manifest["pack"]
        source = pack["method_sources"]["report"]
        if change == "digest":
            source["reference"]["digest"] = "sha256:" + "0" * 64
        elif change == "definition":
            source["definition"]["step"]["does"] = "替换作者方法"
        else:
            pack["method_compiler"] = "unknown"
        with pytest.raises(KnowbotInvalid):
            await admit(db, req)
        async with db() as session:
            assert await WorkbenchRepository(session).get_test_run(req.run_id) is None
    finally:
        await fake.close()
