"""One real model protocol turn → gateway → owner approval → shared adapter."""

import asyncio
from decimal import Decimal

import pytest
from formal_model_support import ModelServer, pending_model_call, setup
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import OWNER, approval
from test_workbench_advisor_db import close
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_model_execution import FormalModelExecution
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.interactions import cancel_task
from app.domain.workbench.capability_contract import Call
from app.infrastructure.workbench.repository import WorkbenchRepository


async def drive_to_approval(db, fake, *, decision="accept", before_wait=None):
    request, remote, factory, conductor, runner = await setup(db, fake)
    await conductor.advance(str(request.run_id))
    await runner.run_once()
    task_id, call = await pending_model_call(db, request)
    if call and decision is not None:
        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            response = await http.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call, decision=decision).model_dump(),
            )
        assert response.status_code == 200, response.text
        Call.model_validate(response.json())
    if before_wait:
        await before_wait(task_id)
    await runner.wait_drivers(timeout=30)
    return request, remote, factory, conductor, runner, task_id, call


@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_model_probe_uses_real_ws_http_approval_and_one_billed_turn(db, decision):
    fake, runner = ModelServer(), None
    await fake.start()
    try:
        (
            request,
            remote,
            factory,
            conductor,
            runner,
            task_id,
            call,
        ) = await drive_to_approval(db, fake, decision=decision)
        assert call is not None, fake.failures
        assert len(fake.turn_inputs) == 1
        assert "expected-never-sent" not in fake.turn_inputs[0]
        assert len(remote.calls) == (1 if decision == "accept" else 0)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(task_id)
            attempts = await repo.list_attempts(task_id)
            assert len(attempts) == 1
            attempt = attempts[0]
            assert task["state"] == (
                "SUCCEEDED" if decision == "accept" else "FAILED"
            ), attempt["refs"]
            assert attempt["turn_ids"] == [attempt["refs"]["runtime_turn_id"]]
            assert attempt["refs"]["formal_model_probe"]["usage_known"] is True
            assert (
                Decimal(task["budget"]["used"]) > 0
                and Decimal(task["budget"]["reserved"]) == 0
            )
            current = await repo.capability_call(OWNER, call["id"])
            assert current["detail"]["execution_kind"] == "formal_probe"
            assert "formal_host" not in current["detail"]
        await FormalModelExecution(runner.stores, None, factory).drive(task_id)
        assert len(fake.turn_inputs) == 1 and len(remote.calls) <= 1
        for _ in range(3):
            await conductor.advance(str(request.run_id))
        async with db() as session:
            result = await FormalExecutions(
                WorkbenchRepository(session), factory(WorkbenchRepository(session))
            ).status("author", str(request.run_id), caller="knowlab")
        assert result["status"] == ("passed" if decision == "accept" else "failed"), (
            result
        )
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def test_cancel_then_actual_completion_settles_fee_and_releases_old_turn(db):
    fake, runner = ModelServer(hold_completion=True), None
    await fake.start()

    async def cancel_before_completion(task_id):
        await asyncio.wait_for(fake.action_returned.wait(), 5)
        async with db() as session:
            repo = WorkbenchRepository(session)
            await cancel_task(repo, task_id, owner_actor_id=OWNER)
            task = await repo.get_task(task_id)
            attempt = (await repo.list_attempts(task_id))[0]
            assert attempt["refs"]["usage_pending"]
            assert task["original_input"]["client_context"]["turn_open"]
        fake.finish_allowed.set()

    try:
        _, remote, factory, _, runner, task_id, _ = await drive_to_approval(
            db, fake, before_wait=cancel_before_completion
        )
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(task_id)
            attempt = (await repo.list_attempts(task_id))[0]
            assert task["state"] == "CANCELLED"
            assert "usage_pending" not in attempt["refs"]
            assert attempt["refs"]["formal_model_probe"]["stop_confirmed"] is True
            assert attempt["refs"]["formal_model_probe"]["usage_known"] is True
            assert "turn_open" not in task["original_input"]["client_context"]
            assert Decimal(task["budget"]["reserved"]) == 0
            assert Decimal(task["budget"]["used"]) > 0
        await FormalModelExecution(runner.stores, None, factory).drive(task_id)
        assert len(fake.turn_inputs) == len(remote.calls) == 1
    finally:
        fake.finish_allowed.set()
        if runner:
            await close(runner)
        await fake.close()


async def test_crash_after_claim_never_starts_a_second_turn_and_keeps_unknown_cost(db):
    fake, runner = ModelServer(), None
    await fake.start()
    entered = asyncio.Event()
    calls = []

    class LostDriver:
        async def run_turn(self, *args, **kwargs):
            calls.append((args, kwargs))
            entered.set()
            await asyncio.Event().wait()

    running = None
    try:
        request, remote, factory, conductor, runner = await setup(db, fake)
        await conductor.advance(str(request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = (await repo.list_test_cases(str(request.run_id)))[0]
            task_id = str(row["task_id"])
            talk = await repo.get_session(str(row["session_id"]))
            link = await runner.link_for(str(talk["sandbox_id"]), repo)
        await runner._thread_for_task(task_id, talk, link)
        driver = FormalModelExecution(runner.stores, LostDriver(), factory)
        running = asyncio.create_task(driver.drive(task_id))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        assert await driver.drive(task_id) == "RUNNING"
        assert len(calls) == 1 and not remote.calls
        async with db() as session:
            task = await WorkbenchRepository(session).get_task(task_id)
        _, stopped = await link.stop_and_confirm(task["thread_id"], 0.1)
        assert stopped is True
        assert await driver.recover(task_id, stopped=stopped) == "FAILED"
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(task_id)
            attempt = (await repo.list_attempts(task_id))[0]
            assert attempt["refs"]["usage_pending"]
            assert attempt["refs"]["formal_model_probe"]["stop_confirmed"] is True
            assert attempt["refs"]["formal_model_probe"]["not_started"] is False
            assert Decimal(task["budget"]["reserved"]) > 0
        assert len(calls) == 1
    finally:
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if runner:
            await close(runner)
        await fake.close()


@pytest.mark.parametrize("mode", ["missing_usage", "second_call", "unsafe_startup"])
async def test_model_probe_uncertainty_and_extra_authority_cannot_pass_or_replay(
    db, mode
):
    fake = ModelServer(
        usage=mode != "missing_usage",
        twice=mode == "second_call",
        permissive=mode == "unsafe_startup",
    )
    runner = None
    await fake.start()
    try:
        _, remote, factory, _, runner, task_id, _ = await drive_to_approval(db, fake)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(task_id)
            attempts = await repo.list_attempts(task_id)
            assert task["state"] == "FAILED", attempts
            assert len(remote.calls) <= 1
            if mode == "unsafe_startup":
                assert fake.turn_inputs == [] and remote.calls == []
                assert attempts[0]["refs"]["formal_model_probe"]["not_started"] is True
                assert Decimal(task["budget"]["reserved"]) == 0
            if mode == "missing_usage":
                assert attempts[0]["refs"]["usage_pending"]
                assert Decimal(task["budget"]["reserved"]) > 0
        await FormalModelExecution(runner.stores, None, factory).drive(task_id)
        assert len(fake.turn_inputs) <= 1
    finally:
        if runner:
            await close(runner)
        await fake.close()
