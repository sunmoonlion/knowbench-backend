"""Bind ordinary material reads to the owner's input selection before confirmation."""

from app.application.workbench.knowbase_method_read import TOOL
from app.application.workbench.method_access import check_requirement
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.data_function import descriptor, registration
from app.domain.workbench.formal_method_admission_generated import FormalMethodSource
from app.domain.workbench.formal_method_generated import CallerValue, LiteralValue
from app.domain.workbench.source_licenses import SourceIdentity


def is_material_read(implementation):
    return (implementation.kind, implementation.source, implementation.entry) == (
        "service_function",
        "knowbase",
        TOOL,
    )


def material_inputs(graph, bindings):
    nodes = {n.path: n for n in graph.nodes} if graph else {}

    def root_binding(path, binding):
        # Follow frozen caller aliases only. Outputs produced at run time cannot identify a selected version.
        while path and isinstance(binding, CallerValue):
            parent = nodes[path]
            binding = (
                parent.step.inputs[binding.name].binding
                if binding.name in parent.step.inputs
                else None
            )
            path = parent.parent_path
        return binding

    values = dict(bindings["values"])
    pins, used, issues = {}, set(), []

    def issue(path, slot):
        issues.append(
            {"code": "material_authority_required", "path": list(path), "slot": slot}
        )

    for leaf in graph.leaf_nodes if graph else ():
        if not is_material_read(leaf.definition.procedure.implementation):
            continue
        inputs = leaf.step.inputs
        material = inputs.get("material_id")
        version = inputs.get("version_no")
        origin = root_binding(leaf.parent_path, material.binding) if material else None
        if not isinstance(origin, CallerValue) or not material or not version:
            issue(leaf.path, "material_id")
            continue
        slot = origin.name
        pin = bindings["materials"].get(slot)
        if not pin:
            issue(leaf.path, slot)
            continue
        used.add(slot)
        for field, value in (
            (material, pin["material_id"]),
            (version, pin["version_no"]),
        ):
            binding = root_binding(leaf.parent_path, field.binding)
            if isinstance(binding, CallerValue):
                actual = values.setdefault(binding.name, value)
            elif isinstance(binding, LiteralValue):
                actual = binding.value
            else:
                issue(leaf.path, slot)
                break
            if (
                type(actual) is not type(value)
                or actual != value
                or not field.slot.accepts(value)
            ):
                issue(leaf.path, slot)
                break
        else:
            pins[leaf.path] = pin
    for slot in sorted(bindings["materials"].keys() - used):
        issue((), slot)
    return values, pins, issues


async def read_source_and_quote(methods, owner, subject, leaf, values):
    impl = leaf.definition.procedure.implementation
    identity = SourceIdentity(
        kind=impl.kind,
        source=impl.source,
        entry=impl.entry,
        digest=impl.digest,
        version=impl.version,
    )
    license = methods.source_licenses.executable(subject, identity, now=methods.now())
    if not license:
        raise CapabilityError("Missing consumer execution license")
    source = FormalMethodSource(
        **identity.model_dump(),
        asset_id=None,
        asset_revision=None,
        license_basis=license.basis,
    )
    asset = await methods.registry.execution_asset(owner, source)
    tool = registration(source, descriptor(asset["function"]))
    policy = methods.operations.formal_policy(tool)
    needs = leaf.definition.access_requirements
    if (
        len(needs) != 1
        or policy.effect != "read"
        or policy.operation != TOOL
        or not any(
            r.resource_role == "material" and r.argument_path == ("material_id",)
            for r in policy.resources
        )
    ):
        raise CapabilityError("Missing exact registered material read policy")
    tool.validate_arguments(values)
    check_requirement(needs[0], policy, values)
    cost = policy.formal_execution_cost
    return source, {
        "maximum": cost.incremental_cost,
        "currency": cost.currency,
        "basis": policy.policy_digest,
    }
