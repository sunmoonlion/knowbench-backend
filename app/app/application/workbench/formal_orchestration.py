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
    select_plan,
    unique_plan,
)


async def resolve_plan(
    capabilities, owner, draft, method_id, *, bindings=None, grants=None, values=None
):
    return await resolve_owned_plan(
        capabilities.repo,
        capabilities.formal_sources,
        capabilities.operations,
        owner,
        draft,
        method_id,
        bindings=bindings,
        values=values,
        grants=grants,
    )


async def resolve_owned_plan(
    repo,
    formal_sources,
    operations,
    owner,
    draft,
    method_id,
    *,
    bindings=None,
    grants=None,
    values=None,
):
    catalog = FormalMethodCatalog(repo, formal_sources, operations)
    async with repo.transaction():
        resolved = {}
        postorder = []
        assets = set()
        shared_bindings = {}

        async def collect(current, ancestors):
            if not isinstance(current.procedure, Orchestration):
                return
            if len(ancestors) >= 16:
                raise CapabilityError("Formal dependency depth limit")
            if (
                (
                    current.procedure.has_candidate_choice
                    or current.procedure.object_batch
                )
                and len(ancestors) == 1
                and values is None
            ):
                refs = (
                    ref for step in current.procedure.steps for ref in step.candidates
                )
            else:
                refs = (step.candidates[0] for step in unique_plan(current))
            for ref in refs:
                if ref.method_id in ancestors:
                    raise CapabilityError("Cyclic formal method dependency")
                key = (ref.method_id, ref.version)
                if key in resolved:
                    if resolved[key].reference != ref:
                        raise CapabilityError("A frozen dependency has two digests")
                    continue
                if len(resolved) >= 1000:
                    raise CapabilityError("Formal dependency closure limit")
                frozen, grant = await formal_sources.registry.dependency(
                    owner, ref, grants=grants
                )
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
                        shared_bindings[key] = (grant, binding)
                        assets.add((str(owner), str(binding.source.asset_id)))
                    assets.add((str(publisher), str(source.asset_id)))
                await collect(frozen.draft.definition, (*ancestors, ref.method_id))
                postorder.append(key)

        try:
            active = (
                select_plan(draft.definition, values)
                if values is not None
                else draft.definition
            )
            await collect(active, (method_id,))
            definitions = {
                key: FrozenMethod(f.reference, f.draft.definition)
                for key, f in resolved.items()
            }
            # Expansion counts invocations, including repeated references; only
            # immutable registration reads are deduplicated above.
            compiled = compile_method(method_id, active, definitions)
            if isinstance(active.procedure, Orchestration) and (
                active.procedure.has_candidate_choice or active.procedure.object_batch
            ):
                refs = tuple(f.reference for _, f in sorted(resolved.items()))
            else:
                refs = expand_unique_plan(method_id, active, definitions).dependencies
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
            if key in shared_bindings:
                grant, binding = shared_bindings[key]
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
        return tuple(resolved[(ref.method_id, ref.version)] for ref in refs)
