"""Recheck sealed expert proofs without depending on disposable execution rows.

The repository-owned admission receipt authenticates the sealed evidence. This
module additionally reconciles its identities, bindings, grants and outcomes;
it never fills missing historical fields or confers current resource authority.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import FormalTaskSnapshot, validate_values
from app.domain.workbench.formal_method_compiler_generated import (
    FrozenMethod,
    compile_method,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalExecutionCaseResult,
    evaluate_assertions,
    fingerprint,
)
from app.domain.workbench.formal_method_generated import Orchestration, ToolProcedure
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    expand_unique_plan,
    export_outputs,
    unique_plan,
    validate_plan_inputs,
    validate_step_outputs,
)
from app.domain.workbench.method_authorization import MethodAuthorization

OLD_ADAPTER = "knowbench/formal-orchestration-host/v1"
TREE_ADAPTER = "knowbench/formal-orchestration-tree-host/v1"


def require(condition, issue):
    if not condition:
        raise CapabilityError(issue)


def ref_key(ref):
    return ref.method_id, ref.version, ref.digest


def exact_id(value):
    return str(UUID(str(value)))


def _artifact(raw, *, step_id, identities):
    require(
        isinstance(raw, dict)
        and set(raw) == {"id", "name", "version", "digest"}
        and raw["name"] == "formal-step:" + step_id
        and type(raw["version"]) is int
        and raw["version"] == 1
        and isinstance(raw["digest"], str)
        and len(raw["digest"]) == 64
        and all(char in "0123456789abcdef" for char in raw["digest"]),
        "Sealed step artifact identity is invalid",
    )
    key = ("artifact", exact_id(raw["id"]))
    require(key not in identities, "Artifact identity reused by another step")
    identities.add(key)


def _identity(identities, kind, value):
    key = (kind, exact_id(value))
    require(key not in identities, "Execution identity reused by another case or step")
    identities.add(key)
    return key[1]


def verify_leaf_authority(
    owner,
    snapshot,
    result,
    leaf,
    step,
    inputs,
    item,
    root_call_id,
    operations,
    completed_at,
    *,
    tree_host=None,
):
    """Strict historical one-level leaf proof; approval is checked at consumption."""
    source = leaf.draft.source
    require(
        source is not None and source.kind == "mcp", "Sealed leaf has no MCP source"
    )
    assert source is not None
    policy = operations.require_source(
        source.source, source.entry, source.digest.removeprefix("sha256:")
    )
    operation = policy.resolve(inputs)
    require(policy.formal_execution_cost is not None, "Leaf cost policy is unavailable")
    governance, observed = item["governance"], item["observed_call"]
    child_id, call_id = exact_id(item["method_call_id"]), exact_id(item["call_id"])
    expected_host = {
        "task_id": str(result.task_id),
        "attempt_id": exact_id(item["attempt_id"]),
        "snapshot_digest": digest(snapshot.document()),
        "slot": step.id,
        "method_id": str(leaf.reference.method_id),
        "run_id": str(snapshot.run_id),
        "case_id": snapshot.case_id,
        "repetition": snapshot.repetition,
        "source_digest": source.fingerprint(),
        "reference": leaf.reference.model_dump(mode="json"),
        "parent_method_id": str(snapshot.method_id),
        "method_call_id": child_id,
        "inputs_digest": digest(inputs),
    }
    if tree_host is not None:
        expected_host.update(tree_host)
    require(
        isinstance(governance, dict)
        and governance["formal_host"] == expected_host
        and governance["call_id"] == call_id
        and governance["method_call_id"] == child_id
        and governance["root_call_id"] == root_call_id
        and governance["input_digest"] == digest(inputs)
        and governance["registration_id"]
        == digest(["mcp", source.source, source.entry])
        and governance["registration_digest"] == source.digest.removeprefix("sha256:")
        and governance["operation"] == operation
        and governance["approved"] is True
        and governance["direct_grants"] is True
        and observed["call_id"] == call_id
        and observed["parent_call_id"] == child_id
        and observed["status"] == "succeeded"
        and isinstance(observed["adapter_result_digest"], str)
        and len(observed["adapter_result_digest"]) == 64,
        "Sealed leaf provenance or current operation policy changed",
    )
    auth = MethodAuthorization.model_validate(governance["authorization"])
    parent = MethodAuthorization.model_validate(governance["parent_authorization"])
    approval = governance["approval"]
    approved_at = datetime.fromisoformat(approval["consumed_at"])
    expiry = datetime.fromisoformat(approval["expires_at"])
    grant_expiry = min(expiry, parent.expires_at) if tree_host is not None else expiry
    require(
        str(auth.call_id) == child_id
        and str(auth.parent_call_id) == root_call_id
        and auth.parent_revision == parent.revision
        and str(parent.call_id) == root_call_id
        and parent.parent_call_id is None
        and auth.method_id == str(leaf.reference.method_id)
        and parent.method_id == str(snapshot.method_id)
        and not auth.revoked
        and not parent.revoked
        and len(auth.grants) == 1
        and digest(auth.model_dump(mode="json")) == governance["authorization_digest"]
        and approval["decision"] == "accept"
        and approval["responded_by"] == str(owner)
        and approval["subject_digest"] == observed["digest"]
        and approved_at.tzinfo is not None
        and approved_at <= completed_at
        and approved_at < auth.expires_at == grant_expiry <= parent.expires_at,
        "Sealed child approval or authorization is invalid",
    )
    declared = leaf.draft.definition.access_requirements[0].model_dump(mode="json")
    exact = {
        **declared,
        "operations": [operation["operation"]],
        "resources": operation["resources"],
        "data_transfer": {
            **declared["data_transfer"],
            "argument_fields": sorted(inputs),
        },
    }
    grant = auth.grants[0]
    require(
        grant in parent.grants
        and grant.grant_id == digest([call_id, observed["digest"]])
        and not grant.delegable
        and grant.resource_authority is None
        and grant.recipient_call_id == auth.call_id
        and grant.confirmation_context == digest(snapshot.document())
        and str(grant.connection_id) == str(source.asset_id)
        and grant.connection_revision == source.asset_revision
        and grant.operation_digest == policy.policy_digest
        and grant.method_ids == (str(leaf.reference.method_id),)
        and grant.requirement.model_dump(mode="json") == exact
        and grant.expires_at == auth.expires_at,
        "Sealed grant does not cover this exact leaf invocation",
    )
    return parent, auth


def _old_case(
    owner, frozen, request, case, result, item, resolved, identities, operations
):
    snapshot = FormalTaskSnapshot.model_validate(item["snapshot"])
    require(
        snapshot.request_digest == request.fingerprint()
        and digest(snapshot.document()) == item["snapshot_digest"]
        and snapshot.run_id == request.run_id
        and snapshot.method_id == request.method_id
        and snapshot.draft == request.draft
        and snapshot.caller == "host"
        and snapshot.case_id == case.case_id
        and snapshot.repetition == result.repetition
        and fingerprint(snapshot.arguments) == fingerprint(case.input.values)
        and snapshot.definition_digest == frozen.reference.digest,
        "Sealed expert snapshot differs from its execution request",
    )
    plan = unique_plan(snapshot.draft.definition)
    expected = {ref_key(step.candidates[0]) for step in plan}
    require(
        {ref_key(f.reference) for f in snapshot.dependencies} == expected
        and len(snapshot.dependencies) == len(expected)
        and all(
            f == resolved.get((f.reference.method_id, f.reference.version))
            for f in snapshot.dependencies
        ),
        "Sealed dependency versions differ from their owned registrations",
    )
    _identity(identities, "task", result.task_id)
    root = _identity(identities, "method_call", item["root_call_id"])
    validate_plan_inputs(snapshot.draft.definition, snapshot.arguments)
    retained = item["steps"]
    require(len(retained) == len(plan), "Sealed proof omits or adds a mandatory step")
    accepted, versions, grants = {}, [], []
    for index, (step, recorded) in enumerate(zip(plan, retained, strict=True)):
        leaf = resolved[(step.candidates[0].method_id, step.candidates[0].version)]
        procedure = leaf.draft.definition.procedure
        require(
            isinstance(procedure, ToolProcedure),
            "One-level proof cannot qualify nested execution",
        )
        assert isinstance(procedure, ToolProcedure)
        inputs = bind_step_inputs(step, snapshot.arguments, accepted)
        validate_values(procedure.inputs, inputs)
        bindings = [v for v in versions if v["step_id"] in step.depends_on]
        require(
            recorded["slot"] == step.id
            and recorded["reference"] == leaf.reference.model_dump(mode="json")
            and recorded["status"] == "COMPLETED"
            and recorded["outcome"] == "succeeded"
            and recorded["inputs_digest"] == digest(inputs)
            and recorded["input_bindings"] == bindings
            and recorded["output_artifacts"] == [recorded["artifact"]],
            "Sealed step order, identity or input bindings changed",
        )
        _identity(identities, "attempt", recorded["attempt_id"])
        _identity(identities, "method_call", recorded["method_call_id"])
        _identity(identities, "tool_call", recorded["call_id"])
        _identity(identities, "interaction", recorded["governance"]["interaction_id"])
        _artifact(recorded["artifact"], step_id=step.id, identities=identities)
        parent, auth = verify_leaf_authority(
            owner,
            snapshot,
            result,
            leaf,
            step,
            inputs,
            recorded,
            root,
            operations,
            frozen.validation.completed_at,
        )
        grants.append(auth.grants[0])
        require(
            parent.revision == index + 1 and parent.grants == tuple(grants),
            "Sealed cumulative parent grants changed",
        )
        outputs = validate_step_outputs(step, recorded["outputs"])
        require(
            fingerprint(outputs) == fingerprint(recorded["outputs"])
            and recorded["outputs_digest"] == digest(outputs),
            "Sealed output projection changed",
        )
        accepted[step.id] = outputs
        versions.append({"step_id": step.id, **recorded["artifact"]})
    require(
        str(result.attempt_id) == retained[-1]["attempt_id"]
        and str(result.call_id) == retained[-1]["call_id"],
        "Sealed case does not identify its final actual step",
    )
    exported = export_outputs(snapshot.draft.definition, accepted)
    require(
        fingerprint(exported) == fingerprint(item["outputs"])
        and item["outputs_digest"] == digest(exported),
        "Sealed expert exports changed",
    )
    assertions = evaluate_assertions(case, exported)
    require(result.assertions == assertions, "Sealed expert assertions changed")
    passed = case.expected.outcome == "succeeded" and all(a.passed for a in assertions)
    require(
        result.status == ("passed" if passed else "failed"),
        "Sealed expert verdict changed",
    )
    return passed


def verify_sealed_plan(owner, frozen, request, evidence, resolved, *, operations):
    """Qualify historical expert evidence; current source checks belong to closure resolution."""
    require(
        isinstance(frozen.draft.definition.procedure, Orchestration)
        and frozen.draft.allowed_callers == ("host",)
        and frozen.visibility == "private"
        and frozen.draft.source is None,
        "Only private host-only expert admission is supported",
    )
    definitions = {
        key: FrozenMethod(value.reference, value.draft.definition)
        for key, value in resolved.items()
    }
    compiled = compile_method(
        frozen.reference.method_id, frozen.draft.definition, definitions
    )
    expanded = expand_unique_plan(
        frozen.reference.method_id, frozen.draft.definition, definitions
    )
    require(
        {ref_key(r) for r in request.dependencies}
        == {ref_key(r) for r in compiled.dependencies}
        == {ref_key(r) for r in frozen.validation.dependencies}
        and len(request.dependencies) == len(compiled.dependencies)
        and all(
            resolved[(r.method_id, r.version)].owner_subject == frozen.owner_subject
            and resolved[(r.method_id, r.version)].visibility == "private"
            for r in expanded.dependencies
        ),
        "Expert dependency closure differs from its owned frozen versions",
    )
    require(
        evidence["adapter"] in (OLD_ADAPTER, TREE_ADAPTER),
        "Unsupported sealed orchestration adapter",
    )
    require(
        evidence["adapter"] == TREE_ADAPTER
        or all(n.kind == "tool" for n in expanded.nodes),
        "One-level proof cannot qualify a recursive plan",
    )
    require(
        evidence["adapter"] != TREE_ADAPTER
        or any(n.kind != "tool" for n in expanded.nodes),
        "Tree proof requires a recursive plan",
    )
    cost = Decimal(evidence["model_cost"])
    require(
        cost.is_finite()
        and cost == 0
        and cost < Decimal(request.execution.budget.limit),
        "Expert model cost does not reconcile",
    )
    cases = {
        (case.case_id, n): case
        for case in request.execution.suite.cases
        for n in range(1, case.repetitions + 1)
    }
    require(
        len(evidence["cases"]) == len(cases),
        "Expert proof does not cover every test repetition",
    )
    seen, identities, counts = set(), set(), {}
    for item in evidence["cases"]:
        result = FormalExecutionCaseResult.model_validate(item["result"])
        key = result.case_id, result.repetition
        require(
            key in cases
            and key not in seen
            and item["validated"] is True
            and item["errors"] == []
            and item["governance"] is None
            and item["model_turn_ids"] == []
            and not item.get("model_provenance")
            and result.caller == cases[key].caller == "host"
            and result.outcome == "succeeded"
            and result.rejection_code is None
            and result.status in ("passed", "failed"),
            "Expert case has missing, duplicated or unqualified execution evidence",
        )
        seen.add(key)
        checker = _tree_case if evidence["adapter"] == TREE_ADAPTER else _old_case
        passed = checker(
            owner,
            frozen,
            request,
            cases[key],
            result,
            item,
            resolved,
            identities,
            operations,
        )
        counts[result.case_id] = counts.get(result.case_id, 0) + int(passed)
    require(
        all(
            counts.get(case.case_id, 0) >= case.minimum_passes
            for case in request.execution.suite.cases
        )
        and sum(counts.values()) > 0,
        "Expert test thresholds were not met",
    )


def _zero(value):
    number = Decimal(str(value))
    return number.is_finite() and number == 0


def _tree_artifact(raw, name, identities):
    require(
        isinstance(raw, dict)
        and set(raw) == {"id", "name", "version", "digest"}
        and raw["name"] == name
        and type(raw["version"]) is int
        and raw["version"] == 1
        and isinstance(raw["digest"], str)
        and len(raw["digest"]) == 64
        and all(c in "0123456789abcdef" for c in raw["digest"]),
        "Tree artifact identity is invalid",
    )
    _identity(identities, "artifact", raw["id"])


def _tree_case(
    owner, frozen, request, case, result, item, resolved, identities, operations
):
    """Recompute the retained invocation tree, including historical scope versions."""
    from app.domain.workbench.formal_recursive import node_artifact, node_key, node_slot

    snapshot = FormalTaskSnapshot.model_validate(item["snapshot"])
    definitions = {
        key: FrozenMethod(value.reference, value.draft.definition)
        for key, value in resolved.items()
    }
    plan = expand_unique_plan(
        snapshot.method_id, snapshot.draft.definition, definitions
    )
    require(
        item["adapter"] == TREE_ADAPTER
        and snapshot.request_digest == request.fingerprint()
        and digest(snapshot.document()) == item["snapshot_digest"]
        and snapshot.run_id == request.run_id
        and snapshot.method_id == request.method_id
        and snapshot.draft == request.draft
        and snapshot.caller == "host"
        and snapshot.case_id == case.case_id
        and snapshot.repetition == result.repetition
        and fingerprint(snapshot.arguments) == fingerprint(case.input.values)
        and snapshot.definition_digest == frozen.reference.digest
        and len(snapshot.dependencies) == len(plan.dependencies)
        and {ref_key(f.reference) for f in snapshot.dependencies}
        == {ref_key(r) for r in plan.dependencies}
        and all(
            f == resolved.get((f.reference.method_id, f.reference.version))
            for f in snapshot.dependencies
        )
        and any(n.kind != "tool" for n in plan.nodes)
        and item["tree"]
        == {
            "method_kind": plan.method_kind,
            "depth": plan.depth,
            "expansions": plan.expansions,
            "steps": plan.steps,
            "leaf_count": len(plan.leaf_nodes),
        },
        "Sealed tree definition or dependency closure changed",
    )
    task_id = _identity(identities, "task", result.task_id)
    root_id = _identity(identities, "method_call", item["root_call_id"])
    root, task = item["root_call"], item["task"]
    deadline = datetime.fromisoformat(task["run_created_at"]) + timedelta(
        seconds=request.execution.timeout_seconds + request.execution.max_wait_seconds
    )
    require(
        task["id"] == task_id
        and task["state"] == "SUCCEEDED"
        and task["current_step"] == len(plan.leaf_nodes) - 1
        and datetime.fromisoformat(task["scope_deadline"]) == deadline
        and _zero(task["budget"].get("used", "0"))
        and _zero(task["budget"].get("reserved", "0")),
        "Sealed tree task or budget changed",
    )
    session_id = exact_id(root["session_id"])

    def call_fact(
        raw, call_id, parent, key, *, attempt=None, tool=False, identity_digest=None
    ):
        require(
            raw["id"] == call_id
            and raw["parent_call_id"] == parent
            and raw["task_id"] == task_id
            and raw["attempt_id"] == attempt
            and raw["session_id"] == session_id
            and raw["owner_actor_id"] == str(owner)
            and raw["call_key"] == key
            and raw["status"] == "succeeded"
            and raw["kind"] == ("tool" if tool else "method")
            and raw["origin"] == ("managed" if tool else "method")
            and (identity_digest is None or raw["digest"] == identity_digest),
            "Sealed tree call identity changed",
        )

    call_fact(
        root,
        root_id,
        None,
        "task:" + task_id,
        identity_digest=digest(["task:" + task_id]),
    )
    records = item["nodes"]
    require(len(records) == len(plan.nodes), "Tree omits or adds an invocation")
    nodes = {node.path: node for node in plan.nodes}
    retained = {}
    for node, raw in zip(plan.nodes, records, strict=True):
        require(raw["path"] == list(node.path), "Tree invocation order changed")
        retained[node.path] = raw
    leaves = [retained[n.path] for n in plan.leaf_nodes]
    require(item["steps"] == leaves, "Tree leaf evidence projection differs")
    scopes: dict[tuple[str, ...], Any] = {(): item["root_scope_facts"]}
    scopes.update(
        {
            n.path: retained[n.path]["scope_facts"]
            for n in plan.nodes
            if n.kind != "tool"
        }
    )
    histories, currents, initial_scopes, event_ids = {}, {}, {}, set()

    def event(raw, event_type, attempt=None):
        key = exact_id(raw["id"])
        require(
            key not in event_ids and type(raw["cursor"]) is int and raw["cursor"] >= 0,
            "Sealed scope event identity reused",
        )
        event_ids.add(key)
        require(
            raw["type"] == event_type
            and raw["task_id"] == task_id
            and raw["attempt_id"] == (attempt if attempt else "None")
            and datetime.fromisoformat(raw["created_at"]).tzinfo is not None,
            "Sealed scope event context changed",
        )

    for path, fact in scopes.items():
        call_id = root_id if not path else exact_id(retained[path]["method_call_id"])
        method_id = snapshot.method_id if not path else nodes[path].reference.method_id
        parent_path = path[:-1]
        parent_id = (
            None
            if not path
            else root_id
            if not parent_path
            else exact_id(retained[parent_path]["method_call_id"])
        )
        event(fact["created"], "method/scopeCreated")
        payload = fact["created"]["payload"]
        initial = MethodAuthorization.model_validate(payload["authorization"])
        current = MethodAuthorization.model_validate(fact["authorization"])
        require(
            fact["path"] == list(path)
            and payload["path"] == list(path)
            and payload["snapshot_digest"] == item["snapshot_digest"]
            and str(initial.call_id) == call_id
            and initial.method_id == str(method_id)
            and (str(initial.parent_call_id) if initial.parent_call_id else None)
            == parent_id
            and initial.revision == 1
            and not initial.revoked
            and not initial.grants
            and initial.expires_at == deadline
            and (
                initial.parent_revision is None
                if not path
                else initial.parent_revision is not None
            )
            and current.call_id == initial.call_id
            and current.method_id == initial.method_id
            and current.parent_call_id == initial.parent_call_id
            and current.parent_revision == initial.parent_revision
            and current.expires_at == initial.expires_at,
            "Sealed scope identity changed",
        )
        for revocation in fact["revocations"]:
            event(revocation, "method/permissionsRevoked")
            require(
                revocation["payload"]["call_id"] == call_id,
                "Sealed revocation changed scope",
            )
        initial_scopes[path], currents[path], histories[path] = (
            initial,
            current,
            [initial],
        )
        if path:
            require(
                retained[path]["scope_authorization"]
                == current.model_dump(mode="json"),
                "Sealed scope projection differs",
            )

    grants, last_end, leaf_index = [], None, 0

    def visit(definition, inputs, path, parent_id):
        nonlocal last_end, leaf_index
        validate_plan_inputs(definition, inputs)
        accepted, versions = {}, []
        for step in unique_plan(definition):
            node = nodes[(*path, step.id)]
            raw = retained[node.path]
            arguments = bind_step_inputs(step, inputs, accepted)
            bindings = [v for v in versions if v["step_id"] in step.depends_on]
            method_call = _identity(identities, "method_call", raw["method_call_id"])
            identity = {
                "identity_basis": "formal_recursive",
                "task_id": task_id,
                "snapshot_digest": item["snapshot_digest"],
                "path": list(node.path),
                "parent_path": list(path),
                "reference": node.reference.model_dump(mode="json"),
                "step_digest": digest(step.model_dump(mode="json", by_alias=True)),
                "parent_definition_digest": definition.digest(),
            }
            require(
                raw["parent_path"] == list(path)
                and raw["parent_call_id"] == parent_id
                and raw["reference"] == node.reference.model_dump(mode="json")
                and raw["kind"] == node.kind
                and raw["identity"] == identity
                and raw["identity_digest"] == digest(identity)
                and raw["inputs_digest"] == digest(arguments)
                and raw["input_bindings"] == bindings,
                "Sealed tree invocation binding changed",
            )
            call_fact(
                raw["call"],
                method_call,
                parent_id,
                node_key(task_id, node.path),
                identity_digest=digest(identity),
                attempt=raw["attempt_id"] if node.kind == "tool" else None,
            )
            if node.kind == "tool":
                leaf = resolved[(node.reference.method_id, node.reference.version)]
                procedure = leaf.draft.definition.procedure
                require(
                    isinstance(procedure, ToolProcedure),
                    "Tree leaf is not a direct method",
                )
                assert isinstance(procedure, ToolProcedure)
                validate_values(procedure.inputs, arguments)
                validate_values(procedure.outputs, raw["method_outputs"])
                output = validate_step_outputs(step, raw["method_outputs"])
                attempt = _identity(identities, "attempt", raw["attempt_id"])
                tool = _identity(identities, "tool_call", raw["call_id"])
                _identity(
                    identities, "interaction", raw["governance"]["interaction_id"]
                )
                start, end = (
                    datetime.fromisoformat(raw["created_at"]),
                    datetime.fromisoformat(raw["ended_at"]),
                )
                require(
                    start.tzinfo is not None
                    and start <= end
                    and (last_end is None or last_end <= start)
                    and raw["status"] == "COMPLETED"
                    and raw["outcome"] == "succeeded"
                    and raw["slot"] == node_slot(node.path)
                    and raw["turn_ids"] == []
                    and _zero((raw["budget_consumed"] or {}).get("amount", "0"))
                    and not any(
                        (raw["budget_consumed"] or {}).get("tokens", {}).values()
                    )
                    and raw["method_outputs_digest"] == digest(raw["method_outputs"]),
                    "Sealed tree leaf execution changed",
                )
                last_end = end
                call_fact(
                    raw["tool_call"],
                    tool,
                    method_call,
                    "formal-host:" + task_id + ":" + node_slot(node.path),
                    attempt=attempt,
                    tool=True,
                )
                parent_method = (
                    snapshot.method_id if not path else nodes[path].reference.method_id
                )
                observed = {
                    "call_id": tool,
                    "parent_call_id": method_call,
                    "status": raw["tool_call"]["status"],
                    "digest": raw["tool_call"]["digest"],
                    "adapter_result_digest": raw["tool_call"]["adapter_result_digest"],
                }
                parent, auth = verify_leaf_authority(
                    owner,
                    snapshot,
                    result,
                    leaf,
                    step,
                    arguments,
                    {**raw, "observed_call": observed},
                    root_id,
                    operations,
                    frozen.validation.completed_at,
                    tree_host={
                        "slot": node_slot(node.path),
                        "parent_method_id": str(parent_method),
                        "adapter": TREE_ADAPTER,
                        "path": list(node.path),
                        "parent_method_call_id": parent_id,
                        "root_method_call_id": root_id,
                    },
                )
                governance = raw["governance"]
                consumed = datetime.fromisoformat(governance["approval"]["consumed_at"])
                grants.append(auth.grants[0])
                require(
                    parent.revision == leaf_index + 2
                    and parent.grants == tuple(grants)
                    and start <= consumed <= end,
                    "Sealed root grant accumulation changed",
                )
                confirmation = governance["confirmation_event"]
                event(confirmation, "method/permissionsConfirmed", attempt)
                payload = confirmation["payload"]
                require(
                    governance["authority_source"]
                    == payload["authority_source"]
                    == "owner_exact_leaf"
                    and governance["call_path"]
                    == payload["call_path"]
                    == list(node.path)
                    and payload["authorization"] == governance["authorization"]
                    and payload["parent_authorization"]
                    == governance["parent_authorization"]
                    and payload["interaction_id"] == governance["interaction_id"]
                    and payload["action_digest"] == raw["tool_call"]["digest"]
                    and payload["context"] == item["snapshot_digest"]
                    and payload["scope_authorizations"]
                    == governance["scope_authorizations"],
                    "Sealed leaf confirmation event changed",
                )
                histories[()].append(parent)
                paths = [node.path[:i] for i in range(len(node.path))]
                require(
                    len(governance["scope_authorizations"]) == len(paths)
                    and governance["scope_facts"] == [scopes[p] for p in paths],
                    "Sealed approval scope chain changed",
                )
                for scope_path, document in zip(
                    paths, governance["scope_authorizations"], strict=True
                ):
                    historical = MethodAuthorization.model_validate(document)
                    fact, initial = scopes[scope_path], initial_scopes[scope_path]
                    require(
                        historical in histories[scope_path]
                        and not historical.revoked
                        and datetime.fromisoformat(fact["created"]["created_at"])
                        <= consumed
                        < historical.expires_at
                        and fact["created"]["cursor"] < confirmation["cursor"]
                        and not any(
                            datetime.fromisoformat(e["created_at"]) <= consumed
                            for e in fact["revocations"]
                        )
                        and (not historical.grants if scope_path else True),
                        "Sealed scope did not authorize the historical invocation",
                    )
                    require(
                        initial.parent_revision is None
                        or initial.parent_revision
                        <= currents[scope_path[:-1]].revision,
                        "Sealed scope parent revision changed",
                    )
                leaf_index += 1
                require(
                    raw["output_artifacts"] == [raw["artifact"]],
                    "Tree leaf artifact projection changed",
                )
            else:
                exported = visit(node.definition, arguments, node.path, method_call)
                require(
                    fingerprint(exported) == fingerprint(raw["exports"])
                    and raw["exports_digest"] == digest(exported),
                    "Sealed nested exports changed",
                )
                _tree_artifact(
                    raw["exports_artifact"],
                    node_artifact(node.path, exports=True),
                    identities,
                )
                output = validate_step_outputs(step, exported)
            require(
                fingerprint(output) == fingerprint(raw["outputs"])
                and raw["outputs_digest"] == digest(output),
                "Sealed node outputs changed",
            )
            _tree_artifact(raw["artifact"], node_artifact(node.path), identities)
            accepted[step.id] = output
            versions.append(
                {
                    "step_id": step.id,
                    "path": list(node.path),
                    "method_call_id": method_call,
                    **raw["artifact"],
                }
            )
        return export_outputs(definition, accepted)

    outputs = visit(snapshot.draft.definition, snapshot.arguments, (), root_id)
    for path, current in currents.items():
        versions = histories[path]
        prior = next(
            (
                a
                for a in versions
                if a.revision == current.revision - int(current.revoked)
            ),
            None,
        )
        require(
            prior is not None
            and current.model_copy(
                update={"revision": prior.revision, "revoked": prior.revoked}
            )
            == prior
            and (
                not current.revoked
                or any(
                    e["payload"].get("revision") == current.revision
                    for e in scopes[path]["revocations"]
                )
            ),
            "Sealed scope final state differs from its event history",
        )
    require(
        str(result.attempt_id) == leaves[-1]["attempt_id"]
        and str(result.call_id) == leaves[-1]["call_id"]
        and fingerprint(outputs) == fingerprint(item["outputs"])
        and digest(outputs) == item["outputs_digest"],
        "Sealed root exports changed",
    )
    _tree_artifact(
        item["root_exports_artifact"], node_artifact((), exports=True), identities
    )
    assertions = evaluate_assertions(case, outputs)
    passed = case.expected.outcome == "succeeded" and all(a.passed for a in assertions)
    require(
        result.assertions == assertions
        and result.status == ("passed" if passed else "failed"),
        "Sealed tree verdict changed",
    )
    return passed
