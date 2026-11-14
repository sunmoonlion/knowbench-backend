"""Method declarations and user-confirmed account bindings constrain MCP execution."""

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.method_capabilities import AccessRequirement


def declared_access(scope, policy):
    binding = next(
        (
            t
            for t in scope["requirements"].tools
            if (t.url, t.name) == (policy.service_url, policy.tool_name)
        ),
        None,
    )
    requirement = next(
        (
            r
            for r in scope["access_requirements"]
            if binding and r.requirement_id == binding.requirement_id
        ),
        None,
    )
    if requirement is None:
        raise CapabilityError("Method tool has no declared permission")
    grant = {
        "requirement": requirement.model_dump(mode="json"),
        "operation_digest": policy.policy_digest,
    }
    check_access(grant, policy)
    return grant


def check_access(grant, policy, arguments=None):
    if (
        not isinstance(grant, dict)
        or grant.get("operation_digest") != policy.policy_digest
    ):
        raise CapabilityError("Method permission was not confirmed for this operation")
    try:
        requirement = AccessRequirement.model_validate(grant.get("requirement"))
    except ValueError as exc:
        raise CapabilityError("Method permission confirmation is invalid") from exc
    if (
        requirement.capability_ref != policy.capability_ref
        or requirement.version != policy.version
        or policy.operation not in requirement.operations
        or requirement.data_transfer.destination != policy.service_url
    ):
        raise CapabilityError("Tool operation differs from the method permission")
    scopes = {
        (r.resource_kind, r.resource_role): set(r.values) for r in requirement.resources
    }
    if set(scopes) != {(r.resource_kind, r.resource_role) for r in policy.resources}:
        raise CapabilityError("Method resource roles differ from the trusted operation")
    for resource in policy.resources:
        if resource.allowed_values is not None and not scopes[
            (resource.resource_kind, resource.resource_role)
        ] <= set(resource.allowed_values):
            raise CapabilityError("Method resource scope exceeds the platform scope")
    if arguments is None:
        return
    if not set(arguments) <= set(requirement.data_transfer.argument_fields):
        raise CapabilityError("Outbound arguments exceed the method permission")
    operation = policy.resolve(arguments)
    for resource in operation["resources"]:
        if (
            not set(resource["values"])
            <= scopes[(resource["resource_kind"], resource["resource_role"])]
        ):
            raise CapabilityError(
                "Operation resource is outside the confirmed method scope"
            )
