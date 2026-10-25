"""0022 服务流：工作台这头只搬运——签 role=workbench 令牌连会合点 /service，一问一答；拒绝与错误码照搬成 HTTP。"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

import pytest
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import make_client as make_client  # noqa: F401
from websockets.asyncio.server import serve

from app.application.workbench.agent_service import (
    AgentBusy,
    AgentOffline,
    AgentOutdated,
    AgentRefused,
    AgentServiceFailed,
    validate_path,
)
from app.application.workbench.tokens import TokenIssuer
from app.bootstrap.workbench import workbench_store
from app.domain.workbench.errors import WorkbenchError
from app.infrastructure.workbench.agent_service_ws import WsAgentService
from app.interfaces.endpoints.workbench_routes import agent_service

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"


def issuer() -> TokenIssuer:
    from joserfc.jwk import ECKey

    return TokenIssuer(ECKey.generate_key("P-256").as_pem(private=True).decode())


class FakeRelay:
    """只做 /service 这一段：收 hello，按剧本回 reject 或 welcome，再按剧本答那一问。"""

    def __init__(
        self, *, reject: str | None = None, answer: dict[str, Any] | None = None
    ):
        self.reject = reject
        self.answer = answer or {"result": {"ok": True}}
        self.hellos: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    async def handler(self, ws):
        if ws.request.path != "/service":
            await ws.close(code=1008)
            return
        self.hellos.append(json.loads(await ws.recv()))
        if self.reject:
            await ws.send(json.dumps({"type": "reject", "reason": self.reject}))
            await ws.close(code=4001)
            return
        await ws.send(
            json.dumps({"type": "welcome", "conn": "abcd1234", "kind": "service"})
        )
        request = json.loads(await ws.recv())
        self.requests.append(request)
        # 先来一帧不相干的通知，客户端要能跳过
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "noise"}))
        await ws.send(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], **self.answer})
        )
        await ws.wait_closed()

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def claims_of(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


async def test_client_signs_a_workbench_token_and_returns_the_result():
    async with FakeRelay(
        answer={"result": {"roots": ["C:\\r"], "libraryDir": None}}
    ) as relay:
        service = WsAgentService(relay.url, issuer())
        result = await service.call("u-abc", "service/hello")
    assert result == {"roots": ["C:\\r"], "libraryDir": None}
    hello = relay.hellos[0]
    assert (
        hello["role"] == "workbench"
        and hello["user"] == "u-abc"
        and "codex" not in hello
    )
    claims = claims_of(hello["token"])
    assert (
        claims["aud"] == "relay"
        and claims["sub"] == "u-abc"
        and claims["role"] == "workbench"
    )
    assert claims["exp"] - claims["iat"] == 600
    assert relay.requests[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "service/hello",
        "params": {},
    }


@pytest.mark.parametrize(
    "reason,exc",
    [
        ("agent offline", AgentOffline),
        ("agent has no service channel", AgentOutdated),
        ("too many service streams", AgentBusy),
        ("bad token", AgentServiceFailed),
    ],
)
async def test_relay_rejections_become_the_right_errors(reason, exc):
    async with FakeRelay(reject=reason) as relay:
        with pytest.raises(exc):
            await WsAgentService(relay.url, issuer()).call("u-abc", "service/hello")


async def test_agent_errors_keep_their_code():
    async with FakeRelay(
        answer={"error": {"code": -32010, "message": "outside"}}
    ) as relay:
        with pytest.raises(AgentRefused) as caught:
            await WsAgentService(relay.url, issuer()).call(
                "u-abc", "fs/readFile", {"path": "x"}
            )
    assert caught.value.code == "outside_whitelist" and caught.value.http_status == 403
    async with FakeRelay(answer={"error": {"code": -32099, "message": "odd"}}) as relay:
        with pytest.raises(AgentRefused) as caught:
            await WsAgentService(relay.url, issuer()).call(
                "u-abc", "fs/readFile", {"path": "x"}
            )
    assert caught.value.code == "agent_refused" and caught.value.http_status == 502


async def test_unreachable_relay_is_a_502():
    with pytest.raises(AgentServiceFailed):
        await WsAgentService("ws://127.0.0.1:9", issuer()).call(
            "u-abc", "service/hello"
        )


def test_path_validation():
    assert validate_path("C:\\Users\\me\\文件.md") == "C:\\Users\\me\\文件.md"
    for bad in ("", "  ", "a\x00b", "a\nb", "x" * 1025):
        with pytest.raises(WorkbenchError):
            validate_path(bad)


# ---------------- 路由：归属、配对、透传、参数 ----------------
class FakeService:
    def __init__(
        self, result: dict[str, Any] | None = None, raise_: Exception | None = None
    ):
        self.result = result or {"ok": True}
        self.raise_ = raise_
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.waits: list[float | None] = []

    async def call(self, relay_user, method, params=None, *, wait_seconds=None):
        self.calls.append((relay_user, method, params))
        self.waits.append(wait_seconds)
        if self.raise_:
            raise self.raise_
        return self.result


async def paired_environment(http, db, owner: str, *, relay_user: str = "u-aaa") -> str:  # noqa: F811
    r = await http.post(
        "/api/workbench/environments", json={"name": "laptop", "roots": ["C:\\r"]}
    )
    assert r.status_code == 201, r.text
    async with db() as s:
        repo = workbench_store(s)
        async with repo.transaction():
            await repo.put_relay_identity(
                owner,
                relay_user=relay_user,
                agent_token_ciphertext="a",
                sandbox_token_ciphertext="s",
            )
    return r.json()["environment_id"]


def use(http, service):
    http._transport.app.dependency_overrides[agent_service] = lambda: service  # type: ignore[attr-defined]


async def test_service_hello_passes_through_and_checks_ownership(make_client, db):  # noqa: F811
    async with make_client(A) as http:
        env = await paired_environment(http, db, A)
        fake = FakeService({"roots": ["C:\\r"], "capabilities": ["service-v1"]})
        use(http, fake)
        r = await http.get(f"/api/workbench/environments/{env}/service")
        assert r.status_code == 200 and r.json()["roots"] == ["C:\\r"]
        assert fake.calls[-1] == ("u-aaa", "service/hello", None)
        before = len(fake.calls)
    # 别人的电脑：404，也不去问代理
    async with make_client(B) as other:
        use(other, fake)
        r = await other.get(f"/api/workbench/environments/{env}/service")
        assert r.status_code == 404 and len(fake.calls) == before


async def test_local_ticket_is_signed_for_the_owner_only(make_client, db):  # noqa: F811
    from app.interfaces.endpoints.workbench_routes import token_issuer

    signer = issuer()
    async with make_client(A) as http:
        env = await paired_environment(http, db, A, relay_user="u-abc")
        # 没签名私钥：没有票
        r = await http.get(f"/api/workbench/environments/{env}/local-ticket")
        assert r.status_code == 503 and r.json()["code"] == "agent_service_unconfigured"
        http._transport.app.dependency_overrides[token_issuer] = lambda: signer  # type: ignore[attr-defined]
        r = await http.get(f"/api/workbench/environments/{env}/local-ticket")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["relay_user"] == "u-abc"
        claims = claims_of(body["ticket"])
        assert claims["aud"] == "local-agent" and claims["sub"] == "u-abc"
        assert (
            claims["exp"] - claims["iat"] == 600 and body["expires_at"] == claims["exp"]
        )
        assert "role" not in claims
    async with make_client(B) as other:
        other._transport.app.dependency_overrides[token_issuer] = lambda: signer  # type: ignore[attr-defined]
        r = await other.get(f"/api/workbench/environments/{env}/local-ticket")
        assert r.status_code == 404
        # 自己的电脑但还没配对过
        r = await other.post("/api/workbench/environments", json={"name": "pc-b"})
        r = await other.get(
            f"/api/workbench/environments/{r.json()['environment_id']}/local-ticket"
        )
        assert r.status_code == 503 and r.json()["code"] == "agent_offline"


async def test_routes_without_pairing_or_configuration(make_client, db):  # noqa: F811
    async with make_client(A) as http:
        r = await http.post("/api/workbench/environments", json={"name": "laptop"})
        env = r.json()["environment_id"]
        # 没配会合点与私钥：这条路不存在
        r = await http.get(f"/api/workbench/environments/{env}/service")
        assert r.status_code == 503 and r.json()["code"] == "agent_service_unconfigured"
        # 配了，但这个账号没配对过
        fake = FakeService()
        use(http, fake)
        r = await http.get(f"/api/workbench/environments/{env}/service")
        assert (
            r.status_code == 503
            and r.json()["code"] == "agent_offline"
            and fake.calls == []
        )


async def test_routes_translate_agent_refusals(make_client, db):  # noqa: F811
    async with make_client(A) as http:
        env = await paired_environment(http, db, A)
        for exc, status, code in [
            (AgentRefused(-32010, "outside"), 403, "outside_whitelist"),
            (AgentRefused(-32011, "big"), 413, "too_large"),
            (AgentRefused(-32014, "no"), 403, "local_denied"),
            (AgentOutdated("old"), 409, "agent_outdated"),
            (AgentOffline("off"), 503, "agent_offline"),
        ]:
            use(http, FakeService(raise_=exc))
            r = await http.get(f"/api/workbench/environments/{env}/service")
            assert (r.status_code, r.json()["code"]) == (status, code)


async def test_remote_files_pass_through_for_the_owner_only(make_client, db):  # noqa: F811
    """账 165：远程登录 link 的电脑看文件，经服务流转给本地 link；别人的电脑 404、不去问。"""
    async with make_client(A) as http:
        env = await paired_environment(http, db, A)
        fake = FakeService(
            {"entries": [{"name": "a.md", "type": "file"}], "truncated": False}
        )
        use(http, fake)
        base = f"/api/workbench/environments/{env}/remote"
        r = await http.get(f"{base}/fs/dir", params={"path": "C:\\r"})
        assert r.status_code == 200 and r.json()["entries"][0]["name"] == "a.md"
        assert fake.calls[-1] == ("u-aaa", "fs/readDirectory", {"path": "C:\\r"})
        await http.get(f"{base}/fs/meta", params={"path": "C:\\r\\a.md"})
        assert fake.calls[-1][1:] == ("fs/getMetadata", {"path": "C:\\r\\a.md"})
        await http.get(f"{base}/fs/file", params={"path": "C:\\r\\a.md", "offset": 5})
        assert fake.calls[-1][1:] == (
            "fs/readFile",
            {"path": "C:\\r\\a.md", "offset": 5, "length": 512 * 1024},
        )
        r = await http.get(
            f"{base}/fs/file", params={"path": "x", "length": 512 * 1024 + 1}
        )
        assert r.status_code == 422
        before = len(fake.calls)
    async with make_client(B) as other:
        use(other, fake)
        r = await other.get(
            f"/api/workbench/environments/{env}/remote/fs/dir", params={"path": "C:\\r"}
        )
        assert r.status_code == 404 and len(fake.calls) == before


async def test_remote_calls_go_only_to_the_computer_connected_now(make_client, db):  # noqa: F811
    """选的这台此刻没连着（连着的是另一台的本地 link）：不转，回 409（Fable 评审 P2-11）。"""
    async with make_client(A) as http:
        env = await paired_environment(http, db, A)
        async with db() as s:
            repo = workbench_store(s)
            async with repo.transaction():
                await repo.set_environment_status(env, "offline")
        fake = FakeService({"entries": [], "truncated": False})
        use(http, fake)
        r = await http.get(
            f"/api/workbench/environments/{env}/remote/fs/dir", params={"path": "C:\\r"}
        )
        assert (r.status_code, r.json()["code"]) == (409, "machine_not_connected")
        assert fake.calls == []


async def test_remote_projects_folder_and_clone(make_client, db):  # noqa: F811
    """账 165：远程建文件夹、拉仓库。先不带令牌拉；要登录时回 401 auth_required，带上令牌再拉；拉仓库多等一会儿。"""
    from app.application.workbench.agent_service import CLONE_TIMEOUT_SECONDS

    async with make_client(A) as http:
        env = await paired_environment(http, db, A)
        base = f"/api/workbench/environments/{env}/remote/projects"
        fake = FakeService({"path": "C:\\r\\new", "created": True})
        use(http, fake)
        r = await http.post(f"{base}/folder", json={"path": "C:\\r\\new"})
        assert r.status_code == 200 and r.json()["created"] is True
        assert fake.calls[-1][1:] == ("projects/folder", {"path": "C:\\r\\new"})
        assert fake.waits[-1] is None
        use(http, FakeService(raise_=AgentRefused(-32020, "auth_required")))
        r = await http.post(
            f"{base}/clone", json={"url": "https://github.com/a/b", "path": "C:\\r\\b"}
        )
        assert (r.status_code, r.json()["code"]) == (401, "auth_required")
        fake = FakeService({"path": "C:\\r\\b", "branch": "main"})
        use(http, fake)
        r = await http.post(
            f"{base}/clone",
            json={
                "url": "https://github.com/a/b",
                "path": "C:\\r\\b",
                "auth": {"token": "ghp_secret", "remember": True},
            },
        )
        assert r.status_code == 200 and "ghp_secret" not in r.text
        # 记不记住不转给那台电脑：远程的令牌只用这一次（Fable 评审 P1-4）
        assert fake.calls[-1][1:] == (
            "projects/clone",
            {
                "url": "https://github.com/a/b",
                "path": "C:\\r\\b",
                "auth": {"token": "ghp_secret"},
            },
        )
        assert fake.waits[-1] == CLONE_TIMEOUT_SECONDS
        use(http, FakeService(raise_=AgentRefused(-32020, "something new")))
        r = await http.post(f"{base}/folder", json={"path": "C:\\r\\x"})
        assert (r.status_code, r.json()["code"]) == (502, "project_failed")
        r = await http.post(f"{base}/folder", json={"path": "C:\\r\\x", "extra": 1})
        assert r.status_code == 422


def test_uuid_fixture_values_are_valid():
    uuid.UUID(A), uuid.UUID(B)
    assert asyncio.iscoroutinefunction(WsAgentService.call)
