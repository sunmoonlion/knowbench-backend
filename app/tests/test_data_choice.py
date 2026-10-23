"""请专家时选数据、钉住（SDD 0042 第五节）：声明的规矩、核所选、签授权与交还。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from app.application.workbench import data_choice
from app.application.workbench.agent_service import AgentOffline, AgentRefused
from app.application.workbench.data_choice import (
    DataChooser,
    GrantReleaser,
    Prepared,
    last_choices,
    under,
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
LIBRARY = "C:\\Users\\u\\知识库"
PDF = b"%PDF-1.7 annual report"


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


@pytest.mark.parametrize(
    ("path", "directory", "inside"),
    [
        ("C:\\Users\\u\\知识库\\年报.pdf", LIBRARY, True),
        ("c:/users/U/知识库/2024/年报.pdf", LIBRARY, True),
        ("C:\\Users\\u\\知识库", LIBRARY, False),
        ("C:\\Users\\u\\知识库2\\年报.pdf", LIBRARY, False),
        ("C:\\Users\\u\\知识库\\..\\秘密.pdf", LIBRARY, False),
        ("/home/u/kb/a.pdf", "/home/u/kb", True),
        ("/home/u/KB/a.pdf", "/home/u/kb", False),
        ("/home/u/kb/../x", "/home/u/kb/", False),
    ],
)
def test_local_data_must_be_inside_the_library(path, directory, inside) -> None:
    assert under(path, directory) is inside


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


class Agent:
    def __init__(self, *, digest: bool = True, library: str | None = LIBRARY) -> None:
        self.digest = digest
        self.library = library
        self.calls: list[tuple[str, Any]] = []

    async def call(self, relay_user, method, params=None, *, wait_seconds=None):  # type: ignore[no-untyped-def]
        assert relay_user == "relay-1"
        self.calls.append((method, params))
        if method == "service/hello":
            return {"libraryDir": self.library}
        if method == "fs/digest":
            if not self.digest:
                raise AgentRefused(-32601, "Method not found")
            return {
                "size": len(PDF),
                "mtime": 1,
                "sha256": hashlib.sha256(PDF).hexdigest(),
            }
        if method == "fs/getMetadata":
            return {"type": "file", "size": len(PDF), "mtime": 1}
        if method == "fs/readFile":
            offset, length = params["offset"], params["length"]
            return {
                "content": base64.b64encode(PDF[offset : offset + length]).decode(),
                "size": len(PDF),
            }
        raise AssertionError(method)


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


async def test_cloud_and_local_choices_are_pinned_with_one_grant() -> None:
    cloud, agent = Cloud(), Agent()
    got = await prepare(
        DataChooser(cloud=cloud, agent=agent),
        [
            {"need": "annual_reports", "source": "cloud", "item_id": "i1"},
            {
                "need": "annual_reports",
                "source": "local",
                "path": LIBRARY + "\\2023 年报.pdf",
            },
        ],
    )
    assert got.grant_id == "grant-1"
    assert cloud.granted == [
        {
            "ref": "knowbench:k-1",
            "items": [{"item_id": "i1", "sha256": "c" * 64}],
            "ttl": data_choice.GRANT_TTL_SECONDS,
        }
    ]
    first, second = got.pins
    assert (first["source"], first["slot"], first["grant_id"]) == (
        "cloud",
        "annual_reports",
        "grant-1",
    )
    assert (second["source"], second["name"], second["media_type"]) == (
        "local",
        "2023 年报.pdf",
        "application/pdf",
    )
    assert (
        second["sha256"] == hashlib.sha256(PDF).hexdigest()
        and second.get("grant_id") is None
    )
    assert [m for m, _ in agent.calls] == ["service/hello", "fs/digest"]


async def test_an_old_link_is_read_back_block_by_block(monkeypatch) -> None:
    monkeypatch.setattr(data_choice, "MAX_READ_BYTES", 8)
    agent = Agent(digest=False)
    got = await prepare(
        DataChooser(cloud=Cloud(), agent=agent),
        [{"need": "annual_reports", "source": "local", "path": LIBRARY + "\\a.pdf"}],
    )
    assert got.pins[0]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert [p["offset"] for m, p in agent.calls if m == "fs/readFile"] == [0, 8, 16]
    assert got.grant_id is None  # 只有本地的不签授权


@pytest.mark.parametrize(
    ("choices", "agent", "expected", "words"),
    [
        ([], Agent(), DataRequired, "choose the data"),
        (
            [{"need": "annual_reports", "source": "cloud", "item_id": "t1"}],
            Agent(),
            DataChoiceInvalid,
            "format",
        ),
        (
            [{"need": "annual_reports", "source": "cloud", "item_id": "nope"}],
            Agent(),
            DataChoiceInvalid,
            "not in your",
        ),
        (
            [
                {
                    "need": "annual_reports",
                    "source": "local",
                    "path": "C:\\Users\\u\\项目\\a.pdf",
                }
            ],
            Agent(),
            DataChoiceInvalid,
            "local knowledge base folder",
        ),
        (
            [
                {
                    "need": "annual_reports",
                    "source": "local",
                    "path": LIBRARY + "\\a.pdf",
                }
            ],
            Agent(library=None),
            DataChoiceInvalid,
            "no local knowledge base",
        ),
        (
            [
                {
                    "need": "annual_reports",
                    "source": "local",
                    "path": LIBRARY + "\\a.pdf",
                }
            ],
            None,
            UserDataUnavailable,
            "cannot be reached",
        ),
    ],
)
async def test_bad_choices_are_refused_before_any_grant(
    choices, agent, expected, words
) -> None:
    cloud = Cloud()
    with pytest.raises(expected, match=words):
        await prepare(DataChooser(cloud=cloud, agent=agent), choices)
    assert cloud.granted == []


async def test_an_expert_without_needs_takes_no_data() -> None:
    chooser = DataChooser(cloud=Cloud(), agent=Agent())
    assert (await prepare(chooser, [], declared=[])).pins == []
    with pytest.raises(DataChoiceInvalid):
        await prepare(
            chooser, [{"need": "x", "source": "cloud", "item_id": "i1"}], declared=[]
        )


async def test_link_refusals_and_offline_computers() -> None:
    class Refusing(Agent):
        def __init__(self, failure: Exception) -> None:
            super().__init__()
            self.failure = failure

        async def call(self, relay_user, method, params=None, *, wait_seconds=None):  # type: ignore[no-untyped-def]
            if method == "fs/digest":
                raise self.failure
            return await super().call(relay_user, method, params)

    choice = [
        {"need": "annual_reports", "source": "local", "path": LIBRARY + "\\a.pdf"}
    ]
    with pytest.raises(DataChoiceInvalid, match="not there"):
        await prepare(
            DataChooser(cloud=Cloud(), agent=Refusing(AgentRefused(-32012, "x"))),
            choice,
        )
    with pytest.raises(AgentOffline):
        await prepare(
            DataChooser(cloud=Cloud(), agent=Refusing(AgentOffline("off"))), choice
        )


async def test_an_unused_grant_is_handed_back() -> None:
    cloud = Cloud()
    chooser = DataChooser(cloud=cloud, agent=None)
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

    async def grants_to_release(self, *, limit: int = 50) -> list[str]:
        return list(self.pending)

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


async def test_the_release_loop_stops_when_asked() -> None:
    stop = asyncio.Event()
    releaser = GrantReleaser(Stores([]), Cloud(), interval_seconds=0.01)  # type: ignore[arg-type]
    running = asyncio.create_task(releaser.run_forever(stop))
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(running, 1)
