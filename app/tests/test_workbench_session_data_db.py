"""聊天、工作里选用云端知识库的几份（SDD 0042 第四节）：真数据库、经网页接口选，模型用两个工具读。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import A, B, bootstrap
from test_workbench_routes import make_client as make_client  # noqa: F401

import app.interfaces.endpoints.workbench_routes as routes
from app.application.workbench.pinned_data import DataSources, PinnedData
from app.application.workbench.session_data import SessionLibrary, session_note
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import DataChoiceInvalid, NotFound, RecordsRefused
from app.infrastructure.documents.table_extract import CachedTableExtractor
from app.infrastructure.documents.text_extract import CachedTextExtractor
from app.interfaces.http.middleware.auth import get_web_knowbase_token_provider

CSV = "项目,2024\n营业收入,250.25\n".encode()
ITEMS = {
    "i1": {"name": "营收.csv", "media_type": "text/csv; charset=utf-8", "data": CSV},
    "img": {"name": "图.png", "media_type": "image/png", "data": b"\x89PNG"},
}


class Cloud:
    """knowbase 的替身：核资料、签授权、交还、凭授权读。"""

    def __init__(self) -> None:
        self.granted: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def item(self, user_token: str, item_id: str) -> dict[str, Any]:
        if item_id not in ITEMS:
            raise DataChoiceInvalid("a chosen item is not in your cloud knowledge base")
        found = ITEMS[item_id]
        return {
            "name": found["name"],
            "media_type": found["media_type"],
            "size_bytes": len(found["data"]),
            "sha256": hashlib.sha256(found["data"]).hexdigest(),
        }

    async def grant(self, user_token, *, ref, items, ttl_seconds, kind="delegation"):  # type: ignore[no-untyped-def]
        self.granted.append(
            {"ref": ref, "items": items, "kind": kind, "ttl": ttl_seconds}
        )
        return f"g-{len(self.granted)}"

    async def release(self, grant_id: str) -> None:
        self.released.append(grant_id)

    async def content(self, *, grant_id: str, item_id: str) -> bytes:
        return ITEMS[item_id]["data"]


@pytest.fixture
def cloud(make_client, monkeypatch):  # noqa: F811
    fake = Cloud()
    monkeypatch.setattr(routes, "cloud_grants", lambda: fake)

    async def user_token() -> str:
        return "user-token"

    make_client.app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
        user_token
    )
    return fake


def source(cloud: Cloud) -> DataSources:
    return DataSources(
        pinned=PinnedData(
            cloud=cloud,
            agent=None,
            text=CachedTextExtractor(),
            tables=CachedTableExtractor(),
        ),
        legacy=None,  # type: ignore[arg-type]
    )


async def test_a_conversation_chooses_cloud_data_and_the_model_reads_it(
    make_client,  # noqa: F811
    db,  # noqa: F811
    cloud,
) -> None:
    async with make_client(A) as http:
        _, _, sid = await bootstrap(http)
        url = f"/api/workbench/sessions/{sid}/cloud-data"
        assert (await http.get(url)).json()["items"] == []
        bad = await http.put(url, json={"items": ["img"]})
        assert (bad.status_code, bad.json()["code"]) == (422, "data_choice_invalid")
        chosen = await http.put(url, json={"items": ["i1", "i1"]})
        assert chosen.status_code == 200, chosen.text
        assert [i["name"] for i in chosen.json()["items"]] == ["营收.csv"]
        assert chosen.json()["expires_at"]
        assert cloud.granted == [
            {
                "ref": f"knowbench-session:{sid}",
                "items": [{"item_id": "i1", "sha256": hashlib.sha256(CSV).hexdigest()}],
                "kind": "session",
                "ttl": 24 * 3600,
            }
        ]
        # 改选：换一条授权，交还旧的；清空：只交还
        await http.put(url, json={"items": ["i1"]})
        assert cloud.released == ["g-1"]
        assert (await http.get(url)).json()["items"][0]["item_id"] == "i1"
    async with make_client(B) as http:
        # 别人的对话：没有
        assert (await http.get(url)).status_code == 404

    async with workbench_stores(db)() as repo:
        library = SessionLibrary(repo, source(cloud))
        listed = await library.list(sid)
        assert [i["item_id"] for i in listed["items"]] == ["i1"]
        read = await library.read(sid, "i1")
        assert "营业收入" in read["text"]
        assert read["citation"] == {
            "item_id": "i1",
            "sha256": hashlib.sha256(CSV).hexdigest(),
            "page": 1,
            "pages": 1,
        }
        with pytest.raises(NotFound):
            await library.read(sid, "img")
        rows = await repo.session_data(sid)
        note = session_note(sid, rows, datetime.now(UTC))
        assert note and "营收.csv" in note and f'conversation="{sid}"' in note
        # 过了时限：不说、也不给读
        later = datetime.now(UTC) + timedelta(hours=25)
        assert session_note(sid, rows, later) is None
        with pytest.raises(RecordsRefused, match="ended"):
            await SessionLibrary(repo, source(cloud), now=lambda: later).list(sid)

    async with make_client(A) as http:
        cleared = await http.put(url, json={"items": []})
        assert cleared.json() == {
            "contract_version": 2,
            "items": [],
            "expires_at": None,
        }
        assert cloud.released == ["g-1", "g-2"]
    async with workbench_stores(db)() as repo:
        with pytest.raises(RecordsRefused, match="nothing"):
            await SessionLibrary(repo, source(cloud)).list(sid)
