"""固定专家的计划回执（SDD 0050 第二、四节）。

平台按作者的顺序替固定专家写第 1 版、也是唯一一版计划回执。**不开尝试**：不进时间线、
不改委托状态、不进交回的「做过什么」，固定专家对用户看起来和原来一样；回执的
`attempt_id` 填 `FIXED_PLAN_ATTEMPT`，对应 artifact 的 attempt_id 为空。

只给还没开始走步骤的新委托写；已经按旧路走了的委托不补（不改钉正在执行的任务）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import pack_for_task
from app.application.workbench.execution_plan import is_dynamic
from app.domain.workbench.errors import MethodPlanInvalid, StaleStateVersion
from app.domain.workbench.fixed_plan import fixed_plan, validate_fixed_plan
from app.domain.workbench.method_plan import (
    PLAN_ARTIFACT,
    PLAN_STEP,
    AcceptedMethodPlan,
    FixedMethodPlan,
)
from app.domain.workbench.packs import ExpertPack

log = logging.getLogger(__name__)

FIXED_PLAN_ATTEMPT = "author-order"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def fixed_pack_digest(task: dict[str, Any], pack: ExpertPack) -> str:
    """分身委托上钉着清单摘要就用它；内置包的委托没有，按包的规范 JSON 算。"""
    pinned = task.get("pack_digest")
    if isinstance(pinned, str) and _DIGEST.fullmatch(pinned):
        return pinned
    body = json.dumps(
        pack.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


async def accept_fixed_plan(
    repo: WorkbenchStore, task_id: str, *, expected_version: int
) -> AcceptedMethodPlan | None:
    """写回执并把 `accepted_plan_version` 设成 1；不该写或写不了就回 None，委托照旧路走。"""
    async with repo.transaction():
        task = await repo.get_task(task_id, for_update=True)
        if int(task["state_version"]) != expected_version:
            raise StaleStateVersion(
                "task changed before the fixed plan", task_id=task_id
            )
        if is_dynamic(task) or task.get("accepted_plan_version") is not None:
            return None
        pack = pack_for_task(task)
        if pack is None:
            return None
        steps = {s.step_id for s in pack.workflow}
        started = any(
            a.get("step_id") in steps or a.get("step_id") == PLAN_STEP
            for a in await repo.list_attempts(task_id)
        )
        if int(task["current_step"]) != 0 or started:
            return None  # 已经按旧路开始了：不补回执
        plan = fixed_plan(pack)
        if plan is None:
            log.warning(
                "fixed plan cannot express pack %s v%s; task %s stays on the old path",
                pack.profile_id,
                pack.version,
                task_id,
            )
            return None
        receipt = AcceptedMethodPlan(
            task_id=task_id,
            attempt_id=FIXED_PLAN_ATTEMPT,
            pack_digest=fixed_pack_digest(task, pack),
            version=1,
            proposal=plan,
        )
        artifact = await repo.put_artifact(
            task_id=task_id,
            attempt_id=None,
            name=PLAN_ARTIFACT,
            kind="method_plan",
            content=receipt.model_dump(mode="json"),
        )
        if artifact["version"] != 1:
            raise MethodPlanInvalid("plan history differs from the accepted pointer")
        await repo.cas_task(
            task_id, expected_version=expected_version, accepted_plan_version=1
        )
        return receipt


def validate_fixed_receipt(
    task: dict[str, Any], artifact: dict[str, Any], plan: AcceptedMethodPlan
) -> None:
    """`plan_receipts.validate_receipt` 遇到固定计划时调：核包、摘要、版本与平台写的标记。"""
    assert isinstance(plan.proposal, FixedMethodPlan)
    pack = pack_for_task(task)
    if (
        pack is None
        or is_dynamic(task)
        or plan.version != 1
        or artifact["attempt_id"] is not None
        or plan.attempt_id != FIXED_PLAN_ATTEMPT
        or plan.pack_digest != fixed_pack_digest(task, pack)
        or plan.requirements_digest is not None
    ):
        raise MethodPlanInvalid("fixed plan receipt does not match its task")
    try:
        validate_fixed_plan(pack, plan.proposal)
    except ValueError as exc:
        raise MethodPlanInvalid(str(exc)) from None
