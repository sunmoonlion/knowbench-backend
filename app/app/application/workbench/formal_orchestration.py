"""Resolve the exact private leaves of a deterministic formal expert plan.

Definition compilation proves structure. Each leaf must independently retain
real host execution qualification and its current reviewed source and policy.
Nothing here creates a grant or executes a call.
"""

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_compiler_generated import (
    FrozenMethod,
    compile_method,
)
from app.domain.workbench.formal_method_plan_generated import unique_plan


async def resolve_plan(capabilities, owner, draft, method_id):
    return await resolve_owned_plan(
        capabilities.repo,
        capabilities.formal_sources,
        capabilities.operations,
        owner,
        draft,
        method_id,
    )


async def resolve_owned_plan(repo, formal_sources, operations, owner, draft, method_id):
    try:
        steps = unique_plan(draft.definition)
    except ValueError as exc:
        raise CapabilityError(str(exc)) from exc
    references = tuple(
        {
            (
                step.candidates[0].method_id,
                step.candidates[0].version,
                step.candidates[0].digest,
            ): step.candidates[0]
            for step in steps
        }.values()
    )
    catalog = FormalMethodCatalog(repo, formal_sources, operations)
    async with repo.transaction():
        # Match ordinary selection/asset mutation lock order across all leaves.
        assets = set()
        for ref in references:
            row = await repo.formal_method(owner, str(ref.method_id), ref.version)
            if row is None:
                raise CapabilityError("A plan dependency has no private registration")
            frozen = FrozenFormalMethod.model_validate(row["document"])
            if frozen.reference != ref or frozen.draft.source is None:
                raise CapabilityError("A plan dependency is not an exact direct method")
            if frozen.draft.source.asset_id is not None:
                assets.add(str(frozen.draft.source.asset_id))
        for asset_id in sorted(assets):
            await repo.capability_asset(owner, asset_id, lock=True)
        leaves = tuple(
            [
                (await catalog.resolve_owned(owner, ref, caller="host")).frozen
                for ref in references
            ]
        )
        try:
            compiled = compile_method(
                method_id,
                draft.definition,
                {
                    (leaf.reference.method_id, leaf.reference.version): FrozenMethod(
                        leaf.reference, leaf.draft.definition
                    )
                    for leaf in leaves
                },
            )
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        if compiled.method_kind != "expert":
            raise CapabilityError("This runtime requires a one-level formal expert")
        return leaves
