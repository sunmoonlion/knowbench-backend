"""沙箱按需拉起（0003 D9）：无 key 拒绝；首次签发会合点身份并只回一次代理令牌；供给器与会合点收到正确的东西；状态与回收。"""

from __future__ import annotations

import asyncio

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import A  # noqa: F401
from test_workbench_routes import make_client as make_client

from app.application.workbench.provisioning import relay_user_for
from app.application.workbench.tokens import (
    TokenIssuer,
    generate_private_key_pem,
    jti_of,
    verify,
)
from app.infrastructure.workbench.provisioner import (
    HttpProvisioner,
    ProvisionerConfig,
)
from app.interfaces.endpoints.workbench_routes import (
    credential_cipher,
    provisioning_backends,
    token_issuer,
)


class FakeRelayAdmin:
    def __init__(self):
        self.tokens: dict[str, tuple[str, str]] = {}
        self.revoked: list[str] = []
        self.revoked_jtis: list[str] = []
        self.public_keys: list[str] = []

    async def set_tokens(self, user, agent, sandbox):
        self.tokens[user] = (agent, sandbox)

    async def revoke(self, user):
        self.revoked.append(user)

    async def set_public_key(self, pem):
        self.public_keys.append(pem)

    async def revoke_jti(self, jtis):
        self.revoked_jtis.extend(jtis)


class FakeProvisionerApi:
    """记录收到的 spec；模拟供给器的三个接口。"""

    def __init__(self):
        self.specs: list[dict] = []
        self.token = "cap-" + "t" * 40
        self.present = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer prov-secret"
        user = request.url.path.rsplit("/", 1)[1]
        if request.method == "PUT":
            import json

            self.specs.append(json.loads(request.content))
            created = not self.present
            self.present = True
            return httpx.Response(
                200,
                json={
                    "user": user,
                    "app_server_url": f"ws://sandbox-{user}.sandbox-pool.svc.cluster.local:47800",
                    "app_server_token": self.token,
                    "created": created,
                    "status": "starting",
                    "ready": False,
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": "ready" if self.present else "absent",
                    "ready": self.present,
                },
            )
        if request.method == "DELETE":
            self.present = False
            return httpx.Response(
                200,
                json={
                    "removed": {"deployment": True},
                    "pvc_kept": "purge" not in str(request.url),
                },
            )
        return httpx.Response(405)


def test_relay_user_is_stable_and_dns_safe():
    assert relay_user_for("1BCBD353-8e76-4a04-9e58-cbb13b43cb46") == "u-1bcbd3538e76"
    assert relay_user_for("1BCBD353-8e76-4a04-9e58-cbb13b43cb46") == relay_user_for(
        "1bcbd353-8e76-4a04-9e58-cbb13b43cb46"
    )


async def test_provision_flow(make_client, db):  # noqa: F811
    http = make_client(A)
    app = http._transport.app  # type: ignore[attr-defined]
    key = Fernet(Fernet.generate_key())
    fake_api = FakeProvisionerApi()
    relay = FakeRelayAdmin()
    provisioner = HttpProvisioner(
        ProvisionerConfig(
            url="http://prov",
            token="prov-secret",
            model_provider="kimi",
            model="kimi-k3",
            provider_base_url="https://api.moonshot.cn/v1",
        ),
        transport=httpx.MockTransport(fake_api.handler),
    )
    app.dependency_overrides[credential_cipher] = lambda: key
    app.dependency_overrides[provisioning_backends] = lambda: (
        provisioner,
        relay,
        "wss://edge.test/relay",
    )

    # 没有 key → 409
    r = await http.post("/api/workbench/sandboxes/provision")
    assert r.status_code == 409 and r.json()["code"] == "no_active_credential"
    assert (await http.get("/api/workbench/sandboxes/provisioned")).json()[
        "status"
    ] == "absent"

    # 登记 key 后拉起：首次返回代理令牌；供给器收到解密后的 key 与沙箱令牌；会合点登记了同一对令牌
    assert (
        await http.post(
            "/api/workbench/credentials",
            json={"provider": "kimi", "api_key": "sk-live-key-1234567890"},
        )
    ).status_code == 201
    concurrent = await asyncio.gather(
        http.post("/api/workbench/sandboxes/provision"),
        http.post("/api/workbench/sandboxes/provision"),
    )
    assert sorted(r.status_code for r in concurrent) == [200, 409]
    r = next(r for r in concurrent if r.status_code == 200)
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    relay_user = body["relay"]["user"]
    assert relay_user == relay_user_for(A)
    assert body["relay"]["url"] == "wss://edge.test/relay"
    assert body["relay"]["agent_token"] and len(body["relay"]["agent_token"]) >= 32
    assert body["app_server_url"].startswith("ws://sandbox-" + relay_user)
    assert "app_server_token" not in body and "sk-live" not in r.text
    spec = fake_api.specs[0]
    assert "agent_token_expires_at" not in spec
    assert "identity_revision" not in spec
    assert (
        spec["model_key"] == "sk-live-key-1234567890"
        and spec["relay_user"] == relay_user
        and spec["model_provider"] == "kimi"
    )
    assert relay.tokens[relay_user] == (
        body["relay"]["agent_token"],
        spec["relay_token"],
    )
    assert spec["relay_token"] != body["relay"]["agent_token"]

    # 沙箱登记到了该用户名下。能力令牌存在账里，不给浏览器
    answered = await http.get("/api/workbench/sandboxes")
    listed = answered.json()["sandboxes"]
    mine = [s for s in listed if s["provisioned"]]
    assert len(mine) == 1 and mine[0]["app_server_url"] == body["app_server_url"]
    assert mine[0]["relay_user"] == relay_user
    assert "token_ref" not in mine[0]
    assert fake_api.token not in answered.text and fake_api.token not in r.text
    async with db() as s:
        stored = (
            await s.execute(
                text("select token_ref from workbench_sandboxes where provisioned")
            )
        ).scalar_one()
    assert stored == "inline:" + fake_api.token

    # 再拉一次：不再回代理令牌；沙箱行复用；会合点再次登记同一对令牌
    r2 = await http.post("/api/workbench/sandboxes/provision")
    assert (
        r2.json()["relay"]["agent_token"] is None
        and r2.json()["sandbox_id"] == body["sandbox_id"]
    )
    assert (
        len(fake_api.specs) == 2 and relay.tokens[relay_user][1] == spec["relay_token"]
    )

    # 状态同步到沙箱行
    st = (await http.get("/api/workbench/sandboxes/provisioned")).json()
    assert st["status"] == "ready" and st["sandbox_id"] == body["sandbox_id"]
    assert [
        s["status"]
        for s in (await http.get("/api/workbench/sandboxes")).json()["sandboxes"]
        if s["provisioned"]
    ] == ["ready"]

    # 回收
    gone = (await http.delete("/api/workbench/sandboxes/provisioned")).json()
    assert gone["status"] == "deleted" and gone["pvc_kept"] is True
    assert [
        s["status"]
        for s in (await http.get("/api/workbench/sandboxes")).json()["sandboxes"]
        if s["provisioned"]
    ] == ["deleted"]


async def test_unconfigured_provisioning_is_503(make_client):  # noqa: F811
    http = make_client(A)
    app = http._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[credential_cipher] = lambda: Fernet(Fernet.generate_key())
    app.dependency_overrides.pop(provisioning_backends, None)
    r = await http.post("/api/workbench/sandboxes/provision")
    assert r.status_code == 503 and r.json()["code"] == "provisioning_unavailable"


async def test_jwt_identity_and_rotation(make_client, db):  # noqa: F811
    """D10：配了签名密钥时三种令牌都是可验的 JWT；撤换按 jti 吊销、公钥推到会合点、沙箱在线则滚动。"""
    http = make_client(A)
    app = http._transport.app  # type: ignore[attr-defined]
    key = Fernet(Fernet.generate_key())
    fake_api = FakeProvisionerApi()
    relay = FakeRelayAdmin()
    issuer = TokenIssuer(generate_private_key_pem(), issuer="wb-test")
    provisioner = HttpProvisioner(
        ProvisionerConfig(
            url="http://prov", token="prov-secret", model_provider="kimi"
        ),
        transport=httpx.MockTransport(fake_api.handler),
    )
    app.dependency_overrides[credential_cipher] = lambda: key
    app.dependency_overrides[provisioning_backends] = lambda: (
        provisioner,
        relay,
        "wss://edge.test/relay",
    )
    app.dependency_overrides[token_issuer] = lambda: issuer

    jwks = (await http.get("/api/workbench/token-keys")).json()
    assert jwks["keys"][0]["kid"] == issuer.kid and "d" not in jwks["keys"][0]

    # 没身份时撤换 → 409
    assert (
        await http.post(
            "/api/workbench/sandboxes/relay-identity/rotate",
            json={"expected_revision": "0" * 64},
        )
    ).status_code == 409

    await http.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "sk-live-key-1234567890"},
    )
    body = (await http.post("/api/workbench/sandboxes/provision")).json()
    user = body["relay"]["user"]
    pub = issuer.public_pem()
    agent_claims = verify(
        body["relay"]["agent_token"], pub, audience="relay", issuer="wb-test"
    )
    assert agent_claims["sub"] == user and agent_claims["role"] == "agent"
    spec = fake_api.specs[0]
    assert verify(spec["relay_token"], pub, audience="relay")["role"] == "sandbox"
    # 沙箱不再拿 knowbase 的令牌：平台的数到不了用户的沙箱（数据拓扑，待办账 111）
    assert "knowbase_mcp_token" not in spec
    assert (
        relay.public_keys == [pub]
        and relay.tokens[user][0] == body["relay"]["agent_token"]
    )
    old_jtis = {jti_of(body["relay"]["agent_token"]), jti_of(spec["relay_token"])}

    # 撤换：旧 jti 吊销，新代理令牌回一次，沙箱（在线）用新沙箱令牌滚动
    r = await http.post(
        "/api/workbench/sandboxes/relay-identity/rotate",
        json={"expected_revision": body["relay"]["identity_revision"]},
    )
    assert r.status_code == 200, r.text
    rotated = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert agent_claims["exp"] - agent_claims["iat"] == 30 * 24 * 3600
    assert (
        verify(spec["relay_token"], pub, audience="relay")["exp"]
        - verify(spec["relay_token"], pub, audience="relay")["iat"]
        == 90 * 24 * 3600
    )
    assert body["relay"]["agent_token_expires_at"]
    # Replayed request cannot mint/revoke a second identity.
    again = await http.post(
        "/api/workbench/sandboxes/relay-identity/rotate",
        json={"expected_revision": body["relay"]["identity_revision"]},
    )
    assert again.status_code == 409 and again.json()["code"] == "relay_identity_changed"
    assert again.headers["cache-control"] == "no-store"
    status = (await http.get("/api/workbench/sandboxes/provisioned")).json()
    assert (
        "agent_token" not in status
        and status["identity_revision"] == rotated["relay"]["identity_revision"]
    )

    assert (
        set(relay.revoked_jtis) == old_jtis
        and rotated["revoked"] == 2
        and rotated["sandbox_rolled"] is True
    )
    new_agent = rotated["relay"]["agent_token"]
    assert (
        new_agent != body["relay"]["agent_token"] and jti_of(new_agent) not in old_jtis
    )
    assert relay.tokens[user][0] == new_agent
    assert (
        len(fake_api.specs) == 2
        and fake_api.specs[1]["relay_token"] != spec["relay_token"]
    )
    assert (
        verify(fake_api.specs[1]["relay_token"], pub, audience="relay")["sub"] == user
    )
    assert "sk-live" not in r.text

    # 再拉起：不再回代理令牌，登记的仍是撤换后的那对
    again = (await http.post("/api/workbench/sandboxes/provision")).json()
    assert again["relay"]["agent_token"] is None and relay.tokens[user][0] == new_agent


async def test_global_sandbox_cap(make_client, db):  # noqa: F811
    """F-SBX-08：上限 1。A 拉起后 B 被拒（503 sandbox_capacity_full）；A 再更新不受限；A 回收后 B 能拉起。"""
    from test_workbench_routes import B

    from app.interfaces.endpoints.workbench_routes import sandbox_global_limit

    key = Fernet(Fernet.generate_key())
    fake_api = FakeProvisionerApi()
    relay = FakeRelayAdmin()
    provisioner = HttpProvisioner(
        ProvisionerConfig(
            url="http://prov", token="prov-secret", model_provider="kimi"
        ),
        transport=httpx.MockTransport(fake_api.handler),
    )

    def client_for(actor):
        http = make_client(actor)
        app = http._transport.app  # type: ignore[attr-defined]
        app.dependency_overrides[credential_cipher] = lambda: key
        app.dependency_overrides[provisioning_backends] = lambda: (
            provisioner,
            relay,
            "wss://edge.test/relay",
        )
        app.dependency_overrides[sandbox_global_limit] = lambda: 1
        return http

    a = client_for(A)
    await a.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "sk-live-key-aaaa1111"},
    )
    assert (await a.post("/api/workbench/sandboxes/provision")).status_code == 200

    b = client_for(B)
    await b.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "sk-live-key-bbbb2222"},
    )
    r = await b.post("/api/workbench/sandboxes/provision")
    assert r.status_code == 503 and r.json()["code"] == "sandbox_capacity_full"
    assert "sk-live" not in r.text

    a = client_for(A)
    assert (
        await a.post("/api/workbench/sandboxes/provision")
    ).status_code == 200  # 更新不受限
    assert (await a.delete("/api/workbench/sandboxes/provisioned")).json()[
        "status"
    ] == "deleted"

    b = client_for(B)
    assert (await b.post("/api/workbench/sandboxes/provision")).status_code == 200

    # 0 = 不设上限
    a = client_for(A)
    a._transport.app.dependency_overrides[sandbox_global_limit] = lambda: 0  # type: ignore[attr-defined]
    assert (await a.post("/api/workbench/sandboxes/provision")).status_code == 200


async def test_the_records_tools_are_given_to_the_sandbox_only_when_configured(
    make_client,
    db,  # noqa: F811
    monkeypatch,
):
    """专家读项目记录的工具服务：开关开着、地址配了，供给沙箱时才带上地址与令牌。"""
    from app.interfaces.endpoints import workbench_routes

    http = make_client(A)
    app = http._transport.app  # type: ignore[attr-defined]
    fake_api = FakeProvisionerApi()
    issuer = TokenIssuer(generate_private_key_pem(), issuer="wb-test")
    provisioner = HttpProvisioner(
        ProvisionerConfig(
            url="http://prov", token="prov-secret", model_provider="kimi"
        ),
        transport=httpx.MockTransport(fake_api.handler),
    )
    cipher = Fernet(Fernet.generate_key())
    app.dependency_overrides[credential_cipher] = lambda: cipher
    app.dependency_overrides[provisioning_backends] = lambda: (
        provisioner,
        FakeRelayAdmin(),
        "wss://edge.test/relay",
    )
    app.dependency_overrides[token_issuer] = lambda: issuer
    await http.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "test-model-key-0001"},
    )

    assert workbench_routes.records_mcp_url() == ""  # 默认是关着的
    assert (await http.post("/api/workbench/sandboxes/provision")).status_code == 200
    assert "records_mcp_url" not in fake_api.specs[0]
    assert "records_mcp_token" not in fake_api.specs[0]

    address = "http://knowbench-api.svc/api/mcp/workbench"
    monkeypatch.setattr(workbench_routes, "records_mcp_url", lambda: address)
    assert (await http.post("/api/workbench/sandboxes/provision")).status_code == 200
    spec = fake_api.specs[1]
    assert spec["records_mcp_url"] == address
    assert issuer.owner_of_records_token(spec["records_mcp_token"]) == A
    # 这张令牌只能读记录：拿去当知识服务的、会合点的令牌都验不过
    for audience in ("knowbase", "relay"):
        try:
            verify(spec["records_mcp_token"], issuer.public_pem(), audience=audience)
        except ValueError:
            continue
        raise AssertionError(audience)
    assert "knowbase_mcp_token" not in spec


async def test_identity_operation_rejects_concurrency_and_releases_after_error(db):
    import pytest

    from app.domain.workbench.errors import RelayIdentityBusy
    from app.infrastructure.workbench.repository import WorkbenchRepository

    async with db() as first, db() as second:
        a, b = WorkbenchRepository(first), WorkbenchRepository(second)
        with pytest.raises(ValueError, match="fixture"):
            async with a.relay_identity_operation(A):
                await (
                    a.commit()
                )  # commits during an operation must not release its lock
                with pytest.raises(RelayIdentityBusy):
                    async with b.relay_identity_operation(A):
                        raise AssertionError("concurrent identity operation entered")
                async with b.relay_identity_operation("other-owner"):
                    pass
                raise ValueError("fixture")
        async with b.relay_identity_operation(A):
            pass
