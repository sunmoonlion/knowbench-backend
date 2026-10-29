"""作为运行方跑孵化的一轮（专家孵化方案第八节，契约 knowbot-incubation/v1，knowlab 定）。

这里是工作台这一侧的解析模型与纯函数：收到的请求怎么读、交给模型的材料只有哪几样、
交回物合不合契约。契约的真源在 knowlab（`knowlab-app/contracts/knowbot-incubation/v1/`），
本仓只持锁（`knowbench-app/contracts/knowlab-incubation-lock.json`），测试核样例合提供方
schema。工作稿的格子形状归 knowlab（`app/domain/knowbots/workdraft.py`），这里原样转给模型、
不解析；交回物的规矩（建议只碰方法层的格子、不碰工程层，出处按种类引，自查改成的缺口在
缺口表里）照提供方逐条守，不合的这一轮记失败。
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.test_runs import Runner

CONTRACT = "knowbot-incubation/v1"
INCUBATION_ID = r"^[A-Za-z0-9._:-]{8,128}$"
ANSWER_ID = r"^[A-Za-z0-9._:-]{1,128}$"
ITEM_ID = r"^[A-Za-z0-9_-]{1,32}$"
STEP_ID = r"^[A-Za-z][A-Za-z0-9_-]{0,47}$"
FINAL = frozenset({"succeeded", "failed", "over_budget", "cancelled"})
MAX_REQUEST_BYTES = 1_000_000
_READING = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# 方法层的格子（同 knowlab workdraft 的路径）：「高级」、挑的数据、整份步骤列表不在里面
_STEP = STEP_ID[1:-1]
_CELL = r"[A-Za-z0-9_-]{1,32}"
_NAME = r"[^\s./\\\"'（）()、，,；;：:]{1,64}"
CELL_PATH = (
    r"^(name|tagline|scope|scope/(does|does_not|elsewhere)"
    rf"|steps/{_STEP}(/(title|does|why|uses|rules|ask_before|outputs|checks|on_fail))?"
    rf"|steps/{_STEP}/rules/{_CELL}"
    rf"|steps/{_STEP}/ask_before/{_CELL}"
    rf"|steps/{_STEP}/outputs/{_NAME}"
    rf"|steps/{_STEP}/checks/[0-9]{{1,2}})$"
)
ENGINEERING_KEYS = frozenset({"advanced", "method_text"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------- 收到的请求 ----------
class Seed(Strict):
    kind: Literal["question", "six_questions", "skill"]
    text: str = Field(min_length=1, max_length=60_000)


class Workdraft(Strict):
    """当前工作稿：格子（形状归 knowlab，这里不解析）与每格出处。"""

    body: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=5000)


class AnsweredGap(Strict):
    id: str = Field(pattern=ANSWER_ID)
    question: str = Field(min_length=1, max_length=2000)
    proposal: str = Field(default="", max_length=4000)
    answer: str = Field(min_length=1, max_length=4000)


class TableFormat(Strict):
    name: str = Field(default="", max_length=255)
    columns: tuple[str, ...] = Field(default=(), max_length=200)
    rows: tuple[str, ...] = Field(default=(), max_length=500)


class DataDescription(Strict):
    """一份数据在 knowbase 里的描述（0049 的几维），是描述不是内容；knowlab 照 knowbase 给的转来。"""

    subject: str | None = Field(default=None, max_length=64)
    doc_type: str | None = Field(default=None, max_length=64)
    period_start: str | None = Field(default=None, max_length=10)
    period_end: str | None = Field(default=None, max_length=10)
    data_format: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)


class DataFormat(Strict):
    """作者挑的一份数据的格式（0029 第 6.4 节、专家孵化方案第 8.2 节）：名字、文件类型、描述的
    几维、已就绪的读法、各表的表名与行列标签，没有内容。"""

    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="", max_length=128)
    description: DataDescription = DataDescription()
    readings: tuple[str, ...] = Field(default=(), max_length=10)
    tables: tuple[TableFormat, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def _reading_names(self) -> DataFormat:
        # 同提供方：读法是短的小写名（original、text、pages、tables……）
        if any(not _READING.fullmatch(r) for r in self.readings):
            raise ValueError("a reading is a short lowercase name")
        return self


class LatestTestStep(Strict):
    """报告里一步的结果：过没过、没过的验收名、一条一句为什么没过。"""

    step_id: str = Field(min_length=1, max_length=128)
    accepted: bool
    failed_checks: tuple[str, ...] = Field(default=(), max_length=64)
    details: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def _short(self) -> LatestTestStep:
        if any(len(x) > 200 for x in (*self.failed_checks, *self.details)):
            raise ValueError("each check name and detail is at most 200 characters")
        return self


class LatestTestExpectation(Strict):
    """一条期望核没核上；期望值本身不给（照着期望值改方法等于把答案写进规则）。"""

    artifact: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=256)
    kind: Literal["equals", "approx", "contains"]
    passed: bool


class LatestTestCase(Strict):
    case_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    question: str = Field(min_length=1, max_length=4000)
    status: Literal["passed", "failed", "needs_human", "error"]
    steps: tuple[LatestTestStep, ...] = Field(default=(), max_length=200)
    expectations: tuple[LatestTestExpectation, ...] = Field(default=(), max_length=64)
    note: str | None = Field(default=None, max_length=1000)


class LatestTest(Strict):
    """最近一次有结论的测试的报告（专家孵化方案第 8.4 节「带上测试报告让 AI 改」）：只有报告，
    成稿、各步交回物、测试数据都不在里面（0029 第 6.4 节）。"""

    number: int = Field(ge=1)
    draft_version: str = Field(min_length=1, max_length=32)
    status: Literal["passed", "failed"]
    cases: tuple[LatestTestCase, ...] = Field(default=(), max_length=50)


class Budget(Strict):
    limit: str = Field(pattern=r"^[0-9]{1,6}(\.[0-9]{1,2})?$")
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def _more_than_zero(self) -> Budget:
        # 上限是作者的钱（decisions ⑯）：零就是没给，不当成「不设上限」
        if Decimal(self.limit) <= 0:
            raise ValueError("the budget limit is more than zero")
        return self


class IncubationRequest(Strict):
    contract: Literal["knowbot-incubation/v1"]
    incubation_id: str = Field(pattern=INCUBATION_ID)
    owner_subject: str = Field(pattern=r"^[A-Za-z0-9._:@-]{1,512}$")
    action: Literal["draft", "revise"]
    instruction: str | None = Field(
        default=None, min_length=1, max_length=4000, exclude_if=lambda v: v is None
    )
    step_id: str | None = Field(
        default=None, pattern=STEP_ID, exclude_if=lambda v: v is None
    )
    seed: Seed
    # 用户先让自己的模型直接答过的回答（从 knowbench 转去的问题申请才有，请专家方案设计
    # 第 4.9 节第 6 条）：模型写的，只算提议
    reference: str | None = Field(
        default=None, min_length=1, max_length=20000, exclude_if=lambda v: v is None
    )
    workdraft: Workdraft = Workdraft()
    answers: tuple[AnsweredGap, ...] = Field(default=(), max_length=200)
    data_formats: tuple[DataFormat, ...] = Field(default=(), max_length=200)
    # 作者点「让 AI 改…」时勾了「带上测试报告」才有（专家孵化方案第 8.4 节）
    test_report: LatestTest | None = Field(default=None, exclude_if=lambda v: v is None)
    budget: Budget
    timeout_seconds: int = Field(ge=60, le=86400)
    # 谁跑：作者本人，在他的电脑上（decisions ⑯，形状同测试）
    runner: Runner

    @model_validator(mode="after")
    def _instruction_with_revise(self) -> IncubationRequest:
        if (self.action == "revise") != (self.instruction is not None):
            raise ValueError("revise needs the author's instruction, draft has none")
        if self.step_id is not None and self.action != "revise":
            raise ValueError("only revise names a step")
        if self.test_report is not None and self.action != "revise":
            raise ValueError("only revise carries a test report")
        if _bytes(self.model_dump(mode="json")) > MAX_REQUEST_BYTES:
            raise ValueError("the request is larger than the limit")
        return self

    def digest(self) -> str:
        """同一个 incubation_id 重复提交：内容相同回原状态，不同则 409。"""
        body = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def material(request: IncubationRequest) -> dict[str, Any]:
    """交给模型的只有契约列死的这几样（0029 第 6.4 节）：动作与指令、创意原文、当前工作稿、
    答过的缺口、数据的格式，还有作者的模型先前直接答过的回答（只算提议）与「让 AI 改」时带上的
    测试报告。作者是谁、孵化号、钱、电脑都不给模型。"""
    body = request.model_dump(
        mode="json",
        include={
            "action",
            "instruction",
            "step_id",
            "seed",
            "reference",
            "workdraft",
            "answers",
            "data_formats",
            "test_report",
        },
    )
    return body


def label(request: IncubationRequest) -> str:
    """作者在工作台里看到的这件委托的一句话（不是给模型的指令）。"""
    if request.action == "draft":
        return "孵化：AI 起草"
    return f"孵化：按指令改：{request.instruction or ''}"[:200]


def task_key(incubation_id: str) -> str:
    """这一轮在作者工作台里的委托幂等键。"""
    return "incubation-" + hashlib.sha256(incubation_id.encode()).hexdigest()[:40]


# ---------- 交回物（照提供方逐条守）----------
class SourceEntry(Strict):
    path: str = Field(pattern=CELL_PATH)
    kind: Literal["idea", "answer", "proposal"]
    quote: str | None = Field(
        default=None, min_length=1, max_length=4000, exclude_if=lambda v: v is None
    )
    ref: str | None = Field(
        default=None, pattern=ANSWER_ID, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def _cites_what_its_kind_says(self) -> SourceEntry:
        if (self.kind == "idea") != (self.quote is not None):
            raise ValueError("an idea source quotes the seed, and only it quotes")
        if (self.kind == "answer") != (self.ref is not None):
            raise ValueError("an answer source names the answered gap, and only it")
        return self


class Gap(Strict):
    id: str = Field(pattern=ITEM_ID)
    question: str = Field(min_length=1, max_length=2000)
    proposal: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _short_options(self) -> Gap:
        if any(not o.strip() or len(o) > 200 for o in self.options):
            raise ValueError("each option is 1 to 200 characters")
        return self


class Suggestion(Strict):
    id: str = Field(pattern=ITEM_ID)
    path: str = Field(pattern=CELL_PATH)
    op: Literal["set", "add", "remove", "move"]
    value: Any = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _value_fits_op(self) -> Suggestion:
        if self.op == "remove" and self.value is not None:
            raise ValueError("remove carries no value")
        if self.op == "move" and not (
            isinstance(self.value, int)
            and not isinstance(self.value, bool)
            and 0 <= self.value < 100
        ):
            raise ValueError("move carries the new position, from 0")
        if self.op in ("set", "add") and self.value is None:
            raise ValueError(f"{self.op} carries a value")
        if _engineering(self.value):
            raise ValueError("a suggestion does not touch the engineering layer")
        return self


class SelfCheck(Strict):
    suggestion_id: str = Field(pattern=ITEM_ID)
    turned_into_gap: str = Field(pattern=ITEM_ID)


class Handback(Strict):
    sources: tuple[SourceEntry, ...] = Field(default=(), max_length=2000)
    gaps: tuple[Gap, ...] = Field(default=(), max_length=200)
    suggestions: tuple[Suggestion, ...] = Field(default=(), max_length=500)
    self_check: tuple[SelfCheck, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> Handback:
        gaps = [g.id for g in self.gaps]
        suggestions = [s.id for s in self.suggestions]
        if len(gaps) != len(set(gaps)) or len(suggestions) != len(set(suggestions)):
            raise ValueError("gap ids and suggestion ids must be unique")
        for check in self.self_check:
            if check.turned_into_gap not in gaps:
                raise ValueError("a self check names a gap that is not in the gaps")
            if check.suggestion_id in suggestions:
                raise ValueError(
                    "a suggestion turned into a gap is no longer suggested"
                )
        return self


def _engineering(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in ENGINEERING_KEYS or _engineering(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_engineering(item) for item in value)
    return False


def _bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
