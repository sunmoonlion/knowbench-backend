"""网页路由：用依赖覆盖注入 web 身份与测试库；验开关、归属、幂等、命令入队、事件游标。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401

from app.bootstrap.api import create_app
from app.domain.security import Principal
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import require_workbench_enabled
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    get_web_knowbase_token_provider,
)
from core.config import Settings


def principal(actor_id: str) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        actor_type="user",
        subject=f"sub-{actor_id}",
        issuer="https://casdoor.test",
        app="knowbench",
        surface="web",
        audience="web",
        policy_version="knowbench-web-v2",
        actor_id=uuid.UUID(actor_id),
        roles=("user",),
        scopes=frozenset({"web"}),
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
    )


@pytest.fixture
def make_client(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("WORKBENCH_ENABLED", "true")
    app = create_app(Settings())

    async def override_db():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_db
    # get_settings 是缓存的：开关用依赖覆盖，不靠环境变量
    app.dependency_overrides[require_workbench_enabled] = lambda: None

    # 请专家时才换的 knowbase 用户令牌（SDD 0042）：测试里不经 Casdoor，要用的测试自己换掉
    async def no_knowbase_token() -> str:
        raise AssertionError("this test did not expect a knowbase token")

    app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
        no_knowbase_token
    )

    def as_user(actor_id: str):
        app.dependency_overrides[get_web_current_user] = lambda: principal(actor_id)
        return AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        )

    as_user.app = app  # type: ignore[attr-defined]  # 要换别的依赖的测试用
    return as_user


A = str(uuid.uuid4())
B = str(uuid.uuid4())


async def bootstrap(http):
    env = (
        await http.post(
            "/api/workbench/environments",
            json={
                "name": "pc",
                "roots": ["/home/u/research"],
                "codex_version": "0.155.1",
            },
        )
    ).json()["environment_id"]
    sb = (
        await http.post(
            "/api/workbench/sandboxes",
            json={
                "app_server_url": "ws://sandbox:47800",
                "token_ref": "env:APP_SERVER_TOKEN",
            },
        )
    ).json()["sandbox_id"]
    r = await http.post(
        "/api/workbench/sessions",
        json={
            "environment_id": env,
            "sandbox_id": sb,
            "project_root": "/home/u/research/proj",
        },
    )
    assert r.status_code == 201, r.text
    return env, sb, r.json()["session_id"]


async def test_session_turn_handover_and_events(make_client, db):  # noqa: F811
    async with make_client(A) as http:
        env, sb, sid = await bootstrap(http)
        r = await http.get(f"/api/workbench/sessions/{sid}")
        assert r.status_code == 200 and r.json()["session"]["wheel"] == "user"
        r = await http.post(
            f"/api/workbench/sessions/{sid}/turns",
            json={"text": "hello", "request_id": "req-1"},
        )
        assert r.status_code == 202 and r.json()["cursor"] == 2
        r = await http.get(f"/api/workbench/sessions/{sid}/events")
        assert [e["type"] for e in r.json()["events"]] == [
            "session/created",
            "turn/requested",
        ] and r.json()["next_cursor"] == 2
        r = await http.get(f"/api/workbench/sessions/{sid}/events", params={"after": 2})
        assert r.json()["events"] == [] and r.json()["next_cursor"] == 2
        body = {
            "idempotency_key": "k-0000-0001",
            "profile_id": "DATA_QUERY",
            "original_input": {"text": "核对附注"},
            "budget_limit": "10",
        }
        r = await http.post(f"/api/workbench/sessions/{sid}/handover", json=body)
        assert r.status_code == 201 and r.json()["created"] is True
        task_id = r.json()["task_id"]
        r2 = await http.post(f"/api/workbench/sessions/{sid}/handover", json=body)
        assert r2.json()["task_id"] == task_id and r2.json()["created"] is False
        r3 = await http.post(
            f"/api/workbench/sessions/{sid}/handover",
            json={**body, "original_input": {"text": "别的"}},
        )
        assert r3.status_code == 409 and r3.json()["code"] == "idempotency_conflict"
        r = await http.post(
            f"/api/workbench/sessions/{sid}/turns", json={"text": "sneak"}
        )
        assert r.status_code == 409 and r.json()["code"] == "wheel_held_by_other"
        r = await http.get(f"/api/workbench/tasks/{task_id}")
        assert r.status_code == 200 and r.json()["task"]["state"] == "RECEIVED"
        r = await http.post(f"/api/workbench/tasks/{task_id}/cancel")
        assert r.status_code == 202 and r.json()["state"] == "CANCELLED"
        r = await http.get(f"/api/workbench/sessions/{sid}")
        assert r.json()["session"]["wheel"] == "user"
    async with db() as s:
        rows = await s.execute(
            text(
                "select kind from workbench_commands where session_id = :s order by created_at"
            ),
            {"s": sid},
        )
        assert [x[0] for x in rows.all()] == [
            "session.start_thread",
            "turn.start",
            "task.drive",
        ]


async def test_other_user_sees_nothing(make_client):
    async with make_client(A) as http:
        env, sb, sid = await bootstrap(http)
    async with make_client(B) as http:
        assert (await http.get(f"/api/workbench/sessions/{sid}")).status_code == 404
        assert (
            await http.get(f"/api/workbench/sessions/{sid}/events")
        ).status_code == 404
        assert (
            await http.post(f"/api/workbench/sessions/{sid}/turns", json={"text": "x"})
        ).status_code == 404
        assert (await http.get("/api/workbench/sessions")).json()["sessions"] == []


async def test_validation_and_root_whitelist(make_client):
    async with make_client(A) as http:
        r = await http.post(
            "/api/workbench/sandboxes",
            json={"app_server_url": "http://x", "token_ref": "env:T"},
        )
        assert r.status_code == 422
        r = await http.post(
            "/api/workbench/sandboxes",
            json={"app_server_url": "ws://x", "token_ref": "vault:T"},
        )
        assert r.status_code == 422
        env = (
            await http.post(
                "/api/workbench/environments",
                json={"name": "pc", "roots": ["/home/u/research"]},
            )
        ).json()["environment_id"]
        sb = (
            await http.post(
                "/api/workbench/sandboxes",
                json={"app_server_url": "ws://x", "token_ref": "env:T"},
            )
        ).json()["sandbox_id"]
        r = await http.post(
            "/api/workbench/sessions",
            json={"environment_id": env, "sandbox_id": sb, "project_root": "/etc"},
        )
        assert r.status_code == 400 and r.json()["code"] == "root_outside_whitelist"


async def test_disabled_flag_hides_everything(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("WORKBENCH_ENABLED", "false")
    app = create_app(Settings())
    app.dependency_overrides[get_web_current_user] = lambda: principal(A)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        assert (await http.get("/api/workbench/sessions")).status_code == 404


async def test_prefs_flow_into_new_sessions(make_client):
    http = make_client(A)
    r = await http.get("/api/workbench/prefs")
    assert r.status_code == 200 and r.json()["approval_policy"] == "on-request"
    r = await http.put(
        "/api/workbench/prefs", json={"model": "kimi-k3", "approval_policy": "never"}
    )
    assert r.status_code == 200 and r.json()["model"] == "kimi-k3"
    r = await http.put("/api/workbench/prefs", json={"approval_policy": "yolo"})
    assert r.status_code == 422
    env, sb, _first = await bootstrap(http)
    sid = (
        await http.post(
            "/api/workbench/sessions",
            json={
                "environment_id": env,
                "sandbox_id": sb,
                "project_root": "/home/u/research/p",
            },
        )
    ).json()["session_id"]
    view = (await http.get(f"/api/workbench/sessions/{sid}")).json()
    assert view["session"]["thread_settings"] == {
        "approvalPolicy": "never",
        "sandbox": "workspace-write",
        "model": "kimi-k3",
    }


async def test_credentials_never_echo_the_key(make_client, db):  # noqa: F811
    from cryptography.fernet import Fernet

    from app.interfaces.endpoints.workbench_routes import credential_cipher

    http = make_client(A)
    r = await http.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "sk-test-1234567890abcd"},
    )
    assert r.status_code == 503 and r.json()["code"] == "credential_store_unconfigured"

    key = Fernet.generate_key()
    http._transport.app.dependency_overrides[credential_cipher] = lambda: Fernet(key)  # type: ignore[attr-defined]
    r = await http.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "sk-test-1234567890abcd"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["hint"] == "abcd" and "sk-test" not in r.text and "api_key" not in body
    cid = body["id"]
    listed = (await http.get("/api/workbench/credentials")).json()["credentials"]
    assert [c["status"] for c in listed] == ["active"] and "ciphertext" not in listed[0]
    async with db() as s:
        stored = (
            await s.execute(
                text("select ciphertext from workbench_credentials where id = :i"),
                {"i": cid},
            )
        ).scalar_one()
    assert (
        "sk-test" not in stored
        and Fernet(key).decrypt(stored.encode()) == b"sk-test-1234567890abcd"
    )

    # 同一个 app 对象的身份覆盖是全局的：先换成 B 试撤销，再换回 A
    other = make_client(B)
    assert (
        await other.post(f"/api/workbench/credentials/{cid}/revoke")
    ).status_code == 404
    http = make_client(A)
    assert (
        await http.post(f"/api/workbench/credentials/{cid}/revoke")
    ).status_code == 200
    assert (
        await http.post(f"/api/workbench/credentials/{cid}/revoke")
    ).status_code == 404
    listed = (await http.get("/api/workbench/credentials")).json()["credentials"]
    assert listed[0]["status"] == "revoked"


async def test_credentials_take_a_public_https_base_url_only(make_client, db):  # noqa: F811
    from cryptography.fernet import Fernet

    from app.interfaces.endpoints.workbench_routes import credential_cipher

    http = make_client(A)
    http._transport.app.dependency_overrides[credential_cipher] = lambda: Fernet(
        Fernet.generate_key()
    )  # type: ignore[attr-defined]
    for bad in (
        "http://api.example.com/v1",
        "https://localhost/v1",
        "https://10.0.0.8/v1",
    ):
        r = await http.post(
            "/api/workbench/credentials",
            json={
                "provider": "kimi",
                "api_key": "sk-test-1234567890abcd",
                "base_url": bad,
            },
        )
        assert r.status_code == 422, bad
    r = await http.post(
        "/api/workbench/credentials",
        json={
            "provider": "kimi",
            "api_key": "sk-test-1234567890abcd",
            "base_url": "https://proxy.example.com/openai/v1/",
        },
    )
    assert (
        r.status_code == 201
        and r.json()["base_url"] == "https://proxy.example.com/openai/v1"
    )
    listed = (await http.get("/api/workbench/credentials")).json()["credentials"]
    assert listed[0]["base_url"] == "https://proxy.example.com/openai/v1"
    r = await http.post(
        "/api/workbench/credentials",
        json={"provider": "kimi", "api_key": "sk-test-1234567890abcd", "base_url": ""},
    )
    assert r.status_code == 201 and r.json()["base_url"] is None


async def test_prefs_carry_mcp_source_switches(make_client):
    http = make_client(A)
    r = await http.get("/api/workbench/prefs")
    assert r.json()["mcp_sources"] == {}  # knowbase 撤了（数据拓扑）
    # 旧页面还会送 knowbase 的开关：收下但不起作用
    r = await http.put(
        "/api/workbench/prefs",
        json={"approval_policy": "on-request", "mcp_sources": {"knowbase": False}},
    )
    assert r.status_code == 200 and r.json()["mcp_sources"] == {}
    r = await http.put(
        "/api/workbench/prefs", json={"model": "kimi-k3", "approval_policy": "never"}
    )
    assert r.json()["mcp_sources"] == {} and r.json()["model"] == "kimi-k3"
    r = await http.put(
        "/api/workbench/prefs",
        json={"approval_policy": "on-request", "mcp_sources": {"records": True}},
    )
    assert r.status_code == 422


async def test_local_link_reports_the_assigned_link_and_token_expiry(make_client, db):  # noqa: F811
    """账 143：网页打开工作台时问后端「配给当前账号的那份本地 link」的编号乙、连没连着、令牌哪天到期。"""
    import base64
    import json as _json

    from cryptography.fernet import Fernet

    from app.infrastructure.workbench.repository import WorkbenchRepository
    from app.interfaces.endpoints.workbench_routes import credential_cipher

    http = make_client(A)
    key = Fernet.generate_key()
    http._transport.app.dependency_overrides[credential_cipher] = lambda: Fernet(key)  # type: ignore[attr-defined]
    empty = (await http.get("/api/workbench/local-link")).json()
    assert (
        empty["assigned"] is None
        and empty["token_expires_at"] is None
        and empty["copied"] is False
    )

    payload = (
        base64.urlsafe_b64encode(_json.dumps({"exp": 1793836800, "sub": "u"}).encode())
        .decode()
        .rstrip("=")
    )
    token = f"eyJhbGciOiJFUzI1NiJ9.{payload}.sig"
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.put_relay_identity(
                A,
                relay_user="u-a",
                agent_token_ciphertext=Fernet(key).encrypt(token.encode()).decode(),
                sandbox_token_ciphertext="s",
            )
            await repo.report_environment(
                owner_actor_id=A,
                name="pc",
                agent_version="0.5.2",
                codex_version="0.155.1",
                roots=[],
                ceiling={},
                device_id="0f8fad5b-d9cb-469f-a165-70867728950e",
                host="DESKTOP-A",
                os="Windows",
            )
    body = (await http.get("/api/workbench/local-link")).json()
    assert body["assigned"]["device_id"] == "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert body["assigned"]["status"] == "online" and body["copied"] is False
    assert body["token_expires_at"].startswith("2026-11-05")
    assert "agent_token_ciphertext" not in _json.dumps(body)
    # 别人看不到
    other = (await make_client(B).get("/api/workbench/local-link")).json()
    assert other["assigned"] is None
