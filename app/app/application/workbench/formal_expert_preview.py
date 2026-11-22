"""Read-only consumer preview of the fixed invocation graph and its input provenance."""

from decimal import Decimal

from app.application.workbench.method_access import check_requirement
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_compiler_generated import FrozenMethod
from app.domain.workbench.formal_method_generated import (
    CallerValue,
    LiteralValue,
    MethodRef,
    Orchestration,
    StepValue,
)
from app.domain.workbench.formal_method_plan_generated import (
    expand_unique_plan,
    unique_plan,
)
from app.domain.workbench.method_authorization import covers_requirement
from app.domain.workbench.tool_registry import connection_registration


async def fixed_graph(registry, owner, selected, definition):
    """Resolve only a deterministic graph, one qualified frontier at a time."""
    root = MethodRef.model_validate(selected)
    frontier = [definition]
    resolved = {}
    try:
        for _ in range(16):
            needed = set()
            for current in frontier:
                if isinstance(current.procedure, Orchestration):
                    if current.procedure.collaboration:
                        return None, "collaboration"
                    for step in unique_plan(current):
                        needed.add(step.candidates[0])
            needed -= resolved.keys()
            if not needed:
                break
            if root in needed or len(needed) + len(resolved) > 1000:
                return None, "unsupported_graph"
            found = await registry.planning_definitions(owner, needed)
            if found.keys() != needed:
                return None, "dependency_unavailable"
            resolved.update(found)
            frontier = found.values()
        else:
            return None, "unsupported_graph"
        graph = expand_unique_plan(
            root.method_id,
            definition,
            {
                (ref.method_id, ref.version): FrozenMethod(ref, d)
                for ref, d in resolved.items()
            },
        )
    except ValueError:
        return None, "unsupported_graph"
    return graph, None


def project_arguments(node, parent_values, parent_pending):
    values, pending = {}, {}
    for key, item in node.step.inputs.items():
        binding = item.binding
        if isinstance(binding, LiteralValue):
            values[key] = binding.value
        elif isinstance(binding, CallerValue):
            if binding.name in parent_values:
                values[key] = parent_values[binding.name]
            elif binding.name in parent_pending:
                pending[key] = parent_pending[binding.name]
        elif isinstance(binding, StepValue):
            pending[key] = {
                "step": [*node.parent_path, binding.step],
                "output": binding.output,
            }
    return values, pending


def mcp_quote(leaf, root, accounts, assets, operations, values, pending):
    proc = leaf.definition.procedure
    impl = proc.implementation
    requirements = leaf.definition.access_requirements
    if len(requirements) != 1:
        return None, "permission_unresolved"
    need = requirements[0]
    matches = {
        accounts[r.requirement_id]["asset_id"]
        for r in root.access_requirements
        if r.requirement_id in accounts and covers_requirement(r, need)
    }
    if len(matches) != 1:
        return None, "account_unresolved"
    asset = assets.get(matches.pop())
    if not asset or asset["status"] != "active":
        return None, "account_unresolved"
    try:
        registration = connection_registration(asset, impl.entry)
        if (
            registration.source != "mcp"
            or registration.service != impl.source
            or "sha256:" + registration.descriptor_digest != impl.digest
        ):
            return None, "operation_changed"
        policy = operations.formal_policy(registration)
        if policy.version != impl.version:
            return None, "operation_changed"
        check_requirement(need, policy)
        if not (values.keys() | pending.keys()) <= set(
            need.data_transfer.argument_fields
        ):
            return None, "operation_unverified"
        if not pending and all(
            name in values or (not slot.required and slot.on_missing == "omit")
            for name, slot in proc.inputs.items()
        ):
            registration.validate_arguments(values)
            check_requirement(need, policy, values)
    except CapabilityError:
        return None, "operation_unverified"
    cost = policy.formal_execution_cost
    return {
        "maximum": cost.incremental_cost,
        "currency": cost.currency,
        "basis": policy.policy_digest,
        "asset_id": asset["id"],
        "revision": asset["revision"],
    }, None


async def execution_preview(
    task_id,
    revision,
    selected,
    review_digest,
    definition,
    slots,
    bindings,
    graph,
    graph_issue,
    invalid_materials,
    invalid_accounts,
    assets,
    methods,
    owner,
    subject,
):
    from app.application.workbench.ordinary_material import (
        material_inputs,
        read_source_and_quote,
    )

    values, pins, issues = material_inputs(graph, bindings)
    bindings = {**bindings, "values": values}
    operations = methods.operations
    read_sources = {}

    def issue(code, path=(), slot=None):
        issues.append({"code": code, "path": list(path), "slot": slot})

    for name, slot in slots.items():
        if name not in bindings["values"] and (
            slot.required or slot.on_missing != "omit"
        ):
            issue("input_required", slot=name)
    for name in invalid_materials:
        issue("material_unavailable", slot=name)
    for name in invalid_accounts:
        issue("account_unavailable", slot=name)
    if graph_issue:
        issue(graph_issue)
    total = Decimal(0)
    priced = graph is not None
    steps = []
    assets_by_id = {a["id"]: a for a in assets}
    contexts = {(): (bindings["values"], {})}
    for node in graph.nodes if graph else ():
        values, pending = project_arguments(node, *contexts[node.parent_path])
        if node.kind != "tool":
            contexts[node.path] = values, pending
            continue
        proc = node.definition.procedure
        kind = proc.implementation.kind
        quote = None
        if kind == "mcp":
            accounts = {
                k: p
                for k, p in bindings["accounts"].items()
                if k not in invalid_accounts
            }
            quote, problem = mcp_quote(
                node, definition, accounts, assets_by_id, operations, values, pending
            )
            if problem:
                issue(problem, node.path)
        elif node.path in pins:
            try:
                read_sources[node.path], quote = await read_source_and_quote(
                    methods, owner, subject, node, values
                )
            except CapabilityError:
                issue("operation_unverified", node.path)
        elif (
            kind in ("skill_script", "registered_script")
            and operations.script_tariff
            and any(
                r.data_transfer.destination == operations.script_tariff.destination
                and r.capability_ref == "isolated-script"
                for r in node.definition.access_requirements
            )
        ):
            tariff = operations.script_tariff.quote()
            quote = {
                "maximum": tariff["maximum"],
                "currency": tariff["currency"],
                "basis": tariff["tariff_digest"],
            }
        else:
            issue("quote_unavailable", node.path)
        if quote:
            total += Decimal(quote["maximum"])
        else:
            priced = False
        steps.append(
            {
                "path": list(node.path),
                "reference": node.reference.model_dump(mode="json"),
                "kind": kind,
                "title": node.definition.capability_declaration.title,
                "account": (
                    assets_by_id[quote["asset_id"]]["detail"].get("account_label")
                    or assets_by_id[quote["asset_id"]]["name"]
                )
                if quote and "asset_id" in quote
                else None,
                "permissions": [
                    {
                        "operations": list(r.operations),
                        "destination": r.data_transfer.destination,
                        "fields": list(r.data_transfer.argument_fields),
                        "purpose": r.data_transfer.purpose,
                    }
                    for r in node.definition.access_requirements
                ],
                **({"material": pins[node.path]} if node.path in pins else {}),
                "from_outputs": pending,
                "quote": quote,
            }
        )
    limit = bindings["budget"]["limit"]
    if limit is not None and total > Decimal(limit):
        issue("budget_exceeded")
    return {
        "task_id": task_id,
        "revision": revision,
        "reference": selected,
        "review_digest": review_digest,
        "bindings": bindings,
        "steps": steps,
        "issues": issues,
        "estimated_maximum": format(total, "f") if priced else None,
        "currency": "CNY",
    }, read_sources
