"""Read-only author and accepted-plan receipts shared by planning and execution.

Neither reader writes task state nor infers acceptance from the latest artifact.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.task_requirements import for_task as task_requirements
from app.domain.workbench.errors import MethodPlanInvalid
from app.domain.workbench.method_catalog import MethodCatalog, parse_catalog
from app.domain.workbench.method_plan import PLAN_ARTIFACT, AcceptedMethodPlan


def catalog_for_task(task: dict[str, Any]) -> MethodCatalog:
    try:
        catalog = parse_catalog(task.get("pack_snapshot"))
    except ValidationError:
        raise MethodPlanInvalid("task does not pin a dynamic method catalog") from None
    if not task.get("pack_digest") or catalog.version != task["profile_version"]:
        raise MethodPlanInvalid("task author version is not pinned")
    return catalog


async def read_plan(
    repo: WorkbenchStore, task: dict[str, Any]
) -> AcceptedMethodPlan | None:
    """Read exactly the accepted version; never infer it from the latest artifact."""
    version = task.get("accepted_plan_version")
    if version is None:
        return None
    return await read_plan_version(repo, task, int(version))


async def read_plan_version(
    repo: WorkbenchStore, task: dict[str, Any], version: int
) -> AcceptedMethodPlan:
    if version < 1 or version > (task.get("accepted_plan_version") or 0):
        raise MethodPlanInvalid("plan version is not accepted")
    artifact = await repo.get_artifact(
        task_id=str(task["id"]), name=PLAN_ARTIFACT, version=version
    )
    return validate_receipt(task, artifact, version)


def validate_receipt(
    task: dict[str, Any], artifact: dict[str, Any] | None, version: int
) -> AcceptedMethodPlan:
    if artifact is None or artifact["kind"] != "method_plan":
        raise MethodPlanInvalid("accepted plan artifact is missing")
    try:
        plan = AcceptedMethodPlan.model_validate(artifact["content"])
    except ValidationError:
        raise MethodPlanInvalid("accepted plan receipt is invalid") from None
    if (
        plan.task_id != str(task["id"])
        or str(artifact["task_id"]) != str(task["id"])
        or plan.version != version
        or artifact["version"] != version
        or plan.proposal.parent_version != version - 1
        or plan.pack_digest != task["pack_digest"]
        or plan.attempt_id != str(artifact["attempt_id"])
    ):
        raise MethodPlanInvalid(
            "accepted plan does not match its task and author version"
        )
    requirements = task_requirements(task, catalog_for_task(task))
    if plan.requirements_digest != (requirements.digest() if requirements else None):
        raise MethodPlanInvalid("accepted plan refers to different task requirements")
    return plan
