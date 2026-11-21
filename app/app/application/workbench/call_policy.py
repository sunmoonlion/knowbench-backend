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

    External operations require a human decision or a matching, live scoped
    read receipt. Callers check the receipt only after verifying this policy.
    Platform and KnowBase operations retain their resource-owner checks at
    dispatch; they are not authorized by a remote readOnlyHint annotation.
    """
    registration.validate_arguments(arguments)
    if (
        registration.source in ("skill_script", "registered_script")
        and method
        and access
    ):
        if operations is None:
            raise CapabilityError("Script pricing policy is unavailable")
        policy = operations.formal_policy(registration)
        check_access(access, policy, arguments)
        return CallDecision(policy.resolve(arguments), True)
    if registration.source in ("mcp", "a2a"):
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
            "effect": "execute"
            if registration.source in ("skill_script", "registered_script")
            else "read",
            "resources": [],
            "arguments_digest": digest(arguments),
        },
        registration.source in ("skill_script", "registered_script"),
    )


async def send_mcp(remote, url, token, name, arguments, *, before_send=None):
    """HTTP adapters recheck after session negotiation. Simple in-process adapters send immediately."""
    if before_send:
        negotiated = getattr(remote, "call_checked", None)
        if negotiated:
            return await negotiated(
                url, token, name, arguments, before_send=before_send
            )
        await before_send()
    return await remote.call(url, token, name, arguments)
