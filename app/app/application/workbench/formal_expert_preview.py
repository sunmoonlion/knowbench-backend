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
    ReviewPhaseStep,
    StepValue,
)
from app.domain.workbench.formal_method_plan_generated import (
    BatchItem,
    BatchRows,
    InvalidObjectList,
    UnmatchedCandidate,
    expand_unique_plan,
    review_group,
    select_plan,
    unique_plan,
)
from app.domain.workbench.method_authorization import (
    METHOD_SCOPE_HOURS,
    covers_requirement,
)
from app.domain.workbench.tool_registry import connection_registration


async def fixed_graph(registry, owner, selected, definition, values):
    try:
        definition = select_plan(definition, values)
    except InvalidObjectList:
        return None, "invalid_object_list"
    except UnmatchedCandidate:
        return None, "candidate_unmatched"
    """Resolve only a deterministic graph, one qualified frontier at a time."""
    root = MethodRef.model_validate(selected)
    frontier = [definition]
    resolved = {}
    try:
        for _ in range(16):
            needed = set()
            for current in frontier:
                if isinstance(current.procedure, Orchestration):
                    if current.procedure.collaboration and not review_group(
                        current.procedure
                    ):
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
        if isinstance(binding, BatchItem):
            values[key] = binding.object_id
        elif isinstance(binding, BatchRows):
            pending[key] = {
                "step": [*node.parent_path, binding.step],
                "output": binding.output,
                "objects": [
                    {"step": k, "object_id": obj} for k, obj in binding.members
                ],
            }
        elif isinstance(binding, LiteralValue):
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
            registration.source != impl.kind
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
    task,
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
    service,
    owner,
    subject,
):
    from app.application.workbench.ordinary_material import (
        material_inputs,
        read_source_and_quote,
    )

    values, pins, issues = material_inputs(graph, bindings)
    bindings = {**bindings, "values": values}
    methods = service.formal_registry.methods
    operations = methods.operations
    local_sources = {}

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
    steps, experts = [], []
    assets_by_id = {a["id"]: a for a in assets}
    contexts = {(): (bindings["values"], {})}
    review_edge = (
        review_group(definition.procedure)
        if isinstance(definition.procedure, Orchestration)
        else None
    )
    for node in graph.nodes if graph else ():
        values, pending = project_arguments(node, *contexts[node.parent_path])
        if node.kind != "tool":
            experts.append(
                {
                    "path": list(node.path),
                    "reference": node.reference.model_dump(mode="json"),
                    "title": node.definition.capability_declaration.title,
                    "outputs": list(node.step.outputs),
                }
            )
            contexts[node.path] = values, pending
            continue
        proc = node.definition.procedure
        role = (
            node.step.role if isinstance(node.step, ReviewPhaseStep) else node.step.id
        )
        reviewing = bool(review_edge and role == review_edge.reviewer)
        kind = proc.implementation.kind
        quote = None
        if kind in ("mcp", "a2a"):
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
                local_sources[node.path], quote = await read_source_and_quote(
                    methods, owner, subject, node, values
                )
            except CapabilityError:
                issue("operation_unverified", node.path)
        elif kind in (
            "skill_script",
            "registered_script",
            "skill_instruction",
            "registered_instruction",
        ):
            from app.application.workbench.ordinary_local_tools import local_tool_quote
            from app.domain.workbench.errors import WorkbenchError

            if (kind.endswith("_script") and not service.script_enabled) or (
                kind.endswith("_instruction") and not service.instruction_service
            ):
                issue("driver_unavailable", node.path)
            else:
                try:
                    local_sources[node.path], quote = await local_tool_quote(
                        service,
                        owner,
                        subject,
                        task,
                        node,
                        values,
                        pending,
                        reviewer=reviewing,
                        requirement=review_edge.requirement
                        if reviewing and review_edge
                        else None,
                        producer=bool(review_edge and role in review_edge.producers),
                    )
                except (WorkbenchError, ValueError):
                    issue("local_tool_unavailable", node.path)
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
                **(
                    {"independent_review_of": " / ".join(review_edge.producers)}
                    if review_edge and reviewing
                    else {}
                ),
                **(
                    {"review_phase": node.step.phase}
                    if isinstance(node.step, ReviewPhaseStep)
                    else {}
                ),
                "from_outputs": pending,
                "quote": quote,
            }
        )
    limit = bindings["budget"]["limit"]
    if limit is not None and total > Decimal(limit):
        issue("budget_exceeded")
    choices = []
    if graph and isinstance(definition.procedure, Orchestration):
        for template in definition.procedure.steps:
            rule = template.candidate_choice
            if rule:
                source = template.inputs[rule.input].binding
                if isinstance(source, CallerValue):
                    value = bindings["values"][source.name]
                    selected_node = next(
                        n for n in graph.nodes if n.path == (template.id,)
                    )
                    choices.append(
                        {
                            "step": template.id,
                            "input": source.name,
                            "value": value,
                            "reference": selected_node.reference.model_dump(
                                mode="json"
                            ),
                        }
                    )
    review_budget = None
    if review_edge and graph and isinstance(definition.procedure, Orchestration):
        proc = definition.procedure
        comparison = (proc.collaboration or {})[review_edge.reviewer].comparison
        if comparison:
            reworks = min(
                comparison.max_reworks,
                proc.max_steps // 3 - 1,
                (proc.max_expansions - 1) // 3 - 1,
            )
            review_budget = {
                "max_reworks": reworks,
                "maximum_with_reworks": format(total * (1 + reworks), "f")
                if priced
                else None,
            }
    return {
        **({"review_budget": review_budget} if review_budget else {}),
        **({"choices": choices} if choices else {}),
        **(
            {
                "object_batch": {
                    "maximum": definition.procedure.object_batch.max_objects,
                    "on_missing": definition.procedure.object_batch.on_missing,
                    "members": [
                        {"step": n.step.id, "object_id": port.binding.object_id}
                        for n in graph.nodes
                        if not n.parent_path
                        for port in n.step.inputs.values()
                        if isinstance(port.binding, BatchItem)
                    ],
                }
            }
            if graph
            and isinstance(definition.procedure, Orchestration)
            and definition.procedure.object_batch
            else {}
        ),
        **(
            {"subexperts": experts, "scope_hours": METHOD_SCOPE_HOURS}
            if experts
            else {}
        ),
        "task_id": str(task["id"]),
        "revision": revision,
        "reference": selected,
        "review_digest": review_digest,
        "bindings": bindings,
        "steps": steps,
        "issues": issues,
        "estimated_maximum": format(total, "f") if priced else None,
        "currency": "CNY",
    }, local_sources
