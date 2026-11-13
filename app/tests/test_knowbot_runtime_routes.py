"""运行方的内部接口（SDD 0027 §3.4）：只认 knowlab 的服务身份，契约外的请求 422（真数据库）。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from knowbot_fixtures import manifest
from sqlalchemy import text
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.bootstrap.api import create_app
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_testrun_service,
)
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.workbench.repository import WorkbenchRepository
from core.config import Settings

BASE = "/api/internal/v1/knowbot-runtime/test-runs"
ENV = "7a1f0c2e-5b6d-4e8f-9a0b-1c2d3e4f5a6b"  # 作者接入的那台电脑
ROOT = "/home/me/work"


@pytest.mark.parametrize("allowed", [False, True])
async def test_dynamic_intake_uses_deployment_owner_list(
    client, db, monkeypatch, allowed
):
    from test_referenced_method_catalog import manifest as dynamic_manifest

    from app.bootstrap import workbench

    client.dependency_overrides[require_testrun_service] = lambda: ServiceCaller(
        caller="knowlab",
        principal=None,  # type: ignore[arg-type]
    )
    config = Settings(
        KNOWBOT_DYNAMIC_TEST_OWNERS=["user-123"] if allowed else ["someone-else"]
    )
    monkeypatch.setattr(workbench, "get_settings", lambda: config)
    await authors_computer(db)
    request = body("dynamic-route") | {"manifest": dynamic_manifest()}
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as http:
        response = await http.post(BASE, json=request)
        assert response.status_code == (202 if allowed else 422), response.text
        if allowed:
            # Closing new intake preserves the already accepted run and its digest.
            monkeypatch.setattr(workbench, "get_settings", lambda: Settings())
            assert (await http.post(BASE, json=request)).json() == response.json()
        else:
            async with db() as session:
                assert (
                    await WorkbenchRepository(session).get_test_run("dynamic-route")
                    is None
                )
        assert (
            await http.post(
                BASE, json=request | {"dynamic_method_owners": ["user-123"]}
            )
        ).status_code == 422


@pytest.mark.parametrize(
    "owners", [["*"], ["user-123", "user-123"], [""], [" user-123"]]
)
def test_dynamic_owner_configuration_rejects_ambiguous_subjects(owners):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(KNOWBOT_DYNAMIC_TEST_OWNERS=owners)


def body(run_id="knowlab-run-0100"):
    return {
        "contract": "knowbot-test-run/v1",
        "run_id": run_id,
        "owner_subject": "user-123",
        "manifest": manifest(),
        "cases": [{"case_id": "c1", "input": {"question": "看看"}}],
        "budget": {"limit": "5"},
        "timeout_seconds": 600,
        "runner": {"kind": "user", "environment_id": ENV, "workspace_root": ROOT},
    }


async def authors_computer(db) -> None:  # noqa: F811
    """作者登录过工作台、接入了一台电脑（白名单里一个根目录）。"""
    async with db() as s:
        await s.execute(
            text(
                """insert into auth_user (id, issuer, subject, roles, scopes)
                   values (:i, 'https://casdoor.test', 'user-123', '[]', '[]')"""
            ),
            {"i": OWNER},
        )
        await s.commit()
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            made = await repo.register_environment(
                owner_actor_id=OWNER,
                name="我的电脑",
                agent_version="0.5.3",
                codex_version="0.155.1",
                roots=[ROOT],
                ceiling={},
            )
            await s.execute(
                text("update workbench_environments set id = :n where id = :o"),
                {"n": ENV, "o": made},
            )


@pytest.fixture
def client(db, monkeypatch):  # noqa: F811
    app = create_app(Settings())

    async def override_db():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_db
    return app


async def test_without_the_relation_configured_everything_is_refused(
    client, monkeypatch
):
    for key in list(__import__("os").environ):
        if key.startswith("SERVICE_AUTH_TESTRUN"):
            monkeypatch.delenv(key)
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        assert (await h.post(BASE, json=body())).status_code == 401  # 没带令牌
        refused = await h.post(BASE, json=body(), headers={"Authorization": "Bearer x"})
        assert refused.status_code == 503
        assert refused.json()["code"] == "service_relation_unconfigured"
    # 公网地址来的：当没有这个接口（审读第 1 条）
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://testserver"
    ) as h:
        hidden = await h.post(BASE, json=body(), headers={"Authorization": "Bearer x"})
        assert hidden.status_code == 404


async def test_knowlab_submits_reads_and_answers(client, db):  # noqa: F811
    client.dependency_overrides[require_testrun_service] = lambda: ServiceCaller(
        caller="knowlab",
        principal=None,  # type: ignore[arg-type]
    )
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        # 都在作者的电脑上跑（decisions ⑯）：没有运行方的不合契约；作者没接入那台电脑 409
        no_runner = {k: v for k, v in body().items() if k != "runner"}
        assert (await h.post(BASE, json=no_runner)).status_code == 422
        not_his = await h.post(BASE, json=body())
        assert not_his.status_code == 409
        assert not_his.json()["code"] == "test_computer_unavailable"
        await authors_computer(db)
        made = await h.post(BASE, json=body())
        assert made.status_code == 202, made.text
        assert made.json()["status"] == "accepted"
        assert made.headers["cache-control"] == "no-store"
        assert (await h.get(f"{BASE}/knowlab-run-0100")).json()["run_id"] == (
            "knowlab-run-0100"
        )
        assert (await h.get(f"{BASE}/knowlab-run-9999")).status_code == 404
        bad = await h.post(BASE, json=body() | {"contract": "other"})
        assert bad.status_code == 422
        not_open = await h.post(
            f"{BASE}/knowlab-run-0100/answers",
            json={
                "contract": "knowbot-test-run/v1",
                "answers": [{"question_id": "q1", "text": "x", "answered_by": "user"}],
            },
        )
        assert not_open.status_code == 409
        assert not_open.json()["code"] == "question_not_open"
        # 一条用例的产出（SDD 0037）：还没跑完就是 404；用例编号不合规则 422
        none_yet = await h.get(f"{BASE}/knowlab-run-0100/cases/c1/outputs")
        assert none_yet.status_code == 404, none_yet.text
        assert (
            await h.get(f"{BASE}/knowlab-run-9999/cases/c1/outputs")
        ).status_code == 404
        assert (
            await h.get(f"{BASE}/knowlab-run-0100/cases/c%201/outputs")
        ).status_code == 422


async def test_knowlab_asks_whether_the_user_has_a_model_key(client, db):  # noqa: F811
    """SDD 0046 第三节「没填不能发起」：没登录过、没填、填了各回什么；撤了的不算。"""
    client.dependency_overrides[require_testrun_service] = lambda: ServiceCaller(
        caller="knowlab",
        principal=None,  # type: ignore[arg-type]
    )
    url = "/api/internal/v1/knowbot-runtime/users/user-123/readiness"
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        assert (await h.get(url)).json() == {"model_key": False}  # 没登录过工作台
        await authors_computer(db)
        got = await h.get(url)
        assert got.json() == {"model_key": False}
        assert got.headers["cache-control"] == "no-store"
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                made = await repo.add_credential(
                    owner_actor_id=OWNER,
                    sandbox_id=None,
                    provider="openai",
                    ciphertext="sealed",
                    hint="sk-…abcd",
                )
        assert (await h.get(url)).json() == {"model_key": True}
        async with db() as s:
            await s.execute(
                text(
                    "update workbench_credentials set status = 'revoked' where id = :i"
                ),
                {"i": made["id"]},
            )
            await s.commit()
        assert (await h.get(url)).json() == {"model_key": False}
        bad = "/api/internal/v1/knowbot-runtime/users/a%20b/readiness"
        assert (await h.get(bad)).status_code == 422


async def test_knowlab_cancels_a_test_and_again_gets_the_same(client, db):  # noqa: F811
    """cancel（10-30 加）：走 testrun 关系；没有这次测试 404；停下记成取消，重复调幂等。"""
    client.dependency_overrides[require_testrun_service] = lambda: ServiceCaller(
        caller="knowlab",
        principal=None,  # type: ignore[arg-type]
    )
    await authors_computer(db)
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        assert (await h.post(f"{BASE}/knowlab-run-9999/cancel")).status_code == 404
        assert (await h.post(BASE, json=body("knowlab-run-0300"))).status_code == 202
        first = await h.post(f"{BASE}/knowlab-run-0300/cancel")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "cancelled"
        assert first.headers["cache-control"] == "no-store"
        again = await h.post(f"{BASE}/knowlab-run-0300/cancel")
        assert again.json() == first.json()
    client.dependency_overrides.pop(require_testrun_service)
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        no_token = await h.post(f"{BASE}/knowlab-run-0300/cancel")
        assert no_token.status_code == 401


async def test_capability_catalog_requires_internal_service_identity(client, db):
    url = "/api/internal/v1/knowbot-runtime/users/user-123/capabilities"
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as http:
        assert (await http.get(url)).status_code in (401, 403)
        client.dependency_overrides[require_testrun_service] = lambda: ServiceCaller(
            caller="knowlab", principal=None
        )
        response = await http.get(url)
        assert response.status_code == 200 and response.json() == {
            "tools": [],
            "skills": [],
        }
        assert response.headers["cache-control"] == "no-store"
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://testserver"
    ) as http:
        assert (await http.get(url)).status_code == 404
