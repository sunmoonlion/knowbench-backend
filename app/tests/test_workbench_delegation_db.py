"""从专家入口请专家，到专家做完（F-PROJ-05）：假 app-server 按脚本回复，真数据库。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from test_fin_review_advisor_db import ALL, say
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.delegation import DelegationService
from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.project_service import ProjectService
from app.bootstrap.workbench import build_runner
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")

WORKSPACE = "/home/u/research"


class Recording(ScriptedAppServer):
    def __init__(self, replies):
        super().__init__(replies)
        self.requests: list[dict] = []

    async def handle(self, ws, msg):
        self.requests.append(msg)
        await super().handle(ws, msg)


async def test_a_new_conversation_gets_its_thread_and_the_expert_finishes(db):
    fake = Recording([say(r) for r in ALL])
    await fake.start()
    runner = None
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                env = await repo.register_environment(
                    owner_actor_id=OWNER,
                    name="pc",
                    agent_version="0.1.0",
                    codex_version="0.155.1",
                    roots=[WORKSPACE],
                    ceiling={},
                )
                await repo.register_sandbox(
                    owner_actor_id=OWNER,
                    app_server_url=f"ws://127.0.0.1:{fake.port}",
                    token_ref="inline:t",
                    codex_version="0.155.1",
                )
            project = await ProjectService(repo).create(
                owner_actor_id=OWNER,
                environment_id=env,
                workspace_root=WORKSPACE,
                path="hengrui",
            )
            made = await DelegationService(repo).from_expert_entry(
                owner_actor_id=OWNER,
                project_id=project["id"],
                profile_id="FIN_REVIEW",
                question="恒瑞医药 2023 到 2025 年的盈利能力怎么样？",
                budget_limit=Decimal("2"),
                idempotency_key="k-delegation-0001",
            )
            before = await repo.get_task(made["task_id"])
            assert before["thread_id"] is None  # 交出去的时候对话还没有线

        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r", poll_seconds=0.05
        )
        await drive(runner)

        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(made["task_id"])
            talk = await repo.get_session(made["session_id"])
            shown = await ExpertDesk(repo).steps(made["task_id"], owner_actor_id=OWNER)
            events = await repo.list_events(session_id=made["session_id"])
        assert task["state"] == "SUCCEEDED"
        assert task["thread_id"] == talk["thread_id"] is not None
        assert talk["wheel"] == "user"  # 做完交回
        assert [s["status"] for s in shown["steps"]] == ["accepted"] * 7

        methods = [r["method"] for r in fake.requests]
        assert methods.count("thread/start") == 1
        assert methods.index("thread/start") < methods.index("turn/start")
        assert methods.count("turn/start") == 7
        turns = [r["params"] for r in fake.requests if r["method"] == "turn/start"]
        assert {t["threadId"] for t in turns} == {talk["thread_id"]}
        for turn in turns:
            assert turn["sandboxPolicy"]["type"] == "workspaceWrite"
            assert turn["cwd"] == "/home/u/research/hengrui"
        announced = [
            e["payload"]["mode"]
            for e in events
            if e["type"] == "session/mode_announced"
        ]
        assert announced == ["expert"]
        # 用户的原话进了第一步
        assert "恒瑞医药" in json.dumps(turns[0]["input"], ensure_ascii=False)
    finally:
        if runner is not None:
            await close(runner)
        await fake.close()
