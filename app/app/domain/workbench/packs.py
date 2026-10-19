"""专家包与步骤契约（methods.md「专家包」、task-contract.md「步骤交回物」）。

专家包是数据：workflow[] 的每一步说清输入引用、方法文本、输出 schema、验收条目、不合格的去向。顾问按它逐步发 turn；
它不是 skill（做成 skill 等于整包交出去），也不写判断规则（F-POS-02）。内置四份：SMOKE（跑机制）、DATA_QUERY（问数五阶段，工具随 0006 来）、
FIN_REVIEW（财报体检七步，0008-info 段四）、FIN_FORECAST（财务分析与预测五步，0023）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.deliverables import DELIVERABLE_RULES
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
        # 数值类（0030 §3.3）：要查计算记录、向 knowlab 取格子，由工作台应用层判，不在 judge 里
        "computed",  # path 处每项带 compute_id 与 value：记录里有这一次计算，结果一致
        "identity",  # path 处每项带 left_id 与 right_id：两次计算的结果在容差内相等
        "sourced",  # path 处每项带 address 与 value：重新取这一格，与 value 一致
        # 引文（SDD 0036）：path 处每项带 quotes（一串 {dataset_id, page, text}），引了的都要在
        # 那份 PDF 的第 page 页找到；写了 field/equals 的，那样的项至少引一条
        "quoted",
    ]
    keys: tuple[str, ...] = ()
    path: str | None = None
    min_items: int = 1
    max_chars: int = 20000
    field: str | None = None
    equals: str | int | float | bool | None = None
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
        return self


# 要查计算记录或调 knowlab 的验收：由工作台应用层判（acceptance_numbers），不在纯函数 judge 里
NUMERIC_RULES = frozenset({"computed", "identity", "sourced", "quoted"})


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

DATA_QUERY = ExpertPack(
    pack_id="DATA_QUERY",
    version="1",
    profile_id="DATA_QUERY",
    title="问数（第 23–25 课五阶段）",
    name="问数",
    tagline="对已有的数据问一个数",
    # 2026-10-18 撤下（待办账 111，数据拓扑）：平台不带自己的投资逻辑与数据，用户看不到、请不了；
    # 留着只为历史委托还能显示。「财务分析与预测」改由我们以普通用户经 knowlab 孵化
    internal=True,
    requires_tools=True,
    dossier=(
        DossierBlock(
            key="answer",
            title="回答",
            kind="text",
            artifact="note",
            path="answer",
            section="answer",
        ),
        # 表取整理那一步验过的交回物，不取成稿里模型重抄的那一份
        DossierBlock(
            key="result",
            title="结果表",
            kind="table",
            artifact="table",
            path="table",
            section="data",
            also={"notes": "notes"},
        ),
        DossierBlock(
            key="definitions",
            title="查询用的口径",
            kind="table",
            artifact="sql",
            path="columns",
            section="verified",
            also={"sql": "sql"},
            columns=(
                ("name", "列"),
                ("definition", "口径"),
            ),
        ),
        DossierBlock(
            key="rows",
            title="查询带回的原样结果",
            kind="table",
            artifact="rows",
            path="rows",
            section="verified",
            folded=True,
            also={"row_count": "row_count", "data_version": "data_version"},
        ),
        DossierBlock(
            key="limitations",
            title="局限",
            kind="list",
            artifact="note",
            path="limitations",
            section="limits",
        ),
        DossierBlock(
            key="citations",
            title="出处",
            kind="list",
            artifact="note",
            path="citations",
            section="sources",
        ),
    ),
    workflow=(
        StepContract(
            step_id="rewrite",
            title="改写问题",
            summary="把问题改写成没有歧义的：查什么、哪个期间、什么口径",
            why="口语的问题常有歧义：哪一年、合并报表还是母公司。先说清楚查什么，免得查出来的不是你要的。",
            method_text="Rewrite the user's data question into an unambiguous analytical question: entities, metrics, period, grain, filters. "
            + JSON_ONLY,
            output_artifact="rewritten",
            output_schema={
                "question": "string",
                "entities": ["string"],
                "metrics": ["string"],
                "period": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("question", "metrics"),
                    message="缺改写后的问题或指标",
                    label="有改写后的问题和指标",
                ),
            ),
            max_reworks=1,
        ),
        StepContract(
            step_id="sql_generate",
            title="生成 SQL",
            summary="对照数据的结构写出查询，并说明每一列的口径",
            why="查询照着数据的真实结构写，每一列是什么口径写明白。你看得到它到底查了什么。",
            input_refs=("rewritten",),
            method_text="Using the schema tool, write ONE SQL statement answering the rewritten question. Explain each column's 口径. "
            + JSON_ONLY,
            tools=("describe_schema", "metric_definitions"),
            output_artifact="sql",
            output_schema={
                "sql": "string",
                "columns": [{"name": "string", "definition": "string"}],
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("sql",),
                    message="缺查询语句",
                    label="有查询语句",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="sql",
                    message="查询语句为空",
                    label="查询语句不是空的",
                ),
            ),
            max_reworks=2,
        ),
        StepContract(
            step_id="sql_execute",
            title="执行 SQL",
            summary="执行查询，原样带回结果和数据版本",
            why="查询的结果原样带回，不改数；同时记下数据版本，以后能复查。",
            input_refs=("sql",),
            method_text="Run the SQL with the run_sql tool. Return rows verbatim (max 200) and the data version reported by the tool. "
            + JSON_ONLY,
            tools=("run_sql",),
            output_artifact="rows",
            output_schema={"rows": [{}], "row_count": 0, "data_version": "string"},
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("rows", "data_version"),
                    message="缺结果或数据版本",
                    label="有结果和数据版本",
                ),
            ),
            on_reject="back",
            back_to="sql_generate",
            max_reworks=1,
        ),
        StepContract(
            step_id="normalize",
            title="整理结果",
            summary="统一单位、币种、期间的写法，标出缺的值",
            why="单位、币种、期间的写法统一之后才好比较。缺的值标出来，不补、不估。",
            input_refs=("rewritten", "rows"),
            method_text="Normalize rows into the answer table: units, currency, period labels; flag missing values. "
            + JSON_ONLY,
            output_artifact="table",
            output_schema={"table": [{}], "notes": ["string"]},
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("table",),
                    message="缺整理后的表",
                    label="有整理后的表",
                ),
            ),
            max_reworks=1,
        ),
        StepContract(
            step_id="final",
            title="成稿",
            summary="写出答案：每个数带数据版本和口径，写明局限；结论栏留空",
            why="答案里每个数都带出处和口径，并写明局限。只给事实，不给评级和买卖建议。",
            input_refs=("rewritten", "table"),
            method_text="Write the research note: the answer table, each number with its data version and 口径, limitations. Conclusion field stays empty for the user. "
            + "The deliverable file is the research note itself. "
            + DELIVERABLE_RULES
            + " "
            + JSON_ONLY,
            output_artifact="note",
            output_file="底稿/数据查询-成稿.md",
            output_schema={
                "answer": "string",
                "table": [{}],
                "citations": ["string"],
                "limitations": ["string"],
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
                    message="缺回答或出处",
                    label="有回答和出处",
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
        "解决什么": "对已入库数据集的口径明确的数值问题",
        "不解决什么": "预测、评级、买卖建议",
        "输入": "用户的问题，其中写明要查的指标与期间",
        "输出": "答案表：每个数带数据版本与口径，附局限；结论栏留空",
        "谁做判断": "用户。专家只取数、说明口径",
        "失败怎么办": "查询带不回结果时退回重写查询；成稿不合格时交人处理",
    },
)

# ---------------------------------------------------------------- 财报体检
# 先确认数据能不能用（摸底、勾稽），再取数、算指标、对照官方，最后成稿。
# 勾稽与对照这两步不给返工额度：不合格直接交人。返工的提示会催模型「按格式重交」，
# 在这两步上等于催它把不平的数改成平的。
_FIN_INPUT_CHARS = 24000
_FIN_DATASET = (
    "Every knowledge tool call in this step MUST pass the argument "
    "dataset=<profile.dataset>; never query the default dataset. "
)
_FIN_SQL = (
    "Write SQL in the dialect stated in the description of the run_sql tool and "
    "use table names alone, without schema or database prefix. "
)
_FIN_NUMBERS = (
    "Use only values returned by the tools in this step or given in the input "
    "artifacts; never recall or estimate a number. "
)

FIN_REVIEW = ExpertPack(
    pack_id="FIN_REVIEW",
    version="1",
    profile_id="FIN_REVIEW",
    title="财报体检（先验数据，再算指标，不下结论）",
    name="财报体检",
    tagline="先验数据，再算指标，不下结论",
    # 2026-10-18 撤下（待办账 111，数据拓扑）：平台不带自己的投资逻辑与数据，用户看不到、请不了；
    # 留着只为历史委托还能显示。「财务分析与预测」改由我们以普通用户经 knowlab 孵化
    internal=True,
    requires_tools=True,
    dossier=(
        DossierBlock(
            key="answer",
            title="回答",
            kind="text",
            artifact="note",
            path="answer",
            section="answer",
        ),
        # 两张表取各自那一步验过的交回物，不取成稿里模型重抄的那一份
        DossierBlock(
            key="metrics",
            title="指标表",
            kind="table",
            artifact="metrics",
            path="table",
            section="data",
            also={"data_version": "data_version"},
            columns=(
                ("display_name", "指标"),
                ("metric_name", "指标代码"),
                ("fiscal_year", "年度"),
                ("report_type", "报告"),
                ("basis", "口径"),
                ("value", "值"),
                ("unit", "单位"),
                ("applicable", "适用"),
                ("reason_if_not", "不适用的原因"),
                ("formula", "算法"),
                ("source", "来自"),
            ),
        ),
        DossierBlock(
            key="facts",
            title="取数表",
            kind="table",
            artifact="facts",
            path="table",
            section="data",
            folded=True,
            also={"data_version": "data_version"},
            columns=(
                ("display_name", "科目"),
                ("item", "字段"),
                ("fiscal_year", "年度"),
                ("report_type", "报告"),
                ("report_date", "报告期"),
                ("basis", "口径"),
                ("value", "值"),
                ("unit", "单位"),
                ("statement", "报表"),
            ),
        ),
        DossierBlock(
            key="reconcile",
            title="勾稽",
            kind="checks",
            artifact="reconcile",
            path="checks",
            section="verified",
            also={
                "continuity": "continuity",
                "unexplained_breaks": "unexplained_breaks",
            },
            columns=(
                ("rule_id", "编号"),
                ("rule", "规则"),
                ("periods_checked", "验了几期"),
                ("unbalanced", "不平的期数"),
                ("unbalanced_periods", "不平的期"),
            ),
        ),
        DossierBlock(
            key="crosscheck",
            title="对照官方",
            kind="coverage",
            artifact="crosscheck",
            section="verified",
        ),
        DossierBlock(
            key="mismatched",
            title="和公司披露的数对不上的",
            kind="table",
            artifact="crosscheck",
            path="mismatched",
            section="verified",
            columns=(
                ("fiscal_year", "年度"),
                ("item", "字段"),
                ("basis", "口径"),
                ("official", "公司披露的"),
                ("dataset", "数据集里的"),
                ("source_report", "出自"),
                ("page", "页"),
            ),
        ),
        DossierBlock(
            key="not_covered",
            title="没有对照到的",
            kind="table",
            artifact="crosscheck",
            path="not_covered",
            section="verified",
            columns=(("fiscal_year", "年度"), ("report_type", "报告")),
        ),
        DossierBlock(
            key="matched",
            title="和公司披露的数一致的",
            kind="table",
            artifact="crosscheck",
            path="matched",
            section="verified",
            folded=True,
            columns=(
                ("fiscal_year", "年度"),
                ("item", "字段"),
                ("basis", "口径"),
                ("value", "值"),
                ("source_report", "出自"),
                ("page", "页"),
            ),
        ),
        DossierBlock(
            key="observations",
            title="观察",
            kind="list",
            artifact="note",
            path="observations",
            section="notes",
        ),
        DossierBlock(
            key="caveats",
            title="提醒",
            kind="list",
            artifact="note",
            path="caveats",
            section="notes",
        ),
        DossierBlock(
            key="limitations",
            title="局限",
            kind="list",
            artifact="note",
            path="limitations",
            section="limits",
        ),
        DossierBlock(
            key="citations",
            title="出处",
            kind="list",
            artifact="note",
            path="citations",
            section="sources",
        ),
    ),
    workflow=(
        StepContract(
            step_id="scope",
            title="定范围",
            summary="认出公司、期间、报告类型",
            why="先把问的是哪家公司、哪几年、哪种报告认准。认错了，后面每一步都白做。",
            method_text=(
                "Rewrite the user's question into the scope of a financial statement "
                "review of ONE A-share listed company: the six-digit security code, the "
                "periods (fiscal years, or report dates for interim reports), the report "
                "types (年报, 中报, 一季报, 三季报) and the aspects asked about. "
                "If the question names no period, use the five most recent fiscal years "
                "and say so in `assumptions`. If the security code cannot be determined "
                "from the question, leave `security_code` empty; do not guess. "
                "Do not call any tool in this step. " + JSON_ONLY
            ),
            output_artifact="scope",
            output_schema={
                "company": "string",
                "security_code": "six digits",
                "periods": ["2023", "2024", "2025"],
                "report_types": ["年报"],
                "aspects": ["string"],
                "assumptions": ["string"],
                "question": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("security_code", "periods", "report_types"),
                    message="缺证券代码、期间或报告类型",
                    label="有证券代码、期间、报告类型",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="security_code",
                    message="问题里认不出证券代码",
                    label="认得出是哪家公司",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="periods",
                    min_items=1,
                    message="期间为空",
                    label="期间不是空的",
                ),
            ),
            on_reject="human",
            max_reworks=1,
        ),
        StepContract(
            step_id="profile",
            title="数据摸底",
            summary="确认有这家公司的数据、数据到哪一天、各期是什么口径",
            why="先看我们有没有这家公司的数据、数据到哪一天。没有数据就停下，不拿别处的数来凑。",
            input_refs=("scope",),
            method_text=(
                "Find the dataset and describe what it can support, before any number "
                "is used. 1) Call list_datasets and pick the dataset whose "
                "security_code equals scope.security_code. If there is none, the "
                "company has not been ingested: set `dataset` to null and "
                "`not_ingested` to true and stop; never pick another dataset and never "
                "use the default dataset. 2) With dataset=<that dataset> on every call: "
                "read dataset_metadata (all rows) and copy every note that matters for "
                "the periods in scope into `notes`, verbatim; read the statement rows "
                "in scope and list for each period its `basis` (原始披露, 追溯调整后 or "
                "未核实) and `verified`; read disclosure_calendar for the official "
                "disclosure date of each report. 3) List in `basis_breaks` every pair "
                "of adjacent periods in scope whose basis differs. "
                "Disclosure dates come ONLY from disclosure_calendar; the column "
                "aggregator_notice_date in statement rows is a third-party date and "
                "must not be reported as the disclosure date. " + JSON_ONLY
            ),
            tools=("list_datasets", "describe_schema", "metric_definitions", "run_sql"),
            output_artifact="profile",
            output_schema={
                "dataset": "string or null",
                "not_ingested": False,
                "security_code": "string",
                "data_version": "string, as reported by the tools",
                "as_of": "string, as reported by the tools",
                "notes": ["verbatim notes from dataset_metadata"],
                "periods": [
                    {
                        "fiscal_year": 2025,
                        "report_type": "年报",
                        "report_date": "2025-12-31",
                        "basis": "原始披露",
                        "verified": True,
                        "official_disclosed_date": "string or null",
                    }
                ],
                "basis_breaks": [
                    {"between": ["2020", "2021"], "basis": ["原始披露", "追溯调整后"]}
                ],
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("dataset", "data_version", "periods", "basis_breaks"),
                    message="缺数据集、数据版本、期间或口径断点",
                    label="有数据集、数据版本、期间、口径断点",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="dataset",
                    message="这家公司未入库：没有对应的数据集",
                    label="有对应的数据集",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="data_version",
                    message="没有数据版本",
                    label="有数据版本",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="periods",
                    min_items=1,
                    message="范围内没有任何一期的数据",
                    label="范围内至少有一期数据",
                ),
            ),
            missing_data=MissingData(),
            on_reject="human",
            max_reworks=1,
        ),
        StepContract(
            step_id="reconcile",
            title="勾稽",
            summary="三张表之间、前后两年之间对不对得上",
            why="三张表对不上，说明数据本身有问题；带着问题往下算，指标都不可信。所以对不上就停下来问你，不自己把数改平。",
            input_refs=("scope", "profile"),
            method_text=(
                "Check that the statements in scope are internally consistent before "
                "anything is computed from them. " + _FIN_DATASET + "1) Read the table "
                "reconciliation_rules. For EVERY rule run its check over every period "
                "in scope and report one entry in `checks`: how many periods were "
                "checked and how many were unbalanced (absolute difference above the "
                "tolerance given by the rule table, 1 yuan if none is given). "
                "2) Cross-period continuity: for each pair of consecutive annual "
                "reports in scope, compare the opening cash balance with the closing "
                "cash balance of the previous year; list every pair that differs in "
                "`continuity` with the difference. 3) For each entry in `continuity`, "
                "look for the explanation in profile.notes. If the notes explain it, "
                "quote the note in `note`. If they do not, add the fiscal year to "
                "`unexplained_breaks`. Report what the queries return. An unbalanced "
                "check is a finding, not a mistake to correct: never adjust, round "
                "away or omit a difference. " + _FIN_SQL + _FIN_NUMBERS + JSON_ONLY
            ),
            tools=("run_sql",),
            output_artifact="reconcile",
            output_schema={
                "checks": [
                    {
                        "rule_id": "R01",
                        "rule": "string",
                        "periods_checked": 5,
                        "unbalanced": 0,
                        "unbalanced_periods": ["report_date"],
                    }
                ],
                "continuity": [
                    {"fiscal_year": 2021, "diff": 0.0, "note": "quoted from the notes"}
                ],
                "unexplained_breaks": [],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("checks", "continuity", "unexplained_breaks"),
                    message="缺勾稽结果、跨期连续性或未解释的断点",
                    label="有勾稽结果、跨期连续性、未解释的断点",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="checks",
                    min_items=1,
                    message="没有做勾稽",
                    label="做了勾稽",
                ),
                AcceptanceRule(
                    kind="all_equal",
                    path="checks",
                    field="unbalanced",
                    equals=0,
                    message="有勾稽规则不平，不往下算",
                    label="每条勾稽规则都平",
                ),
                AcceptanceRule(
                    kind="list_empty",
                    path="unexplained_breaks",
                    message="有跨期断点在数据集说明里找不到解释",
                    label="跨期的断点都有解释",
                ),
            ),
            on_reject="human",
            max_reworks=0,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="extract",
            title="取数",
            summary="把问题要用到的报表科目按期取出来，每个数带口径",
            why="只取问题用得到的科目，每个数记下出自哪一期、什么口径。后面的指标只用这里取出来的数。",
            input_refs=("scope", "profile"),
            method_text=(
                "Extract the statement items needed for the aspects in scope, for the "
                "periods in scope. " + _FIN_DATASET + "Use describe_schema and the "
                "table field_dictionary for the meaning and unit of each column. Every "
                "row of `table` is one number and carries the basis of the statement "
                "row it was read from. At most 120 rows: keep the items the question "
                "needs. A value that is NULL in the dataset is reported as null, never "
                "as 0. " + _FIN_SQL + _FIN_NUMBERS + JSON_ONLY
            ),
            tools=("describe_schema", "run_sql"),
            output_artifact="facts",
            output_schema={
                "table": [
                    {
                        "fiscal_year": 2025,
                        "report_type": "年报",
                        "report_date": "2025-12-31",
                        "basis": "原始披露",
                        "statement": "income_statement",
                        "item": "operate_income",
                        "display_name": "营业收入",
                        "value": 0.0,
                        "unit": "元",
                    }
                ],
                "sql": ["every SQL statement that was run"],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("table", "data_version"),
                    message="缺取数表或数据版本",
                    label="有取数表和数据版本",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="table",
                    min_items=1,
                    message="取数表为空",
                    label="取数表不是空的",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="data_version",
                    message="没有数据版本",
                    label="有数据版本",
                ),
            ),
            on_reject="back",
            back_to="profile",
            max_reworks=1,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="metrics",
            title="算指标",
            summary="按数据集里登记的口径算指标；分母不成立或口径不同的，标为不适用",
            why="指标按数据集里登记的口径算，不临时发明算法。分母不成立的标「不适用」，不硬算。",
            input_refs=("scope", "profile", "facts"),
            method_text=(
                "Compute the metrics the question needs, using ONLY definitions "
                "returned by metric_definitions for this dataset. "
                + _FIN_DATASET
                + "1) Call metric_definitions. 2) For every needed metric whose "
                "definition says queryable=true, and when the tool query_metric is "
                "available, get the values with query_metric (metrics by name, "
                "filters on report_type and fiscal_year) and copy value, applicable "
                "and reason exactly as returned; set `source` to query_metric. Do not "
                "recompute these yourself. 3) For the other metrics, or when "
                "query_metric is not available, compute with run_sql following the "
                "definition; set `source` to run_sql and give the formula and the "
                "inputs. A ratio whose denominator is negative or within 1 yuan of "
                "zero is NOT applicable: set `applicable` to false, `value` to null and "
                "give the reason. A metric that needs two periods (an average of "
                "opening and closing balances, growth, a difference of ratios) is "
                "computed only when both periods have the same basis in "
                "profile.periods; otherwise set `applicable` to false with the reason "
                "口径不同. Ratios are reported as returned (0.25 means 25%); do not "
                "rescale. Do not state whether a value is good or bad. "
                + _FIN_SQL
                + _FIN_NUMBERS
                + JSON_ONLY
            ),
            tools=("metric_definitions", "query_metric", "run_sql"),
            output_artifact="metrics",
            output_schema={
                "table": [
                    {
                        "fiscal_year": 2025,
                        "report_type": "年报",
                        "basis": "原始披露",
                        "metric_name": "gross_margin",
                        "display_name": "毛利率",
                        "value": 0.0,
                        "unit": "比率",
                        "formula": "string, from the metric definition",
                        "inputs": {"operate_income": 0.0, "operate_cost": 0.0},
                        "applicable": True,
                        "reason_if_not": "",
                        "source": "query_metric or run_sql",
                    }
                ],
                "sql": ["every SQL statement that was run"],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("table", "data_version"),
                    message="缺指标表或数据版本",
                    label="有指标表和数据版本",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="table",
                    min_items=1,
                    message="指标表为空",
                    label="指标表不是空的",
                ),
            ),
            on_reject="rework",
            max_reworks=2,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="crosscheck",
            title="对照官方",
            summary="拿取到的数和公司年报里自己披露的数逐项对照",
            why="我们的数是采集来的。和公司年报里自己披露的数对一遍，对不上的要让你知道。",
            input_refs=("scope", "profile", "facts"),
            method_text=(
                "Compare the key items in facts with the figures the company itself "
                "published. " + _FIN_DATASET + "The table official_key_figures holds "
                "figures read from the annual reports, each with its basis, source "
                "report, disclosure date and page. For every annual period in scope "
                "and every item present in both places, compare the value in facts "
                "with the official figure OF THE SAME BASIS (tolerance 1 yuan). List "
                "agreements in `matched` and disagreements in `mismatched`, each with "
                "source report and page. A period with no official figure of the same "
                "basis is listed in `not_covered`, not in `mismatched`. `coverage` says "
                "in one sentence how many of the periods in scope were compared. A "
                "disagreement is a finding: never alter a value to make it agree. "
                + _FIN_SQL
                + _FIN_NUMBERS
                + JSON_ONLY
            ),
            tools=("run_sql",),
            output_artifact="crosscheck",
            output_schema={
                "matched": [
                    {
                        "fiscal_year": 2025,
                        "item": "operate_income",
                        "basis": "原始披露",
                        "value": 0.0,
                        "source_report": "string",
                        "page": 6,
                    }
                ],
                "mismatched": [
                    {
                        "fiscal_year": 2025,
                        "item": "string",
                        "basis": "string",
                        "official": 0.0,
                        "dataset": 0.0,
                        "source_report": "string",
                        "page": 6,
                    }
                ],
                "not_covered": [{"fiscal_year": 2026, "report_type": "中报"}],
                "coverage": "string",
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("matched", "mismatched", "coverage"),
                    message="缺一致项、不一致项或覆盖说明",
                    label="有一致项、不一致项、覆盖说明",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="coverage",
                    message="没有说明对照的覆盖范围",
                    label="说明了对照覆盖了哪些期",
                ),
                AcceptanceRule(
                    kind="list_empty",
                    path="mismatched",
                    message="有数字与公司披露的原文对不上，不往下写",
                    label="和公司披露的数都对得上",
                ),
            ),
            on_reject="human",
            max_reworks=0,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="note",
            title="成稿",
            summary="只用前面各步的结果写底稿：列事实和提醒，结论栏留空",
            why="底稿只写前面各步验过的事实和提醒。结论栏留空：下结论是你的事。",
            input_refs=(
                "scope",
                "profile",
                "reconcile",
                "facts",
                "metrics",
                "crosscheck",
            ),
            method_text=(
                "Write the working paper in Chinese from the input artifacts only; do "
                "not call any tool. `answer` answers the user's question with the "
                "numbers from facts and metrics, each with its period and basis. "
                "`observations` state facts and the direction of change only; no "
                "evaluation words such as 好, 差, 优秀, 值得, 健康. `caveats` MUST "
                "contain: every basis break in profile.basis_breaks that lies within "
                "or next to the periods used, with its explanation quoted from "
                "profile.notes; every period whose basis is 未核实; every metric "
                "marked not applicable, with the reason; every entry of "
                "reconcile.continuity; every accounting note in profile.notes that "
                "affects comparability of the periods used. If there is nothing of a "
                "kind, do not invent one; if there is no caveat at all, say "
                "'本次范围内没有口径断点、未核实期间或不适用的指标' as the only caveat. "
                "State an accounting fact only when profile.notes states it; never "
                "from your own knowledge. `citations` name the dataset, the data "
                "version, and for official figures the source report and page. "
                "No forecast, valuation, rating, target price, or advice to buy, sell "
                "or hold: when the question asks for one, `answer` says that this "
                "review does not give investment advice and lists what the data shows. "
                "`conclusion` stays an empty string: the user writes the conclusion. "
                "The deliverable file is the working paper itself. "
                + DELIVERABLE_RULES
                + " "
                + JSON_ONLY
            ),
            output_artifact="note",
            output_file="底稿/财报体检-成稿.md",
            output_schema={
                "answer": "string",
                "tables": [{"title": "string", "rows": [{}]}],
                "observations": ["string"],
                "caveats": ["string"],
                "limitations": ["string"],
                "citations": ["string"],
                "data_version": "string",
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
                    keys=("answer", "caveats", "citations", "data_version"),
                    message="缺回答、提醒、出处或数据版本",
                    label="有回答、提醒、出处、数据版本",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="answer",
                    message="回答为空",
                    label="回答不是空的",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="caveats",
                    min_items=1,
                    message="没有提醒",
                    label="有提醒",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="citations",
                    min_items=1,
                    message="没有出处",
                    label="有出处",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="data_version",
                    message="没有数据版本",
                    label="有数据版本",
                ),
                AcceptanceRule(
                    kind="blank",
                    path="conclusion",
                    message="结论栏必须留空",
                    label="结论栏是空的",
                ),
                AcceptanceRule(
                    kind="no_positioning_advice",
                    message="含有评级、目标价或买卖建议的措辞",
                    label="没有评级、目标价、买卖建议",
                ),
                AcceptanceRule(
                    kind="max_chars",
                    max_chars=30000,
                    message="底稿过长",
                    label="底稿没有超长",
                ),
            ),
            on_reject="human",
            max_reworks=1,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
    ),
    answers={
        "解决什么": "对一家已入库的 A 股非金融公司，按指定期间给出带出处、带口径、先过勾稽的财务体检底稿",
        "不解决什么": "预测、估值、评级、买卖或仓位建议；没有入库的公司；附注与经营数据",
        "输入": "用户的问题，其中含公司与期间",
        "输出": "底稿：数据摸底、勾稽结果、取数表、指标表、与官方的对照、观察与提醒；结论栏留空",
        "谁做判断": "用户。专家只列事实、算指标、提示口径问题",
        "失败怎么办": "公司未入库、勾稽不平、与官方对不上时，不往下算，交人处理",
    },
)

# ---------------------------------------------------------------- 财务分析与预测
# 设计：k8s 仓 tree-build/SDD/modules/0023-fin-forecast.md 第五节。把口径定死，看清过去，按说得清的方法
# 外推。与财报体检的区别在编排：定范围找不到数据集就交人；分析不过退回取数；预测方法写死、不给返工——
# 返工提示会催模型改方法、凑结果，与勾稽、对照官方不给返工同理。计算一律在 SQL 里做，结果列用口径名
# 作别名，评测按列名比对。所有者 2026-10-13 要演示，切成可用、版本 1（原定本地十题过线后再切）。
_FC_DATASET = (
    "Every knowledge tool call in this step MUST pass the argument "
    "dataset=<scope.dataset>; never query the default dataset. "
)
_FC_SQL = (
    "Do every computation in SQL (sums, averages, minimums, maximums, ratios, "
    "growth rates); never compute a number in your head. Name every computed column "
    "exactly after its metric_name from metric_definitions (for example AS gmv), "
    "name the period column `period`, and write periods in the formats declared by "
    "dataset_metadata.period_formats (months YYYY-MM, quarters YYYY-Qn). Keep ratios "
    "as fractions between 0 and 1 and do not round them in SQL. Dimension columns "
    "keep their field names. "
)
_FC_PERIODS = (
    "Use complete periods only. If dataset_metadata declares complete_start and "
    "complete_end, use exactly that range; otherwise treat as incomplete the leading "
    "and trailing periods whose volume is below half the median of the other "
    "periods, and say that this is how you decided. An incomplete period is never "
    "read as a decline. "
)

FIN_FORECAST = ExpertPack(
    pack_id="FIN_FORECAST",
    version="2",
    profile_id="FIN_FORECAST",
    title="财务分析与预测（先定口径，再看走势，带区间预测）",
    name="财务分析与预测",
    tagline="看清收入、成本、现金流的走势，给出可核对的预测",
    # 2026-10-18 撤下（待办账 111，数据拓扑）：平台不带自己的投资逻辑与数据，用户看不到、请不了；
    # 留着只为历史委托还能显示。「财务分析与预测」改由我们以普通用户经 knowlab 孵化
    internal=True,
    requires_tools=True,
    dossier=(
        DossierBlock(
            key="answer",
            title="回答",
            kind="text",
            artifact="note",
            path="answer",
            section="answer",
        ),
        DossierBlock(
            key="series",
            title="取数表",
            kind="table",
            artifact="series",
            path="rows",
            section="data",
            also={"columns": "columns", "data_version": "data_version"},
        ),
        DossierBlock(
            key="forecast",
            title="预测表",
            kind="table",
            artifact="forecast",
            path="rows",
            section="data",
            also={"method": "method", "assumptions": "assumptions", "window": "window"},
        ),
        DossierBlock(
            key="quarters",
            title="按季度合计",
            kind="table",
            artifact="forecast",
            path="quarters",
            section="data",
        ),
        DossierBlock(
            key="findings",
            title="分析发现",
            kind="list",
            artifact="analysis",
            path="findings",
            section="verified",
        ),
        DossierBlock(
            key="limitations",
            title="局限",
            kind="list",
            artifact="note",
            path="limitations",
            section="limits",
        ),
        DossierBlock(
            key="citations",
            title="出处",
            kind="list",
            artifact="note",
            path="citations",
            section="sources",
        ),
    ),
    workflow=(
        StepContract(
            step_id="scope",
            title="定范围",
            summary="认出数据集，把问题落到口径、期间和粒度上；说不清是哪家就先问你，数据里没有的口径照实列出",
            why="口径先定，后面的数和预测才有意义；同一个问题，口径不同，答案可以差一倍。说不清是哪家、或找不到能回答的数据，就停下问你。",
            method_text=(
                "Turn the user's question into a fixed scope before any number is "
                "used. 1) Call list_datasets, and metric_definitions for each dataset "
                "listed. This version supports only datasets whose metrics are "
                "aggregated per period (kind=aggregate), such as sales or order data; "
                "it does not support financial statement datasets whose metrics are "
                "computed per report row (kind=row). Keep only the datasets that have "
                "kind=aggregate metrics for what the question asks. 2) Choose among "
                "the kept datasets. If the question names the company or the dataset, "
                "take that one. If it does not (for example 这个公司 or 它) and exactly "
                "one kept dataset fits, take it and say so in `assumptions`. If more "
                "than one fits, ask the user which one, with the dataset titles as the "
                "options, and use the answer. If none fits, or what the question asks "
                "is only in a financial statement dataset, set `dataset` to null, say "
                "why in `assumptions` and stop; never use a dataset that does not fit. "
                "3) With dataset=<that dataset> on every call: read dataset_metadata "
                "(all rows) and copy into `notes`, verbatim, every note about periods, "
                "truncation, missing data, keys and status rules; call describe_schema. "
                "4) Map the question onto metrics returned by metric_definitions and "
                "list each in `metrics` with its name and its kind copied exactly as "
                "returned (a missing kind is written as unknown); use only names that "
                "exist there. Every quantity the question asks for whose definition "
                "has kind=unavailable, or that has no definition at all, goes into "
                "`unavailable` with the reason; never substitute another field for it. "
                "5) Set `period` to the range and grain the question asks for, never "
                "finer than the data is stored in. If the question names no range, or "
                "a vague one such as 近两年, use complete_start to complete_end and say "
                "so in `assumptions`; if the dataset declares no complete period, use "
                "at most the latest 36 periods and say so. Copy complete_start and "
                "complete_end into `complete_period`. 6) If the question asks for a "
                "forecast, put the periods to forecast in `forecast.horizon` (for the "
                "next quarter or quarters: the quarters after the last complete "
                "quarter, counting a quarter that is only partly complete as the "
                "first); otherwise set `forecast` to null. Do not ask the user about "
                "definitions that metric_definitions settles; state your reading in "
                "`assumptions`. " + JSON_ONLY
            ),
            tools=("list_datasets", "describe_schema", "metric_definitions", "run_sql"),
            output_artifact="scope",
            output_schema={
                "question": "string",
                "dataset": "string or null",
                "data_version": "string, as reported by the tools",
                "period": {"start": "YYYY-MM", "end": "YYYY-MM", "grain": "month"},
                "complete_period": {"start": "YYYY-MM", "end": "YYYY-MM"},
                "metrics": [
                    {"name": "metric_name from metric_definitions", "kind": "aggregate"}
                ],
                "unavailable": [{"name": "string", "reason": "string"}],
                "forecast": {"horizon": ["YYYY-Qn"]},
                "notes": ["verbatim notes from dataset_metadata"],
                "assumptions": ["string"],
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=(
                        "dataset",
                        "data_version",
                        "period",
                        "metrics",
                        "unavailable",
                    ),
                    message="缺数据集、数据版本、期间、口径或数据里没有的口径",
                    label="有数据集、数据版本、期间、口径",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="dataset",
                    message="没有找到能回答这个问题的数据集",
                    label="认出了能回答的数据集",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="data_version",
                    message="没有数据版本",
                    label="有数据版本",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="metrics",
                    min_items=1,
                    message="问题没有落到任何口径上",
                    label="问题落到了口径上",
                ),
                AcceptanceRule(
                    kind="all_equal",
                    path="metrics",
                    field="kind",
                    equals="aggregate",
                    message="这份数据不是按期汇总的经营数据：本版只支持这一类（例如样例电商），财报类请用「财报体检」",
                    label="口径都是按期汇总的（本版支持的数据）",
                ),
            ),
            on_reject="human",
            max_reworks=0,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="pull",
            title="取数",
            summary="按口径把每期的数取出来，每列写明出自哪张表、什么口径；不完整的期排除并说明",
            why="后面的分析和预测都建立在这张表上：每列从哪来、怎么算，先摆出来给你核对。",
            input_refs=("scope",),
            method_text=(
                "Pull the series the scope needs. "
                + _FC_DATASET
                + "For each metric in scope.metrics (by its name), query it per period "
                "over scope.period at scope.period.grain, never splitting data into a "
                "finer grain than it is stored in, following its definition "
                "(expression and row_filter) exactly. Return one row per period with "
                "one column per metric. List every period inside scope.period that is "
                "not complete in `excluded_periods` with the reason, and leave it out "
                "of `rows`. In `columns` give, for each column, its name, its display "
                "name and its source (table, field and filter). Put every SQL "
                "statement you ran in `sql`. "
                + _FC_SQL
                + _FC_PERIODS
                + _FIN_NUMBERS
                + JSON_ONLY
            ),
            tools=("describe_schema", "run_sql"),
            output_artifact="series",
            output_schema={
                "columns": [
                    {
                        "name": "gmv",
                        "display_name": "成交额",
                        "source": "table.field and filter",
                    }
                ],
                "rows": [{"period": "YYYY-MM", "gmv": 0.0}],
                "excluded_periods": [{"period": "YYYY-MM", "reason": "string"}],
                "sql": ["every SQL statement that was run"],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("columns", "rows", "excluded_periods", "sql", "data_version"),
                    message="缺列说明、取数结果、排除的期、查询语句或数据版本",
                    label="有列说明、取数结果、排除的期、查询语句、数据版本",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="rows",
                    min_items=1,
                    message="取数结果为空",
                    label="取到了数",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="sql",
                    min_items=1,
                    message="没有交回执行过的查询语句",
                    label="交回了执行过的查询语句",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="data_version",
                    message="没有数据版本",
                    label="有数据版本",
                ),
            ),
            on_reject="rework",
            max_reworks=1,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="analyze",
            title="分析",
            summary="看走势、结构和季节性，每条发现都带它依据的数和查询",
            why="先把过去说清楚，预测才有依据；每条发现都能回到数据上核对。",
            input_refs=("scope", "series"),
            method_text=(
                "Describe the past before anyone forecasts it. "
                + _FC_DATASET
                + "Using the series, and further queries where needed: 1) trend: "
                "change from period to period and, where the same period a year "
                "earlier is in range, year on year; 2) structure, only where the "
                "question needs it: shares by region or category and how they changed; "
                "3) seasonality only if the series covers at least two complete years; "
                "otherwise say in a finding that it cannot be judged from this data. "
                "Each finding states the fact, the numbers it rests on, and the SQL "
                "that produced those numbers. At least two findings. Describe only; do "
                "not say whether something is good or bad. "
                + _FC_SQL
                + _FIN_NUMBERS
                + JSON_ONLY
            ),
            tools=("run_sql",),
            output_artifact="analysis",
            output_schema={
                "findings": [
                    {
                        "text": "string",
                        "numbers": ["string"],
                        "sql": "the SQL that produced the numbers",
                    }
                ],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("findings",),
                    message="缺分析发现",
                    label="有分析发现",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="findings",
                    min_items=2,
                    message="分析发现少于两条",
                    label="至少两条发现",
                ),
            ),
            on_reject="back",
            back_to="pull",
            max_reworks=0,  # 不先重做本步：不过就直接退回取数，回来仍不过交人
            max_backs=1,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="forecast",
            title="预测",
            summary="按固定的方法外推：先回测看误差，再给每期的预测值、区间和假设；残缺的季度用实际加预测",
            why="预测一定带区间和假设，只给一个数等于没说；方法写死、不合格不返工，不让模型改方法凑结果。",
            input_refs=("scope", "series", "analysis"),
            method_text=(
                "Forecast with the fixed method below; do not choose another method. "
                + _FC_DATASET
                + "Method: moving average. 1) Complete periods come from "
                "scope.complete_period and series.excluded_periods. The window is the "
                "6 latest complete periods; if fewer than 6 exist use all of them; if "
                "fewer than 3 exist, do not forecast: leave `rows` empty and say why in "
                "`method` and `assumptions`. 2) Backtest first when there are at least "
                "12 complete periods: move the window back by 6 periods, forecast the "
                "6 periods after it with the same rule, compare with the actual "
                "values, and report the mean absolute percentage error and how many "
                "actual values fall inside the range. Otherwise set `backtest` to null "
                "and say why in `assumptions`. 3) For each period in the horizon, the "
                "point forecast is the average of the window and the range is the "
                "minimum and the maximum of the window. The forecast starts at the "
                "first period after the last complete period; a period that exists in "
                "the data but is incomplete is forecast, not read. 4) Quarters: a "
                "quarter's point is the sum of its monthly points and its range is the "
                "sum of the monthly lows and highs; a quarter with complete months "
                "adds their actual values instead of forecasts for those months, and "
                "lists them in `actual_months`. Compute everything in SQL; name the "
                "columns forecast_point, forecast_low and forecast_high, and the period "
                "column period. Give at least two assumptions (for example: the recent "
                "level persists; no new promotion, policy or definition change). Never "
                "call a forecast certain, a promise or a target. "
                + _FC_SQL
                + _FIN_NUMBERS
                + JSON_ONLY
            ),
            tools=("run_sql",),
            output_artifact="forecast",
            output_schema={
                "method": "string",
                "window": {"start": "YYYY-MM", "end": "YYYY-MM", "count": 6},
                "excluded_periods": [{"period": "YYYY-MM", "reason": "string"}],
                "assumptions": ["string"],
                "rows": [
                    {
                        "period": "YYYY-MM",
                        "forecast_point": 0.0,
                        "forecast_low": 0.0,
                        "forecast_high": 0.0,
                    }
                ],
                "quarters": [
                    {
                        "period": "YYYY-Qn",
                        "forecast_point": 0.0,
                        "forecast_low": 0.0,
                        "forecast_high": 0.0,
                        "actual_months": ["YYYY-MM"],
                    }
                ],
                "backtest": {
                    "window": {"start": "YYYY-MM", "end": "YYYY-MM"},
                    "mape": 0.0,
                    "inside": 0,
                    "total": 6,
                },
                "sql": ["every SQL statement that was run"],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("method", "window", "assumptions", "rows", "backtest", "sql"),
                    message="缺方法、窗口、假设、预测值、回测或查询语句",
                    label="有方法、窗口、假设、预测值、回测、查询语句",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="assumptions",
                    min_items=2,
                    message="假设少于两条",
                    label="至少两条假设",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="rows",
                    min_items=1,
                    message="没有预测值",
                    label="有预测值",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="sql",
                    min_items=1,
                    message="没有交回执行过的查询语句",
                    label="交回了执行过的查询语句",
                ),
            ),
            on_reject="human",
            max_reworks=0,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
        StepContract(
            step_id="writeup",
            title="成稿",
            summary="把范围、取数、发现、预测写成一篇可核对的稿子，落到项目文件里",
            why="给你的是一篇能逐条核对的稿子：数都有出处，预测带区间和假设，缺的数据和截断照实写进局限。",
            input_refs=("scope", "series", "analysis", "forecast"),
            method_text=(
                "Write the deliverable in Chinese from the input artifacts only; do not "
                "call any data tool (writing the files is the only tool use). `answer` answers the question in plain words first: the "
                "trend and, when a forecast was asked, the point and the range for each "
                "forecast period with the method in one sentence. `limitations` MUST "
                "contain: every item of scope.unavailable with its reason; every "
                "excluded or incomplete period with its reason; that the forecast "
                "assumes the recent level persists, with the backtest error (or why "
                "there was no backtest); any note in scope.notes that limits the "
                "reading. `citations` name the dataset, its title and the data version. "
                "No valuation, rating, target price, or advice to buy, sell or hold; a "
                "forecast is an extrapolation, never a promise or a target. "
                "The deliverable file holds the full write-up: answer, the series with "
                "sources, the findings, the forecast table with method, window, "
                "assumptions and backtest, limitations and sources. "
                + DELIVERABLE_RULES
                + " "
                + JSON_ONLY
            ),
            output_artifact="note",
            output_file="底稿/财务分析与预测-成稿.md",
            output_schema={
                "answer": "string",
                "limitations": ["string"],
                "citations": ["string"],
                "data_version": "string",
            },
            acceptance=(
                AcceptanceRule(
                    kind="json_object",
                    message="交回物不是 JSON 对象",
                    label="交回的格式对",
                ),
                AcceptanceRule(
                    kind="required_keys",
                    keys=("answer", "limitations", "citations", "data_version"),
                    message="缺回答、局限、出处或数据版本",
                    label="有回答、局限、出处、数据版本",
                ),
                AcceptanceRule(
                    kind="non_empty",
                    path="answer",
                    message="回答为空",
                    label="回答不是空的",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="limitations",
                    min_items=1,
                    message="没有写局限",
                    label="写了局限",
                ),
                AcceptanceRule(
                    kind="list_min",
                    path="citations",
                    min_items=1,
                    message="没有出处",
                    label="有出处",
                ),
                AcceptanceRule(
                    kind="no_positioning_advice",
                    message="含有评级、目标价或买卖建议的措辞",
                    label="没有评级、目标价、买卖建议",
                ),
                AcceptanceRule(
                    kind="max_chars",
                    max_chars=30000,
                    message="成稿过长",
                    label="成稿没有超长",
                ),
            ),
            on_reject="human",
            max_reworks=1,
            input_max_chars=_FIN_INPUT_CHARS,
        ),
    ),
    answers={
        "解决什么": "对一份已入库的经营或财务数据，按定死的口径看清过去的走势与结构，并用说得清的方法给出带区间、带假设、先回测的预测",
        "不解决什么": "估值、评级、买卖建议；数据里没有的口径（例如没有成本就不算毛利）；复杂的统计模型",
        "输入": "一个问题，例如：近两年按月收入怎么走，预测下两个季度",
        "输出": "成稿：回答、取数表（每列带出处）、分析发现、预测表（方法、区间、假设、回测）、局限、出处",
        "谁做判断": "用户。专家只摆数、说走势、给带假设的预测",
        "失败怎么办": "找不到能回答的数据集或范围认不出时停下问你；分析不过退回取数一次；预测不合格不返工，直接交你看",
    },
)

BUILTIN_PACKS: dict[str, ExpertPack] = {
    p.profile_id: p for p in (SMOKE, DATA_QUERY, FIN_REVIEW, FIN_FORECAST)
}


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
