"""Ledger implementation for versioned method plans; no model calls or dispatch.

The fixed runner remains closed to dynamic experts. These operations are used by
the later planner integration through Ledger, under the existing transaction.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.application.workbench.execution_plan import (
    accepted_results,
    execution_for_task,
)
from app.application.workbench.plan_receipts import catalog_for_task, validate_receipt

# Existing callers keep this read API; ownership is the shared read-only module.
from app.application.workbench.plan_receipts import read_plan as read_plan
from app.domain.workbench.errors import (
    IdempotencyConflict,
    MethodPlanInvalid,
    StaleStateVersion,
)
from app.domain.workbench.knowbots import V2_TOOLS
from app.domain.workbench.method_plan import (
    PLAN_ARTIFACT,
    PLAN_STEP,
    AcceptedMethodPlan,
    MethodPlan,
    validate_plan,
)
from app.domain.workbench.states import ATTEMPT_TERMINAL, AttemptState, TaskState

if TYPE_CHECKING:
    from app.application.ports.workbench import WorkbenchStore
    from app.application.workbench.ledger import Ledger


async def _ready(
    repo: WorkbenchStore, task: dict[str, Any], expected_version: int
) -> None:
    if int(task["state_version"]) != expected_version:
        raise StaleStateVersion("task changed before planning", task_id=str(task["id"]))
    if (
        task["state"] not in (TaskState.QUEUED, TaskState.RUNNING)
        or task.get("cancel_requested_at") is not None
        or task.get("active_attempt_id") is not None
        or task.get("active_interaction_id") is not None
    ):
        raise MethodPlanInvalid("planning is only allowed at an idle step boundary")
    session = await repo.get_session(str(task["session_id"]))
    if session["wheel"] != "advisor" or str(session["active_task_id"]) != str(
        task["id"]
    ):
        raise MethodPlanInvalid("task does not hold the session")


def _rounds(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in attempts if a.get("step_id") == PLAN_STEP]


async def open_plan(
    ledger: Ledger, task_id: str, *, expected_version: int
) -> dict[str, Any]:
    repo = ledger.repo
    async with repo.transaction():
        task = await repo.get_task(task_id, for_update=True)
        await _ready(repo, task, expected_version)
        catalog = catalog_for_task(task)
        previous = await read_plan(repo, task)
        attempts = await repo.list_attempts(task_id)
        if any(a["status"] not in ATTEMPT_TERMINAL for a in attempts):
            raise MethodPlanInvalid("another attempt is still unsettled")
        if len(_rounds(attempts)) >= 1 + catalog.planning.max_replans:
            raise MethodPlanInvalid(
                "planning attempt limit exhausted, including failed attempts"
            )
        return await ledger.open_attempt(
            task_id,
            step_id=PLAN_STEP,
            step_version="1",
            reserve=Decimal(catalog.planning.reserve),
            meta={
                "kind": "method_plan",
                "parent_version": previous.version if previous else 0,
                "pack_digest": task["pack_digest"],
                "planning_round": len(_rounds(attempts)) + 1,
                "current_step": int(task["current_step"]),
            },
        )


async def accept_plan(
    ledger: Ledger,
    task_id: str,
    *,
    attempt_id: str,
    expected_version: int,
    proposal: MethodPlan,
) -> dict[str, Any]:
    repo = ledger.repo
    async with repo.transaction():
        # All plan acceptors take the task lock first; receipt publication and CAS
        # are one commit, serialized with cancellation and competing acceptors.
        task = await repo.get_task(task_id, for_update=True)
        catalog = catalog_for_task(task)
        attempt = await repo.get_attempt(attempt_id)
        if str(attempt["task_id"]) != task_id or attempt.get("step_id") != PLAN_STEP:
            raise MethodPlanInvalid("not this task's planning attempt")
        refs = attempt.get("refs") or {}
        artifacts = await repo.list_artifacts(task_id)
        accepted = next(
            (
                a
                for a in artifacts
                if a["name"] == PLAN_ARTIFACT and str(a["attempt_id"]) == attempt_id
            ),
            None,
        )
        if accepted is not None:
            artifact = await repo.get_artifact(
                task_id=task_id, name=PLAN_ARTIFACT, version=int(accepted["version"])
            )
            receipt = validate_receipt(task, artifact, int(accepted["version"]))
            if receipt.proposal.digest() != proposal.digest():
                raise IdempotencyConflict(
                    "planning attempt already accepted different content"
                )
            # Read-only replay even after a later plan/cancellation; never rewind.
            return {"version": accepted["version"], "replayed": True}
        await _ready(repo, task, expected_version)
        previous = await read_plan(repo, task)
        parent = previous.version if previous else 0
        if (
            attempt["status"] != AttemptState.COMPLETED
            or refs.get("kind") != "method_plan"
            or refs.get("pack_digest") != task["pack_digest"]
            or refs.get("parent_version") != parent
            or refs.get("current_step") != int(task["current_step"])
        ):
            raise MethodPlanInvalid(
                "planning attempt is incomplete or based on stale inputs"
            )
        attempts = await repo.list_attempts(task_id)
        rounds = _rounds(attempts)
        if len(rounds) > 1 + catalog.planning.max_replans:
            raise MethodPlanInvalid("planning attempt limit exceeded")
        # A later planning attempt supersedes an earlier unaccepted reply.
        if refs.get("planning_round") != len(rounds):
            raise MethodPlanInvalid("a later planning attempt superseded this reply")
        if any(a["status"] not in ATTEMPT_TERMINAL for a in attempts):
            raise MethodPlanInvalid("another attempt is still unsettled")
        completed = int(task["current_step"])
        protected = completed
        results: dict[str, int] = {}
        if previous:
            steps = previous.proposal.steps
            started = {
                a.get("step_id") for a in attempts if a.get("step_id") != PLAN_STEP
            }
            for index, step in enumerate(steps):
                if step.instance_id in started:
                    protected = max(protected, index + 1)
            # Replanning and dispatch must agree on accepted outputs. Never
            # infer a version from an unrelated latest Artifact of the same name.
            view = await execution_for_task(repo, task)
            assert view is not None
            try:
                accepted_outputs = await accepted_results(repo, task, view)
            except MethodPlanInvalid as exc:
                raise MethodPlanInvalid(
                    "completed prefix has no valid accepted result: " + str(exc)
                ) from None
            results = {
                key: int(value["version"]) for key, value in accepted_outputs.items()
            }
        try:
            validate_plan(
                catalog,
                proposal,
                previous=previous,
                protected_prefix=protected,
                completed_prefix=completed,
                results=results,
                available_tools=V2_TOOLS,
            )
        except ValueError as exc:
            raise MethodPlanInvalid(str(exc)) from None
        receipt = AcceptedMethodPlan(
            task_id=task_id,
            attempt_id=attempt_id,
            pack_digest=task["pack_digest"],
            version=parent + 1,
            proposal=proposal,
        )
        artifact = await repo.put_artifact(
            task_id=task_id,
            attempt_id=attempt_id,
            name=PLAN_ARTIFACT,
            kind="method_plan",
            content=receipt.model_dump(mode="json"),
        )
        if artifact["version"] != parent + 1:
            raise MethodPlanInvalid("plan history differs from the accepted pointer")
        await repo.cas_task(
            task_id,
            expected_version=expected_version,
            accepted_plan_version=artifact["version"],
        )
        await repo.append_event(
            session_id=str(task["session_id"]),
            kind="task",
            event_type="task/plan_accepted",
            task_id=task_id,
            attempt_id=attempt_id,
            payload={
                "version": artifact["version"],
                "parent_version": parent,
                "reason": proposal.reason,
                "protected_prefix": protected,
                "artifact_id": artifact["id"],
                "pack_digest": task["pack_digest"],
            },
        )
        return {"version": artifact["version"], "replayed": False}
