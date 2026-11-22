"""Continue a sealed comparison or ask its owner to adjudicate the exact versions."""

from decimal import Decimal

from app.application.workbench.ordinary_local_tools import graph_route
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import InteractionRejected, WorkbenchError
from app.domain.workbench.formal_execution import host_snapshot, invocation_definition
from app.domain.workbench.formal_method_plan_generated import (
    export_outputs,
    review_stage_id,
)
from app.domain.workbench.models import Budget, InteractionPrompt
from app.domain.workbench.ordinary_expert import ConsumerPlan
from app.domain.workbench.states import TaskState, WaitingReason

ADJUDICATE = "review_adjudication"
CHANGE_ROUTE = "review_route_confirmation"


async def append_round(repo, task, decision):
    policy = dict(task.get("execution_policy") or {})
    rounds = policy.get("formal_review_rounds", [])
    if decision["iteration"] != len(rounds) + 1 or decision["iteration"] > 2:
        raise CapabilityError("Review revision is stale")
    await repo.cas_task(
        str(task["id"]),
        expected_version=task["state_version"],
        execution_policy={**policy, "formal_review_rounds": [*rounds, decision]},
        current_step=int(task["current_step"]) + 1,
    )


async def after_comparison(
    repo, led, task, snapshot, step, outputs, capability_factory
):
    """Called under the parent task lock, after charging and sealing the exact comparison."""
    procedure = snapshot.draft.definition.procedure
    policy = procedure.collaboration[step.role].comparison
    verdict = outputs.get("needs_rework")
    if verdict is False or (verdict is None and policy.max_reworks == 0):
        return None
    tid = str(task["id"])
    if not isinstance(snapshot, ConsumerPlan):
        return (
            await led.transition(
                tid, TaskState.FAILED, reason={"code": "comparison_requires_revision"}
            )
        )["state"]
    from app.application.workbench.formal_orchestration_execution import (
        accepted_outputs,
    )

    _, artifacts = await accepted_outputs(
        repo, task, snapshot, int(task["current_step"]) + 1
    )
    iteration = step.iteration + 1
    root = next(iter(procedure.collaboration[step.role].review_requirements))
    requirement = procedure.collaboration[step.role].review_requirements[root]
    subject = {
        "reviewer": step.role,
        "round": step.iteration,
        "artifacts": artifacts,
        "snapshot_digest": task["pack_digest"],
    }
    reason = "复核要求返工，已达到本委托允许的轮数。"
    kind = ADJUDICATE
    if type(verdict) is not bool:
        reason = "本轮没有给出布尔型返工结论，需要你裁定。"
    elif iteration <= min(
        policy.max_reworks,
        (procedure.max_expansions - 1) // 3 - 1,
        procedure.max_steps // 3 - 1,
    ):
        try:
            caps = capability_factory(repo) if capability_factory else None
            if not caps or not caps.instruction_service:
                raise CapabilityError("Instruction execution is unavailable")
            owner = str(task["owner_actor_id"])
            svc = await caps.instruction_service(caps.formal_sources, owner)
            session = await repo.get_session(
                str(task["session_id"]), owner_actor_id=owner
            )
            currency = task["budget"]["currency"]
            producer = await graph_route(svc, owner, session, currency, producer=True)
            reviewer = await graph_route(
                svc, owner, session, currency, requirement=requirement
            )
            maximum = Decimal(producer["maximum"]) + 2 * Decimal(reviewer["maximum"])
            if maximum > Budget.model_validate(task["budget"]).available:
                reason = "剩余预算不足以覆盖完整的一轮返工与复核。"
            else:
                policies = {
                    root: digest(producer),
                    step.role: digest(reviewer),
                    review_stage_id(step.role, "blind"): digest(reviewer),
                }
                decision = {
                    "iteration": iteration,
                    "comparison": artifacts[-1],
                    "policies": policies,
                }
                previous_policies = (
                    snapshot._review_rounds[-1]["policies"]
                    if step.iteration
                    else snapshot.policies
                )
                if policies == previous_policies:
                    await append_round(repo, task, decision)
                    return (await led.transition(tid, TaskState.QUEUED))["state"]
                kind = CHANGE_ROUTE
                subject.update(
                    next_round=decision,
                    producer=producer,
                    reviewer=reviewer,
                    maximum=str(maximum),
                )
                reason = (
                    f"第 {iteration} 次返工将使用 {producer['provider']}/{producer['model']}，"
                    f"目的地 {producer['base_url']}，连接 {producer['credential_id']}，上限 {producer['maximum']}。"
                    f"两次复核使用 {reviewer['provider']}/{reviewer['model']}，每次上限 {reviewer['maximum']}，目的地 {reviewer['base_url']}，"
                    f"连接 {reviewer['credential_id']}，整轮上限 {currency} {maximum}。"
                )
        except (WorkbenchError, ValueError) as exc:
            reason = "本轮缺少获权且有报价的模型路由。" + str(exc)[:200]
    subject["reason"] = reason
    choice = (
        ("confirm_route", "确认本轮路由并继续")
        if kind == CHANGE_ROUTE
        else ("accept_delivery", "按当前版本接收成果")
    )
    await led.open_interaction(
        tid,
        kind=kind,
        waiting_reason=WaitingReason.APPROVAL,
        prompt=InteractionPrompt(
            title="委托人裁定",
            question=reason,
            subject=subject,
            options=[
                {"id": choice[0], "label": choice[1]},
                {"id": "stop", "label": "停止委托，保留已发生成本与产物"},
            ],
            evidence=artifacts,
        ),
        subject_digest=digest(subject),
    )
    return TaskState.WAITING


async def resolve_decision(led, task, interaction, response):
    from app.application.workbench.formal_orchestration_execution import (
        accepted_outputs,
    )

    repo = led.repo
    tid = str(task["id"])
    subject = interaction["prompt"]["subject"]
    snapshot = host_snapshot(task)
    decision = response.get("decision")
    if (
        not isinstance(snapshot, ConsumerPlan)
        or digest(subject) != interaction["subject_digest"]
        or decision not in {o["id"] for o in interaction["prompt"]["options"]}
        or task["pack_digest"] != subject["snapshot_digest"]
    ):
        raise InteractionRejected("Review the current decision and its exact evidence")
    accepted, artifacts = await accepted_outputs(
        repo, task, snapshot, int(task["current_step"]) + 1
    )
    if artifacts != subject["artifacts"]:
        raise InteractionRejected("Review evidence changed")
    iid = str(interaction["id"])
    reason = {"interaction": iid, "decision": decision, "reviewer": subject["reviewer"]}
    if decision == "stop":
        return await led.transition(
            tid, TaskState.CANCELLED, active_interaction_id=None, reason=reason
        )
    if interaction["kind"] == CHANGE_ROUTE:
        await append_round(repo, task, subject["next_round"])
        return await led.transition(
            tid, TaskState.QUEUED, active_interaction_id=None, reason=reason
        )
    exports = export_outputs(invocation_definition(snapshot), accepted)
    attempt = next(
        a
        for a in await repo.list_attempts(tid)
        if a["step_id"] == artifacts[-1]["step_id"]
    )
    final = await repo.put_artifact(
        task_id=tid,
        attempt_id=str(attempt["id"]),
        name="expert_delivery",
        kind="handback",
        content=exports,
    )
    await led.transition(
        tid, TaskState.QUEUED, active_interaction_id=None, reason=reason
    )
    await led.transition(tid, TaskState.RUNNING)
    return await led.transition(
        tid, TaskState.SUCCEEDED, terminal_result_ref=final["id"], reason=reason
    )
