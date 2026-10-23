"""读这件委托钉住的数据（SDD 0042 第五节）：请专家时用户从本地知识库（A）、云端知识库（B）选的那几份。

- A：经会合点服务流请本地 link 读（`fs/readFile`，一次最多 512 KiB，一块块接起来）；
- B：凭这件委托的运行授权向 knowbase 读（knowbase 按授权里钉的摘要再核一次）。
取到字节先核 sha256，与请专家那一刻记下的不一样就报「数据在这次委托开始后被改过」，不给内容。
抽文字、分页、按格子取、核引文用读数层（domain/workbench/data_read），结果的形状与原来 knowlab
的 `knowbot-data/v1` 一样，模型看到的工具结果不变。抽出来的文字、表按 sha256 缓存，缓存里有就
不再去取字节。

委托该不该读、读哪几份由 knowbot_data.data_scope 先核；这里只认 purpose 里带来的那几份。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

from app.application.ports.workbench import (
    CloudDataReader,
    KnowbotDataSource,
    TableExtractor,
    TextExtractor,
)
from app.application.workbench.agent_service import (
    MAX_READ_BYTES,
    AgentRefused,
    AgentService,
)
from app.domain.workbench.data_read import (
    CellsResultV1,
    CellValue,
    Citation,
    DatasetEntry,
    QuoteFound,
    ReadResultV1,
    TableResultV1,
    TableRow,
    Tables,
    TableUnreadable,
    find_quote,
    paginate,
    pdf_pages,
    table_page,
)
from app.domain.workbench.errors import (
    DataChanged,
    NotFound,
    UserDataUnavailable,
    WorkbenchError,
)

PINNED = "pinned"
CLOUD = "cloud"
LOCAL = "local"
PDF = "application/pdf"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 与 knowbase 收的一致
RATE_RETRIES = 3  # 本地 link 说太快了（-32013）：歇一下再读这一块
CHANGED = "the data was changed after this task started"
NOT_A_TABLE = "this data cannot be read as a table; use read_my_data"
NO_SHEET = "there is no such sheet in this data"


def pinned_purpose(
    pins: list[dict[str, Any]], relay_user: str | None
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """（给数据源的用途，数据编号 → （来源，sha256））。来源记在「版本」那一栏。"""
    purpose = {"kind": PINNED, "relay_user": relay_user, "data": pins}
    bound = {str(p["dataset_id"]): (str(p["source"]), str(p["sha256"])) for p in pins}
    return purpose, bound


def _citation(pin: dict[str, Any], page: int, pages: int) -> Citation:
    return Citation(
        dataset_id=str(pin["dataset_id"]),
        version=str(pin["source"]),
        sha256=str(pin["sha256"]),
        page=page,
        pages=pages,
    )


def _dump(result: Any) -> dict[str, Any]:
    out = result.model_dump(mode="json")
    out.pop("contract", None)
    return out


class PinnedData:
    def __init__(
        self,
        *,
        cloud: CloudDataReader,
        agent: AgentService | None,
        text: TextExtractor,
        tables: TableExtractor,
    ) -> None:
        self.cloud = cloud
        self.agent = agent
        self.text = text
        self.tables_of = tables

    async def list(self, purpose: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            DatasetEntry(
                dataset_id=str(p["dataset_id"]),
                name=str(p["name"]),
                content_type=str(p["media_type"]),
                size_bytes=int(p["size_bytes"]),
                version=str(p["source"]),
                sha256=str(p["sha256"]),
            ).model_dump(mode="json")
            for p in purpose["data"]
        ]

    async def read(
        self, purpose: dict[str, Any], dataset_id: str, page: int
    ) -> dict[str, Any]:
        pin = self._pin(purpose, dataset_id)
        extracted = await self.text.extract(
            load=lambda: self._bytes(purpose, pin),
            content_type=str(pin["media_type"]),
            sha256=str(pin["sha256"]),
        )
        text, pages = paginate(extracted.text, page)
        note = f"There are only {pages} pages." if page > pages else extracted.note
        return _dump(
            ReadResultV1(
                dataset_id=str(pin["dataset_id"]),
                name=str(pin["name"]),
                content_type=str(pin["media_type"]),
                page=page,
                pages=pages,
                text=text,
                truncated=extracted.truncated,
                note=note,
                citation=_citation(pin, page, pages),
            )
        )

    async def table(
        self, purpose: dict[str, Any], dataset_id: str, sheet: str | None, page: int
    ) -> dict[str, Any]:
        pin = self._pin(purpose, dataset_id)
        tables = await self._tables(purpose, pin)
        found = tables.sheet(sheet)
        if found is None:
            raise UserDataUnavailable(NO_SHEET)
        rows, pages = table_page(found, page)
        return _dump(
            TableResultV1(
                dataset_id=str(pin["dataset_id"]),
                name=str(pin["name"]),
                content_type=str(pin["media_type"]),
                sheets=tuple(s.name for s in tables.sheets),
                sheet=found.name,
                columns=found.columns,
                page=page,
                pages=pages,
                rows=tuple(
                    TableRow(
                        label=row.label,
                        values={c: v.raw for c, v in row.cells.items() if v.raw},
                    )
                    for row in rows
                ),
                note=f"There are only {pages} pages." if page > pages else None,
                citation=_citation(pin, page, pages),
            )
        )

    async def cells(
        self, purpose: dict[str, Any], cells: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        loaded: dict[str, tuple[dict[str, Any], Tables]] = {}
        for ref in cells:
            if ref["dataset_id"] not in loaded:
                pin = self._pin(purpose, ref["dataset_id"])
                loaded[ref["dataset_id"]] = (pin, await self._tables(purpose, pin))
        found = []
        for ref in cells:
            pin, tables = loaded[ref["dataset_id"]]
            sheet = tables.sheet(ref["sheet"])
            row = sheet.row(ref["row"]) if sheet else None
            cell = row.cells.get(ref["column"]) if row else None
            known = (
                sheet is not None and row is not None and ref["column"] in sheet.columns
            )
            found.append(
                CellValue(
                    dataset_id=ref["dataset_id"],
                    sheet=ref["sheet"],
                    row=ref["row"],
                    column=ref["column"],
                    found=known,
                    raw=(cell.raw if cell else "") if known else None,
                    number=cell.number if cell else None,
                    version=str(pin["source"]),
                    sha256=str(pin["sha256"]),
                )
            )
        return _dump(CellsResultV1(cells=tuple(found)))["cells"]

    async def quotes(
        self, purpose: dict[str, Any], quotes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        pages: dict[str, tuple[dict[str, Any], dict[int, str], bool]] = {}
        for ref in quotes:
            dataset_id = str(ref["dataset_id"])
            if dataset_id in pages:
                continue
            pin = self._pin(purpose, dataset_id)
            if str(pin["media_type"]).split(";")[0].strip() != PDF:
                pages[dataset_id] = (pin, {}, False)
                continue
            extracted = await self.text.extract(
                load=lambda pin=pin: self._bytes(purpose, pin),
                content_type=str(pin["media_type"]),
                sha256=str(pin["sha256"]),
            )
            pages[dataset_id] = (pin, pdf_pages(extracted.text), extracted.truncated)
        found = []
        for ref in quotes:
            pin, numbered, truncated = pages[str(ref["dataset_id"])]
            miss = find_quote(
                numbered, int(ref["page"]), str(ref["text"]), truncated=truncated
            )
            found.append(
                QuoteFound(
                    dataset_id=str(ref["dataset_id"]),
                    page=int(ref["page"]),
                    found=miss is None,
                    reason=miss,
                    version=str(pin["source"]),
                    sha256=str(pin["sha256"]),
                ).model_dump(mode="json")
            )
        return found

    # ---------- 内部 ----------

    def _pin(self, purpose: dict[str, Any], dataset_id: str) -> dict[str, Any]:
        for pin in purpose["data"]:
            if str(pin["dataset_id"]) == dataset_id:
                return pin
        raise NotFound("data not found")

    async def _tables(self, purpose: dict[str, Any], pin: dict[str, Any]) -> Tables:
        try:
            return await self.tables_of.tables(
                load=lambda: self._bytes(purpose, pin),
                content_type=str(pin["media_type"]),
                sha256=str(pin["sha256"]),
            )
        except TableUnreadable:
            raise UserDataUnavailable(NOT_A_TABLE) from None

    async def _bytes(self, purpose: dict[str, Any], pin: dict[str, Any]) -> bytes:
        if pin["source"] == CLOUD:
            data = await self.cloud.content(
                grant_id=str(pin["grant_id"]), item_id=str(pin["item_id"])
            )
        else:
            data = await self._local(purpose.get("relay_user"), pin)
        if hashlib.sha256(data).hexdigest() != pin["sha256"]:
            raise DataChanged(CHANGED)
        return data

    async def _local(self, relay_user: str | None, pin: dict[str, Any]) -> bytes:
        if self.agent is None:
            raise UserDataUnavailable("the user's computer cannot be reached from here")
        if not relay_user:
            raise UserDataUnavailable("the user's computer is not paired")
        expected = int(pin["size_bytes"])
        if expected > MAX_FILE_BYTES:
            raise UserDataUnavailable("the file is too large to read")
        chunks: list[bytes] = []
        offset = 0
        while True:
            got = await self._read_block(relay_user, str(pin["path"]), offset)
            if int(got.get("size") or 0) != expected:
                raise DataChanged(CHANGED)
            piece = base64.b64decode(str(got.get("content") or ""), validate=True)
            if not piece and offset < expected:
                raise UserDataUnavailable(
                    "the user's computer answered outside the contract"
                )
            chunks.append(piece)
            offset += len(piece)
            if offset >= expected:
                return b"".join(chunks)

    async def _read_block(
        self, relay_user: str, path: str, offset: int
    ) -> dict[str, Any]:
        assert self.agent is not None
        for attempt in range(RATE_RETRIES + 1):
            try:
                return await self.agent.call(
                    relay_user,
                    "fs/readFile",
                    {"path": path, "offset": offset, "length": MAX_READ_BYTES},
                )
            except AgentRefused as exc:
                if exc.code == "rate_limited" and attempt < RATE_RETRIES:
                    await _pause(attempt)
                    continue
                if exc.code == "not_found":
                    raise DataChanged(CHANGED) from None
                raise UserDataUnavailable(
                    f"the user's computer refused to read the file ({exc.code})"
                ) from None
            except WorkbenchError:
                raise UserDataUnavailable(
                    "the user's computer is offline or cannot be reached now"
                ) from None
        raise UserDataUnavailable("the user's computer is busy")  # pragma: no cover


async def _pause(attempt: int) -> None:
    await asyncio.sleep(0.2 * (attempt + 1))


class DataSources:
    """按用途分给钉住的那几份（新路）或 knowlab（旧路，「财务分析与预测」第 1 版停用前）。"""

    def __init__(self, *, pinned: KnowbotDataSource, legacy: KnowbotDataSource) -> None:
        self.pinned = pinned
        self.legacy = legacy

    def _pick(self, purpose: dict[str, Any]) -> KnowbotDataSource:
        return self.pinned if purpose.get("kind") == PINNED else self.legacy

    async def list(self, purpose: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._pick(purpose).list(purpose)

    async def read(
        self, purpose: dict[str, Any], dataset_id: str, page: int
    ) -> dict[str, Any]:
        return await self._pick(purpose).read(purpose, dataset_id, page)

    async def table(
        self, purpose: dict[str, Any], dataset_id: str, sheet: str | None, page: int
    ) -> dict[str, Any]:
        return await self._pick(purpose).table(purpose, dataset_id, sheet, page)

    async def cells(
        self, purpose: dict[str, Any], cells: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        return await self._pick(purpose).cells(purpose, cells)

    async def quotes(
        self, purpose: dict[str, Any], quotes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return await self._pick(purpose).quotes(purpose, quotes)
