"""固定专家的计划：平台按作者定的顺序替它写的、完全约束的计划（SDD 0050）。

不调模型、不花预算、不许改。和动态计划共用回执、谱系、验收与收尾；产出名仍是作者起的名字
（`step.output_artifact`），所以 knowlab 测试里按名字写的断言、页面、报告都不用改。

规划模型交上来的计划只走 `parse_plan`（method-plan/v1、v2），这里的契约不在那里面：
模型交一份 fixed-plan 冒充作者顺序，解析就不过。
"""

from __future__ import annotations

import re

from app.domain.workbench.method_catalog import METHOD_ID
from app.domain.workbench.method_plan import (
    FixedMethodPlan,
    InputBinding,
    PlannedStep,
)
from app.domain.workbench.packs import ExpertPack

FIXED_PLAN_CONTRACT = "knowbench/fixed-plan/v1"
FIXED_PLAN_REASON = "作者指定的固定顺序（平台按专家包生成，不经模型规划）"


def fixed_plan(pack: ExpertPack) -> FixedMethodPlan | None:
    """按 workflow 顺序生成；表达不了的包回 None，调用方照旧路走（SDD 0050 第四节）。

    表达不了：没有步骤；步骤 id 或输入名不合方法 id 的格式；步骤 id 重复；某个输入名
    没有前面哪一步产出它；退回指向的不是本步的输入；开了竞争（第一期不开）。
    """
    if not pack.workflow:
        return None
    producer: dict[str, str] = {}  # 产出名 -> 前面最近一步产出它的步骤 id
    seen: set[str] = set()
    steps: list[PlannedStep] = []
    for index, step in enumerate(pack.workflow):
        if (
            step.mode != "single"
            or step.step_id in seen
            or not re.fullmatch(METHOD_ID, step.step_id)
        ):
            return None
        bindings = []
        for ref in step.input_refs:
            if ref not in producer or not re.fullmatch(METHOD_ID, ref):
                return None
            bindings.append(InputBinding(slot=ref, instance_id=producer[ref]))
        if len({b.slot for b in bindings}) != len(bindings):
            return None
        if step.on_reject == "back" and (
            step.back_to is None
            or step.back_to not in {b.instance_id for b in bindings}
        ):
            return None
        if len(set(step.tools)) != len(step.tools):
            return None
        steps.append(
            PlannedStep(
                instance_id=step.step_id,
                method_id=step.step_id,
                inputs=tuple(bindings),
                tools=tuple(step.tools),
                reason=f"作者指定的第 {index + 1} 步",
            )
        )
        seen.add(step.step_id)
        producer[step.output_artifact] = step.step_id
    last = pack.workflow[-1].step_id
    return FixedMethodPlan(
        contract=FIXED_PLAN_CONTRACT,
        parent_version=0,
        reason=FIXED_PLAN_REASON,
        steps=tuple(steps),
        final_outputs={last: last},
    )


def validate_fixed_plan(pack: ExpertPack, plan: FixedMethodPlan) -> None:
    """回执里的固定计划必须和按这一版包重新生成的一字不差；不一致就是被改过或包换了。"""
    if fixed_plan(pack) != plan:
        raise ValueError("fixed plan differs from the pinned expert pack")
