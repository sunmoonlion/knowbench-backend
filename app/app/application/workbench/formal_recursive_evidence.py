"""Independently reconcile nested formal calls, values and historical authority.

The driver supplies no qualification verdict. The frozen definitions determine
all invocation instances, values and exports. Calls form the real method tree;
leaf resource grants are separately direct from the root, never delegated by
an intermediate execution scope. Only complete observed host trees qualify.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.application.workbench.formal_orchestration_evidence import _approval
from app.application.workbench.script_budget import task_script_cost
from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_adapter import host_kind, retained_registration
from app.domain.workbench.formal_execution import (
    case_key,
    snapshot_for_task,
    structured_outputs,
)
from app.domain.workbench.formal_host import host_intent_digest
from app.domain.workbench.formal_method_compiler_generated import FrozenMethod
from app.domain.workbench.formal_method_execution_generated import (
    bounded_json,
    evaluate_assertions,
    fingerprint,
)
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    expand_unique_plan,
    export_outputs,
    unique_plan,
    validate_plan_inputs,
    validate_step_outputs,
)
from app.domain.workbench.formal_recursive import (
    TREE_ADAPTER,
    node_artifact,
    node_identity,
    node_key,
    node_slot,
)
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.states import AttemptState, TaskState


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _time(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _zero(value):
    amount = Decimal(str(value))
    return amount.is_finite() and amount == 0


def _ref(reference):
    return (reference.method_id, reference.version, reference.digest)


def _call_fact(call):
    return {
        key: str(call[key]) if call.get(key) is not None else None
        for key in (
            "id",
            "parent_call_id",
            "task_id",
            "attempt_id",
            "session_id",
            "owner_actor_id",
            "call_key",
            "digest",
            "status",
            "kind",
            "origin",
        )
    }


def _event_fact(event):
    return {
        "id": str(event["id"]),
        "type": event["type"],
        "cursor": event["cursor"],
        "created_at": _time(event["created_at"]).isoformat(),
        "task_id": str(event.get("task_id")),
        "attempt_id": str(event.get("attempt_id")),
        "payload": event["payload"],
    }


async def _artifact(repo, task, reference, name, outputs, *, attempt_id=None):
    _require(isinstance(reference, dict), "artifact_reference_missing")
    row = await repo.get_artifact(
        task_id=str(task["id"]), name=name, version=reference["version"]
    )
    _require(
        row is not None
        and str(row["id"]) == reference["id"]
        and str(row["task_id"]) == str(task["id"])
        and (str(row["attempt_id"]) if row.get("attempt_id") else None) == attempt_id
        and row["name"] == reference["name"] == name
        and row["version"] == reference["version"] == 1
        and row["kind"] == "step"
        and row["digest"] == reference["digest"]
        and fingerprint(row["content"]) == fingerprint(outputs),
        "artifact_identity_or_content_mismatch",
    )
    return reference


def _result(actual, outputs):
    _require(
        isinstance(actual, dict)
        and actual.get("outcome") == "succeeded"
        and actual.get("rejection_code") is None
        and fingerprint(actual.get("outputs")) == fingerprint(outputs)
        and actual.get("outputs_digest") == digest(outputs),
        "independent_output_mismatch",
    )


class _Tree:
    def __init__(
        self,
        repo,
        owner,
        task,
        snapshot,
        plan,
        attempts,
        events,
        run,
        request,
        evidence,
    ):
        self.repo, self.owner, self.task = repo, owner, task
        self.snapshot, self.plan, self.attempts = snapshot, plan, attempts
        self.events, self.evidence = events, evidence
        self.nodes = {node.path: node for node in plan.nodes}
        self.frozen = {
            (f.reference.method_id, f.reference.version): f
            for f in snapshot.dependencies
        }
        self.calls, self.artifacts, self.scope_facts = {}, [], {}
        self.leaf_index = 0
        self.deadline = _time(run["created_at"]) + timedelta(
            seconds=request.execution.timeout_seconds
            + request.execution.max_wait_seconds
        )

    async def call(self, node, parent, inputs, bindings):
        call = await self.repo.call_by_key(
            self.owner,
            str(self.task["session_id"]),
            node_key(self.task["id"], node.path),
        )
        _require(call is not None, "method_call_missing")
        identity = node_identity(self.task, node)
        _require(
            call["kind"] == "method"
            and call["origin"] == "method"
            and call["status"] == "succeeded"
            and str(call["owner_actor_id"]) == str(self.owner)
            and str(call["session_id"]) == str(self.task["session_id"])
            and str(call["task_id"]) == str(self.task["id"])
            and str(call["parent_call_id"]) == str(parent["id"])
            and (node.kind == "tool" or call.get("attempt_id") is None)
            and call["call_key"] == node_key(self.task["id"], node.path)
            and call["digest"] == digest(identity)
            and all(call["detail"].get(key) == value for key, value in identity.items())
            and fingerprint(call["detail"].get("arguments")) == fingerprint(inputs)
            and call["detail"].get("arguments_digest") == digest(inputs)
            and call["detail"].get("input_bindings") == bindings,
            "method_call_identity_or_input_mismatch",
        )
        expansion = [
            e
            for e in self.events
            if e["type"] == "method/expanded"
            and e["payload"].get("call_id") == str(call["id"])
        ]
        _require(
            len(expansion) == 1
            and str(expansion[0]["task_id"]) == str(self.task["id"])
            and expansion[0]["payload"].get("parent_call_id") == str(parent["id"])
            and all(expansion[0]["payload"].get(k) == v for k, v in identity.items())
            and expansion[0]["payload"].get("arguments_digest") == digest(inputs),
            "method_expansion_event_mismatch",
        )
        self.calls[node.path] = call
        return call

    async def scope(self, path, call, method_id, parent):
        created = [
            e
            for e in self.events
            if e["type"] == "method/scopeCreated"
            and (e["payload"].get("authorization") or {}).get("call_id")
            == str(call["id"])
        ]
        _require(len(created) == 1, "scope_creation_missing")
        event = created[0]
        initial = MethodAuthorization.model_validate(event["payload"]["authorization"])
        current = MethodAuthorization.model_validate(
            await self.repo.method_authorization(self.owner, str(call["id"]))
        )
        _require(
            str(event["task_id"]) == str(self.task["id"])
            and event["payload"].get("path") == list(path)
            and event["payload"].get("snapshot_digest") == self.task["pack_digest"]
            and initial.revision == 1
            and not initial.revoked
            and not initial.grants
            and str(initial.call_id) == str(call["id"])
            and initial.method_id == str(method_id)
            and initial.expires_at == self.deadline
            and initial.parent_call_id == (parent.call_id if parent else None)
            and (
                initial.parent_revision is None
                if parent is None
                else initial.parent_revision is not None
                and initial.parent_revision <= parent.revision
            )
            and current.call_id == initial.call_id
            and current.method_id == initial.method_id
            and current.parent_call_id == initial.parent_call_id
            and current.parent_revision == initial.parent_revision
            and current.expires_at == initial.expires_at
            and current.revision >= initial.revision
            and (not current.grants if path else True),
            "scope_identity_mismatch",
        )
        if path:
            parent_initial, _, parent_fact = self.scope_facts[path[:-1]]
            parent_created = parent_fact["created"]
            _require(
                parent_created["cursor"] < event["cursor"]
                and _time(parent_created["created_at"]) <= _time(event["created_at"]),
                "scope_parent_creation_order_mismatch",
            )
            parent_at_creation = parent_initial
            for previous in self.events:
                if previous["cursor"] >= event["cursor"]:
                    break
                prior_payload = previous["payload"]
                if previous[
                    "type"
                ] == "method/permissionsRevoked" and prior_payload.get(
                    "call_id"
                ) == str(parent_initial.call_id):
                    raise ValueError("scope_created_after_parent_revocation")
                if previous["type"] == "method/permissionsConfirmed" and (
                    prior_payload.get("parent_authorization") or {}
                ).get("call_id") == str(parent_initial.call_id):
                    parent_at_creation = MethodAuthorization.model_validate(
                        prior_payload["parent_authorization"]
                    )
            _require(
                initial.parent_revision == parent_at_creation.revision
                and not parent_at_creation.revoked
                and _time(event["created_at"]) < parent_at_creation.expires_at,
                "scope_parent_revision_not_yet_effective",
            )
        revoked = [
            e
            for e in self.events
            if e["type"] == "method/permissionsRevoked"
            and e["payload"].get("call_id") == str(call["id"])
        ]
        if current.revoked:
            _require(
                any(e["payload"].get("revision") == current.revision for e in revoked),
                "scope_revocation_history_missing",
            )
        histories = [initial]
        if not path:
            for confirmation in self.events:
                payload = confirmation["payload"]
                if confirmation["type"] != "method/permissionsConfirmed":
                    continue
                parent = MethodAuthorization.model_validate(
                    payload["parent_authorization"]
                )
                leaf = MethodAuthorization.model_validate(payload["authorization"])
                previous = histories[-1]
                _require(
                    len(leaf.grants) == 1
                    and parent
                    == previous.model_copy(
                        update={
                            "revision": previous.revision + 1,
                            "grants": (*previous.grants, leaf.grants[0]),
                        }
                    )
                    and leaf.parent_call_id == parent.call_id
                    and leaf.parent_revision == parent.revision
                    and payload.get("authority_source") == "owner_exact_leaf",
                    "root_accumulated_direct_grants_mismatch",
                )
                histories.append(parent)
        known = next((a for a in histories if a.revision == current.revision), None)
        if current.revoked:
            known = next(
                (a for a in histories if a.revision == current.revision - 1), None
            )
        _require(
            known is not None
            and current.model_copy(
                update={"revoked": known.revoked, "revision": known.revision}
            )
            == known,
            "scope_current_history_mismatch",
        )
        facts = {
            "path": list(path),
            "created": _event_fact(event),
            "authorization": current.model_dump(mode="json"),
            "revocations": [_event_fact(e) for e in revoked],
        }
        self.scope_facts[path] = (initial, current, facts)
        return current

    def approved_scopes(self, node, confirmation, consumed):
        raw = confirmation["payload"].get("scope_authorizations")
        paths = [node.path[:i] for i in range(len(node.path))]
        _require(
            isinstance(raw, list)
            and len(raw) == len(paths)
            and confirmation["payload"].get("call_path") == list(node.path),
            "approval_scope_chain_missing",
        )
        facts = []
        for path, document in zip(paths, raw, strict=True):
            historical = MethodAuthorization.model_validate(document)
            initial, current, fact = self.scope_facts[path]
            _require(
                historical.call_id == initial.call_id
                and historical.method_id == initial.method_id
                and historical.parent_call_id == initial.parent_call_id
                and historical.parent_revision == initial.parent_revision
                and historical.expires_at == self.deadline
                and not historical.revoked
                and initial.revision <= historical.revision <= current.revision
                and _time(fact["created"]["created_at"])
                <= consumed
                < historical.expires_at
                and (not historical.grants if path else True)
                and not any(
                    _time(e["created_at"]) <= consumed for e in fact["revocations"]
                ),
                "approval_scope_was_not_valid",
            )
            versions = [initial]
            if not path:
                versions += [
                    MethodAuthorization.model_validate(
                        e["payload"]["parent_authorization"]
                    )
                    for e in self.events
                    if e["type"] == "method/permissionsConfirmed"
                    and e["cursor"] <= confirmation["cursor"]
                    and (e["payload"].get("parent_authorization") or {}).get("call_id")
                    == str(initial.call_id)
                ]
            _require(historical in versions, "approval_scope_history_mismatch")
            facts.append(fact)
        return facts

    async def visit(self, definition, inputs, path, parent):
        validate_plan_inputs(definition, inputs)
        accepted, versions = {}, []
        for step in unique_plan(definition):
            node = self.nodes[(*path, step.id)]
            arguments = bind_step_inputs(step, inputs, accepted)
            bindings = [v for v in versions if v["step_id"] in step.depends_on]
            call = await self.call(node, parent, arguments, bindings)
            item = {
                "path": list(node.path),
                "parent_path": list(path),
                "reference": node.reference.model_dump(mode="json"),
                "kind": node.kind,
                "method_call_id": str(call["id"]),
                "parent_call_id": str(parent["id"]),
                "call": _call_fact(call),
                "identity": node_identity(self.task, node),
                "identity_digest": call["digest"],
                "inputs_digest": digest(arguments),
                "input_bindings": bindings,
            }
            self.evidence["nodes"].append(item)
            if node.kind == "tool":
                outputs, artifact = await self.leaf(
                    node, call, arguments, bindings, item
                )
            else:
                parent_scope = self.scope_facts[path][1]
                current = await self.scope(
                    node.path, call, node.reference.method_id, parent_scope
                )
                exported = await self.visit(node.definition, arguments, node.path, call)
                _result(call["detail"].get("exports"), exported)
                export_ref = await _artifact(
                    self.repo,
                    self.task,
                    call["detail"].get("exports_artifact"),
                    node_artifact(node.path, exports=True),
                    exported,
                )
                outputs = validate_step_outputs(step, exported)
                _result(call["detail"].get("result"), outputs)
                _require(
                    call["detail"].get("execution_attempt_id") is None,
                    "intermediate_attempt_forbidden",
                )
                artifact = await _artifact(
                    self.repo,
                    self.task,
                    call["detail"].get("output_artifact"),
                    node_artifact(node.path),
                    outputs,
                )
                self.artifacts.extend([export_ref["id"], artifact["id"]])
                returned = [
                    e
                    for e in self.events
                    if e["type"] == "method/returned"
                    and e["payload"].get("call_id") == str(call["id"])
                ]
                _require(
                    len(returned) == 1
                    and returned[0]["payload"].get("path") == list(node.path)
                    and returned[0]["payload"].get("exports_artifact") == export_ref
                    and returned[0]["payload"].get("output_artifact") == artifact
                    and returned[0]["payload"].get("outputs_digest")
                    == digest(exported),
                    "method_return_event_mismatch",
                )
                item.update(
                    exports=exported,
                    exports_digest=digest(exported),
                    exports_artifact=export_ref,
                    scope_authorization=current.model_dump(mode="json"),
                    scope_facts=self.scope_facts[node.path][2],
                )
            item.update(
                outputs=outputs, outputs_digest=digest(outputs), artifact=artifact
            )
            accepted[step.id] = outputs
            versions.append(
                {
                    "step_id": step.id,
                    "path": list(node.path),
                    "method_call_id": str(call["id"]),
                    **artifact,
                }
            )
        return export_outputs(definition, accepted)

    async def leaf(self, node, child, inputs, bindings, item):
        index = self.leaf_index
        self.leaf_index += 1
        attempt = self.attempts[index]
        refs = attempt.get("refs") or {}
        actual = refs.get("formal_result") or {}
        leaf = self.frozen[(node.reference.method_id, node.reference.version)]
        source = leaf.draft.source
        _require(
            source is not None and len(leaf.draft.definition.access_requirements) == 1,
            "leaf_source_or_access_missing",
        )
        assert source is not None
        slot = node_slot(node.path)
        _require(
            str(attempt["task_id"]) == str(self.task["id"])
            and str(attempt["session_id"]) == str(self.task["session_id"])
            and attempt["step_id"] == slot
            and attempt["step_version"] == digest(node_identity(self.task, node))
            and attempt["status"] == AttemptState.COMPLETED
            and attempt.get("turn_ids") == []
            and not refs.get("usage_pending")
            and not refs.get("pending_usage")
            and not refs.get("pending_cost")
            and not attempt.get("turn_open")
            and _zero((attempt.get("budget_consumed") or {}).get("amount", "0"))
            and not any(
                (attempt.get("budget_consumed") or {}).get("tokens", {}).values()
            )
            and refs.get("kind") == "formal_orchestration_step"
            and refs.get("slot") == slot
            and refs.get("path") == list(node.path)
            and refs.get("step_index") == index
            and refs.get("reference") == node.reference.model_dump(mode="json")
            and refs.get("snapshot_digest") == self.task["pack_digest"]
            and refs.get("method_call_id") == str(child["id"])
            and refs.get("parent_method_call_id") == str(child["parent_call_id"])
            and refs.get("root_method_call_id") == str(self.calls[()]["id"])
            and refs.get("arguments_digest") == digest(inputs)
            and refs.get("input_bindings") == bindings
            and attempt.get("ended_at") is not None
            and (
                index == 0
                or self.attempts[index - 1]["ended_at"] <= attempt["created_at"]
            )
            and child["detail"].get("execution_attempt_id") == str(attempt["id"])
            and str(child.get("attempt_id")) == str(attempt["id"])
            and child["detail"].get("result") == actual,
            "leaf_attempt_or_binding_mismatch",
        )
        call = await self.repo.capability_call(self.owner, refs["capability_call_id"])
        intent = call["detail"]
        parent_method_id = (
            self.nodes[node.parent_path].reference.method_id
            if node.parent_path
            else self.snapshot.method_id
        )
        expected_host = {
            "task_id": str(self.task["id"]),
            "attempt_id": str(attempt["id"]),
            "snapshot_digest": self.task["pack_digest"],
            "slot": slot,
            "method_id": str(node.reference.method_id),
            "run_id": str(self.snapshot.run_id),
            "case_id": self.snapshot.case_id,
            "repetition": self.snapshot.repetition,
            "source_digest": source.fingerprint(),
            "parent_method_id": str(parent_method_id),
            "reference": node.reference.model_dump(mode="json"),
            "method_call_id": str(child["id"]),
            "inputs_digest": digest(inputs),
            "adapter": TREE_ADAPTER,
            "path": list(node.path),
            "parent_method_call_id": str(child["parent_call_id"]),
            "root_method_call_id": str(self.calls[()]["id"]),
        }
        registration = retained_registration(leaf.draft, intent["tool"])
        _require(
            str(call["id"]) == refs["capability_call_id"]
            and call["kind"] == "tool"
            and call["origin"] == "managed"
            and call["status"] == "succeeded"
            and str(call["owner_actor_id"]) == str(self.owner)
            and str(call["session_id"]) == str(self.task["session_id"])
            and str(call["task_id"]) == str(self.task["id"])
            and str(call["attempt_id"]) == str(attempt["id"])
            and str(call["parent_call_id"]) == str(child["id"])
            and call["call_key"] == "formal-host:" + str(self.task["id"]) + ":" + slot
            and intent.get("execution_kind") == host_kind(source)
            and intent.get("formal_host") == expected_host
            and fingerprint(intent.get("arguments")) == fingerprint(inputs)
            and call["digest"] == host_intent_digest(intent)
            and (
                call.get("connection_id") is None
                if source.kind == "skill_script"
                else str(call.get("connection_id")) == str(source.asset_id)
            )
            and str(call["source_asset_id"]) == str(source.asset_id)
            and intent["revision"] == source.asset_revision
            and intent["tool"]["name"] == registration.name
            and call["registration_id"] == registration.tool_id
            and call["registration_digest"] == registration.descriptor_digest,
            "leaf_tool_identity_mismatch",
        )
        confirmations = [
            e
            for e in self.events
            if e["type"] == "method/permissionsConfirmed"
            and str(e.get("attempt_id")) == str(attempt["id"])
            and e["payload"].get("interaction_id") == str(call["interaction_id"])
            and e["payload"].get("action_digest") == call["digest"]
        ]
        _require(
            len(confirmations) == 1
            and confirmations[0]["payload"].get("authority_source")
            == "owner_exact_leaf",
            "leaf_authority_event_missing",
        )
        governance = await _approval(
            self.repo,
            self.owner,
            self.task,
            attempt,
            child,
            call,
            leaf,
            inputs,
            self.calls[()],
            confirmations,
            authorization_deadline=self.deadline,
        )
        consumed = _time(governance["approval"]["consumed_at"])
        _require(
            _time(attempt["created_at"]) <= consumed <= _time(attempt["ended_at"]),
            "leaf_approval_time_mismatch",
        )
        governance.update(
            authority_source="owner_exact_leaf",
            call_path=list(node.path),
            scope_authorizations=confirmations[0]["payload"]["scope_authorizations"],
            scope_facts=self.approved_scopes(node, confirmations[0], consumed),
            confirmation_event=_event_fact(confirmations[0]),
        )
        method_outputs = structured_outputs(leaf.draft, intent["result"])
        bounded_json(method_outputs)
        outputs = validate_step_outputs(node.step, method_outputs)
        _result(actual, outputs)
        versions = attempt.get("output_artifacts") or []
        _require(
            len(versions) == 1
            and child["detail"].get("output_artifact") == versions[0],
            "leaf_artifact_reference_mismatch",
        )
        artifact = await _artifact(
            self.repo,
            self.task,
            versions[0],
            node_artifact(node.path),
            outputs,
            attempt_id=str(attempt["id"]),
        )
        self.artifacts.append(artifact["id"])
        item.update(
            attempt_id=str(attempt["id"]),
            slot=slot,
            status=attempt["status"],
            outcome=actual["outcome"],
            call_id=str(call["id"]),
            tool_call={
                **_call_fact(call),
                "adapter_result_digest": digest(intent.get("result")),
            },
            method_outputs=method_outputs,
            method_outputs_digest=digest(method_outputs),
            governance=governance,
            output_artifacts=versions,
            outputs=outputs,
            outputs_digest=digest(outputs),
            artifact=artifact,
            turn_ids=attempt["turn_ids"],
            budget_consumed=attempt["budget_consumed"],
            created_at=_time(attempt["created_at"]).isoformat(),
            ended_at=_time(attempt["ended_at"]).isoformat(),
        )
        self.evidence["steps"][index] = dict(item)
        return outputs, artifact


async def collect_recursive_evidence(repo, request, case, repetition, result) -> dict:
    evidence = {
        "adapter": TREE_ADAPTER,
        "result": result,
        "governance": None,
        "validated": False,
        "errors": [],
        "steps": [],
        "nodes": [],
    }
    stage = "root"
    try:
        owner = await repo.actor_for_subject(request.owner_subject)
        _require(owner is not None and result.get("task_id"), "task_or_owner_missing")
        task = await repo.get_task(result["task_id"], owner_actor_id=owner)
        snapshot = snapshot_for_task(task)
        resolved = {
            (f.reference.method_id, f.reference.version): FrozenMethod(
                f.reference, f.draft.definition
            )
            for f in snapshot.dependencies
        }
        plan = expand_unique_plan(
            snapshot.method_id, snapshot.draft.definition, resolved
        )
        _require(
            any(n.kind != "tool" for n in plan.nodes),
            "tree_adapter_requires_nested_calls",
        )
        attempts = await repo.list_attempts(str(task["id"]))
        evidence.update(
            snapshot_digest=task["pack_digest"], snapshot=snapshot.document()
        )
        evidence["steps"] = [
            {
                "attempt_id": str(a["id"]),
                "status": a["status"],
                "path": (a.get("refs") or {}).get("path"),
                "slot": (a.get("refs") or {}).get("slot"),
                "reference": (a.get("refs") or {}).get("reference"),
                "call_id": (a.get("refs") or {}).get("capability_call_id"),
                "governance": None,
            }
            for a in attempts
        ]
        direct_refs = {
            _ref(s.candidates[0]) for s in unique_plan(snapshot.draft.definition)
        }
        _require(
            str(task["owner_actor_id"]) == str(owner)
            and str(task["id"]) == result["task_id"]
            and snapshot.run_id == request.run_id
            and snapshot.method_id == request.method_id
            and snapshot.request_digest == request.fingerprint()
            and snapshot.draft == request.draft
            and snapshot.case_id == result["case_id"] == case.case_id
            and snapshot.repetition == result["repetition"] == repetition
            and snapshot.caller == case.caller == result.get("caller", "host") == "host"
            and fingerprint(snapshot.arguments) == fingerprint(case.input.values)
            and len(request.dependencies) == len(direct_refs)
            and {_ref(ref) for ref in request.dependencies} == direct_refs
            and len(snapshot.dependencies) == len(plan.dependencies)
            and {_ref(f.reference) for f in snapshot.dependencies}
            == {_ref(ref) for ref in plan.dependencies},
            "tree_snapshot_identity_mismatch",
        )
        for frozen in snapshot.dependencies:
            row = await repo.formal_method(
                owner, str(frozen.reference.method_id), frozen.reference.version
            )
            _require(
                row is not None
                and row["caller"] == "knowlab"
                and row["document"] == frozen.model_dump(mode="json", by_alias=True),
                "dependency_registration_mismatch",
            )
        run = await repo.get_test_run(str(request.run_id))
        rows = await repo.list_test_cases(str(request.run_id))
        cases = [
            row for row in rows if row["case_id"] == case_key(case.case_id, repetition)
        ]
        _require(
            run is not None
            and run["request"] == request.model_dump(mode="json", by_alias=True)
            and run["digest"] == request.fingerprint().removeprefix("sha256:")
            and run["owner_subject"] == request.owner_subject
            and run["caller"] == "knowlab"
            and len(cases) == 1
            and cases[0].get("result") == result
            and str(cases[0].get("task_id")) == str(task["id"])
            and str(cases[0].get("session_id")) == str(task["session_id"]),
            "persisted_tree_case_mismatch",
        )
        _require(
            task["state"] == TaskState.SUCCEEDED
            and result["outcome"] == "succeeded"
            and task.get("active_attempt_id") is None
            and task.get("active_interaction_id") is None
            and task.get("cancel_requested_at") is None
            and len(attempts) == len(plan.leaf_nodes)
            and str(attempts[-1]["id"]) == result["attempt_id"]
            and task["current_step"] == len(plan.leaf_nodes) - 1,
            "tree_execution_incomplete",
        )
        events = await repo.list_record_events(
            session_id=str(task["session_id"]),
            types=(
                "method/scopeCreated",
                "method/expanded",
                "method/returned",
                "method/permissionsConfirmed",
                "method/permissionsRevoked",
            ),
        )
        events = [e for e in events if str(e.get("task_id")) == str(task["id"])]
        model_events = await repo.list_record_events(
            session_id=str(task["session_id"]),
            types=("turn/started", "turn/completed", "thread/tokenUsage/updated"),
        )
        _require(not model_events, "unexpected_tree_model_execution")
        _require(
            Decimal(str(task["budget"].get("used", "0")))
            == await task_script_cost(repo, task["id"])
            and _zero(task["budget"].get("reserved", "0")),
            "unexpected_tree_cost",
        )
        root = await repo.call_by_key(
            owner, str(task["session_id"]), "task:" + str(task["id"])
        )
        _require(
            root is not None
            and root["kind"] == "method"
            and root["origin"] == "method"
            and root["status"] == "succeeded"
            and root.get("parent_call_id") is None
            and root.get("attempt_id") is None
            and str(root["owner_actor_id"]) == str(owner)
            and str(root["session_id"]) == str(task["session_id"])
            and str(root["task_id"]) == str(task["id"])
            and root["digest"] == digest(["task:" + str(task["id"])])
            and root["detail"].get("identity_basis") == "host_task"
            and root["detail"].get("task_id") == str(task["id"]),
            "tree_root_call_mismatch",
        )
        assert root is not None and run is not None
        tree = _Tree(
            repo, owner, task, snapshot, plan, attempts, events, run, request, evidence
        )
        tree.calls[()] = root
        await tree.scope((), root, snapshot.method_id, None)
        stage = "nodes"
        outputs = await tree.visit(
            snapshot.draft.definition, snapshot.arguments, (), root
        )
        _result(root["detail"].get("exports"), outputs)
        root_artifact = await _artifact(
            repo,
            task,
            root["detail"].get("exports_artifact"),
            node_artifact((), exports=True),
            outputs,
        )
        tree.artifacts.append(root_artifact["id"])
        _require(
            attempts[-1]["refs"].get("formal_exports")
            == {
                "outputs": outputs,
                "outputs_digest": digest(outputs),
                "artifact": root_artifact,
            }
            and result.get("call_id") == attempts[-1]["refs"].get("capability_call_id"),
            "root_export_projection_mismatch",
        )
        stage = "inventory"
        calls = await repo.list_task_calls(owner, str(task["id"]))
        expected_calls = {str(c["id"]) for c in tree.calls.values()} | {
            s["call_id"] for s in evidence["steps"]
        }
        _require(
            len(calls) == len(expected_calls)
            and {str(c["id"]) for c in calls} == expected_calls,
            "unexpected_tree_call_inventory",
        )
        artifacts = await repo.list_artifacts(str(task["id"]))
        _require(
            len(artifacts) == len(tree.artifacts)
            and {str(a["id"]) for a in artifacts} == set(tree.artifacts),
            "unexpected_tree_artifact_inventory",
        )
        stage = "assertions"
        assertions = evaluate_assertions(case, outputs)
        passed = case.expected.outcome == "succeeded" and all(
            a.passed for a in assertions
        )
        _require(
            result["assertions"] == [a.model_dump(mode="json") for a in assertions]
            and result["status"] == ("passed" if passed else "failed")
            and result.get("rejection_code") is None,
            "tree_assertion_or_verdict_mismatch",
        )
        evidence.update(
            validated=True,
            outputs=outputs,
            outputs_digest=digest(outputs),
            root_call_id=str(root["id"]),
            root_call=_call_fact(root),
            root_exports_artifact=root_artifact,
            root_scope_facts=tree.scope_facts[()][2],
            model_turn_ids=[],
            task={
                "id": str(task["id"]),
                "state": task["state"],
                "budget": task["budget"],
                "current_step": task["current_step"],
                "scope_deadline": tree.deadline.isoformat(),
                "run_created_at": _time(run["created_at"]).isoformat(),
            },
            tree={
                "method_kind": plan.method_kind,
                "depth": plan.depth,
                "expansions": plan.expansions,
                "steps": plan.steps,
                "leaf_count": len(plan.leaf_nodes),
            },
        )
    except ValueError as exc:
        code = (
            str(exc)
            if str(exc).replace("_", "").isalnum() and len(str(exc)) < 100
            else "invalid_persisted_evidence"
        )
        evidence["errors"].append(f"{stage}:{code}")
    except (
        WorkbenchError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        InvalidOperation,
        StopIteration,
    ):
        evidence["errors"].append(f"{stage}:missing_or_invalid_persisted_evidence")
    return evidence
