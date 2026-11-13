"""Real ledger: scoped package execution, one approval, revocation and durable outputs."""

import asyncio
import base64
import hashlib
import io
import json
import zipfile

import pytest
from test_capabilities_db import service
from test_workbench_ledger_db import OWNER, seed
from test_workbench_ledger_db import db as db

from app.domain.workbench.capabilities import (
    ActionDecision,
    AssetAction,
    CapabilityError,
    tool_key,
)
from app.domain.workbench.skill_package import SkillPackageCreate
from app.infrastructure.workbench.repository import WorkbenchRepository


class Executor:
    def __init__(self):
        self.started, self.cancelled = [], []
        self.receipt = {"state": "running"}

    async def start(self, owner, run, body):
        self.started.append((owner, run, body))
        return self.receipt

    async def status(self, owner, run):
        return self.receipt

    async def cancel(self, owner, run):
        self.cancelled.append((owner, run))


async def setup(db, *, method=False):
    _, _, sid = await seed(db)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("SKILL.md", "Run scripts/calculate.py")
        z.writestr("scripts/calculate.py", "print(42)")
        z.writestr("references/source.txt", "21")
    executor = Executor()
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        svc.script_executor = executor
        asset = await svc.create_skill(
            OWNER,
            SkillPackageCreate(
                request_id="skill-script-create",
                name="calculate",
                description="Calculate",
                version="1",
                archive_base64=base64.b64encode(archive.getvalue()).decode(),
            ),
            package=True,
        )
        if method:
            from sqlalchemy import text
            from test_workbench_ledger_db import handover

            from app.application.workbench.ledger import Ledger
            from app.domain.workbench.capabilities import SelectionPut, digest
            from app.domain.workbench.packs import SMOKE
            from app.domain.workbench.states import TaskState

            repo = svc.repo
            led = Ledger(repo)
            task_id = (await led.handover(handover(sid), owner_actor_id=OWNER))[
                "task_id"
            ]
            pack = SMOKE.model_dump(mode="json")
            pack["workflow"][0]["managed_capabilities"] = {
                "tools": [],
                "skills": [
                    {
                        "name": asset["name"],
                        "version": "1",
                        "digest": asset["detail"]["digest"],
                    }
                ],
            }
            async with repo.transaction():
                await s.execute(
                    text(
                        "UPDATE workbench_tasks SET pack_source='knowmart', pack_snapshot=cast(:p as jsonb), pack_digest=:d WHERE id=:id"
                    ),
                    {
                        "p": json.dumps(pack),
                        "d": "sha256:" + digest(pack),
                        "id": task_id,
                    },
                )
            selection = await svc.selection(OWNER, sid)
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=selection["revision"],
                    skills=[asset["id"]],
                    method_context=selection["method"]["context"],
                ),
            )
            await led.transition(task_id, TaskState.VALIDATING)
            await led.transition(task_id, TaskState.QUEUED)
            await led.open_attempt(
                task_id, step_id=pack["workflow"][0]["step_id"], step_version="1"
            )
        prepared = await svc.prepare(
            OWNER, sid, scene="method" if method else "conversation"
        )
        cfg = prepared["config"]["mcp_servers.sunmoon_capabilities"]
        token = cfg["http_headers"]["Authorization"].removeprefix("Bearer ")
        key = tool_key(asset["id"], "skill_run")
        assert key in cfg["enabled_tools"]
    return sid, executor, asset, key, token


async def approve(svc, sid, key, token):
    staged = await svc.request_call(
        sid, token, key, {"path": "scripts/calculate.py", "stdin": "21"}
    )
    call = await svc.repo.capability_call(OWNER, staged["call_id"])
    decision = ActionDecision(
        decision="accept",
        token=call["detail"]["approval_token"],
        subject_digest=call["digest"],
    )
    return await svc.decide(OWNER, call["id"], decision), decision


@pytest.mark.parametrize("method", [False, True])
async def test_script_approval_receipt_and_artifact_redaction(db, method):
    sid, executor, asset, key, token = await setup(db, method=method)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        svc.script_executor = executor
        with pytest.raises(CapabilityError):
            await svc.request_call(sid, token, key, {"path": "scripts/invented.py"})
        call, decision = await approve(svc, sid, key, token)
        assert call["status"] == "dispatching" and len(executor.started) == 1
        await svc.decide(OWNER, call["id"], decision)
        assert len(executor.started) == 1
        supplied = executor.started[0][2]
        assert supplied["stdin"] == "21" and len(supplied["files"]) == 3
        executor.receipt = {
            "state": "finished",
            "result": {
                "exit_code": 0,
                "stdout": "42",
                "stderr": "",
                "timed_out": False,
                "output_truncated": False,
                "artifacts": [
                    {
                        "path": "answer.txt",
                        "data_base64": "NDI=",
                        "sha256": hashlib.sha256(b"42").hexdigest(),
                    }
                ],
            },
        }
        await svc.reconcile_scripts()
        stored = await svc.repo.capability_call(OWNER, call["id"])
        assert stored["status"] == "succeeded"
        assert stored["detail"]["result"]["artifacts"][0]["data_base64"] == "NDI="
        public = await svc.calls(OWNER, sid)
        assert public[0]["source_asset_id"] == asset["id"]
        assert public[0]["connection_id"] is None
        assert public[0]["detail"]["connection_id"] is None
        assert "data_base64" not in json.dumps(public, default=str)
        assert public[0]["detail"]["result"]["artifacts"][0]["download_url"].endswith(
            "/artifacts/0"
        )
        assert executor.cancelled == [(OWNER, call["id"])]


@pytest.mark.parametrize("method", [False, True])
async def test_disabled_skill_stops_running_script(db, method):
    sid, executor, asset, key, token = await setup(db, method=method)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        svc.script_executor = executor
        call, _ = await approve(svc, sid, key, token)
        await svc.action(
            OWNER,
            asset["id"],
            AssetAction(action="disable", revision=asset["revision"]),
        )
        await svc.reconcile_scripts()
        assert (await svc.repo.capability_call(OWNER, call["id"]))["status"] == "failed"
        assert executor.cancelled and len(executor.started) == 1


async def test_http_script_waits_for_real_result_and_protects_download(db):
    from httpx import ASGITransport, AsyncClient
    from test_capability_routes_db import application
    from test_workbench_ledger_db import OTHER
    from test_workbench_routes import principal

    from app.interfaces.endpoints import capability_routes as routes
    from app.interfaces.http.middleware.auth import get_web_current_user

    sid, executor, asset, key, token = await setup(db)
    app = application(db, None)

    async def configured():
        async with db() as s:
            svc = service(WorkbenchRepository(s))
            svc.script_executor = executor
            yield svc

    app.dependency_overrides[routes.service] = configured
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        pending = asyncio.create_task(
            http.post(
                f"/api/mcp/capabilities/{sid}",
                headers={"Authorization": "Bearer " + token},
                json={
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": key,
                        "arguments": {"path": "scripts/calculate.py"},
                    },
                },
            )
        )
        try:
            for _ in range(100):
                async with db() as s:
                    calls = await WorkbenchRepository(s).capability_calls(OWNER, sid)
                if calls:
                    break
                await asyncio.sleep(0.02)
            assert calls and not pending.done()
            call = calls[0]
            response = await http.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json={
                    "decision": "accept",
                    "token": call["detail"]["approval_token"],
                    "subject_digest": call["digest"],
                },
            )
            assert (
                response.status_code == 200
                and response.json()["status"] == "dispatching"
            )
            assert not pending.done()
            executor.receipt = {
                "state": "finished",
                "result": {
                    "exit_code": 0,
                    "stdout": "42",
                    "stderr": "",
                    "timed_out": False,
                    "output_truncated": False,
                    "artifacts": [
                        {
                            "path": "answer.txt",
                            "data_base64": "NDI=",
                            "sha256": hashlib.sha256(b"42").hexdigest(),
                        }
                    ],
                },
            }
            async with db() as s:
                svc = service(WorkbenchRepository(s))
                svc.script_executor = executor
                await svc.reconcile_scripts()
            finished = await asyncio.wait_for(pending, 5)
            assert (
                finished.status_code == 200
                and "text/event-stream" in finished.headers["content-type"]
            )
            message = json.loads(
                next(
                    line[6:]
                    for line in finished.text.splitlines()
                    if line.startswith("data: ")
                )
            )
            receipt = json.loads(message["result"]["content"][0]["text"])
            assert (
                receipt["status"] == "succeeded" and "data_base64" not in finished.text
            )
            download = f"/api/workbench/capability-calls/{call['id']}/artifacts/0"
            assert (await http.get(download)).content == b"42"
            import os
            from pathlib import Path

            if os.environ.get("CAPABILITY_UI_OUT"):
                out = Path(os.environ["CAPABILITY_UI_OUT"])
                await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
                for name, path in [
                    ("catalog", "/api/workbench/capabilities"),
                    ("selection", f"/api/workbench/sessions/{sid}/capabilities"),
                    ("calls", f"/api/workbench/sessions/{sid}/capability-calls"),
                ]:
                    captured = await http.get(path)
                    assert captured.status_code == 200
                    (out / (name + ".json")).write_text(captured.text)
                (out / "dispatching.json").write_text(response.text)
            app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
            assert (await http.get(download)).status_code == 404
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_user_can_stop_script_without_relaunch(db):
    sid, executor, asset, key, token = await setup(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        svc.script_executor = executor
        call, _ = await approve(svc, sid, key, token)
        stopped = await svc.cancel_script(OWNER, call["id"])
        assert stopped["status"] == "failed"
        await svc.reconcile_scripts()
        assert (
            executor.cancelled == [(OWNER, call["id"])] and len(executor.started) == 1
        )
