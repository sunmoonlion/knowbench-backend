"""一样数据的描述：照 knowbase 的契约 `data-request/v1` 写的同一份模型。

真源在 `knowbase-app/contracts/data-request/v1/`，本仓 `contracts/` 的锁钉住它的摘要；
并列检出 knowbase-app 时，测试核这里生成的 schema 与真源一致（不计说明文字）。

清单 v2 的专家没有数据需求声明（decisions ⑭）：委托第 0 步模型读专家逻辑和用户问题，
用 `request_data` 按这个形状提出要哪些数据，knowbase 解析，用户确认、可改（SDD 0043
第三节）；运行中再要数据也是这样。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

DocType = Literal[
    "disclosure",
    "research",
    "news",
    "briefing",
    "policy",
    "statistics",
    "other",
    "annual_report",
    "semiannual_report",
    "q1_report",
    "q3_report",
    "interim_announcement",
    "prospectus",
    "listing_announcement",
    "ir_activity",
    "ir_qa",
    "research_morning",
    "research_macro",
    "research_strategy",
    "research_industry",
    "research_company",
    "research_quant",
    "research_market",
    "research_fixed_income",
    "research_commodity",
    "research_derivatives",
    "research_fund",
    "research_fx",
    "research_reits",
    "research_esg",
    "research_other",
    "financial_news",
    "recommended_news",
    "negative_news",
    "brief",
    "law",
    "administrative_regulation",
    "departmental_rule",
    "normative_document",
    "local_regulation",
    "judicial_interpretation",
    "policy_interpretation",
    "business_rule",
    "regulatory_letter",
    "statistical_bulletin",
    "statistical_data",
    "census",
    "webpage",
    "other_table",
    "other_text",
]
DataFormat = Literal["pdf", "excel", "csv", "text", "markdown", "html"]
Edition = Literal["full", "summary", "english"]
Topic = Literal[
    "results",
    "ipo",
    "refinancing",
    "dividend",
    "meetings",
    "governance",
    "operations",
    "restructuring",
    "equity",
    "trading",
    "intermediary",
    "supervision",
]
Industry = Literal[
    "石油石化",
    "煤炭",
    "有色金属",
    "电力及公用事业",
    "钢铁",
    "基础化工",
    "建筑",
    "建材",
    "轻工制造",
    "机械",
    "电力设备",
    "国防军工",
    "汽车",
    "商贸零售",
    "消费者服务",
    "家电",
    "纺织服装",
    "医药",
    "食品饮料",
    "农林牧渔",
    "银行",
    "非银行金融",
    "房地产",
    "交通运输",
    "电子元器件",
    "通信",
    "计算机",
    "传媒",
    "综合",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataTable(_Strict):
    """表格类数据要有的一张表与几列（按表头的字认，不按位置）；CSV 不写表名。"""

    sheet: str | None = Field(default=None, min_length=1, max_length=100)
    columns: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        min_length=1, max_length=50
    )


class PeriodRule(_Strict):
    """固定期间（start、end）；定期报告最近 N 个完整的报告期（latest + count，
    可加截至任务日期已披露）；其余类型最近 N 个月内披露的（recent + months），
    或不限期间（none）。定期报告的期间是报告期，其余类型的期间是披露时间窗
    （SDD 0049）。"""

    kind: Literal["fixed", "latest", "recent", "none"]
    start: date | None = None
    end: date | None = None
    count: int | None = Field(default=None, ge=1, le=10)
    disclosed_by_task_date: bool = False
    months: int | None = Field(default=None, ge=1, le=60)


class SourceQuery(_Strict):
    """Confirmed query against a registered supplier; credentials never travel here."""

    channel: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$")
    service: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    arguments: dict[str, JsonValue] = Field(max_length=50)
    request_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class DataRequest(_Strict):
    """一样数据：主体（证券代码、机构编号；空是不限主体）、类型（细类，或只写大类）、
    期间规则、版本形态、格式、份数、是不是必需、要的读法、来源限制、多候选时的优先
    策略、表格类的表与列；事项与行业写了就是缩小范围；名字、说明（为什么要）、口径
    只展示。"""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    subject: str | None = Field(default=None, min_length=1, max_length=64)
    doc_type: DocType
    period: PeriodRule
    formats: tuple[DataFormat, ...] = Field(min_length=1, max_length=6)
    required: bool = True
    min_count: int = Field(default=1, ge=1, le=200)
    max_count: int = Field(default=1, ge=1, le=200)
    reading: Literal["original", "text", "pages", "table"] = "original"
    sources: tuple[Literal["disclosure", "collected", "upload", "output"], ...] = Field(
        default=(), max_length=4
    )
    priority: Literal["public_first", "upload_first", "newest"] | None = None
    tables: tuple[DataTable, ...] = Field(default=(), max_length=20)
    caliber: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=500)
    edition: Edition = "full"
    topics: tuple[Topic, ...] = Field(default=(), max_length=12)
    industry: Industry | None = None
    source_query: SourceQuery | None = None
