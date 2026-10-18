"""工作台的工具服务走 HTTP（PRD/apps/investment.md 7.4）：鉴权、工具清单、调用、拒绝。"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from test_fin_review_pack import PROFILE, SCOPE
from test_workbench_advisor_db import close
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_records_db import world
from test_workbench_review_db import unbalanced

from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem
from app.bootstrap.api import create_app
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import (
    require_workbench_enabled,
    token_issuer,
)
from app.interfaces.mcp.workbench_mcp import limiter, records_enabled, records_rate
from core.config import Settings

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")

URL = "/api/mcp/workbench"
ISSUER = TokenIssuer(generate_private_key_pem())
SOMEONE_ELSE = TokenIssuer(generate_private_key_pem())


def bearer(owner=OWNER, issuer=ISSUER, **more):
    token = issuer.records_token("u-abc", owner_actor_id=owner, sandbox="u-abc", **more)
    return {"Authorization": f"Bearer {token.token}"}


def rpc(method, params=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}


def tool(name, **arguments):
    return rpc("tools/call", {"name": name, "arguments": arguments})


@pytest.fixture
def served(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("WORKBENCH_ENABLED", "true")
    app = create_app(Settings())

    async def override_db():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_workbench_enabled] = lambda: None
    app.dependency_overrides[token_issuer] = lambda: ISSUER
    app.dependency_overrides[records_enabled] = lambda: True
    app.dependency_overrides[records_rate] = lambda: 60
    limiter.calls.clear()
    return app


def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_the_handshake_and_the_list_of_tools(served):
    async with client(served) as http:
        hello = await http.post(
            URL,
            json=rpc("initialize", {"protocolVersion": "2025-06-18"}),
            headers=bearer(),
        )
        assert hello.status_code == 200
        assert hello.json()["result"]["serverInfo"]["name"] == "sunmoon-workbench"
        assert hello.json()["result"]["protocolVersion"] == "2025-06-18"
        noted = await http.post(
            URL,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=bearer(),
        )
        assert noted.status_code == 202
        listed = (await http.post(URL, json=rpc("tools/list"), headers=bearer())).json()
        names = [t["name"] for t in listed["result"]["tools"]]
        assert names == [
            "list_project_conversations",
            "read_project_conversation",
            "list_library",
            "read_library_item",
            "list_my_data",
            "read_my_data",
            "read_my_table",
            "compute",
        ]
        # 只读：除了 compute（只记计算记录）没有写的工具；项目、用户数据与计算都要带委托编号，
        # 知识库的两个不用（聊天里也能读）
        for spec in listed["result"]["tools"]:
            assert (
                spec["name"].startswith(("list_", "read_")) or spec["name"] == "compute"
            )
            if (
                "project" in spec["name"]
                or "my_" in spec["name"]
                or spec["name"] == "compute"
            ):
                assert "task" in spec["inputSchema"]["required"]
            else:
                assert "task" not in spec["inputSchema"].get("required", [])
        assert (await http.get(URL, headers=bearer())).status_code == 405
        unknown = await http.post(URL, json=rpc("resources/list"), headers=bearer())
        assert unknown.json()["error"]["code"] == -32601


async def test_who_is_let_in(served):
    async with client(served) as http:
        for headers in (
            {},
            {"Authorization": "Bearer not-a-token"},
            {"Authorization": "Basic abc"},
            bearer(issuer=SOMEONE_ELSE),  # 不是我们签的
            bearer(ttl_seconds=-10),  # 过期的
            # 别的用途的令牌（给知识服务的）不能拿来读记录
            {
                "Authorization": f"Bearer {ISSUER.knowbase_token('u-abc', sandbox='u-abc').token}"
            },
        ):
            got = await http.post(URL, json=rpc("tools/list"), headers=headers)
            assert got.status_code == 401, headers
            assert got.headers["www-authenticate"] == "Bearer"
        bad = await http.post(URL, content=b"{not json", headers=bearer())
        assert bad.status_code == 400

    served.dependency_overrides[token_issuer] = lambda: None  # 没配签名密钥
    async with client(served) as http:
        got = await http.post(URL, json=rpc("tools/list"), headers=bearer())
        assert got.status_code == 401

    served.dependency_overrides[token_issuer] = lambda: ISSUER
    served.dependency_overrides[records_enabled] = lambda: False  # 开关关着
    async with client(served) as http:
        got = await http.post(URL, json=rpc("tools/list"), headers=bearer())
        assert got.status_code == 404


async def test_calling_the_tools(served, db):  # noqa: F811
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        async with client(served) as http:
            got = await http.post(
                URL,
                json=tool("list_project_conversations", task=ids["task"]),
                headers=bearer(),
            )
            assert got.status_code == 200
            assert got.headers["cache-control"] == "no-store"
            result = got.json()["result"]
            assert result["isError"] is False
            listed = result["structuredContent"]["conversations"]
            assert len(listed) == 3
            assert (
                json.loads(result["content"][0]["text"]) == result["structuredContent"]
            )

            read = await http.post(
                URL,
                json=tool(
                    "read_project_conversation",
                    task=ids["task"],
                    conversation=ids["chat"],
                ),
                headers=bearer(),
            )
            entries = read.json()["result"]["structuredContent"]["entries"]
            assert entries[0]["said"] == "毛利率是什么"

            # 拒绝的都是给模型看的一句话，不是服务的错
            for call in (
                tool(
                    "read_project_conversation",
                    task=ids["task"],
                    conversation=ids["elsewhere"],
                ),
                tool("list_project_conversations", task=str(uuid.uuid4())),
                tool("list_project_conversations"),
            ):
                refused = (await http.post(URL, json=call, headers=bearer())).json()
                assert refused["result"]["isError"] is True, call
                assert "not found" in refused["result"]["content"][0]["text"]

            # 别的用户的令牌：这个委托对他不存在
            other = await http.post(
                URL,
                json=tool("list_project_conversations", task=ids["task"]),
                headers=bearer(owner=str(uuid.uuid4())),
            )
            assert other.json()["result"]["isError"] is True

            for gone in (
                "delete_project",
                "list_project_dossiers",
                "read_project_dossier",
            ):
                wrong = await http.post(
                    URL, json=tool(gone, task=ids["task"]), headers=bearer()
                )
                assert wrong.json()["error"]["code"] == -32602, gone
            shown = got.text + read.text
            assert "token" not in shown and "Advisor step" not in shown
    finally:
        await close(runner)
        await fake.close()


async def test_too_many_calls_are_slowed_down(served, db):  # noqa: F811
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    served.dependency_overrides[records_rate] = lambda: 3
    try:
        async with client(served) as http:
            answers = []
            for _ in range(5):
                got = await http.post(
                    URL,
                    json=tool("list_project_conversations", task=ids["task"]),
                    headers=bearer(),
                )
                answers.append(got.json()["result"]["isError"])
            assert answers == [False, False, False, True, True]
    finally:
        await close(runner)
        await fake.close()
