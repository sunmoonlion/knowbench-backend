"""Read the server-pinned request, outside user input and client_context."""

from typing import Any

from app.domain.workbench.errors import MethodPlanInvalid
from app.domain.workbench.method_catalog import CapabilityMethodCatalog, MethodCatalog
from app.domain.workbench.task_requirements import TaskRequirements


def for_task(task: dict[str, Any], catalog: MethodCatalog) -> TaskRequirements | None:
    if not isinstance(catalog, CapabilityMethodCatalog):
        return None
    original = task.get("original_input") or {}
    try:
        accepted = TaskRequirements.model_validate(original.get("task_requirements"))
        accepted.verify(
            question=original["input"]["text"], expert_digest=task["pack_digest"]
        )
        declared = {cap.id for cap in catalog.public_capabilities}
        if any(
            not set(caps) <= declared for caps in accepted.capability_mapping.values()
        ):
            raise ValueError("requirement names an unknown capability")
    except (ValueError, KeyError, TypeError):
        raise MethodPlanInvalid(
            "task has no valid server-pinned capability requirements"
        ) from None
    return accepted
