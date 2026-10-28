"""专家包与步骤契约（methods.md「专家包」、task-contract.md「步骤交回物」）。

专家包是数据：workflow[] 的每一步说清输入引用、方法文本、输出 schema、验收条目、不合格的去向。顾问按它逐步发 turn；
它不是 skill（做成 skill 等于整包交出去），也不写判断规则（F-POS-02）。内置只剩 SMOKE（跑机制，不给用户看）：
「问数」「财报体检」「财务分析与预测」三份读平台的旧数据工具，10-28 撤了（0027、decisions ⑰）；专家都是用户的分身。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.tool_policy import (
    ToolPolicy,
    ToolPolicyOverride,
    merge_policy,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptanceRule(Strict):
    """确定性检查。kind 决定怎么判；judge 全是纯函数（application/workbench/acceptance.py）。"""

    kind: Literal[
        "json_object",
        "required_keys",
        "non_empty",
        "list_min",
        "no_positioning_advice",
        "max_chars",
        "all_equal",  # path 处是列表，每一项的 field 都等于 equals
        "list_empty",  # path 处是列表，而且是空的
        "blank",  # path 处没有内容：缺失、null 或空白字符串
        # 数值类（0030 §3.3）：要查计算记录、回 knowbase 取数与原文，由工作台应用层判，不在 judge 里
        "computed",  # path 处每项带 compute_id 与 value：记录里有这一次计算，结果一致
        "identity",  # path 处每项带 left_id 与 right_id：两次计算的结果在容差内相等
        "sourced",  # path 处每项带 address 与 value：重新取这一格，与 value 一致
        # 引文（SDD 0036）：path 处每项带 quotes（一串 {dataset_id, page, text}），引了的都要在
        # 那份 PDF 的第 page 页找到；写了 field/equals 的，那样的项至少引一条
        "quoted",
        # 结构（审读 10-19：第 3 稿第 6 步空交也能过）：path 处是列表，每项都有 keys；写了
        # field/values 的，那一项只能取 values 里的一个
        "each_has",
        # 对得上上游（同上）：path（加上 also 列的几处）各项的 field，与上游交回物 source
        # （「交回物名.点号路径」，项是对象时取 source_field）一一对应，不多不少不重
        "covers",
    ]
    keys: tuple[str, ...] = ()
    path: str | None = None
    min_items: int = 1
    max_chars: int = 20000
    field: str | None = None
    equals: str | int | float | bool | None = None
    values: tuple[str | int | float | bool, ...] = ()  # each_has：field 只能取这几个
    source: str | None = None  # covers：上游「交回物名.点号路径」
    source_field: str | None = None  # covers：上游的项是对象时取哪一项
    also: tuple[str, ...] = ()  # covers：同一交回物里一起算的别的几处列表
    tolerance: float = Field(default=0, ge=0)  # 数值类：差的绝对值不超过它算一致
    expression: str | None = None  # computed：记录里的算式必须就是这个（比语法树）
    message: str = ""  # 不通过时的说明
    label: str = ""  # 这条验收的名字，通过时也显示

    @model_validator(mode="after")
    def _numeric_rules_name_a_path(self) -> AcceptanceRule:
        if self.kind in NUMERIC_RULES and not self.path:
            raise ValueError(f"acceptance {self.kind} needs a path")
        if self.expression is not None and self.kind != "computed":
            raise ValueError("only a computed acceptance can require an expression")
        if self.kind == "each_has" and not self.path:
            raise ValueError("acceptance each_has needs a path")
        if self.values and (self.kind != "each_has" or not self.field):
            raise ValueError("only each_has with a field can limit values")
        if self.kind == "covers" and not (
            self.field and self.source and "." in self.source
        ):
            raise ValueError(
                "acceptance covers needs a field and a source artifact.path"
            )
        if (self.source or self.source_field or self.also) and self.kind != "covers":
            raise ValueError("only covers reads an upstream artifact")
        return self


OUTPUT_FILE_CHARS = 256


def safe_relative_path(path: str) -> bool:
    """POSIX 相对路径：不以 / 开头、没有盘符、反斜杠、NUL，没有空段与 . .. 段，不太长。"""
    if not path or len(path) > OUTPUT_FILE_CHARS or "\\" in path or "\x00" in path:
        return False
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


# 要查计算记录或回 knowbase 查的验收：由工作台应用层判（acceptance_numbers），不在纯函数 judge 里
NUMERIC_RULES = frozenset({"computed", "identity", "sourced", "quoted", "covers"})


class MissingData(Strict):
    """这一步的交回物里，哪几项说的是「这家公司没有入库」。"""

    flag: str = "not_ingested"  # 为真就是没有入库
    dataset: str = "dataset"  # 空的也算没有入库
    code: str = "security_code"  # 证券代码写在哪


class AskBefore(Strict):
    id: str
    question: str
    options: tuple[dict[str, str], ...] = ()  # {"id","label"}；空 = 只自由作答
    allow_other: bool = True


class StepContract(Strict):
    step_id: str
    step_version: str = "1"
    title: str
    summary: str = ""  # 给用户看的一句白话。方法的原文不给用户看
    # 为什么要做这一步，也是给用户看的一句白话。专家处理期间一直摆在页面上（所有者 2026-10-04）
    why: str = ""
    input_refs: tuple[str, ...] = ()  # 上游步骤的 artifact 名（取最新版本）
    method_text: str
    tools: tuple[
        str, ...
    ] = ()  # 这一步允许的 MCP 工具（服务端按 token 鉴权；此处用于 turn 输入说明）
    output_artifact: str  # 交回物名
    output_schema: dict[str, Any] = Field(
        default_factory=dict
    )  # 给模型看的结构说明（JSON 对象）
    acceptance: tuple[AcceptanceRule, ...] = ()
    on_reject: Literal["rework", "back", "human"] = "rework"
    back_to: str | None = None
    max_reworks: int = 1
    # 这一步最多把专家退回前面几次。用完了仍然不过，就停下来问用户（所有者 2026-10-04 定：1 次）。
    # 没有这个上限的话，专家可以在两步之间一直来回；现在又没有预算上限拦着
    max_backs: int = 1
    mode: Literal["single", "compete"] = (
        "single"  # compete 第一期不开（C-A10），位置留好
    )
    reserve: str = "0.05"  # 这一步预留的预算（币种随 Task）
    input_max_chars: int = 8000  # 每个上游交回物放进 turn 输入的字符上限；超出会注明
    # 这一步会发现「没有数据」的，写明怎么认。认出来就不重做（重做也不会有数据），直接问用户
    missing_data: MissingData | None = None
    # 工具级审批的步骤级覆盖：只能比专家包更严或更具体（approval.md「工具级：策略」）
    tool_policy: ToolPolicyOverride | None = None
    # 开步前工作台先问用户的问题（approval.md「Task 级：澄清·工作台发起」）；答案进 compose
    ask_before: tuple[AskBefore, ...] = ()
    # 这一步给人看的成稿写到项目目录里的哪个文件（相对项目目录；0015 §3.3）。结构化结果仍按
    # output_schema 回在正文里做机器验收；文件才是给用户的那一份
    output_file: str | None = None

    @model_validator(mode="after")
    def _output_file_stays_inside(self) -> StepContract:
        """成稿路径只许项目内的相对路径（审读 10-19 第 4 条）：它会拼进读测试电脑文件的请求，
        代理的白名单是最后一道，不是唯一一道。"""
        if self.output_file is not None and not safe_relative_path(self.output_file):
            raise ValueError("output_file must be a plain relative path inside the project")
        return self


class Scope(Strict):
    """这位专家接什么、不接什么。第一步先按它判问题是不是给这位专家的（受理关口，账 130）。

    不写就从六问的「解决什么」「不解决什么」里取，六问没写的再用名字与一句话介绍：分身的清单不用改也有这道关口。
    """

    does: str  # 接什么：一两句白话
    does_not: str = ""  # 明确不接的
    elsewhere: str = ""  # 不接的去哪里问


class DossierBlock(Strict):
    """底稿里的一块：取哪个交回物的哪一项，画成什么。"""

    key: str
    title: str
    kind: Literal["text", "list", "table", "tables", "checks", "coverage"]
    artifact: str  # 交回物名
    path: str | None = None  # 交回物里的哪一项；不给就是整个
    section: Literal["answer", "data", "verified", "notes", "limits", "sources"]
    folded: bool = False  # 默认折叠
    # 这一块还要带上的项：名字 → 路径
    also: dict[str, str] = Field(default_factory=dict)
    # 表的列：（交回物里的名字，给用户看的名字），按这个顺序排。不给就按行里有什么排什么
    columns: tuple[tuple[str, str], ...] = ()


class ExpertPack(Strict):
    pack_id: str
    version: str
    profile_id: str
    title: str
    name: str = ""  # 给用户看的名字。代号不给用户看
    tagline: str = ""
    internal: bool = False  # 只给我们自己测机制用，不进清单
    # 筹备中的专家：进清单让人看得见、读得到它要做什么，但请不了（所有者 2026-10-13 定：先占位，做好再切）
    status: Literal["ready", "preparing"] = "ready"
    preparing_note: str = ""  # 筹备中时给用户的一句话：在做什么、什么时候能用
    dossier: tuple[DossierBlock, ...] = ()

    def deliverable_files(self) -> tuple[str, ...]:
        """这位专家会往项目里写哪些成稿文件（0015 §3.3）。"""
        return tuple(step.output_file for step in self.workflow if step.output_file)

    workflow: tuple[StepContract, ...]
    # 2026-10-12 起由 tool_policy 三档表代替；留着只为老数据读得出来，运行时不看它
    auto_allow: dict[str, bool] = Field(
        default_factory=lambda: {
            "file_change_in_project": True,
            "command_escalation": False,
        }
    )
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    answers: dict[str, str] = Field(default_factory=dict)  # 六问
    requires_tools: bool = False
    scope: Scope | None = None

    def intake_scope(self) -> Scope | None:
        """受理时按什么判。只给我们自己测机制用的专家不设关口（所有者 2026-10-17：演示时问了无关问题，模型报错）。"""
        if self.internal:
            return None
        if self.scope is not None:
            return self.scope
        does = self.answers.get("解决什么") or "；".join(
            part for part in (self.name or self.title, self.tagline) if part
        )
        return Scope(does=does, does_not=self.answers.get("不解决什么", ""))

    @model_validator(mode="after")
    def _steps_only_stricter(self) -> ExpertPack:
        for st in self.workflow:
            merge_policy(self.tool_policy, st.tool_policy)  # 放宽的在这里就报错
        return self

    def step(self, index: int) -> StepContract | None:
        return self.workflow[index] if 0 <= index < len(self.workflow) else None

    def policy_for(self, step: StepContract | None) -> ToolPolicy:
        return merge_policy(self.tool_policy, step.tool_policy if step else None)

    def index_of(self, step_id: str) -> int:
        for i, s in enumerate(self.workflow):
            if s.step_id == step_id:
                return i
        raise KeyError(step_id)


JSON_ONLY = (
    "Reply with ONE JSON object only, no markdown fences, no commentary before or after it. "
    "Do not modify any files for this step unless the step says so."
)

SMOKE = ExpertPack(
    pack_id="SMOKE",
    version="1",
    profile_id="SMOKE",
    title="机制烟测：两步一返工",
    name="机制烟测",
    tagline="验证专家逐步驾驶同一个 Codex 的机制",
    internal=True,
    workflow=(
        StepContract(
            step_id="plan",
            title="拆步",
            summary="把问题拆成不超过三步",
            method_text="Read the user's question. Produce a short plan of at most 3 concrete steps to answer it using only files in the current project directory. "
            + JSON_ONLY,
            output_artifact="plan",
            output_schema={"plan": ["string"], "assumptions": ["string"]},
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("plan",),
                    message="缺 plan",
                    label="有计划",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="plan",
                    min_items=1,
                    message="plan 为空",
                    label="计划不是空的",
                ),
            ),
            on_reject="rework",
            max_reworks=1,
        ),
        StepContract(
            step_id="answer",
            title="按计划作答",
            summary="按计划作答，每个说法带出处",
            input_refs=("plan",),
            method_text="Follow the plan artifact given below. Answer the user's question. Every factual claim must carry a citation to a file path in the project or the literal string 'none'. Leave the conclusion field empty: the user fills conclusions. "
            + JSON_ONLY,
            output_artifact="answer",
            output_file="底稿/烟测-回答.md",
            output_schema={
                "answer": "string",
                "citations": ["string"],
                "conclusion": "",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("answer", "citations"),
                    message="缺 answer 或 citations",
                    label="有回答和出处",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="answer",
                    message="answer 为空",
                    label="回答不是空的",
                ),
                AcceptanceRule(
                    kind="no_positioning_advice",
                    path="answer",
                    message="含有评级、目标价或买卖建议的措辞",
                    label="没有评级、目标价、买卖建议",
                ),
            ),
            on_reject="human",
            max_reworks=1,
        ),
    ),
    answers={
        "解决什么": "验证顾问逐步驾驶同一个 Codex 的机制",
        "不解决什么": "任何真实投研问题",
    },
)

BUILTIN_PACKS: dict[str, ExpertPack] = {p.profile_id: p for p in (SMOKE,)}


def listed_packs() -> list[ExpertPack]:
    """给用户看的专家。只给我们自己测机制用的不在里面。"""
    return [p for p in BUILTIN_PACKS.values() if not p.internal]


def find_pack(profile_id: str, version: str | None = None) -> ExpertPack | None:
    pack = BUILTIN_PACKS.get(profile_id)
    if pack is None or (version not in (None, "", pack.version)):
        return None
    return pack


def current_pack_version(profile_id: str) -> str:
    """新委托绑哪一版：没点名就是现在上架的那一版。没有这位专家时记 "1"，受理时照样打回。"""
    pack = BUILTIN_PACKS.get(profile_id)
    return pack.version if pack is not None else "1"
