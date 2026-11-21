import base64
from functools import partial
from uuid import uuid4

import pytest
from test_formal_script_execution_db import TARIFF, Executor
from test_instruction_requests_db import PRICES, ROUTE
from test_workbench_ledger_db import OWNER, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.instruction_requests import InstructionRequests
from app.application.workbench.ledger import Ledger
from app.application.workbench.source_tasks import drive_script, preview, submit
from app.bootstrap.workbench import capability_service
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import IdempotencyConflict, NotFound
from app.domain.workbench.registry_sources import SourceDraft
from app.domain.workbench.source_tasks import SourceTaskConfirmation, SourceTaskInput
from app.infrastructure.workbench.repository import WorkbenchRepository


async def setup(db, kind, script="print(1)"):
    _, sandbox, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.set_session_kind(sid, "expert")
            if kind == "instruction":
                await repo.add_credential(
                    owner_actor_id=OWNER,
                    provider=ROUTE.provider,
                    ciphertext="fixture",
                    hint="test",
                    sandbox_id=sandbox,
                    base_url=ROUTE.base_url,
                )
            draft = SourceDraft(
                kind=kind,
                name="run.py" if kind == "script" else "摘要",
                content=script if kind == "script" else "把本次参数概括为一句话。",
            )
            source = await repo.register_direct_revision(OWNER, uuid4(), 1, draft)
    request = SourceTaskInput(
        source_id=source["source_id"],
        version=1,
        digest=source["digest"],
        arguments={"to": "office"},
        budget_limit="1",
    )
    executor = Executor()
    executor.url = TARIFF.provisioner_url

    def service(repo):
        svc = capability_service(repo)
        svc.script_executor = executor
        svc.operations.script_tariff = TARIFF
        svc.instruction_service = partial(
            InstructionRequests.for_account,
            routes=(ROUTE,),
            model=ROUTE.model,
            prices=PRICES,
            default_url="",
        )
        return svc

    return sid, request, service, executor


async def confirm(svc, sid, req):
    snap = await preview(svc, OWNER, sid, req)
    body = SourceTaskConfirmation(
        **req.model_dump(),
        approval_digest=snap.task_columns()["pack_digest"],
        idempotency_key="source-task-01",
    )
    first = await submit(svc, OWNER, sid, body)
    assert (await submit(svc, OWNER, sid, body))["task_id"] == first["task_id"]
    with pytest.raises(IdempotencyConflict):
        await submit(
            svc, OWNER, sid, body.model_copy(update={"arguments": {"to": "another"}})
        )
    return first["task_id"]


async def test_script_uses_confirmed_version_and_recovers_receipt(db):
    sid, req, service, executor = await setup(db, "script")
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        with pytest.raises(NotFound):
            await preview(svc, str(uuid4()), sid, req)
        tid = await confirm(svc, sid, req)
        async with repo.transaction():
            await repo.register_direct_revision(
                OWNER,
                req.source_id,
                2,
                SourceDraft(kind="script", name="run.py", content="print(2)"),
            )
        frames = []

        async def prepare(owner, run, body):
            frames.append(body)

        executor.prepare = prepare
        await drive_script(svc, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        await drive_script(svc, tid)
        await svc.reconcile_scripts()
        await drive_script(svc, tid)
        task = await repo.get_task(tid)
        assert task["state"] == "SUCCEEDED"
        assert len(executor.calls) == len(frames) == 1
        assert len(frames[0]["files"]) == 1
        assert base64.b64decode(frames[0]["files"][0]["data_base64"]) == b"print(1)"
        assert task["budget"]["used"] == "0.040000"
        artifact = await repo.get_artifact(
            task_id=tid, name="registry-result", version=1
        )
        assert artifact["content"] == {"receipt": "office"}
    import json

    from httpx import ASGITransport, AsyncClient
    from test_formal_method_use_routes_db import application

    async with AsyncClient(
        transport=ASGITransport(app=application(db, service)),
        base_url="http://testserver",
    ) as web:
        response = await web.get(
            f"/api/workbench/capability-calls/{executor.calls[0]}/artifacts/0"
        )
        assert response.status_code == 200
        assert json.loads(response.content) == {"receipt": "office"}


async def test_instruction_uses_registered_text_and_revocation_blocks_claim(db):
    sid, req, service, _ = await setup(db, "instruction")
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        tid = await confirm(svc, sid, req)
        ledger = Ledger(repo)
        await ledger.transition(tid, "VALIDATING")
        await ledger.transition(tid, "QUEUED")
        model = await svc.instruction_service(svc.formal_sources, OWNER)
        aid = await model.open(OWNER, tid, caller="registry-source", managed=True)
        task = await repo.get_task(tid)
        quote, job = await model.authority(OWNER, task, "registry-source")
        assert job["input"] == {
            "instructions": "把本次参数概括为一句话。",
            "arguments": {"to": "office"},
        }
        await ledger.request_cancel(tid, owner_actor_id=OWNER)
        with pytest.raises(CapabilityError):
            await model.claim(
                OWNER,
                tid,
                aid,
                caller="registry-source",
                credential_id=quote["credential_id"],
                turn_id="a",
                body={},
            )


async def test_registered_instruction_through_dedicated_runner(db):
    from contextlib import asynccontextmanager

    from test_instruction_runner_db import drive, stack

    from app.domain.workbench.instruction_requests import leaf_responses_body
    from app.domain.workbench.source_tasks import instruction_input

    async def source_case(db):
        sid, req, service, _ = await setup(db, "instruction")

        @asynccontextmanager
        async def host():
            async with db() as session:
                svc = service(WorkbenchRepository(session))
                yield await svc.instruction_service(svc.formal_sources, OWNER)

        async with db() as session:
            repo = WorkbenchRepository(session)
            tid = await confirm(service(repo), sid, req)
            async with repo.transaction():
                await repo.register_direct_revision(
                    OWNER,
                    req.source_id,
                    2,
                    SourceDraft(kind="instruction", name="摘要", content="新版"),
                )
            cred = await repo.model_request_credential(OWNER)
        wire = leaf_responses_body(
            instruction_input("把本次参数概括为一句话。", req.arguments),
            ROUTE.model,
            ROUTE.output_tokens,
        )
        wire["input"] = wire["input"][-1:]
        return host, tid, {"id": str(cred["id"])}, wire

    s = await stack(db, setup_case=source_case, result={"result": "ready"})
    s.units.ready = True
    await drive(s)
    await drive(s)
    async with s.host() as model:
        task = await model.repo.get_task(s.task)
        assert task["state"] == "SUCCEEDED"
        assert len(s.turns) == 1 and len(s.provider_requests) == 1
        artifact = await model.repo.get_artifact(
            task_id=s.task, name="formal-step:direct", version=1
        )
        assert artifact["content"] == {"result": "ready"}


async def test_web_approval_and_changed_authority_between_prepare_and_input(db):
    from httpx import ASGITransport, AsyncClient
    from test_formal_method_use_routes_db import application

    from app.domain.workbench.states import TaskState

    sid, req, service, executor = await setup(db, "script")
    app = application(db, service)
    path = f"/api/workbench/sessions/{sid}/method-skill-center/source-tasks"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as web:
        form = req.model_dump(mode="json")
        quote = await web.post(path + "/preview", json=form)
        assert quote.status_code == 200, quote.text
        body = {
            **form,
            "approval_digest": quote.json()["approval_digest"],
            "idempotency_key": "registered-web-01",
        }
        mismatch = await web.post(path, json={**body, "arguments": {"to": "different"}})
        assert mismatch.status_code >= 400
        made = await web.post(path, json=body)
        assert made.status_code == 200, made.text
        tid = made.json()["task_id"]

        async def cancel_before_input(owner, run, payload):
            async with db() as session:
                await Ledger(WorkbenchRepository(session)).request_cancel(
                    tid, owner_actor_id=owner
                )

        executor.prepare = cancel_before_input
        async with db() as session:
            repo = WorkbenchRepository(session)
            await drive_script(service(repo), tid)
            assert executor.calls == []
            assert (await repo.get_task(tid))["state"] == TaskState.CANCELLED
            assert (await repo.get_task(tid))["active_attempt_id"] is None or (
                await repo.list_attempts(tid)
            )[0]["status"] == "CANCELLED"
            calls = await service(repo).calls(OWNER, sid)
            from app.domain.workbench.capability_contract import Call

            assert (
                Call.model_validate(calls[0]).detail.execution_kind == "registry_script"
            )
        events = await web.get("/api/workbench/method-skill-center/activity")
        assert events.status_code == 200, events.text
        event = next(e for e in events.json() if e["record_id"] == tid)
        assert (
            event["source"]["digest"] == req.digest and event["status"] == "CANCELLED"
        )
        again = await web.post(path, json=body)
        assert again.json()["task_id"] == tid and not again.json()["created"]


@pytest.mark.skipif(
    __import__("os").environ.get("RUN_REGISTRY_SCRIPT_CONTAINER") != "1",
    reason="uses the local isolated script image",
)
async def test_registered_source_executes_in_real_worker(db):
    import asyncio
    import json

    from test_formal_script_execution_db import SOURCE

    from app.domain.workbench.script_contract_generated import (
        ScriptExecution,
        execution_frame,
    )

    sid, req, service, executor = await setup(db, "script", SOURCE)
    adapter_start = executor.start

    async def run_image(owner, run, body):
        frame, raw = execution_frame(ScriptExecution.model_validate(body))
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--pids-limit=64",
            "--tmpfs",
            "/tmp",
            "-i",
            "--entrypoint",
            "/usr/bin/python3",
            "luna-local/script-input:work8",
            "/opt/sandbox/skill_execute.py",
            str(frame.size),
            frame.sha256,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(raw), 60)
        assert proc.returncode == 0, err.decode()
        ack, result = out.decode().splitlines()
        assert ack == "SUNMOON_INPUT_ACCEPTED"
        await adapter_start(
            owner, run, body
        )  # Supplies the controlled metering fixture.
        executor.receipts[run]["result"] = json.loads(result)

    executor.start = run_image
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        tid = await confirm(svc, sid, req)
        await drive_script(svc, tid)
        await svc.reconcile_scripts()
        await drive_script(svc, tid)
        assert (await repo.get_task(tid))["state"] == "SUCCEEDED"
        artifact = await repo.get_artifact(
            task_id=tid, name="registry-result", version=1
        )
        assert artifact["content"] == {"receipt": "office"}


async def test_uncertain_script_is_never_resent_and_late_usage_settles_once(db):
    import asyncio

    from sqlalchemy import text

    sid, req, service, executor = await setup(db, "script")
    start = executor.start

    async def lost_ack(owner, run, body):
        await start(owner, run, body)
        raise OSError("response lost")

    executor.start = lost_ack
    async with db() as session:
        tid = await confirm(service(WorkbenchRepository(session)), sid, req)

    async def resume():
        async with db() as session:
            await drive_script(service(WorkbenchRepository(session)), tid)

    await asyncio.gather(resume(), resume())
    assert len(executor.calls) == 1
    cid = executor.calls[0]
    receipt = executor.receipts.pop(cid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        async with repo.transaction():
            await session.execute(
                text(
                    "update workbench_calls set updated_at=now()-interval '3 minutes' where id=:id"
                ),
                {"id": cid},
            )
        await svc.reconcile_scripts()
        await drive_script(svc, tid)
        assert (await repo.get_task(tid))["state"] == "FAILED"
        assert (await repo.capability_call(OWNER, cid))["status"] == "unknown"
        executor.receipts[cid] = receipt
        await svc.reconcile_scripts()
        await svc.reconcile_scripts()
        await drive_script(svc, tid)
        assert (await repo.get_task(tid))["budget"]["used"] == "0.040000"
        assert (await repo.get_task(tid))["state"] == "FAILED"
    assert executor.calls == [cid]
