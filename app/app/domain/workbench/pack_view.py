"""专家包给用户看的那一面（PRD/apps/investment-expert.md 第四、五、十节）。

方法的原文是我们的东西，也是易损件：这里的任何函数都不把它放进返回值。
"不通过时怎么办"的说法只在这里生成一次，页面不自己拼，免得两处说得不一样。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.workbench.packs import ExpertPack, StepContract


def after_rejection(step: StepContract, pack: ExpertPack) -> dict[str, Any]:
    """这一步验收不过之后会怎样。先重做（有额度的话），额度用完再按去向走。"""
    finally_: str = step.on_reject
    back_index: int | None = None
    if step.on_reject == "back" and step.back_to:
        back_index = pack.index_of(step.back_to)
    else:
        finally_ = "human"  # 去向写的是重做、或退回却没写退到哪：额度用完都是交人
    parts: list[str] = []
    if step.max_reworks > 0:
        parts.append(f"重做，最多 {step.max_reworks} 次")
    backs = 0
    if finally_ == "back" and back_index is not None and step.max_backs > 0:
        backs = step.max_backs
        parts.append(f"退回第 {back_index + 1} 步，最多 {backs} 次")
    parts.append("停下来问你")
    if backs == 0 and finally_ == "back":
        finally_, back_index = "human", None  # 一次都不许退：等于交人
    return {
        "reworks": step.max_reworks,
        "then": finally_,
        "back_to": None if back_index is None else back_index + 1,
        "backs": backs,
        "text": "；仍不过，".join(parts),
    }


def step_view(step: StepContract, pack: ExpertPack, index: int) -> dict[str, Any]:
    uses = []
    for name in step.input_refs:
        source = next(
            (
                (i, pack.workflow[i])
                for i in range(index - 1, -1, -1)
                if pack.workflow[i].output_artifact == name
            ),
            None,
        )
        if source is not None:
            uses.append(
                {"artifact": name, "step": source[0] + 1, "title": source[1].title}
            )
    return {
        "index": index + 1,
        "step_id": step.step_id,
        "title": step.title,
        "summary": step.summary,
        "why": step.why,
        "uses": uses,
        "tools": list(step.tools),
        "returns": step.output_artifact,
        "checks": [{"label": rule.label or rule.kind} for rule in step.acceptance],
        "after_rejection": after_rejection(step, pack),
        "reserve": step.reserve,
    }


def pack_view(pack: ExpertPack) -> dict[str, Any]:
    answers = pack.answers
    return {
        "id": pack.profile_id,
        "version": pack.version,
        "name": pack.name or pack.title,
        "tagline": pack.tagline,
        "solves": answers.get("解决什么", ""),
        "does_not_solve": answers.get("不解决什么", ""),
        "input": answers.get("输入", ""),
        "output": answers.get("输出", ""),
        "who_judges": answers.get("谁做判断", ""),
        "on_failure": answers.get("失败怎么办", ""),
        "uses_our_data": pack.requires_tools,
        # 筹备中的专家：看得见、请不了
        "status": pack.status,
        "preparing_note": pack.preparing_note,
        "steps": [step_view(s, pack, i) for i, s in enumerate(pack.workflow)],
        # 每一步预留之和：预算低于它，专家连一遍都走不完
        "least_budget": str(sum_reserves(pack)),
    }


def sum_reserves(pack: ExpertPack) -> Decimal:
    return sum((Decimal(s.reserve) for s in pack.workflow), Decimal("0"))
