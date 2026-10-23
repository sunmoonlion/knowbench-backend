"""读用户的数据：抽文字、按页给、按格子取、核引文（SDD 0042；原在 knowlab，0027 §3.5、0030 §3.1、0036）。

10-23 所有者定专家不绑数据、请专家用请的人自己的数据（云端知识库在 knowbase，本地知识库在用户电脑），
读的一方是工作台：取到钉住的那一份的字节后在这里抽、在这里分页与核对，结果的形状与原来
knowlab 的 `knowbot-data/v1` 一样（模型看到的工具结果不变）。这里只放纯逻辑与结果的形状；
抽文字、解析表格在 `infrastructure/documents/`。

格子的数一律是十进制字符串，不经浮点。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from app.domain.workbench.models import Strict

CONTRACT = "knowbot-data/v1"
# 一页最多这么多字节（UTF-8）。运行方的模型一次看得到的工具结果有上限
# （knowbench 放到约 9 万字节），超了会从中间省掉、模型还不知道。原来按 2 万字切，
# 中文一页 6 万字节上下，年报第 5 页「主要会计数据」正好落在被省掉的中间
# （2026-10-19 K19 第 2 稿；knowbench 待办账 137）。按字节切、留出余量
PAGE_BYTES = 60_000
MAX_TEXT_CHARS = 5_000_000  # 一份数据抽出的文字最多这么多，超出截断并说明
DATASET_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TABLE_PAGE_CELLS = (
    4_000  # 表格一页最多这么多格（行数按列数折算，至少一行），同时不超过 PAGE_BYTES
)
MAX_CELLS_PER_REQUEST = 500  # 一次取格子最多这么多个（同 0030 §3.2 一次计算的输入上限）
DECIMAL = r"^-?[0-9]+(\.[0-9]+)?$"
Label = Annotated[str, Field(min_length=1, max_length=512)]


class DatasetEntry(Strict):
    dataset_id: str
    name: str
    content_type: str
    size_bytes: int
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ListResultV1(Strict):
    contract: Literal["knowbot-data/v1"] = CONTRACT
    datasets: tuple[DatasetEntry, ...]


class Citation(Strict):
    """引用这份数据时写上：哪份、哪一版、内容摘要、第几页。"""

    dataset_id: str
    version: str
    sha256: str
    page: int
    pages: int


class ReadResultV1(Strict):
    contract: Literal["knowbot-data/v1"] = CONTRACT
    dataset_id: str
    name: str
    content_type: str
    page: int = Field(ge=1)
    pages: int = Field(ge=1)
    text: str
    # 抽出的文字超过上限被截断了，或者抽不出字（比如扫描的 PDF）：照实说，不让模型猜
    truncated: bool = False
    note: str | None = None
    citation: Citation


class TableRow(Strict):
    label: str
    values: dict[str, str]  # 列标签 → 原文；空格子不列


class TableResultV1(Strict):
    contract: Literal["knowbot-data/v1"] = CONTRACT
    dataset_id: str
    name: str
    content_type: str
    sheets: tuple[str, ...]  # 这份数据里所有的表
    sheet: str
    columns: tuple[str, ...]
    page: int = Field(ge=1)
    pages: int = Field(ge=1)
    rows: tuple[TableRow, ...]
    note: str | None = None
    citation: Citation


class CellValue(Strict):
    """按请求的次序一一对应。

    没有这一行或这一列：found 为 false；有但读不成数：number 为空。
    """

    dataset_id: str
    sheet: str
    row: str
    column: str
    found: bool
    raw: str | None = None
    number: str | None = Field(default=None, pattern=DECIMAL)
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CellsResultV1(Strict):
    contract: Literal["knowbot-data/v1"] = CONTRACT
    cells: tuple[CellValue, ...]


# 核引文（SDD 0036）：专家引的原文，回 PDF 那一页找。
# 规整后太短的哪儿都找得到，证明不了什么
MAX_QUOTES_PER_REQUEST = 100
QUOTE_MIN_CHARS = 10
QUOTE_MIN_LETTERS = (
    8  # 汉字或字母至少这么多：纯数字、日期一类年报每页都有，不能证明引对了段落
)
QUOTE_MAX_CHARS = 1_000
QuoteMiss = Literal[
    "not_found", "no_pages", "page_out_of_range", "too_short", "too_long"
]


class QuoteRef(Strict):
    dataset_id: str = Field(pattern=DATASET_ID)
    # PDF 的第几页：抽取时每页前面「## Page N」的 N，阅读器里的页码；不是 read 的阅读页
    page: int = Field(ge=1, le=1_000_000)
    text: str = Field(min_length=1, max_length=4_000)


class QuoteFound(Strict):
    """按请求的次序一一对应。找到：found 为 true；没找到：reason 说为什么。"""

    dataset_id: str
    page: int
    found: bool
    reason: QuoteMiss | None = None
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class QuotesResultV1(Strict):
    contract: Literal["knowbot-data/v1"] = CONTRACT
    quotes: tuple[QuoteFound, ...]


@dataclass(frozen=True)
class Cell:
    """表格里的一格：原文，能读成数时的十进制字符串。"""

    raw: str
    number: str | None = None


@dataclass(frozen=True)
class Row:
    label: str
    cells: dict[str, Cell]


@dataclass(frozen=True)
class Sheet:
    name: str
    columns: tuple[str, ...]
    rows: tuple[Row, ...]

    def row(self, label: str) -> Row | None:
        return next((r for r in self.rows if r.label == label), None)


@dataclass(frozen=True)
class Tables:
    """一份表格数据解析出的所有表。"""

    sheets: tuple[Sheet, ...]

    def sheet(self, name: str | None) -> Sheet | None:
        if name is None:
            return self.sheets[0] if self.sheets else None
        return next((s for s in self.sheets if s.name == name), None)


class TableUnreadable(Exception):
    """这份数据不是表格，或读不出来（消息可以给运行方看）。"""


def table_page(sheet: Sheet, page: int) -> tuple[tuple[Row, ...], int]:
    """第 page 页的行与总页数；空表也算一页。

    一页不超过 TABLE_PAGE_CELLS 格、也不超过 PAGE_BYTES，至少一行。
    """
    bounds = _table_pages(sheet)
    if page > len(bounds):
        return (), len(bounds)
    start, end = bounds[page - 1]
    return sheet.rows[start:end], len(bounds)


def _table_pages(sheet: Sheet) -> list[tuple[int, int]]:
    per_page = max(1, TABLE_PAGE_CELLS // max(1, len(sheet.columns)))
    budget = PAGE_BYTES - _bytes(list(sheet.columns)) - 1_000  # 外层的键、出处等
    bounds: list[tuple[int, int]] = []
    start, size = 0, 0
    for i, row in enumerate(sheet.rows):
        values = {c: cell.raw for c, cell in row.cells.items() if cell.raw}
        n = _bytes({"label": row.label, "values": values}) + 2  # 行与行之间的「, 」
        if i > start and (size + n > budget or i - start >= per_page):
            bounds.append((start, i))
            start, size = i, 0
        size += n
    bounds.append((start, len(sheet.rows)))
    return bounds


def _bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True)
class Extracted:
    """一份数据抽出来的文字。"""

    text: str
    truncated: bool = False
    note: str | None = None


_PAGE_MARK = re.compile(r"^## Page ([0-9]+)$", re.MULTILINE)


_SPACES = re.compile(r"\s+")
_ALNUM = re.compile(r"[A-Za-z0-9]")


def _gap(found: re.Match[str]) -> str:
    """一段空白：两边都是字母或数字的留一个空格（分词），别的去掉。"""
    text, start, end = found.string, found.start(), found.end()
    between = (
        start > 0
        and end < len(text)
        and _ALNUM.match(text[start - 1])
        and _ALNUM.match(text[end])
    )
    return " " if between else ""


def normalize_quote(text: str) -> str:
    """全角半角统一（NFKC）；中文里的空白去掉（PDF 抽出的中文常被断行、插空格）。

    字母、数字之间的空白留一个：那是分词，去掉了「now here」就成了「nowhere」
    （审读 10-19）。
    """
    return _SPACES.sub(_gap, unicodedata.normalize("NFKC", text))


def pdf_pages(text: str) -> dict[int, str]:
    """按抽取时每页前面的「## Page N」切开：PDF 第几页 → 那一页的字。"""
    marks = list(_PAGE_MARK.finditer(text))
    ends = [m.start() for m in marks[1:]] + ([len(text)] if marks else [])
    return {
        int(m.group(1)): text[m.end() : end] for m, end in zip(marks, ends, strict=True)
    }


def find_quote(
    pages: dict[int, str], page: int, quote: str, *, truncated: bool = False
) -> QuoteMiss | None:
    """原话在第 page 页，或从第 page 页跨到下一页：回 None；不然回没找到的原因。

    只做规整后的精确匹配，不做「差几个字也算」（SDD 0036 第三节）。抽取被截断
    时，最后一页不全：在那一页找不到算「超出页码」（平台没抽到），不算引错
    （审读 10-19）。
    """
    wanted = normalize_quote(quote)
    letters = sum(1 for ch in wanted if unicodedata.category(ch).startswith("L"))
    if len(wanted) < QUOTE_MIN_CHARS or letters < QUOTE_MIN_LETTERS:
        return "too_short"
    if len(wanted) > QUOTE_MAX_CHARS:
        return "too_long"
    if not pages:
        return "no_pages"
    if page not in pages:
        return "page_out_of_range"
    # 接上下一页开头不到一段原话的长度：这样找到的一定是从第 page 页开始的
    here = normalize_quote(pages[page])
    following = normalize_quote(pages.get(page + 1, ""))[: len(wanted) - 1]
    if wanted in here + following:
        return None
    return "page_out_of_range" if truncated and page == max(pages) else "not_found"


def paginate(text: str, page: int) -> tuple[str, int]:
    """第 page 页（从 1 起）与总页数。空文字也算一页；页码超出就回空的最后一页之后。

    按字节切，一页不超过 PAGE_BYTES，在行尾切；PDF 抽出的「## Page N」行前面能切就切，
    年报的一页尽量不拆到两页里。各页接起来就是原文。
    """
    bounds = _text_pages(text)
    if page > len(bounds):
        return "", len(bounds)
    start, end = bounds[page - 1]
    return text[start:end], len(bounds)


def _text_pages(text: str) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    start = size = pos = 0
    for line in text.splitlines(keepends=True):
        n = len(line.encode("utf-8"))
        marker = line.startswith("## Page ")
        if size and (size + n > PAGE_BYTES or (marker and size >= PAGE_BYTES // 2)):
            bounds.append((start, pos))
            start, size = pos, 0
        while n > PAGE_BYTES:  # 一行就超了：按字切开
            cut = _fitting(line, PAGE_BYTES)
            bounds.append((pos, pos + cut))
            pos, line = pos + cut, line[cut:]
            start, n = pos, len(line.encode("utf-8"))
        pos += len(line)
        size += n
    if pos > start or not bounds:
        bounds.append((start, pos))
    return bounds


def _fitting(text: str, limit: int) -> int:
    """text 开头有几个字合起来不超过 limit 字节（至少一个）。"""
    total = 0
    for i, ch in enumerate(text):
        total += len(ch.encode("utf-8"))
        if total > limit:
            return max(1, i)
    return len(text)
