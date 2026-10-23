"""knowbase 用户资料客户端（契约 user-data/v1，SDD 0042）：用户令牌与服务身份两条路。"""

from __future__ import annotations

import httpx
import pytest

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.external.knowbase_user_data import (
    KnowbaseRefused,
    KnowbaseUserData,
)

URL = "https://knowbase.internal"
RELATIONS = ("user-data-read", "user-data-grant-test", "user-data-purchase")


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


def client(
    answer, **env: str
) -> tuple[KnowbaseUserData, list[httpx.Request], FakeOidc]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    oidc = FakeOidc()
    return (
        KnowbaseUserData(
            environ(**env), transport=httpx.MockTransport(handler), oidc_client=oidc
        ),
        seen,
        oidc,
    )


async def test_user_calls_carry_the_user_token_and_the_raw_file():
    kb, seen, oidc = client(lambda r: httpx.Response(201, json={"item_id": "i1"}))
    assert await kb.upload("user-tok", "营收.csv", b"a,b\n") == {"item_id": "i1"}
    request = seen[0]
    assert (
        request.method == "POST"
        and request.url.path == "/api/internal/v1/user-data/items"
    )
    assert request.url.params["name"] == "营收.csv"
    assert request.headers["authorization"] == "Bearer user-tok"
    assert request.content == b"a,b\n"
    assert oidc.calls == []  # 用户的事不用服务身份


async def test_downloads_come_back_as_bytes_with_their_digest():
    kb, seen, _ = client(
        lambda r: httpx.Response(
            200,
            content=b"%PDF-1",
            headers={"content-type": "application/pdf", "x-content-sha256": "ab" * 32},
        )
    )
    got = await kb.download("user-tok", "item/../x")
    assert (got.data, got.media_type, got.sha256) == (
        b"%PDF-1",
        "application/pdf",
        "ab" * 32,
    )
    # 编号原样当一段路径，不能拼出别的路径
    assert seen[0].url.raw_path.endswith(b"/items/item%2F..%2Fx/content")


async def test_service_calls_use_their_own_relation_and_cache_the_token():
    kb, seen, oidc = client(lambda r: httpx.Response(200, content=b"data"))
    await kb.read_content("g1", "i1")
    await kb.read_content("g1", "i2")
    assert oidc.calls == ["knowbase:user-data-read"]
    assert seen[0].headers["authorization"] == "Bearer svc-knowbase:user-data-read"
    assert seen[1].url.path == "/api/internal/v1/user-data/grants/g1/items/i2/content"


async def test_test_grants_and_purchases_use_their_relations():
    kb, seen, oidc = client(lambda r: httpx.Response(201, json={"ok": True}))
    await kb.test_grant(
        owner="alice", consent_id="c1", reader_app="knowbench", ref="t5", items=[]
    )
    await kb.purchase(
        seller="s",
        seller_item_id="i",
        buyer="b",
        buyer_app="knowlab",
        transaction_ref="tx",
    )
    assert oidc.calls == [
        "knowbase:user-data-grant-test",
        "knowbase:user-data-purchase",
    ]
    assert [r.url.path for r in seen] == [
        "/api/internal/v1/user-data/test-grants",
        "/api/internal/v1/user-data/purchases",
    ]


async def test_refusals_keep_knowbases_code_and_failures_are_unavailable():
    kb, _, _ = client(
        lambda r: httpx.Response(409, json={"code": "in_use", "message": "x"})
    )
    with pytest.raises(KnowbaseRefused) as refused:
        await kb.delete("user-tok", "i1")
    assert (refused.value.status, refused.value.code) == (409, "in_use")
    kb, _, _ = client(lambda r: httpx.Response(503, json={}))
    with pytest.raises(ServiceUnavailableError) as down:
        await kb.items("user-tok")
    assert down.value.code == "knowbase_unavailable"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    kb, _, _ = client(unreachable)
    with pytest.raises(ServiceUnavailableError):
        await kb.items("user-tok")


@pytest.mark.parametrize(
    "env",
    [
        {"KNOWBASE_INTERNAL_URL": ""},
        {"KNOWBASE_INTERNAL_URL": "http://knowbase.internal"},  # 只走 https
    ],
)
async def test_without_a_knowbase_address_nothing_is_sent(env):
    kb, seen, _ = client(lambda r: httpx.Response(200, json=[]), **env)
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.items("user-tok")
    assert caught.value.code == "knowbase_unconfigured" and seen == []


async def test_a_missing_service_relation_is_unconfigured():
    kb, seen, _ = client(
        lambda r: httpx.Response(200, json={}),
        KNOWBASE_USER_DATA_READ_SERVICE_SCOPE="knowbase:something-else",
    )
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.view_grant("g1")
    assert caught.value.code == "knowbase_unconfigured" and seen == []


class TradingAuth:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def user_token(self, session_id: str | None, *, audience: str) -> str:
        self.calls.append((session_id, audience))
        return "for-knowbase"


async def test_the_web_dependency_trades_this_sessions_token(monkeypatch):
    from app.interfaces.http.middleware import auth as middleware
    from core.config import Settings

    trading = TradingAuth()
    monkeypatch.setattr(middleware, "web_auth_service", trading)
    monkeypatch.setattr(
        middleware,
        "get_settings",
        lambda: Settings(_env_file=None, knowbase_user_audience="knowbase-client"),
    )
    token = await middleware.get_web_knowbase_token(session=None, session_id="sid")
    assert token == "for-knowbase"
    assert trading.calls == [("sid", "knowbase-client")]
    monkeypatch.setattr(middleware, "get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(ServiceUnavailableError) as caught:
        await middleware.get_web_knowbase_token(session=None, session_id="sid")
    assert caught.value.code == "knowbase_unconfigured"
