"""把用户的表格数据转成带地址的格子（SDD 0030 §3.1）。

CSV 是一张表（表名 `data`）；Excel 每张工作表一张表（xlsx 用 openpyxl，xls 用 xlrd）。
第一行是列标签（日期写成 YYYY-MM-DD，xls 里的日期格也一样），第一列是行标签
（科目名）。重名的行、重名的列都按出现次序加 `#2`、`#3`（财报常把合并与母公司
并排，同一报告期出现两次；审读第 5 条）。每格给原文与能读成数的十进制字符串
（千分位、括号负数、全角负号都认；`-`、空白读成空）。文件是用户传的，当作不可信：
xlsx 先看解压后的大小，行数、列数、格子数都有上限。同一份内容（sha256）只解析一次。

端口是 application/ports/knowbots.TableExtractor。
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import math
import re
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.domain.workbench.data_read import Cell, Row, Sheet, Tables, TableUnreadable

MAX_XLSX_UNZIPPED = 512 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 400
MAX_CELLS = 200_000
MAX_EXPONENT = 30  # 绝对值大于 1e30 或小于 1e-30 的不当数（多半是读错了）
CACHE_TABLES = 16
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _label(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _decimal(number: Decimal) -> str | None:
    if not number.is_finite():
        return None
    if number and not -MAX_EXPONENT <= number.adjusted() <= MAX_EXPONENT:
        return None
    text = format(number.normalize(), "f")
    return "0" if text in {"-0", "0"} else text


def number_of(value: Any) -> str | None:
    """格子能读成数就回十进制字符串，读不成回 None。不猜单位、不处理百分号。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _decimal(Decimal(value))
    if isinstance(value, float):
        # Excel 存的是浮点：用最短的十进制写法（0.1 就是 0.1），不把二进制误差带进来
        return _decimal(Decimal(repr(value))) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, dt.date):
        return None
    text = str(value).strip().replace(",", "").replace("，", "").replace(" ", "")
    text = text.replace("−", "-").replace("－", "-")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if not text or text in {"-", "--", "—"} or not _NUMBER.match(text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return _decimal(-number if negative else number)


def _numbered(labels: Iterable[str]) -> list[str]:
    """重名的按出现次序加 `#2`、`#3`；空的留空（那一列不要）。"""
    seen: dict[str, int] = {}
    out = []
    for label in labels:
        if label:
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label}#{seen[label]}"
        out.append(label)
    return out


def _sheet(name: str, rows: Iterable[Iterable[Any]]) -> Sheet:
    it = iter(rows)
    header = next(it, None)
    if header is None:
        return Sheet(name=name, columns=(), rows=())
    labels = [_label(v) for v in list(header)[:MAX_COLUMNS]]
    columns = tuple(_numbered(labels[1:]))
    seen: dict[str, int] = {}
    out: list[Row] = []
    cells = 0
    for raw in it:
        values = list(raw)[: len(labels)]
        if not any(v not in (None, "") for v in values):
            continue
        label = _label(values[0]) if values else ""
        if not label:
            continue
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label}#{seen[label]}"
        row_cells = {}
        for column, value in zip(columns, values[1:], strict=False):
            if not column:
                continue
            row_cells[column] = Cell(raw=_label(value), number=number_of(value))
        cells += len(row_cells)
        if len(out) >= MAX_ROWS or cells > MAX_CELLS:
            raise TableUnreadable("the table is too large")
        out.append(Row(label=label, cells=row_cells))
    return Sheet(name=name, columns=tuple(c for c in columns if c), rows=tuple(out))


def _xls_value(cell: Any, datemode: Literal[0, 1]) -> Any:
    """xls 的日期格存的是序列号：转回日期，列标签才是 2024-12-31 而不是 45657。"""
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
            return cell.value
    return cell.value


def extract_tables(data: bytes, content_type: str) -> Tables:
    kind = content_type.split(";")[0].strip()
    try:
        if kind == "text/csv":
            text = data.decode("utf-8-sig", errors="replace")
            return Tables(sheets=(_sheet("data", csv.reader(io.StringIO(text))),))
        if kind.endswith("spreadsheetml.sheet"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist()) > MAX_XLSX_UNZIPPED:
                    raise TableUnreadable("the workbook is too large when unpacked")
            from openpyxl import load_workbook

            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                return Tables(
                    sheets=tuple(
                        _sheet(s.title, s.iter_rows(values_only=True))
                        for s in book.worksheets
                    )
                )
            finally:
                book.close()
        if kind == "application/vnd.ms-excel":
            import xlrd

            book = xlrd.open_workbook(file_contents=data, on_demand=True)
            try:
                sheets = []
                for index in range(book.nsheets):
                    sheet = book.sheet_by_index(index)
                    rows = (
                        [_xls_value(c, book.datemode) for c in sheet.row(r)]
                        for r in range(sheet.nrows)
                    )
                    sheets.append(_sheet(sheet.name, rows))
                    book.unload_sheet(index)
                return Tables(sheets=tuple(sheets))
            finally:
                book.release_resources()
    except TableUnreadable:
        raise
    except Exception as exc:  # 用户的坏文件：不把内部错误给出去
        raise TableUnreadable("the file could not be read as a table") from exc
    raise TableUnreadable("only CSV and Excel data can be read as a table")


class CachedTableExtractor:
    """按 sha256 缓存解析结果（只留最近 16 份）。"""

    def __init__(self, size: int = CACHE_TABLES) -> None:
        self._cache: OrderedDict[str, Tables] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    async def tables(self, *, data: bytes, content_type: str, sha256: str) -> Tables:
        key = f"{sha256}:{content_type}"
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        result = await asyncio.to_thread(extract_tables, data, content_type)
        with self._lock:
            self._cache[key] = result
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return result
