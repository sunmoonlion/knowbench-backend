"""Resolve exact registered dependencies and authorized tools for an incubation run.

Definition compilation proves structure. Each leaf must independently retain
real host execution qualification and its current reviewed source and policy.
Nothing here creates a grant or executes a call.
"""

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_compiler_generated import (
    FrozenMethod,
    compile_method,
)
from app.domain.workbench.formal_method_generated import Orchestration
from app.domain.workbench.formal_method_plan_generated import (
    expand_unique_plan,
    unique_plan,
)


async def resolve_plan(capabilities, owner, draft, method_id, *, bindings=None):
    return await resolve_owned_plan(
        capabilities.repo,
        capabilities.formal_sources,
        capabilities.operations,
        owner,
        draft,
        method_id,
        bindings=bindings,
    )


async def resolve_owned_plan(
    repo, formal_sources, operations, owner, draft, method_id, *, bindings=None
):
    catalog = FormalMethodCatalog(repo, formal_sources, operations)
    async with repo.transaction():
        resolved = {}
        postorder = []
        assets = set()
        grants = {}

        async def collect(current, ancestors):
            if not isinstance(current.procedure, Orchestration):
                return
            if len(ancestors) >= 16:
                raise CapabilityError("Formal dependency depth limit")
            for step in unique_plan(current):
                ref = step.candidates[0]
                if ref.method_id in ancestors:
                    raise CapabilityError("Cyclic formal method dependency")
                key = (ref.method_id, ref.version)
                if key in resolved:
                    if resolved[key].reference != ref:
                        raise CapabilityError("A frozen dependency has two digests")
                    continue
                if len(resolved) >= 1000:
                    raise CapabilityError("Formal dependency closure limit")
                frozen, grant = await formal_sources.registry.dependency(owner, ref)
                if (
                    frozen.reference != ref
                    or "host" not in frozen.draft.allowed_callers
                ):
                    raise CapabilityError("A dependency has no exact host definition")
                resolved[key] = frozen
                source = frozen.draft.source
                if source is not None and source.asset_id is not None:
                    publisher = owner
                    if grant:
                        publisher = await repo.actor_for_subject(grant.owner_subject)
                        binding = await formal_sources.bind_tool(owner, frozen, grant)
                        grants[key] = (grant, binding)
                        assets.add((str(owner), str(binding.source.asset_id)))
                    assets.add((str(publisher), str(source.asset_id)))
                await collect(frozen.draft.definition, (*ancestors, ref.method_id))
                postorder.append(key)

        try:
            await collect(draft.definition, (method_id,))
            definitions = {
                key: FrozenMethod(f.reference, f.draft.definition)
                for key, f in resolved.items()
            }
            # Expansion counts invocations, including repeated references; only
            # immutable registration reads are deduplicated above.
            plan = expand_unique_plan(method_id, draft.definition, definitions)
            compiled = compile_method(method_id, draft.definition, definitions)
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        if compiled.method_kind != "expert":
            raise CapabilityError("This runtime requires an orchestrated method")
        # Acquire all mutable asset locks in one global order, including those
        # buried below already-qualified experts. Never recurse while acquiring
        # locks in a caller-controlled graph order.
        for actor, asset_id in sorted(assets):
            await repo.capability_asset(actor, asset_id, lock=True)
        for key in postorder:
            frozen = resolved[key]
            if key in grants:
                grant, binding = grants[key]
                admitted = await catalog.shared_tool(grant)
                if await formal_sources.bind_tool(owner, admitted, grant) != binding:
                    raise CapabilityError("Incubation source binding changed")
                if bindings is not None:
                    bindings.append(binding)
            else:
                admitted = await catalog.resolve_owned_admission(
                    owner, frozen.reference, resolved=resolved
                )
            if admitted != frozen:
                raise CapabilityError("Formal dependency admission changed")
        return tuple(
            resolved[(ref.method_id, ref.version)] for ref in plan.dependencies
        )
