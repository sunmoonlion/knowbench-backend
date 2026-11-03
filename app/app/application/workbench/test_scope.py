"""Initial scope check for an unpublished author test, in its existing planner turn.

Only the frozen draft's public declarations are used. No marketplace publication,
extra task, database mutation outside the ledger, or backend model client.
"""

from typing import Any

from app.domain.workbench.expert_match import (
    DirectorySnapshot,
    ExpertMatch,
    validate_match,
)
from app.domain.workbench.method_catalog import CapabilityMethodCatalog
from app.domain.workbench.method_plan import CapabilityMethodPlan
from app.domain.workbench.packs import Strict
from app.domain.workbench.task_requirements import TaskRequirements, from_match

SCOPE_ARTIFACT = "_author_test_scope"


class InitialTestPlan(Strict):
    scope: ExpertMatch
    plan: CapabilityMethodPlan | None


def pending(task: dict[str, Any], catalog: Any) -> bool:
    return (
        task.get("pack_source") == "knowlab-test"
        and isinstance(catalog, CapabilityMethodCatalog)
        and "task_requirements" not in (task.get("original_input") or {})
        and not task.get("accepted_plan_version")
    )


def directory(
    task: dict[str, Any], catalog: CapabilityMethodCatalog
) -> DirectorySnapshot:
    return DirectorySnapshot.model_validate(
        {
            "contract": "knowmart/expert-directory/v2",
            "complete": True,
            "experts": [
                {
                    "knowbot_id": str(task["id"]),
                    "version": "1",
                    "digest": task["pack_digest"],
                    "name": catalog.name or catalog.title,
                    "tagline": catalog.tagline,
                    "scope": catalog.scope.model_dump(mode="json"),
                    "declaration": "capabilities/v1",
                    "runtime_contract": catalog.runtime_contract,
                    "public_capabilities": [
                        c.model_dump(mode="json") for c in catalog.public_capabilities
                    ],
                    "free": True,
                    "price": None,
                }
            ],
        }
    )


def verify(
    task: dict[str, Any], catalog: CapabilityMethodCatalog, output: InitialTestPlan
) -> tuple[Any, TaskRequirements | None]:
    question = task["original_input"]["input"]["text"]
    receipt = validate_match(question, output.scope, directory(task, catalog))
    if receipt.result.outcome not in {"summon", "candidates"}:
        if output.plan is not None:
            raise ValueError("an uncovered author test question must not have a plan")
        return receipt, None
    if output.plan is None:
        raise ValueError("covered author test question requires its execution plan")
    requirements = from_match(
        receipt,
        match_task_id=str(task["id"]),
        match_artifact_version=1,
        knowbot_id=str(task["id"]),
        version="1",
        expert_digest=task["pack_digest"],
        question=question,
    )
    return receipt, requirements
