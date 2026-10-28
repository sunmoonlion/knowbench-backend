"""孵化的内部接口（专家孵化方案第八节）：只认 knowlab 的 incubate 服务身份，契约外的请求 422
（真数据库）；关系的绑定只许 knowlab，别的调用方配了也不认。"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from test_knowbot_incubations_db import request
from test_knowbot_runtime_routes import ENV, ROOT, authors_computer
from test_knowbot_runtime_routes import client as client  # noqa: F401
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.errors.exceptions import ForbiddenError, UnauthorizedError
from app.domain.workbench.packs import INCUBATE, listed_packs
from app.infrastructure.security.relation_auth import (
    RelationServiceVerifier,
    ServiceCaller,
    relation_bindings,
    require_incubate_service,
    require_testrun_service,
)

BASE = "/api/internal/v1/knowbot-runtime/incubations"


def body(incubation_id: str = "knowlab-inc-0100") -> dict[str, Any]:
    return request(incubation_id, environment_id=ENV, root=ROOT).model_dump(mode="json")


def as_knowlab() -> ServiceCaller:
    return ServiceCaller(caller="knowlab", principal=None)  # type: ignore[arg-type]


async def test_without_the_incubate_relation_everything_is_refused(
    client,  # noqa: F811
    monkeypatch,
):
    for key in list(__import__("os").environ):
        if key.startswith("SERVICE_AUTH_INCUBATE"):
            monkeypatch.delenv(key)
    # 测试的身份开不了孵化的门：两种关系各管各的接口
    client.dependency_overrides[require_testrun_service] = as_knowlab
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        assert (await h.post(BASE, json=body())).status_code == 401  # 没带令牌
        refused = await h.post(BASE, json=body(), headers={"Authorization": "Bearer x"})
        assert refused.status_code == 503
        assert refused.json()["code"] == "service_relation_unconfigured"
        got = await h.get(
            f"{BASE}/knowlab-inc-0100", headers={"Authorization": "Bearer x"}
        )
        assert got.status_code == 503
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://testserver"
    ) as h:  # 公网地址来的：当没有这个接口
        hidden = await h.post(BASE, json=body(), headers={"Authorization": "Bearer x"})
        assert hidden.status_code == 404


async def test_knowlab_submits_reads_and_cancels(client, db):  # noqa: F811
    client.dependency_overrides[require_incubate_service] = as_knowlab
    async with AsyncClient(
        transport=ASGITransport(app=client), base_url="http://127.0.0.1"
    ) as h:
        no_runner = {k: v for k, v in body().items() if k != "runner"}
        bad = await h.post(BASE, json=no_runner)
        assert bad.status_code == 422 and bad.json()["code"] == "incubation_invalid"
        not_his = await h.post(BASE, json=body())  # 作者没接入那台电脑
        assert not_his.status_code == 409
        assert not_his.json()["code"] == "incubation_computer_unavailable"
        await authors_computer(db)
        made = await h.post(BASE, json=body())
        assert made.status_code == 202, made.text
        assert made.json()["status"] == "queued"
        assert made.headers["cache-control"] == "no-store"
        again = await h.post(BASE, json=body())
        assert again.status_code == 202 and again.json() == made.json()
        other = await h.post(BASE, json=body() | {"timeout_seconds": 900})
        assert (
            other.status_code == 409 and other.json()["code"] == "incubation_conflict"
        )
        read = await h.get(f"{BASE}/knowlab-inc-0100")
        assert read.json()["incubation_id"] == "knowlab-inc-0100"
        assert (await h.get(f"{BASE}/knowlab-inc-9999")).status_code == 404
        assert (await h.get(f"{BASE}/bad id")).status_code == 422
        cancelled = await h.post(f"{BASE}/knowlab-inc-0100/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert (await h.post(f"{BASE}/knowlab-inc-9999/cancel")).status_code == 404


def test_the_incubate_relation_binds_knowlab_only() -> None:
    env = {
        "SERVICE_AUTH_INCUBATE_CALLERS": "knowlab,knowmart",
        "SERVICE_AUTH_INCUBATE_KNOWLAB_AUDIENCE": "sunmoon-knowlab-knowbench-incubate",
        "SERVICE_AUTH_INCUBATE_KNOWLAB_SUBJECT_ALLOWLIST": (
            "admin/sunmoonai-knowlab-knowbench-incubate"
        ),
        "SERVICE_AUTH_INCUBATE_KNOWLAB_REQUIRED_SCOPE": "knowbench:incubate",
        "SERVICE_AUTH_INCUBATE_KNOWMART_AUDIENCE": "sunmoon-knowmart-knowbench-incubate",
        "SERVICE_AUTH_INCUBATE_KNOWMART_SUBJECT_ALLOWLIST": "admin/knowmart",
        "SERVICE_AUTH_INCUBATE_KNOWMART_REQUIRED_SCOPE": "knowbench:incubate",
    }
    [binding] = relation_bindings("incubate", env)
    assert binding.caller == "knowlab"
    assert binding.required_scope == "knowbench:incubate"
    # 范围写成测试的不算
    wrong = env | {"SERVICE_AUTH_INCUBATE_KNOWLAB_REQUIRED_SCOPE": "knowbench:testrun"}
    assert relation_bindings("incubate", wrong) == []


class FakeOidc:
    """按受众签的令牌：受众对不上就是 audience_mismatch，其余照令牌里的声明给。"""

    def __init__(self, claims: dict[str, dict[str, Any]]) -> None:
        self.claims = claims

    async def verify_access_token(self, encoded: str, *, audience: str):
        found = self.claims.get(encoded)
        if found is None or found["aud"] != audience:
            raise UnauthorizedError("audience", code="audience_mismatch")
        return found


async def test_only_knowlabs_service_identity_passes() -> None:
    env = {
        "SERVICE_AUTH_INCUBATE_CALLERS": "knowlab,knowmart",
        "SERVICE_AUTH_INCUBATE_KNOWLAB_AUDIENCE": "aud-knowlab",
        "SERVICE_AUTH_INCUBATE_KNOWLAB_SUBJECT_ALLOWLIST": "admin/knowlab-incubate",
        "SERVICE_AUTH_INCUBATE_KNOWLAB_REQUIRED_SCOPE": "knowbench:incubate",
        "SERVICE_AUTH_INCUBATE_KNOWMART_AUDIENCE": "aud-knowmart",
        "SERVICE_AUTH_INCUBATE_KNOWMART_SUBJECT_ALLOWLIST": "admin/knowmart",
        "SERVICE_AUTH_INCUBATE_KNOWMART_REQUIRED_SCOPE": "knowbench:incubate",
    }
    common = {"iss": "https://casdoor.test", "iat": 1, "exp": 4102444800}
    oidc = FakeOidc(
        {
            "knowlab": common
            | {
                "aud": "aud-knowlab",
                "sub": "admin/knowlab-incubate",
                "scope": "knowbench:incubate",
            },
            "knowmart": common
            | {
                "aud": "aud-knowmart",
                "sub": "admin/knowmart",
                "scope": "knowbench:incubate",
            },
            "testrun": common
            | {
                "aud": "aud-knowlab",
                "sub": "admin/knowlab-incubate",
                "scope": "knowbench:testrun",
            },
        }
    )
    verifier = RelationServiceVerifier(
        "incubate",
        oidc_client=oidc,  # type: ignore[arg-type]
        environ=env,
    )
    passed = await verifier.verify("knowlab")
    assert passed.caller == "knowlab"
    with pytest.raises(UnauthorizedError):  # 不是 knowlab：这种关系不认它
        await verifier.verify("knowmart")
    with pytest.raises(ForbiddenError) as caught:  # knowlab 的测试身份不是孵化身份
        await verifier.verify("testrun")
    assert caught.value.code == "service_scope_denied"


def test_the_incubation_method_is_not_an_expert_users_can_ask() -> None:
    """孵化方法只供孵化用（专家孵化方案第八节）：不在请专家的列表里，也看不了、请不了。"""
    from app.application.workbench.catalog import builtin_on_offer, builtin_on_view
    from app.domain.workbench.errors import NoSuchExpert

    assert INCUBATE.internal
    assert INCUBATE.profile_id not in {p.profile_id for p in listed_packs()}
    for look in (builtin_on_view, builtin_on_offer):
        with pytest.raises(NoSuchExpert):
            look(INCUBATE.profile_id)
    [step] = INCUBATE.workflow
    assert step.tools == () and step.on_reject == "rework" and step.max_reworks == 1
    policy = INCUBATE.policy_for(step)
    assert (policy.command, policy.file_change_in_project, policy.questions) == (
        "deny",
        "deny",
        "deny",
    )
