"""运行方的内部接口（SDD 0027 §3.4）：只认 knowlab 的服务身份，契约外的请求 422（真数据库）。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from knowbot_fixtures import manifest
from test_workbench_ledger_db import db as db

from app.bootstrap.api import create_app
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_testrun_service,
)
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints import knowbot_runtime_routes
from core.config import Settings

BASE = "/api/internal/v1/knowbot-runtime/test-runs"


def body(run_id="knowlab-run-0100"):
    return {
        "contract": "knowbot-test-run/v1",
        "run_id": run_id,
        "owner_subject": "user-123",
        "manifest": manifest(),
        "cases": [{"case_id": "c1", "input": {"question": "看看"}}],
        "budget": {"limit": "5"},
        "timeout_seconds": 600,
    }


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


async def test_knowlab_submits_reads_and_answers(client, monkeypatch):
    client.dependency_overrides[require_testrun_service] = lambda: ServiceCaller(
        caller="knowlab",
        principal=None,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        knowbot_runtime_routes,
        "get_settings",
        lambda: Settings(
            KNOWBOT_TEST_SUBJECT="knowbot-tester", KNOWBOT_TEST_WORKSPACE="/workspace"
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
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
