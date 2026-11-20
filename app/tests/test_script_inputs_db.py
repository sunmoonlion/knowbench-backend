"""User approval, live resource bindings and input handoff use the real call ledger."""

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_capabilities_db import approval, service
from test_skill_execution_db import setup
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.infrastructure.workbench.repository import WorkbenchRepository


class Materials:
    def __init__(self, material, session):
        self.material = material
        self.session = session
        self.page_chars = 60000
        self.allowed = True
        self.content = "经批准的正文" * 14000
        self.calls = []

    def data_token(self, subject, *, sandbox, tools):
        assert (
            subject == "script-reader"
            and sandbox == "skill-input"
            and tools == ["read_input"]
        )
        return SimpleNamespace(token="synthetic-read-only")

    async def data_mcp(self, token, request):
        args = request["params"]["arguments"]
        assert token == "synthetic-read-only" and args["session"] == self.session
        assert args["material_id"] == self.material and args["version_no"] == 1
        self.calls.append(args["offset"])
        if not self.allowed:
            return {"result": {"isError": True}}
        start = args["offset"]
        part = self.content[start : start + self.page_chars]
        return {
            "result": {
                "isError": False,
                "structuredContent": {
                    "source": {
                        "material_id": self.material,
                        "version_no": 1,
                        "reading": "text/3",
                    },
                    "offset": start,
                    "text": part,
                    "more": start + len(part) < len(self.content),
                },
            }
        }


@pytest.mark.parametrize(
    "change", ["none", "revoked", "replaced", "too_big", "lost_call"]
)
async def test_material_is_resolved_only_after_approval_and_rechecked_before_input(
    db, change
):
    sid, executor, _, key, token = await setup(db)
    kb_session, material = str(uuid4()), str(uuid4())
    data = Materials(material, kb_session)
    args = {
        "path": "scripts/calculate.py",
        "inputs": [
            {
                "kind": "material",
                "name": "report.txt",
                "material_id": material,
                "version_no": 1,
                "reading": "text/3",
            }
        ],
    }
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text("""INSERT INTO auth_user (id,issuer,subject,roles,scopes)
                VALUES (:id,'test','script-reader','[]','[]')"""),
                {"id": OWNER},
            )
            await repo.set_knowbase_session(
                sid, kb_session, datetime.now(UTC) + timedelta(minutes=10)
            )
        svc = service(repo)
        svc.script_executor = executor
        svc.script_material_source = (data, data)
        staged = await request_call(svc, sid, token, key, args)
        call = await repo.capability_call(OWNER, staged["call_id"])
        assert not data.calls
        original = data.content
        if change == "too_big":
            data.content = "界" * 180000

        async def prepare(owner, run, body):
            if change == "revoked":
                data.allowed = False
            if change == "replaced":
                data.content = "替换后的正文"
            if change == "lost_call":
                await svc.cancel_script(OWNER, run)

        executor.prepare = prepare
        receipt = await svc.decide(OWNER, call["id"], approval(call))
        if change == "none":
            assert receipt["status"] == "dispatching"
            assert data.calls == [0, 60000, 0, 60000]
            assert len(executor.started) == 1
            (item,) = executor.started[0][2]["inputs"]
            assert (
                item["path"] == "report.txt"
                and base64.b64decode(item["data_base64"]).decode() == original
            )
            await svc.decide(OWNER, call["id"], approval(call))
            assert len(executor.started) == 1
        else:
            assert receipt["status"] == "failed" and not executor.started
        events = [
            row["payload"]
            for row in await repo.list_events(session_id=sid, after_cursor=0, limit=500)
            if row["type"] == "capability/script_inputs_prepared"
        ]
        if change == "none":
            assert events == [
                {
                    "call_id": call["id"],
                    "subject_digest": call["digest"],
                    "files": [
                        {
                            "name": "report.txt",
                            "bytes": len(original.encode()),
                            "sha256": item["sha256"],
                        }
                    ],
                }
            ]
        else:
            assert events == []
        stored = await repo.capability_call(OWNER, call["id"])
        assert material in json.dumps(stored["detail"]["arguments"])
        assert "经批准的正文" not in str(stored) and "data_base64" not in str(stored)


@pytest.mark.parametrize("bound", [True, False])
async def test_real_method_attempt_accepts_only_bound_producer_field(db, bound):
    sid, executor, _, key, token = await setup(db, method=True)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        svc.script_executor = executor
        state = await repo.get_session(sid, owner_actor_id=OWNER)
        task = await repo.get_task(str(state["active_task_id"]))
        task_id = str(task["id"])
        consumer = await repo.get_attempt(str(task["active_attempt_id"]))
        prev = str(uuid4())
        async with repo.transaction():
            await repo.insert_attempt(
                {**consumer, "id": prev, "status": "COMPLETED", "step_id": "previous"}
            )
            art = await repo.put_artifact(
                task_id=task_id,
                attempt_id=prev,
                name="summary",
                kind="step",
                content={"answer": 21, "private": "not selected"},
            )
            pinned = {k: art[k] for k in ("name", "version", "id")}
            await repo.update_attempt(prev, output_artifacts=[pinned])
            if bound:
                await session.execute(
                    text(
                        "UPDATE workbench_attempts SET input_artifact_versions=cast(:v as jsonb) WHERE id=:id"
                    ),
                    {"v": json.dumps([pinned]), "id": consumer["id"]},
                )
        args = {
            "path": "scripts/calculate.py",
            "inputs": [
                {
                    "kind": "step_output",
                    "task_id": task_id,
                    "artifact": "summary",
                    "version": 1,
                    "field": "answer",
                    "name": "previous.json",
                }
            ],
        }
        staged = await request_call(svc, sid, token, key, args)
        call = await repo.capability_call(OWNER, staged["call_id"])
        receipt = await svc.decide(OWNER, call["id"], approval(call))
        if bound:
            assert receipt["status"] == "dispatching"
            (delivered,) = executor.started[0][2]["inputs"]
            assert base64.b64decode(delivered["data_base64"]) == b"21"
        else:
            assert receipt["status"] == "failed" and not executor.started


async def test_smaller_material_pages_use_character_offsets_and_return_complete_utf8():
    from app.application.workbench.script_inputs import material_text

    material = str(uuid4())
    provider = Materials(material, "session")
    provider.page_chars = 1024
    provider.content = "项🙂a" * 30000

    class Reader:
        async def call(self, _, name, args):
            assert name == "read_input"
            return await provider.data_mcp(
                "synthetic-read-only", {"params": {"arguments": args}}
            )

    actual = await material_text(
        Reader(),
        SimpleNamespace(material_id=material, version_no=1, reading="text/3"),
        {"session": "session"},
    )
    assert actual == provider.content.encode()
    assert provider.calls == list(range(0, len(provider.content), provider.page_chars))
