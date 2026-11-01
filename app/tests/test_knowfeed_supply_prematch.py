"""预切合（契约 knowfeed-supply-prematch/v1，knowfeed 定）：锁与提供方 schema 一致，我们的
模型收发的就是 schema 列的；拼出来的请求合提供方的约束；客户端走 supply-demand 这条关系。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from app.application.errors.exceptions import ServiceUnavailableError
from app.domain.workbench.data_request import DataRequest
from app.domain.workbench.supply_prematch import (
    CONDITIONS,
    CONTRACT,
    PATH,
    RELATION,
    Candidate,
    Conditions,
    PrematchAnswer,
    PrematchItem,
    PrematchKey,
    PrematchRequest,
    PrematchResponse,
    eta_of,
    request_items,
    supply_snapshot,
)
from app.infrastructure.external.knowfeed_supply import KnowfeedSupply

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowfeed-supply-prematch-lock.json"
REQUEST = "prematch-request.schema.json"
RESPONSE = "prematch-response.schema.json"


def _lock() -> dict:
    if not LOCK.exists():
        pytest.skip("parent contracts/ is not checked out")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _provider(relative: str) -> Path:
    root = os.environ.get("KNOWFEED_SUPPLY_PREMATCH_CONTRACT_DIR")
    path = (
        Path(root) / Path(relative).name
        if root
        else PARENT.parent / "knowfeed-app" / relative
    )
    if not path.exists():
        pytest.skip("knowfeed-app is not checked out next to knowbench-app")
    return path


def _schema(name: str) -> dict:
    lock = _lock()
    return json.loads(
        _provider(lock["schemas"][name]["path"]).read_text(encoding="utf-8")
    )


def test_the_lock_matches_the_provider() -> None:
    lock = _lock()
    assert lock["provider"] == "knowfeed-app" and lock["major_version"] == 1
    assert lock["contract"] == "knowfeed-supply-prematch"
    for name in (REQUEST, RESPONSE):
        pinned = lock["schemas"][name]
        path = _provider(pinned["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pinned["sha256"]
        manifest = json.loads(
            (path.parent / "contract-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["schemas"][name] == pinned["sha256"]
    # 入口与关系照提供方写的
    assert manifest["contract"] == CONTRACT
    assert manifest["path"] == lock["path"] == PATH
    assert manifest["relation"] == lock["relation"] == RELATION


def _same_fields(ours: type[BaseModel], theirs: dict) -> None:
    mine = ours.model_json_schema()
    assert set(mine["properties"]) == set(theirs["properties"]), ours.__name__
    assert set(theirs.get("required") or []) <= set(mine.get("required") or []) | {
        "contract"
    }, ours.__name__


def test_our_models_carry_exactly_what_the_schemas_list() -> None:
    request, response = _schema(REQUEST), _schema(RESPONSE)
    _same_fields(PrematchRequest, request)
    _same_fields(PrematchItem, request["$defs"]["PrematchItemV1"])
    _same_fields(PrematchKey, request["$defs"]["PrematchKeyV1"])
    _same_fields(Conditions, request["$defs"]["ConditionsV1"])
    _same_fields(PrematchResponse, response)
    _same_fields(PrematchAnswer, response["$defs"]["PrematchAnswerV1"])
    _same_fields(Candidate, response["$defs"]["CandidateV1"])
    status = response["$defs"]["PrematchAnswerV1"]["properties"]["status"]["enum"]
    assert status == list(PrematchAnswer.model_fields["status"].annotation.__args__)
    assert request["properties"]["items"]["maxItems"] == 50
    assert request["$defs"]["ConditionsV1"]["properties"]["min_license"]["anyOf"][0][
        "enum"
    ] == ["self", "via_expert", "sale"]
    Conditions.model_validate(CONDITIONS)


NEED = DataRequest.model_validate(
    {
        "key": "req1_2",
        "name": "上一年比较",
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period": {"kind": "fixed", "start": "2024-01-01", "end": "2024-12-31"},
        "formats": ["pdf"],
        "reading": "pages",
    }
)


def entry(status: str = "to_confirm", **key: object) -> dict:
    return {
        "ref": "req1_2:abc",
        "need_key": "req1_2",
        "status": status,
        "key": {
            "subject": "600519.SH",
            "doc_type": "annual_report",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "edition": "full",
            "topics": [],
            "industry": None,
        }
        | key,
    }


def test_only_items_to_fetch_with_a_subject_are_asked() -> None:
    [item] = request_items(
        [entry(), entry("resolved"), entry(subject=None), entry(subject="x" * 80)],
        [NEED],
    )
    assert item == {
        "ref": "req1_2:abc",
        "key": {
            "subject": "600519.SH",
            "doc_type": "annual_report",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "edition": "full",
            "topics": [],
        },
        "readings": ["pages"],
    }
    PrematchRequest.model_validate({"items": [item], "conditions": CONDITIONS})
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate({"items": [item], "conditions": CONDITIONS}, _schema(REQUEST))


def test_the_snapshot_and_the_estimate() -> None:
    assert supply_snapshot(None) == {"conditions": CONDITIONS}
    answer = {"contract": CONTRACT, "items": []}
    assert supply_snapshot(answer) == {"conditions": CONDITIONS, "prematch": answer}

    def with_answer(status: str, eta: int | None) -> dict:
        return {"prematch": {"status": status, "eta_seconds": eta}}

    # 保守地逐项相加；取不到的不算；能取的里有一项算不出就不写
    assert eta_of([with_answer("available", 60), with_answer("available", 90)]) == 150
    assert (
        eta_of([with_answer("available", 60), with_answer("unavailable", None)]) == 60
    )
    assert (
        eta_of([with_answer("available", 60), with_answer("available", None)]) is None
    )
    assert eta_of([with_answer("site_pending", None), {}]) is None


def _env() -> dict[str, str]:
    prefix = "KNOWFEED_SUPPLY_DEMAND_SERVICE_"
    return {
        "KNOWFEED_INTERNAL_URL": "https://knowfeed.internal/api/internal/v1/supply",
        prefix + "CLIENT_ID": "knowbench",
        prefix + "CLIENT_SECRET": "secret",
        prefix + "SCOPE": "knowfeed:supply-demand",
        prefix
        + "DISCOVERY_URL": "https://casdoor.internal/.well-known/openid-configuration",
        prefix + "BACKCHANNEL_ENDPOINT": "https://casdoor.internal",
    }


class FakeOidc:
    async def exchange_client_credentials(self, **values: str) -> dict[str, object]:
        return {"access_token": f"svc-{values['scope']}", "expires_in": 300}


def _client(answer) -> tuple[KnowfeedSupply, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    return (
        KnowfeedSupply(
            _env(), transport=httpx.MockTransport(handler), oidc_client=FakeOidc()
        ),
        seen,
    )


async def test_the_client_asks_with_the_supply_demand_relation() -> None:
    answer = {
        "contract": CONTRACT,
        "items": [
            {
                "ref": "req1_2:abc",
                "status": "available",
                "license": "self",
                "eta_seconds": 120,
                "readings_missing": [],
                "candidates": [
                    {
                        "channel_id": "cninfo",
                        "name": "巨潮资讯",
                        "license": "self",
                        "status": "active",
                        "readings": ["original", "pages"],
                        "eta_seconds": 120,
                    }
                ],
            }
        ],
    }
    feed, seen = _client(lambda r: httpx.Response(200, json=answer))
    items = request_items([entry()], [NEED])
    got = await feed.prematch(items, CONDITIONS)
    assert got["items"][0]["eta_seconds"] == 120
    [request] = seen
    assert request.method == "POST"
    assert request.url.path == "/api/internal/v1/supply/prematch"
    assert request.headers["authorization"] == "Bearer svc-knowfeed:supply-demand"
    assert json.loads(request.content) == {"items": items, "conditions": CONDITIONS}


async def test_an_answer_off_contract_or_a_failure_is_unavailable() -> None:
    feed, _ = _client(lambda r: httpx.Response(200, json={"items": [{"ref": "x"}]}))
    with pytest.raises(ServiceUnavailableError):
        await feed.prematch(request_items([entry()], [NEED]), CONDITIONS)
    feed, _ = _client(lambda r: httpx.Response(503))
    with pytest.raises(ServiceUnavailableError):
        await feed.prematch(request_items([entry()], [NEED]), CONDITIONS)
    with pytest.raises(ServiceUnavailableError) as err:
        await KnowfeedSupply({}).prematch([], CONDITIONS)
    assert err.value.code == "knowfeed_unconfigured"
