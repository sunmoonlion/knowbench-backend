"""把用户的数据抽成文字（SDD 0042；从 knowlab 搬来，原 0027 §3.5）。

端口是 application/ports/knowbots.TextExtractor。

CSV、TXT、MD 原样；Excel（xlsx、xls）每张表转成 CSV；PDF 逐页抽文字。文件是用户传的，
当作不可信：xlsx 先看解压后的总大小，字数、单元格数、页数都有上限，到了就停、标截断。
同一份内容（sha256）抽一次，结果在进程里缓存。解析是同步库，放到线程里跑。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any

from app.domain.workbench.data_read import MAX_TEXT_CHARS, Extracted

log = logging.getLogger(__name__)

MAX_XLSX_UNZIPPED = 512 * 1024 * 1024  # xlsx 解压后超过这么大不打开（防压缩炸弹）
MAX_CELLS = 2_000_000
MAX_PDF_PAGES = 2_000
CACHE_CHARS = 8_000_000  # 缓存里的文字总量上限（中文约 32 MB），超了先丢最久没用的
NO_TEXT = "No readable text was found (a scanned document has only images)."
UNREADABLE = "The file could not be read as its type."


class _Full(Exception):
    """字数到上限了。"""


class _Text:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.limit = MAX_TEXT_CHARS
        self.truncated = False

    def add(self, piece: str) -> None:
        room = self.limit - self.size
        if len(piece) > room:
            self.parts.append(piece[:room])
            self.size = self.limit
            self.truncated = True
            raise _Full
        self.parts.append(piece)
        self.size += len(piece)

    def value(self) -> str:
        return "".join(self.parts)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _sheet(
    out: _Text, name: str, rows: Iterable[Iterable[Any]], cells: list[int]
) -> None:
    out.add(f"## Sheet: {name}\n")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        values = [_cell(v) for v in row]
        while values and values[-1] == "":
            values.pop()
        cells[0] += len(values)
        if cells[0] > MAX_CELLS:
            out.truncated = True
            raise _Full
        writer.writerow(values)
        out.add(buffer.getvalue())
        buffer.seek(0)
        buffer.truncate()
    out.add("\n")


def _xlsx(data: bytes, out: _Text) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(i.file_size for i in archive.infolist()) > MAX_XLSX_UNZIPPED:
            raise ValueError("workbook too large when unpacked")
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        cells = [0]
        for sheet in book.worksheets:
            _sheet(out, sheet.title, sheet.iter_rows(values_only=True), cells)
    finally:
        book.close()


def _xls(data: bytes, out: _Text) -> None:
    import xlrd

    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    try:
        cells = [0]
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            rows: Iterator[Iterable[Any]] = (
                sheet.row_values(r) for r in range(sheet.nrows)
            )
            _sheet(out, sheet.name, rows, cells)
            book.unload_sheet(index)
    finally:
        book.release_resources()


def _pdf(data: bytes, out: _Text) -> None:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if number > MAX_PDF_PAGES:
                out.truncated = True
                raise _Full
            text = page.extract_text() or ""
            out.add(f"## Page {number}\n{text}\n\n")
            page.close()  # 释放这一页的解析缓存，大文件不攒内存


def extract_text(data: bytes, content_type: str) -> Extracted:
    """同步抽取。认不出的类型、坏文件：回空文字并说明，不抛给模型看内部错误。"""
    kind = content_type.split(";")[0].strip()
    out = _Text()
    try:
        if kind.startswith("text/"):
            out.add(data.decode("utf-8-sig", errors="replace"))
        elif kind == "application/pdf":
            _pdf(data, out)
        elif kind.endswith("spreadsheetml.sheet"):
            _xlsx(data, out)
        elif kind == "application/vnd.ms-excel":
            _xls(data, out)
        else:
            return Extracted(text="", note=UNREADABLE)
    except _Full:
        pass
    except Exception as exc:  # 用户的坏文件：记类型，不记内容
        log.warning(
            "knowbot_data_extract_failed type=%s error=%s", kind, type(exc).__name__
        )
        return Extracted(text="", note=UNREADABLE)
    text = out.value()
    note = None
    if out.truncated:
        note = "The text was cut at the size limit; later parts are not available."
    elif (
        kind == "application/pdf"
        and not "".join(
            line for line in text.splitlines() if not line.startswith("## Page ")
        ).strip()
    ):
        note = NO_TEXT
    return Extracted(text=text, truncated=out.truncated, note=note)


class CachedTextExtractor:
    """按 sha256 缓存抽取结果；同一份内容并发来读只抽一次。缓存里没有才调 `load` 取字节
    （字节可能要经服务流从用户电脑上一块块读，或从 knowbase 取，SDD 0042）。

    抽取单独跑成一个任务，来读的人只是等它：读的那头先超时走了（一份两百页的年报要抽十几、
    二十秒，调用方的超时可能更短），抽取照样做完、进缓存，下一次来读直接拿到，不会每次都从头抽、
    每次都超时（K19 预演发现）。
    """

    def __init__(self, cache_chars: int = CACHE_CHARS) -> None:
        self._cache: OrderedDict[str, Extracted] = OrderedDict()
        self._chars = 0
        self._limit = cache_chars
        self._lock = threading.Lock()
        self._pending: dict[str, asyncio.Task[Extracted]] = {}

    async def extract(
        self,
        *,
        load: Callable[[], Awaitable[bytes]],
        content_type: str,
        sha256: str,
    ) -> Extracted:
        key = f"{sha256}:{content_type}"
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._extract(key, load, content_type))
            self._pending[key] = task
        return await asyncio.shield(task)

    async def _extract(
        self, key: str, load: Callable[[], Awaitable[bytes]], content_type: str
    ) -> Extracted:
        try:
            data = await load()
            result = await asyncio.to_thread(extract_text, data, content_type)
            self._remember(key, result)
            return result
        finally:
            self._pending.pop(key, None)

    def _remember(self, key: str, result: Extracted) -> None:
        size = len(result.text)
        if size > self._limit:
            return
        with self._lock:
            self._cache[key] = result
            self._chars += size
            while self._chars > self._limit and self._cache:
                _, dropped = self._cache.popitem(last=False)
                self._chars -= len(dropped.text)
