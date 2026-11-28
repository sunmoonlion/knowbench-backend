import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.encoders import jsonable_encoder
from test_knowbot_test_runs_db import (
    AUTHOR,
    TrialAppServer,
    accept,
    request,
    runner_for,
    setup_test_computer,
)
from test_workbench_ledger_db import db as db
from test_workbench_task_runtime_db import ANSWER, PLAN, close, drive

from app.application.workbench.test_execution import execution_view
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import NotFound
from app.infrastructure.workbench.repository import WorkbenchRepository


def validate(view):
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads(
        (parent / "contracts/knowlab-test-execution-lock.json").read_text()
    )
    schema = parent.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    jsonschema.validate(jsonable_encoder(view), json.loads(schema.read_text()))


async def test_process_is_bound_to_owner_run_and_case_and_reuses_archived_outputs(db):
    fake = TrialAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    run_id = "knowlab-live-view-0001"
    try:
        await setup_test_computer(db, fake)
        await accept(db, request(run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            waiting = await execution_view(repo, run_id, owner_subject=AUTHOR)
            validate(waiting)
            assert waiting["cases"][0]["state"] == "PENDING"
            with pytest.raises(NotFound):
                await execution_view(repo, run_id, owner_subject="another-author")
            with pytest.raises(NotFound):
                await execution_view(
                    repo, run_id, owner_subject=AUTHOR, case_id="not-this-case"
                )
        conductor = KnowbotTestConductor(workbench_stores(db))
        await conductor.tick()
        async with db() as session:
            live = await execution_view(
                WorkbenchRepository(session), run_id, owner_subject=AUTHOR, case_id="c1"
            )
            validate(live)
            assert live["cases"][0]["steps"] == []  # the planner has not run yet
            assert live["cases"][0]["outputs"]["deliverable"] is None
        await drive(runner)
        await conductor.tick()
        async with db() as session:
            repo = WorkbenchRepository(session)
            final = await execution_view(
                repo, run_id, owner_subject=AUTHOR, case_id="c1"
            )
            validate(final)
            assert final["cases"][0]["state"] == "SUCCEEDED"
            assert len(final["cases"][0]["steps"]) == 2
            assert final["cases"][0]["outputs"] == await repo.get_test_case_outputs(
                run_id, "c1"
            )
            assert "inline:t" not in json.dumps(jsonable_encoder(final))
    finally:
        await close(runner)
        await fake.close()
