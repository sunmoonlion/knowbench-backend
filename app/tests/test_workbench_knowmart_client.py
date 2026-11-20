"""向 knowmart 取清单的客户端：路径、令牌、404 与不可达的区分、契约核对（假 Casdoor 与假 knowmart）。"""

from __future__ import annotations

import httpx
import pytest
from knowbot_fixtures import KNOWBOT, manifest

from app.domain.workbench.errors import KnowbotUnavailable, NoSuchExpert
from app.domain.workbench.knowbots import manifest_digest
from app.infrastructure.external.knowmart_client import KnowmartSummonClient

ENV = {
    "KNOWMART_INTERNAL_URL": "https://knowmart-tls.ns.svc.cluster.local/api/internal/v1/knowmart",
    "KNOWMART_SUMMON_SERVICE_CLIENT_ID": "sunmoon-knowbench-knowmart-summon",
    "KNOWMART_SUMMON_SERVICE_CLIENT_SECRET": "not-a-real-secret",
    "KNOWMART_SUMMON_SERVICE_SCOPE": "knowmart:summon",
    "KNOWMART_SUMMON_SERVICE_DISCOVERY_URL": "https://casdoor.example.test/.well-known/openid-configuration",
    "KNOWMART_SUMMON_SERVICE_BACKCHANNEL_ENDPOINT": "https://casdoor-internal.example.test",
}


class FakeOidc:
    async def exchange_client_credentials(self, **kw):
        assert kw["scope"] == "knowmart:summon"
        return {"access_token": "service-token", "expires_in": 300}


def _client(handler) -> KnowmartSummonClient:
    return KnowmartSummonClient(
        ENV, oidc_client=FakeOidc(), transport=httpx.MockTransport(handler)
    )


def _found(doc, **over):
    return {
        "contract_version": 1,
        "knowbot_id": KNOWBOT,
        "name": doc["name"],
        "tagline": doc["tagline"],
        "version": doc["version"],
        "digest": manifest_digest(doc),
        "manifest": doc,
    } | over


async def test_summon_asks_for_the_owners_version_with_a_service_token() -> None:
    seen: list[httpx.Request] = []
    doc = manifest()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_found(doc))

    got = await _client(handler).summon(
        owner_subject="user/1", knowbot_id=KNOWBOT, version="1"
    )
    assert got["manifest"] == doc and got["digest"] == manifest_digest(doc)
    assert seen[0].url.raw_path.decode() == (
        f"/api/internal/v1/knowmart/owners/user%2F1/knowbots/{KNOWBOT}@1"
    )
    assert seen[0].headers["authorization"] == "Bearer service-token"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404, json={"code": "knowbot_not_found"}), NoSuchExpert),
        (httpx.Response(403, json={"code": "forbidden_scope"}), KnowbotUnavailable),
        (httpx.Response(503), KnowbotUnavailable),
        (httpx.Response(200, json={"knowbot_id": "other"}), KnowbotUnavailable),
    ],
)
async def test_not_found_is_told_apart_from_unavailable(response, error) -> None:
    with pytest.raises(error):
        await _client(lambda _: response).summon(
            owner_subject="u", knowbot_id=KNOWBOT, version="1"
        )


async def test_another_version_in_the_answer_is_refused() -> None:
    doc = manifest()
    with pytest.raises(KnowbotUnavailable):
        await _client(
            lambda _: httpx.Response(200, json=_found(doc, version="2"))
        ).summon(owner_subject="u", knowbot_id=KNOWBOT, version="1")


async def test_unconfigured_summon_is_unavailable() -> None:
    with pytest.raises(KnowbotUnavailable):
        await KnowmartSummonClient({}).summon(
            owner_subject="u", knowbot_id=KNOWBOT, version="1"
        )


async def test_formal_use_decisions_bind_owner_and_each_pinned_version():
    import json
    from uuid import uuid4

    from app.domain.workbench.formal_method_generated import MethodRef

    owner = "test/author"
    ref = {"method_id": str(uuid4()), "digest": "sha256:" + "0" * 64}
    refs = [{**ref, "version": n} for n in range(1, 102)]
    sizes = []

    def handler(request):
        assert request.url.path.endswith("/formal-methods/eligible")
        assert request.headers["authorization"] == "Bearer service-token"
        query = json.loads(request.content)
        assert query["owner_subject"] == owner
        batch = query["references"]
        sizes.append(len(batch))
        return httpx.Response(200, json={"owner_subject": owner, "allowed": batch[:1]})

    result = await _client(handler).eligible_formal_refs(owner, refs)
    assert sizes == [100, 1]
    assert result == {MethodRef.model_validate(refs[i]) for i in (0, 100)}
    for reply in [
        {"owner_subject": "other", "allowed": []},
        {"owner_subject": owner, "allowed": [refs[1]]},
        {"owner_subject": owner, "allowed": [{**refs[0], "digest": "bad"}]},
    ]:
        with pytest.raises(KnowbotUnavailable):
            await _client(
                lambda _, reply=reply: httpx.Response(200, json=reply)
            ).eligible_formal_refs(owner, refs[:1])
