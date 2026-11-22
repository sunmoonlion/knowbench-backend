"""Resolve an ordinary user's exact script or instruction and price its isolated execution."""

from app.application.workbench.method_access import check_requirement
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_adapter import script_registration
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodDraft,
)


async def graph_route(
    svc, owner, session, currency, *, requirement=None, producer=False
):
    from app.domain.workbench.review_routes import RouteKey

    base = await svc.account_quote(owner, session, currency)
    policy = svc.formal.review_policy
    if requirement is None and not producer:
        return base
    if policy is None:
        if requirement == "different_model_family":
            raise CapabilityError("Verified model families are unavailable")
        selected = await svc.repo.reviewer_preference(owner) if requirement else None
        if selected and selected != {
            k: base[k] for k in ("credential_id", "provider", "model")
        }:
            raise CapabilityError("Selected reviewer route is not loaded")
        return base
    key = RouteKey(provider=base["provider"], model=base["model"])
    view = await policy.choices(
        svc.repo,
        owner,
        requirement or "isolated_reviewer",
        sandbox=session["sandbox_id"],
        producer_route=key,
    )
    family = view["producer"]["family"] if view["producer"] else None
    if producer:
        return {**base, "family": family} if family else base
    pin = view["preference"] or {
        k: base[k] for k in ("credential_id", "provider", "model")
    }
    option = next((o for o in view["options"] if o["pin"] == pin and o["quote"]), None)
    if not option:
        if not view["preference"] and requirement != "different_model_family":
            return base
        if (
            not view["preference"]
            and len(view["options"]) == 1
            and view["options"][0]["quote"]
        ):
            option = view["options"][0]
        else:
            raise CapabilityError("Select an available and priced review route")
    quote = {**option["quote"], "credential_id": option["pin"]["credential_id"]}
    if quote["currency"] != currency or not await svc.repo.lock_active_credential(
        owner, quote["credential_id"]
    ):
        raise CapabilityError("The selected review account or currency changed")
    if option["family"]:
        quote["family"] = option["family"]
    if any(
        quote[k] != base[k] for k in ("credential_id", "provider", "model", "base_url")
    ):
        quote["review_connection"] = True
    if requirement == "different_model_family":
        if not family or not quote.get("family") or quote["family"] == family:
            raise CapabilityError("An independent, verified model family is required")
        quote["producer_family"] = family
    return quote


async def local_tool_quote(
    service,
    owner,
    subject,
    task,
    node,
    values,
    pending,
    *,
    reviewer=False,
    requirement=None,
    producer=False,
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
        route = await graph_route(
            svc,
            owner,
            session,
            "CNY",
            requirement=requirement or ("isolated_reviewer" if reviewer else None),
            producer=producer,
        )
        return source, {
            "currency": route["currency"],
            "maximum": route["maximum"],
            "basis": digest(route),
            "provider": route["provider"],
            "model": route["model"],
            "destination": route["base_url"] + "/responses",
            "fields": fields,
            **({"family": route["family"]} if route.get("family") else {}),
            **({"credential_id": route["credential_id"]} if reviewer else {}),
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
