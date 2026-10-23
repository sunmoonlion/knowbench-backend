"""分身读用户数据（SDD 0027 §3.5）：工作台这一道核（请来的分身）、清单的规矩、读 knowlab 的客户端。"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from knowbot_fixtures import KNOWBOT, manifest

from app.application.workbench.knowbot_data import KnowbotData
from app.domain.workbench.errors import (
    KnowbotInvalid,
    NotFound,
    RecordsRefused,
    UserDataUnavailable,
)
from app.domain.workbench.knowbots import data_bindings, pack_from_manifest
from app.infrastructure.external.knowlab_data_client import KnowlabDataClient

OWNER = str(uuid.uuid4())
DATASET = str(uuid.uuid4())
BINDING = {"dataset_id": DATASET, "version": "1", "sha256": "a" * 64}
DIGEST = "sha256:" + "d" * 64


class Repo:
    def __init__(self, pin: dict[str, Any] | None) -> None:
        self.pin = pin
        self.data: list[dict[str, Any]] = []  # 委托钉住的数据（SDD 0042）
        self.relay_asked = 0

    async def list_task_data(self, task_id):
        return self.data

    async def get_relay_identity(self, owner_actor_id):
        self.relay_asked += 1
        return {"relay_user": "relay-1"} if owner_actor_id == OWNER else None

    async def get_knowbot_pin(self, *, owner_actor_id, knowbot_id, version):
        if self.pin and (owner_actor_id, knowbot_id, version) == (
            OWNER,
            KNOWBOT,
            "1",
        ):
            return self.pin
        return None


class Records:
    """读项目记录那一层的替身：委托由测试给定，记下读了什么。"""

    def __init__(self, task: dict[str, Any], pin: dict[str, Any] | None) -> None:
        self.task = task
        self.repo = Repo(pin)
        self.owner = OWNER
        self.noted: list[tuple[str, dict]] = []

    async def _working(self, task):
        return self.task

    async def _noted(self, task, tool, **what):
        self.noted.append((tool, what))


class Source:
    def __init__(self) -> None:
        self.purposes: list[dict] = []

    async def list(self, purpose):
        self.purposes.append(purpose)
        return [{**BINDING, "name": "年报.csv"}]

    async def read(self, purpose, dataset_id, page):
        self.purposes.append(purpose)
        return {
            "dataset_id": dataset_id,
            "text": "x",
            "citation": {**BINDING, "page": page},
        }


def _task(**over):
    return {
        "id": str(uuid.uuid4()),
        "pack_source": "knowmart",
        "profile_id": f"knowbot:{KNOWBOT}@1",
        "pack_digest": DIGEST,
    } | over


def _pin(**over):
    doc = manifest()
    doc["data_bindings"] = [BINDING]
    return {"owner_subject": "casdoor-user-1", "digest": DIGEST, "manifest": doc} | over


async def test_a_summoned_knowbot_reads_as_its_owner_with_the_pinned_digest() -> None:
    records, source = Records(_task(), _pin()), Source()
    data = KnowbotData(records, source)  # type: ignore[arg-type]
    assert (await data.list("t"))["datasets"][0]["dataset_id"] == DATASET
    assert (await data.read("t", DATASET, page="2"))["citation"]["page"] == 2
    assert source.purposes[0] == {
        "kind": "knowbot",
        "owner_subject": "casdoor-user-1",
        "knowbot_id": KNOWBOT,
        "version": "1",
        "digest": DIGEST,
    }
    assert [t for t, _ in records.noted] == ["list_my_data", "read_my_data"]


@pytest.mark.parametrize(
    "task, pin",
    [
        (_task(pack_source="builtin", profile_id="fin-review"), _pin()),  # 内置专家
        (_task(), None),  # 没钉过
        (_task(pack_digest="sha256:" + "e" * 64), _pin()),  # 委托上的不是钉住的那一版
        (_task(), _pin(manifest=manifest())),  # 这一版没绑数据
    ],
)
async def test_without_a_bound_version_nothing_is_asked(task, pin) -> None:
    source = Source()
    with pytest.raises(RecordsRefused):
        await KnowbotData(Records(task, pin), source).list("t")  # type: ignore[arg-type]
    assert source.purposes == []


async def test_only_bound_data_is_read() -> None:
    source = Source()
    with pytest.raises(NotFound):
        await KnowbotData(Records(_task(), _pin()), source).read("t", str(uuid.uuid4()))  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await KnowbotData(Records(_task(), _pin()), source).read("t", "../x")  # type: ignore[arg-type]
    assert source.purposes == []


def test_a_knowbot_may_list_only_the_data_tools() -> None:
    doc = manifest()
    doc["pack"]["workflow"][0]["tools"] = ["list_my_data", "read_my_data"]
    assert pack_from_manifest(doc, knowbot_id=KNOWBOT, version="1").workflow[0].tools
    doc["pack"]["workflow"][0]["tools"] = ["list_my_data", "run_sql"]
    with pytest.raises(KnowbotInvalid, match="tools a knowbot cannot use"):
        pack_from_manifest(doc, knowbot_id=KNOWBOT, version="1")
    doc = manifest(requires_tools=True)
    with pytest.raises(KnowbotInvalid, match="platform's data"):
        pack_from_manifest(doc, knowbot_id=KNOWBOT, version="1")


def test_bindings_are_read_from_the_manifest() -> None:
    assert data_bindings({"data_bindings": [BINDING, {"x": 1}, "y"]}) == {
        DATASET: ("1", "a" * 64)
    }
    assert data_bindings(None) == {} and data_bindings({"data_bindings": "x"}) == {}


# ---------- 读 knowlab 的客户端 ----------

ENV = {
    "KNOWLAB_INTERNAL_URL": "https://knowlab-tls.ns.svc.cluster.local/api/internal/v1/knowlab",
    "KNOWLAB_READ_SERVICE_CLIENT_ID": "sunmoon-knowbench-knowlab-read",
    "KNOWLAB_READ_SERVICE_CLIENT_SECRET": "not-a-real-secret",
    "KNOWLAB_READ_SERVICE_SCOPE": "knowlab:read",
    "KNOWLAB_READ_SERVICE_DISCOVERY_URL": "https://casdoor.example.test/.well-known/openid-configuration",
    "KNOWLAB_READ_SERVICE_BACKCHANNEL_ENDPOINT": "https://casdoor-internal.example.test",
}
PURPOSE = {"kind": "test", "run_id": "knowlab-run-0001"}


class FakeOidc:
    async def exchange_client_credentials(self, **kw):
        assert kw["scope"] == "knowlab:read"
        return {"access_token": "service-token", "expires_in": 300}


def _client(handler) -> KnowlabDataClient:
    return KnowlabDataClient(
        ENV, oidc_client=FakeOidc(), transport=httpx.MockTransport(handler)
    )


async def test_the_client_posts_the_contract_with_a_service_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/list"):
            return httpx.Response(
                200, json={"contract": "knowbot-data/v1", "datasets": [BINDING]}
            )
        return httpx.Response(
            200,
            json={
                "contract": "knowbot-data/v1",
                "dataset_id": DATASET,
                "text": "x",
                "citation": BINDING,
            },
        )

    client = _client(handler)
    assert await client.list(PURPOSE) == [BINDING]
    got = await client.read(PURPOSE, DATASET, 3)
    assert got["text"] == "x" and "contract" not in got
    assert [r.url.path for r in seen] == [
        "/api/internal/v1/knowlab/knowbot-data/list",
        "/api/internal/v1/knowlab/knowbot-data/read",
    ]
    assert all(r.headers["authorization"] == "Bearer service-token" for r in seen)
    assert json.loads(seen[1].content) == {
        "contract": "knowbot-data/v1",
        "purpose": PURPOSE,
        "dataset_id": DATASET,
        "page": 3,
    }


async def test_not_found_and_unavailable_are_told_apart() -> None:
    missing = _client(
        lambda r: httpx.Response(404, json={"code": "knowbot_data_not_found"})
    )
    with pytest.raises(NotFound):
        await missing.list(PURPOSE)
    down = _client(lambda r: httpx.Response(503, json={}))
    with pytest.raises(UserDataUnavailable):
        await down.list(PURPOSE)
    odd = _client(lambda r: httpx.Response(200, json={"datasets": []}))
    with pytest.raises(UserDataUnavailable):
        await odd.list(PURPOSE)
    with pytest.raises(UserDataUnavailable):
        await KnowlabDataClient({}).list(PURPOSE)  # 没配：不去连


def test_reading_user_data_waits_long_enough_for_a_big_annual_report() -> None:
    """knowlab 第一次抽一份两百页的年报要十几、二十秒：读数的超时要比它长，又要短于模型调工具的
    60 秒（K19 预演）。"""
    from app.bootstrap.workbench import KNOWLAB_DATA_TIMEOUT, knowbot_data_source

    knowbot_data_source.cache_clear()
    try:
        client = knowbot_data_source().legacy  # type: ignore[attr-defined]
        assert isinstance(client, KnowlabDataClient)
        assert client._options == {"timeout": KNOWLAB_DATA_TIMEOUT}
        assert 20 < KNOWLAB_DATA_TIMEOUT < 60
    finally:
        knowbot_data_source.cache_clear()
