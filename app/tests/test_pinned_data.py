"""读这件委托钉住的数据（SDD 0042 第五节）：云端经 knowbase、本地经服务流，读时核摘要。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from typing import Any

import pytest
from test_data_read import _pdf
from test_knowbot_compute import EventRepo
from test_workbench_knowbot_data import OWNER, Records, _pin, _task

from app.application.workbench import pinned_data
from app.application.workbench.acceptance import Verdict
from app.application.workbench.acceptance_numbers import judge_numbers
from app.application.workbench.agent_service import AgentOffline, AgentRefused
from app.application.workbench.knowbot_compute import KnowbotCompute
from app.application.workbench.knowbot_data import KnowbotData, data_scope
from app.application.workbench.pinned_data import DataSources, PinnedData
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import (
    DataChanged,
    NotFound,
    UserDataUnavailable,
)
from app.domain.workbench.packs import AcceptanceRule
from app.infrastructure.documents.table_extract import CachedTableExtractor
from app.infrastructure.documents.text_extract import CachedTextExtractor
from app.infrastructure.external.knowbase_user_data import (
    Content,
    KnowbaseGrantedData,
    KnowbaseRefused,
)

CSV = "项目,2024-06-30\n营业收入,250.25\n营业成本,(150)\n".encode()
PDF = _pdf("Revenue grew 15.7 percent on stronger sales")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cloud_pin(data: bytes = CSV, **over: Any) -> dict[str, Any]:
    return {
        "dataset_id": uuid.uuid4(),
        "position": 0,
        "slot": "利润表",
        "source": "cloud",
        "item_id": "item-1",
        "grant_id": "grant-1",
        "path": None,
        "name": "利润表.csv",
        "media_type": "text/csv",
        "size_bytes": len(data),
        "sha256": _sha(data),
    } | over


def local_pin(data: bytes = CSV, **over: Any) -> dict[str, Any]:
    return (
        cloud_pin(
            data,
            source="local",
            item_id=None,
            grant_id=None,
            path="C:\\资料\\利润表.csv",
        )
        | over
    )


class Cloud:
    def __init__(self, data: bytes = CSV) -> None:
        self.data = data
        self.reads: list[tuple[str, str]] = []
        self.checks: list[tuple[str, str]] = []
        self.refuse: Exception | None = None
        self.refused_grants: set[str] = set()

    async def content(self, *, grant_id: str, item_id: str) -> bytes:
        self.reads.append((grant_id, item_id))
        await asyncio.sleep(0)  # 让并发来读的人赶上这一次抽取
        if self.refuse is not None:
            raise self.refuse
        return self.data

    async def check(self, *, grant_id: str, item_id: str) -> str:
        self.checks.append((grant_id, item_id))
        if self.refuse is not None or grant_id in self.refused_grants:
            raise UserDataUnavailable("the permission to read this data has ended")
        return _sha(self.data)


class Agent:
    """本地 link 的替身：按 offset、length 回一块，像 service.ts 的 fs/readFile。"""

    def __init__(self, data: bytes = CSV, *, digest: bool = True) -> None:
        self.data = data
        self.digest = digest  # 老的 link 没有 fs/digest
        self.calls: list[dict[str, Any]] = []
        self.digests: list[str] = []
        self.fail: list[Exception] = []

    async def call(self, relay_user, method, params=None, *, wait_seconds=None):  # type: ignore[no-untyped-def]
        assert relay_user == "relay-1"
        if method == "fs/digest":
            if not self.digest:
                raise AgentRefused(-32601, "no such method")
            self.digests.append(params["path"])
            return {"size": len(self.data), "sha256": _sha(self.data)}
        assert method == "fs/readFile"
        self.calls.append(params)
        if self.fail:
            raise self.fail.pop(0)
        offset, length = params["offset"], params["length"]
        piece = self.data[offset : offset + length]
        return {
            "content": base64.b64encode(piece).decode(),
            "size": len(self.data),
            "mtime": 1,
            "eof": offset + len(piece) >= len(self.data),
        }


class Legacy:
    async def list(self, purpose):  # type: ignore[no-untyped-def]
        raise AssertionError("pinned data must not go to knowlab")


def source(cloud: Cloud | None = None, agent: Agent | None = None) -> DataSources:
    return DataSources(
        pinned=PinnedData(
            cloud=cloud or Cloud(),
            agent=agent,
            text=CachedTextExtractor(),
            tables=CachedTableExtractor(),
        ),
        legacy=Legacy(),  # type: ignore[arg-type]
    )


def records_with(*pins: dict[str, Any]) -> Records:
    records = Records(_task(session_id="s1"), _pin())
    records.repo.data = list(pins)
    return records


# ---------- 读哪几份 ----------


async def test_pinned_data_wins_over_the_authors_bindings() -> None:
    pin = cloud_pin()
    records = records_with(pin)
    purpose, bound = await data_scope(records.repo, OWNER, records.task)  # type: ignore[arg-type]
    assert purpose["kind"] == "pinned" and purpose["relay_user"] is None
    assert bound == {str(pin["dataset_id"]): ("cloud", pin["sha256"])}
    assert records.repo.relay_asked == 0  # 只有云端的不问本地 link 是谁


async def test_local_pins_carry_the_users_link() -> None:
    records = records_with(local_pin())
    purpose, _ = await data_scope(records.repo, OWNER, records.task)  # type: ignore[arg-type]
    assert purpose["relay_user"] == "relay-1"


async def test_listing_shows_only_the_pinned_ones() -> None:
    pin = cloud_pin()
    data = KnowbotData(records_with(pin), source())  # type: ignore[arg-type]
    listed = (await data.list("t"))["datasets"]
    assert listed == [
        {
            "dataset_id": str(pin["dataset_id"]),
            "name": "利润表.csv",
            "content_type": "text/csv",
            "size_bytes": len(CSV),
            "version": "cloud",
            "sha256": pin["sha256"],
        }
    ]


async def test_data_not_pinned_is_not_found() -> None:
    data = KnowbotData(records_with(cloud_pin()), source())  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await data.read("t", str(uuid.uuid4()))


# ---------- 云端 ----------


async def test_cloud_data_is_read_under_the_tasks_grant_and_cached() -> None:
    pin = cloud_pin()
    cloud = Cloud()
    data = KnowbotData(records_with(pin), source(cloud))  # type: ignore[arg-type]
    first = await data.read("t", str(pin["dataset_id"]))
    assert "营业收入" in first["text"] and first["pages"] == 1
    assert first["citation"]["version"] == "cloud"
    assert cloud.checks == []  # 取了字节就是核过了
    await data.read("t", str(pin["dataset_id"]))
    await data.table("t", str(pin["dataset_id"]))
    assert cloud.reads == [("grant-1", "item-1")] * 2  # 文字、表各抽一次
    assert cloud.checks == [("grant-1", "item-1")]  # 缓存里有：不取字节，仍核一次


# ---------- 缓存不跳过授权与摘要（Astra 评审 F01） ----------


async def test_a_revoked_grant_stops_cached_reads() -> None:
    pin = cloud_pin()
    cloud = Cloud()
    data = KnowbotData(records_with(pin), source(cloud))  # type: ignore[arg-type]
    await data.read("t", str(pin["dataset_id"]))
    cloud.refuse = UserDataUnavailable("the permission to read this data has ended")
    with pytest.raises(UserDataUnavailable, match="ended"):
        await data.read("t", str(pin["dataset_id"]))


async def test_the_same_content_under_another_grant_is_checked_on_its_own() -> None:
    """两件委托钉了同一份内容（摘要一样）：缓存是一份，授权各核各的。"""
    mine, theirs = cloud_pin(), cloud_pin(grant_id="grant-2")
    cloud = Cloud()
    pinned = source(cloud)
    await KnowbotData(records_with(mine), pinned).read("t", str(mine["dataset_id"]))  # type: ignore[arg-type]
    cloud.refused_grants.add("grant-2")
    with pytest.raises(UserDataUnavailable):
        await KnowbotData(records_with(theirs), pinned).read(  # type: ignore[arg-type]
            "t", str(theirs["dataset_id"])
        )
    assert cloud.reads == [("grant-1", "item-1")]


async def test_concurrent_readers_that_share_one_extraction_are_each_checked() -> None:
    pin = cloud_pin()
    cloud = Cloud()
    data = KnowbotData(records_with(pin), source(cloud))  # type: ignore[arg-type]
    await asyncio.gather(*(data.read("t", str(pin["dataset_id"])) for _ in range(3)))
    assert len(cloud.reads) == 1 and len(cloud.checks) == 2


async def test_a_local_file_changed_after_caching_is_refused() -> None:
    pin = local_pin()
    agent = Agent()
    data = KnowbotData(records_with(pin), source(agent=agent))  # type: ignore[arg-type]
    await data.read("t", str(pin["dataset_id"]))
    assert agent.digests == []
    await data.read("t", str(pin["dataset_id"]))
    assert agent.digests == ["C:\\资料\\利润表.csv"]
    agent.data = CSV.replace(b"250.25", b"250.26")
    with pytest.raises(DataChanged):
        await data.read("t", str(pin["dataset_id"]))


async def test_an_old_link_without_digest_is_read_whole_to_check() -> None:
    pin = local_pin()
    agent = Agent(digest=False)
    data = KnowbotData(records_with(pin), source(agent=agent))  # type: ignore[arg-type]
    await data.read("t", str(pin["dataset_id"]))
    agent.data = CSV.replace(b"250.25", b"250.26")
    with pytest.raises(DataChanged):
        await data.read("t", str(pin["dataset_id"]))
    assert len(agent.calls) == 2


async def test_content_changed_since_the_task_started_is_refused() -> None:
    pin = cloud_pin()
    data = KnowbotData(records_with(pin), source(Cloud(CSV + b"x")))  # type: ignore[arg-type]
    with pytest.raises(DataChanged, match="changed after this task started"):
        await data.read("t", str(pin["dataset_id"]))


@pytest.mark.parametrize(
    ("refusal", "expected"),
    [
        (KnowbaseRefused(409, "sha256_mismatch"), DataChanged),
        (KnowbaseRefused(404, "item_not_found"), DataChanged),
        (KnowbaseRefused(403, "grant_inactive"), UserDataUnavailable),
        (KnowbaseRefused(404, "grant_not_found"), UserDataUnavailable),
    ],
)
async def test_knowbase_refusals_become_plain_words(refusal, expected) -> None:
    class Client:
        async def read_content(self, grant_id: str, item_id: str) -> Content:
            raise refusal

    with pytest.raises(expected) as caught:
        await KnowbaseGrantedData(Client()).content(grant_id="g", item_id="i")  # type: ignore[arg-type]
    assert expected is not UserDataUnavailable or not isinstance(
        caught.value, DataChanged
    )


async def test_knowbase_check_refusals_read_like_content_refusals() -> None:
    class Client:
        async def read_check(self, grant_id: str, item_id: str) -> dict[str, Any]:
            if grant_id == "gone":
                raise KnowbaseRefused(403, "grant_inactive")
            return {"item_id": item_id, "sha256": "abc"}

    reader = KnowbaseGrantedData(Client())  # type: ignore[arg-type]
    assert await reader.check(grant_id="g", item_id="i") == "abc"
    with pytest.raises(UserDataUnavailable, match="grant_inactive"):
        await reader.check(grant_id="gone", item_id="i")


async def test_knowbase_hands_back_the_bytes() -> None:
    class Client:
        async def read_content(self, grant_id: str, item_id: str) -> Content:
            return Content(data=b"abc", media_type="text/csv", sha256=_sha(b"abc"))

    got = await KnowbaseGrantedData(Client()).content(grant_id="g", item_id="i")  # type: ignore[arg-type]
    assert got == b"abc"


# ---------- 本地 ----------


async def test_local_data_is_read_block_by_block(monkeypatch) -> None:
    monkeypatch.setattr(pinned_data, "MAX_READ_BYTES", 16)
    pin = local_pin()
    agent = Agent()
    data = KnowbotData(records_with(pin), source(agent=agent))  # type: ignore[arg-type]
    got = await data.table("t", str(pin["dataset_id"]))
    assert [r["label"] for r in got["rows"]] == ["营业收入", "营业成本"]
    assert [c["offset"] for c in agent.calls] == list(range(0, len(CSV), 16))
    assert {c["path"] for c in agent.calls} == {"C:\\资料\\利润表.csv"}


async def test_a_busy_link_is_asked_again(monkeypatch) -> None:
    async def no_pause(attempt: int) -> None:
        return None

    monkeypatch.setattr(pinned_data, "_pause", no_pause)
    pin = local_pin()
    agent = Agent()
    agent.fail = [AgentRefused(-32013, "slow down")] * 2
    data = KnowbotData(records_with(pin), source(agent=agent))  # type: ignore[arg-type]
    assert "营业收入" in (await data.read("t", str(pin["dataset_id"])))["text"]


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (Agent(CSV + b"more"), DataChanged),  # 大小变了
        (Agent(CSV.replace(b"250.25", b"250.26")), DataChanged),  # 内容变了
        (None, UserDataUnavailable),  # 没配会合点
    ],
)
async def test_local_changes_and_missing_links_are_refused(agent, expected) -> None:
    pin = local_pin()
    data = KnowbotData(records_with(pin), source(agent=agent))  # type: ignore[arg-type]
    with pytest.raises(expected):
        await data.read("t", str(pin["dataset_id"]))


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AgentRefused(-32012, "gone"), DataChanged),  # 挪走、删了
        (AgentRefused(-32010, "outside"), UserDataUnavailable),
        (AgentOffline("offline"), UserDataUnavailable),
    ],
)
async def test_link_refusals(failure, expected) -> None:
    pin = local_pin()
    agent = Agent()
    agent.fail = [failure]
    data = KnowbotData(records_with(pin), source(agent=agent))  # type: ignore[arg-type]
    with pytest.raises(expected) as caught:
        await data.read("t", str(pin["dataset_id"]))
    assert expected is DataChanged or not isinstance(caught.value, DataChanged)


# ---------- 按格子读、算、核引文 ----------


async def test_compute_reads_cells_from_the_pinned_table() -> None:
    pin = cloud_pin()
    records = records_with(pin)
    records.repo = EventRepo(records.repo)
    got = await KnowbotCompute(records, source()).compute(  # type: ignore[arg-type]
        "t",
        "毛利",
        "r + c",
        {
            "r": calc.address(str(pin["dataset_id"]), "data", "营业收入", "2024-06-30"),
            "c": calc.address(str(pin["dataset_id"]), "data", "营业成本", "2024-06-30"),
        },
    )
    assert got["value"] == "100.25"


async def test_a_text_file_is_not_a_table() -> None:
    text = "就是一段话".encode()
    pin = cloud_pin(text, media_type="text/plain", name="说明.txt")
    data = KnowbotData(records_with(pin), source(Cloud(text)))  # type: ignore[arg-type]
    with pytest.raises(UserDataUnavailable, match="cannot be read as a table"):
        await data.table("t", str(pin["dataset_id"]))


async def test_quotes_are_checked_against_the_pinned_pdf() -> None:
    pin = cloud_pin(PDF, media_type="application/pdf", name="年报.pdf")
    records = records_with(pin)
    records.repo = EventRepo(records.repo)
    rule = AcceptanceRule(kind="quoted", path="items")
    quote = {"dataset_id": str(pin["dataset_id"]), "page": 1}
    verdict = Verdict(
        ok=True,
        parsed={
            "items": [
                {"quotes": [quote | {"text": "Revenue grew 15.7 percent"}]},
                {"quotes": [quote | {"text": "Revenue fell 15.7 percent"}]},
            ]
        },
    )
    got = await judge_numbers(
        verdict,
        (rule,),
        repo=records.repo,  # type: ignore[arg-type]
        task=records.task | {"owner_actor_id": OWNER},
        source=source(Cloud(PDF)),
    )
    assert not got.ok and "#1 page 1: not_found" in got.checks[0]["detail"]
    assert "#0" not in got.checks[0]["detail"]


async def test_changed_data_is_the_platforms_problem_in_acceptance() -> None:
    pin = cloud_pin()
    records = records_with(pin)
    records.repo = EventRepo(records.repo)
    rule = AcceptanceRule(kind="sourced", path="items")
    address = calc.address(str(pin["dataset_id"]), "data", "营业收入", "2024-06-30")
    got = await judge_numbers(
        Verdict(ok=True, parsed={"items": [{"address": address, "value": "250.25"}]}),
        (rule,),
        repo=records.repo,  # type: ignore[arg-type]
        task=records.task | {"owner_actor_id": OWNER},
        source=source(Cloud(CSV + b"x")),
    )
    assert not got.ok and got.checks[0].get("platform") is True
    assert "data_changed" in got.checks[0]["detail"]
