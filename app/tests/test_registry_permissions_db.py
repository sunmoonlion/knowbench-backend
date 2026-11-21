import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import OWNER, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.ledger import Ledger
from app.bootstrap.workbench import build_runner
from app.domain.workbench.native_permissions import permission_profile
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize(
    "case",
    [
        "accept",
        "decline",
        "network_off",
        "outside",
        "stale",
        "other_device",
        "engine_signal_only",
        "other_turn",
        "timeout",
    ],
)
async def test_exact_turn_permissions_through_runner_and_durable_interaction(
    db, case, monkeypatch
):
    env, sb, sid = await seed(db)
    runner = build_runner(
        db, publisher=RedisPublisher(None, "test"), runner_id="registry-permissions"
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_sessions SET kind='work',thread_id='th' WHERE id=:id"
                ),
                {"id": sid},
            )
            await s.execute(
                text("UPDATE workbench_environments SET status='online' WHERE id=:id"),
                {"id": env},
            )
        link = await runner.link_for(sb, repo)
    client = AsyncMock()
    link.client = client
    link.active_turns["th"] = "turn"
    path = (
        "/home/u/research/proj/report.txt"
        if case != "outside"
        else "/home/u/research/elsewhere"
    )
    fs = {"read": None, "write": [path]}
    req = {
        "threadId": "th",
        "turnId": "turn" if case != "other_turn" else "old",
        "itemId": "item",
        "environmentId": runner.environment_key,
        "cwd": "/home/u/research/proj",
        "permissions": {
            "fileSystem": fs,
            "network": {"enabled": True} if case == "network_off" else None,
        },
    }
    if case == "timeout":
        monkeypatch.setattr(
            link, "_wait_answer", AsyncMock(return_value=(None, "timeout"))
        )
    work = asyncio.create_task(
        link.on_server_request(1, "item/permissions/requestApproval", req)
    )
    try:
        if case not in ("outside", "network_off", "other_turn", "timeout"):
            async with asyncio.timeout(5):
                while not link.pending_approvals:  # noqa: ASYNC110 (observe the live runner request)
                    await asyncio.sleep(0.01)
            async with db() as s:
                repo = WorkbenchRepository(s)
                item = (await repo.list_interactions(session_id=sid, status="pending"))[
                    0
                ]
                assert item["prompt"]["subject"]["permissions"] == {"fileSystem": fs}
                assert {o["id"] for o in item["prompt"]["options"]} == {
                    "accept",
                    "decline",
                }
                opened = next(
                    e
                    for e in await repo.list_events(session_id=sid)
                    if e["type"] == "interaction/opened"
                )
                answer = {"decision": "decline" if case == "decline" else "accept"}
                if case != "engine_signal_only":
                    await Ledger(repo).respond_interaction(
                        str(item["id"]),
                        token=opened["payload"]["token"],
                        response=answer,
                        owner_actor_id=OWNER,
                    )
                if case == "stale":
                    link.active_turns["th"] = "new"
                if case == "other_device":
                    async with repo.transaction():
                        await s.execute(
                            text(
                                "UPDATE workbench_environments SET host='changed-host' WHERE id=:id"
                            ),
                            {"id": env},
                        )
                assert link.resolve_approval(
                    item["prompt"]["subject"]["request_id"], answer
                )
        await asyncio.wait_for(work, 5)
        client.respond.assert_awaited_once_with(
            1,
            {
                "permissions": {"fileSystem": fs} if case == "accept" else {},
                "scope": "turn",
            },
        )
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


def test_permission_profile_obeys_local_ceiling_and_concrete_paths():
    env = {
        "roots": ["/work"],
        "status": "online",
        "ceiling": {"sandbox": "read-only", "network": True},
    }
    network = {"network": {"enabled": True}}
    assert permission_profile(network, "/work/p", env) == network
    for path in ["/work/p/../a", "/work/p/*", "/other", "relative"]:
        assert (
            permission_profile({"fileSystem": {"read": [path]}}, "/work/p", env) is None
        )
    assert (
        permission_profile({"fileSystem": {"write": ["/work/p/doc"]}}, "/work/p", env)
        is None
    )
    assert (
        permission_profile(
            {
                "fileSystem": {
                    "entries": [
                        {"path": {"type": "special", "value": "root"}, "access": "read"}
                    ]
                }
            },
            "/work/p",
            env,
        )
        is None
    )
