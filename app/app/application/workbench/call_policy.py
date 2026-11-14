"""One operation decision for platform, resource relay and managed adapters.

Host identity and resource-owner checks surround this decision; neither a tool
description nor a method declaration constitutes a user authorization. The
returned decision is evidence for the existing Interaction, not an approval.
"""

from dataclasses import dataclass
from typing import Any

from app.application.workbench.method_access import check_access
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.tool_operations import OperationCatalog
from app.domain.workbench.tool_registry import ToolRegistration


@dataclass(frozen=True)
class CallDecision:
    operation: dict[str, Any]
    requires_approval: bool


def authorize_operation(
    registration: ToolRegistration,
    arguments: dict[str, Any],
    *,
    operations: OperationCatalog | None = None,
    method: bool = False,
    access: dict | None = None,
    operation_digest: str | None = None,
) -> CallDecision:
    """Resolve only trusted adapters, fail closed on changed or missing mappings.

    Exact-action approval remains mandatory for external reads too, until the
    task-scope read approval work package supplies a persisted approval fact.
    Platform and KnowBase operations retain their resource-owner checks at
    dispatch; they are not authorized by a remote readOnlyHint annotation.
    """
    registration.validate_arguments(arguments)
    if registration.source == "mcp":
        if operations is None:
            raise CapabilityError("External operation policy is unavailable")
        policy = operations.require(registration)
        if operation_digest != policy.policy_digest:
            raise CapabilityError("Operation policy changed; prepare a new turn")
        operation = policy.resolve(arguments)
        if method:
            check_access(access, policy, arguments)
        return CallDecision(operation, True)
    if registration.source == "platform":
        # Import locally to keep the registration projection independent of policy.
        from app.application.workbench.tool_registry import PLATFORM_TOOLS

        current = PLATFORM_TOOLS.get(registration.name)
        if current is None or current != registration:
            raise CapabilityError("Unregistered platform operation")
    elif registration.source == "knowbase":
        from app.application.workbench.data_relay import RELAYED

        if registration.service != "knowbase-data" or registration.name not in RELAYED:
            raise CapabilityError("Unregistered data operation")
    elif registration.source != "skill_script":
        raise CapabilityError("Unsupported operation adapter")
    return CallDecision(
        {
            "policy_digest": digest(
                [
                    registration.source,
                    registration.tool_id,
                    registration.descriptor_digest,
                ]
            ),
            "capability_ref": registration.source,
            "version": registration.descriptor_digest,
            "operation": registration.name,
            "effect": "execute" if registration.source == "skill_script" else "read",
            "resources": [],
            "arguments_digest": digest(arguments),
        },
        registration.source == "skill_script",
    )
