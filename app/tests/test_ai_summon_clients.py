"""AI 请专家要调的三条服务关系的客户端：路径、范围、没配就是没开、答得不对算不可用。"""

from __future__ import annotations

import json

import httpx

from app.infrastructure.external.ai_summon_clients import (
    KnowlabIncubationRequests,
    KnowmartDemands,
)

CASDOOR = {
    "DISCOVERY_URL": "https://casdoor.example.test/.well-known/openid-configuration",
    "BACKCHANNEL_ENDPOINT": "https://casdoor-internal.example.test",
    "CLIENT_SECRET": "not-a-real-secret",
}


def env(target: str, relation: str, url: str) -> dict[str, str]:
    key = f"{target.upper()}_{relation.upper().replace('-', '_')}_SERVICE_"
    return {
        f"{target.upper()}_INTERNAL_URL": url,
        key + "CLIENT_ID": f"sunmoon-knowbench-{target}-{relation}",
        key + "SCOPE": f"{target}:{relation}",
        **{key + k: v for k, v in CASDOOR.items()},
    }


class FakeOidc:
    def __init__(self, scope: str) -> None:
        self.scope = scope

    async def exchange_client_credentials(self, **kw):  # type: ignore[no-untyped-def]
        assert kw["scope"] == self.scope
        return {"access_token": "service-token", "expires_in": 300}


MART = "https://knowmart-tls.ns.svc.cluster.local/api/internal/v1/knowmart"
LAB = "https://knowlab-tls.ns.svc.cluster.local/api/internal/v1/knowlab"


async def test_demands_and_incubation_requests_post_to_their_places() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/demands"):
            return httpx.Response(201, json={"demand": {"demand_id": "d1"}})
        return httpx.Response(201, json={"request_id": "r1", "created": True})

    demands = KnowmartDemands(
        env("knowmart", "demand", MART),
        oidc_client=FakeOidc("knowmart:demand"),
        transport=httpx.MockTransport(handler),
    )
    assert await demands.publish("u", {"title": "t"}) == {"demand_id": "d1"}
    desk = KnowlabIncubationRequests(
        env("knowlab", "incubation-request", LAB),
        oidc_client=FakeOidc("knowlab:incubation-request"),
        transport=httpx.MockTransport(handler),
    )
    assert (await desk.submit({"owner_subject": "u"}))["request_id"] == "r1"
    assert [r.url.path for r in seen] == [
        "/api/internal/v1/knowmart/owners/u/demands",
        "/api/internal/v1/knowlab/incubation-requests",
    ]
    assert json.loads(seen[1].content) == {"owner_subject": "u"}
