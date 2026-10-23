"""专家的数据需求声明与请专家时选的数据（SDD 0042 第五节，契约 knowbot-manifest/v1 的 `data_needs`）。

专家只是逻辑、不绑数据（10-23）：新格式的清单写声明——要请的人提供哪几样、什么格式、几份、
必填还是可选、表格类要有哪几张表哪几列。请专家时用户按声明逐样从本地知识库（A）或云端知识库
（B）选，必填的缺了不能开始；选中的钉进这件委托（workbench_task_data）。

声明由 knowmart 交付时按外层模型核过（测试中的草稿由 knowlab 核过），这里按同一套规矩再读一遍，
不合的当作清单坏了。没写声明的是旧格式，读作者绑的（knowbot_data 的旧路）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

NEED_KEY = r"^[a-z][a-z0-9_]{0,39}$"
DataFormat = Literal["pdf", "excel", "csv", "text"]
TABULAR: frozenset[str] = frozenset({"excel", "csv"})
MAX_NEEDS = 20
MAX_DATA_ITEMS = 64  # 一次请专家最多选这么多份（knowbase 一条运行授权的上限）
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# 文件名后缀 → （内容类型，格式）：与 knowbase、knowlab 收的几种一致
KINDS: dict[str, tuple[str, str]] = {
    ".pdf": ("application/pdf", "pdf"),
    ".xlsx": (XLSX, "excel"),
    ".xls": ("application/vnd.ms-excel", "excel"),
    ".csv": ("text/csv", "csv"),
    ".txt": ("text/plain", "text"),
    ".md": ("text/markdown", "text"),
}
_FORMAT_OF_TYPE = {content_type: fmt for content_type, fmt in KINDS.values()}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataTable(_Strict):
    sheet: str | None = Field(default=None, min_length=1, max_length=100)
    columns: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        min_length=1, max_length=50
    )


class DataNeed(_Strict):
    key: str = Field(pattern=NEED_KEY)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    formats: tuple[DataFormat, ...] = Field(min_length=1, max_length=4)
    required: bool = True
    min_count: int = Field(default=1, ge=1, le=MAX_DATA_ITEMS)
    max_count: int = Field(default=1, ge=1, le=MAX_DATA_ITEMS)
    tables: tuple[DataTable, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> DataNeed:
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("a format may be listed only once")
        if self.max_count < self.min_count:
            raise ValueError("max_count must not be below min_count")
        if self.tables and not set(self.formats) <= TABULAR:
            raise ValueError("tables can be required only of Excel or CSV data")
        return self


class DataChoice(_Strict):
    """请专家时选的一份：对上声明里的哪一样，从云端知识库（资料编号）还是本地知识库（路径）。"""

    need: str = Field(pattern=NEED_KEY)
    source: Literal["cloud", "local"]
    item_id: str | None = Field(default=None, min_length=1, max_length=64)
    path: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _one_place(self) -> DataChoice:
        if (self.source == "cloud") != (self.item_id is not None) or (
            self.source == "local"
        ) != (self.path is not None):
            raise ValueError("cloud data names an item_id, local data names a path")
        return self


class BadNeeds(ValueError):
    """清单里的声明不合规矩。"""


def needs_of(manifest: object) -> list[DataNeed]:
    """清单的数据需求声明；旧格式是空的。不合规矩抛 BadNeeds。"""
    raw = manifest.get("data_needs") if isinstance(manifest, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_NEEDS:
        raise BadNeeds("data_needs must be a list of at most 20")
    try:
        needs = [DataNeed.model_validate(item) for item in raw]
    except ValidationError:
        raise BadNeeds("a data need is invalid") from None
    keys = [n.key for n in needs]
    if len(keys) != len(set(keys)):
        raise BadNeeds("each data need needs its own key")
    if sum(n.max_count for n in needs) > MAX_DATA_ITEMS:
        raise BadNeeds("the data needs ask for too many items")
    return needs


def kind_of_name(name: str) -> tuple[str, str] | None:
    """按文件名认（内容类型，格式）；认不出是 None（本地知识库里的文件用）。"""
    dot = name.rfind(".")
    return KINDS.get(name[dot:].lower()) if dot >= 0 else None


def format_of(content_type: str) -> str | None:
    return _FORMAT_OF_TYPE.get(content_type.split(";")[0].strip().lower())


def choice_problems(needs: list[DataNeed], chosen: list[dict[str, Any]]) -> list[str]:
    """选的数据按声明核：每份对上声明里的一样、格式是那一样收的、每样份数在范围里、
    必填的不缺、同一份不选两次。chosen 每份带 need、format（认不出是 None）、identity
    （云端是资料编号，本地是路径）。回不合的地方（白话、英文，给网页按 code 说人话前也给模型看）。
    """
    by_key = {n.key: n for n in needs}
    counts = dict.fromkeys(by_key, 0)
    problems: list[str] = []
    seen: set[str] = set()
    for item in chosen:
        need = by_key.get(str(item.get("need")))
        if need is None:
            problems.append("a chosen item is for a need the expert does not declare")
            continue
        counts[need.key] += 1
        if item.get("format") not in need.formats:
            problems.append(f"{need.name}: a chosen item is not in a format it takes")
        identity = str(item.get("identity"))
        if identity in seen:
            problems.append(f"{need.name}: the same item is chosen twice")
        seen.add(identity)
    for key, n in counts.items():
        need = by_key[key]
        if (need.required or n) and not need.min_count <= n <= need.max_count:
            problems.append(
                f"{need.name}: choose {need.min_count} to {need.max_count}, {n} chosen"
            )
    return problems
