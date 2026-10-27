"""请专家时选数据、钉住（SDD 0042 第五节）：声明的规矩、核所选、签授权与交还。"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from pydantic import ValidationError

from app.application.workbench import data_choice
from app.application.workbench.data_choice import (
    DataChooser,
    GrantReleaser,
    Prepared,
    last_choices,
)
from app.domain.workbench.data_needs import (
    BadNeeds,
    DataChoice,
    choice_problems,
    kind_of_name,
    needs_of,
)
from app.domain.workbench.errors import (
    DataChoiceInvalid,
    DataRequired,
    UserDataUnavailable,
)
from app.domain.workbench.models import HandoverRequest

REPORTS = {
    "key": "annual_reports",
    "name": "年报",
    "formats": ["pdf"],
    "min_count": 1,
    "max_count": 2,
}
TABLES = {
    "key": "statements",
    "name": "三张报表",
    "formats": ["excel", "csv"],
    "required": False,
}


def needs(*raw: dict) -> list:
    return needs_of({"data_needs": list(raw)})


# ---------- 声明与所选的规矩 ----------


def test_old_manifests_have_no_needs_and_bad_needs_are_refused() -> None:
    assert needs_of({"data_bindings": []}) == []
    assert [n.key for n in needs(REPORTS, TABLES)] == ["annual_reports", "statements"]
    for bad in ([REPORTS, REPORTS], [{**REPORTS, "formats": ["word"]}], "年报"):
        with pytest.raises(BadNeeds):
            needs_of({"data_needs": bad})


def test_choices_follow_the_needs() -> None:
    declared = needs(REPORTS, TABLES)
    one = {"need": "annual_reports", "format": "pdf", "identity": "a"}
    assert choice_problems(declared, [one]) == []
    assert choice_problems(declared, []) == ["年报: choose 1 to 2, 0 chosen"]
    assert choice_problems(
        declared, [one, {**one, "identity": "b"}, {**one, "identity": "c"}]
    ) == ["年报: choose 1 to 2, 3 chosen"]
    assert choice_problems(declared, [one, {**one}]) == [
        "年报: the same item is chosen twice"
    ]
    assert choice_problems(
        declared, [one, {"need": "statements", "format": "text", "identity": "t"}]
    ) == ["三张报表: a chosen item is not in a format it takes"]
    assert choice_problems(
        declared, [one, {"need": "other", "format": "pdf", "identity": "x"}]
    ) == ["a chosen item is for a need the expert does not declare"]


def test_a_choice_names_exactly_one_place() -> None:
    DataChoice(need="annual_reports", source="cloud", item_id="i1")
    DataChoice(need="annual_reports", source="local", path="C:\\a.pdf")
    for bad in (
        {"source": "cloud", "path": "C:\\a.pdf"},
        {"source": "local", "item_id": "i1"},
        {"source": "cloud", "item_id": "i1", "path": "C:\\a.pdf"},
    ):
        with pytest.raises(ValidationError):
            DataChoice(need="annual_reports", **bad)


def test_file_kinds_by_name() -> None:
    assert kind_of_name("2024 年报.PDF") == ("application/pdf", "pdf")
    assert kind_of_name("表.xlsx")[1] == "excel"  # type: ignore[index]
    assert kind_of_name("说明.docx") is None and kind_of_name("README") is None


def test_a_request_without_data_keeps_its_digest() -> None:
    base = {
        "idempotency_key": "k-00000001",
        "session_id": "s",
        "profile_id": "p",
        "original_input": {"text": "x"},
    }
    plain = HandoverRequest(**base)
    assert plain.digest() == HandoverRequest(**base, data=[]).digest()
    chosen = HandoverRequest(
        **base, data=[{"need": "a", "source": "cloud", "item_id": "i"}]
    )
    assert chosen.digest() != plain.digest()


# ---------- 选数据服务 ----------


class Cloud:
    def __init__(self) -> None:
        self.items = {
            "i1": {
                "name": "2024 年报.pdf",
                "media_type": "application/pdf",
                "size_bytes": 10,
                "sha256": "c" * 64,
            },
            "t1": {
                "name": "说明.txt",
                "media_type": "text/plain; charset=utf-8",
                "size_bytes": 3,
                "sha256": "d" * 64,
            },
        }
        self.granted: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def item(self, user_token: str, item_id: str) -> dict[str, Any]:
        assert user_token == "user-token"
        if item_id not in self.items:
            raise DataChoiceInvalid("a chosen item is not in your cloud knowledge base")
        return self.items[item_id]

    async def grant(self, user_token, *, ref, items, ttl_seconds) -> str:  # type: ignore[no-untyped-def]
        self.granted.append({"ref": ref, "items": items, "ttl": ttl_seconds})
        return "grant-1"

    async def release(self, grant_id: str) -> None:
        self.released.append(grant_id)


async def token() -> str:
    return "user-token"


async def prepare(chooser: DataChooser, choices: list[dict], declared=None):  # type: ignore[no-untyped-def]
    return await chooser.prepare(
        needs=declared if declared is not None else needs(REPORTS, TABLES),
        choices=choices,
        relay_user="relay-1",
        user_token=token,
        ref="knowbench:k-1",
    )


async def test_cloud_choices_are_pinned_with_one_grant() -> None:
    cloud = Cloud()
    got = await prepare(
        DataChooser(cloud=cloud),
        [{"need": "annual_reports", "source": "cloud", "item_id": "i1"}],
    )
    assert got.grant_id == "grant-1"
    assert cloud.granted == [
        {
            "ref": "knowbench:k-1",
            "items": [{"item_id": "i1", "sha256": "c" * 64}],
            "ttl": data_choice.GRANT_TTL_SECONDS,
        }
    ]
    [pin] = got.pins
    assert (pin["source"], pin["slot"], pin["grant_id"]) == (
        "cloud",
        "annual_reports",
        "grant-1",
    )


@pytest.mark.parametrize(
    ("choices", "expected", "words"),
    [
        ([], DataRequired, "choose the data"),
        (
            [{"need": "annual_reports", "source": "cloud", "item_id": "t1"}],
            DataChoiceInvalid,
            "format",
        ),
        (
            [{"need": "annual_reports", "source": "cloud", "item_id": "nope"}],
            DataChoiceInvalid,
            "not in your",
        ),
        (
            # 本地知识库已退役（SDD 0043 第 4.1 节）：新请求里选本地的一律拒
            [
                {"need": "annual_reports", "source": "cloud", "item_id": "i1"},
                {"need": "annual_reports", "source": "local", "path": "C:\\a.pdf"},
            ],
            DataChoiceInvalid,
            "local data sources are retired",
        ),
    ],
)
async def test_bad_choices_are_refused_before_any_grant(
    choices, expected, words
) -> None:
    cloud = Cloud()
    with pytest.raises(expected, match=words):
        await prepare(DataChooser(cloud=cloud), choices)
    assert cloud.granted == []


async def test_an_expert_without_needs_takes_no_data() -> None:
    chooser = DataChooser(cloud=Cloud())
    assert (await prepare(chooser, [], declared=[])).pins == []
    with pytest.raises(DataChoiceInvalid):
        await prepare(
            chooser, [{"need": "x", "source": "cloud", "item_id": "i1"}], declared=[]
        )


async def test_an_unused_grant_is_handed_back() -> None:
    cloud = Cloud()
    chooser = DataChooser(cloud=cloud)
    await chooser.release(Prepared(pins=[], grant_id="grant-9"))
    await chooser.release(Prepared())
    assert cloud.released == ["grant-9"]


def test_last_choices_come_back_as_the_dialog_wants_them() -> None:
    pins = [
        {
            "slot": "annual_reports",
            "source": "cloud",
            "item_id": "i1",
            "path": None,
            "name": "a.pdf",
        },
        {
            "slot": "statements",
            "source": "local",
            "item_id": None,
            "path": "C:\\b.csv",
            "name": "b.csv",
        },
    ]
    assert last_choices(pins) == [
        {"need": "annual_reports", "source": "cloud", "name": "a.pdf", "item_id": "i1"},
        {"need": "statements", "source": "local", "name": "b.csv", "path": "C:\\b.csv"},
    ]


# ---------- 委托结束交还授权 ----------


class Stores:
    def __init__(self, pending: list[str]) -> None:
        self.pending = pending
        self.marked: list[str] = []

    @contextlib.asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield

    async def grants_to_release(self, *, limit: int = 50) -> list[dict]:
        return [
            {"grant_id": g, "source": "knowbase" if g.startswith("v2-") else "cloud"}
            for g in self.pending
        ]

    async def mark_grant_released(self, grant_id: str) -> None:
        self.marked.append(grant_id)


async def test_ended_tasks_hand_their_grants_back() -> None:
    class Flaky(Cloud):
        async def release(self, grant_id: str) -> None:
            if grant_id == "g-down":
                raise UserDataUnavailable("down")
            await super().release(grant_id)

    stores, cloud = Stores(["g-1", "g-down", "g-2"]), Flaky()
    assert await GrantReleaser(stores, cloud).sweep() == 2  # type: ignore[arg-type]
    assert cloud.released == ["g-1", "g-2"] and stores.marked == ["g-1", "g-2"]
    # 第四版的授权走 v2 的交还；没配 v2 的交还就先不动，下一轮再来
    released_v2: list[str] = []

    async def release_v2(grant_id: str) -> None:
        released_v2.append(grant_id)

    stores = Stores(["v2-a", "g-3"])
    assert await GrantReleaser(stores, Cloud()).sweep() == 1  # type: ignore[arg-type]
    assert stores.marked == ["g-3"]
    stores = Stores(["v2-a"])
    releaser = GrantReleaser(stores, Cloud(), release_v2=release_v2)  # type: ignore[arg-type]
    assert await releaser.sweep() == 1
    assert released_v2 == ["v2-a"] and stores.marked == ["v2-a"]


async def test_the_release_loop_stops_when_asked() -> None:
    stop = asyncio.Event()
    releaser = GrantReleaser(Stores([]), Cloud(), interval_seconds=0.01)  # type: ignore[arg-type]
    running = asyncio.create_task(releaser.run_forever(stop))
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(running, 1)
