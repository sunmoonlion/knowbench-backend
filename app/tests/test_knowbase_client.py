"""调 knowbase 的公共底子（knowbase_client）与数据第四版客户端（user-data/v2）：
用户令牌与服务身份两条路、拒绝与不可达。"""

from __future__ import annotations

import httpx
import pytest

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.external.knowbase_client import KnowbaseRefused
from app.infrastructure.external.knowbase_data_v2 import KnowbaseDataV2

URL = "https://knowbase.internal"
RELATIONS = ("user-data-read", "user-data-output")


def environ(**changes: str) -> dict[str, str]:
    env = {"KNOWBASE_INTERNAL_URL": URL}
    for relation in RELATIONS:
        key = "KNOWBASE_" + relation.upper().replace("-", "_") + "_SERVICE_"
        env |= {
            key + "CLIENT_ID": f"{relation}-client",
            key + "CLIENT_SECRET": "secret",
            key + "SCOPE": f"knowbase:{relation}",
            key
            + "DISCOVERY_URL": "https://casdoor.internal/.well-known/openid-configuration",
            key + "BACKCHANNEL_ENDPOINT": "https://casdoor.internal",
        }
    return env | changes


class FakeOidc:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exchange_client_credentials(self, **values: str) -> dict[str, object]:
        self.calls.append(values["scope"])
        return {"access_token": f"svc-{values['scope']}", "expires_in": 300}


def client(answer, **env: str) -> tuple[KnowbaseDataV2, list[httpx.Request], FakeOidc]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    oidc = FakeOidc()
    return (
        KnowbaseDataV2(
            environ(**env), transport=httpx.MockTransport(handler), oidc_client=oidc
        ),
        seen,
        oidc,
    )


async def test_user_calls_carry_the_user_token():
    kb, seen, oidc = client(lambda r: httpx.Response(200, json={"id": "r1"}))
    assert await kb.resolution("user-tok", "r/../x") == {"id": "r1"}
    request = seen[0]
    assert request.method == "GET"
    assert request.headers["authorization"] == "Bearer user-tok"
    # 编号原样当一段路径，不能拼出别的路径
    assert request.url.raw_path == b"/api/internal/v2/user-data/resolutions/r%2F..%2Fx"
    assert oidc.calls == []  # 用户的事不用服务身份


async def test_service_calls_use_their_own_relation_and_cache_the_token():
    kb, seen, oidc = client(lambda r: httpx.Response(200, json={"revision": 2}))
    await kb.refresh("alice", "r1")
    await kb.release_run_grant("g/1")
    assert oidc.calls == ["knowbase:user-data-read"]
    assert seen[0].headers["authorization"] == "Bearer svc-knowbase:user-data-read"
    assert [r.url.raw_path for r in seen] == [
        b"/api/internal/v2/user-data/service/resolutions/r1/refresh?subject=alice",
        b"/api/internal/v2/user-data/run-grants/g%2F1/release",
    ]


async def test_the_draft_goes_to_the_output_area_with_its_own_relation():
    """SDD 0043 第 2.6 节：成稿以 knowbench 的服务身份（user-data-output）写进产出区，请求体
    就是文件、原样的字节，受益用户、任务、运行、名字与出处在查询参数里；上游许可按这件委托
    的授权 ref 算（独立审 10-30），数据不全的带标记；409 之后能按同样几项查回已写的那份。"""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json={"material_id": "m1", "version_no": 1, "sha256": "a" * 64}
            )
        return httpx.Response(
            201, json={"material_id": "m1", "version_no": 1, "sha256": "a" * 64}
        )

    kb, seen, oidc = client(answer)
    binary = b"%PDF-1.7\n\xff\xfe\x00"
    written = await kb.write_output(
        beneficiary="user-123",
        task_ref="knowbench:task:t1",
        run_ref="t1",
        name="成稿.pdf",
        content=binary,
        expert_id="k1",
        expert_version="3",
        grant_ref="knowbench:k-1",
        task_started_at="2026-10-30T01:00:00+00:00",
        task_finished_at="2026-10-30T02:00:00+00:00",
        incomplete=True,
    )
    assert written["material_id"] == "m1"
    await kb.write_output(
        beneficiary="user-123",
        task_ref="knowbench:task:t2",
        run_ref="t2",
        name="成稿.md",
        content=b"# x",
    )
    found = await kb.find_output(
        beneficiary="user-123",
        task_ref="knowbench:task:t1",
        run_ref="t1",
        name="成稿.pdf",
    )
    assert found["material_id"] == "m1"
    put, bare, lookup = seen
    assert put.url.path == "/api/internal/v2/user-data/outputs"
    assert put.headers["authorization"] == "Bearer svc-knowbase:user-data-output"
    assert put.headers["content-type"] == "application/octet-stream"
    assert put.content == binary
    assert dict(put.url.params) == {
        "beneficiary": "user-123",
        "task_ref": "knowbench:task:t1",
        "run_ref": "t1",
        "name": "成稿.pdf",
        "expert_id": "k1",
        "expert_version": "3",
        "grant_ref": "knowbench:k-1",
        "task_started_at": "2026-10-30T01:00:00+00:00",
        "task_finished_at": "2026-10-30T02:00:00+00:00",
        "incomplete": "true",
    }
    assert dict(bare.url.params) == {
        "beneficiary": "user-123",
        "task_ref": "knowbench:task:t2",
        "run_ref": "t2",
        "name": "成稿.md",
    }
    assert (lookup.method, lookup.url.path) == (
        "GET",
        "/api/internal/v2/user-data/outputs/lookup",
    )
    assert lookup.headers["authorization"] == "Bearer svc-knowbase:user-data-output"
    assert oidc.calls == ["knowbase:user-data-output"]


async def test_the_user_asks_for_a_fresh_resolution_with_their_own_token():
    """「传好了，重新查」：凭本人令牌请 knowbase 重解析（形状同服务那条）。"""
    kb, seen, oidc = client(lambda r: httpx.Response(200, json={"revision": 3}))
    assert (await kb.refresh_mine("user-tok", "r1"))["revision"] == 3
    [request] = seen
    assert (request.method, request.url.path) == (
        "POST",
        "/api/internal/v2/user-data/resolutions/r1/refresh",
    )
    assert request.headers["authorization"] == "Bearer user-tok"
    assert oidc.calls == []


async def test_refusals_keep_knowbases_code_and_failures_are_unavailable():
    kb, _, _ = client(
        lambda r: httpx.Response(409, json={"code": "stale", "message": "x"})
    )
    with pytest.raises(KnowbaseRefused) as refused:
        await kb.revise_needs("user-tok", "r1", [])
    assert (refused.value.status, refused.value.code) == (409, "stale")
    kb, _, _ = client(lambda r: httpx.Response(503, json={}))
    with pytest.raises(ServiceUnavailableError) as down:
        await kb.resolution("user-tok", "r1")
    assert down.value.code == "knowbase_unavailable"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    kb, _, _ = client(unreachable)
    with pytest.raises(ServiceUnavailableError):
        await kb.resolution("user-tok", "r1")
    kb, _, _ = client(lambda r: httpx.Response(204))
    await kb.release_run("knowbench:k1")  # 没有回包也算成了


@pytest.mark.parametrize(
    "env",
    [
        {"KNOWBASE_INTERNAL_URL": ""},
        {"KNOWBASE_INTERNAL_URL": "http://knowbase.internal"},  # 只走 https
    ],
)
async def test_without_a_knowbase_address_nothing_is_sent(env):
    kb, seen, _ = client(lambda r: httpx.Response(200, json={}), **env)
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.resolution("user-tok", "r1")
    assert caught.value.code == "knowbase_unconfigured" and seen == []


async def test_a_missing_service_relation_is_unconfigured():
    kb, seen, _ = client(
        lambda r: httpx.Response(200, json={}),
        KNOWBASE_USER_DATA_READ_SERVICE_SCOPE="knowbase:something-else",
    )
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.refresh("alice", "r1")
    assert caught.value.code == "knowbase_unconfigured" and seen == []


class TradingAuth:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def user_token(self, session_id: str | None, *, audience: str) -> str:
        self.calls.append((session_id, audience))
        return "for-knowbase"


async def test_the_web_dependency_trades_this_sessions_token_when_used(monkeypatch):
    from app.interfaces.http.middleware import auth as middleware
    from core.config import Settings

    trading = TradingAuth()
    monkeypatch.setattr(middleware, "web_auth_service", trading)
    monkeypatch.setattr(
        middleware,
        "get_settings",
        lambda: Settings(_env_file=None, knowbase_user_audience="knowbase-client"),
    )
    provide = await middleware.get_web_knowbase_token_provider(
        session=None, session_cookie="sid"
    )
    assert trading.calls == []  # 真要用时才换
    assert await provide() == "for-knowbase"
    assert trading.calls == [("sid", "knowbase-client")]
    monkeypatch.setattr(middleware, "get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(ServiceUnavailableError) as caught:
        await provide()
    assert caught.value.code == "knowbase_unconfigured"
