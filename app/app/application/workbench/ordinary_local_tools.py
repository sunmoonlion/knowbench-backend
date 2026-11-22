"""Resolve an ordinary user's exact script or instruction and price its isolated execution."""

from app.application.workbench.method_access import check_requirement
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_adapter import script_registration
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodDraft,
)


async def check_reviewer_route(repo, owner, route):
    selected = await repo.reviewer_preference(owner)
    if selected and selected != {
        k: route[k] for k in ("credential_id", "provider", "model")
    }:
        raise CapabilityError("Selected reviewer route is not loaded")


async def local_tool_quote(
    service, owner, subject, task, node, values, pending, *, reviewer=False
):
    methods = service.formal_registry.methods
    proc = node.definition.procedure
    impl = proc.implementation
    source = await methods.registry.local_tool_source(owner, subject, impl)
    fields = sorted(values.keys() | pending.keys())
    if source.is_instruction:
        if (
            proc.max_model_turns != 1
            or node.definition.access_requirements
            or node.definition.what_requires_approval
            or impl.requirement_ids
        ):
            raise CapabilityError("Only a single isolated inference is supported")
        await methods.instruction_source(owner, subject, source)
        svc = await service.instruction_service(methods, owner)
        session = await methods.repo.get_session(
            str(task["session_id"]), owner_actor_id=owner
        )
        route = await svc.account_quote(owner, session, "CNY")
        if reviewer:
            await check_reviewer_route(methods.repo, owner, route)
        return source, {
            "currency": route["currency"],
            "maximum": route["maximum"],
            "basis": digest(route),
            "provider": route["provider"],
            "model": route["model"],
            "destination": route["base_url"] + "/responses",
            "fields": fields,
        }
    draft = FormalMethodDraft(
        definition=node.definition, source=source, allowed_callers=("host",)
    )
    policy = methods.operations.formal_policy(script_registration(draft))
    requirements = node.definition.access_requirements
    if len(requirements) != 1 or not set(fields) <= set(
        requirements[0].data_transfer.argument_fields
    ):
        raise CapabilityError("Missing exact script permission")
    check_requirement(requirements[0], policy, None if pending else values)
    tariff = policy.tariff.quote()
    return source, {
        "maximum": tariff["maximum"],
        "currency": tariff["currency"],
        "basis": policy.policy_digest,
        "per_container_second": tariff["per_container_second"],
        "maximum_seconds": tariff["maximum_seconds"],
    }
