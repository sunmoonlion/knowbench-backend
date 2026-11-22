"""Pure retained-shape reconciliation; fixtures are not runtime admission evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from method_access_support import ACCESS
from test_capabilities_db import OWNER, TOOLS, URL
from test_formal_host_calls_db import FORMAL_OPERATION
from test_formal_method_admission_db import frozen, validation_request

from app.application.workbench.formal_orchestration_admission import verify_sealed_plan
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import FormalTaskSnapshot
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodValidationRequest,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
    evaluate_assertions,
)
from app.domain.workbench.formal_method_generated import AccessRequirement
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
)
from app.domain.workbench.tool_operations import OperationCatalog
from app.domain.workbench.tool_registry import ToolRegistration


def shape():
    source: dict[str, Any] = dict(
        asset_id=str(uuid4()),
        asset_revision=1,
        kind="mcp",
        source=URL,
        version="1",
        digest="sha256:" + FORMAL_OPERATION.descriptor_digest,
        entry="send",
        license_basis="synthetic-only",
    )
    leaf_request = validation_request(source)
    doc = leaf_request.model_dump(mode="json", by_alias=True)
    doc["draft"]["definition"]["access_requirements"] = [deepcopy(ACCESS)]
    doc["draft"]["definition"]["procedure"]["implementation"]["requirement_ids"] = [
        ACCESS["requirement_id"]
    ]
    leaf = frozen(FormalMethodValidationRequest.model_validate(doc))
    definition = deepcopy(doc["draft"]["definition"])
    direct = definition["procedure"]
    definition["procedure"] = {
        "kind": "orchestration",
        "instructions": "One mandatory method",
        "max_depth": 2,
        "max_expansions": 2,
        "max_steps": 1,
        "max_replans": 0,
        "steps": [
            {
                "id": "only",
                "candidates": [leaf.reference.model_dump(mode="json")],
                "select_when": "Sole candidate",
                "required_when": "Always",
                "always_required": True,
                "depends_on": [],
                "inputs": {
                    "to": {
                        "slot": direct["inputs"]["to"],
                        "binding": {"source": "caller", "name": "to"},
                    }
                },
                "outputs": direct["outputs"],
                "on_failure": "fail",
            }
        ],
        "exports": {
            "receipt": {"step": "only", "output": "receipt", "always_present": True}
        },
    }
    values, outputs = {"to": "test@example.test"}, {"receipt": "ok"}
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": [
                {
                    "case_id": "one",
                    "input": {"values": values},
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {"kind": "equals", "pointer": "/receipt", "value": "ok"}
                        ],
                    },
                }
            ],
        }
    ).freeze()
    doc.update(
        run_id=str(uuid4()),
        method_id=str(uuid4()),
        dependencies=[leaf.reference.model_dump(mode="json")],
        draft={"definition": definition, "source": None, "allowed_callers": ["host"]},
    )
    request = FormalMethodExecutionRequest.model_validate(
        {
            **doc,
            "contract": "knowlab/formal-method-execution/v1",
            "execution": {
                "suite": suite.model_dump(mode="json"),
                "runner": {
                    "kind": "user",
                    "environment_id": str(uuid4()),
                    "workspace_root": "/test",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 60,
                "max_wait_seconds": 60,
            },
        }
    )
    root = frozen(request.admission_request())
    root = root.model_copy(
        update={
            "validation": root.validation.model_copy(
                update={"dependencies": (leaf.reference,)}
            )
        }
    )
    snapshot = FormalTaskSnapshot(
        runtime_contract=request.contract,
        run_id=request.run_id,
        case_id="one",
        repetition=1,
        request_digest=request.fingerprint(),
        method_id=request.method_id,
        draft=request.draft,
        arguments=values,
        definition_digest=request.draft.definition.digest(),
        dependencies=(leaf,),
    )
    task_id, attempt_id, root_id, child_id, call_id, interaction_id = [
        str(uuid4()) for _ in range(6)
    ]
    operation = FORMAL_OPERATION.resolve(values)
    now = datetime.now(UTC) - timedelta(seconds=10)
    expiry = now + timedelta(seconds=1)  # Historical grant already expired at sealing.
    action_digest = "a" * 64
    exact = {
        **ACCESS,
        "operations": [operation["operation"]],
        "resources": operation["resources"],
        "data_transfer": {**ACCESS["data_transfer"], "argument_fields": sorted(values)},
    }
    grant = PermissionGrant(
        grant_id=digest([call_id, action_digest]),
        confirmation_context=digest(snapshot.document()),
        connection_id=UUID(source["asset_id"]),
        connection_revision=1,
        operation_digest=FORMAL_OPERATION.policy_digest,
        requirement=AccessRequirement.model_validate(exact),
        delegable=False,
        recipient_call_id=UUID(child_id),
        expires_at=expiry,
        method_ids=(str(leaf.reference.method_id),),
    )
    parent = MethodAuthorization(
        call_id=UUID(root_id),
        revision=1,
        parent_call_id=None,
        parent_revision=None,
        method_id=str(root.reference.method_id),
        revoked=False,
        expires_at=expiry,
        grants=(grant,),
    )
    auth = MethodAuthorization(
        call_id=UUID(child_id),
        revision=1,
        parent_call_id=UUID(root_id),
        parent_revision=1,
        method_id=str(leaf.reference.method_id),
        revoked=False,
        expires_at=expiry,
        grants=(grant,),
    )
    assert leaf.draft.source is not None
    host = dict(
        task_id=task_id,
        attempt_id=attempt_id,
        snapshot_digest=digest(snapshot.document()),
        slot="only",
        method_id=str(leaf.reference.method_id),
        run_id=str(request.run_id),
        case_id="one",
        repetition=1,
        source_digest=leaf.draft.source.fingerprint(),
        reference=leaf.reference.model_dump(mode="json"),
        parent_method_id=str(root.reference.method_id),
        method_call_id=child_id,
        inputs_digest=digest(values),
    )
    artifact = dict(
        id=str(uuid4()), name="formal-step:only", version=1, digest="b" * 64
    )
    result = dict(
        case_id="one",
        repetition=1,
        status="passed",
        outcome="succeeded",
        rejection_code=None,
        task_id=task_id,
        attempt_id=attempt_id,
        call_id=call_id,
        assertions=[
            a.model_dump(mode="json")
            for a in evaluate_assertions(suite.cases[0], outputs)
        ],
        detail="",
    )
    step = dict(
        attempt_id=attempt_id,
        slot="only",
        reference=leaf.reference.model_dump(mode="json"),
        status="COMPLETED",
        outcome="succeeded",
        method_call_id=child_id,
        call_id=call_id,
        observed_call=dict(
            call_id=call_id,
            parent_call_id=child_id,
            status="succeeded",
            digest=action_digest,
            adapter_result_digest="c" * 64,
        ),
        input_bindings=[],
        output_artifacts=[artifact],
        artifact=artifact,
        inputs_digest=digest(values),
        outputs=outputs,
        outputs_digest=digest(outputs),
        governance=dict(
            operation=operation,
            call_id=call_id,
            method_call_id=child_id,
            root_call_id=root_id,
            input_digest=digest(values),
            registration_id=digest(["mcp", URL, "send"]),
            registration_digest=FORMAL_OPERATION.descriptor_digest,
            formal_host=host,
            direct_grants=True,
            approved=True,
            authorization=auth.model_dump(mode="json"),
            authorization_digest=digest(auth.model_dump(mode="json")),
            parent_authorization=parent.model_dump(mode="json"),
            interaction_id=interaction_id,
            approval=dict(
                subject_digest=action_digest,
                decision="accept",
                responded_by=OWNER,
                consumed_at=now.isoformat(),
                expires_at=expiry.isoformat(),
            ),
        ),
    )
    item = dict(
        result=result,
        governance=None,
        validated=True,
        errors=[],
        snapshot_digest=digest(snapshot.document()),
        snapshot=snapshot.document(),
        steps=[step],
        outputs=outputs,
        outputs_digest=digest(outputs),
        root_call_id=root_id,
        model_turn_ids=[],
    )
    evidence: dict[str, Any] = dict(
        adapter="knowbench/formal-orchestration-host/v1", model_cost="0", cases=[item]
    )
    return (
        root,
        request,
        evidence,
        {(leaf.reference.method_id, leaf.reference.version): leaf},
    )


def verify(root, request, evidence, resolved):
    verify_sealed_plan(
        OWNER,
        root,
        request,
        evidence,
        resolved,
        operations=OperationCatalog((FORMAL_OPERATION,)),
    )


def test_historical_expiry_and_exact_pins_survive_without_execution_rows():
    verify(*shape())


@pytest.mark.parametrize(
    "change",
    [
        "output",
        "binding",
        "parent",
        "reference",
        "approved",
        "result",
        "extra_case",
        "missing_step",
        "cost",
        "adapter",
        "scope",
        "policy",
    ],
)
def test_changed_retained_facts_cannot_be_replaced_by_validated_true(change):
    root, request, evidence, resolved = shape()
    item, step = evidence["cases"][0], evidence["cases"][0]["steps"][0]
    if change == "output":
        step["outputs"] = {"receipt": "changed"}
    elif change == "binding":
        step["input_bindings"] = [{"invented": True}]
    elif change == "parent":
        step["governance"]["parent_authorization"]["grants"] = []
    elif change == "reference":
        step["reference"]["version"] += 1
    elif change == "approved":
        step["governance"]["approval"]["responded_by"] = str(uuid4())
    elif change == "result":
        item["result"]["assertions"][0]["passed"] = False
    elif change == "extra_case":
        evidence["cases"].append(deepcopy(item))
    elif change == "missing_step":
        item["steps"] = []
    elif change == "cost":
        evidence["model_cost"] = "0.001"
    elif change == "adapter":
        evidence["adapter"] = "knowbench/formal-orchestration-tree-host/v1"
    elif change == "scope":
        step["governance"]["authorization"]["grants"][0]["method_ids"] = []
    else:
        step["governance"]["operation"]["policy_digest"] = "f" * 64
    with pytest.raises((CapabilityError, ValueError)):
        verify(root, request, evidence, resolved)


def test_exact_source_lookup_preserves_descriptor_and_mcp_source_checks():
    catalog = OperationCatalog((FORMAL_OPERATION,))
    tool = ToolRegistration.discover("mcp", URL, TOOLS[0])
    assert catalog.require(tool) == catalog.require_source(
        URL, "send", tool.descriptor_digest
    )
    for url, name, descriptor in (
        (URL + "/other", "send", tool.descriptor_digest),
        (URL, "other", tool.descriptor_digest),
        (URL, "send", "a" * 64),
    ):
        with pytest.raises(CapabilityError):
            catalog.require_source(url, name, descriptor)
    with pytest.raises(CapabilityError):
        catalog.require(ToolRegistration.discover("skill_script", URL, TOOLS[0]))


def tree_shape():
    """Build only a pure reconciliation sample, never a usable database admission."""
    from app.domain.workbench.formal_method_compiler_generated import FrozenMethod
    from app.domain.workbench.formal_method_plan_generated import expand_unique_plan
    from app.domain.workbench.formal_recursive import node_artifact, node_key, node_slot

    middle, old_request, old_evidence, resolved = shape()
    leaf = next(iter(resolved.values()))
    old = old_evidence["cases"][0]
    raw_leaf = deepcopy(old["steps"][0])
    doc = old_request.model_dump(mode="json", by_alias=True)
    doc["method_id"], doc["run_id"] = str(uuid4()), str(uuid4())
    procedure = doc["draft"]["definition"]["procedure"]
    procedure.update(max_depth=3, max_expansions=3, max_steps=2)
    procedure["steps"][0].update(
        id="nested", candidates=[middle.reference.model_dump(mode="json")]
    )
    procedure["exports"]["receipt"]["step"] = "nested"
    doc["dependencies"] = [middle.reference.model_dump(mode="json")]
    request = FormalMethodExecutionRequest.model_validate(doc)
    root = frozen(request.admission_request())
    root = root.model_copy(
        update={
            "validation": root.validation.model_copy(
                update={"dependencies": (middle.reference,)}
            )
        }
    )
    resolved[(middle.reference.method_id, middle.reference.version)] = middle
    snapshot = FormalTaskSnapshot(
        runtime_contract=request.contract,
        run_id=request.run_id,
        case_id="one",
        repetition=1,
        request_digest=request.fingerprint(),
        method_id=request.method_id,
        draft=request.draft,
        arguments=old_request.execution.suite.cases[0].input.values,
        definition_digest=request.draft.definition.digest(),
        dependencies=(leaf, middle),
    )
    plan = expand_unique_plan(
        request.method_id,
        request.draft.definition,
        {
            key: FrozenMethod(f.reference, f.draft.definition)
            for key, f in resolved.items()
        },
    )
    task_id, root_id = old["result"]["task_id"], old["root_call_id"]
    middle_id, session_id = str(uuid4()), str(uuid4())
    child_id, tool_id, attempt_id = (
        raw_leaf["method_call_id"],
        raw_leaf["call_id"],
        raw_leaf["attempt_id"],
    )
    approved = datetime.fromisoformat(raw_leaf["governance"]["approval"]["consumed_at"])
    run_created = approved - timedelta(seconds=5)
    deadline = run_created + timedelta(seconds=120)
    context_digest = digest(snapshot.document())
    grant = PermissionGrant.model_validate(
        raw_leaf["governance"]["authorization"]["grants"][0]
    ).model_copy(update={"confirmation_context": context_digest})
    parent = MethodAuthorization(
        call_id=UUID(root_id),
        revision=2,
        parent_call_id=None,
        parent_revision=None,
        method_id=str(root.reference.method_id),
        revoked=False,
        expires_at=deadline,
        grants=(grant,),
    )
    initial = parent.model_copy(update={"revision": 1, "grants": ()})
    scope = MethodAuthorization(
        call_id=UUID(middle_id),
        revision=1,
        parent_call_id=UUID(root_id),
        parent_revision=1,
        method_id=str(middle.reference.method_id),
        revoked=False,
        expires_at=deadline,
        grants=(),
    )
    auth = MethodAuthorization.model_validate(
        raw_leaf["governance"]["authorization"]
    ).model_copy(update={"parent_revision": 2, "grants": (grant,)})

    def event(kind, cursor, payload, *, attempt=None):
        return dict(
            id=str(uuid4()),
            type=kind,
            cursor=cursor,
            created_at=(approved - timedelta(seconds=1)).isoformat(),
            task_id=task_id,
            attempt_id=attempt or "None",
            payload=payload,
        )

    def scope_fact(path, initial_auth, current, cursor):
        return dict(
            path=list(path),
            authorization=current.model_dump(mode="json"),
            revocations=[],
            created=event(
                "method/scopeCreated",
                cursor,
                dict(
                    path=list(path),
                    snapshot_digest=context_digest,
                    authorization=initial_auth.model_dump(mode="json"),
                ),
            ),
        )

    root_scope = scope_fact((), initial, parent, 1)
    middle_scope = scope_fact(("nested",), scope, scope, 2)

    def call(call_id, parent_id, key, action_digest, *, attempt=None, tool=False):
        return dict(
            id=call_id,
            parent_call_id=parent_id,
            call_key=key,
            digest=action_digest,
            task_id=task_id,
            session_id=session_id,
            attempt_id=attempt,
            owner_actor_id=OWNER,
            status="succeeded",
            kind="tool" if tool else "method",
            origin="managed" if tool else "method",
        )

    def artifact(path, exports=False):
        return dict(
            id=str(uuid4()),
            name=node_artifact(path, exports=exports),
            version=1,
            digest="b" * 64,
        )

    values, outputs = snapshot.arguments, old["outputs"]
    nodes = []
    for node in plan.nodes:
        is_leaf = node.kind == "tool"
        call_id, parent_id = (child_id, middle_id) if is_leaf else (middle_id, root_id)
        parent_definition = (
            middle.draft.definition if is_leaf else root.draft.definition
        )
        identity = dict(
            identity_basis="formal_recursive",
            task_id=task_id,
            snapshot_digest=context_digest,
            path=list(node.path),
            parent_path=list(node.parent_path),
            reference=node.reference.model_dump(mode="json"),
            step_digest=digest(node.step.model_dump(mode="json", by_alias=True)),
            parent_definition_digest=parent_definition.digest(),
        )
        item: dict[str, Any] = dict(
            path=list(node.path),
            parent_path=list(node.parent_path),
            reference=node.reference.model_dump(mode="json"),
            kind=node.kind,
            method_call_id=call_id,
            parent_call_id=parent_id,
            identity=identity,
            identity_digest=digest(identity),
            inputs_digest=digest(values),
            input_bindings=[],
            outputs=outputs,
            outputs_digest=digest(outputs),
            artifact=artifact(node.path),
            call=call(
                call_id,
                parent_id,
                node_key(task_id, node.path),
                digest(identity),
                attempt=attempt_id if is_leaf else None,
            ),
        )
        if is_leaf:
            governance = deepcopy(raw_leaf["governance"])
            host = governance["formal_host"]
            host.update(
                snapshot_digest=context_digest,
                run_id=str(request.run_id),
                slot=node_slot(node.path),
                parent_method_id=str(middle.reference.method_id),
                adapter="knowbench/formal-orchestration-tree-host/v1",
                path=list(node.path),
                parent_method_call_id=middle_id,
                root_method_call_id=root_id,
            )
            governance.update(
                authorization=auth.model_dump(mode="json"),
                authorization_digest=digest(auth.model_dump(mode="json")),
                parent_authorization=parent.model_dump(mode="json"),
                authority_source="owner_exact_leaf",
                call_path=list(node.path),
                scope_authorizations=[
                    parent.model_dump(mode="json"),
                    scope.model_dump(mode="json"),
                ],
                scope_facts=[root_scope, middle_scope],
            )
            governance["confirmation_event"] = event(
                "method/permissionsConfirmed",
                3,
                dict(
                    authorization=governance["authorization"],
                    parent_authorization=governance["parent_authorization"],
                    authority_source="owner_exact_leaf",
                    call_path=list(node.path),
                    context=context_digest,
                    interaction_id=governance["interaction_id"],
                    action_digest=raw_leaf["observed_call"]["digest"],
                    scope_authorizations=governance["scope_authorizations"],
                ),
                attempt=attempt_id,
            )
            tool = call(
                tool_id,
                child_id,
                "formal-host:" + task_id + ":" + node_slot(node.path),
                raw_leaf["observed_call"]["digest"],
                attempt=attempt_id,
                tool=True,
            )
            tool["adapter_result_digest"] = "c" * 64
            item.update(
                attempt_id=attempt_id,
                slot=node_slot(node.path),
                status="COMPLETED",
                outcome="succeeded",
                call_id=tool_id,
                tool_call=tool,
                method_outputs=outputs,
                method_outputs_digest=digest(outputs),
                governance=governance,
                output_artifacts=[item["artifact"]],
                turn_ids=[],
                budget_consumed={"amount": "0", "tokens": {}},
                created_at=(approved - timedelta(seconds=1)).isoformat(),
                ended_at=(approved + timedelta(seconds=1)).isoformat(),
            )
        else:
            item.update(
                exports=outputs,
                exports_digest=digest(outputs),
                exports_artifact=artifact(node.path, True),
                scope_authorization=scope.model_dump(mode="json"),
                scope_facts=middle_scope,
            )
        nodes.append(item)
    evidence: dict[str, Any] = dict(
        adapter="knowbench/formal-orchestration-tree-host/v1",
        model_cost="0",
        cases=[
            dict(
                adapter="knowbench/formal-orchestration-tree-host/v1",
                result=old["result"],
                governance=None,
                validated=True,
                errors=[],
                nodes=nodes,
                steps=[nodes[-1]],
                snapshot=snapshot.document(),
                snapshot_digest=context_digest,
                outputs=outputs,
                outputs_digest=digest(outputs),
                root_call_id=root_id,
                root_call=call(
                    root_id, None, "task:" + task_id, digest(["task:" + task_id])
                ),
                root_exports_artifact=artifact((), True),
                root_scope_facts=root_scope,
                model_turn_ids=[],
                task=dict(
                    id=task_id,
                    state="SUCCEEDED",
                    current_step=0,
                    budget={"used": "0", "reserved": "0"},
                    scope_deadline=deadline.isoformat(),
                    run_created_at=run_created.isoformat(),
                ),
                tree=dict(
                    method_kind=plan.method_kind,
                    depth=plan.depth,
                    expansions=plan.expansions,
                    steps=plan.steps,
                    leaf_count=1,
                ),
            )
        ],
    )
    return root, request, evidence, resolved


def test_retained_tree_recomputes_scope_and_leaf_proofs():
    root, request, evidence, resolved = tree_shape()
    verify_sealed_plan(
        OWNER,
        root,
        request,
        evidence,
        resolved,
        operations=OperationCatalog((FORMAL_OPERATION,)),
    )


@pytest.mark.parametrize("changed", ["future_revision", "parent_created_later"])
def test_retained_tree_requires_parent_authority_at_scope_creation(changed):
    root, request, evidence, resolved = tree_shape()
    item = evidence["cases"][0]
    middle, leaf = item["nodes"]
    if changed == "future_revision":
        # Keep every redundant projection consistent. Root revision 2 only
        # exists at the leaf approval, after this middle scope was created.
        middle["scope_authorization"]["parent_revision"] = 2
        middle["scope_facts"]["authorization"]["parent_revision"] = 2
        middle["scope_facts"]["created"]["payload"]["authorization"][
            "parent_revision"
        ] = 2
        leaf["governance"]["scope_authorizations"][1]["parent_revision"] = 2
        error = "parent revision was not effective"
    else:
        item["root_scope_facts"]["created"]["cursor"] = 2
        middle["scope_facts"]["created"]["cursor"] = 1
        error = "created before its parent"
    with pytest.raises(CapabilityError, match=error):
        verify_sealed_plan(
            OWNER,
            root,
            request,
            evidence,
            resolved,
            operations=OperationCatalog((FORMAL_OPERATION,)),
        )


@pytest.mark.parametrize(
    "changed",
    [
        "parent",
        "scope",
        "output",
        "projection",
        "deadline",
        "cost",
        "history",
        "closure",
    ],
)
def test_retained_tree_rejects_changed_proofs(changed):
    root, request, evidence, resolved = tree_shape()
    item = evidence["cases"][0]
    leaf = item["nodes"][-1]
    if changed == "parent":
        leaf["parent_call_id"] = item["root_call_id"]
    elif changed == "scope":
        leaf["governance"]["scope_authorizations"] = leaf["governance"][
            "scope_authorizations"
        ][:1]
    elif changed == "output":
        leaf["method_outputs"] = {"receipt": "changed"}
    elif changed == "projection":
        item["steps"] = []
    elif changed == "deadline":
        item["task"]["run_created_at"] = datetime.now(UTC).isoformat()
    elif changed == "cost":
        leaf["budget_consumed"]["amount"] = "0.1"
    elif changed == "history":
        item["root_scope_facts"]["authorization"]["revision"] = 100
    else:
        item["snapshot"]["dependencies"] = item["snapshot"]["dependencies"][:1]
    with pytest.raises((CapabilityError, ValueError)):
        verify_sealed_plan(
            OWNER,
            root,
            request,
            evidence,
            resolved,
            operations=OperationCatalog((FORMAL_OPERATION,)),
        )
