"""Real WS/HTTP incubation fixture. All model and supplier endpoints are local fakes."""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import CIPHER
from test_formal_execution_db import StructuredRemote
from test_formal_host_calls_db import FORMAL_OPERATION
from test_formal_method_admission_db import setup as admission_setup
from test_formal_method_use_routes_db import application
from test_source_licenses import license_record
from test_workbench_ledger_db import OWNER, seed
from test_workbench_task_runtime_db import ScriptedAppServer
from usage_support import MODEL

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import build_runner, workbench_stores
from app.domain.workbench.formal_execution import FORMAL_RUNTIME
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.formal_model import PROBE_SERVER, PROBE_TOOL
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository


class ModelServer(ScriptedAppServer):
    def __init__(
        self, *, usage=True, twice=False, permissive=False, hold_completion=False
    ):
        super().__init__()
        self.app = None
        self.config = {}
        self.arguments = {"to": "test@example.test"}
        self.tool_name = PROBE_TOOL
        self.repeat_identity = False
        self.thread_configs = {}
        self.responses = []
        self.failures = []
        self.usage, self.twice, self.permissive = usage, twice, permissive
        self.tasks = set()
        self.action_returned = asyncio.Event()
        self.finish_allowed = asyncio.Event()
        if not hold_completion:
            self.finish_allowed.set()

    async def handle(self, ws, msg):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            await self._handle(ws, msg)
        except Exception as exc:
            self.failures.append((msg["method"], type(exc).__name__, str(exc)))
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "error": {"code": -32000, "message": type(exc).__name__},
                    }
                )
            )
        finally:
            self.tasks.discard(task)

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await super().close()

    async def _handle(self, ws, msg):
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]

        async def answer(result):
            await ws.send(json.dumps({"id": rid, "result": result}))

        async def event(name, params):
            await ws.send(json.dumps({"method": name, "params": params}))

        if method == "initialize":
            await answer({"userAgent": "sunmoon-workbench/0.170.0 (Linux) test"})
        elif method == "experimentalFeature/list":
            await answer(
                {
                    "data": [
                        {
                            "name": name,
                            "enabled": self.permissive
                            if name == "multi_agent"
                            else False,
                        }
                        for name in ("multi_agent", "multi_agent_v2", "goals")
                    ]
                }
            )
        elif method == "config/read":
            await answer({"config": {"mcp_servers": {"unrelated": {"enabled": True}}}})
        elif method in ("thread/resume", "thread/read"):
            if method == "thread/resume":
                self.config = self.thread_configs.setdefault(params["threadId"], {})
                self.config.update(params.get("config", {}))
            await answer(
                {
                    "thread": {
                        "id": params["threadId"],
                        "status": {"type": "idle"},
                        "turns": [],
                    },
                    "model": MODEL,
                }
            )
        elif method == "mcpServerStatus/list":
            config = self.thread_configs.get(params["threadId"], {})
            if "mcp_servers." + PROBE_SERVER not in config:
                await answer({"data": [], "nextCursor": None})
                return
            connection = config["mcp_servers." + PROBE_SERVER]
            async with AsyncClient(
                transport=ASGITransport(self.app), base_url="http://testserver"
            ) as http:
                response = await http.post(
                    connection["url"],
                    headers=connection["http_headers"],
                    json={"id": 1, "method": "tools/list"},
                )
            tools = response.json()["result"]["tools"]
            await answer(
                {
                    "data": [
                        {
                            "name": PROBE_SERVER,
                            "runtimeStatus": "connected",
                            "tools": {t["name"]: t for t in tools},
                            "resources": [],
                            "resourceTemplates": [],
                        }
                    ],
                    "nextCursor": None,
                }
            )
        elif method == "turn/start":
            thread, turn = params["threadId"], "turn-" + uuid4().hex
            self.turn_inputs.append(json.dumps(params))
            await answer({"turn": {"id": turn, "threadId": thread}})
            await asyncio.sleep(0.05)
            await event(
                "turn/started",
                {"threadId": thread, "turn": {"id": turn, "status": "inProgress"}},
            )
            connection = self.thread_configs[thread]["mcp_servers." + PROBE_SERVER]
            for index in range(2 if self.twice else 1):
                item = {
                    "id": "candidate-" + str(0 if self.repeat_identity else index),
                    "type": "mcpToolCall",
                    "server": PROBE_SERVER,
                    "tool": self.tool_name,
                    "arguments": self.arguments,
                    "status": "inProgress",
                }
                await event(
                    "item/started", {"threadId": thread, "turnId": turn, "item": item}
                )
                async with AsyncClient(
                    transport=ASGITransport(self.app), base_url="http://testserver"
                ) as http:
                    response = await http.post(
                        connection["url"],
                        headers=connection["http_headers"],
                        json={
                            "id": index + 2,
                            "method": "tools/call",
                            "params": {
                                "name": self.tool_name,
                                "arguments": item["arguments"],
                                "_meta": {
                                    "threadId": thread,
                                    "callId": item["id"],
                                    "x-codex-turn-metadata": {
                                        "thread_id": thread,
                                        "turn_id": turn,
                                    },
                                },
                            },
                        },
                    )
                lines = [
                    line[6:]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                result = json.loads(lines[-1]) if lines else response.json()
                self.responses.append(result)
                await event(
                    "item/completed",
                    {
                        "threadId": thread,
                        "turnId": turn,
                        "item": {
                            **item,
                            "status": "completed",
                            "result": result.get("result"),
                        },
                    },
                )
            self.action_returned.set()
            await self.finish_allowed.wait()
            if self.usage:
                await event(
                    "thread/tokenUsage/updated",
                    {
                        "threadId": thread,
                        "turnId": turn,
                        "tokenUsage": self.meter.report(thread, self.tokens),
                    },
                )
            await event(
                "turn/completed",
                {"threadId": thread, "turn": {"id": turn, "status": "completed"}},
            )
        else:
            await super().handle(ws, msg)


async def setup(db, fake):
    admission, asset = await admission_setup(db, licensed=True)
    env, sandbox, _ = await seed(db)
    doc = admission.model_dump(mode="json", by_alias=True)
    doc["draft"]["allowed_callers"] = ["model"]
    doc["draft"]["definition"]["access_requirements"] = [deepcopy(ACCESS)]
    doc["draft"]["definition"]["procedure"]["implementation"]["requirement_ids"] = [
        ACCESS["requirement_id"]
    ]
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": [
                {
                    "case_id": "model-notice",
                    "caller": "model",
                    "input": {
                        "values": {"to": "test@example.test"},
                        "model_prompt": "Send the supplied notice using the candidate.",
                    },
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {
                                "kind": "equals",
                                "pointer": "/receipt",
                                "value": "expected-never-sent",
                            }
                        ],
                    },
                }
            ],
        }
    ).freeze()
    request = FormalMethodExecutionRequest.model_validate(
        {
            **doc,
            "contract": FORMAL_RUNTIME,
            "execution": {
                "suite": suite.model_dump(mode="json"),
                "model_probe": {"max_turns": 1, "max_method_calls": 1},
                "runner": {
                    "kind": "user",
                    "environment_id": env,
                    "workspace_root": "/home/u/research",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 60,
                "max_wait_seconds": 120,
            },
        }
    )
    record = license_record(request.draft.source.model_dump())
    remote = StructuredRemote()

    class ObservedCapabilities(Capabilities):
        async def prepare(self, *args, **kwargs):
            try:
                return await super().prepare(*args, **kwargs)
            except Exception as exc:
                fake.failures.append(("prepare", type(exc).__name__, str(exc)))
                raise

    def factory(repo):
        return ObservedCapabilities(
            repo,
            remote,
            CIPHER,
            operations=(FORMAL_OPERATION,),
            source_licenses=(record,),
            runtime_url="http://testserver/api/mcp/capabilities",
        )

    conductor, runner = await model_runner(db, fake, request, factory, sandbox)
    return request, remote, factory, conductor, runner


async def model_runner(db, fake, request, factory, sandbox):
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_sandboxes SET app_server_url=:url, token_ref='inline:test' WHERE id=:id"
                ),
                {"url": f"ws://127.0.0.1:{fake.port}", "id": sandbox},
            )
        await FormalExecutions(repo, factory(repo)).accept(request, caller="knowlab")
    runner = build_runner(
        db,
        publisher=RedisPublisher(None, "test"),
        runner_id="formal-probe",
        poll_seconds=0.05,
    )
    runner.capabilities = factory
    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    fake.app = application(db, factory)
    return conductor, runner


async def pending_model_call(db, request):
    call = None
    for _ in range(300):
        async with db() as session:
            repo = WorkbenchRepository(session)
            rows = await repo.list_test_cases(str(request.run_id))
            task_id = str(rows[0]["task_id"])
            attempts = await repo.list_attempts(task_id)
            linked = (
                (attempts[-1].get("refs") or {}).get("capability_call_id")
                if attempts
                else None
            )
            if linked:
                call = await repo.capability_call(OWNER, linked)
                break
            task = await repo.get_task(task_id)
            if task["state"] in ("FAILED", "CANCELLED", "REJECTED"):
                break
        await asyncio.sleep(0.02)
    return task_id, call
