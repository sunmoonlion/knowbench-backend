"""Author assertions and multi-final evidence follow accepted plan identities."""

import copy
import json

import jsonschema
import pytest
from method_plan_fixtures import proposal
from test_knowbot_test_run_lock import _lock, _schema
from test_method_advisor_db import OTHER_ROWS, ROWS, Driver, patch_catalog
from test_method_plans_db import task
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.advisor import Advisor
from app.application.workbench.deliverable_check import ReadFile
from app.application.workbench.execution_plan import (
    final_results,
)
from app.application.workbench.execution_plan import (
    test_method_results as method_assertion_results,
)
from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.test_runs import Account, KnowbotTestConductor
from app.domain.workbench.method_plan import output_name
from app.domain.workbench.states import TaskState
from app.domain.workbench.test_runs import RunCase, case_result
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

pytestmark = pytest.mark.asyncio


def run_case():
    return RunCase.model_validate(
        {
            "case_id": "c1",
            "input": {"question": "synthetic"},
            "expectations": [
                {
                    "artifact": "inspect",
                    "path": "rows.0.id",
                    "kind": "equals",
                    "value": "two",
                },
                {
                    "artifact": "report",
                    "path": "rows.0.id",
                    "kind": "equals",
                    "value": "two",
                },
            ],
        }
    )


async def exported(db, repo, row, *, reader=None, timed_out=False):
    view = await ExpertDesk(repo).steps(str(row["id"]), owner_actor_id=OWNER)
    out = await KnowbotTestConductor(SqlWorkbenchStores(db), reader=reader)._outputs(
        repo,
        {"run_id": "synthetic-method-test"},
        run_case(),
        row,
        Account(OWNER, "unused", "unused"),
        steps=len(view["steps"]),
        timed_out=timed_out,
    )
    jsonschema.validate(out, _schema(_lock(), "test-run-case-outputs.schema.json"))
    assert out["error"] is None
    return out, view


async def test_assertions_and_exports_use_explicit_final_instance_not_latest(db):
    tid, _ = await task(db)
    await patch_catalog(
        db, tid, lambda d: d["planning"].update(final_units=["inspect", "report"])
    )
    raw = proposal().model_dump(mode="json")
    first = raw["steps"][0]
    first["instance_id"] = "first_inspection"
    second = copy.deepcopy(first)
    second["instance_id"] = "second_inspection"
    raw["steps"].insert(1, second)
    raw["steps"][-1].update(
        instance_id="final_document",
        inputs=[{"slot": "inspect", "instance_id": "second_inspection"}],
    )
    raw["final_outputs"] = {"inspect": "second_inspection", "report": "final_document"}
    assert (
        await Advisor(
            SqlWorkbenchStores(db),
            Driver([json.dumps(raw), ROWS, OTHER_ROWS, OTHER_ROWS]),
        ).drive(tid)
        == TaskState.SUCCEEDED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=output_name("second_inspection"),
                kind="step",
                content=json.loads(ROWS),
            )
        selected = await final_results(repo, row)
        assert set(selected) == {"inspect", "report"}
        assert selected["inspect"]["version"] == 1
        assert selected["inspect"]["content"] == json.loads(OTHER_ROWS)
        out, view = await exported(db, repo, row)
        result = case_result(
            case=run_case(),
            task=row,
            steps=view["steps"],
            artifacts={k: a["content"] for k, a in selected.items()},
        )
        assert result["status"] == "passed" and result["expectations"] == [True, True]
        assert out["plan_version"] == 1
        assert {f["method_id"]: f["instance_id"] for f in out["final_outputs"]} == raw[
            "final_outputs"
        ]
        assert {f["method_id"]: f["result"]["id"] for f in out["final_outputs"]} == {
            name: str(artifact["id"]) for name, artifact in selected.items()
        }
        assert all(s["current"] is True for s in out["steps"])
        # Exercise the real conductor's persistence path, not just its pure predicate.
        async with repo.transaction():
            await repo.insert_test_run(
                {
                    "run_id": "synthetic-method-test",
                    "caller": "knowlab",
                    "owner_subject": "synthetic-author",
                    "digest": "a" * 64,
                    "request": {},
                    "budget_limit": "1",
                    "timeout_seconds": 600,
                    "max_wait_seconds": 600,
                },
                [{"case_id": "c1", "position": 0}],
            )
        run = await repo.get_test_run("synthetic-method-test")
        await KnowbotTestConductor(SqlWorkbenchStores(db))._record(
            repo, run, run_case(), {}, row, Account(OWNER, "unused", "unused")
        )
        persisted = (await repo.list_test_cases(run["run_id"]))[0]
        assert persisted["result"]["status"] == "passed"
        saved_outputs = await repo.get_test_case_outputs(run["run_id"], "c1")
        assert saved_outputs["final_outputs"] == out["final_outputs"]


async def test_rewind_keeps_historical_output_but_invalidates_assertions_and_finals(db):
    tid, _ = await task(db)
    await patch_catalog(
        db, tid, lambda d: d["planning"].update(final_units=["inspect", "report"])
    )
    raw = proposal().model_dump(mode="json")
    raw["final_outputs"]["inspect"] = "inspect_1"
    advisor = Advisor(
        SqlWorkbenchStores(db), Driver([json.dumps(raw), ROWS, "{}", "{}"])
    )
    for _ in range(4):
        assert await advisor._step_once(tid) == TaskState.QUEUED
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert await final_results(repo, row) == {}
        assert await method_assertion_results(repo, row) == {}
        out, view = await exported(db, repo, row)
        assert out["steps"][0]["outcome"] == "accepted"
        assert out["steps"][0]["artifact"] == json.loads(ROWS)
        assert all(s["current"] is False for s in out["steps"])
        assert all(
            f["result"] is None and "artifact" not in f for f in out["final_outputs"]
        )
        result = case_result(
            case=run_case(), task=row, steps=view["steps"], artifacts={}
        )
        assert result["expectations"] == [False, False]
        assert result["steps"][0]["accepted"] is False


async def test_each_final_file_is_read_in_the_owner_workspace_and_timeout_is_explicit(
    db,
):
    tid, _ = await task(db)

    def files(d):
        d["planning"]["final_units"] = ["inspect", "report"]
        for unit in d["method_units"]:
            if unit["contract"]["step_id"] in ("inspect", "report"):
                unit["contract"]["output_file"] = unit["contract"]["step_id"] + ".md"

    await patch_catalog(db, tid, files)
    raw = proposal().model_dump(mode="json")
    raw["final_outputs"]["inspect"] = "inspect_1"

    class Files:
        async def exists(self, **kwargs):
            return True

        async def digest(self, **kwargs):
            return "a" * 64

    class Reader:
        def __init__(self):
            self.calls = []

        async def read(self, **kwargs):
            self.calls.append(kwargs)
            return ReadFile(text=kwargs["path"], mtime_ms=None)

    assert (
        await Advisor(
            SqlWorkbenchStores(db),
            Driver([json.dumps(raw), ROWS, ROWS]),
            deliverables=Files(),
        ).drive(tid)
        == TaskState.SUCCEEDED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        reader = Reader()
        out, _ = await exported(db, repo, row, reader=reader, timed_out=True)
        assert len(reader.calls) == 2 and all(c["owner"] == OWNER for c in reader.calls)
        accepted = await final_results(repo, row)
        assert {
            f["method_id"]: f["deliverable"]["path"] for f in out["final_outputs"]
        } == {name: a["workspace_path"] for name, a in accepted.items()}
        session_row = await repo.get_session(str(row["session_id"]))
        assert all(c["directory"] == session_row["project_root"] for c in reader.calls)
        assert all(
            f["deliverable"]["step_id"] == f["instance_id"]
            for f in out["final_outputs"]
        )
        assert all(
            f["note"] == "超时收尾时取的，可能没写完" for f in out["final_outputs"]
        )
        assert out["deliverable"] is None


async def test_unplanned_task_exports_no_invented_final_result(db):
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert await final_results(repo, row) == {}
        out, _ = await exported(db, repo, row)
        assert out["steps"] == [] and "final_outputs" not in out
        assert "plan_version" not in out


async def test_v2_collector_checks_current_intermediate_output_without_promoting_it_to_final(
    db,
):
    tid, _ = await task(db)
    plan = proposal().model_dump(mode="json")
    assert (
        await Advisor(
            SqlWorkbenchStores(db), Driver([json.dumps(plan), ROWS, ROWS])
        ).drive(tid)
        == TaskState.SUCCEEDED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=output_name("inspect_1"),
                kind="step",
                content=json.loads(OTHER_ROWS),
            )
            await repo.insert_test_run(
                {
                    "run_id": "v2-intermediate-case",
                    "caller": "knowlab",
                    "owner_subject": "synthetic-author",
                    "digest": "b" * 64,
                    "request": {"contract": "knowbot-test-run/v2"},
                    "budget_limit": "1",
                    "timeout_seconds": 600,
                    "max_wait_seconds": 600,
                },
                [{"case_id": "c1", "position": 0}],
            )
        selected = await method_assertion_results(repo, row)
        assert set(selected) == {"inspect", "report"}
        assert selected["inspect"]["content"] == json.loads(ROWS)
        assert set(await final_results(repo, row)) == {"report"}
        case = run_case().model_dump(mode="json")
        for e in case["expectations"]:
            e["value"] = "one"
        # An assertion cannot force the optional review to run or pass by absence.
        case["expectations"].append(
            {
                "artifact": "review",
                "path": "notes",
                "kind": "contains",
                "value": "checked",
            }
        )
        run = await repo.get_test_run("v2-intermediate-case")
        await KnowbotTestConductor(SqlWorkbenchStores(db))._record(
            repo,
            run,
            RunCase.model_validate(case),
            {},
            row,
            Account(OWNER, "unused", "unused"),
        )
        result = (await repo.list_test_cases(run["run_id"]))[0]["result"]
        assert result["expectations"] == [True, True, False]
        assert result["status"] == "failed"
        saved = await repo.get_test_case_outputs(run["run_id"], "c1")
        assert [x["method_id"] for x in saved["final_outputs"]] == ["report"]
