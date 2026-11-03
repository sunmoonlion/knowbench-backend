"""工作台的工具服务走 HTTP（PRD/apps/investment.md 7.4）：鉴权、工具清单、调用、拒绝。"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sample_replies import PROFILE, SCOPE
from test_workbench_advisor_db import close
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_records_db import world
from test_workbench_review_db import unbalanced

from app.application.workbench.data_relay import RELAYED
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


def tool(tool_name, **arguments):
    return rpc("tools/call", {"name": tool_name, "arguments": arguments})


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
        specs = {t["name"]: t for t in listed["result"]["tools"]}
        assert specs["find_expert"]["annotations"]["readOnlyHint"] is True
        # Proposing data and recording calculations must not inherit the
        # directory's read-only exemption from runtime approval.
        for name in ("request_data", "compute"):
            assert specs[name].get("annotations", {}).get("readOnlyHint") is not True
        # knowbase 没配：数据工具不列，项目、要数据、查主体与计算照常；v1 的三个读数据
        # 工具（list_my_data、read_my_data、read_my_table）没了（decisions ⑰）
        assert names == [
            "list_project_conversations",
            "read_project_conversation",
            "discover_source_tools",
            "find_subject",
            "request_data",
            "find_expert",
            "compute",
        ]
        # 只读：除了 compute（只记计算记录）与 request_data（只列进用户的确认清单）没有
        # 写的工具（find_subject 只查证券名录，find_expert 只查专家目录）；都要带委托编号
        for spec in listed["result"]["tools"]:
            assert spec["name"].startswith(("list_", "read_")) or spec["name"] in (
                "compute",
                "discover_source_tools",
                "request_data",
                "find_subject",
                "find_expert",
            )
            assert "task" in spec["inputSchema"]["required"]
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


async def test_empty_compute_args_explain_missing_parameters_not_missing_task(served):
    async with client(served) as http:
        reply = (await http.post(URL, json=tool("compute"), headers=bearer())).json()
    assert reply["result"]["isError"] is True
    assert "requires a non-empty task argument" in reply["result"]["content"][0]["text"]


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


async def test_a_result_too_large_for_the_model_is_refused_in_words(
    served, db, monkeypatch
):  # noqa: F811
    """工具结果大过模型一次看得到的上限：不交出去让 Codex 从中间省掉，明说太大、换小一点的读法（待办账 137）。"""
    from app.domain.workbench.projects import THREAD_CONFIG, TOOL_RESULT_MAX_BYTES
    from app.interfaces.mcp import workbench_mcp

    assert THREAD_CONFIG["tool_output_token_limit"] * 4 > TOOL_RESULT_MAX_BYTES
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:

        async def huge(records, name, args, reader=None):
            return {"text": "年" * (TOOL_RESULT_MAX_BYTES // 3 + 1)}

        monkeypatch.setattr(workbench_mcp, "call", huge)
        async with client(served) as http:
            got = await http.post(
                URL,
                json=tool("list_project_conversations", task=ids["task"]),
                headers=bearer(),
            )
        result = got.json()["result"]
        assert result["isError"] is True
        assert "too large to show in one go" in result["content"][0]["text"]
    finally:
        await close(runner)
        await fake.close()


class FakeDataMcp:
    """knowbase 数据 MCP 的替身：记下转来的每一条与令牌。"""

    FACT = "fact:11111111-2222-3333-4444-555555555555"

    def __init__(self) -> None:
        self.seen: list[tuple[dict, dict]] = []

    async def data_mcp(self, token, message):
        import base64

        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        self.seen.append((claims, message))
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "list_inputs",
                            "description": "x",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"run": {"type": "string"}},
                                "required": ["run"],
                            },
                        },
                        {"name": "not_relayed", "inputSchema": {"type": "object"}},
                        *[
                            {"name": name, "inputSchema": {"type": "object"}}
                            for name in sorted(RELAYED - {"list_inputs"})
                        ],
                    ]
                },
            }
        name = message["params"]["name"]
        structured = {"ok": True}
        if name == "read_facts":
            structured = {
                "facts": [{"ref": self.FACT, "value": "1500.000000"}],
                "not_found": [],
            }
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(structured)}],
                "structuredContent": structured,
                "isError": False,
            },
        }


async def test_the_data_tools_are_relayed_to_knowbase(served, db, monkeypatch):  # noqa: F811
    """0048：数据工具照 knowbase 的定义挂出来（run 换成 task），带 task 换成运行授权
    ref，令牌当场签、只许这一个工具；全市场不要授权；没登记会话的对话读不了。"""
    from sqlalchemy import text

    import app.application.workbench.data_relay as relay_module
    import app.interfaces.mcp.workbench_mcp as mcp
    from app.infrastructure.workbench.repository import WorkbenchRepository

    fake_kb = FakeDataMcp()
    monkeypatch.setattr(mcp, "knowbase_v2", lambda: fake_kb)
    relay_module._tools_cache.clear()
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        async with db() as s:
            await s.execute(
                text(
                    """insert into auth_user (id, issuer, subject, roles, scopes)
                       values (:i, 'https://casdoor.test', 'casdoor-alice', '[]', '[]')"""
                ),
                {"i": OWNER},
            )
            await s.commit()
        async with client(served) as http:
            # 没有清单的委托没有运行授权：读不了用户的数据
            bare = await http.post(
                URL, json=tool("list_inputs", task=ids["task"]), headers=bearer()
            )
            assert "has no data" in bare.json()["result"]["content"][0]["text"]
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.insert_task_resolution(
                    {
                        "task_id": ids["task"],
                        "resolution_id": "res-1",
                        "revision": 1,
                        "consumer": "expert:k@1",
                        "params": {},
                        "grant_ref": "knowbench:run:relay-1",
                        "status": "ready",
                    }
                )
        async with client(served) as http:
            listed = (
                await http.post(URL, json=rpc("tools/list"), headers=bearer())
            ).json()
            relayed = {t["name"]: t for t in listed["result"]["tools"]}
            assert "not_relayed" not in relayed
            schema = relayed["list_inputs"]["inputSchema"]
            assert schema["required"] == ["task"] and "run" not in schema["properties"]

            got = await http.post(
                URL, json=tool("list_inputs", task=ids["task"]), headers=bearer()
            )
            assert got.json()["result"]["structuredContent"] == {"ok": True}
            claims, message = fake_kb.seen[-1]
            # 换成请专家时服务端生成的那个 ref（不是客户端的幂等钥匙）
            assert message["params"]["arguments"] == {"run": "knowbench:run:relay-1"}
            assert (claims["aud"], claims["sub"], claims["azp"], claims["tools"]) == (
                "knowbase-data",
                "casdoor-alice",
                "knowbench",
                ["list_inputs"],
            )
            assert claims["sandbox"] == "u-abc" and claims["exp"] - claims["iat"] <= 300

            await http.post(
                URL,
                json=tool("query_facts", scope="market", codes=["IS.REVENUE"]),
                headers=bearer(),
            )
            assert "run" not in fake_kb.seen[-1][1]["params"]["arguments"]

            chat = await http.post(
                URL,
                json=tool("read_input", conversation=ids["chat"], material_id="m"),
                headers=bearer(),
            )
            assert "no data session" in chat.json()["result"]["content"][0]["text"]
            # 专家正在做的那段对话：读数据只认 task，不收 conversation
            async with db() as s, s.begin():
                await s.execute(
                    text(
                        "update workbench_sessions set wheel = 'advisor',"
                        " knowbase_session_id = 'kb-s-9',"
                        " knowbase_session_expires_at = now() + interval '1 hour'"
                        " where id = :s"
                    ),
                    {"s": ids["chat"]},
                )
            held = await http.post(
                URL,
                json=tool("read_input", conversation=ids["chat"], material_id="m"),
                headers=bearer(),
            )
            assert "give the task id" in held.json()["result"]["content"][0]["text"]
            assert fake_kb.seen[-1][1]["params"]["arguments"].get("session") is None
            stranger = await http.post(
                URL,
                json=tool("list_inputs", task=str(uuid.uuid4())),
                headers=bearer(),
            )
            assert stranger.json()["result"]["isError"] is True

            computed = await http.post(
                URL,
                json=rpc(
                    "tools/call",
                    {
                        "name": "compute",
                        "arguments": {
                            "task": ids["task"],
                            "name": "营业收入的两倍",
                            "expression": "x * 2",
                            "inputs": {"x": FakeDataMcp.FACT},
                        },
                    },
                ),
                headers=bearer(),
            )
            result = computed.json()["result"]
            assert result["isError"] is False, result
            assert result["structuredContent"]["value"] == "3000"
            assert fake_kb.seen[-1][1]["params"]["name"] == "read_facts"

            # Regression: real reconciliation calls used A/L/E; keep symbols as written.
            upper = await http.post(
                URL,
                json=tool(
                    "compute",
                    task=ids["task"],
                    name="balance",
                    expression="A - (L + E)",
                    inputs={
                        "A": FakeDataMcp.FACT,
                        "L": FakeDataMcp.FACT,
                        "E": FakeDataMcp.FACT,
                    },
                ),
                headers=bearer(),
            )
            upper_result = upper.json()["result"]
            assert upper_result["isError"] is False, upper_result
            assert upper_result["structuredContent"]["value"] == "-1500"
            assert upper_result["structuredContent"]["formula"] == "A - (L + E)"
            assert fake_kb.seen[-1][1]["params"]["name"] == "read_facts"

            async with db() as s, s.begin():  # 回到没有清单的委托
                await s.execute(text("delete from workbench_task_resolution"))
            asked = await http.post(
                URL,
                json=tool(
                    "request_data",
                    task=ids["task"],
                    needs=[{"doc_type": "annual_report", "reason": "上一年"}],
                ),
                headers=bearer(),
            )
            assert (
                "does not use listed data"
                in asked.json()["result"]["content"][0]["text"]
            )
    finally:
        await close(runner)
        await fake.close()
