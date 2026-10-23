"""聊天、工作里选用云端知识库的几份（SDD 0042 第四节「聊天、工作时选用几份」）。

用户在一段对话里选云端知识库（B）的几份：凭本人令牌向 knowbase 核这几份在他的 knowbench 分区里，
签一条「会话」运行授权（只读这几份、钉这几个摘要，24 小时兜底）。改选就换一条、交还旧的；
清空就只交还。模型在这一轮的说明里看到对话编号与能读的几份，用 `list_library`、
`read_library_item` 读（读法与请专家钉住的数据同一套：取字节、核摘要、抽文字、分页）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.application.ports.workbench import (
    CloudGrants,
    KnowbotDataSource,
    WorkbenchStore,
)
from app.application.workbench.data_choice import MAX_FILE_BYTES, UserToken
from app.application.workbench.pinned_data import CLOUD, pinned_purpose
from app.domain.workbench.data_needs import format_of
from app.domain.workbench.errors import (
    DataChoiceInvalid,
    NotFound,
    RecordsRefused,
    WorkbenchError,
)

log = logging.getLogger(__name__)

MAX_ITEMS = 20  # 一段对话最多选用这么多份
GRANT_TTL = timedelta(hours=24)  # 「会话」运行授权的兜底时限
ENDED = (
    "the user's permission to read these has ended; ask them to choose the data again"
)


def listed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给网页、给模型看的样子（不带授权编号）。"""
    return [
        {
            "item_id": r["item_id"],
            "name": r["name"],
            "media_type": r["media_type"],
            "size_bytes": int(r["size_bytes"]),
            "sha256": r["sha256"],
        }
        for r in rows
    ]


def session_note(
    session_id: str, rows: list[dict[str, Any]], now: datetime
) -> str | None:
    """这一轮给模型的说明：这段对话能读云端知识库里的哪几份、怎么读。没有、过期了就不说。"""
    live = [r for r in rows if r["expires_at"] > now]
    if not live:
        return None
    names = "\n".join(f"- {r['name']}" for r in live)
    return (
        "The user chose these items of their cloud knowledge base for this conversation:\n"
        f"{names}\n"
        f'List them with list_library(conversation="{session_id}") and read one with '
        f'read_library_item(conversation="{session_id}", item=<item_id>, page=<n>). '
        "Only these can be read; quote the citation when you use the content."
    )


class SessionCloudData:
    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        cloud: CloudGrants,
        now: Any = None,
    ) -> None:
        self.repo = repo
        self.cloud = cloud
        self.now = now or (lambda: datetime.now(UTC))

    async def listing(self, *, session_id: str, owner_actor_id: str) -> dict[str, Any]:
        await self.repo.get_session(session_id, owner_actor_id=owner_actor_id)
        rows = await self.repo.session_data(session_id)
        return self._view(rows)

    async def choose(
        self,
        *,
        session_id: str,
        owner_actor_id: str,
        item_ids: list[str],
        user_token: UserToken,
    ) -> dict[str, Any]:
        """换成这几份（空的就是不用了）。"""
        await self.repo.get_session(session_id, owner_actor_id=owner_actor_id)
        wanted = list(dict.fromkeys(item_ids))
        if len(wanted) > MAX_ITEMS:
            raise DataChoiceInvalid(f"choose at most {MAX_ITEMS} items")
        before = await self.repo.session_data(session_id)
        rows: list[dict[str, Any]] = []
        if wanted:
            token = await user_token()
            items = []
            for item_id in wanted:
                item = await self.cloud.item(token, item_id)
                if format_of(str(item.get("media_type") or "")) is None:
                    raise DataChoiceInvalid(
                        f"{item.get('name')} cannot be read as text"
                    )
                if int(item.get("size_bytes") or 0) > MAX_FILE_BYTES:
                    raise DataChoiceInvalid(f"{item.get('name')} is larger than 50 MB")
                items.append((item_id, item))
            grant_id = await self.cloud.grant(
                token,
                ref=f"knowbench-session:{session_id}",
                items=[
                    {"item_id": i, "sha256": str(item["sha256"])} for i, item in items
                ],
                ttl_seconds=int(GRANT_TTL.total_seconds()),
                kind="session",
            )
            expires = self.now() + GRANT_TTL
            rows = [
                {
                    "item_id": i,
                    "grant_id": grant_id,
                    "name": str(item.get("name") or i)[:255],
                    "media_type": str(item.get("media_type")),
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "sha256": str(item["sha256"]),
                    "expires_at": expires,
                }
                for i, item in items
            ]
        async with self.repo.transaction():
            await self.repo.replace_session_data(session_id, rows)
        for grant_id in {r["grant_id"] for r in before}:
            try:
                await self.cloud.release(grant_id)
            except WorkbenchError:
                log.warning("could not hand back a session grant %s", grant_id)
        return self._view(rows)

    def _view(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        live = [r for r in rows if r["expires_at"] > self.now()]
        return {
            "items": listed(live),
            "expires_at": live[0]["expires_at"].isoformat() if live else None,
        }


class SessionLibrary:
    """模型读这段对话选用的几份：`list_library`、`read_library_item`。"""

    def __init__(
        self, repo: WorkbenchStore, source: KnowbotDataSource, *, now: Any = None
    ):
        self.repo = repo
        self.source = source
        self.now = now or (lambda: datetime.now(UTC))

    async def list(self, session_id: str) -> dict[str, Any]:
        rows = await self._live(session_id)
        return {"items": listed(rows)}

    async def read(self, session_id: str, item: Any, page: Any = 1) -> dict[str, Any]:
        rows = await self._live(session_id)
        item_id = str(item) if isinstance(item, str) else ""
        if not any(r["item_id"] == item_id for r in rows):
            raise NotFound("this item was not chosen for this conversation")
        try:
            number = max(1, int(page or 1))
        except (TypeError, ValueError):
            number = 1
        pins = [
            {**r, "dataset_id": r["item_id"], "source": CLOUD, "path": None}
            for r in rows
        ]
        purpose, _ = pinned_purpose(pins, None)
        found = await self.source.read(purpose, item_id, number)
        citation = found.pop("citation", {})
        found.pop("dataset_id", None)
        return {
            "item_id": item_id,
            **found,
            "citation": {
                "item_id": item_id,
                "sha256": citation.get("sha256"),
                "page": citation.get("page"),
                "pages": citation.get("pages"),
            },
        }

    async def _live(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.repo.session_data(session_id)
        if not rows:
            raise RecordsRefused(
                "nothing from the cloud knowledge base was chosen here"
            )
        live = [r for r in rows if r["expires_at"] > self.now()]
        if not live:
            raise RecordsRefused(ENDED)
        return live
